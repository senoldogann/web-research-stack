"""Lightweight citation faithfulness verifier.

After LLM synthesis, this module checks that each [N] citation in the
answer text is actually supported by the corresponding source content.
It does NOT call an LLM — it uses keyword-overlap heuristics that are
fast, free, and language-agnostic.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "can",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "not",
        "no",
        "nor",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "then",
        "there",
        "here",
    }
)


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens, excluding stopwords and very short tokens."""
    tokens = re.findall(r"[a-z][a-z0-9]*", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 2]


def _claim_windows(text: str) -> Iterator[tuple[int, str]]:
    """Yield (citation_number, claim_window) for each [N] occurrence in text.

    The 'claim window' is the sentence or phrase immediately before the marker —
    the text span the citation is supposed to support.
    """
    for m in re.finditer(r"\[(\d+)\]", text):
        num = int(m.group(1))
        prefix = text[max(0, m.start() - 300) : m.start()]
        # Clip to the nearest sentence boundary inside the prefix
        boundary = max(
            prefix.rfind(". "),
            prefix.rfind(".\n"),
            prefix.rfind("! "),
            prefix.rfind("? "),
        )
        if boundary >= 0:
            prefix = prefix[boundary + 2 :]
        yield num, prefix.strip()


def _jaccard(a: list[str], b: list[str]) -> float:
    """Jaccard similarity between two token sequences."""
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    union = len(set_a | set_b)
    return len(set_a & set_b) / union if union > 0 else 0.0


def verify_citations(
    synthesis_text: str,
    source_contents: list[str],
    min_overlap_threshold: float = 0.04,
) -> list[dict[str, Any]]:
    """Check each [N] citation in *synthesis_text* against *source_contents[N-1]*.

    Args:
        synthesis_text:       Synthesised answer with ``[N]`` markers.
        source_contents:      Parallel list of raw source content strings.
        min_overlap_threshold: Jaccard threshold below which a citation is "weak".

    Returns:
        One audit dict per ``[N]`` occurrence::

            {
                "citation_num": int,
                "claim":        str,   # text window before the marker (≤120 chars)
                "overlap":      float, # Jaccard score
                "supported":    bool,
                "reason":       str,   # "ok" | "out_of_range" | "empty_source" | "weak"
            }
    """
    results: list[dict[str, Any]] = []

    for citation_num, claim in _claim_windows(synthesis_text):
        source_idx = citation_num - 1

        if source_idx < 0 or source_idx >= len(source_contents):
            results.append(
                {
                    "citation_num": citation_num,
                    "claim": claim[:120],
                    "overlap": 0.0,
                    "supported": False,
                    "reason": "out_of_range",
                }
            )
            continue

        source_text = source_contents[source_idx] or ""
        if not source_text.strip():
            results.append(
                {
                    "citation_num": citation_num,
                    "claim": claim[:120],
                    "overlap": 0.0,
                    "supported": False,
                    "reason": "empty_source",
                }
            )
            continue

        claim_tokens = _tokenize(claim)
        # First 5 000 chars of source is plenty for overlap checking
        source_tokens = _tokenize(source_text[:5000])

        overlap = _jaccard(claim_tokens, source_tokens)
        supported = overlap >= min_overlap_threshold
        results.append(
            {
                "citation_num": citation_num,
                "claim": claim[:120],
                "overlap": round(overlap, 4),
                "supported": supported,
                "reason": "ok" if supported else "weak",
            }
        )

    return results


def citation_audit_summary(
    synthesis_text: str,
    source_contents: list[str],
) -> dict[str, Any]:
    """Return a high-level audit report for *synthesis_text*.

    Args:
        synthesis_text:  Synthesised answer string.
        source_contents: Parallel list of source content strings.

    Returns:
        ::

            {
                "total_citations":     int,
                "supported_citations": int,
                "weak_citations":      list[dict],   # items with supported=False
                "faithfulness_score":  float,        # supported / total (0-1)
            }
    """
    items = verify_citations(synthesis_text, source_contents)
    total = len(items)
    supported_count = sum(1 for i in items if i["supported"])

    return {
        "total_citations": total,
        "supported_citations": supported_count,
        "weak_citations": [i for i in items if not i["supported"]],
        "faithfulness_score": round(supported_count / total, 3) if total > 0 else 1.0,
    }
