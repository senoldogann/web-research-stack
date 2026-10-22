"""Multi-angle discovery, source typing, and diversity-aware ranking.

A search provider answers one query per call. General discovery needs more than
the top hits of a single phrasing: current material, historical background, and
different kinds of source. The service asks a small set of query angles and
merges them here with deterministic, explainable ranking instead of trusting a
provider's order.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from web_scraper.web_tools.search import SearchResult

DEPTHS = ("fast", "discover", "deep")
ANGLE_ORDER = {"base": 0, "current": 1, "background": 2, "primary": 3}


def _normalise_token(token: str) -> str:
    folded = unicodedata.normalize("NFKD", token.casefold())
    without_marks = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    return without_marks.replace("ı", "i")


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "best",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "vs",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "bir",
        "bu",
        "da",
        "de",
        "daha",
        "en",
        "için",
        "ile",
        "mi",
        "mı",
        "mu",
        "mü",
        "nasıl",
        "ne",
        "nedir",
        "olan",
        "olarak",
        "şu",
        "çok",
    }
)
_TOKEN_RE = re.compile(r"[^\W_][\w.+-]+", re.UNICODE)
_YEAR_RE = re.compile(r"\b(?:19[89]\d|20[0-4]\d)\b")
_STOPWORD_KEYS = frozenset(_normalise_token(word) for word in _STOPWORDS)

_TECH_HINTS = frozenset(
    {
        "angular",
        "ansible",
        "anthropic",
        "api",
        "async",
        "asyncio",
        "authentication",
        "aws",
        "azure",
        "claude",
        "cli",
        "compiler",
        "config",
        "css",
        "database",
        "django",
        "docker",
        "embedding",
        "fastapi",
        "flask",
        "framework",
        "gcp",
        "git",
        "github",
        "golang",
        "graphql",
        "grpc",
        "huggingface",
        "html",
        "http",
        "install",
        "javascript",
        "json",
        "jwt",
        "kubernetes",
        "kotlin",
        "langchain",
        "library",
        "llm",
        "mcp",
        "mongodb",
        "mysql",
        "next.js",
        "nextjs",
        "node",
        "nodejs",
        "numpy",
        "oauth",
        "openai",
        "pandas",
        "playwright",
        "postgresql",
        "pydantic",
        "pytest",
        "python",
        "pytorch",
        "rag",
        "react",
        "redis",
        "rust",
        "sdk",
        "selenium",
        "server",
        "sklearn",
        "sql",
        "sqlalchemy",
        "sqlite",
        "swift",
        "tensorflow",
        "terraform",
        "typescript",
        "vue",
        "websocket",
        "yaml",
    }
)

_OFFICIAL_HOSTS = frozenset(
    {
        "ai.google.dev",
        "angular.io",
        "cloud.google.com",
        "developer.apple.com",
        "developer.mozilla.org",
        "docs.anthropic.com",
        "docs.aws.amazon.com",
        "docs.djangoproject.com",
        "docs.docker.com",
        "docs.github.com",
        "docs.npmjs.com",
        "docs.oracle.com",
        "docs.pydantic.dev",
        "docs.python.org",
        "docs.rs",
        "docs.sqlalchemy.org",
        "doc.rust-lang.org",
        "fastapi.tiangolo.com",
        "flask.palletsprojects.com",
        "go.dev",
        "huggingface.co",
        "kotlinlang.org",
        "kubernetes.io",
        "learn.microsoft.com",
        "mongodb.com",
        "nextjs.org",
        "nodejs.org",
        "pkg.go.dev",
        "platform.openai.com",
        "postgresql.org",
        "pytorch.org",
        "python.org",
        "react.dev",
        "redis.io",
        "tensorflow.org",
        "typescriptlang.org",
        "vuejs.org",
    }
)
# Public bodies whose hosts carry no government suffix: every EU institution
# under europa.eu, the UK government, and Finnish authorities and public
# services (Yle is the public broadcaster, Oamk a public university of applied
# sciences, ouka.fi the City of Oulu).
_PUBLIC_SECTOR_HOSTS = frozenset(
    {
        "dvv.fi",
        "eduskunta.fi",
        "europa.eu",
        "gov.uk",
        "kela.fi",
        "oamk.fi",
        "ouka.fi",
        "stat.fi",
        "traficom.fi",
        "vero.fi",
        "yle.fi",
    }
)
_GOVERNMENT_SUFFIX = re.compile(r"(?:^|\.)(?:gov|mil|int)$|(?:^|\.)gov\.[a-z]{2}$")
_ACADEMIC_HOSTS = frozenset(
    {
        "aalto.fi",
        "abo.fi",
        "acm.org",
        "arxiv.org",
        "biorxiv.org",
        "helsinki.fi",
        "ieee.org",
        "jstor.org",
        "jyu.fi",
        "lappeenranta.fi",
        "lut.fi",
        "medrxiv.org",
        "nature.com",
        "ncbi.nlm.nih.gov",
        "oulu.fi",
        "pubmed.ncbi.nlm.nih.gov",
        "science.org",
        "springer.com",
        "ssrn.com",
        "tuni.fi",
        "utu.fi",
        "uwasa.fi",
    }
)
_ACADEMIC_SUFFIX = re.compile(r"\.edu$|\.(?:edu|ac)\.[a-z]{2}$")
_NEWS_HOSTS = frozenset(
    {
        "aljazeera.com",
        "apnews.com",
        "axios.com",
        "bbc.co.uk",
        "bbc.com",
        "bloomberg.com",
        "cnbc.com",
        "cnn.com",
        "economist.com",
        "ft.com",
        "npr.org",
        "nytimes.com",
        "politico.com",
        "reuters.com",
        "theguardian.com",
        "washingtonpost.com",
        "wsj.com",
    }
)
_REFERENCE_HOSTS = frozenset(
    {
        "britannica.com",
        "ourworldindata.org",
        "pewresearch.org",
        "scholar.google.com",
        "statista.com",
        "wikipedia.org",
    }
)
_CODE_HOSTS = frozenset({"bitbucket.org", "codeberg.org", "github.com", "gitlab.com"})
_COMMUNITY_HOSTS = frozenset(
    {
        "dev.to",
        "discourse.org",
        "hashnode.com",
        "lobste.rs",
        "medium.com",
        "news.ycombinator.com",
        "reddit.com",
        "stackexchange.com",
        "stackoverflow.com",
    }
)

_AUTHORITY = {
    "official": 0.24,
    "academic": 0.20,
    "news": 0.14,
    "reference": 0.10,
    "code": 0.06,
    "community": 0.04,
    "other": 0.0,
}
# The score of a result matching about a third of the query from a single
# angle's middle ranks; below it, a result is listed only when nothing
# relevant scores higher.
MIN_RESULT_SCORE = 0.25


@dataclass(frozen=True)
class SearchAngle:
    """One query the discovery strategy sends to the provider."""

    name: str
    query: str


@dataclass
class DiscoveryCandidate:
    """A deduplicated result plus the angle ranks that surfaced it."""

    result: SearchResult
    angle_ranks: dict[str, int] = field(default_factory=dict)


def build_search_angles(
    query: str,
    depth: str,
    *,
    max_length: int = 500,
    year: Optional[int] = None,
) -> list[SearchAngle]:
    """Return the query angles for a depth.

    ``fast`` keeps the caller's phrasing alone. ``discover`` adds the current
    year and a historical/background angle. ``deep`` adds an official or
    primary-source angle. A suffix that would overflow ``max_length`` truncates
    the base rather than dropping the angle.
    """
    normalized_depth = (depth or "discover").strip().lower()
    if normalized_depth not in DEPTHS:
        raise ValueError(f"Unknown search depth: {depth}")

    base = " ".join((query or "").split())
    if not base:
        raise ValueError("Query must not be empty")

    angles = [SearchAngle("base", base[:max_length].rstrip())]
    if normalized_depth == "fast":
        return angles

    current_year = year or datetime.now().year
    if str(current_year) not in base:
        current = _fit_suffix(base, str(current_year), max_length)
        if current and current.casefold() != base.casefold():
            angles.append(SearchAngle("current", current))

    background = _fit_suffix(base, "history background", max_length)
    if background:
        angles.append(SearchAngle("background", background))

    if normalized_depth == "deep":
        suffix = "official documentation" if _is_technical_query(base) else "primary sources"
        primary = _fit_suffix(base, suffix, max_length)
        if primary:
            angles.append(SearchAngle("primary", primary))

    return angles


def query_tokens(text: str) -> set[str]:
    """Tokenise a query or result for deterministic lexical overlap.

    Turkish letters are folded to their ASCII equivalents so a query written
    with ``İ``/``ı`` matches a result written with ``i`` (and vice versa).
    """
    tokens = {
        _normalise_token(match.group(0).strip(".-+")) for match in _TOKEN_RE.finditer(text or "")
    }
    meaningful = {token for token in tokens if len(token) > 1 and token not in _STOPWORD_KEYS}
    return meaningful or {token for token in tokens if len(token) > 1}


def relevance_score(query: str, title: str, snippet: str, url: str) -> float:
    """Share of meaningful query tokens that appear in the result."""
    wanted = query_tokens(query)
    if not wanted:
        return 0.0
    found = query_tokens(f"{title} {snippet} {url}")
    return len(wanted & found) / len(wanted)


def infer_publication_year(
    title: str,
    snippet: str,
    url: str,
    *,
    year_now: Optional[int] = None,
) -> Optional[int]:
    """Infer a publication year from what a provider exposes.

    DuckDuckGo's HTML results carry no structured date, so the newest plausible
    year in the title, snippet, or URL stands in. ``None`` means unknown, not
    old; callers must not treat it as a freshness signal.
    """
    upper = (year_now or datetime.now().year) + 1
    years = [
        int(match.group(0))
        for text in (title, snippet, url)
        for match in _YEAR_RE.finditer(text or "")
        if 1980 <= int(match.group(0)) <= upper
    ]
    return max(years) if years else None


def classify_recency(year: Optional[int], *, year_now: Optional[int] = None) -> str:
    """Map an inferred year to current, recent, historical, or unknown."""
    if year is None:
        return "unknown"
    current = year_now or datetime.now().year
    if year >= current - 1:
        return "current"
    if year >= current - 4:
        return "recent"
    return "historical"


def classify_source_type(url: str) -> str:
    """Classify a result URL as official, academic, news, reference, code, or community."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").lower()
    except ValueError:
        return "other"
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "other"

    if (
        _host_matches(host, _OFFICIAL_HOSTS)
        or _host_matches(host, _PUBLIC_SECTOR_HOSTS)
        or _GOVERNMENT_SUFFIX.search(host)
    ):
        return "official"
    if _host_matches(host, _ACADEMIC_HOSTS) or _ACADEMIC_SUFFIX.search(host):
        return "academic"
    if _host_matches(host, _NEWS_HOSTS):
        return "news"
    if _host_matches(host, _REFERENCE_HOSTS):
        return "reference"
    if _host_matches(host, _CODE_HOSTS) or host.endswith(".github.io"):
        return "code"
    if _host_matches(host, _COMMUNITY_HOSTS):
        return "community"
    if any(
        segment in path
        for segment in ("/docs/", "/documentation/", "/api/", "/reference/", "/manual/")
    ):
        return "official"
    return "other"


