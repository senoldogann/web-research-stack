"""Core ``ResearchAgent`` class — orchestrates the full research pipeline.

Inherits HTTP / LLM transport from ``LLMClient`` and delegates stateless
helpers (prompts, ranking, URL utilities, text utilities) to their own
modules so this file stays focused on orchestration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote_plus

import httpx

from web_scraper.async_scrapers import WebScraperAsync
from web_scraper.config import config
from web_scraper.content_safety import sanitize_scraped_text
from web_scraper.duckduckgo_search import get_best_sources_ddg
from web_scraper.google_search import get_best_sources
from web_scraper.research.citation_verifier import citation_audit_summary
from web_scraper.research.constants import SOURCE_TEMPLATES, STATUS_MESSAGES
from web_scraper.research.llm_client import LLMClient
from web_scraper.research.models import ResearchReport, ResearchResult
from web_scraper.research.profile_collectors import (
    collect_arxiv_results,
    collect_hackernews_results,
    collect_pubmed_results,
    collect_rss_feed_results,
    collect_stackexchange_results,
    collect_wikipedia_results,
)
from web_scraper.research.prompts import (
    build_query_rewrite_prompt,
    build_source_count_decision_prompt,
    build_source_selection_prompt,
    build_synthesis_prompt,
)
from web_scraper.research.ranking import (
    expand_selected_sources,
    merge_and_rank_search_results,
)
from web_scraper.research.retry_utils import async_retry
from web_scraper.research.text_utils import (
    clean_query_text,
    detect_query_language,
    extract_date_from_snippet,
    extract_direct_urls,
    extract_json_payload,
    extract_publication_date,
    filter_low_quality_results,
    has_subpage_crawl_intent,
    repair_truncated_json,
)
from web_scraper.research.url_utils import (
    classify_source_tier,
    get_official_doc_urls_for_query,
)

logger = logging.getLogger(__name__)
ResearchProfile = Literal["technical", "news", "academic"]
ResearchProfileInput = Literal["technical", "news", "academic", "auto"]

# Code-query keywords re-used in _plan_research_with_results for official-docs injection
_CODE_SOURCE_KW: frozenset[str] = frozenset(
    {
        "code",
        "example",
        "how to",
        "api",
        "library",
        "framework",
        "implement",
        "tutorial",
        "usage",
        "syntax",
        "function",
        "class",
        "method",
        "kod",
        "örnek",
        "nasıl",
        "kütüphane",
        "kullanım",
    }
)


class ResearchAgent(LLMClient):
    """Full research pipeline: search → scrape → rank → synthesise.

    The class inherits ``LLMClient`` for all LLM transport and adds the
    agentic orchestration on top.
    """

    # Expose constants at class level for backward compatibility
    SOURCE_TEMPLATES = SOURCE_TEMPLATES

    # ------------------------------------------------------------------
    # Backward-compat class-method delegates (tests call these directly)
    # The real implementations live in their own modules; these thin
    # wrappers keep existing call-sites on ResearchAgent working.
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_publication_date(content: str) -> Optional[str]:
        return extract_publication_date(content)

    @staticmethod
    def _classify_source_tier(url: str) -> int:
        return classify_source_tier(url)

    @staticmethod
    def _filter_low_quality_results(results: list, min_chars: int = 100) -> list:
        return filter_low_quality_results(results, min_chars)

    @staticmethod
    def _merge_and_rank_search_results(
        query: str,
        result_sets: list,
        limit: int,
        research_profile: ResearchProfile = "technical",
    ) -> list:
        return merge_and_rank_search_results(
            query,
            result_sets,
            limit,
            research_profile=research_profile,
        )

    @staticmethod
    def _build_synthesis_prompt(
        query: str,
        results: list,
        deep_mode: bool,
        temporal_scope: Optional[dict] = None,
    ) -> str:
        return build_synthesis_prompt(query, results, deep_mode, temporal_scope)

    @staticmethod
    def _build_query_rewrite_prompt(query: str, deep_mode: bool) -> str:
        return build_query_rewrite_prompt(query, deep_mode)

    @staticmethod
    def _build_source_selection_prompt(
        query: str, ddg_results: list, max_to_check: int, deep_mode: bool
    ) -> str:
        return build_source_selection_prompt(query, ddg_results, max_to_check, deep_mode)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(
        self,
        model: Optional[str] = None,
        host: Optional[str] = None,
        max_concurrent: Optional[int] = None,
        timeout_per_source: Optional[float] = None,
        provider: str = "ollama",
        openai_api_key: Optional[str] = None,
        ollama_api_key: Optional[str] = None,
    ) -> None:
        super().__init__(
            model=model,
            host=host,
            max_concurrent=max_concurrent,
            timeout_per_source=timeout_per_source,
            provider=provider,
            openai_api_key=openai_api_key,
            ollama_api_key=ollama_api_key,
        )
        # Language detected from the current query — set during research()
        self._query_lang: str = "en"

    @staticmethod
    def _detect_profile_from_query(query: str) -> ResearchProfile:
        """Infer the most appropriate research profile from *query* text."""
        lower = query.lower()

        _ACADEMIC_KW: frozenset[str] = frozenset(
            {
                "paper", "study", "research", "journal", "arxiv", "pubmed",
                "doi", "citation", "abstract", "hypothesis", "experiment",
                "peer review", "peer-review", "literature review", "methodology",
                "meta-analysis", "clinical trial", "dissertation", "thesis",
                # Turkish
                "makale", "araştırma", "çalışma", "tez", "yayın", "akademik",
                "bilimsel", "dergi", "atıf", "deneysel",
            }
        )
        _NEWS_KW: frozenset[str] = frozenset(
            {
                "news", "breaking", "latest", "today", "yesterday", "this week",
                "this month", "current", "update", "announcement", "headline",
                "report", "journalist", "press", "coverage", "incident",
                # Turkish
                "haber", "son dakika", "güncel", "bugün", "dün", "bu hafta",
                "bu ay", "gelişme", "açıklama", "basın",
            }
        )

        if any(kw in lower for kw in _ACADEMIC_KW):
            return "academic"
        if any(kw in lower for kw in _NEWS_KW):
            return "news"
        return "technical"

    @staticmethod
    def _normalize_profile(research_profile: str) -> ResearchProfile:
        allowed: set[str] = {"technical", "news", "academic"}
        if research_profile in allowed:
            return research_profile  # type: ignore[return-value]
        return "technical"

    # ------------------------------------------------------------------
    # I18n helpers
    # ------------------------------------------------------------------

    def _msg(self, key: str, **kwargs: Any) -> str:
        """Return a localised status message for the current query language."""
        lang = getattr(self, "_query_lang", "en")
        messages = STATUS_MESSAGES.get(lang, STATUS_MESSAGES["en"])
        template = messages.get(key, STATUS_MESSAGES["en"].get(key, key))
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template

    # ------------------------------------------------------------------
    # Source-count resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_target_source_count(
        requested_max_sources: Optional[int],
        ai_suggested_sources: Optional[int],
        deep_mode: bool,
    ) -> int:
        """Clamp requested or AI-decided source counts into safe mode ranges."""
        if requested_max_sources is not None:
            if deep_mode:
                return min(
                    max(requested_max_sources, config.research_deep_min_sources),
                    config.research_deep_max_sources,
                )
            return min(
                max(requested_max_sources, config.research_normal_auto_min_sources),
                config.research_normal_auto_max_sources,
            )

        suggested = ai_suggested_sources
        if suggested is None:
            suggested = (
                config.research_default_deep_source_target
                if deep_mode
                else config.research_default_normal_source_target
            )

        if deep_mode:
            return min(
                max(suggested, config.research_deep_min_sources),
                config.research_deep_max_sources,
            )
        return min(
            max(suggested, config.research_normal_auto_min_sources),
            config.research_normal_auto_max_sources,
        )

    # ------------------------------------------------------------------
    # Query preparation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_search_queries(
        original_query: str,
        normalized_query: str,
        search_queries: list[str],
    ) -> list[str]:
        """Deduplicate and cap query variants while preserving the original."""
        candidates = [normalized_query, original_query]
        candidates.extend(search_queries)

        seen_queries: set[str] = set()
        result: list[str] = []

        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            cleaned = clean_query_text(candidate)
            if len(cleaned) < 2 or len(cleaned) > config.max_query_length:
                continue
            key = cleaned.casefold()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            result.append(cleaned)
            if len(result) >= config.research_query_rewrite_max_variants:
                break

        if result:
            return result

        cleaned_original = clean_query_text(original_query)
        return [cleaned_original] if cleaned_original else []

    async def _prepare_search_queries(self, query: str, deep_mode: bool = False) -> dict:
        """Turn raw user input into one or more search-ready query variants."""
        cleaned_query = clean_query_text(query)
        fallback_queries = self._normalize_search_queries(cleaned_query, cleaned_query, [])
        fallback = {
            "query_ready": True,
            "normalized_query": cleaned_query,
            "search_queries": fallback_queries,
            "rewrite_reason": "Input used directly",
            "temporal_scope": None,
        }

        # Skip LLM rewrite in standard mode — only spend the inference time
        # when deep_mode is active and the extra query variants are worthwhile.
        if not cleaned_query or not config.research_enable_query_rewrite or not deep_mode:
            return fallback

        prompt = build_query_rewrite_prompt(cleaned_query, deep_mode)

        try:
            ai_response = await self._call_llm(
                prompt, config.research_query_rewrite_timeout_seconds
            )
            payload = extract_json_payload(ai_response)

            normalized_query = clean_query_text(payload.get("normalized_query") or cleaned_query)
            rewritten_queries = payload.get("search_queries", [])
            if not isinstance(rewritten_queries, list):
                rewritten_queries = []

            search_queries = self._normalize_search_queries(
                original_query=cleaned_query,
                normalized_query=normalized_query,
                search_queries=[c for c in rewritten_queries if isinstance(c, str)],
            )

            if not search_queries:
                return fallback

            query_ready = payload.get("query_ready")
            rewrite_reason = payload.get("rewrite_reason", "")
            temporal_scope = payload.get("temporal_scope")
            if not isinstance(temporal_scope, dict):
                temporal_scope = None

            # Safety net: add current year if scope is "current" but LLM forgot
            if temporal_scope and temporal_scope.get("type") == "current":
                current_year = str(datetime.now().year)
                search_queries = self._ensure_year_in_queries(search_queries, current_year)

            return {
                "query_ready": query_ready if isinstance(query_ready, bool) else True,
                "normalized_query": search_queries[0],
                "search_queries": search_queries,
                "rewrite_reason": rewrite_reason if isinstance(rewrite_reason, str) else "",
                "temporal_scope": temporal_scope,
            }
        except Exception:
            return fallback

    @staticmethod
    def _ensure_year_in_queries(queries: list[str], year: str) -> list[str]:
        """Append *year* to any query that contains no year token (20XX / 19XX)."""
        if not year or not queries:
            return queries
        year_pattern = re.compile(r"\b(19|20)\d{2}\b")
        return [q if year_pattern.search(q) else f"{q} {year}" for q in queries]

    # ------------------------------------------------------------------
    # Search collection
    # ------------------------------------------------------------------

    async def _collect_duckduckgo_results(
        self,
        search_queries: list[str],
        search_pool_size: int,
        temporal_scope: Optional[dict] = None,
    ) -> list[dict]:
        """Search all query variants concurrently and merge unique DuckDuckGo results."""
        if not search_queries:
            return []

        per_query_budget = min(
            max(5, math.ceil(search_pool_size / max(len(search_queries), 1)) + 2),
            config.research_deep_max_sources,
        )

        date_filter = None
        if temporal_scope and temporal_scope.get("type") == "current":
            date_filter = "year"

        async def _search_variant(search_query: str) -> tuple[str, list[dict]]:
            results = await async_retry(
                lambda: get_best_sources_ddg(
                    search_query,
                    max_sources=per_query_budget,
                    date_filter=date_filter,
                ),
                max_attempts=3,
                base_delay=1.5,
                label=f"DDG:{search_query[:40]}",
            )
            return search_query, results

        variant_outcomes = await asyncio.gather(
            *[_search_variant(q) for q in search_queries],
            return_exceptions=True,
        )

        merged_results: list[dict] = []
        seen_urls: set[str] = set()

        for outcome in variant_outcomes:
            if isinstance(outcome, BaseException):
                logger.warning(f"DDG variant search failed: {outcome}")
                continue
            search_query, variant_results = outcome  # type: ignore[misc]
            for result in variant_results:
                url = result.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                enriched = dict(result)
                enriched["search_query"] = search_query
                enriched["search_provider"] = "duckduckgo"
                if not enriched.get("publication_date"):
                    extracted = extract_date_from_snippet(enriched.get("snippet", ""))
                    if extracted:
                        enriched["publication_date"] = extracted
                merged_results.append(enriched)

        return merged_results[:search_pool_size]

    async def _collect_google_results(
        self,
        search_queries: list[str],
        search_pool_size: int,
    ) -> list[dict]:
        """Search all Google query variants concurrently."""
        if not search_queries:
            return []

        per_query_budget = min(
            max(3, math.ceil(search_pool_size / max(len(search_queries), 1))),
            config.research_deep_max_sources,
        )

        async def _search_variant(search_query: str) -> tuple[str, list[dict]]:
            results = await async_retry(
                lambda: get_best_sources(search_query, max_sources=per_query_budget),
                max_attempts=2,
                base_delay=2.0,
                label=f"Google:{search_query[:40]}",
            )
            return search_query, results

        variant_outcomes = await asyncio.gather(
            *[_search_variant(q) for q in search_queries],
            return_exceptions=True,
        )

        merged_results: list[dict] = []
        seen_urls: set[str] = set()

        for outcome in variant_outcomes:
            if isinstance(outcome, BaseException):
                logger.warning(f"Google variant search failed: {outcome}")
                continue
            search_query, variant_results = outcome  # type: ignore[misc]
            for result in variant_results:
                url = result.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                enriched = dict(result)
                enriched["search_query"] = search_query
                enriched["search_provider"] = "google"
                merged_results.append(enriched)

        return merged_results[:search_pool_size]

    async def _collect_search_results(
        self,
        query: str,
        search_queries: list[str],
        search_pool_size: int,
        target_count: int,
        temporal_scope: Optional[dict] = None,
        research_profile: ResearchProfile = "technical",
        deep_mode: bool = False,
    ) -> dict:
        """Collect DDG and Google results concurrently, then merge and rank."""
        ddg_results: list[dict] = []
        google_results: list[dict] = []
        profile_results: list[dict] = []
        ddg_error: Optional[str] = None
        google_error: Optional[str] = None
        profile_error: Optional[str] = None
        selected_profile = self._normalize_profile(research_profile)

        labeled_tasks: list[tuple[str, asyncio.Task]] = [
            (
                "duckduckgo",
                asyncio.create_task(
                    self._collect_duckduckgo_results(
                        search_queries=search_queries,
                        search_pool_size=search_pool_size,
                        temporal_scope=temporal_scope,
                    )
                ),
            )
        ]
        if config.research_enable_google_fallback:
            labeled_tasks.append(
                (
                    "google",
                    asyncio.create_task(
                        self._collect_google_results(
                            search_queries=search_queries,
                            search_pool_size=search_pool_size,
                        )
                    ),
                )
            )

        if deep_mode:
            _profile_timeout = config.duckduckgo_request_timeout_seconds
            _profile_pool = search_pool_size
            if selected_profile == "academic":
                # arXiv + PubMed in parallel for academic deep mode
                labeled_tasks.extend(
                    [
                        (
                            "profile",
                            asyncio.create_task(
                                collect_arxiv_results(
                                    search_queries=search_queries,
                                    search_pool_size=_profile_pool,
                                    timeout_seconds=_profile_timeout,
                                )
                            ),
                        ),
                        (
                            "profile",
                            asyncio.create_task(
                                collect_pubmed_results(
                                    search_queries=search_queries,
                                    search_pool_size=_profile_pool,
                                    timeout_seconds=_profile_timeout,
                                )
                            ),
                        ),
                    ]
                )
            elif selected_profile == "news":
                # HN Algolia + RSS news feeds in parallel for news deep mode
                labeled_tasks.extend(
                    [
                        (
                            "profile",
                            asyncio.create_task(
                                collect_hackernews_results(
                                    search_queries=search_queries,
                                    search_pool_size=_profile_pool,
                                    timeout_seconds=_profile_timeout,
                                )
                            ),
                        ),
                        (
                            "profile",
                            asyncio.create_task(
                                collect_rss_feed_results(
                                    search_queries=search_queries,
                                    search_pool_size=_profile_pool,
                                    timeout_seconds=_profile_timeout,
                                )
                            ),
                        ),
                    ]
                )
            else:
                # Wikipedia + StackExchange in parallel for technical deep mode
                labeled_tasks.extend(
                    [
                        (
                            "profile",
                            asyncio.create_task(
                                collect_wikipedia_results(
                                    search_queries=search_queries,
                                    search_pool_size=_profile_pool,
                                    timeout_seconds=_profile_timeout,
                                )
                            ),
                        ),
                        (
                            "profile",
                            asyncio.create_task(
                                collect_stackexchange_results(
                                    search_queries=search_queries,
                                    search_pool_size=_profile_pool,
                                    timeout_seconds=_profile_timeout,
                                )
                            ),
                        ),
                    ]
                )

        raw_results = await asyncio.gather(
            *[task for _, task in labeled_tasks],
            return_exceptions=True,
        )

        for (label, _), outcome in zip(labeled_tasks, raw_results):
            if isinstance(outcome, BaseException):
                if label == "duckduckgo":
                    ddg_error = str(outcome)
                elif label == "google":
                    google_error = str(outcome)
                else:
                    # Accumulate profile errors; use the first one for reporting
                    if profile_error is None:
                        profile_error = str(outcome)
                continue

            if label == "duckduckgo":
                ddg_results = list(outcome)  # type: ignore[arg-type]
            elif label == "google":
                google_results = list(outcome)  # type: ignore[arg-type]
            else:
                # Accumulate from multiple profile collectors
                profile_results.extend(list(outcome))  # type: ignore[arg-type]

        min_google_fallback = min(
            max(1, config.research_google_fallback_min_results),
            max(1, search_pool_size),
        )
        if ddg_results and len(ddg_results) >= min_google_fallback:
            google_results = []

        ranked = merge_and_rank_search_results(
            query=query,
            result_sets=[ddg_results, google_results, profile_results],
            limit=search_pool_size,
            research_profile=research_profile,
        )
        providers_used = sorted(
            {
                provider
                for result in ranked
                for provider in str(result.get("search_provider", "")).split(",")
                if provider
            }
        )

        return {
            "results": ranked,
            "providers_used": providers_used,
            "fallback_used": bool(google_results),
            "ddg_error": ddg_error,
            "google_error": google_error,
            "profile_error": profile_error,
            "profile_provider_used": bool(profile_results),
        }

    # ------------------------------------------------------------------
    # Source scraping
    # ------------------------------------------------------------------

    # Minimum content length before we escalate to Playwright scraper
    _PLAYWRIGHT_ESCALATION_MIN_CHARS: int = 250

    async def _scrape_source(
        self, source_config: dict, original_query: str, deep_mode: bool = False
    ) -> ResearchResult:
        """Scrape a single source and return a ``ResearchResult``.

        Falls back to Playwright if the HTTP scraper returns fewer than
        ``_PLAYWRIGHT_ESCALATION_MIN_CHARS`` characters of meaningful content.
        """
        source_type = source_config["type"]
        url = source_config["url"]

        try:
            async with WebScraperAsync(timeout=self.timeout_per_source) as scraper:
                data = await scraper.scrape(url)

                if data.error:
                    return ResearchResult(
                        source=source_type,
                        url=url,
                        title=data.title or "",
                        content="",
                        error=data.error,
                    )

                content = sanitize_scraped_text(
                    data.content,
                    max_chars=config.max_source_content_chars,
                )

                # Playwright escalation: if HTTP scrape returned thin content,
                # try Playwright to handle JS-rendered pages.
                if len(content.strip()) < self._PLAYWRIGHT_ESCALATION_MIN_CHARS:
                    try:
                        from web_scraper.playwright_scrapers import PlaywrightScraper

                        pw_timeout_ms = int(self.timeout_per_source * 1000)
                        async with PlaywrightScraper(timeout=pw_timeout_ms) as pw:
                            pw_data = await pw.scrape(url)
                        if pw_data.content and not pw_data.error:
                            pw_content = sanitize_scraped_text(
                                pw_data.content,
                                max_chars=config.max_source_content_chars,
                            )
                            if len(pw_content.strip()) > len(content.strip()):
                                logger.debug(
                                    "Playwright escalation yielded %d chars for %s",
                                    len(pw_content),
                                    url,
                                )
                                data = pw_data
                                content = pw_content
                    except Exception as pw_err:
                        logger.debug("Playwright escalation failed for %s: %s", url, pw_err)

                if not deep_mode and len(content) > config.research_non_deep_source_char_cap:
                    content = (
                        content[: config.research_non_deep_source_char_cap]
                        + "\n\n[Content truncated...]"
                    )

                relevance = await self._calculate_relevance(original_query, content)

                return ResearchResult(
                    source=source_type,
                    url=data.url,
                    title=data.title,
                    content=content,
                    relevance_score=relevance,
                    source_tier=classify_source_tier(data.url),
                    publication_date=extract_publication_date(content),
                )

        except Exception as e:
            return ResearchResult(source=source_type, url=url, title="", content="", error=str(e))

    # ------------------------------------------------------------------
    # Direct URL crawl (bypass search engine)
    # ------------------------------------------------------------------

    # Absolute ceiling for subpages (root page is +1 on top of this)
    _DIRECT_CRAWL_MAX_SUBPAGES: int = 99

    # File extensions that are never worth scraping as content pages
    _SKIP_EXTENSIONS: frozenset[str] = frozenset(
        {
            ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
            ".css", ".js", ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".pdf", ".zip", ".tar", ".gz", ".rar", ".exe", ".dmg",
            ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv",
            ".xml", ".json", ".txt", ".csv",
        }
    )

    @staticmethod
    def _score_subpage_url(url: str, root_url: str) -> int:
        """Return a priority score for *url* (higher = more valuable).

        Scoring rationale:
        - Deep, descriptive paths score higher than shallow or paginated ones.
        - Query-param-only variants of the same path are penalised.
        - Common navigation dead-ends (login, logout, cart, print) score zero.
        - Fragment-only differences from root get lowest score.
        """
        from urllib.parse import urlparse

        _SKIP_PATH_SEGMENTS: frozenset[str] = frozenset(
            {
                "login", "logout", "register", "signup", "sign-up",
                "cart", "basket", "checkout", "payment", "order",
                "print", "share", "embed", "feed", "rss",
                "giris", "cikis", "uye-ol", "kayit", "sepet", "odeme",
                "search", "ara", "tag", "etiket",
            }
        )

        try:
            parsed = urlparse(url)
            root_parsed = urlparse(root_url)

            # Fragment-only difference → very low
            if parsed.path == root_parsed.path and parsed.query == root_parsed.query:
                return 0

            path = parsed.path.rstrip("/").lower()
            segments = [s for s in path.split("/") if s]

            # Skip known dead-ends
            if any(seg in _SKIP_PATH_SEGMENTS for seg in segments):
                return 0

            score = 10

            # Depth bonus (more segments = more specific content)
            score += min(len(segments), 5) * 5

            # Penalty for query parameters (paginated, filtered views)
            if parsed.query:
                score -= 8

            # Penalty for purely numeric last segment (e.g. /page/2, /urun/12345)
            last_seg = segments[-1] if segments else ""
            if last_seg.isdigit():
                score -= 6

            # Bonus for descriptive slug (letters + hyphens, no digits)
            import re as _re
            if last_seg and _re.match(r"^[a-z\-\u00c0-\u024f]+$", last_seg):
                score += 4

            return max(score, 0)
        except Exception:
            return 1

    def _select_subpages(
        self,
        raw_links: list[str],
        root_url: str,
        max_subpages: int,
    ) -> list[str]:
        """Score, deduplicate, and cap *raw_links* to the most valuable subset.

        Strategy:
        1. Drop media/binary extensions.
        2. Score each URL by content-value heuristics.
        3. Deduplicate by normalised path pattern (avoid 50 product-list pages).
        4. Return up to *max_subpages* URLs, ordered by descending score.
        """
        import re as _re
        from urllib.parse import urlparse

        skip_ext = self._SKIP_EXTENSIONS
        seen_urls: set[str] = set()
        # Maps a "pattern key" (path with digits replaced) to count seen
        pattern_counter: dict[str, int] = {}
        # Max pages sharing the same path pattern (avoids e.g. 40 product pages)
        _PATTERN_CAP = 5

        scored: list[tuple[int, str]] = []

        for url in raw_links:
            url_clean = url.rstrip("/")
            if not url_clean or url_clean in seen_urls:
                continue
            seen_urls.add(url_clean)

            # Drop binary/media extensions
            lower = url_clean.lower().split("?")[0]
            if any(lower.endswith(ext) for ext in skip_ext):
                continue

            score = self._score_subpage_url(url_clean, root_url)
            if score == 0:
                continue

            # Pattern deduplication: normalise all digits → "N"
            try:
                path = urlparse(url_clean).path
            except Exception:
                path = url_clean
            pattern_key = _re.sub(r"\d+", "N", path)
            count = pattern_counter.get(pattern_key, 0)
            if count >= _PATTERN_CAP:
                continue
            pattern_counter[pattern_key] = count + 1

            scored.append((score, url_clean))

        # Sort descending by score, then alphabetically for stability
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [url for _, url in scored[:max_subpages]]

    async def _crawl_direct_url(
        self,
        root_url: str,
        include_subpages: bool,
        query: str,
        deep_mode: bool = False,
        progress_sink: Optional[Callable[[str], None]] = None,
        max_subpages: int = _DIRECT_CRAWL_MAX_SUBPAGES,
    ) -> list[dict]:
        """Scrape *root_url* directly (no search-engine lookup).

        When *include_subpages* is ``True``, internal links discovered on the
        root page are scored, deduplicated, and filtered before scraping —
        up to *max_subpages*.  Returns source-config dicts compatible with
        ``_scrape_source``.
        """
        if progress_sink:
            progress_sink(self._msg("gathering_data"))

        # -- 1. Scrape root -------------------------------------------------
        root_result = await self._scrape_source(
            {"type": "direct", "url": root_url, "title": root_url},
            query,
            deep_mode,
        )

        source_configs: list[dict] = [
            {"type": "direct", "url": root_url, "title": root_result.title or root_url}
        ]

        if not include_subpages:
            return source_configs

        # -- 2. Discover internal links from the root page HTML ------------
        try:
            async with WebScraperAsync(timeout=self.timeout_per_source) as scraper:
                raw = await scraper.scrape(root_url)
                internal_links: list[str] = [
                    lnk.get("url", "")
                    for lnk in raw.links.get("internal", [])
                    if lnk.get("url", "").startswith("http")
                ]
        except Exception as exc:
            logger.warning("Could not extract internal links from %s: %s", root_url, exc)
            internal_links = []

        # -- 3. Score and select best subpages ------------------------------
        selected = self._select_subpages(
            raw_links=[lnk for lnk in internal_links if lnk != root_url],
            root_url=root_url,
            max_subpages=max_subpages,
        )

        if progress_sink and selected:
            progress_sink(
                self._msg("sources_to_check", count=len(selected) + 1)
            )

        for lnk in selected:
            source_configs.append({"type": "subpage", "url": lnk, "title": lnk})

        return source_configs

    # ------------------------------------------------------------------
    # Research planning
    # ------------------------------------------------------------------

    async def _plan_research(
        self,
        query: str,
        max_sources: Optional[int],
        deep_mode: bool = False,
        search_queries: Optional[List[str]] = None,
        progress_sink: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Search for sources and build a scrape plan.

        Deep mode fires the AI source-count decision concurrently with the
        search to reduce latency.
        """
        ai_suggested_sources = None
        effective_search_queries = search_queries or [query]

        preliminary_max = self._resolve_target_source_count(
            requested_max_sources=max_sources,
            ai_suggested_sources=None,
            deep_mode=deep_mode,
        )
        preliminary_pool_size = min(
            max(
                preliminary_max
                + (
                    config.research_search_pool_extra_deep
                    if deep_mode
                    else config.research_search_pool_extra_normal
                ),
                config.research_deep_min_sources if deep_mode else preliminary_max,
            ),
            config.research_deep_max_sources,
        )

        if max_sources is None and deep_mode:
            if progress_sink:
                progress_sink(self._msg("analyzing_complexity"))

            num_decision_prompt = build_source_count_decision_prompt(query, deep_mode)

            async def _get_ai_source_count() -> Optional[int]:
                try:
                    ai_num = await self._call_llm(
                        num_decision_prompt, config.research_planning_timeout_seconds
                    )
                    num_match = re.search(r"\d+", ai_num.strip())
                    return int(num_match.group()) if num_match else None
                except Exception as e:
                    logger.warning(f"AI source count decision failed: {e}")
                    return None

            async def _run_search() -> dict:
                return await self._collect_search_results(
                    query=query,
                    search_queries=effective_search_queries,
                    search_pool_size=preliminary_pool_size,
                    target_count=preliminary_max,
                )

            count_result, search_collection = await asyncio.gather(
                _get_ai_source_count(), _run_search()
            )
            ai_suggested_sources = count_result
        else:
            if progress_sink:
                progress_sink(self._msg("searching_sources", count=preliminary_max))
            try:
                search_collection = await self._collect_search_results(
                    query=query,
                    search_queries=effective_search_queries,
                    search_pool_size=preliminary_pool_size,
                    target_count=preliminary_max,
                )
            except Exception as e:
                logger.error(f"Research planning failed: {e}", exc_info=True)
                return self._default_strategy(
                    query, deep_mode=deep_mode, target_count=preliminary_max
                )

        max_to_check = self._resolve_target_source_count(
            requested_max_sources=max_sources,
            ai_suggested_sources=ai_suggested_sources,
            deep_mode=deep_mode,
        )

        ddg_results = search_collection["results"]

        if not ddg_results:
            logger.warning("No search results found, using fallback strategy")
            return self._default_strategy(query, deep_mode=deep_mode, target_count=max_to_check)

        if progress_sink:
            progress_sink(self._msg("ranking_results", count=len(ddg_results)))

        # Standard mode: skip the source-selection LLM call entirely.
        # The keyword-based expand_selected_sources is fast and accurate enough.
        if not deep_mode:
            return {
                "sources": expand_selected_sources(
                    selected_sources=[],
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    query=query,
                ),
                "reasoning": "Keyword-ranked results (standard mode)",
                "depth": "standard",
            }

        prompt = build_source_selection_prompt(
            query=query,
            ddg_results=ddg_results,
            max_to_check=max_to_check,
            deep_mode=deep_mode,
        )

        try:
            ai_response = await self._call_llm(
                prompt, config.research_source_selection_timeout_seconds
            )
            start = ai_response.index("{")
            end = ai_response.rindex("}") + 1
            strategy = json.loads(ai_response[start:end])

            valid_sources = [
                s
                for s in strategy.get("sources", [])
                if s.get("url") and s["url"].startswith("http")
            ]

            if valid_sources:
                strategy["sources"] = expand_selected_sources(
                    selected_sources=valid_sources[:max_to_check],
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    query=query,
                )
                strategy["depth"] = "deep"
                return strategy

        except Exception as e:
            logger.warning(f"AI parsing failed ({e}), using search results directly")

        return {
            "sources": expand_selected_sources(
                selected_sources=[],
                fallback_results=ddg_results,
                target_count=max_to_check,
                query=query,
            ),
            "reasoning": "Using top search results",
            "depth": "deep",
        }

    async def _plan_research_with_results(
        self,
        query: str,
        max_sources: Optional[int],
        deep_mode: bool = False,
        search_results: Optional[list[dict]] = None,
        progress_sink: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Fast-path planning that uses pre-collected search results."""
        ai_suggested_sources = None
        ddg_results = search_results or []

        if max_sources is None and deep_mode:
            if progress_sink:
                progress_sink(self._msg("analyzing_deep"))
            num_decision_prompt = build_source_count_decision_prompt(query, deep_mode)
            try:
                ai_num = await self._call_llm(
                    num_decision_prompt, config.research_planning_timeout_seconds
                )
                num_match = re.search(r"\d+", ai_num.strip())
                if num_match:
                    ai_suggested_sources = int(num_match.group())
            except Exception as e:
                logger.warning(f"AI source count decision failed: {e}")

        max_to_check = self._resolve_target_source_count(
            requested_max_sources=max_sources,
            ai_suggested_sources=ai_suggested_sources,
            deep_mode=deep_mode,
        )

        if not ddg_results:
            logger.warning("No search results found, using fallback strategy")
            return self._default_strategy(query, deep_mode=deep_mode, target_count=max_to_check)

        if progress_sink:
            progress_sink(self._msg("ranking_results", count=len(ddg_results)))

        # Standard mode fast-path: skip LLM source selection entirely.
        if not deep_mode:
            return {
                "sources": expand_selected_sources(
                    selected_sources=[],
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    query=query,
                ),
                "reasoning": "Keyword-ranked results (standard mode)",
                "depth": "standard",
            }

        prompt = build_source_selection_prompt(
            query=query,
            ddg_results=ddg_results,
            max_to_check=max_to_check,
            deep_mode=deep_mode,
        )

        try:
            ai_response = await self._call_llm(
                prompt, config.research_source_selection_timeout_seconds
            )
            start = ai_response.index("{")
            end = ai_response.rindex("}") + 1
            strategy = json.loads(ai_response[start:end])

            valid_sources = [
                s
                for s in strategy.get("sources", [])
                if s.get("url") and s["url"].startswith("http")
            ]

            # Inject official documentation URLs as priority sources for code queries
            query_lower = query.lower()
            if any(kw in query_lower for kw in _CODE_SOURCE_KW):
                official_doc_urls = get_official_doc_urls_for_query(query)
                if official_doc_urls:
                    existing_urls = {s.get("url") for s in valid_sources}
                    injected = [
                        {
                            "type": "official_docs",
                            "url": u,
                            "title": f"Official Documentation: {u}",
                            "priority": 0,
                        }
                        for u in official_doc_urls
                        if u not in existing_urls
                    ]
                    if injected:
                        logger.info(
                            f"Injecting {len(injected)} official doc URLs for code query: "
                            + ", ".join(s["url"] for s in injected)
                        )
                    valid_sources = injected + valid_sources

            if valid_sources:
                strategy["sources"] = expand_selected_sources(
                    selected_sources=valid_sources[:max_to_check],
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    query=query,
                )
                strategy["depth"] = "deep"
                return strategy

        except Exception as e:
            logger.warning(f"AI parsing failed ({e}), using search results directly")

        return {
            "sources": expand_selected_sources(
                selected_sources=[],
                fallback_results=ddg_results,
                target_count=max_to_check,
                query=query,
            ),
            "reasoning": "Using top search results",
            "depth": "deep" if deep_mode else "standard",
        }

    def _default_strategy(self, query: str, deep_mode: bool, target_count: int) -> dict:
        """Fallback research strategy when search fails entirely."""
        encoded_query = quote_plus(query)

        template_sources = [
            {
                "type": name,
                "url": template.format(query=encoded_query),
                "title": f"{name} results for {query}",
                "priority": index + 1,
            }
            for index, (name, template) in enumerate(SOURCE_TEMPLATES.items())
        ]

        if not deep_mode:
            return {
                "sources": template_sources[: max(1, min(target_count, len(template_sources)))],
                "depth": "standard",
                "reasoning": "Using fallback source templates",
            }

        expanded: list[dict] = []
        deep_target = min(target_count, config.research_deep_max_sources)
        while len(expanded) < deep_target:
            for source in template_sources:
                if len(expanded) >= deep_target:
                    break
                source_copy = dict(source)
                source_copy["priority"] = len(expanded) + 1
                expanded.append(source_copy)

        return {
            "sources": expanded,
            "depth": "deep",
            "reasoning": "Using fallback source templates",
        }

    # ------------------------------------------------------------------
    # Relevance scoring
    # ------------------------------------------------------------------

    async def _calculate_relevance(self, query: str, content: str) -> float:
        """Simple lexical relevance score (0–1)."""
        query_words = set(query.lower().split())
        content_lower = content.lower()
        matches = sum(1 for word in query_words if word in content_lower)
        return round(min(matches / max(len(query_words), 1), 1.0), 2)

    # ------------------------------------------------------------------
    # Provider availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the configured LLM provider is reachable."""
        if self.provider == "openai":
            return bool(self.openai_api_key)
        try:
            response = httpx.get(
                f"{self.host}/api/tags",
                timeout=min(5.0, config.research_planning_timeout_seconds),
            )
            return response.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    async def _synthesize_findings(
        self,
        query: str,
        results: list[ResearchResult],
        deep_mode: bool = False,
        temporal_scope: Optional[dict] = None,
    ) -> dict:
        """AI-driven synthesis of all scraped sources into a structured report."""
        successful_results = filter_low_quality_results(results)

        citation_ordered = sorted(
            [r for r in successful_results if not r.error and r.content],
            key=lambda r: (r.source_tier, -r.relevance_score),
        )
        cited_sources_list = [
            {"url": r.url, "title": r.title, "source": r.source} for r in citation_ordered
        ]

        if not successful_results:
            return {
                "executive_summary": "No relevant information found from the checked sources.",
                "summary": "No relevant information found from the checked sources.",
                "key_findings": [],
                "data_table": [],
                "conflicts_uncertainty": [],
                "confidence_level": "Low",
                "confidence_reason": "No usable sources were retrieved.",
                "detailed_analysis": "",
                "recommendations": "",
                "cited_sources": [],
            }

        prompt = build_synthesis_prompt(query, successful_results, deep_mode, temporal_scope)

        # Post-processing helpers
        def fix_citations(text: str) -> str:
            text = re.sub(r"(\[[0-9]+\])(\[[0-9]+\])", r"\1, \2", text)
            while re.search(r"(\[[0-9]+\])(\[[0-9]+\])", text):
                text = re.sub(r"(\[[0-9]+\])(\[[0-9]+\])", r"\1, \2", text)
            return text

        def fix_recommendations(text: str) -> str:
            lines = text.split("\n")
            fixed: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped and (
                    stripped[0].isdigit() or stripped.startswith("-") or stripped.startswith("•")
                ):
                    if fixed and fixed[-1].strip():
                        fixed.append("")
                fixed.append(line)
            return "\n".join(fixed)

        def _build_report_from_data(data: dict) -> dict:
            raw_confidence = str(data.get("confidence_level", "Medium")).strip()
            if raw_confidence not in {"High", "Medium", "Low"}:
                raw_confidence = "Medium"

            recommendations = fix_citations(
                fix_recommendations(data.get("recommendations", "") or "")
            )
            detailed_analysis = fix_citations(data.get("detailed_analysis", "") or "")
            executive_summary = fix_citations(
                data.get("executive_summary", "") or data.get("summary", "")
            )

            # Build citation audit from the executive summary + detailed analysis
            analysis_text = executive_summary + "\n" + detailed_analysis
            source_contents = [r.content or "" for r in citation_ordered]
            audit = citation_audit_summary(analysis_text, source_contents)

            return {
                "executive_summary": executive_summary,
                "summary": executive_summary,
                "key_findings": data.get("key_findings", []),
                "data_table": data.get("data_table", []),
                "conflicts_uncertainty": data.get("conflicts_uncertainty", []),
                "confidence_level": raw_confidence,
                "confidence_reason": data.get("confidence_reason", ""),
                "detailed_analysis": detailed_analysis,
                "recommendations": recommendations,
                "cited_sources": cited_sources_list,
                "citation_audit": audit,
            }

        synthesis_timeout = (
            config.research_deep_synthesis_timeout_seconds
            if deep_mode
            else config.research_synthesis_timeout_seconds
        )
        initial_max_tokens = 32000 if deep_mode else 12000
        retry_max_tokens = 48000 if deep_mode else 20000

        try:
            ai_response = await self._call_llm(prompt, synthesis_timeout, initial_max_tokens)

            try:
                start = ai_response.index("{")
                end = ai_response.rindex("}") + 1
                data = json.loads(ai_response[start:end])
                return _build_report_from_data(data)
            except (ValueError, json.JSONDecodeError):
                pass

            logger.warning("Synthesis JSON parse failed, attempting repair...")
            repaired = repair_truncated_json(ai_response)
            if repaired and isinstance(repaired.get("executive_summary"), str):
                logger.info("JSON repair succeeded on first attempt")
                return _build_report_from_data(repaired)

            logger.warning("JSON repair insufficient, retrying with larger token budget...")
            try:
                ai_response_retry = await self._call_llm(
                    prompt, synthesis_timeout * 1.3, retry_max_tokens
                )
                try:
                    start = ai_response_retry.index("{")
                    end = ai_response_retry.rindex("}") + 1
                    data = json.loads(ai_response_retry[start:end])
                    logger.info("Retry with larger token budget succeeded")
                    return _build_report_from_data(data)
                except (ValueError, json.JSONDecodeError):
                    repaired_retry = repair_truncated_json(ai_response_retry)
                    if repaired_retry and isinstance(repaired_retry.get("executive_summary"), str):
                        logger.info("JSON repair succeeded on retry")
                        return _build_report_from_data(repaired_retry)
            except Exception as retry_err:
                logger.warning(f"Retry LLM call failed: {retry_err}")

            logger.error("All JSON parse/repair attempts failed, using raw excerpt")
            exec_excerpt = ai_response[:1500]
            es_match = re.search(
                r'"executive_summary"\s*:\s*"((?:[^"\\]|\\.)*)"',
                ai_response,
                re.DOTALL,
            )
            if es_match:
                exec_excerpt = es_match.group(1).replace('\\"', '"').replace("\\n", "\n")

            return {
                "executive_summary": exec_excerpt,
                "summary": exec_excerpt,
                "key_findings": [],
                "data_table": [],
                "conflicts_uncertainty": [],
                "confidence_level": "Low",
                "confidence_reason": (
                    "AI response could not be parsed as structured JSON after repair attempts."
                ),
                "detailed_analysis": "",
                "recommendations": "",
                "cited_sources": cited_sources_list,
                "citation_audit": {"total_citations": 0, "faithfulness_score": 1.0},
            }

        except Exception as e:
            logger.error(f"Synthesis LLM call failed: {e}", exc_info=True)
            return {
                "executive_summary": f"Error during synthesis: {str(e)}",
                "summary": f"Error during synthesis: {str(e)}",
                "key_findings": [],
                "data_table": [],
                "conflicts_uncertainty": [],
                "confidence_level": "Low",
                "confidence_reason": "Synthesis step failed with an exception.",
                "detailed_analysis": "",
                "recommendations": "",
                "cited_sources": cited_sources_list,
                "citation_audit": {"total_citations": 0, "faithfulness_score": 1.0},
            }

    # ------------------------------------------------------------------
    # Direct-URL research path (no search engine)
    # ------------------------------------------------------------------

    async def _research_direct_url(
        self,
        query: str,
        direct_urls: list[str],
        max_sources: Optional[int] = None,
        deep_mode: bool = False,
        no_synthesis: bool = False,
        research_profile: ResearchProfile = "technical",
        progress_sink: Optional[Callable[[str], None]] = None,
    ) -> ResearchReport:
        """Scrape one or more explicit URLs instead of running a search.

        Called automatically from :meth:`research` when the query contains
        an ``http(s)://`` URL.  Subpages are discovered and crawled when the
        query contains subpage-crawl intent keywords (e.g. "alt sayfalarını
        da tara" / "crawl subpages").
        """
        include_subpages = has_subpage_crawl_intent(query)
        root_url = direct_urls[0]

        if progress_sink:
            progress_sink(self._msg("gathering_data"))

        # Max subpages: honour explicit max_sources from caller if supplied
        subpage_budget = (max_sources - 1) if max_sources and max_sources > 1 else self._DIRECT_CRAWL_MAX_SUBPAGES

        sources_to_check = await self._crawl_direct_url(
            root_url=root_url,
            include_subpages=include_subpages,
            query=query,
            deep_mode=deep_mode,
            progress_sink=progress_sink,
            max_subpages=subpage_budget,
        )

        # Also include any additional explicit URLs beyond the first
        root_urls_seen = {root_url}
        for extra_url in direct_urls[1:]:
            if extra_url not in root_urls_seen:
                sources_to_check.append({"type": "direct", "url": extra_url, "title": extra_url})
                root_urls_seen.add(extra_url)

        num_sources = len(sources_to_check)

        if progress_sink:
            progress_sink(self._msg("sources_to_check", count=num_sources))

        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def scrape_with_limit(source_config: dict) -> ResearchResult:
            async with semaphore:
                return await self._scrape_source(source_config, query, deep_mode)

        tasks = [scrape_with_limit(s) for s in sources_to_check]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        research_results: list[ResearchResult] = []
        successful = 0

        for i, result in enumerate(raw_results):
            if isinstance(result, BaseException):
                logger.error("Direct-URL source failed: %s — %s", sources_to_check[i]["url"], result)
                research_results.append(
                    ResearchResult(
                        source=sources_to_check[i]["type"],
                        url=sources_to_check[i]["url"],
                        title="",
                        content="",
                        error=str(result),
                    )
                )
            else:
                research_results.append(result)
                successful += 1

        if progress_sink:
            total_chars = sum(len(r.content) for r in research_results if not r.error)
            progress_sink(self._msg("results_summary", successful=successful, total=num_sources))
            progress_sink(self._msg("total_content", chars=f"{total_chars:,}"))
            progress_sink(self._msg("synthesizing"))

        if no_synthesis:
            synthesis: dict = {
                "summary": "[AI synthesis disabled - showing raw content from sources]",
                "key_findings": [],
            }
        else:
            synthesis = await self._synthesize_findings(query, research_results, deep_mode, None)

        report = ResearchReport(
            query=query,
            sources=research_results,
            summary=synthesis.get("summary", ""),
            executive_summary=synthesis.get("executive_summary", ""),
            key_findings=synthesis.get("key_findings", []),
            detailed_analysis=synthesis.get("detailed_analysis", ""),
            recommendations=synthesis.get("recommendations", ""),
            data_table=synthesis.get("data_table", []),
            conflicts_uncertainty=synthesis.get("conflicts_uncertainty", []),
            confidence_level=synthesis.get("confidence_level", "Medium"),
            confidence_reason=synthesis.get("confidence_reason", ""),
            sources_checked=num_sources,
            sources_succeeded=successful,
            sources_failed=num_sources - successful,
        )

        await self._close_http_client()
        return report

    # ------------------------------------------------------------------
    # Public API — non-streaming
    # ------------------------------------------------------------------

    async def research(
        self,
        query: str,
        max_sources: Optional[int] = None,
        deep_mode: bool = False,
        no_synthesis: bool = False,
        research_profile: ResearchProfileInput = "auto",
        progress_sink: Optional[Callable[[str], None]] = None,
    ) -> ResearchReport:
        """Perform comprehensive research on *query*.

        Args:
            query: Research question or topic.
            max_sources: Maximum sources to scrape (AI decides if ``None``).
            deep_mode: When ``True``, retrieve full content from each source.
            no_synthesis: Skip AI synthesis and return raw content.
            progress_sink: Optional callback for incremental status strings.

        Returns:
            :class:`ResearchReport` populated with findings.
        """
        self._query_lang = detect_query_language(query)
        if research_profile == "auto":
            selected_profile = self._detect_profile_from_query(query)
        else:
            selected_profile = self._normalize_profile(research_profile)

        if progress_sink:
            progress_sink(self._msg("starting_research", query=query))
            progress_sink(self._msg("preparing_queries"))

        logger.info(f"Starting research on: {query}")

        cleaned_query = clean_query_text(query)

        # ---- Direct-URL fast path: bypass search engine entirely --------
        # When the user supplies a URL in the query (e.g.
        # "https://example.com/ bu siteyi ve alt sayfalarını tara"),
        # skip DDG/Google search and scrape the target directly.
        direct_urls = extract_direct_urls(query)
        if direct_urls:
            return await self._research_direct_url(
                query=query,
                direct_urls=direct_urls,
                max_sources=max_sources,
                deep_mode=deep_mode,
                no_synthesis=no_synthesis,
                research_profile=selected_profile,
                progress_sink=progress_sink,
            )

        # Fire query-rewrite and early search concurrently
        rewrite_task = asyncio.create_task(self._prepare_search_queries(query, deep_mode=deep_mode))

        early_search_queries = [cleaned_query] if cleaned_query else [query]
        preliminary_max = self._resolve_target_source_count(
            requested_max_sources=max_sources,
            ai_suggested_sources=None,
            deep_mode=deep_mode,
        )
        preliminary_pool_size = min(
            max(
                preliminary_max
                + (
                    config.research_search_pool_extra_deep
                    if deep_mode
                    else config.research_search_pool_extra_normal
                ),
                config.research_deep_min_sources if deep_mode else preliminary_max,
            ),
            config.research_deep_max_sources,
        )

        early_search_task = asyncio.create_task(
            self._collect_search_results(
                query=cleaned_query or query,
                search_queries=early_search_queries,
                search_pool_size=preliminary_pool_size,
                target_count=preliminary_max,
                temporal_scope=None,
                research_profile=selected_profile,
                deep_mode=deep_mode,
            )
        )

        search_context = await rewrite_task
        effective_query = search_context["normalized_query"] or cleaned_query
        search_queries = search_context["search_queries"] or [effective_query]
        temporal_scope = search_context.get("temporal_scope")

        early_search_collection = await early_search_task
        early_results = early_search_collection["results"]

        new_variant_queries = [q for q in search_queries if q != cleaned_query and q != query]
        if new_variant_queries:
            try:
                extra_collection = await self._collect_search_results(
                    query=effective_query,
                    search_queries=new_variant_queries,
                    search_pool_size=preliminary_pool_size,
                    target_count=preliminary_max,
                    temporal_scope=temporal_scope,
                    research_profile=selected_profile,
                    deep_mode=deep_mode,
                )
                seen_urls = {r.get("url") for r in early_results if r.get("url")}
                for r in extra_collection["results"]:
                    url = r.get("url")
                    if url and url not in seen_urls:
                        early_results.append(r)
                        seen_urls.add(url)
            except Exception:
                logger.warning("Extra variant search failed, using early results only")

        if progress_sink:
            progress_sink(self._msg("planning_strategy"))
            if len(search_queries) > 1 or effective_query != cleaned_query:
                progress_sink(self._msg("search_ready_query", query=effective_query))
                progress_sink(self._msg("search_variants", count=len(search_queries)))

        strategy = await self._plan_research_with_results(
            query=effective_query,
            max_sources=max_sources,
            deep_mode=deep_mode,
            search_results=early_results,
            progress_sink=progress_sink,
        )
        sources_to_check = strategy["sources"]
        num_sources = len(sources_to_check)
        research_depth = strategy.get("depth", "standard")

        if progress_sink:
            progress_sink(self._msg("research_plan"))
            progress_sink(self._msg("sources_to_check", count=num_sources))
            progress_sink(
                self._msg(
                    "source_types",
                    types=", ".join(s["type"] for s in sources_to_check),
                )
            )
            progress_sink(self._msg("research_depth_label", depth=research_depth))
            progress_sink("")
            progress_sink(self._msg("gathering_data"))

        logger.info("Gathering data from sources...")
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def scrape_with_limit(source_config: dict) -> ResearchResult:
            async with semaphore:
                return await self._scrape_source(source_config, effective_query, deep_mode)

        tasks = [scrape_with_limit(s) for s in sources_to_check]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        research_results: list[ResearchResult] = []
        successful = 0

        for i, result in enumerate(raw_results):
            if isinstance(result, BaseException):
                logger.error(f"Source failed: {sources_to_check[i]['url']} - {result}")
                if progress_sink:
                    progress_sink(
                        self._msg(
                            "source_failed",
                            source=sources_to_check[i]["type"],
                            error=str(result)[:50],
                        )
                    )
                research_results.append(
                    ResearchResult(
                        source=sources_to_check[i]["type"],
                        url=sources_to_check[i]["url"],
                        title="",
                        content="",
                        error=str(result),
                    )
                )
            else:
                if progress_sink:
                    progress_sink(
                        self._msg(
                            "source_success",
                            source=result.source,
                            title=result.title[:40],
                            chars=len(result.content),
                        )
                    )
                research_results.append(result)
                successful += 1

        if progress_sink:
            progress_sink("")
            progress_sink(self._msg("results_summary", successful=successful, total=num_sources))
            total_chars = sum(len(r.content) for r in research_results if not r.error)
            progress_sink(self._msg("total_content", chars=f"{total_chars:,}"))
            progress_sink("")
            progress_sink(self._msg("synthesizing"))

        if no_synthesis:
            logger.info("Skipping AI synthesis (no_synthesis flag set)")
            synthesis: dict = {
                "summary": "[AI synthesis disabled - showing raw content from sources]",
                "key_findings": [],
            }
        else:
            logger.info("Analyzing and synthesizing findings...")
            synthesis = await self._synthesize_findings(
                query, research_results, deep_mode, temporal_scope
            )

        report = ResearchReport(
            query=query,
            sources=research_results,
            summary=synthesis.get("summary", ""),
            executive_summary=synthesis.get("executive_summary", ""),
            key_findings=synthesis.get("key_findings", []),
            detailed_analysis=synthesis.get("detailed_analysis", ""),
            recommendations=synthesis.get("recommendations", ""),
            data_table=synthesis.get("data_table", []),
            conflicts_uncertainty=synthesis.get("conflicts_uncertainty", []),
            confidence_level=synthesis.get("confidence_level", "Medium"),
            confidence_reason=synthesis.get("confidence_reason", ""),
            sources_checked=num_sources,
            sources_succeeded=successful,
            sources_failed=num_sources - successful,
        )

        await self._close_http_client()
        return report

    # ------------------------------------------------------------------
    # Public API — streaming (SSE)
    # ------------------------------------------------------------------

    async def _research_stream_direct_url(
        self,
        query: str,
        direct_urls: list[str],
        max_sources: Optional[int] = None,
        deep_mode: bool = False,
        research_profile: ResearchProfile = "technical",
    ):
        """SSE generator for the direct-URL fast path."""
        import json as _json

        include_subpages = has_subpage_crawl_intent(query)
        root_url = direct_urls[0]
        subpage_budget = (max_sources - 1) if max_sources and max_sources > 1 else self._DIRECT_CRAWL_MAX_SUBPAGES

        yield f"data: {_json.dumps({'type': 'status', 'message': self._msg('gathering_data')})}\n\n"

        sources_to_check = await self._crawl_direct_url(
            root_url=root_url,
            include_subpages=include_subpages,
            query=query,
            deep_mode=deep_mode,
            max_subpages=subpage_budget,
        )

        # Additional explicit URLs beyond the first
        root_urls_seen = {root_url}
        for extra_url in direct_urls[1:]:
            if extra_url not in root_urls_seen:
                sources_to_check.append({"type": "direct", "url": extra_url, "title": extra_url})
                root_urls_seen.add(extra_url)

        num_sources = len(sources_to_check)
        yield f"data: {_json.dumps({'type': 'status', 'message': self._msg('sources_to_check', count=num_sources)})}\n\n"

        for sc in sources_to_check:
            yield f"data: {_json.dumps({'type': 'source_start', 'url': sc['url'], 'title': sc.get('title', sc['url'])})}\n\n"

        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def scrape_single(source_config: dict) -> dict:
            try:
                async with semaphore:
                    result = await self._scrape_source(source_config, query, deep_mode)
                return {"config": source_config, "result": result, "error": None}
            except Exception as exc:
                return {"config": source_config, "result": None, "error": str(exc)}

        scrape_tasks = [scrape_single(s) for s in sources_to_check]
        research_results: list[ResearchResult] = []
        successful = 0

        for coro in asyncio.as_completed(scrape_tasks):
            scraped = await coro
            sc = scraped["config"]
            url = sc["url"]
            title = sc.get("title", url)
            if scraped["error"]:
                yield f"data: {_json.dumps({'type': 'source_complete', 'url': url, 'title': title, 'success': False})}\n\n"
                research_results.append(
                    ResearchResult(source=sc["type"], url=url, title="", content="", error=scraped["error"])
                )
            else:
                res = scraped["result"]
                yield f"data: {_json.dumps({'type': 'source_complete', 'url': url, 'title': res.title or title, 'success': True})}\n\n"
                research_results.append(res)
                successful += 1

        total_chars = sum(len(r.content) for r in research_results if not r.error)
        yield f"data: {_json.dumps({'type': 'status', 'message': self._msg('gathered_chars', chars=f'{total_chars:,}', successful=successful, total=num_sources)})}\n\n"
        yield f"data: {_json.dumps({'type': 'status', 'message': self._msg('synthesizing')})}\n\n"

        synthesis = await self._synthesize_findings(query, research_results, deep_mode, None)

        report_dict = {
            "query": query,
            "executive_summary": synthesis.get("executive_summary", ""),
            "summary": synthesis.get("summary", ""),
            "key_findings": synthesis.get("key_findings", []),
            "data_table": synthesis.get("data_table", []),
            "conflicts_uncertainty": synthesis.get("conflicts_uncertainty", []),
            "confidence_level": synthesis.get("confidence_level", "Medium"),
            "confidence_reason": synthesis.get("confidence_reason", ""),
            "detailed_analysis": synthesis.get("detailed_analysis", ""),
            "recommendations": synthesis.get("recommendations", ""),
            "sources": [
                {
                    "source": s.source,
                    "url": s.url,
                    "title": s.title,
                    "content": s.content,
                    "relevance_score": s.relevance_score,
                    "source_tier": s.source_tier,
                    "publication_date": s.publication_date,
                    "error": s.error,
                }
                for s in research_results
            ],
            "sources_checked": num_sources,
            "sources_succeeded": successful,
            "sources_failed": num_sources - successful,
            "cited_sources": synthesis.get("cited_sources", []),
        }

        yield f"data: {_json.dumps({'type': 'result', 'data': report_dict})}\n\n"

    async def research_stream(
        self,
        query: str,
        max_sources: Optional[int] = None,
        deep_mode: bool = False,
        research_profile: ResearchProfileInput = "auto",
    ):
        """Yield Server-Sent Event strings while researching *query*."""
        import json as _json

        self._query_lang = detect_query_language(query)
        if research_profile == "auto":
            selected_profile = self._detect_profile_from_query(query)
        else:
            selected_profile = self._normalize_profile(research_profile)

        yield (
            f"data: {_json.dumps({'type': 'status', 'message': self._msg('starting_research', query=query)})}\n\n"
        )
        yield (
            f"data: {_json.dumps({'type': 'status', 'message': self._msg('preparing_queries')})}\n\n"
        )

        try:
            cleaned_query = clean_query_text(query)

            # ---- Direct-URL fast path ----
            direct_urls = extract_direct_urls(query)
            if direct_urls:
                async for event in self._research_stream_direct_url(
                    query=query,
                    direct_urls=direct_urls,
                    max_sources=max_sources,
                    deep_mode=deep_mode,
                    research_profile=selected_profile,
                ):
                    yield event
                return

            rewrite_task = asyncio.create_task(
                self._prepare_search_queries(query, deep_mode=deep_mode)
            )

            early_search_queries = [cleaned_query] if cleaned_query else [query]
            preliminary_max = self._resolve_target_source_count(
                requested_max_sources=max_sources,
                ai_suggested_sources=None,
                deep_mode=deep_mode,
            )
            preliminary_pool_size = min(
                max(
                    preliminary_max
                    + (
                        config.research_search_pool_extra_deep
                        if deep_mode
                        else config.research_search_pool_extra_normal
                    ),
                    config.research_deep_min_sources if deep_mode else preliminary_max,
                ),
                config.research_deep_max_sources,
            )

            early_search_task = asyncio.create_task(
                self._collect_search_results(
                    query=cleaned_query or query,
                    search_queries=early_search_queries,
                    search_pool_size=preliminary_pool_size,
                    target_count=preliminary_max,
                    temporal_scope=None,
                    research_profile=selected_profile,
                    deep_mode=deep_mode,
                )
            )

            search_context = await rewrite_task
            effective_query = search_context["normalized_query"] or cleaned_query
            search_queries = search_context["search_queries"] or [effective_query]
            temporal_scope = search_context.get("temporal_scope")

            early_search_collection = await early_search_task
            early_results = early_search_collection["results"]

            new_variant_queries = [q for q in search_queries if q != cleaned_query and q != query]
            if new_variant_queries:
                try:
                    extra_collection = await self._collect_search_results(
                        query=effective_query,
                        search_queries=new_variant_queries,
                        search_pool_size=preliminary_pool_size,
                        target_count=preliminary_max,
                        temporal_scope=temporal_scope,
                        research_profile=selected_profile,
                        deep_mode=deep_mode,
                    )
                    seen_urls = {r.get("url") for r in early_results if r.get("url")}
                    for r in extra_collection["results"]:
                        url = r.get("url")
                        if url and url not in seen_urls:
                            early_results.append(r)
                            seen_urls.add(url)
                except Exception:
                    logger.warning("Extra variant search failed, using early results only")

            if len(search_queries) > 1 or effective_query != cleaned_query:
                yield (
                    f"data: {_json.dumps({'type': 'status', 'message': self._msg('generated_variants', count=len(search_queries))})}\n\n"
                )

            yield (
                f"data: {_json.dumps({'type': 'status', 'message': self._msg('planning_strategy')})}\n\n"
            )

            _plan_progress_queue: asyncio.Queue[str] = asyncio.Queue()
            plan_task = asyncio.create_task(
                self._plan_research_with_results(
                    query=effective_query,
                    max_sources=max_sources,
                    deep_mode=deep_mode,
                    search_results=early_results,
                    progress_sink=_plan_progress_queue.put_nowait,
                )
            )
            while not plan_task.done():
                await asyncio.sleep(0.05)
                while not _plan_progress_queue.empty():
                    _m = _plan_progress_queue.get_nowait()
                    yield f"data: {_json.dumps({'type': 'status', 'message': _m})}\n\n"
            while not _plan_progress_queue.empty():
                _m = _plan_progress_queue.get_nowait()
                yield f"data: {_json.dumps({'type': 'status', 'message': _m})}\n\n"
            strategy = await plan_task

            sources_to_check = strategy["sources"]
            num_sources = len(sources_to_check)
            research_depth = strategy.get("depth", "standard")

            yield (
                f"data: {_json.dumps({'type': 'status', 'message': self._msg('found_sources', count=num_sources, depth=research_depth)})}\n\n"
            )
            yield (
                f"data: {_json.dumps({'type': 'status', 'message': self._msg('gathering_data')})}\n\n"
            )

            research_results: list[ResearchResult] = []
            successful = 0

            for source_config in sources_to_check:
                url = source_config["url"]
                source_type = source_config.get("title", source_config["type"])
                yield (
                    f"data: {_json.dumps({'type': 'source_start', 'url': url, 'title': source_type})}\n\n"
                )

            semaphore = asyncio.Semaphore(self.max_concurrent)

            async def scrape_single(source_config: dict) -> dict:
                try:
                    async with semaphore:
                        result = await self._scrape_source(
                            source_config, effective_query, deep_mode
                        )
                    return {"config": source_config, "result": result, "error": None}
                except Exception as e:
                    return {"config": source_config, "result": None, "error": str(e)}

            scrape_tasks = [scrape_single(s) for s in sources_to_check]

            for coro in asyncio.as_completed(scrape_tasks):
                scraped = await coro
                source_config = scraped["config"]
                url = source_config["url"]
                source_type = source_config.get("title", source_config["type"])

                if scraped["error"]:
                    yield (
                        f"data: {_json.dumps({'type': 'source_complete', 'url': url, 'title': source_type, 'success': False})}\n\n"
                    )
                    research_results.append(
                        ResearchResult(
                            source=source_config["type"],
                            url=url,
                            title="",
                            content="",
                            error=scraped["error"],
                        )
                    )
                else:
                    result = scraped["result"]
                    yield (
                        f"data: {_json.dumps({'type': 'source_complete', 'url': url, 'title': result.title or source_type, 'success': True})}\n\n"
                    )
                    research_results.append(result)
                    successful += 1

            total_chars = sum(len(r.content) for r in research_results if not r.error)
            yield (
                f"data: {_json.dumps({'type': 'status', 'message': self._msg('gathered_chars', chars=f'{total_chars:,}', successful=successful, total=num_sources)})}\n\n"
            )

            yield (
                f"data: {_json.dumps({'type': 'status', 'message': self._msg('synthesizing')})}\n\n"
            )

            synthesis = await self._synthesize_findings(
                query, research_results, deep_mode, temporal_scope
            )

            report_dict = {
                "query": query,
                "executive_summary": synthesis.get("executive_summary", ""),
                "summary": synthesis.get("summary", ""),
                "key_findings": synthesis.get("key_findings", []),
                "data_table": synthesis.get("data_table", []),
                "conflicts_uncertainty": synthesis.get("conflicts_uncertainty", []),
                "confidence_level": synthesis.get("confidence_level", "Medium"),
                "confidence_reason": synthesis.get("confidence_reason", ""),
                "detailed_analysis": synthesis.get("detailed_analysis", ""),
                "recommendations": synthesis.get("recommendations", ""),
                "sources": [
                    {
                        "source": s.source,
                        "url": s.url,
                        "title": s.title,
                        "content": s.content,
                        "relevance_score": s.relevance_score,
                        "source_tier": s.source_tier,
                        "publication_date": s.publication_date,
                        "error": s.error,
                    }
                    for s in research_results
                ],
                "sources_checked": num_sources,
                "sources_succeeded": successful,
                "sources_failed": num_sources - successful,
                "cited_sources": synthesis.get("cited_sources", []),
            }

            yield f"data: {_json.dumps({'type': 'result', 'data': report_dict})}\n\n"

        except Exception as _stream_exc:
            logger.error(
                "research_stream_internal_error",
                extra={"query_preview": query[:120], "error": str(_stream_exc)},
                exc_info=True,
            )
            yield (
                f"data: {_json.dumps({'type': 'error', 'message': self._msg('research_failed', error=str(_stream_exc)[:200])})}\n\n"
            )
        finally:
            await self._close_http_client()

    # ------------------------------------------------------------------
    # CLI report formatter
    # ------------------------------------------------------------------

    def format_report(self, report: ResearchReport, no_synthesis: bool = False) -> str:
        """Format a ``ResearchReport`` as a human-readable CLI string."""
        lines: list[str] = []
        lines.append("=" * 70)
        lines.append("🔬 RESEARCH REPORT")
        lines.append(f"Query: {report.query}")
        lines.append("=" * 70)
        lines.append("")

        if no_synthesis:
            lines.append("DETAILED CONTENT FROM SOURCES (AI synthesis disabled)")
            lines.append("-" * 70)
            lines.append("")

            for i, result in enumerate(report.sources, 1):
                if result.error:
                    continue
                lines.append(f"\n{'=' * 70}")
                lines.append(f"📚 SOURCE {i}: {result.source.upper()}")
                lines.append(f"{'=' * 70}")
                lines.append(f"Title: {result.title}")
                lines.append(f"URL: {result.url}")
                lines.append(f"Relevance: {result.relevance_score:.0%}")
                lines.append(f"Content Length: {len(result.content):,} characters")
                lines.append("-" * 70)
                lines.append("")
                content = (
                    result.content
                    if len(result.content) < config.research_non_deep_source_char_cap
                    else (
                        result.content[: config.research_non_deep_source_char_cap]
                        + "\n\n[Content truncated - full content available in raw data]"
                    )
                )
                lines.append(content)
                lines.append("")
        else:
            lines.append("📋 EXECUTIVE SUMMARY")
            lines.append("-" * 70)
            lines.append(report.executive_summary or report.summary)
            lines.append("")

            if report.key_findings:
                lines.append("🔑 KEY FINDINGS")
                lines.append("-" * 70)
                for i, finding in enumerate(report.key_findings, 1):
                    lines.append(f"{i}. {finding}")
                lines.append("")

            if report.data_table:
                lines.append("📊 DATA TABLE")
                lines.append("-" * 70)
                for row in report.data_table:
                    metric = row.get("metric", "")
                    value = row.get("value", "")
                    source = row.get("source", "")
                    date = row.get("date", "unknown")
                    lines.append(f"  {metric}: {value}  [Source: {source}, {date}]")
                lines.append("")

            if report.conflicts_uncertainty:
                lines.append("⚠️  CONFLICTS & UNCERTAINTY")
                lines.append("-" * 70)
                for item in report.conflicts_uncertainty:
                    lines.append(f"  • {item}")
                lines.append("")

            lines.append(f"🎯 CONFIDENCE: {report.confidence_level}")
            if report.confidence_reason:
                lines.append(f"   {report.confidence_reason}")
            lines.append("")

        lines.append("📚 SOURCES CHECKED")
        lines.append("-" * 70)
        for result in report.sources:
            status = "✅" if not result.error else "❌"
            relevance = (
                f"({result.relevance_score:.0%} relevant)" if result.relevance_score > 0 else ""
            )
            lines.append(f"{status} {result.source}: {result.title[:60]}... {relevance}")
            if result.error:
                lines.append(f"   Error: {result.error[:80]}")
        lines.append("")

        lines.append("📊 STATISTICS")
        lines.append("-" * 70)
        lines.append(f"Sources checked: {report.sources_checked}")
        lines.append(f"Sources succeeded: {report.sources_succeeded}")
        success_rate = (
            report.sources_succeeded / report.sources_checked * 100
            if report.sources_checked > 0
            else 0.0
        )
        lines.append(f"Success rate: {success_rate:.1f}%")
        lines.append("=" * 70)

        return "\n".join(lines)
