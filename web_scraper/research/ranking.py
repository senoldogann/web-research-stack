"""Search result scoring, ranking, and deduplication utilities.

All functions are stateless module-level callables.
"""

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from web_scraper.config import config
from web_scraper.research.constants import BLACKLISTED_DOMAINS, TRUSTED_DOMAINS, TECH_DOC_URLS
from web_scraper.research.url_utils import extract_result_domain, normalize_result_url

ResearchProfile = Literal["technical", "news", "academic"]


def tokenize_for_ranking(text: str) -> set[str]:
    """Tokenize short text for deterministic lexical overlap scoring."""
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{1,}", (text or "").lower())
        if len(token) > 1
    }


def get_freshness_score(result: dict) -> float:
    """Calculate a freshness score based on the result's publication date.

    Returns:
        +0.20 for 2025-2026 (very fresh)
        +0.10 for 2024 (recent)
          0.00 for 2023 (neutral)
        -0.10 for 2022
        -0.15 for 2021 or older
          0.00 if date is unknown (no penalty)
    """
    publication_date = result.get("publication_date")

    if not publication_date:
        snippet = result.get("snippet", "")
        year_match = re.search(r"\b(202[0-9]|201[9-9])\b", snippet)
        if year_match:
            try:
                publication_date = year_match.group(1)
            except Exception:  # noqa: S110
                pass

    if not publication_date:
        return 0.0

    year: int | None = None
    try:
        if isinstance(publication_date, str) and len(publication_date) >= 4:
            year = int(publication_date[:4])
    except (ValueError, TypeError):
        return 0.0

    if not year:
        return 0.0

    year_diff = datetime.now().year - year

    if year_diff <= 1:
        return 0.20
    elif year_diff == 2:
        return 0.10
    elif year_diff == 3:
        return 0.0
    elif year_diff == 4:
        return -0.10
    else:
        return -0.15


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
    
    # Check if query contains any known tech keywords to adjust ranking behavior
    is_tech_query = any(re.search(rf"\b{re.escape(kw)}\b", query.lower()) for kw in TECH_DOC_URLS.keys())
    
    if isinstance(url, str):
        try:
            hostname = urlsplit(url).netloc.lower()
            if hostname.startswith("www."):
                hostname = hostname[4:]
            
            # 1. Tech Query Mode: Prioritize documentation, neutralize government/edu noise
            if is_tech_query:
                is_official_doc = False
                for kw, doc_urls in TECH_DOC_URLS.items():
                    if re.search(rf"\b{re.escape(kw)}\b", query.lower()):
                        if any(hostname in urlsplit(doc_url).netloc.lower() for doc_url in doc_urls):
                            is_official_doc = True
                            break
                            
                if is_official_doc:
                    domain_boost = 0.6  # Massive boost for official docs
                elif hostname in TRUSTED_DOMAINS:
                    domain_boost = 0.2  # Slight boost, but docs are better
                elif any(hostname.endswith(f".{tld}") for tld in ["gov", "edu", "int"]):
                    domain_boost = 0.0  # NO BOOST for gov/edu in tech queries!
                elif hostname.endswith(".org"):
                    domain_boost = 0.1
            
            # 2. Standard Mode: Default trusted domain scaling
            else:
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
    else:
        if any(token in url for token in ["docs", "developer", "api", "readthedocs"]):
            profile_adjustment += 0.12

    return round(
        overlap_score
        + exact_query_boost
        + provider_boost
        + domain_boost
        + freshness_score
        + profile_adjustment,
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

            normalized_url = normalize_result_url(url)

            domain_boost = 0.0
            is_tech_query = any(re.search(rf"\b{re.escape(kw)}\b", query.lower()) for kw in TECH_DOC_URLS.keys())
            
            try:
                hostname = urlsplit(normalized_url).netloc.lower()
                if hostname.startswith("www."):
                    hostname = hostname[4:]
                if hostname in BLACKLISTED_DOMAINS:
                    continue

                if is_tech_query:
                    is_official_doc = False
                    for kw, doc_urls in TECH_DOC_URLS.items():
                        if re.search(rf"\b{re.escape(kw)}\b", query.lower()):
                            if any(hostname in urlsplit(doc_url).netloc.lower() for doc_url in doc_urls):
                                is_official_doc = True
                                break
                                
                    if is_official_doc:
                        domain_boost = 0.8
                    elif hostname in TRUSTED_DOMAINS:
                        domain_boost = 0.3
                    elif any(hostname.endswith(f".{tld}") for tld in ["gov", "edu", "int"]):
                        domain_boost = 0.0  # ZERO BOOST for gov/edu on tech queries
                    elif hostname.endswith(".org"):
                        domain_boost = 0.1
                else:
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

            candidate["rank_score"] = round(
                score_search_result(
                    query,
                    candidate,
                    research_profile=research_profile,
                )
                + domain_boost,
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
) -> list[dict]:
    """Top-up AI-selected sources with unused search results when needed.

    When a query is provided, fallback candidates are filtered so that only
    results whose title *or* snippet contain at least one meaningful query
    keyword are added.  This prevents unrelated StackOverflow / generic Q&A
    pages from polluting the source list.
    """
    # Build a set of meaningful query keywords (ignore short stop-words)
    _STOP = {
        "a",
        "an",
        "the",
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
        "ne",
        "mi",
        "ve",
        "ile",
        "da",
        "de",
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

    def _is_relevant(result: dict) -> bool:
        if not topic_keywords:
            return True
        haystack = (
            (result.get("title") or "")
            + " "
            + (result.get("snippet") or "")
            + " "
            + (result.get("url") or "")
        ).lower()
        return any(kw in haystack for kw in topic_keywords)

    unique_sources: list[dict] = []
    seen_urls: set[str] = set()

    for source in selected_sources:
        url = source.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique_sources.append(source)

    for result in fallback_results:
        if len(unique_sources) >= target_count:
            break
        url = result.get("url")
        if not url or url in seen_urls:
            continue
        if not _is_relevant(result):
            continue  # skip off-topic fallback results
        seen_urls.add(url)
        unique_sources.append(
            {
                "type": result.get("source", "unknown"),
                "url": url,
                "title": result.get("title", ""),
                "priority": len(unique_sources) + 1,
            }
        )

    return unique_sources[:target_count]