def annotate_candidate(
    query: str,
    candidate: DiscoveryCandidate,
    *,
    year_now: Optional[int] = None,
) -> dict[str, Any]:
    """Score one candidate and expose the signals behind the score."""
    result = candidate.result
    url = result.url
    source_type = classify_source_type(url)
    year = infer_publication_year(
        result.title,
        result.snippet,
        url,
        year_now=year_now,
    )
    recency = classify_recency(year, year_now=year_now)
    relevance = relevance_score(query, result.title, result.snippet, url)
    angles = sorted(
        candidate.angle_ranks,
        key=lambda name: (ANGLE_ORDER.get(name, 99), candidate.angle_ranks[name]),
    )

    rrf = sum(1.0 / (10.0 + rank) for rank in candidate.angle_ranks.values())
    score = 0.55 * relevance + 1.8 * rrf + _AUTHORITY[source_type]
    score += {"current": 0.10, "recent": 0.05}.get(recency, 0.0)
    if recency == "historical":
        score += 0.08 if source_type in {"official", "academic", "reference"} else 0.04
    if _has_exact_phrase(query, result.title, result.snippet):
        score += 0.10
    if "current" in candidate.angle_ranks and recency == "current":
        score += 0.05
    if (
        "background" in candidate.angle_ranks
        and recency in {"historical", "unknown"}
        and source_type in {"official", "academic", "reference"}
    ):
        score += 0.05
    if "primary" in candidate.angle_ranks and source_type == "official":
        score += 0.08

    return {
        "title": result.title,
        "url": url,
        "snippet": result.snippet,
        "source": result.source,
        "domain": _domain(url),
        "source_type": source_type,
        "year": year,
        "recency": recency,
        "angles": angles,
        "relevance": round(relevance, 6),
        "score": round(score, 6),
    }


