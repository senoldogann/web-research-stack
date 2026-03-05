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
from urllib.parse import quote_plus, urlsplit

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
    build_ambiguous_query_clarification_prompt,
    build_cross_language_variant_prompt,
    build_lite_query_rewrite_prompt,
    build_query_rewrite_prompt,
    build_source_count_decision_prompt,
    build_source_selection_prompt,
    build_synthesis_prompt,
)
from web_scraper.research.ranking import (
    expand_selected_sources,
    get_freshness_score,
    is_soft_error_result,
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
    normalize_result_url,
)

logger = logging.getLogger(__name__)
ResearchProfile = Literal["technical", "news", "academic", "general"]
ResearchProfileInput = Literal["technical", "news", "academic", "general", "auto"]
IntentClass = Literal[
    "current_events",
    "model_release",
    "technical_docs",
    "benchmark_compare",
    "evergreen_general",
]

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

_EXPLICIT_DEEP_REQUEST_KW: frozenset[str] = frozenset(
    {
        "deep",
        "in-depth",
        "comprehensive",
        "comprehensively",
        "detaylı",
        "detayli",
        "kapsamlı",
        "kapsamli",
        "akademik",
        "literature review",
        "full report",
        "thorough",
    }
)

_COMPLEX_QUERY_HINT_KW: frozenset[str] = frozenset(
    {
        "compare",
        "comparison",
        "versus",
        "vs",
        "tradeoff",
        "benchmark",
        "karşılaştır",
        "karsilastir",
        "fark",
        "avantaj",
        "dezavantaj",
        "alternatif",
        "adım adım",
        "adim adim",
    }
)

_CURRENT_EVENTS_INTENT_KW: frozenset[str] = frozenset(
    {
        "what is happening",
        "what's happening",
        "happening in the world",
        "world right now",
        "current events",
        "breaking news",
        "latest news",
        "neler oluyor",
        "dünyada neler oluyor",
        "dunyada neler oluyor",
        "şu an dünyada",
        "su an dunyada",
        "gündem",
        "gundem",
    }
)

_MODEL_RELEASE_INTENT_KW: frozenset[str] = frozenset(
    {
        "latest model",
        "latest openai model",
        "latest anthropic model",
        "new model",
        "openai model",
        "anthropic model",
        "model release",
        "release notes",
        "which llm model best",
        "best llm model",
        "gpt-",
        "claude",
        "opus",
        "gemini",
        "model version",
        "llm leaderboard",
        "model leaderboard",
        "en iyi llm",
        "en iyi model",
        "son model",
        "yeni model",
        "model sürümü",
        "model surumu",
    }
)

_TECHNICAL_DOCS_INTENT_KW: frozenset[str] = frozenset(
    {
        "api docs",
        "documentation",
        "how to implement",
        "how to use",
        "sdk",
        "reference",
        "error:",
        "stack trace",
        "nasıl yapılır",
        "nasil yapilir",
        "dokümantasyon",
        "dokumantasyon",
    }
)

_BENCHMARK_INTENT_KW: frozenset[str] = frozenset(
    {
        "benchmark",
        "leaderboard",
        "vs",
        "versus",
        "compare",
        "comparison",
        "kıyas",
        "kiyas",
        "karşılaştır",
        "karsilastir",
    }
)

_CURRENT_EVENTS_PRIORITY_DOMAINS: tuple[str, ...] = (
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "ft.com",
    "economist.com",
    "npr.org",
    "aljazeera.com",
)

_MODEL_RELEASE_PRIORITY_DOMAINS: tuple[str, ...] = (
    "openai.com",
    "platform.openai.com",
    "anthropic.com",
    "docs.anthropic.com",
    "support.claude.com",
    "ai.google.dev",
    "lmarena.ai",
    "artificialanalysis.ai",
    "huggingface.co",
    "swebench.com",
)

_CURRENT_EVENTS_SEED_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("https://www.reuters.com/world/", "Reuters World News", "Global current events and world news coverage."),
    ("https://apnews.com/world-news", "AP World News", "International breaking news and current events."),
    ("https://www.bbc.com/news/world", "BBC World", "Latest world developments and breaking headlines."),
)

_MODEL_RELEASE_SEED_SOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "https://openai.com/index/",
        "OpenAI News Index",
        "Official OpenAI model release announcements and updates.",
    ),
    (
        "https://help.openai.com/en/articles/9624314-model-release-notes",
        "OpenAI Model Release Notes",
        "Official OpenAI model release notes and version timeline.",
    ),
    (
        "https://support.claude.com/en/articles/12304248-claude-release-notes",
        "Claude Release Notes",
        "Official Anthropic Claude model release notes and updates.",
    ),
    (
        "https://docs.anthropic.com/en/docs/about-claude/models/overview",
        "Anthropic Models Overview",
        "Official Anthropic Claude model versions and capabilities.",
    ),
    (
        "https://lmarena.ai/leaderboard",
        "Chatbot Arena Leaderboard",
        "Live LLM leaderboard and benchmark comparisons.",
    ),
    (
        "https://artificialanalysis.ai/",
        "Artificial Analysis Leaderboard",
        "Independent LLM benchmark and model ranking intelligence index.",
    ),
)

_MODEL_RELEASE_SEED_SOURCES_OPENAI: tuple[tuple[str, str, str], ...] = (
    (
        "https://openai.com/index/",
        "OpenAI News Index",
        "Official OpenAI model release announcements and updates.",
    ),
    (
        "https://help.openai.com/en/articles/9624314-model-release-notes",
        "OpenAI Model Release Notes",
        "Official OpenAI model release notes and version timeline.",
    ),
    (
        "https://platform.openai.com/docs/models",
        "OpenAI Models Documentation",
        "Official OpenAI models documentation and latest model references.",
    ),
    (
        "https://lmarena.ai/leaderboard",
        "Chatbot Arena Leaderboard",
        "Live LLM leaderboard and benchmark comparisons.",
    ),
    (
        "https://artificialanalysis.ai/",
        "Artificial Analysis Leaderboard",
        "Independent LLM benchmark and model ranking intelligence index.",
    ),
)

