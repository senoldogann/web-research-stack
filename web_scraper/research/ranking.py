"""Search result scoring, ranking, and deduplication utilities.

All functions are stateless module-level callables.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from web_scraper.config import config
from web_scraper.research.constants import BLACKLISTED_DOMAINS, TRUSTED_DOMAINS
from web_scraper.research.url_utils import (
    classify_source_tier,
    extract_result_domain,
    normalize_result_url,
)

ResearchProfile = Literal["technical", "news", "academic", "general"]
IntentClass = Literal[
    "current_events",
    "model_release",
    "technical_docs",
    "benchmark_compare",
    "evergreen_general",
]


def tokenize_for_ranking(text: str) -> set[str]:
    """Tokenize short text for deterministic lexical overlap scoring."""
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{1,}", (text or "").lower())
        if len(token) > 1
    }


_SOFT_ERROR_PATTERNS: tuple[str, ...] = (
    "wordpress › hata",
    "wordpress › error",
    "404",
    "not found",
    "page not found",
    "sayfa bulunamadı",
    "sayfa bulunamadi",
    "forbidden",
    "access denied",
    "just a moment",
    "enable javascript and cookies",
    "captcha",
    "domain parked",
    "buy this domain",
    "coming soon",
)

_CF_CHALLENGE_PATTERNS: tuple[str, ...] = (
    "cloudflare",
    "ray id",
    "attention required",
    "verify you are human",
    "checking your browser",
    "just a moment",
    "enable javascript and cookies",
)

_MODEL_RELEASE_INTENT_TOKENS: tuple[str, ...] = (
    "latest model",
    "new model",
    "best llm",
    "llm leaderboard",
    "model leaderboard",
    "model release",
    "release notes",
    "model version",
    "newest model",
    "gpt",
    "claude",
    "opus",
    "llm",
    "model",
    "version",
    "release",
    "benchmark",
    "leaderboard",
    "yeni model",
    "son model",
    "model sürümü",
    "model surumu",
    "sürüm",
    "surum",
    "yayınlandı",
    "cikti",
    "çıktı",
)


def is_soft_error_result(result: dict) -> bool:
    """Return True when a search result appears to be an error/placeholder page."""
    title = str(result.get("title", "") or "").lower()
    snippet = str(result.get("snippet", "") or "").lower()
    url = str(result.get("url", "") or "").lower()
    combined = f"{title} {snippet}"

    if any(pattern in combined for pattern in _SOFT_ERROR_PATTERNS):
        return True

    if "docs.cloudflare.com" in url:
        return False

    # Avoid broad false positives: treat "cloudflare" as an error only when
    # multiple challenge-page signatures appear together.
    if "cloudflare" in combined:
        challenge_hits = sum(pattern in combined for pattern in _CF_CHALLENGE_PATTERNS)
        if challenge_hits >= 2:
            return True

    # Very common low-value placeholders / login walls
    if any(token in url for token in ("/wp-login", "/wp-admin", "/login", "/signin")):
        return True

    return False


def _is_model_release_intent_query(query: str) -> bool:
    q = (query or "").lower()
    return any(token in q for token in _MODEL_RELEASE_INTENT_TOKENS)


def _model_release_authority_boost(query: str, result: dict) -> float:
    """Boost official release pages for model/version/release queries."""
    if not _is_model_release_intent_query(query):
        return 0.0

    url = str(result.get("url", "") or "")
    title = str(result.get("title", "") or "")
    snippet = str(result.get("snippet", "") or "")

    boost = 0.0
    try:
        hostname = urlsplit(url).netloc.lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        path = urlsplit(url).path.lower()
        text = f"{title} {snippet}".lower()

        if hostname in {
            "openai.com",
            "platform.openai.com",
            "anthropic.com",
            "docs.anthropic.com",
            "support.claude.com",
            "ai.google.dev",
        }:
            boost += 0.22
        elif hostname in {"lmarena.ai", "huggingface.co"}:
            boost += 0.14

        if any(token in path for token in ("/index/", "/news/", "/release", "/models", "/model")):
            boost += 0.06

        if any(
            token in text
            for token in (
                "release notes",
                "introducing",
                "announcing",
                "new model",
                "model card",
                "leaderboard",
                "benchmark",
            )
        ):
            boost += 0.04
    except Exception:  # noqa: S110
        return 0.0

    return min(boost, 0.35)


def _is_global_intent_query(query: str) -> bool:
    """Heuristic: does the query ask about global/worldwide scope?"""
    q = (query or "").lower()
    return any(
        token in q
        for token in (
            "world",
            "global",
            "international",
            "dünya",
            "dünyanın",
            "küresel",
            "worldwide",
        )
    )


def _result_quality_penalty(query: str, result: dict, research_profile: ResearchProfile) -> float:
    """Return additive penalty for low-quality / parochial results."""
    penalty = 0.0

    title = str(result.get("title", "") or "")
    snippet = str(result.get("snippet", "") or "")
    url = str(result.get("url", "") or "")

    if len(snippet.strip()) < 20:
        penalty += 0.04

    try:
        hostname = urlsplit(url).netloc.lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]

        # Slight penalty for lower-signal TLDs often used by content farms.
        if any(hostname.endswith(tld) for tld in (".biz", ".click", ".xyz", ".top")):
            penalty += 0.08

        # For global/general questions, avoid over-concentrating on local-only
        # domains unless they are explicitly trusted.
        if research_profile in {"general", "news"} and _is_global_intent_query(query):
            if hostname.endswith(".tr") and hostname not in TRUSTED_DOMAINS:
                penalty += 0.14 if research_profile == "news" else 0.10
    except Exception:  # noqa: S110
        pass

    # Clickbait-ish title hints
    lower_title = title.lower()
    if any(tok in lower_title for tok in ("bakın", "sok", "şok", "inanamayacaksınız", "you won't believe")):
        penalty += 0.06

    return penalty


def get_freshness_score(result: dict) -> float:
    """Calculate a freshness score based on the result's publication date.

    Returns:
        +0.25 for current year (very fresh)
        +0.20 for 1 year old (fresh)
        +0.10 for 2 years old (recent)
          0.00 for 3 years old (neutral)
        -0.10 for 4 years old
        -0.15 for 5 years old
        -0.20 for 6+ years old (stale)
        -0.05 if date is unknown (small penalty — undated content is likely older
               or low-quality; previously this was 0.0 which gave no incentive
               to prefer dated sources over undated ones)
    """
    publication_date = result.get("publication_date")

    if not publication_date:
        # Try to extract a year hint from the snippet (broaden the regex to
        # cover 2015–2029 so older stale pages are also caught)
        snippet = result.get("snippet", "")
        year_match = re.search(r"\b(201[5-9]|202[0-9])\b", snippet)
        if year_match:
            try:
                publication_date = year_match.group(1)
            except Exception:  # noqa: S110
                pass

    if not publication_date:
        # No date signal at all — apply a small penalty to prefer dated sources
        return -0.05

    year: int | None = None
    try:
        if isinstance(publication_date, str) and len(publication_date) >= 4:
            year = int(publication_date[:4])
    except (ValueError, TypeError):
        return -0.05

    if not year:
        return -0.05

    current_year = datetime.now().year
    year_diff = current_year - year

    if year_diff == 0:
        return 0.25  # current year — very fresh
    elif year_diff == 1:
        return 0.20  # 1 year old — fresh
    elif year_diff == 2:
        return 0.10  # 2 years old — recent
    elif year_diff == 3:
        return 0.0  # 3 years old — neutral
    elif year_diff == 4:
        return -0.10
    elif year_diff == 5:
        return -0.15
    else:
        return -0.20  # 6+ years old — clearly stale


def score_search_result(
    query: str,
    result: dict,
    research_profile: ResearchProfile = "technical",
) -> float:
    """Assign a lexical relevance score before diversity-aware reranking."""
    query_tokens = tokenize_for_ranking(query)
    searchable_tokens = (
        tokenize_for_ranking(result.get("title", ""))
        | tokenize_for_ranking(result.get("snippet", ""))
        | tokenize_for_ranking(result.get("source", ""))
    )

    overlap_score = (
        len(query_tokens & searchable_tokens) / len(query_tokens) if query_tokens else 0.0
    )

    exact_query_boost = (
        config.research_rerank_exact_query_boost
        if (result.get("search_query") or "").casefold() == query.casefold()
        else 0.0
    )
    provider_boost = 0.05 if result.get("search_provider") == "duckduckgo" else 0.0

    domain_boost = 0.0
    url = result.get("url", "")
    if isinstance(url, str):
        try:
            hostname = urlsplit(url).netloc.lower()
            if hostname.startswith("www."):
                hostname = hostname[4:]
            if hostname in TRUSTED_DOMAINS:
                domain_boost = 0.3
            elif any(hostname.endswith(f".{tld}") for tld in ["gov", "edu", "int"]):
                domain_boost = 0.3
            elif hostname.endswith(".org"):
                domain_boost = 0.15
            if any(token in hostname for token in query_tokens if len(token) > 3):
                domain_boost += 0.1
        except Exception:  # noqa: S110
            pass

    freshness_score = get_freshness_score(result)

    # Authority boost by source tier (1 = strongest).
    tier_boost_map = {1: 0.30, 2: 0.20, 3: 0.12, 4: 0.06, 5: 0.0}
    source_tier = classify_source_tier(str(result.get("url", "") or ""))
    authority_boost = tier_boost_map.get(source_tier, 0.0)

    profile_adjustment = 0.0
    source_name = str(result.get("source", "")).lower()
    url = str(result.get("url", "")).lower()
    is_academic = any(
        token in url or token in source_name
        for token in ["arxiv", "pubmed", "doi", "acm", "ieee", "springer"]
    )
    is_news = any(
        token in url or token in source_name
        for token in ["reuters", "apnews", "bbc", "cnn", "news", "bloomberg"]
    )

    if research_profile == "news":
        profile_adjustment += freshness_score * 0.8
        if is_news:
            profile_adjustment += 0.15
    elif research_profile == "academic":
        if is_academic:
            profile_adjustment += 0.2
        if any(url.endswith(tld) for tld in [".edu", ".gov", ".org"]):
            profile_adjustment += 0.08
    elif research_profile == "general":
        # General / encyclopaedic queries: boost freshness and diversity.
        # Prefer Wikipedia, encyclopaedic .org sites, reputable news sources
        # over pure tech docs.
        profile_adjustment += freshness_score * 0.5  # moderate freshness weight
        if "wikipedia" in url or "britannica" in url or "encyclopedia" in url:
            profile_adjustment += 0.20
        if any(url.endswith(tld) for tld in [".edu", ".gov"]):
            profile_adjustment += 0.10
        if is_news:
            profile_adjustment += 0.08  # news sources acceptable for general queries
    else:
        # technical
        if any(token in url for token in ["docs", "developer", "api", "readthedocs"]):
            profile_adjustment += 0.12

    quality_penalty = _result_quality_penalty(query, result, research_profile)
    model_release_boost = _model_release_authority_boost(query, result)

    return round(
        overlap_score
        + exact_query_boost
        + provider_boost
        + domain_boost
        + freshness_score
        + authority_boost
        + profile_adjustment
        + model_release_boost
        - quality_penalty,
        6,
    )


def merge_and_rank_search_results(
    query: str,
    result_sets: list[list[dict]],
    limit: int,
    research_profile: ResearchProfile = "technical",
) -> list[dict]:
    """Dedupe multi-provider results and prefer a diverse, relevant shortlist."""
    by_url: dict[str, dict] = {}
    query_tokens = tokenize_for_ranking(query)

    for result_set in result_sets:
        for result in result_set:
            url = result.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue

            # Filter obvious error / placeholder pages before ranking.
            if is_soft_error_result(result):
                continue

            normalized_url = normalize_result_url(url)

            domain_boost = 0.0
            try:
                hostname = urlsplit(normalized_url).netloc.lower()
                if hostname.startswith("www."):
                    hostname = hostname[4:]
                if hostname in BLACKLISTED_DOMAINS:
                    continue

                if hostname in TRUSTED_DOMAINS:
                    domain_boost = 0.5
                elif any(hostname.endswith(f".{tld}") for tld in ["gov", "edu", "int"]):
                    domain_boost = 0.4
                elif hostname.endswith(".org"):
                    domain_boost = 0.2

                if any(token in hostname for token in query_tokens if len(token) > 3):
                    domain_boost += 0.1
            except Exception:  # noqa: S110
                pass

            candidate = dict(result)
            candidate["url"] = normalized_url

            path = urlsplit(normalized_url).path.lower()
            if any(token in path for token in query_tokens if len(token) > 3):
                domain_boost += 0.05

            # Combine base score with domain authority boost.
            # Domain boost is intentionally capped at 0.35 (was effectively 0.5+)
            # so that freshness signals (~0.25 for current-year content) can
            # meaningfully compete with well-known but potentially stale domains.
            capped_domain_boost = min(domain_boost, 0.35)
            candidate["rank_score"] = round(
                score_search_result(
                    query,
                    candidate,
                    research_profile=research_profile,
                )
                + capped_domain_boost,
                6,
            )
            candidate.setdefault("search_provider", "unknown")

            existing = by_url.get(normalized_url)
            if existing is None:
                by_url[normalized_url] = candidate
                continue

            providers = {
                existing.get("search_provider", "unknown"),
                candidate.get("search_provider", "unknown"),
            }
            existing["search_provider"] = ",".join(sorted(providers))
            if candidate["rank_score"] > existing.get("rank_score", 0.0):
                candidate["search_provider"] = existing["search_provider"]
                by_url[normalized_url] = candidate

    remaining = list(by_url.values())
    selected: list[dict] = []
    domain_counts: dict[str, int] = {}

    while remaining and len(selected) < limit:
        best_index = 0
        best_score: float | None = None

        for index, candidate in enumerate(remaining):
            domain = extract_result_domain(candidate["url"])
            seen_count = domain_counts.get(domain, 0)
            diversity_adjustment = (
                config.research_rerank_domain_diversity_boost
                if seen_count == 0
                else (-config.research_rerank_same_domain_penalty * seen_count)
            )
            effective_score = candidate.get("rank_score", 0.0) + diversity_adjustment

            if best_score is None or effective_score > best_score:
                best_index = index
                best_score = effective_score

        chosen = remaining.pop(best_index)
        selected.append(chosen)
        chosen_domain = extract_result_domain(chosen["url"])
        domain_counts[chosen_domain] = domain_counts.get(chosen_domain, 0) + 1

    return selected


def expand_selected_sources(
    selected_sources: list[dict],
    fallback_results: list[dict],
    target_count: int,
    query: str = "",
    research_profile: ResearchProfile = "technical",
    intent_class: IntentClass | None = None,
) -> list[dict]:
    """Top-up AI-selected sources with unused search results when needed.

    When a query is provided, fallback candidates are filtered so that only
    results whose title *or* snippet contain at least one meaningful query
    keyword are added.  This prevents unrelated StackOverflow / generic Q&A
    pages from polluting the source list.
    """
    # Build a set of meaningful query keywords (ignore short stop-words).
    # We intentionally exclude generic superlatives so low-signal pages
    # ("best hashing algorithm", "top tools") do not leak into unrelated queries.
    _STOP = {
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "is",
        "are",
        "of",
        "in",
        "on",
        "for",
        "and",
        "or",
        "to",
        "vs",
        "which",
        "what",
        "who",
        "when",
        "where",
        "why",
        "how",
        "best",
        "top",
        "latest",
        "new",
        "now",
        "current",
        "today",
        "world",
        "global",
        "ne",
        "mi",
        "ve",
        "ile",
        "da",
        "de",
        "en",
        "son",
        "güncel",
        "guncel",
        "şu",
        "su",
        "hangi",
        "nedir",
        "kim",
    }
    _HIGH_SIGNAL_SHORT = {
        "llm",
        "gpt",
        "api",
        "gpu",
        "cpu",
        "ram",
        "ml",
        "nlp",
        "rag",
        "sql",
    }
    topic_keywords: set[str] = (
        {
            w.lower()
            for w in query.replace("-", " ").split()
            if len(w) > 2 and w.lower() not in _STOP
        }
        if query
        else set()
    )
    anchor_keywords: set[str] = {
        kw for kw in topic_keywords if len(kw) >= 4 or kw in _HIGH_SIGNAL_SHORT
    }
    model_release_priority_domains: set[str] = {
        "openai.com",
        "platform.openai.com",
        "help.openai.com",
        "anthropic.com",
        "docs.anthropic.com",
        "support.claude.com",
        "ai.google.dev",
        "lmarena.ai",
        "artificialanalysis.ai",
        "swebench.com",
        "huggingface.co",
    }

    def _is_intent_priority_result(result: dict) -> bool:
        if intent_class not in {"current_events", "model_release"}:
            return False

        url = str(result.get("url", "") or "")
        if not url:
            return False

        tier = classify_source_tier(url)
        if tier <= 3:
            return True

        if intent_class != "model_release":
            return False

        try:
            hostname = urlsplit(url).netloc.lower()
            if hostname.startswith("www."):
                hostname = hostname[4:]
        except Exception:  # noqa: S110
            return False

        return hostname in model_release_priority_domains

    def _is_relevant(result: dict) -> bool:
        # Cross-language resilience: keep intent-priority authoritative domains
        # even when lexical overlap is low (e.g. TR query + EN Reuters headline).
        if _is_intent_priority_result(result):
            return True

        if not topic_keywords:
            return True
        haystack = (
            (result.get("title") or "")
            + " "
            + (result.get("snippet") or "")
            + " "
            + (result.get("url") or "")
        ).lower()

        if anchor_keywords and not any(kw in haystack for kw in anchor_keywords):
            return False

        # When queries have many informative keywords, require stronger overlap.
        match_count = sum(1 for kw in topic_keywords if kw in haystack)
        if len(topic_keywords) >= 4 and match_count < 2:
            return False

        return match_count >= 1

    unique_sources: list[dict] = []
    seen_urls: set[str] = set()
    selected_domains: dict[str, int] = {}
    low_authority_added = 0

    if intent_class in {"current_events", "model_release"}:
        low_authority_cap = 2
        max_per_domain = 1
    elif intent_class == "benchmark_compare":
        low_authority_cap = 1
        max_per_domain = 1
    else:
        low_authority_cap = 2
        max_per_domain = 2

    for source in selected_sources:
        url = source.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        domain = extract_result_domain(url)
        selected_domains[domain] = selected_domains.get(domain, 0) + 1
        unique_sources.append(source)

    for result in fallback_results:
        if len(unique_sources) >= target_count:
            break
        url = result.get("url")
        if not url or url in seen_urls:
            continue
        if not _is_relevant(result):
            continue  # skip off-topic fallback results
        tier = classify_source_tier(str(url))
        domain = extract_result_domain(str(url))

        if selected_domains.get(domain, 0) >= max_per_domain:
            continue

        if tier >= 5 and low_authority_added >= low_authority_cap:
            continue

        if (
            research_profile in {"general", "news"}
            and _is_global_intent_query(query)
            and domain.endswith(".tr")
            and tier >= 5
        ):
            continue

        seen_urls.add(url)
        selected_domains[domain] = selected_domains.get(domain, 0) + 1
        if tier >= 5:
            low_authority_added += 1
        unique_sources.append(
            {
                "type": result.get("source", "unknown"),
                "url": url,
                "title": result.get("title", ""),
                "priority": len(unique_sources) + 1,
            }
        )

    if not unique_sources and fallback_results and intent_class in {"current_events", "model_release"}:
        for result in fallback_results:
            if len(unique_sources) >= min(target_count, 2):
                break
            url = result.get("url")
            if not url or url in seen_urls:
                continue
            if not _is_relevant(result):
                continue
            unique_sources.append(
                {
                    "type": result.get("source", "unknown"),
                    "url": url,
                    "title": result.get("title", ""),
                    "priority": len(unique_sources) + 1,
                }
            )
            seen_urls.add(url)

    return unique_sources[:target_count]