def has_relevant_result(query: str, items: Iterable[dict[str, Any]]) -> bool:
    """Whether any annotated result shares a word of the query.

    Bare numbers do not count when the query has words too: a year or a code
    such as ``9237`` also appears on pages about something else entirely.
    """
    wanted = query_tokens(query)
    words = {token for token in wanted if not token.isdigit()} or wanted
    return any(
        words & query_tokens(f"{item['title']} {item['snippet']} {item['url']}") for item in items
    )


def rank_candidates(
    query: str,
    candidates: Iterable[DiscoveryCandidate],
    limit: int,
    *,
    year_now: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Rank candidates by relevance, agreement, authority, and recency, then diversify.

    Results that share nothing with the query are dropped, and when nothing
    relevant is left the list is empty: an empty answer is honest, a page of
    unrelated links is not. Relevant results scoring under
    ``MIN_RESULT_SCORE`` are dropped beside better ones and kept only when no
    relevant result reaches it.
    """
    annotated = [
        annotate_candidate(query, candidate, year_now=year_now) for candidate in candidates
    ]
    if not has_relevant_result(query, annotated):
        return []
    relevant = [item for item in annotated if item["relevance"] > 0]
    eligible = [item for item in relevant if item["score"] >= MIN_RESULT_SCORE] or relevant

    eligible.sort(
        key=lambda item: (
            -item["score"],
            ANGLE_ORDER.get(item["angles"][0] if item["angles"] else "", 99),
            item["url"],
        )
    )
    return _select_diverse(eligible, limit)


def domain_cap(limit: int) -> int:
    """Maximum results from one domain before diversity relaxation."""
    return max(1, math.ceil(max(limit, 1) / 3))


def build_facets(
    results: list[dict[str, Any]],
    *,
    total_candidates: int,
    limit: int,
) -> dict[str, Any]:
    """Summarise source-type, recency, domain, and angle coverage."""
    angle_counts = Counter(angle for item in results for angle in item.get("angles", []))
    return {
        "source_types": _ordered_counts(item["source_type"] for item in results),
        "recency": _ordered_counts(item["recency"] for item in results),
        "domains": _ordered_counts(item["domain"] for item in results),
        "angles": _ordered_counts(angle_counts.elements()),
        "total_candidates": total_candidates,
        "domain_cap": domain_cap(limit),
    }


def _select_diverse(
    ranked: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    remaining = list(ranked)
    selected: list[dict[str, Any]] = []
    per_domain: Counter[str] = Counter()
    per_type: Counter[str] = Counter()
    cap = domain_cap(limit)

    while remaining and len(selected) < limit:
        best_index = _best_index(remaining, per_domain, per_type, cap)
        if best_index is None:
            best_index = _best_index(remaining, per_domain, per_type, None)
        if best_index is None:
            break
        chosen = remaining.pop(best_index)
        selected.append(chosen)
        per_domain[chosen["domain"]] += 1
        per_type[chosen["source_type"]] += 1
    return selected


def _best_index(
    remaining: list[dict[str, Any]],
    per_domain: Counter[str],
    per_type: Counter[str],
    cap: Optional[int],
) -> Optional[int]:
    best_index: Optional[int] = None
    best_effective: Optional[float] = None
    for index, item in enumerate(remaining):
        if cap is not None and per_domain[item["domain"]] >= cap:
            continue
        type_count = per_type[item["source_type"]]
        effective = item["score"] + (0.04 if type_count == 0 else -0.02 * type_count)
        if best_effective is None or effective > best_effective:
            best_index = index
            best_effective = effective
    return best_index


def _fit_suffix(base: str, suffix: str, max_length: int) -> str:
    room = max_length - len(suffix) - 1
    if room < 1:
        return ""
    return f"{base[:room].rstrip()} {suffix}"


def _is_technical_query(query: str) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", query.casefold()) for word in _TECH_HINTS)


def _host_matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _has_exact_phrase(query: str, *texts: str) -> bool:
    needle = " ".join((query or "").casefold().split())
    if not needle:
        return False
    return any(needle in " ".join((text or "").casefold().split()) for text in texts)


def _ordered_counts(values: Iterable[str]) -> dict[str, int]:
    counts = Counter(value for value in values if value)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