_MODEL_RELEASE_SEED_SOURCES_ANTHROPIC: tuple[tuple[str, str, str], ...] = (
    (
        "https://support.claude.com/en/articles/12304248-claude-release-notes",
        "Claude Release Notes",
        "Official Anthropic Claude model release notes and updates.",
    ),
    (
        "https://docs.anthropic.com/en/docs/about-claude/models/overview",
        "Anthropic Models Overview",
        "Official Anthropic Claude model versions and capabilities.",
    ),
    (
        "https://anthropic.com/news",
        "Anthropic News",
        "Official Anthropic announcements for model releases.",
    ),
    (
        "https://lmarena.ai/leaderboard",
        "Chatbot Arena Leaderboard",
        "Live LLM leaderboard and benchmark comparisons.",
    ),
    (
        "https://artificialanalysis.ai/",
        "Artificial Analysis Leaderboard",
        "Independent LLM benchmark and model ranking intelligence index.",
    ),
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
        """Infer the most appropriate research profile from *query* text.

        Profiles:
        - "academic"  : scientific papers, research, studies
        - "news"      : current events, breaking news, recent updates
        - "technical" : programming, code, APIs, documentation
        - "general"   : open-ended factual / encyclopaedic questions
                        (who is the strongest person, best country, etc.)
        """
        lower = query.lower()

        _ACADEMIC_KW: frozenset[str] = frozenset(
            {
                "paper",
                "study",
                "research",
                "journal",
                "arxiv",
                "pubmed",
                "doi",
                "citation",
                "abstract",
                "hypothesis",
                "experiment",
                "peer review",
                "peer-review",
                "literature review",
                "methodology",
                "meta-analysis",
                "clinical trial",
                "dissertation",
                "thesis",
                # Turkish
                "makale",
                "araştırma",
                "çalışma",
                "tez",
                "yayın",
                "akademik",
                "bilimsel",
                "dergi",
                "atıf",
                "deneysel",
            }
        )
        _NEWS_KW: frozenset[str] = frozenset(
            {
                "news",
                "breaking",
                "latest",
                "today",
                "yesterday",
                "this week",
                "this month",
                "current",
                "update",
                "announcement",
                "headline",
                "report",
                "journalist",
                "press",
                "coverage",
                "incident",
                "what is happening",
                "what's happening",
                "world right now",
                # Turkish
                "haber",
                "son dakika",
                "güncel",
                "bugün",
                "dün",
                "bu hafta",
                "bu ay",
                "gelişme",
                "açıklama",
                "basın",
                "neler oluyor",
                "dünyada neler oluyor",
                "dunyada neler oluyor",
            }
        )
        _TECHNICAL_KW: frozenset[str] = frozenset(
            {
                "code",
                "coding",
                "programming",
                "function",
                "class",
                "method",
                "api",
                "library",
                "framework",
                "tutorial",
                "how to implement",
                "how to use",
                "syntax",
                "debug",
                "error",
                "exception",
                "docker",
                "kubernetes",
                "sql",
                "database",
                "python",
                "javascript",
                "typescript",
                "java",
                "golang",
                "rust",
                "react",
                "node",
                "bash",
                "shell",
                # Turkish
                "kod",
                "kodlama",
                "programlama",
                "fonksiyon",
                "kütüphane",
                "nasıl yapılır",
                "nasıl kullanılır",
                "nasıl yazılır",
            }
        )
        # General / encyclopaedic query signals: "who is", "what is", "which is best",
        # superlatives, comparisons without technical context
        _GENERAL_KW: frozenset[str] = frozenset(
            {
                "who is",
                "who are",
                "what is the best",
                "what is the strongest",
                "what is the most",
                "which is the best",
                "which country",
                "which person",
                "strongest person",
                "richest person",
                "most powerful",
                "most famous",
                "greatest",
                # Turkish
                "kim",
                "kimdir",
                "en güçlü",
                "en zengin",
                "en güzel",
                "en iyi",
                "en büyük",
                "en küçük",
                "en hızlı",
                "en akıllı",
                "en başarılı",
                "en etkili",
                "dünyada en",
                "dünyanın en",
                "hangi ülke",
                "hangi kişi",
                "hangi şirket",
                "ne kadar",
                "nedir",
                "nerede",
                "nerelidir",
            }
        )

        import re as _re

        def _kw_match(keywords: frozenset, text: str) -> bool:
            """Match keywords with word-boundary awareness.

            Multi-word keywords are matched as substrings (they inherently carry
            context).  Single-word keywords are matched as whole words only to
            avoid false positives from substrings (e.g. "dün" inside "dünyanın").
            """
            for kw in keywords:
                if " " in kw:
                    # Multi-word phrase: substring match is fine
                    if kw in text:
                        return True
                else:
                    # Single word: require word boundary so "dün" doesn't match "dünyanın"
                    if _re.search(
                        r"(?<![a-zçğıöşüâîû])" + _re.escape(kw) + r"(?![a-zçğıöşüâîû])", text
                    ):
                        return True
            return False

        if _kw_match(_ACADEMIC_KW, lower):
            return "academic"
        if _kw_match(_NEWS_KW, lower):
            return "news"
        if _kw_match(_TECHNICAL_KW, lower):
            return "technical"
        if _kw_match(_GENERAL_KW, lower):
            return "general"
        return "general"

    @staticmethod
    def _normalize_profile(research_profile: str) -> ResearchProfile:
        allowed: set[str] = {"technical", "news", "academic", "general"}
        if research_profile in allowed:
            return research_profile  # type: ignore[return-value]
        return "general"

    @staticmethod
    def _detect_intent_class(query: str) -> IntentClass:
        q = (query or "").lower()

        if any(token in q for token in _CURRENT_EVENTS_INTENT_KW):
            return "current_events"
        if any(token in q for token in _MODEL_RELEASE_INTENT_KW):
            return "model_release"
        if any(token in q for token in _BENCHMARK_INTENT_KW):
            return "benchmark_compare"
        if any(token in q for token in _TECHNICAL_DOCS_INTENT_KW):
            return "technical_docs"
        return "evergreen_general"

    @staticmethod
    def _profile_for_intent(intent_class: IntentClass) -> ResearchProfile:
        if intent_class == "current_events":
            return "news"
        if intent_class in {"model_release", "technical_docs", "benchmark_compare"}:
            return "technical"
        return "general"

    @staticmethod
    def _status_event(
        message: str,
        phase: Optional[str] = None,
        code: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "status", "message": message}
        if phase:
            payload["phase"] = phase
        if code:
            payload["code"] = code
        return payload

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

    @staticmethod
    def _is_explicit_deep_request(query: str) -> bool:
        q = (query or "").lower()
        return any(token in q for token in _EXPLICIT_DEEP_REQUEST_KW)

    @staticmethod
    def _is_simple_single_intent_query(query: str, research_profile: ResearchProfile) -> bool:
        tokens = [t for t in clean_query_text(query).split() if t]
        if not tokens:
            return False
        q = " ".join(tokens).lower()

        has_complexity_hint = any(token in q for token in _COMPLEX_QUERY_HINT_KW)
        has_question_splitter = any(token in q for token in (" and ", " ve ", ";", " / ", " or "))

        # Technical/academic prompts often benefit from deeper retrieval even
        # when short, so only auto-downgrade for general/news-like quick asks.
        if research_profile in {"technical", "academic"}:
            return False

        return len(tokens) <= 10 and not has_complexity_hint and not has_question_splitter

    @classmethod
    def _resolve_execution_mode(
        cls,
        query: str,
        requested_deep_mode: bool,
        requested_max_sources: Optional[int],
        research_profile: ResearchProfile,
    ) -> tuple[bool, Optional[int], bool]:
        """Resolve effective mode/limits and whether auto-focus was applied."""
        if not requested_deep_mode:
            return False, requested_max_sources, False

        # Product policy: strict user control for deep mode.
        if config.research_strict_deep_mode:
            return True, requested_max_sources, False

        if cls._is_explicit_deep_request(query):
            return True, requested_max_sources, False

        if cls._is_simple_single_intent_query(query, research_profile):
            focused_cap = 8
            if requested_max_sources is None:
                return False, focused_cap, True
            return False, min(requested_max_sources, focused_cap), True

        return True, requested_max_sources, False

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

    async def _prepare_search_queries(
        self,
        query: str,
        deep_mode: bool = False,
        research_profile: ResearchProfile = "general",
    ) -> dict:
        """Turn raw user input into one or more search-ready query variants.

        Standard mode: runs a lightweight "lite rewrite" that only detects
        temporal scope and normalizes the query (adds current year when needed).
        For general-profile queries it also detects ambiguity and generates
        dimension-specific sub-queries.

        Deep mode: runs the full LLM rewrite to produce multiple search variants.
        """
        cleaned_query = clean_query_text(query)
        selected_profile = self._normalize_profile(research_profile)
        fallback_queries = self._normalize_search_queries(cleaned_query, cleaned_query, [])
        fallback = {
            "query_ready": True,
            "normalized_query": cleaned_query,
            "search_queries": fallback_queries,
            "rewrite_reason": "Input used directly",
            "temporal_scope": None,
            "is_ambiguous": False,
            "ambiguous_dimensions": [],
        }

        if not cleaned_query or not config.research_enable_query_rewrite:
            return fallback

        # ------------------------------------------------------------------
        # Standard mode: lite rewrite (temporal scope + ambiguity detection)
        # ------------------------------------------------------------------
        if not deep_mode:
            try:
                lite_prompt = build_lite_query_rewrite_prompt(cleaned_query)
                ai_response = await self._call_llm(
                    lite_prompt, config.research_query_rewrite_timeout_seconds
                )
                payload = extract_json_payload(ai_response)

                normalized_query = clean_query_text(
                    payload.get("normalized_query") or cleaned_query
                )
                temporal_scope = payload.get("temporal_scope")
                if not isinstance(temporal_scope, dict):
                    temporal_scope = None

                is_ambiguous = bool(payload.get("is_ambiguous", False))
                ambiguous_dimensions = payload.get("ambiguous_dimensions") or []
                if not isinstance(ambiguous_dimensions, list):
                    ambiguous_dimensions = []

                # Safety net: ensure year is appended for "current" scope
                if temporal_scope and temporal_scope.get("type") == "current":
                    current_year = str(datetime.now().year)
                    normalized_query_with_year = self._ensure_year_in_queries(
                        [normalized_query], current_year
                    )[0]
                else:
                    normalized_query_with_year = normalized_query

                search_queries = [normalized_query_with_year]

                # If ambiguous: generate dimension-specific sub-queries
                if selected_profile == "general" and is_ambiguous and ambiguous_dimensions:
                    try:
                        dim_prompt = build_ambiguous_query_clarification_prompt(
                            cleaned_query, ambiguous_dimensions
                        )
                        dim_response = await self._call_llm(
                            dim_prompt, config.research_query_rewrite_timeout_seconds
                        )
                        dim_payload = extract_json_payload(dim_response)
                        dim_queries = dim_payload.get("dimension_queries", [])
                        if isinstance(dim_queries, list):
                            extra = [
                                clean_query_text(dq["query"])
                                for dq in dim_queries
                                if isinstance(dq, dict) and isinstance(dq.get("query"), str)
                            ]
                            search_queries.extend(extra)
                            logger.info(
                                "Ambiguous query '%s' expanded into %d dimension queries: %s",
                                cleaned_query,
                                len(extra),
                                extra,
                            )
                    except Exception as dim_err:
                        logger.warning("Ambiguity dimension expansion failed: %s", dim_err)

                # Add one English variant for non-English queries so we can
                # retrieve authoritative global sources as well.
                cross_language_query = await self._build_cross_language_query_variant(
                    cleaned_query,
                    temporal_scope,
                )
                if cross_language_query:
                    # Place early so it survives max-variant trimming.
                    search_queries.insert(1, cross_language_query)

                search_queries = self._normalize_search_queries(
                    original_query=cleaned_query,
                    normalized_query=normalized_query_with_year,
                    search_queries=search_queries[1:],  # skip first (already normalized_query)
                )

                return {
                    "query_ready": True,
                    "normalized_query": search_queries[0]
                    if search_queries
                    else normalized_query_with_year,
                    "search_queries": search_queries
                    if search_queries
                    else [normalized_query_with_year],
                    "rewrite_reason": "Lite rewrite: temporal scope + ambiguity detection",
                    "temporal_scope": temporal_scope,
                    "is_ambiguous": is_ambiguous,
                    "ambiguous_dimensions": ambiguous_dimensions,
                }
            except Exception as lite_err:
                logger.warning("Lite query rewrite failed: %s", lite_err)
                return fallback

        # ------------------------------------------------------------------
        # Deep mode: full LLM rewrite with multiple search variants
        # ------------------------------------------------------------------
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

            # Add one English variant for non-English queries.
            cross_language_query = await self._build_cross_language_query_variant(
                cleaned_query,
                temporal_scope,
            )
            if cross_language_query:
                # Re-normalize with English variant near the front to keep it
                # under max-variant limits.
                search_queries = self._normalize_search_queries(
                    original_query=cleaned_query,
                    normalized_query=search_queries[0],
                    search_queries=[cross_language_query] + search_queries[1:],
                )

            return {
                "query_ready": query_ready if isinstance(query_ready, bool) else True,
                "normalized_query": search_queries[0],
                "search_queries": search_queries,
                "rewrite_reason": rewrite_reason if isinstance(rewrite_reason, str) else "",
                "temporal_scope": temporal_scope,
                "is_ambiguous": False,
                "ambiguous_dimensions": [],
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

    async def _build_cross_language_query_variant(
        self,
        query: str,
        temporal_scope: Optional[dict],
    ) -> Optional[str]:
        """Return one English query variant for non-English inputs.

        This prevents language lock-in where a Turkish query may retrieve mostly
        Turkish local sites and miss higher-authority global sources.
        """
        if not query:
            return None

        query_lang = getattr(self, "_query_lang", "en")
        if query_lang == "en":
            return None

        source_language = "Turkish" if query_lang == "tr" else query_lang
        prompt = build_cross_language_variant_prompt(
            query=query,
            source_language=source_language,
            target_language="English",
        )

        timeout_seconds = max(6.0, min(config.research_query_rewrite_timeout_seconds, 15.0))

        try:
            ai_response = await self._call_llm(prompt, timeout_seconds)
            payload = extract_json_payload(ai_response)
            target_query = clean_query_text(payload.get("target_query") or "")
            if not target_query:
                return None

            if temporal_scope and temporal_scope.get("type") == "current":
                current_year = str(datetime.now().year)
                target_query = self._ensure_year_in_queries([target_query], current_year)[0]

            if target_query.casefold() == query.casefold():
                return None

            return target_query
        except Exception as exc:
            logger.warning("Cross-language query variant generation failed: %s", exc)
            return None

    @staticmethod
    def _is_current_scope(temporal_scope: Optional[dict]) -> bool:
        return not temporal_scope or temporal_scope.get("type") == "current"

    @staticmethod
    def _is_model_release_query(query: str) -> bool:
        q = (query or "").lower()
        release_tokens = (
            "llm",
            "model",
            "version",
            "release",
            "leaderboard",
            "benchmark",
            "gpt",
            "claude",
            "opus",
            "new model",
            "latest model",
            "yeni model",
            "son model",
            "model sürümü",
            "model surumu",
            "sürüm",
            "surum",
        )
        return any(token in q for token in release_tokens)

    @staticmethod
    def _has_fresh_authoritative_results(results: list[dict], top_n: int = 6) -> bool:
        for result in results[:top_n]:
            tier = classify_source_tier(str(result.get("url", "") or ""))
            freshness = get_freshness_score(result)
            if tier <= 3 and freshness >= 0.20:
                return True
        return False

    def _build_release_priority_queries(
        self,
        query: str,
        search_queries: list[str],
    ) -> list[str]:
        current_year = str(datetime.now().year)
        year_pattern = re.compile(r"\b(19|20)\d{2}\b")
        vendor_domains = (
            "openai.com",
            "platform.openai.com",
            "anthropic.com",
            "docs.anthropic.com",
            "support.claude.com",
            "ai.google.dev",
            "lmarena.ai",
            "huggingface.co",
        )

        seeds = [clean_query_text(s) for s in (search_queries[:2] or [query])]
        seeds = [s for s in seeds if s]
        if not seeds:
            return []

        candidates: list[str] = []
        for seed in seeds:
            base = seed if year_pattern.search(seed) else f"{seed} {current_year}"
            for domain in vendor_domains:
                candidates.append(f"{base} site:{domain}")

        # Keep deterministic ordering and avoid unbounded query expansion.
        seen: set[str] = set()
        prioritized: list[str] = []
        for candidate in candidates:
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            prioritized.append(candidate)
            if len(prioritized) >= 8:
                break

        return prioritized

    @staticmethod
    def _tier_counts(results: list[dict], top_n: int = 8) -> dict[str, int]:
        counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0, "tier5": 0}
        for result in results[:top_n]:
            tier = classify_source_tier(str(result.get("url", "") or ""))
            key = f"tier{tier}"
            if key in counts:
                counts[key] += 1
        return counts

    @staticmethod
    def _freshness_summary(results: list[dict]) -> dict[str, Any]:
        newest_date: Optional[str] = None
        newest_year: Optional[int] = None
        recency_window_days: Optional[int] = None
        today = datetime.now().date()

        for result in results:
            publication_date = str(result.get("publication_date") or "").strip()
            if not publication_date:
                continue
            candidate_date: Optional[datetime] = None
            if len(publication_date) >= 10:
                try:
                    candidate_date = datetime.strptime(publication_date[:10], "%Y-%m-%d")
                except ValueError:
                    candidate_date = None
            if candidate_date is None and len(publication_date) >= 4 and publication_date[:4].isdigit():
                try:
                    candidate_date = datetime(int(publication_date[:4]), 1, 1)
                except ValueError:
                    candidate_date = None
            if candidate_date is None:
                continue

            if newest_date is None or candidate_date.date() > datetime.strptime(newest_date, "%Y-%m-%d").date():
                newest_date = candidate_date.strftime("%Y-%m-%d")
                newest_year = candidate_date.year
                recency_window_days = max((today - candidate_date.date()).days, 0)

        return {
            "newest_date": newest_date,
            "newest_year": newest_year,
            "recency_window_days": recency_window_days,
        }

    @staticmethod
    def _normalize_hostname(url: str) -> str:
        try:
            hostname = urlsplit(url).netloc.lower()
            if hostname.startswith("www."):
                hostname = hostname[4:]
            return hostname
        except Exception:  # noqa: S110
            return ""

    @classmethod
    def _has_official_release_source(cls, results: list[dict], top_n: int = 8) -> bool:
        official_domains = {
            "openai.com",
            "platform.openai.com",
            "help.openai.com",
            "anthropic.com",
            "docs.anthropic.com",
            "support.claude.com",
            "ai.google.dev",
        }
        for result in results[:top_n]:
            hostname = cls._normalize_hostname(str(result.get("url", "") or ""))
            if hostname in official_domains:
                return True
        return False

    @classmethod
    def _has_benchmark_source(cls, results: list[dict], top_n: int = 8) -> bool:
        benchmark_domains = {
            "lmarena.ai",
            "artificialanalysis.ai",
            "swebench.com",
            "huggingface.co",
        }
        for result in results[:top_n]:
            hostname = cls._normalize_hostname(str(result.get("url", "") or ""))
            if hostname in benchmark_domains:
                return True
        return False

    @staticmethod
    def _needs_benchmark_source(query: str) -> bool:
        q = (query or "").lower()
        return any(
            token in q
            for token in (
                "best",
                "leaderboard",
                "benchmark",
                "compare",
                "vs",
                "versus",
                "en iyi",
                "kıyas",
                "kiyas",
                "karşılaştır",
                "karsilastir",
            )
        )

    @staticmethod
    def _query_mentions_vendor(query: str) -> bool:
        q = (query or "").lower()
        return any(token in q for token in ("openai", "anthropic", "claude", "gemini", "google ai"))

    @classmethod
    def _passes_authority_floor(
        cls,
        query: str,
        results: list[dict],
        intent_class: IntentClass,
        top_n: int = 8,
    ) -> bool:
        counts = cls._tier_counts(results, top_n=top_n)
        high_authority = counts["tier1"] + counts["tier2"] + counts["tier3"]

        if intent_class == "current_events":
            return high_authority >= 1

        if intent_class == "model_release":
            official_present = cls._has_official_release_source(results, top_n=top_n)
            benchmark_present = cls._has_benchmark_source(results, top_n=top_n)
            vendor_required = cls._query_mentions_vendor(query)
            authority_score = high_authority + (1 if benchmark_present else 0)
            if authority_score < 2:
                return False
            if vendor_required and not official_present:
                return False
            if not vendor_required and not (official_present or benchmark_present):
                return False
            if cls._needs_benchmark_source(query) and not (benchmark_present or official_present):
                return False
            return True

        if intent_class in {"technical_docs", "benchmark_compare"}:
            return high_authority >= 1

        return True

    @staticmethod
    def _recency_hint_for_intent(intent_class: IntentClass) -> Optional[str]:
        if intent_class == "current_events":
            return "day"
        if intent_class == "model_release":
            return "month"
        return None

    def _build_intent_priority_queries(
        self,
        query: str,
        search_queries: list[str],
        intent_class: IntentClass,
    ) -> list[str]:
        current_year = str(datetime.now().year)
        year_pattern = re.compile(r"\b(19|20)\d{2}\b")
        seeds = [clean_query_text(s) for s in (search_queries[:2] or [query])]
        seeds = [s for s in seeds if s]
        if not seeds:
            return []

        if intent_class == "model_release":
            domains = _MODEL_RELEASE_PRIORITY_DOMAINS
            seeds.extend(
                [
                    f"OpenAI release notes {current_year}",
                    f"Anthropic release notes {current_year}",
                    f"LLM leaderboard {current_year}",
                ]
            )
        elif intent_class == "current_events":
            domains = _CURRENT_EVENTS_PRIORITY_DOMAINS
            seeds.extend(
                [
                    f"world news today {current_year}",
                    f"global breaking news {current_year}",
                    f"international headlines {current_year}",
                ]
            )
        else:
            domains = ()

        candidates: list[str] = []
        for seed in seeds:
            base = seed if year_pattern.search(seed) else f"{seed} {current_year}"
            for domain in domains:
                candidates.append(f"{base} site:{domain}")

        seen: set[str] = set()
        prioritized: list[str] = []
        for candidate in candidates:
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            prioritized.append(candidate)
            if len(prioritized) >= 10:
                break
        return prioritized

    @classmethod
    def _promote_priority_results(
        cls,
        ranked: list[dict],
        intent_class: IntentClass,
        limit: int,
    ) -> list[dict]:
        if not ranked:
            return ranked

        def _is_priority(result: dict) -> bool:
            hostname = cls._normalize_hostname(str(result.get("url", "") or ""))
            if intent_class == "model_release":
                return hostname in set(_MODEL_RELEASE_PRIORITY_DOMAINS)
            if intent_class == "current_events":
                return hostname in set(_CURRENT_EVENTS_PRIORITY_DOMAINS) or classify_source_tier(
                    str(result.get("url", "") or "")
                ) <= 3
            return False

        priority_urls: set[str] = set()
        priority_results: list[dict] = []
        for result in ranked:
            url = str(result.get("url", "") or "")
            if not url or url in priority_urls:
                continue
            if _is_priority(result):
                priority_results.append(result)
                priority_urls.add(url)

        if not priority_results:
            return ranked[:limit]

        priority_results.sort(
            key=lambda r: (
                get_freshness_score(r),
                float(r.get("rank_score", 0.0) or 0.0),
            ),
            reverse=True,
        )

        merged: list[dict] = []
        seen: set[str] = set()
        for result in priority_results + ranked:
            url = str(result.get("url", "") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(result)
            if len(merged) >= limit:
                break
        return merged

    @classmethod
    def _build_intent_seed_results(
        cls,
        query: str,
        intent_class: IntentClass,
    ) -> list[dict]:
        if intent_class == "current_events":
            seeds = _CURRENT_EVENTS_SEED_SOURCES
            publication_date = datetime.now().strftime("%Y-%m-%d")
        elif intent_class == "model_release":
            q = (query or "").lower()
            if "openai" in q or "gpt" in q:
                seeds = _MODEL_RELEASE_SEED_SOURCES_OPENAI
            elif "anthropic" in q or "claude" in q or "opus" in q:
                seeds = _MODEL_RELEASE_SEED_SOURCES_ANTHROPIC
            else:
                seeds = _MODEL_RELEASE_SEED_SOURCES
            publication_date = None
        else:
            return []

        results: list[dict] = []
        for url, title, snippet in seeds:
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": f"{query} — {snippet}",
                    "source": cls._normalize_hostname(url) or "seed",
                    "search_provider": "seed",
                    "search_query": query,
                    "publication_date": publication_date,
                }
            )
        return results

    @staticmethod
    def _result_rows_from_research_results(results: list[ResearchResult]) -> list[dict]:
        rows: list[dict] = []
        for result in results:
            if result.error:
                continue
            rows.append(
                {
                    "url": result.url,
                    "publication_date": result.publication_date,
                    "source_tier": result.source_tier,
                }
            )
        return rows

    def _evaluate_final_evidence(
        self,
        query: str,
        intent_class: IntentClass,
        temporal_scope: Optional[dict],
        research_results: list[ResearchResult],
    ) -> dict[str, Any]:
        rows = self._result_rows_from_research_results(research_results)
        top_n = min(12, len(rows)) if rows else 0
        authority_tier_counts = self._tier_counts(rows, top_n=top_n) if rows else {
            "tier1": 0,
            "tier2": 0,
            "tier3": 0,
            "tier4": 0,
            "tier5": 0,
        }
        freshness_summary = self._freshness_summary(rows) if rows else {
            "newest_date": None,
            "newest_year": None,
            "recency_window_days": None,
        }

        if config.research_evidence_gate_enabled:
            if rows:
                evidence_gate_passed = self._passes_authority_floor(
                    query, rows, intent_class, top_n=top_n
                )
                if self._is_current_scope(temporal_scope) and intent_class == "current_events":
                    evidence_gate_passed = evidence_gate_passed and self._has_fresh_authoritative_results(
                        rows, top_n=top_n
                    )
            else:
                evidence_gate_passed = False
        else:
            evidence_gate_passed = True

        high_authority = (
            authority_tier_counts.get("tier1", 0)
            + authority_tier_counts.get("tier2", 0)
            + authority_tier_counts.get("tier3", 0)
        )
        fresh_authoritative = (
            self._has_fresh_authoritative_results(rows, top_n=top_n) if rows else False
        )

        should_abstain = False
        if not rows:
            should_abstain = True
        elif intent_class == "current_events":
            # For real-time world/news queries, one strong source is not enough
            # to safely synthesize broad claims.
            should_abstain = high_authority < 2 or not fresh_authoritative
        elif intent_class == "model_release":
            # Version/model comparisons must not proceed when the authority gate fails.
            should_abstain = not evidence_gate_passed

        return {
            "rows": rows,
            "authority_tier_counts": authority_tier_counts,
            "freshness_summary": freshness_summary,
            "evidence_gate_passed": bool(evidence_gate_passed),
            "should_abstain": bool(should_abstain),
        }

    @staticmethod
    def _build_result_lookup(results: list[dict]) -> dict[str, dict]:
        lookup: dict[str, dict] = {}
        for result in results:
            url = str(result.get("url", "") or "")
            if not url:
                continue
            lookup[url] = result
            try:
                lookup.setdefault(normalize_result_url(url), result)
            except Exception:  # noqa: S110
                pass
        return lookup

    def _source_rows_from_planned_sources(
        self,
        planned_sources: list[dict],
        fallback_results: list[dict],
    ) -> list[dict]:
        rows: list[dict] = []
        fallback_lookup = self._build_result_lookup(fallback_results)
        for source in planned_sources:
            url = str(source.get("url", "") or "")
            if not url:
                continue
            matched = fallback_lookup.get(url)
            if matched is None:
                try:
                    matched = fallback_lookup.get(normalize_result_url(url))
                except Exception:  # noqa: S110
                    matched = None
            row = {"url": url}
            publication_date = None
            if isinstance(matched, dict):
                publication_date = matched.get("publication_date")
            if publication_date:
                row["publication_date"] = publication_date
            rows.append(row)
        return rows

    def _should_fallback_to_ranked_sources(
        self,
        query: str,
        intent_class: IntentClass,
        planned_sources: list[dict],
        fallback_results: list[dict],
    ) -> bool:
        if intent_class not in {"current_events", "model_release"}:
            return False

        planned_rows = self._source_rows_from_planned_sources(planned_sources, fallback_results)
        if not planned_rows:
            return True

        top_n = min(8, len(planned_rows))
        return not self._passes_authority_floor(
            query=query,
            results=planned_rows,
            intent_class=intent_class,
            top_n=top_n,
        )

    def _enforce_intent_quality_guardrail(
        self,
        query: str,
        planned_sources: list[dict],
        fallback_results: list[dict],
        target_count: int,
        research_profile: ResearchProfile,
        intent_class: IntentClass,
    ) -> list[dict]:
        def _build_authoritative_fallback_sources() -> list[dict]:
            if intent_class not in {"current_events", "model_release"}:
                return []

            model_release_priority_domains = {
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

            picked: list[dict] = []
            seen_urls: set[str] = set()
            for result in fallback_results:
                url = str(result.get("url", "") or "")
                if not url or url in seen_urls:
                    continue
                hostname = self._normalize_hostname(url)
                tier = classify_source_tier(url)

                authoritative = tier <= 3
                if intent_class == "model_release" and hostname in model_release_priority_domains:
                    authoritative = True

                if not authoritative:
                    continue

                picked.append(
                    {
                        "type": result.get("source", "unknown"),
                        "url": url,
                        "title": result.get("title", ""),
                        "priority": len(picked) + 1,
                    }
                )
                seen_urls.add(url)
                if len(picked) >= target_count:
                    break

            return picked

        if not self._should_fallback_to_ranked_sources(
            query=query,
            intent_class=intent_class,
            planned_sources=planned_sources,
            fallback_results=fallback_results,
        ):
            return planned_sources

        deterministic_sources = expand_selected_sources(
            selected_sources=[],
            fallback_results=fallback_results,
            target_count=target_count,
            query=query,
            research_profile=research_profile,
            intent_class=intent_class,
        )
        if deterministic_sources and not self._should_fallback_to_ranked_sources(
            query=query,
            intent_class=intent_class,
            planned_sources=deterministic_sources,
            fallback_results=fallback_results,
        ):
            logger.info(
                "Replacing low-authority LLM-selected sources with ranked fallback for intent=%s",
                intent_class,
            )
            return deterministic_sources
        authoritative_sources = _build_authoritative_fallback_sources()
        if authoritative_sources:
            logger.info(
                "Using authoritative fallback sources for intent=%s after strict quality gate",
                intent_class,
            )
            return authoritative_sources
        if deterministic_sources:
            logger.info(
                "Using deterministic fallback sources for intent=%s despite weak authority mix",
                intent_class,
            )
            return deterministic_sources
        return planned_sources

    @staticmethod
    def _build_recency_priority_queries(
        query: str,
        search_queries: list[str],
        intent_class: IntentClass,
    ) -> list[str]:
        base_queries = [clean_query_text(s) for s in (search_queries[:2] or [query])]
        base_queries = [q for q in base_queries if q]
        if not base_queries:
            return []

        if intent_class == "current_events":
            suffixes = ("today", "this week", str(datetime.now().year))
        elif intent_class == "model_release":
            suffixes = ("release notes", "latest", str(datetime.now().year))
        else:
            suffixes = (str(datetime.now().year),)

        candidates: list[str] = []
        for base in base_queries:
            for suffix in suffixes:
                candidates.append(f"{base} {suffix}".strip())

        seen: set[str] = set()
        normalized: list[str] = []
        for candidate in candidates:
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(candidate)
        return normalized[:8]

    @staticmethod
    def _filter_release_query_results(results: list[dict], limit: int) -> list[dict]:
        """Remove low-relevance pages from model-release queries.

        Keeps official domains and benchmark sources, and otherwise requires
        explicit LLM/model/version signals in title/snippet.
        """
        if not results:
            return []

        allow_domains = {
            "openai.com",
            "platform.openai.com",
            "help.openai.com",
            "anthropic.com",
            "docs.anthropic.com",
            "support.claude.com",
            "ai.google.dev",
            "deepmind.google",
            "lmarena.ai",
            "artificialanalysis.ai",
            "huggingface.co",
        }
        topical_tokens = (
            "llm",
            "language model",
            "model version",
            "release notes",
            "leaderboard",
            "benchmark",
            "gpt",
            "claude",
            "gemini",
            "opus",
            "deepseek",
            "qwen",
            "mistral",
        )

        filtered: list[dict] = []
        for result in results:
            url = str(result.get("url", "") or "")
            title = str(result.get("title", "") or "")
            snippet = str(result.get("snippet", "") or "")
            text = f"{title} {snippet}".lower()
            try:
                hostname = urlsplit(url).netloc.lower()
                if hostname.startswith("www."):
                    hostname = hostname[4:]
            except Exception:  # noqa: S110
                hostname = ""

            if hostname in allow_domains:
                filtered.append(result)
                continue

            if any(token in text for token in topical_tokens):
                filtered.append(result)

        return filtered[:limit]

    # ------------------------------------------------------------------
    # Search collection
    # ------------------------------------------------------------------

    async def _collect_duckduckgo_results(
        self,
        search_queries: list[str],
        search_pool_size: int,
        temporal_scope: Optional[dict] = None,
        recency_hint: Optional[str] = None,
    ) -> list[dict]:
        """Search all query variants concurrently and merge unique DuckDuckGo results."""
        if not search_queries:
            return []

        per_query_budget = min(
            max(5, math.ceil(search_pool_size / max(len(search_queries), 1)) + 2),
            config.research_deep_max_sources,
        )

        date_filter = recency_hint
        if date_filter is None and temporal_scope and temporal_scope.get("type") == "current":
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
        temporal_scope: Optional[dict] = None,
        recency_hint: Optional[str] = None,
    ) -> list[dict]:
        """Search all Google query variants concurrently.

        When *temporal_scope* type is "current", appends the current year to
        queries that do not already contain a year token.  Google does not
        expose a machine-readable date-filter parameter in the standard HTML
        interface, so year-appending is the most reliable freshness signal.
        """
        if not search_queries:
            return []

        per_query_budget = min(
            max(3, math.ceil(search_pool_size / max(len(search_queries), 1))),
            config.research_deep_max_sources,
        )

        # Apply year-based freshness to Google queries when scope is "current"
        effective_queries = search_queries
        if temporal_scope and temporal_scope.get("type") == "current":
            current_year = str(datetime.now().year)
            effective_queries = self._ensure_year_in_queries(search_queries, current_year)

        if recency_hint:
            recency_token_map = {
                "day": "today",
                "week": "this week",
                "month": "this month",
                "year": str(datetime.now().year),
            }
            recency_token = recency_token_map.get(recency_hint)
            if recency_token:
                effective_queries = [f"{q} {recency_token}".strip() for q in effective_queries]

        async def _search_variant(search_query: str) -> tuple[str, list[dict]]:
            results = await async_retry(
                lambda: get_best_sources(search_query, max_sources=per_query_budget),
                max_attempts=2,
                base_delay=2.0,
                label=f"Google:{search_query[:40]}",
            )
            return search_query, results

        variant_outcomes = await asyncio.gather(
            *[_search_variant(q) for q in effective_queries],
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
        intent_class: IntentClass = "evergreen_general",
        status_sink: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict:
        """Collect DDG and Google results concurrently, then merge and rank."""
        ddg_results: list[dict] = []
        google_results: list[dict] = []
        profile_results: list[dict] = []
        ddg_error: Optional[str] = None
        google_error: Optional[str] = None
        profile_error: Optional[str] = None
        selected_profile = self._normalize_profile(research_profile)
        retrieval_attempts = 1

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
                            temporal_scope=temporal_scope,
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
                # Wikipedia + StackExchange in parallel for technical/general deep mode
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
        else:
            # Standard mode quality boost: add one lightweight profile collector
            # for non-technical intents to avoid low-quality local-only results.
            _profile_timeout = config.duckduckgo_request_timeout_seconds
            _profile_pool = min(max(2, target_count), 6)

            if selected_profile == "academic":
                labeled_tasks.append(
                    (
                        "profile",
                        asyncio.create_task(
                            collect_arxiv_results(
                                search_queries=search_queries,
                                search_pool_size=_profile_pool,
                                timeout_seconds=_profile_timeout,
                            )
                        ),
                    )
                )
            elif selected_profile == "news":
                labeled_tasks.append(
                    (
                        "profile",
                        asyncio.create_task(
                            collect_rss_feed_results(
                                search_queries=search_queries,
                                search_pool_size=_profile_pool,
                                timeout_seconds=_profile_timeout,
                            )
                        ),
                    )
                )
            elif selected_profile == "general":
                labeled_tasks.append(
                    (
                        "profile",
                        asyncio.create_task(
                            collect_wikipedia_results(
                                search_queries=search_queries,
                                search_pool_size=_profile_pool,
                                timeout_seconds=_profile_timeout,
                            )
                        ),
                    )
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
        ddg_effective_results = [r for r in ddg_results if not is_soft_error_result(r)]
        if (
            intent_class not in {"current_events", "model_release"}
            and ddg_effective_results
            and len(ddg_effective_results) >= min_google_fallback
        ):
            google_results = []

        ranked = merge_and_rank_search_results(
            query=query,
            result_sets=[ddg_results, google_results, profile_results],
            limit=search_pool_size,
            research_profile=research_profile,
        )
        ranked = self._promote_priority_results(ranked, intent_class=intent_class, limit=search_pool_size)

        if self._is_model_release_query(query):
            filtered_ranked = self._filter_release_query_results(ranked, search_pool_size)
            if filtered_ranked:
                ranked = filtered_ranked

        providers_used = sorted(
            {
                provider
                for result in ranked
                for provider in str(result.get("search_provider", "")).split(",")
                if provider
            }
        )
        fallback_used = bool(google_results)

        async def _run_retry_round(
            retry_queries: list[str],
            recency_hint: Optional[str] = None,
        ) -> tuple[list[dict], list[dict]]:
            nonlocal ddg_error, google_error
            priority_pool = min(max(8, search_pool_size), config.research_deep_max_sources)
            extra_ddg_results: list[dict] = []
            extra_google_results: list[dict] = []
            retry_tasks: list[tuple[str, asyncio.Task]] = [
                (
                    "duckduckgo",
                    asyncio.create_task(
                        self._collect_duckduckgo_results(
                            search_queries=retry_queries,
                            search_pool_size=priority_pool,
                            temporal_scope=temporal_scope,
                            recency_hint=recency_hint,
                        )
                    ),
                )
            ]
            if config.research_enable_google_fallback:
                retry_tasks.append(
                    (
                        "google",
                        asyncio.create_task(
                            self._collect_google_results(
                                search_queries=retry_queries,
                                search_pool_size=priority_pool,
                                temporal_scope=temporal_scope,
                                recency_hint=recency_hint,
                            )
                        ),
                    )
                )

            retry_outcomes = await asyncio.gather(
                *[task for _, task in retry_tasks],
                return_exceptions=True,
            )
            for (label, _), outcome in zip(retry_tasks, retry_outcomes):
                if isinstance(outcome, BaseException):
                    if label == "duckduckgo" and ddg_error is None:
                        ddg_error = str(outcome)
                    elif label == "google" and google_error is None:
                        google_error = str(outcome)
                    continue
                if label == "duckduckgo":
                    extra_ddg_results = list(outcome)  # type: ignore[arg-type]
                elif label == "google":
                    extra_google_results = list(outcome)  # type: ignore[arg-type]
            return extra_ddg_results, extra_google_results

        if config.research_retry_aggressive_enabled and intent_class in {
            "current_events",
            "model_release",
        }:
            authority_ok = self._passes_authority_floor(query, ranked, intent_class)
            freshness_ok = (
                self._has_fresh_authoritative_results(ranked)
                if self._is_current_scope(temporal_scope)
                else True
            )

            if not (authority_ok and freshness_ok):
                priority_queries = self._build_intent_priority_queries(
                    query=query,
                    search_queries=search_queries,
                    intent_class=intent_class,
                )
                if priority_queries:
                    retrieval_attempts += 1
                    if status_sink:
                        status_sink(
                            self._status_event(
                                message="Authority-oriented retry started.",
                                phase="retry",
                                code="AUTHORITY_RETRY_STARTED",
                            )
                        )
                    extra_ddg, extra_google = await _run_retry_round(priority_queries)
                    extra_ranked = merge_and_rank_search_results(
                        query=query,
                        result_sets=[extra_ddg, extra_google],
                        limit=search_pool_size,
                        research_profile=research_profile,
                    )
                    if extra_ranked:
                        ranked = merge_and_rank_search_results(
                            query=query,
                            result_sets=[ranked, extra_ranked],
                            limit=search_pool_size,
                            research_profile=research_profile,
                        )
                        ranked = self._promote_priority_results(
                            ranked, intent_class=intent_class, limit=search_pool_size
                        )
                        if intent_class == "model_release":
                            filtered_ranked = self._filter_release_query_results(
                                ranked, search_pool_size
                            )
                            if filtered_ranked:
                                ranked = filtered_ranked
                    fallback_used = fallback_used or bool(extra_google)
                    providers_used = sorted(
                        {
                            provider
                            for result in ranked
                            for provider in str(result.get("search_provider", "")).split(",")
                            if provider
                        }
                    )

            authority_ok = self._passes_authority_floor(query, ranked, intent_class)
            freshness_ok = (
                self._has_fresh_authoritative_results(ranked)
                if self._is_current_scope(temporal_scope)
                else True
            )
            if not (authority_ok and freshness_ok):
                recency_queries = self._build_recency_priority_queries(
                    query=query,
                    search_queries=search_queries,
                    intent_class=intent_class,
                )
                recency_hint = self._recency_hint_for_intent(intent_class)
                if recency_queries:
                    retrieval_attempts += 1
                    if status_sink:
                        status_sink(
                            self._status_event(
                                message="Recency-oriented retry started.",
                                phase="retry",
                                code="RECENCY_RETRY_STARTED",
                            )
                        )
                    extra_ddg, extra_google = await _run_retry_round(
                        recency_queries,
                        recency_hint=recency_hint,
                    )
                    extra_ranked = merge_and_rank_search_results(
                        query=query,
                        result_sets=[extra_ddg, extra_google],
                        limit=search_pool_size,
                        research_profile=research_profile,
                    )
                    if extra_ranked:
                        ranked = merge_and_rank_search_results(
                            query=query,
                            result_sets=[ranked, extra_ranked],
                            limit=search_pool_size,
                            research_profile=research_profile,
                        )
                        ranked = self._promote_priority_results(
                            ranked, intent_class=intent_class, limit=search_pool_size
                        )
                        if intent_class == "model_release":
                            filtered_ranked = self._filter_release_query_results(
                                ranked, search_pool_size
                            )
                            if filtered_ranked:
                                ranked = filtered_ranked
                    fallback_used = fallback_used or bool(extra_google)
                    providers_used = sorted(
                        {
                            provider
                            for result in ranked
                            for provider in str(result.get("search_provider", "")).split(",")
                            if provider
                        }
                    )

        if config.research_retry_aggressive_enabled and intent_class in {
            "current_events",
            "model_release",
        } and not self._passes_authority_floor(query, ranked, intent_class):
            seed_results = self._build_intent_seed_results(query=query, intent_class=intent_class)
            if seed_results:
                ranked = merge_and_rank_search_results(
                    query=query,
                    result_sets=[ranked, seed_results],
                    limit=search_pool_size,
                    research_profile=research_profile,
                )
                ranked = self._promote_priority_results(
                    ranked, intent_class=intent_class, limit=search_pool_size
                )
                if intent_class == "model_release":
                    filtered_ranked = self._filter_release_query_results(ranked, search_pool_size)
                    if filtered_ranked:
                        ranked = filtered_ranked
                providers_used = sorted(
                    {
                        provider
                        for result in ranked
                        for provider in str(result.get("search_provider", "")).split(",")
                        if provider
                    }
                )

        authority_tier_counts = self._tier_counts(ranked, top_n=min(8, len(ranked)))
        freshness_summary = self._freshness_summary(ranked)
        evidence_gate_passed = True
        if config.research_evidence_gate_enabled:
            evidence_gate_passed = self._passes_authority_floor(query, ranked, intent_class)
            if self._is_current_scope(temporal_scope) and intent_class == "current_events":
                evidence_gate_passed = evidence_gate_passed and self._has_fresh_authoritative_results(
                    ranked
                )

        return {
            "results": ranked,
            "providers_used": providers_used,
            "fallback_used": fallback_used,
            "ddg_error": ddg_error,
            "google_error": google_error,
            "profile_error": profile_error,
            "profile_provider_used": bool(profile_results),
            "retrieval_attempts": retrieval_attempts,
            "authority_tier_counts": authority_tier_counts,
            "freshness_summary": freshness_summary,
            "evidence_gate_passed": evidence_gate_passed,
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

                content = ""
                if not data.error:
                    content = sanitize_scraped_text(
                        data.content,
                        max_chars=config.max_source_content_chars,
                    )

                # Playwright escalation triggers in two cases:
                # 1) HTTP scraper returned an explicit error (403/challenge/JS walls)
                # 2) HTTP scraper succeeded but returned very thin content
                #
                # Previously we returned immediately on data.error and never tried
                # Playwright, which caused avoidable failures on JS/anti-bot pages.
                initial_error = str(data.error or "")
                lower_error = initial_error.lower()
                browser_required_hint = any(
                    token in lower_error
                    for token in (
                        "cloudflare",
                        "challenge",
                        "captcha",
                        "forbidden",
                        "403",
                        "429",
                        "503",
                        "javascript",
                        "bot",
                        "blocked",
                    )
                )
                should_try_playwright = bool(data.error and browser_required_hint) or (
                    not data.error and len(content.strip()) < self._PLAYWRIGHT_ESCALATION_MIN_CHARS
                )

                if should_try_playwright:
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
                            if data.error or len(pw_content.strip()) > len(content.strip()):
                                logger.debug(
                                    "Playwright escalation yielded %d chars for %s",
                                    len(pw_content),
                                    url,
                                )
                                data = pw_data
                                content = pw_content
                        elif data.error:
                            # Preserve original error context while indicating fallback failure.
                            fallback_error = str(pw_data.error or "no content from Playwright")
                            return ResearchResult(
                                source=source_type,
                                url=url,
                                title=data.title or "",
                                content="",
                                error=f"{initial_error}; Playwright fallback failed: {fallback_error}",
                            )
                    except Exception as pw_err:
                        logger.debug("Playwright escalation failed for %s: %s", url, pw_err)
                        if data.error:
                            return ResearchResult(
                                source=source_type,
                                url=url,
                                title=data.title or "",
                                content="",
                                error=f"{initial_error}; Playwright fallback error: {pw_err}",
                            )

                if data.error:
                    return ResearchResult(
                        source=source_type,
                        url=url,
                        title=data.title or "",
                        content="",
                        error=data.error,
                    )

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
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".svg",
            ".webp",
            ".ico",
            ".css",
            ".js",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
            ".eot",
            ".pdf",
            ".zip",
            ".tar",
            ".gz",
            ".rar",
            ".exe",
            ".dmg",
            ".mp4",
            ".mp3",
            ".avi",
            ".mov",
            ".wmv",
            ".flv",
            ".xml",
            ".json",
            ".txt",
            ".csv",
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
                "login",
                "logout",
                "register",
                "signup",
                "sign-up",
                "cart",
                "basket",
                "checkout",
                "payment",
                "order",
                "print",
                "share",
                "embed",
                "feed",
                "rss",
                "giris",
                "cikis",
                "uye-ol",
                "kayit",
                "sepet",
                "odeme",
                "search",
                "ara",
                "tag",
                "etiket",
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
            progress_sink(self._msg("sources_to_check", count=len(selected) + 1))

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
        research_profile: ResearchProfile = "technical",
        intent_class: IntentClass = "evergreen_general",
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
                    research_profile=research_profile,
                    deep_mode=deep_mode,
                    intent_class=intent_class,
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
                    research_profile=research_profile,
                    deep_mode=deep_mode,
                    intent_class=intent_class,
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
                    research_profile=research_profile,
                    intent_class=intent_class,
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
                planned_sources = expand_selected_sources(
                    selected_sources=valid_sources[:max_to_check],
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    query=query,
                    research_profile=research_profile,
                    intent_class=intent_class,
                )
                strategy["sources"] = self._enforce_intent_quality_guardrail(
                    query=query,
                    planned_sources=planned_sources,
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    research_profile=research_profile,
                    intent_class=intent_class,
                )
                strategy["depth"] = "deep"
                return strategy

        except Exception as e:
            logger.warning(f"AI parsing failed ({e}), using search results directly")

        planned_sources = expand_selected_sources(
            selected_sources=[],
            fallback_results=ddg_results,
            target_count=max_to_check,
            query=query,
            research_profile=research_profile,
            intent_class=intent_class,
        )
        return {
            "sources": self._enforce_intent_quality_guardrail(
                query=query,
                planned_sources=planned_sources,
                fallback_results=ddg_results,
                target_count=max_to_check,
                research_profile=research_profile,
                intent_class=intent_class,
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
        research_profile: ResearchProfile = "technical",
        intent_class: IntentClass = "evergreen_general",
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
                    research_profile=research_profile,
                    intent_class=intent_class,
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
                planned_sources = expand_selected_sources(
                    selected_sources=valid_sources[:max_to_check],
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    query=query,
                    research_profile=research_profile,
                    intent_class=intent_class,
                )
                strategy["sources"] = self._enforce_intent_quality_guardrail(
                    query=query,
                    planned_sources=planned_sources,
                    fallback_results=ddg_results,
                    target_count=max_to_check,
                    research_profile=research_profile,
                    intent_class=intent_class,
                )
                strategy["depth"] = "deep"
                return strategy

        except Exception as e:
            logger.warning(f"AI parsing failed ({e}), using search results directly")

        planned_sources = expand_selected_sources(
            selected_sources=[],
            fallback_results=ddg_results,
            target_count=max_to_check,
            query=query,
            research_profile=research_profile,
            intent_class=intent_class,
        )
        return {
            "sources": self._enforce_intent_quality_guardrail(
                query=query,
                planned_sources=planned_sources,
                fallback_results=ddg_results,
                target_count=max_to_check,
                research_profile=research_profile,
                intent_class=intent_class,
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

    @staticmethod
    def _compute_new_information_ratio(summary: str, detailed_analysis: str) -> float:
        """Estimate how much detailed_analysis adds beyond summary content."""
        if not detailed_analysis.strip():
            return 1.0

        def _tokens(text: str) -> set[str]:
            return {
                tok
                for tok in re.findall(r"[a-zA-Z0-9çğıöşüÇĞİÖŞÜ]{4,}", text.lower())
                if len(tok) >= 4
            }

        summary_tokens = _tokens(summary)
        detailed_tokens = _tokens(detailed_analysis)
        if not detailed_tokens:
            return 0.0
        new_tokens = detailed_tokens - summary_tokens
        return len(new_tokens) / max(len(detailed_tokens), 1)

    def _build_insufficient_evidence_response(
        self,
        query: str,
        intent_class: IntentClass,
        authority_tier_counts: dict[str, int],
        freshness_summary: dict[str, Any],
        retrieval_attempts: int,
    ) -> dict[str, Any]:
        newest_year = freshness_summary.get("newest_year")
        newest_date = freshness_summary.get("newest_date")
        tier_1_3 = (
            authority_tier_counts.get("tier1", 0)
            + authority_tier_counts.get("tier2", 0)
            + authority_tier_counts.get("tier3", 0)
        )
        lang = getattr(self, "_query_lang", "en")

        if lang == "tr":
            summary = (
                "Yeterli otoriter ve güncel kanıt bulunamadı; bu nedenle kesin bir yanıt vermek güvenli değil. "
                "Sistem ek arama turları çalıştırdı ancak kanıt eşiği yine de geçilemedi."
            )
            findings = [
                f"Top sonuçlarda Tier 1-3 kaynak sayısı yetersiz ({tier_1_3}).",
                f"Retrieval deneme sayısı: {retrieval_attempts}.",
                f"En yeni tarih sinyali: {newest_date or 'bilinmiyor'} (yıl: {newest_year or 'bilinmiyor'}).",
                f"Intent sınıfı: {intent_class}.",
            ]
            rec = (
                "Resmi kurum/vendor domainlerini veya daha net bir kapsam (zaman aralığı, bölge, kaynak türü) belirtip tekrar deneyin."
            )
        else:
            summary = (
                "Insufficient authoritative and fresh evidence was found, so a definitive answer would be unreliable. "
                "Additional retrieval retries were executed, but the evidence gate still failed."
            )
            findings = [
                f"High-authority (tier 1-3) sources in top results are insufficient ({tier_1_3}).",
                f"Retrieval attempts executed: {retrieval_attempts}.",
                f"Newest source signal: {newest_date or 'unknown'} (year: {newest_year or 'unknown'}).",
                f"Intent class: {intent_class}.",
            ]
            rec = (
                "Retry with explicit official domains and a tighter scope (time range, region, source type)."
            )

        return {
            "executive_summary": summary,
            "summary": summary,
            "key_findings": findings,
            "data_table": [],
            "conflicts_uncertainty": [
                "Evidence gate failed: authority/freshness minimum could not be satisfied."
            ],
            "confidence_level": "Low",
            "confidence_reason": "Insufficient authoritative evidence after aggressive retries.",
            "detailed_analysis": "",
            "recommendations": rec,
            "cited_sources": [],
            "extended_analysis_hidden": True,
        }

    @staticmethod
    def _fails_citation_faithfulness_gate(
        synthesis: dict[str, Any],
        intent_class: IntentClass,
    ) -> bool:
        """Return True when citation faithfulness is too weak for high-risk intents."""
        if intent_class not in {"current_events", "model_release"}:
            return False

        audit = synthesis.get("citation_audit")
        if not isinstance(audit, dict):
            return False

        try:
            faithfulness = float(audit.get("faithfulness_score", 1.0) or 0.0)
        except (TypeError, ValueError):
            return False

        return faithfulness < 0.50

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
                "extended_analysis_hidden": True,
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

            def _downgrade(level: str) -> str:
                if level == "High":
                    return "Medium"
                if level == "Medium":
                    return "Low"
                return "Low"

            calibrated_confidence = raw_confidence
            calibration_notes: list[str] = []

            # 1) Source-authority calibration
            authoritative_sources = sum(1 for r in citation_ordered if (r.source_tier or 5) <= 3)
            if authoritative_sources < 2:
                calibrated_confidence = _downgrade(calibrated_confidence)
                calibration_notes.append(
                    f"Only {authoritative_sources} high-authority source(s) (tier 1-3)."
                )

            # 2) Citation-faithfulness calibration
            faithfulness = float(audit.get("faithfulness_score", 1.0) or 0.0)
            if faithfulness < 0.50:
                calibrated_confidence = _downgrade(_downgrade(calibrated_confidence))
                calibration_notes.append(
                    f"Citation faithfulness is low ({faithfulness:.2f})."
                )
            elif faithfulness < 0.70:
                calibrated_confidence = _downgrade(calibrated_confidence)
                calibration_notes.append(
                    f"Citation faithfulness is moderate ({faithfulness:.2f})."
                )

            # 3) Recency calibration for current-topic queries
            years: list[int] = []
            for r in citation_ordered:
                pub = str(r.publication_date or "")
                if len(pub) >= 4 and pub[:4].isdigit():
                    years.append(int(pub[:4]))

            current_scope = not temporal_scope or temporal_scope.get("type") == "current"
            if current_scope:
                if years:
                    newest = max(years)
                    if newest <= datetime.now().year - 2:
                        calibrated_confidence = _downgrade(calibrated_confidence)
                        calibration_notes.append(
                            f"Newest source year is {newest}, which may be stale for a current query."
                        )
                elif calibrated_confidence == "High":
                    calibrated_confidence = "Medium"
                    calibration_notes.append("Most source publication dates are unknown.")

            base_reason = str(data.get("confidence_reason", "") or "").strip()
            if calibration_notes:
                extra_reason = " ".join(calibration_notes)
                confidence_reason = (
                    f"{base_reason} Calibration: {extra_reason}" if base_reason else extra_reason
                )
            else:
                confidence_reason = base_reason

            # Force uncertainty language for low-confidence outputs.
            if calibrated_confidence == "Low":
                if getattr(self, "_query_lang", "en") == "tr":
                    low_reason = "Kanıt gücü düşüktür; sonuçlar temkinli yorumlanmalıdır."
                else:
                    low_reason = "Evidence strength is low; interpret the findings cautiously."
                if calibration_notes:
                    confidence_reason = (
                        f"{low_reason} Calibration: {' '.join(calibration_notes)}"
                    )
                else:
                    confidence_reason = low_reason

                lower_summary = executive_summary.lower()
                uncertainty_markers = (
                    "limited evidence",
                    "insufficient evidence",
                    "uncertain",
                    "muhtemel",
                    "sınırlı kanıt",
                    "belirsiz",
                )
                if not any(marker in lower_summary for marker in uncertainty_markers):
                    prefix = (
                        "Kanıtlar sınırlı; aşağıdaki değerlendirme muhtemeldir. "
                        if getattr(self, "_query_lang", "en") == "tr"
                        else "Evidence is limited; the assessment below is tentative. "
                    )
                    executive_summary = prefix + executive_summary

            new_info_ratio = self._compute_new_information_ratio(
                executive_summary,
                detailed_analysis,
            )
            hide_extended = new_info_ratio < 0.22
            if hide_extended:
                detailed_analysis = ""

            return {
                "executive_summary": executive_summary,
                "summary": executive_summary,
                "key_findings": data.get("key_findings", []),
                "data_table": data.get("data_table", []),
                "conflicts_uncertainty": data.get("conflicts_uncertainty", []),
                "confidence_level": calibrated_confidence,
                "confidence_reason": confidence_reason,
                "detailed_analysis": detailed_analysis,
                "recommendations": recommendations,
                "cited_sources": cited_sources_list,
                "citation_audit": audit,
                "extended_analysis_hidden": hide_extended,
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
                "extended_analysis_hidden": True,
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
                "extended_analysis_hidden": True,
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
        subpage_budget = (
            (max_sources - 1)
            if max_sources and max_sources > 1
            else self._DIRECT_CRAWL_MAX_SUBPAGES
        )

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
                logger.error(
                    "Direct-URL source failed: %s — %s", sources_to_check[i]["url"], result
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
                "extended_analysis_hidden": True,
            }
        else:
            synthesis = await self._synthesize_findings(query, research_results, deep_mode, None)

        direct_intent = self._detect_intent_class(query)
        direct_tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0, "tier5": 0}
        direct_results_meta = [
            {"url": r.url, "publication_date": r.publication_date}
            for r in research_results
            if not r.error
        ]
        for item in direct_results_meta:
            tier = classify_source_tier(str(item.get("url", "") or ""))
            key = f"tier{tier}"
            if key in direct_tier_counts:
                direct_tier_counts[key] += 1

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
            intent_class=direct_intent,
            execution_mode_requested="deep" if deep_mode else "standard",
            execution_mode_effective="deep" if deep_mode else "standard",
            authority_tier_counts=direct_tier_counts,
            freshness_summary=self._freshness_summary(direct_results_meta),
            retrieval_attempts=1,
            evidence_gate_passed=True,
            extended_analysis_hidden=bool(synthesis.get("extended_analysis_hidden", False)),
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
        intent_class = self._detect_intent_class(query)
        if research_profile == "auto":
            if config.research_intent_router_v2:
                selected_profile = self._profile_for_intent(intent_class)
            else:
                selected_profile = self._detect_profile_from_query(query)
        else:
            selected_profile = self._normalize_profile(research_profile)
        effective_deep_mode, effective_max_sources, _ = self._resolve_execution_mode(
            query=query,
            requested_deep_mode=deep_mode,
            requested_max_sources=max_sources,
            research_profile=selected_profile,
        )

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
                max_sources=effective_max_sources,
                deep_mode=effective_deep_mode,
                no_synthesis=no_synthesis,
                research_profile=selected_profile,
                progress_sink=progress_sink,
            )

        # First run query-rewrite to get temporal_scope and variant queries.
        # In deep mode we still fire an early search concurrently with rewrite
        # using the cleaned query; the temporal_scope from rewrite is then applied
        # to a follow-up variant search.
        # In standard mode we await the (lightweight) lite rewrite first so that
        # the date_filter can be applied to the initial search correctly.
        preliminary_max = self._resolve_target_source_count(
            requested_max_sources=effective_max_sources,
            ai_suggested_sources=None,
            deep_mode=effective_deep_mode,
        )
        preliminary_pool_size = min(
            max(
                preliminary_max
                + (
                    config.research_search_pool_extra_deep
                    if effective_deep_mode
                    else config.research_search_pool_extra_normal
                ),
                config.research_deep_min_sources if effective_deep_mode else preliminary_max,
            ),
            config.research_deep_max_sources,
        )

        if effective_deep_mode:
            # Deep mode: fire rewrite and early search concurrently (temporal_scope
            # is applied to follow-up variant search after rewrite completes).
            rewrite_task = asyncio.create_task(
                self._prepare_search_queries(
                    query,
                    deep_mode=True,
                    research_profile=selected_profile,
                )
            )
            early_search_queries = [cleaned_query] if cleaned_query else [query]
            early_search_task = asyncio.create_task(
                self._collect_search_results(
                    query=cleaned_query or query,
                    search_queries=early_search_queries,
                    search_pool_size=preliminary_pool_size,
                    target_count=preliminary_max,
                    temporal_scope=None,
                    research_profile=selected_profile,
                    deep_mode=effective_deep_mode,
                    intent_class=intent_class,
                )
            )
            search_context = await rewrite_task
            effective_query = search_context["normalized_query"] or cleaned_query
            search_queries = search_context["search_queries"] or [effective_query]
            temporal_scope = search_context.get("temporal_scope")

            early_search_collection = await early_search_task
            early_results = early_search_collection["results"]
            search_telemetry = dict(early_search_collection)

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
                        deep_mode=effective_deep_mode,
                        intent_class=intent_class,
                    )
                    seen_urls = {r.get("url") for r in early_results if r.get("url")}
                    for r in extra_collection["results"]:
                        url = r.get("url")
                        if url and url not in seen_urls:
                            early_results.append(r)
                            seen_urls.add(url)
                    search_telemetry["retrieval_attempts"] = max(
                        int(search_telemetry.get("retrieval_attempts", 1) or 1),
                        int(extra_collection.get("retrieval_attempts", 1) or 1),
                    )
                except Exception:
                    logger.warning("Extra variant search failed, using early results only")
        else:
            # Standard mode: run lite rewrite first (fast) so temporal_scope is
            # known before we fire the search — this ensures date_filter is applied
            # to the initial search correctly.
            search_context = await self._prepare_search_queries(
                query,
                deep_mode=False,
                research_profile=selected_profile,
            )
            effective_query = search_context["normalized_query"] or cleaned_query
            search_queries = search_context["search_queries"] or [effective_query]
            temporal_scope = search_context.get("temporal_scope")

            # Log ambiguity detection result
            if search_context.get("is_ambiguous"):
                logger.info(
                    "Ambiguous query detected: '%s' — dimensions: %s",
                    cleaned_query,
                    search_context.get("ambiguous_dimensions"),
                )

            early_search_collection = await self._collect_search_results(
                query=effective_query,
                search_queries=search_queries,
                search_pool_size=preliminary_pool_size,
                target_count=preliminary_max,
                temporal_scope=temporal_scope,
                research_profile=selected_profile,
                deep_mode=False,
                intent_class=intent_class,
            )
            early_results = early_search_collection["results"]
            search_telemetry = dict(early_search_collection)

        search_telemetry["authority_tier_counts"] = self._tier_counts(
            early_results, top_n=min(8, len(early_results))
        )
        search_telemetry["freshness_summary"] = self._freshness_summary(early_results)
        if config.research_evidence_gate_enabled:
            evidence_gate_passed = self._passes_authority_floor(
                effective_query, early_results, intent_class
            )
            if self._is_current_scope(temporal_scope) and intent_class == "current_events":
                evidence_gate_passed = evidence_gate_passed and self._has_fresh_authoritative_results(
                    early_results
                )
            search_telemetry["evidence_gate_passed"] = evidence_gate_passed
        else:
            search_telemetry["evidence_gate_passed"] = True

        if progress_sink:
            progress_sink(self._msg("planning_strategy"))
            if len(search_queries) > 1 or effective_query != cleaned_query:
                progress_sink(self._msg("search_ready_query", query=effective_query))
                progress_sink(self._msg("search_variants", count=len(search_queries)))

        strategy = await self._plan_research_with_results(
            query=effective_query,
            max_sources=effective_max_sources,
            deep_mode=effective_deep_mode,
            search_results=early_results,
            research_profile=selected_profile,
            intent_class=intent_class,
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
                return await self._scrape_source(
                    source_config,
                    effective_query,
                    effective_deep_mode,
                )

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

        final_evidence = self._evaluate_final_evidence(
            query=effective_query,
            intent_class=intent_class,
            temporal_scope=temporal_scope,
            research_results=research_results,
        )

        if no_synthesis:
            logger.info("Skipping AI synthesis (no_synthesis flag set)")
            synthesis: dict = {
                "summary": "[AI synthesis disabled - showing raw content from sources]",
                "key_findings": [],
                "extended_analysis_hidden": True,
            }
        else:
            if (
                config.research_evidence_gate_enabled
                and not final_evidence["evidence_gate_passed"]
                and final_evidence["should_abstain"]
            ):
                logger.warning(
                    "Evidence gate failed for query '%s'; returning insufficient-evidence response.",
                    query,
                )
                synthesis = self._build_insufficient_evidence_response(
                    query=query,
                    intent_class=intent_class,
                    authority_tier_counts=final_evidence["authority_tier_counts"],
                    freshness_summary=final_evidence["freshness_summary"],
                    retrieval_attempts=int(search_telemetry.get("retrieval_attempts", 1) or 1),
                )
            else:
                logger.info("Analyzing and synthesizing findings...")
                synthesis = await self._synthesize_findings(
                    query, research_results, effective_deep_mode, temporal_scope
                )
                if (
                    config.research_evidence_gate_enabled
                    and self._fails_citation_faithfulness_gate(synthesis, intent_class)
                ):
                    logger.warning(
                        "Citation faithfulness gate failed for query '%s'; returning insufficient-evidence response.",
                        query,
                    )
                    synthesis = self._build_insufficient_evidence_response(
                        query=query,
                        intent_class=intent_class,
                        authority_tier_counts=final_evidence["authority_tier_counts"],
                        freshness_summary=final_evidence["freshness_summary"],
                        retrieval_attempts=int(search_telemetry.get("retrieval_attempts", 1) or 1),
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
            intent_class=intent_class,
            execution_mode_requested="deep" if deep_mode else "standard",
            execution_mode_effective="deep" if effective_deep_mode else "standard",
            authority_tier_counts=final_evidence["authority_tier_counts"],
            freshness_summary=final_evidence["freshness_summary"],
            retrieval_attempts=int(search_telemetry.get("retrieval_attempts", 1) or 1),
            evidence_gate_passed=bool(final_evidence["evidence_gate_passed"]),
            extended_analysis_hidden=bool(synthesis.get("extended_analysis_hidden", False)),
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
        subpage_budget = (
            (max_sources - 1)
            if max_sources and max_sources > 1
            else self._DIRECT_CRAWL_MAX_SUBPAGES
        )

        yield f"data: {_json.dumps(self._status_event(self._msg('gathering_data'), phase='scrape', code='SCRAPE_STARTED'))}\n\n"

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
        yield f"data: {_json.dumps(self._status_event(self._msg('sources_to_check', count=num_sources), phase='planning', code='SOURCES_SELECTED'))}\n\n"

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
                    ResearchResult(
                        source=sc["type"], url=url, title="", content="", error=scraped["error"]
                    )
                )
            else:
                res = scraped["result"]
                yield f"data: {_json.dumps({'type': 'source_complete', 'url': url, 'title': res.title or title, 'success': True})}\n\n"
                research_results.append(res)
                successful += 1

        total_chars = sum(len(r.content) for r in research_results if not r.error)
        yield f"data: {_json.dumps(self._status_event(self._msg('gathered_chars', chars=f'{total_chars:,}', successful=successful, total=num_sources), phase='scrape', code='SCRAPE_COMPLETED'))}\n\n"
        yield f"data: {_json.dumps(self._status_event(self._msg('synthesizing'), phase='synth', code='SYNTHESIS_STARTED'))}\n\n"

        synthesis = await self._synthesize_findings(query, research_results, deep_mode, None)
        direct_intent = self._detect_intent_class(query)
        direct_tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0, "tier5": 0}
        direct_results_meta = [
            {"url": r.url, "publication_date": r.publication_date}
            for r in research_results
            if not r.error
        ]
        for item in direct_results_meta:
            tier = classify_source_tier(str(item.get("url", "") or ""))
            key = f"tier{tier}"
            if key in direct_tier_counts:
                direct_tier_counts[key] += 1

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
            "extended_analysis_hidden": bool(synthesis.get("extended_analysis_hidden", False)),
            "metadata": {
                "intent_class": direct_intent,
                "execution_mode_requested": "deep" if deep_mode else "standard",
                "execution_mode_effective": "deep" if deep_mode else "standard",
                "authority_tier_counts": direct_tier_counts,
                "freshness_summary": self._freshness_summary(direct_results_meta),
                "retrieval_attempts": 1,
                "evidence_gate_passed": True,
            },
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
        intent_class = self._detect_intent_class(query)
        if research_profile == "auto":
            if config.research_intent_router_v2:
                selected_profile = self._profile_for_intent(intent_class)
            else:
                selected_profile = self._detect_profile_from_query(query)
        else:
            selected_profile = self._normalize_profile(research_profile)
        effective_deep_mode, effective_max_sources, _ = self._resolve_execution_mode(
            query=query,
            requested_deep_mode=deep_mode,
            requested_max_sources=max_sources,
            research_profile=selected_profile,
        )

        yield (
            f"data: {_json.dumps(self._status_event(self._msg('starting_research', query=query), phase='rewrite', code='INTENT_CLASSIFIED'))}\n\n"
        )
        yield (
            f"data: {_json.dumps(self._status_event(self._msg('preparing_queries'), phase='rewrite', code='QUERY_REWRITE_STARTED'))}\n\n"
        )

        try:
            cleaned_query = clean_query_text(query)
            search_status_events: list[dict[str, Any]] = []

            def _capture_search_status(event: dict[str, Any]) -> None:
                search_status_events.append(event)

            # ---- Direct-URL fast path ----
            direct_urls = extract_direct_urls(query)
            if direct_urls:
                async for event in self._research_stream_direct_url(
                    query=query,
                    direct_urls=direct_urls,
                    max_sources=effective_max_sources,
                    deep_mode=effective_deep_mode,
                    research_profile=selected_profile,
                ):
                    yield event
                return

            preliminary_max = self._resolve_target_source_count(
                requested_max_sources=effective_max_sources,
                ai_suggested_sources=None,
                deep_mode=effective_deep_mode,
            )
            preliminary_pool_size = min(
                max(
                    preliminary_max
                    + (
                        config.research_search_pool_extra_deep
                        if effective_deep_mode
                        else config.research_search_pool_extra_normal
                    ),
                    config.research_deep_min_sources if effective_deep_mode else preliminary_max,
                ),
                config.research_deep_max_sources,
            )

            if effective_deep_mode:
                rewrite_task = asyncio.create_task(
                    self._prepare_search_queries(
                        query,
                        deep_mode=True,
                        research_profile=selected_profile,
                    )
                )
                early_search_queries = [cleaned_query] if cleaned_query else [query]
                early_search_task = asyncio.create_task(
                    self._collect_search_results(
                        query=cleaned_query or query,
                        search_queries=early_search_queries,
                        search_pool_size=preliminary_pool_size,
                        target_count=preliminary_max,
                        temporal_scope=None,
                        research_profile=selected_profile,
                        deep_mode=effective_deep_mode,
                        intent_class=intent_class,
                        status_sink=_capture_search_status,
                    )
                )

                search_context = await rewrite_task
                effective_query = search_context["normalized_query"] or cleaned_query
                search_queries = search_context["search_queries"] or [effective_query]
                temporal_scope = search_context.get("temporal_scope")

                early_search_collection = await early_search_task
                early_results = early_search_collection["results"]
                search_telemetry = dict(early_search_collection)
                while search_status_events:
                    _event = search_status_events.pop(0)
                    yield f"data: {_json.dumps(_event)}\n\n"

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
                            deep_mode=effective_deep_mode,
                            intent_class=intent_class,
                            status_sink=_capture_search_status,
                        )
                        while search_status_events:
                            _event = search_status_events.pop(0)
                            yield f"data: {_json.dumps(_event)}\n\n"
                        seen_urls = {r.get("url") for r in early_results if r.get("url")}
                        for r in extra_collection["results"]:
                            url = r.get("url")
                            if url and url not in seen_urls:
                                early_results.append(r)
                                seen_urls.add(url)
                        search_telemetry["retrieval_attempts"] = max(
                            int(search_telemetry.get("retrieval_attempts", 1) or 1),
                            int(extra_collection.get("retrieval_attempts", 1) or 1),
                        )
                    except Exception:
                        logger.warning("Extra variant search failed, using early results only")
            else:
                # Standard mode: run lite rewrite first so temporal_scope is known
                # before initial search (ensures recency filtering applies).
                search_context = await self._prepare_search_queries(
                    query,
                    deep_mode=False,
                    research_profile=selected_profile,
                )
                effective_query = search_context["normalized_query"] or cleaned_query
                search_queries = search_context["search_queries"] or [effective_query]
                temporal_scope = search_context.get("temporal_scope")

                if search_context.get("is_ambiguous"):
                    logger.info(
                        "Ambiguous query detected (stream): '%s' — dimensions: %s",
                        cleaned_query,
                        search_context.get("ambiguous_dimensions"),
                    )

                early_search_collection = await self._collect_search_results(
                    query=effective_query,
                    search_queries=search_queries,
                    search_pool_size=preliminary_pool_size,
                    target_count=preliminary_max,
                    temporal_scope=temporal_scope,
                    research_profile=selected_profile,
                    deep_mode=False,
                    intent_class=intent_class,
                    status_sink=_capture_search_status,
                )
                early_results = early_search_collection["results"]
                search_telemetry = dict(early_search_collection)
                while search_status_events:
                    _event = search_status_events.pop(0)
                    yield f"data: {_json.dumps(_event)}\n\n"

            search_telemetry["authority_tier_counts"] = self._tier_counts(
                early_results, top_n=min(8, len(early_results))
            )
            search_telemetry["freshness_summary"] = self._freshness_summary(early_results)
            if config.research_evidence_gate_enabled:
                evidence_gate_passed = self._passes_authority_floor(
                    effective_query, early_results, intent_class
                )
                if self._is_current_scope(temporal_scope) and intent_class == "current_events":
                    evidence_gate_passed = (
                        evidence_gate_passed and self._has_fresh_authoritative_results(early_results)
                    )
                search_telemetry["evidence_gate_passed"] = evidence_gate_passed
            else:
                search_telemetry["evidence_gate_passed"] = True

            if len(search_queries) > 1 or effective_query != cleaned_query:
                yield (
                    f"data: {_json.dumps(self._status_event(self._msg('generated_variants', count=len(search_queries)), phase='rewrite', code='QUERY_VARIANTS_READY'))}\n\n"
                )

            yield (
                f"data: {_json.dumps(self._status_event(self._msg('planning_strategy'), phase='planning', code='PLANNING_STARTED'))}\n\n"
            )

            _plan_progress_queue: asyncio.Queue[str] = asyncio.Queue()
            plan_task = asyncio.create_task(
                self._plan_research_with_results(
                    query=effective_query,
                    max_sources=effective_max_sources,
                    deep_mode=effective_deep_mode,
                    search_results=early_results,
                    research_profile=selected_profile,
                    intent_class=intent_class,
                    progress_sink=_plan_progress_queue.put_nowait,
                )
            )
            while not plan_task.done():
                await asyncio.sleep(0.05)
                while not _plan_progress_queue.empty():
                    _m = _plan_progress_queue.get_nowait()
                    yield f"data: {_json.dumps(self._status_event(_m, phase='planning', code='PLANNING_PROGRESS'))}\n\n"
            while not _plan_progress_queue.empty():
                _m = _plan_progress_queue.get_nowait()
                yield f"data: {_json.dumps(self._status_event(_m, phase='planning', code='PLANNING_PROGRESS'))}\n\n"
            strategy = await plan_task

            sources_to_check = strategy["sources"]
            num_sources = len(sources_to_check)
            research_depth = strategy.get("depth", "standard")

            yield (
                f"data: {_json.dumps(self._status_event(self._msg('found_sources', count=num_sources, depth=research_depth), phase='search', code='SOURCES_SELECTED'))}\n\n"
            )
            yield (
                f"data: {_json.dumps(self._status_event(self._msg('gathering_data'), phase='scrape', code='SCRAPE_STARTED'))}\n\n"
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
                            source_config,
                            effective_query,
                            effective_deep_mode,
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
                f"data: {_json.dumps(self._status_event(self._msg('gathered_chars', chars=f'{total_chars:,}', successful=successful, total=num_sources), phase='scrape', code='SCRAPE_COMPLETED'))}\n\n"
            )

            yield (
                f"data: {_json.dumps(self._status_event(self._msg('synthesizing'), phase='synth', code='SYNTHESIS_STARTED'))}\n\n"
            )

            final_evidence = self._evaluate_final_evidence(
                query=effective_query,
                intent_class=intent_class,
                temporal_scope=temporal_scope,
                research_results=research_results,
            )

            if (
                config.research_evidence_gate_enabled
                and not final_evidence["evidence_gate_passed"]
                and final_evidence["should_abstain"]
            ):
                yield (
                    f"data: {_json.dumps(self._status_event('Insufficient authoritative evidence detected.', phase='synth', code='EVIDENCE_GATE_FAILED'))}\n\n"
                )
                synthesis = self._build_insufficient_evidence_response(
                    query=query,
                    intent_class=intent_class,
                    authority_tier_counts=final_evidence["authority_tier_counts"],
                    freshness_summary=final_evidence["freshness_summary"],
                    retrieval_attempts=int(search_telemetry.get("retrieval_attempts", 1) or 1),
                )
            else:
                synthesis = await self._synthesize_findings(
                    query, research_results, effective_deep_mode, temporal_scope
                )
                if (
                    config.research_evidence_gate_enabled
                    and self._fails_citation_faithfulness_gate(synthesis, intent_class)
                ):
                    yield (
                        f"data: {_json.dumps(self._status_event('Insufficient authoritative evidence detected.', phase='synth', code='EVIDENCE_GATE_FAILED'))}\n\n"
                    )
                    synthesis = self._build_insufficient_evidence_response(
                        query=query,
                        intent_class=intent_class,
                        authority_tier_counts=final_evidence["authority_tier_counts"],
                        freshness_summary=final_evidence["freshness_summary"],
                        retrieval_attempts=int(search_telemetry.get("retrieval_attempts", 1) or 1),
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
                "extended_analysis_hidden": bool(synthesis.get("extended_analysis_hidden", False)),
                "metadata": {
                    "intent_class": intent_class,
                    "execution_mode_requested": "deep" if deep_mode else "standard",
                    "execution_mode_effective": "deep" if effective_deep_mode else "standard",
                    "authority_tier_counts": final_evidence["authority_tier_counts"],
                    "freshness_summary": final_evidence["freshness_summary"],
                    "retrieval_attempts": int(search_telemetry.get("retrieval_attempts", 1) or 1),
                    "evidence_gate_passed": bool(final_evidence["evidence_gate_passed"]),
                },
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
