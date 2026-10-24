"""Service layer for search, scrape, map, extract, and crawl jobs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlencode, urlsplit

from web_scraper.api_runtime import InMemoryTTLCache, MetricsRegistry
from web_scraper.web_tools.archive_fetcher import build_archive_fetcher
from web_scraper.web_tools.browser import (
    BrowserPage,
    BrowserRenderError,
    BrowserUnavailableError,
    HeadlessBrowserManager,
)
from web_scraper.web_tools.budget import (
    fit_extract_payload,
    fit_scrape_batch,
    fit_search_read,
    trim_metadata,
)
from web_scraper.web_tools.content import (
    ParsedPage,
    detect_interstitial,
    extract_page,
    extract_structured,
    interstitial_message,
    needs_browser_render,
    parse_sitemap_xml,
)
from web_scraper.web_tools.crawl import CrawlManager, CrawlOptions, canonical_target
from web_scraper.web_tools.crawl_store import CrawlStore
from web_scraper.web_tools.dates import LOW_CONFIDENCE, DateResolution, infer_publication_date
from web_scraper.web_tools.discovery import (
    DEPTHS,
    MIN_RESULT_RELEVANCE,
    DiscoveryCandidate,
    SearchAngle,
    annotate_candidates,
    build_facets,
    build_search_angles,
    classify_source_type,
    rank_candidates,
    relevant_results,
    wants_news,
    within_date_range,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult, HostGate, SafeFetcher
from web_scraper.web_tools.headdates import read_result_dates
from web_scraper.web_tools.metrics import LatencyTracker, WebToolsMetrics
from web_scraper.web_tools.pagecache import CachedDocument, PageCache
from web_scraper.web_tools.paid_search import PAID_PROVIDERS, build_paid_adapters
from web_scraper.web_tools.pdf import PDF_CONTENT_TYPE, PdfError, extract_pdf
from web_scraper.web_tools.providers import ProviderGate, ProviderHealth, failure_kind
from web_scraper.web_tools.recover import (
    MIN_RECOVERED_CHARS,
    amp_candidates,
    feed_candidates,
    feed_document_html,
    find_feed_item,
    host_of,
    is_wall,
    parse_feed,
    recovery_header,
    same_site,
    same_story,
    title_from_url,
    wall_title,
)
from web_scraper.web_tools.robots import RobotsPolicy
from web_scraper.web_tools.search import (
    DuckDuckGoLiteSearchAdapter,
    DuckDuckGoSearchAdapter,
    GoogleSearchAdapter,
    SearchAdapter,
    SearchOptions,
    SearchResult,
    YahooSearchAdapter,
    normalize_domains,
    normalize_language,
    normalize_region,
    parse_iso_date,
)
from web_scraper.web_tools.settings import (
    EXTRACT_MAX_CHARS_CEILING,
    EXTRACT_MAX_CHARS_FLOOR,
    EXTRACT_MAX_ITEMS_CEILING,
    EXTRACT_OFFSET_CEILING,
    MIN_MARKDOWN_CHARS,
    SCRAPE_BATCH_MAX_TOTAL_CHARS,
    SCRAPE_BATCH_MAX_URLS,
    SCRAPE_BATCH_MIN_TOTAL_CHARS,
    SCRAPE_BATCH_MIN_URLS,
    SCRAPE_BATCH_PER_HOST,
    SCRAPE_MAX_LINKS_CEILING,
    SEARCH_READ_CHARS_DEFAULT,
    SEARCH_READ_CHARS_MAX,
    SEARCH_READ_MAX,
    WebToolsSettings,
)
from web_scraper.web_tools.sources import DEFAULT_RULES
from web_scraper.web_tools.urls import (
    UnsafeUrlError,
    canonicalize_url,
    content_hash,
    matches_search_filter,
    matches_url_filters,
    normalize_request_url,
    upgrade_to_https,
    url_in_scope,
    url_on_domains,
)


@dataclass
class LoadedDocument:
    final_url: str
    status_code: int
    content_type: str
    html: str
    page: ParsedPage
    fetched_with: str
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    pages: Optional[int] = None
    # Read from the page cache, not fetched: ``timings`` are then this call's own, and
    # ``cached_timings`` what the fetch cost when it was made.
    cache_hit: bool = False
    cached_timings: dict[str, float] = field(default_factory=dict)


@dataclass
class _Recovery:
    """A walled page read another way, and how: whose text it is, and what to do with it.

    *kind* is archive, amp, print, feed or other_source; *source_url* is where the text was read
    (for the same page's own forms the page's address stays the answer's ``url``; for another
    outlet's copy the answer's ``url`` is that outlet's).
    """

    kind: str
    document: LoadedDocument
    source_url: str
    taken: Optional[str] = None
    complete: bool = True
    title: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    alternates: list[dict[str, Any]] = field(default_factory=list)
    next_action: str = ""

    @property
    def recovered_from(self) -> dict[str, Any]:
        info: dict[str, Any] = {"kind": self.kind, "url": self.source_url}
        if self.taken:
            info["at"] = self.taken
        if self.kind == "feed":
            info["partial"] = not self.complete
        if self.kind == "other_source":
            info["domain"] = host_of(self.source_url)
            info["title"] = self.title
        return info


@dataclass
class _AngleOutcome:
    """What one search angle got: who answered, and how a fallback went."""

    provider: str
    results: list[SearchResult] = field(default_factory=list)
    error: Optional[WebToolError] = None
    fallback: Optional[dict[str, Any]] = None


class _ProviderCoolingDown(Exception):
    """A provider skipped while a better one exists: it is cooling down, or its record is worse."""

    def __init__(self, name: str, reason: str = "provider_cooldown") -> None:
        super().__init__(name)
        self.reason = reason


class WebToolsService:
    """Shared implementation used by the REST API, MCP server, and benchmark."""

    def __init__(
        self,
        *,
        settings: WebToolsSettings,
        fetcher: Any,
        browser: Optional[HeadlessBrowserManager] = None,
        search_adapters: Optional[Mapping[str, SearchAdapter]] = None,
        cache: Optional[InMemoryTTLCache] = None,
        latency: Optional[LatencyTracker] = None,
        metrics_registry: Optional[MetricsRegistry] = None,
        page_cache: Optional[PageCache] = None,
        archive_fetcher: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher
        # What reads web.archive.org (the CDX list and the replays); without one, the fetcher
        # does, as a service made by hand with a fake always did.
        self._archive = archive_fetcher
        self.browser = browser
        self.search_adapters = dict(search_adapters or {})
        self.cache = cache or InMemoryTTLCache(
            ttl_seconds=settings.cache_ttl_seconds,
            max_entries=settings.cache_max_entries,
        )
        self._pages = page_cache or PageCache(
            ttl_seconds=settings.page_cache_ttl_seconds,
            max_entries=settings.page_cache_max_entries,
            max_bytes=settings.page_cache_max_bytes,
        )
        self.metrics = WebToolsMetrics(
            latency or LatencyTracker(),
            metrics_registry,
        )
        self._crawl: Optional[CrawlManager] = None
        self._provider_gates: dict[str, ProviderGate] = {}
        self._provider_health: dict[str, ProviderHealth] = {}
        self._content_urls: OrderedDict[str, str] = OrderedDict()
        self._source_rules = DEFAULT_RULES.extended(
            settings.official_hosts, settings.aggregator_hosts
        )

    @classmethod
    def from_env(
        cls,
        *,
        metrics_registry: Optional[MetricsRegistry] = None,
        cache: Optional[InMemoryTTLCache] = None,
        latency: Optional[LatencyTracker] = None,
    ) -> WebToolsService:
        settings = WebToolsSettings.from_env()
        fetcher = SafeFetcher(
            timeout_seconds=settings.fetch_timeout_seconds,
            max_response_bytes=settings.max_response_bytes,
            max_redirects=settings.max_redirects,
            retry_attempts=settings.fetch_retries,
            max_concurrency=settings.global_concurrency,
            host_gate=HostGate(
                max_per_host=settings.host_concurrency,
                min_interval_ms=settings.host_min_interval_ms,
            ),
        )
        browser = (
            HeadlessBrowserManager(
                preference=settings.browser_preference,
                timeout_seconds=settings.browser_timeout_seconds,
                idle_seconds=settings.browser_idle_seconds,
                post_load_wait_ms=settings.browser_post_load_wait_ms,
                max_contexts=settings.browser_max_contexts,
            )
            if settings.browser_enabled
            else None
        )
        adapters: dict[str, SearchAdapter] = {
            "duckduckgo": DuckDuckGoSearchAdapter(
                region=settings.search_region,
                language=settings.search_language,
            ),
            "duckduckgo_lite": DuckDuckGoLiteSearchAdapter(
                region=settings.search_region,
                language=settings.search_language,
            ),
            "yahoo": YahooSearchAdapter(language=settings.search_language),
            "google": GoogleSearchAdapter(),
        }
        # Brave and Serper exist only when their key is in the environment.
        adapters.update(build_paid_adapters(timeout=settings.search_timeout_seconds))
        return cls(
            settings=settings,
            fetcher=fetcher,
            browser=browser,
            search_adapters=adapters,
            cache=cache,
            latency=latency,
            metrics_registry=metrics_registry,
            archive_fetcher=build_archive_fetcher(settings),
        )

    async def close(self) -> None:
        if self._crawl is not None:
            await self._crawl.close()
        for adapter in self.search_adapters.values():
            close_adapter = getattr(adapter, "aclose", None)
            if close_adapter is not None:
                await close_adapter()
        for reader in (self.fetcher, self._archive):
            close_reader = getattr(reader, "aclose", None)
            if close_reader is not None:
                await close_reader()
        if self.browser is not None:
            await self.browser.stop()

    async def search(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        provider: Optional[str] = None,
        depth: Optional[str] = None,
        region: Optional[str] = None,
        language: Optional[str] = None,
        sites: Optional[Sequence[str]] = None,
        exclude_sites: Optional[Sequence[str]] = None,
        date_from: Optional[date | str] = None,
        date_to: Optional[date | str] = None,
        read: int = 0,
        read_chars: Optional[int] = None,
        archive: bool = False,
        resolve_dates: Optional[bool] = None,
        strict_dates: bool = False,
        compact: bool = False,
        debug: bool = False,
        primary_first: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Discover URLs for a query: ranked results, and what it took to get them.

        ``primary_first`` makes an organization's own site and the news outlets that report it
        lead (unset: it does for a query that asks for news); a known aggregator ranks lower
        whatever the query.

        ``compact`` shapes the answer for a model that pays for every character (see
        ``_present_search``); the search cache keeps the whole answer, so a compact call and a
        full one share it. ``debug`` adds the facets, the queries, the timings and the score
        parts to a compact answer; a full answer (``compact`` false) holds them anyway.
        """
        cleaned = (query or "").strip()
        if not cleaned:
            raise WebToolError("invalid_request", "Query must not be empty")
        if len(cleaned) > self.settings.max_query_length:
            raise WebToolError(
                "invalid_request",
                f"Query exceeds {self.settings.max_query_length} characters",
            )

        resolved_depth = (depth or self.settings.search_default_depth).strip().lower()
        if resolved_depth not in DEPTHS:
            raise WebToolError(
                "invalid_request",
                f"Unknown search depth: {resolved_depth}",
                detail="Use 'fast', 'discover' or 'deep'",
            )

        provider_name = self._primary_provider(provider)
        if provider_name not in self.search_adapters:
            keyed = PAID_PROVIDERS.get(provider_name)
            if keyed is not None:
                raise WebToolError(
                    "invalid_request",
                    f"Search provider {provider_name} is off: it needs {keyed.env} in the environment",
                    detail="no_key",
                )
            raise WebToolError("invalid_request", f"Unknown search provider: {provider_name}")
        resolved_limit = min(
            max(limit or self.settings.default_search_limit, 1),
            self.settings.max_search_limit,
        )
        angle_limit = (
            resolved_limit
            if resolved_depth == "fast"
            else min(max(resolved_limit * 2, 8), self.settings.max_search_limit)
        )
        options, site_list, excluded_list = self._search_parameters(
            region, language, sites, exclude_sites, date_from, date_to
        )
        reading = self._read_plan(read, read_chars)
        operators = _site_operators(site_list, excluded_list)
        if len(operators) > self.settings.max_query_length // 2:
            raise WebToolError(
                "invalid_request",
                "sites and exclude_sites are too long together",
                detail=f"{len(operators)} characters of operators",
            )

        reading_dates, date_warnings = self._dates_plan(
            cleaned, resolve_dates, strict_dates, options
        )
        cache_key = (
            f"search:{provider_name}:{resolved_depth}:{resolved_limit}:"
            f"{self.settings.search_region}:{self.settings.search_language}:"
            f"{_parameters_key(options, site_list, excluded_list)}:"
            f"dates{int(reading_dates)}{int(strict_dates)}:"
            f"first{'a' if primary_first is None else int(primary_first)}:"
            f"{cleaned.casefold()}"
        )
        started = time.perf_counter()
        cached = await self.cache.get(cache_key)
        if cached is not None:
            self.metrics.count("cache_hits_total", operation="search")
            answer = _from_cache(cached, started)
            await self._read_results(answer, reading, archive)
            return _present_search(answer, compact=compact, debug=debug)

        # Every angle carries the operators and the parameters; the operators take room
        # from the query, never from the limit the provider has.
        angles = build_search_angles(
            cleaned,
            resolved_depth,
            max_length=self.settings.max_query_length - (len(operators) + 1 if operators else 0),
            explicit_range=bool(options.date_from or options.date_to),
        )
        if operators:
            angles = [replace(angle, query=f"{angle.query} {operators}") for angle in angles]
        candidates: dict[str, DiscoveryCandidate] = {}
        query_reports: list[dict[str, Any]] = []
        angle_timings: dict[str, float] = {}
        warnings: list[str] = []
        first_error: Optional[WebToolError] = None
        answered_by: list[str] = []
        responded: list[str] = []
        fallback_answered = False

        # The base goes first: a refusal it meets starts the provider's cooldown
        # before the other angles ask. Those then run together, each through the
        # provider's gate (search_provider_concurrency calls at a time, with its
        # jitter), within the angle budget, and are merged in angle order
        # whichever answers first.
        fast = resolved_depth == "fast"
        answered = [
            await self._timed_angle(
                provider_name, angles[0], angle_limit, fast=fast, options=options
            )
        ]
        answered += await self._angles_after_base(
            provider_name, angles[1:], angle_limit, options=options
        )

        for angle, (outcome, angle_ms) in zip(angles, answered):
            angle_timings[angle.name] = round(angle_ms, 2)
            self.metrics.observe(
                "search_angle",
                angle_ms,
                provider=outcome.provider,
                angle=angle.name,
            )
            report: dict[str, Any] = {
                "angle": angle.name,
                "query": angle.query,
                "date_filter": angle.date_filter,
                "provider": outcome.provider,
                "results": len(outcome.results),
                "new": 0,
                "error": outcome.error.to_dict() if outcome.error is not None else None,
            }
            if outcome.fallback is not None:
                report["fallback"] = outcome.fallback
            query_reports.append(report)

            if outcome.error is not None:
                if first_error is None:
                    first_error = outcome.error
                if angle.name != "base":
                    # An angle the search's own time budget cut is no refusal of the provider.
                    cut = outcome.error.detail == "angle_budget"
                    warnings.append(f"{angle.name}_query_{'cut' if cut else 'failed'}")
                continue

            if outcome.provider not in responded:
                responded.append(outcome.provider)
            if outcome.results and outcome.provider not in answered_by:
                answered_by.append(outcome.provider)
            if outcome.provider != provider_name:
                fallback_answered = True
            for rank, item in enumerate(outcome.results):
                try:
                    target = canonicalize_url(item.url)
                except UnsafeUrlError:
                    continue
                existing = candidates.get(target)
                if existing is None:
                    candidates[target] = DiscoveryCandidate(
                        result=SearchResult(
                            title=item.title,
                            url=target,
                            snippet=item.snippet,
                            source=item.source,
                            published=item.published,
                        ),
                        angle_ranks={angle.name: rank},
                    )
                    report["new"] += 1
                else:
                    existing.angle_ranks.setdefault(angle.name, rank)
                    if len(item.snippet) > len(existing.result.snippet):
                        existing.result = SearchResult(
                            title=item.title or existing.result.title,
                            url=target,
                            snippet=item.snippet,
                            source=item.source or existing.result.source,
                            published=item.published or existing.result.published,
                        )

        if not candidates and first_error is not None:
            raise first_error

        if site_list or excluded_list or options.date_from or options.date_to:
            candidates = {
                target: candidate
                for target, candidate in candidates.items()
                if _passes_filters(candidate, site_list, excluded_list, options)
            }

        if any(
            report["angle"] == "base" and report["error"] is not None for report in query_reports
        ):
            warnings.append("base_query_failed")
        if fallback_answered:
            warnings.append("provider_fallback")

        search_ms = (time.perf_counter() - started) * 1000

        def rank_pool(
            size: int, found: Optional[Mapping[str, DateResolution]] = None
        ) -> list[dict[str, Any]]:
            if resolved_depth == "fast":
                # Provider order, minus results that hold no distinctive word of the query. The
                # whole pool is scored, so BM25 sees the same corpus as in discover and deep;
                # annotate_candidates keeps the provider's order.
                annotated = annotate_candidates(
                    cleaned,
                    candidates.values(),
                    dates=found,
                    rules=self._source_rules,
                    primary_first=primary_first,
                )
                return relevant_results(cleaned, annotated)[:size]
            return rank_candidates(
                cleaned,
                candidates.values(),
                size,
                dates=found,
                rules=self._source_rules,
                primary_first=primary_first,
            )

        # A strict search drops the results that cannot be dated, so it ranks a longer list and
        # dates more of it, to have the limit left after the drop.
        pool = (
            min(resolved_limit * 2, self.settings.search_resolve_dates_max)
            if strict_dates
            else resolved_limit
        )
        pool = max(pool, resolved_limit)
        ranking_started = time.perf_counter()
        results = rank_pool(pool)
        ranking_ms = (time.perf_counter() - ranking_started) * 1000
        dates_report: Optional[dict[str, Any]] = None
        dates_ms = 0.0
        if reading_dates and results:
            dates_started = time.perf_counter()
            reading_top = max(
                self.settings.search_resolve_dates_top,
                min(pool, self.settings.search_resolve_dates_max) if strict_dates else 0,
            )
            found, reading_report = await read_result_dates(
                results,
                fetcher=self.fetcher,
                cached_html=self._cached_html,
                top=reading_top,
                budget_seconds=self.settings.search_resolve_dates_budget_ms / 1000,
                max_bytes=self.settings.search_resolve_dates_bytes,
                head_cap=self.settings.search_resolve_dates_head_cap,
                concurrency=self.settings.search_resolve_dates_concurrency,
                snippets={
                    target: (
                        candidate.result.title,
                        candidate.result.snippet,
                        candidate.result.published,
                    )
                    for target, candidate in candidates.items()
                },
            )
            if found:
                # The dates move freshness, so the order may move: ranked again with them.
                results = rank_pool(pool, found)
                if options.date_from or options.date_to:
                    results = [
                        row
                        for row in results
                        if within_date_range(row["date"], options.date_from, options.date_to)
                    ]
            if reading_report.cut:
                date_warnings.append(f"resolve_dates_cut:{reading_report.cut}")
            dates_report = reading_report.to_dict()
            dates_ms = (time.perf_counter() - dates_started) * 1000
        dropped = 0
        if strict_dates:
            kept: list[dict[str, Any]] = []
            for row in results:
                if row["date"] and row["date_confidence"] >= LOW_CONFIDENCE:
                    kept.append(row)
                    if len(kept) == resolved_limit:
                        break
                else:
                    dropped += 1
            if dropped and results:
                date_warnings.append(f"strict_dates_dropped:{dropped}")
            results = kept
        else:
            results = results[:resolved_limit]
        if dates_report is not None:
            dates_report["strict"] = strict_dates
            dates_report["dropped"] = dropped
        if not results and not dropped:
            # The provider answered, but nothing it returned is about the query.
            warnings.append("no_relevant_results")
        elif results and all(row["relevance"] < MIN_RESULT_RELEVANCE for row in results):
            warnings.append("low_relevance_results")
        warnings.extend(date_warnings)
        facets = build_facets(
            results,
            total_candidates=len(candidates),
            limit=resolved_limit,
        )

        total_ms = (time.perf_counter() - started) * 1000
        self.metrics.observe(
            "search",
            search_ms,
            provider=provider_name,
            depth=resolved_depth,
        )
        self.metrics.observe("ranking", ranking_ms, operation="search")
        if dates_report is not None:
            self.metrics.observe("search_dates", dates_ms)
        self.metrics.observe("total", total_ms, operation="search")
        self.metrics.count(
            "searches_total",
            provider=provider_name,
            depth=resolved_depth,
        )
        timings = {
            "search_ms": round(search_ms, 2),
            "ranking_ms": round(ranking_ms, 2),
            "total_ms": round(total_ms, 2),
        }
        if dates_report is not None:
            timings["dates_ms"] = round(dates_ms, 2)
        for name, value in angle_timings.items():
            timings[f"angle_{name}_ms"] = value
        payload = {
            "query": cleaned,
            # Who answered: the fallback's name when it stood in for the
            # requested provider, both joined with "+" when they shared angles.
            "provider": "+".join(answered_by or responded) or provider_name,
            "requested_provider": provider_name,
            "depth": resolved_depth,
            "results": results,
            "facets": facets,
            "queries": query_reports,
            "warnings": warnings,
            "timings": timings,
            "cache_hit": False,
        }
        if dates_report is not None:
            payload["dates"] = dates_report
        # The search is cached as it is, without the reading: what is read is the pages' own
        # cache entries, and a search that reads no page never gets one's content.
        await self.cache.set(
            cache_key,
            _for_cache(payload),
            ttl_seconds=self.settings.search_cache_ttl_seconds,
        )
        await self._read_results(payload, reading, archive)
        return _present_search(payload, compact=compact, debug=debug)

    def _dates_plan(
        self,
        query: str,
        resolve_dates: Optional[bool],
        strict_dates: bool,
        options: SearchOptions,
    ) -> tuple[bool, list[str]]:
        """Whether this search reads the head of its top results' pages for their dates.

        Unless a call says, it does when the query asks for news or the call gives a date range
        (and when ``strict_dates`` needs dates to drop by); ``search_resolve_dates`` off turns it
        off for every call, and a call that asked is told so.
        """
        mode = self.settings.search_resolve_dates
        if mode == "off":
            return False, (["resolve_dates_disabled"] if resolve_dates else [])
        if resolve_dates is not None:
            return resolve_dates, []
        wanted = (
            mode == "on"
            or strict_dates
            or bool(options.date_from or options.date_to)
            or wants_news(query)
        )
        return wanted, []

    def _cached_html(self, url: str) -> Optional[str]:
        """The HTML of a page the page cache holds, if it does: reading it costs no request."""
        key = self._page_key(url)
        stored = self._pages.get(key) if key is not None else None
        return stored.html if stored is not None and stored.html else None

    @staticmethod
    def _read_plan(read: int, read_chars: Optional[int]) -> tuple[int, int]:
        """How many results a search reads, and how many characters of each."""
        if isinstance(read, bool) or not 0 <= int(read) <= SEARCH_READ_MAX:
            raise WebToolError(
                "invalid_request", f"read must be 0 to {SEARCH_READ_MAX}", detail=f"got {read}"
            )
        if read_chars is None:
            return int(read), SEARCH_READ_CHARS_DEFAULT
        if not MIN_MARKDOWN_CHARS <= int(read_chars) <= SEARCH_READ_CHARS_MAX:
            raise WebToolError(
                "invalid_request",
                f"read_chars must be {MIN_MARKDOWN_CHARS} to {SEARCH_READ_CHARS_MAX}",
                detail=f"got {read_chars}",
            )
        return int(read), int(read_chars)

    async def _read_results(
        self, answer: dict[str, Any], reading: tuple[int, int], archive: bool
    ) -> None:
        """Read the top results of a search answer, in place: ``content`` or ``content_error``.

        The pages are read together, as a batch scrape reads them, and each result is
        replaced by a new one, so the answer the search cache holds stays without content.
        The whole answer is held to ``search_read_max_total_chars``. A walled page is read
        from the Internet Archive only when ``archive`` was asked for. ``read_ms`` says what
        reading took.
        """
        count, chars = reading
        top = answer["results"][:count]
        if not top:
            return
        started = time.perf_counter()
        # A search that reads its top pages is quick: a walled one is read from the archive when
        # asked, not hunted for in other forms and other outlets (web_scrape does that).
        pages = await self._read_pages(
            [item["url"] for item in top],
            max_chars=chars,
            archive=archive,
            links=False,
            max_links=None,
            recover=False,
        )
        read: list[dict[str, Any]] = []
        for item, page in zip(top, pages):
            if "error" in page:
                read.append({**item, "content_error": page["error"]})
            else:
                text = page["markdown"]
                read.append({**item, "content": text, "content_truncated": len(text) >= chars})
        read_ms = (time.perf_counter() - started) * 1000
        timings = dict(answer["timings"])
        timings["read_ms"] = round(read_ms, 2)
        timings["total_ms"] = round(timings.get("total_ms", 0.0) + read_ms, 2)
        answer["results"] = read + answer["results"][count:]
        answer["timings"] = timings
        fit_search_read(answer, self.settings.search_read_max_total_chars)
        self.metrics.observe("search_read", read_ms)

    async def _angles_after_base(
        self,
        provider: str,
        angles: list[SearchAngle],
        limit: int,
        *,
        options: Optional[SearchOptions] = None,
    ) -> list[tuple[_AngleOutcome, float]]:
        """The angles after the base, together, each cut once its time budget ran out.

        A refused angle can wait out the provider's cooldown or start the headless
        Google fallback (seconds to tens of seconds), so the base's answer is not
        held for it. The side angles (current, background) are cut at
        ``search_angle_budget_ms``; the primary-source angle of a deep search, the one worth
        waiting for, is asked first (it queues first at the provider's gate) and goes on to
        ``search_primary_budget_ms`` while the side angles are already cut. A cut angle is
        reported as a ``timeout`` with the detail ``angle_budget``; being the search's own
        limit, it marks no provider as failed. The answers are returned in the angles' order.
        """
        if not angles:
            return []
        delay = self.settings.search_angle_delay_ms / 1000
        budget = self.settings.search_angle_budget_ms / 1000
        primary_budget = max(self.settings.search_primary_budget_ms / 1000, budget)
        # The primary angle is scheduled first: it is the last angle in the order of the answer
        # and would otherwise queue behind the side angles at a gate of one call at a time.
        schedule = sorted(range(len(angles)), key=lambda index: angles[index].name != "primary")
        tasks: list[Optional[asyncio.Task[tuple[_AngleOutcome, float]]]] = [None] * len(angles)
        for position, index in enumerate(schedule, start=1):
            tasks[index] = asyncio.create_task(
                self._timed_angle(
                    provider, angles[index], limit, stagger=delay * position, options=options
                )
            )
        running = [task for task in tasks if task is not None]  # in the angles' order
        primaries = {tasks[index] for index, angle in enumerate(angles) if angle.name == "primary"}
        pending: set[asyncio.Task[tuple[_AngleOutcome, float]]] = set()
        try:
            if budget:
                _, pending = await asyncio.wait(running, timeout=budget)
                for task in pending - primaries:
                    task.cancel()
                waiting = pending & primaries
                if waiting:
                    _, waiting = await asyncio.wait(waiting, timeout=primary_budget - budget)
                pending = (pending - primaries) | waiting
            else:
                await asyncio.wait(running)
        finally:
            for task in running:
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

        answered: list[tuple[_AngleOutcome, float]] = []
        for task in running:
            allowed = primary_budget if task in primaries else budget
            if task in pending:
                cut = WebToolError(
                    "timeout",
                    f"The angle ran past the search's time budget ({allowed:g} s)",
                    detail="angle_budget",
                )
                answered.append((_AngleOutcome(provider, error=cut), allowed * 1000))
            elif task.exception() is not None:
                failed = WebToolError(
                    "search_failed",
                    "Search angle failed",
                    detail=type(task.exception()).__name__,
                )
                answered.append((_AngleOutcome(provider, error=failed), 0.0))
            else:
                answered.append(task.result())
        return answered

    async def _timed_angle(
        self,
        provider: str,
        angle: SearchAngle,
        limit: int,
        *,
        stagger: float = 0.0,
        fast: bool = False,
        options: Optional[SearchOptions] = None,
    ) -> tuple[_AngleOutcome, float]:
        """One angle's outcome and its milliseconds, queueing for the gate included."""
        if stagger > 0:
            await asyncio.sleep(stagger)
        started = time.perf_counter()
        outcome = await self._search_angle(
            provider, angle.query, limit, angle.date_filter, fast=fast, options=options
        )
        return outcome, (time.perf_counter() - started) * 1000

    async def _search_angle(
        self,
        primary: str,
        query: str,
        limit: int,
        date_filter: Optional[str] = None,
        *,
        fast: bool = False,
        options: Optional[SearchOptions] = None,
    ) -> _AngleOutcome:
        """Ask the primary provider; on any failure, walk the fallback chain.

        The chain is ``search_fallback_providers`` (the primary and a name without an
        adapter left out): each fallback is asked in turn, through its own gate and with
        its own cooldown, until one answers. A provider that is cooling down after a
        recent failure is skipped without a call, the primary too when a fallback is
        healthy. If every fallback fails, the primary is still asked when it was skipped:
        a cooldown only steers calls, it never fails a search. The angle's ``fallback``
        record says who was asked, in order, and why each was left.
        """
        chain = self._fallback_chain(primary, fast=fast)
        if not chain:
            try:
                results = await self._call_provider(
                    primary, query, limit, date_filter, options=options
                )
            except WebToolError as exc:
                return _AngleOutcome(primary, error=exc)
            return _AngleOutcome(primary, results)

        tried: list[dict[str, Any]] = []
        primary_error: Optional[WebToolError] = None
        try:
            results = await self._call_provider(primary, query, limit, date_filter, yield_to=chain)
        except _ProviderCoolingDown as skipped:
            reason = skipped.reason
            tried.append({"provider": primary, "status": "skipped", "reason": reason})
        except WebToolError as exc:
            if all(self._provider_health_for(name).cooling_down() for name in chain):
                return _AngleOutcome(primary, error=exc)
            primary_error, reason = exc, exc.detail or exc.code
            tried.append({"provider": primary, "status": "failed", "reason": reason})
        else:
            return _AngleOutcome(primary, results)

        record: dict[str, Any] = {
            "from": primary,
            "to": chain[0],
            "reason": reason,
            "error": None,
            "tried": tried,
        }
        for name in chain:
            if self._provider_health_for(name).cooling_down():
                tried.append({"provider": name, "status": "skipped", "reason": "provider_cooldown"})
                continue
            record["to"] = name
            self.metrics.count("search_fallbacks_total", provider=name)
            try:
                # A cold headless browser (Google) can take longer than a plain HTTP search.
                results = await self._call_provider(
                    name,
                    query,
                    limit,
                    date_filter,
                    options=options,
                    timeout=self.settings.search_fallback_timeout_seconds,
                )
            except WebToolError as exc:
                record["error"] = exc.to_dict()
                tried.append(
                    {"provider": name, "status": "failed", "reason": exc.detail or exc.code}
                )
                continue
            record["error"] = None
            tried.append({"provider": name, "status": "answered", "reason": None})
            return _AngleOutcome(name, results, fallback=record)

        if primary_error is not None:
            return _AngleOutcome(primary, error=primary_error, fallback=record)
        try:
            results = await self._call_provider(primary, query, limit, date_filter, options=options)
        except WebToolError as last:
            tried.append(
                {"provider": primary, "status": "failed", "reason": last.detail or last.code}
            )
            return _AngleOutcome(primary, error=last, fallback=record)
        tried.append({"provider": primary, "status": "answered", "reason": None})
        return _AngleOutcome(primary, results, fallback=record)

    async def _call_provider(
        self,
        name: str,
        query: str,
        limit: int,
        date_filter: Optional[str] = None,
        *,
        options: Optional[SearchOptions] = None,
        yield_to: Sequence[str] = (),
        timeout: Optional[float] = None,
    ) -> list[SearchResult]:
        """One paced provider call; failures update only this provider's health.

        With ``yield_to`` set, a provider that is cooling down raises
        ``_ProviderCoolingDown`` instead of being called, as long as one of the
        providers it would yield to is not cooling down itself.
        """
        adapter = self.search_adapters[name]
        health = self._provider_health_for(name)
        gate = self._provider_gate_for(name)
        window = self.settings.search_provider_window_seconds

        def should_yield() -> bool:
            """Whether a provider in the list is in better standing than this one: not cooling
            when this one is, or answering more of its calls."""
            return bool(yield_to) and health.standing(window) > min(
                self._provider_health_for(other).standing(window) for other in yield_to
            )

        def gave_way() -> _ProviderCoolingDown:
            return _ProviderCoolingDown(
                name, "provider_cooldown" if health.cooling_down() else "provider_unhealthy"
            )

        if should_yield():
            raise gave_way()
        async with gate.turn():
            # A call queued behind a failure re-checks before spending its turn.
            if should_yield():
                raise gave_way()
            await gate.pace()
            # An adapter is given the date filter and the options only when a caller asked for
            # them: with none, the call is what it always was, so an adapter written as
            # ``search(query, limit)`` keeps working.
            extra: dict[str, Any] = {}
            if date_filter is not None:
                extra["date_filter"] = date_filter
            if options:
                extra["options"] = options
            call_started = time.perf_counter()
            try:
                results = await asyncio.wait_for(
                    adapter.search(query, limit, **extra),
                    timeout=timeout or self.settings.search_timeout_seconds,
                )
            except asyncio.TimeoutError:
                failure = WebToolError(
                    "timeout",
                    f"Search provider {name} timed out",
                    detail="search_timeout",
                )
            except WebToolError as exc:
                failure = exc
            except Exception as exc:  # noqa: BLE001 - provider boundary
                # Whatever a provider raises (Yahoo's RuntimeError for an HTTP error status, a
                # parser given a page it did not expect) is its failure, never the search's.
                failure = WebToolError(
                    "search_failed",
                    "Search provider failed",
                    detail=type(exc).__name__,
                )
            else:
                health.record_success((time.perf_counter() - call_started) * 1000)
                return results
            finally:
                gate.finish()

        kind = failure_kind(failure)
        cooldown = health.record_failure(
            self.settings.search_provider_cooldown_seconds,
            self.settings.search_provider_max_cooldown_seconds,
            kind=kind,
            limit=self.settings.search_provider_failure_limit,
            error=failure,
            latency_ms=(time.perf_counter() - call_started) * 1000,
        )
        self.metrics.count("search_provider_failures_total", provider=name, kind=kind)
        if cooldown:
            self.metrics.count("search_provider_cooldowns_total", provider=name, kind=kind)
        raise failure

    @staticmethod
    def _search_parameters(
        region: Optional[str],
        language: Optional[str],
        sites: Optional[Sequence[str]],
        exclude_sites: Optional[Sequence[str]],
        date_from: Optional[date | str],
        date_to: Optional[date | str],
    ) -> tuple[SearchOptions, list[str], list[str]]:
        """The caller's search parameters, checked as the request model checks them."""
        try:
            options = SearchOptions(
                region=normalize_region(region) if region else None,
                language=normalize_language(language) if language else None,
                date_from=parse_iso_date(date_from, "date_from") if date_from else None,
                date_to=parse_iso_date(date_to, "date_to") if date_to else None,
            )
            site_list = normalize_domains(sites, "sites")
            excluded_list = normalize_domains(exclude_sites, "exclude_sites")
        except ValueError as exc:
            raise WebToolError("invalid_request", str(exc)) from exc
        if options.date_from and options.date_to and options.date_from > options.date_to:
            raise WebToolError("invalid_request", "date_from must not be after date_to")
        return options, site_list, excluded_list

    def _fallback_chain(self, primary: str, *, fast: bool = False) -> list[str]:
        """The providers a failed primary falls to, in the order they are asked.

        Those of ``search_fallback_providers`` that have an adapter, the primary left
        out. A quick search (``fast``) asks the plain-HTTP providers before a headless
        browser, whatever the configured order: it waits for neither a cooldown nor a
        cold browser while an HTTP provider can answer.
        """
        names = [
            name
            for name in self._route_after(primary)
            if name != primary and name in self.search_adapters
        ]
        window = self.settings.search_provider_window_seconds
        healths = {name: self._provider_health_for(name) for name in names}

        def key(name: str) -> tuple[int, bool]:
            headless = fast and bool(getattr(self.search_adapters[name], "headless", False))
            return healths[name].standing(window), headless

        # Healthier providers first, by how their recent calls went; ties keep the configured
        # order (the sort is stable). A cooling provider keeps its place: it is skipped there
        # without a call, and the answer records it.
        slots = [index for index, name in enumerate(names) if not healths[name].cooling_down()]
        movers = sorted((names[index] for index in slots), key=key)
        for index, name in zip(slots, movers):
            names[index] = name
        return names

    def _route_after(self, primary: str) -> list[str]:
        """The providers a search falls to when *primary* fails, before their health orders them.

        The configured fallback chain; and the providers with a key, which a call that names its
        own provider reaches last and an unnamed call reaches first (see ``_primary_provider``);
        DuckDuckGo stays a fallback of a keyed provider.
        """
        paid = [name for name in PAID_PROVIDERS if name in self.search_adapters]
        configured = self.settings.search_fallback_providers
        if primary in paid:
            rest = [*paid, self.settings.search_provider, *configured]
        else:
            rest = [*configured, *paid]
        return list(dict.fromkeys(rest))

    def _primary_provider(self, requested: Optional[str]) -> str:
        """The provider a search asks first: the one a call names, else the first with a key, else the setting.

        A provider with a key (Brave, Serper) is used first when the call names none; none of them
        is on without its key.
        """
        name = (requested or "").strip().lower()
        if name:
            return name
        for paid in PAID_PROVIDERS:
            if paid in self.search_adapters:
                return paid
        return self.settings.search_provider

    def health(self) -> dict[str, Any]:
        """Each search provider's state in this process, for ``web_health``.

        Cheap: it reads the record the calls left (no request is made). A provider is ``ok``,
        ``degraded`` (it answered under 4 of 5 of its recent calls), ``cooling`` (with the time
        left) or ``no_key`` (Brave and Serper without their key, with the variable they read; the
        key itself is never in an answer). Each has its calls in this process, the share that met
        a wall (``block_rate``), the mean latency of its calls and its last error. ``order`` is the
        order the next search would ask them in.
        """
        names = list(self.search_adapters)
        names += [name for name in PAID_PROVIDERS if name not in names]
        providers: list[dict[str, Any]] = []
        for name in names:
            if name not in self.search_adapters:
                providers.append(
                    {"name": name, "state": "no_key", "key_env": PAID_PROVIDERS[name].env}
                )
                continue
            known = self._provider_health.get(name)
            providers.append({"name": name, **(known or ProviderHealth()).report()})
        return {
            "providers": providers,
            "order": self._ask_order(),
            "calls": sum(entry.get("calls", 0) for entry in providers),
            "cooldown_seconds": self.settings.search_provider_cooldown_seconds,
            "failures_before_cooldown": self.settings.search_provider_failure_limit,
        }

    def _ask_order(self) -> list[str]:
        """The providers in the order the next unnamed search would ask them: the cooling ones last."""
        primary = self._primary_provider(None)
        if primary not in self.search_adapters:
            return []
        route = [primary, *self._fallback_chain(primary)]
        window = self.settings.search_provider_window_seconds
        healths = {name: self._provider_health_for(name) for name in route}
        active = sorted(
            (name for name in route if not healths[name].cooling_down()),
            key=lambda name: healths[name].standing(window),
        )
        return active + [name for name in route if healths[name].cooling_down()]

    def _provider_gate_for(self, name: str) -> ProviderGate:
        gate = self._provider_gates.get(name)
        if gate is None:
            # A provider that states a rate (Brave's free plan: one call a second) is never paced
            # faster than it.
            floor = int(getattr(self.search_adapters.get(name), "min_gap_ms", 0) or 0)
            gate = ProviderGate(
                self.settings.search_provider_concurrency,
                max(self.settings.search_jitter_min_ms, floor),
                max(self.settings.search_jitter_max_ms, floor),
            )
            self._provider_gates[name] = gate
        return gate

    def _provider_health_for(self, name: str) -> ProviderHealth:
        return self._provider_health.setdefault(name, ProviderHealth())

    async def scrape(
        self,
        url: str,
        *,
        max_chars: Optional[int] = None,
        archive: bool = False,
        links: bool = True,
        max_links: Optional[int] = None,
        recover: Optional[bool] = None,
        lean: bool = False,
    ) -> dict[str, Any]:
        """Read one page. A page that answers a wall may be read another way (see ``_recover``).

        ``lean`` returns the text without the metadata blocks (see ``_lean_view``). A page whose
        Markdown was cut says so (``truncated``, ``total_chars``, ``next_offset``) and, when the
        page cache keeps it, gives a ``content_id`` that ``scrape_content`` reads the rest with.

        ``recover`` unset reads a walled page every way there is, unless ``archive`` alone was
        asked for (then only the archive, as it has always been); ``recover: true`` is every way
        whatever else is asked, ``recover: false`` none but the archive when ``archive`` is set.
        The text of a page read another way begins with a line that says whose it is, and the
        answer's ``recovered_from`` says how; a page that could not be read any way raises the
        wall's error with ``alternates`` and ``next_action``.
        """
        requested = self._canonical(url)
        fetch_target = self._request_url(url)
        limit = self._markdown_limit(max_chars)
        link_cap = self._links_limit(max_links)
        recovering = self._recovering(recover, archive)
        cache_key = (
            f"scrape:{requested}:{limit}"
            + (":archive" if archive else "")
            + (":recover" if recovering else "")
            + (f":links{link_cap}" if links else ":nolinks")
        )

        started = time.perf_counter()
        cached = await self.cache.get(cache_key)
        # A cut page's content_id lives as long as the page cache keeps the page (5 minutes), the
        # answer cache a little longer: an answer whose id has gone is read again, not repeated.
        if cached is not None and not self._content_gone(cached):
            self.metrics.count("cache_hits_total", operation="scrape")
            answer = _from_cache(cached, started)
            return _lean_view(answer) if lean else answer

        recovery: Optional[_Recovery] = None
        try:
            document = await self._load_page(fetch_target, max_chars=limit)
        except WebToolError as exc:
            if exc.code not in _ARCHIVABLE:
                raise
            walled = is_wall(exc)
            if not archive and not (recovering and walled):
                if walled and self.settings.scrape_recover == "off" and recover is not False:
                    exc.hint = _RECOVER_SETTING_HINT
                elif walled and recover is False:
                    exc.hint = _RECOVER_OFF_HINT
                else:
                    exc.hint = _ARCHIVE_HINT
                raise
            recovery = await self._recover(
                fetch_target,
                requested,
                limit,
                exc,
                archive=archive,
                everything=recovering and walled,
            )
            document = recovery.document
        content_id = self._content_id_of(fetch_target) if recovery is None else None
        payload = self._page_payload(
            document, requested, links=links, max_links=link_cap, content_id=content_id
        )
        if recovery is not None:
            payload.update(recovery.fields)
            payload["recovered_from"] = recovery.recovered_from
            payload["alternates"] = recovery.alternates
            payload["next_action"] = recovery.next_action
        elif payload.get("content_id"):
            self._remember_content(payload["content_id"], requested)
        total_ms = (time.perf_counter() - started) * 1000
        payload["timings"]["total_ms"] = round(total_ms, 2)
        self.metrics.observe("total", total_ms, operation="scrape")
        self.metrics.count("scrapes_total", mode=payload["fetched_with"])
        await self.cache.set(cache_key, _for_cache(payload))
        return _lean_view(payload) if lean else payload

    async def scrape_content(
        self,
        content_id: str,
        *,
        offset: int = 0,
        max_chars: Optional[int] = None,
        lean: bool = True,
    ) -> dict[str, Any]:
        """Read on in a page a scrape cut: the part of its Markdown from *offset*, by ``content_id``.

        The page is read from the page cache, never fetched again, so what comes is the text the
        first scrape cut. The answer is the scrape's own (``next_offset`` and the same
        ``content_id`` while more is left), lean unless ``lean`` is false. An id the cache no
        longer holds is an error that names the page to scrape again.
        """
        limit = self._markdown_limit(max_chars)
        started = time.perf_counter()
        stored = self._pages.get_by_id(content_id)
        if stored is None:
            address = self._content_urls.get(content_id)
            raise WebToolError(
                "content_expired",
                f"The content {content_id} is no longer held (pages are kept for "
                f"{self.settings.page_cache_ttl_seconds} s)",
                url=address,
                hint=(
                    f"Scrape {address} again; its answer gives a new content_id."
                    if address
                    else "Scrape the page again; its answer gives a new content_id."
                ),
            )
        document = self._document_from_cache(stored, limit, started, offset=offset)
        total = document.page.total_chars or 0
        if offset > total:
            raise WebToolError(
                "invalid_request",
                "offset is past the end of the content",
                detail=f"offset {offset}, total_chars {total}",
            )
        payload = self._page_payload(
            document,
            self._canonical(stored.final_url),
            links=False,
            max_links=0,
            content_id=content_id,
            offset=offset,
        )
        payload["cache_hit"] = True
        self.metrics.count("content_reads_total")
        return _lean_view(payload) if lean else payload

    def _content_id_of(self, url: str) -> Optional[str]:
        """The content id of the page the page cache keeps for an address, if it keeps it."""
        key = self._page_key(url)
        return self._pages.id_for(key) if key is not None else None

    def _content_gone(self, answer: Mapping[str, Any]) -> bool:
        """Whether a cached answer names a content_id the page cache no longer holds."""
        content_id = answer.get("content_id")
        return bool(content_id) and self._pages.get_by_id(str(content_id)) is None

    def _remember_content(self, content_id: str, address: str) -> None:
        """Which page a content id was given for, so that its expiry can say what to scrape again."""
        self._content_urls[content_id] = address
        self._content_urls.move_to_end(content_id)
        while len(self._content_urls) > _CONTENT_URLS_KEPT:
            self._content_urls.popitem(last=False)

    def _recovering(self, recover: Optional[bool], archive: bool) -> bool:
        """Whether a walled page is read every way there is: the setting allows it and the call
        asked for it, or did not narrow the way to the archive."""
        if self.settings.scrape_recover == "off":
            return False
        if recover is None:
            return not archive
        return recover

    async def scrape_many(
        self,
        urls: Sequence[str],
        *,
        max_chars: Optional[int] = None,
        archive: bool = False,
        links: bool = True,
        max_links: Optional[int] = None,
        max_total_chars: Optional[int] = None,
        recover: Optional[bool] = None,
        lean: bool = False,
    ) -> dict[str, Any]:
        """Read 2 to 10 pages in one call: ``{"pages": [...], "timings": {...}}``.

        The pages come back in the order given, each its scrape answer, or ``{"url",
        "error"}`` with the error envelope: one page failing never fails the others. At
        most ``scrape_batch_concurrency`` are read at a time and two from one host. The
        whole answer stays under ``max_total_chars``: each page's Markdown is cut to fit
        (see ``fit_scrape_batch``) and says so with ``markdown_truncated``.
        """
        targets = list(urls)
        if not SCRAPE_BATCH_MIN_URLS <= len(targets) <= SCRAPE_BATCH_MAX_URLS:
            raise WebToolError(
                "invalid_request",
                f"urls takes {SCRAPE_BATCH_MIN_URLS} to {SCRAPE_BATCH_MAX_URLS} URLs",
                detail=f"got {len(targets)}",
            )
        cap = self._batch_total(max_total_chars)
        started = time.perf_counter()
        pages = await self._read_pages(
            targets,
            max_chars=max_chars,
            archive=archive,
            links=links,
            max_links=max_links,
            recover=recover,
            lean=lean,
        )
        total_ms = round((time.perf_counter() - started) * 1000, 2)
        answer: dict[str, Any] = {"pages": pages, "timings": {"total_ms": total_ms}}
        fit_scrape_batch(answer, cap)
        self.metrics.count("scrape_batches_total")
        return answer

    async def _read_pages(
        self,
        targets: Sequence[str],
        *,
        max_chars: Optional[int],
        archive: bool,
        links: bool,
        max_links: Optional[int],
        recover: Optional[bool] = None,
        lean: bool = False,
    ) -> list[dict[str, Any]]:
        """Scrape pages together, in the order given: each answer, or ``{"url", "error"}``.

        At most ``scrape_batch_concurrency`` at a time and two from one host. Every page
        answer is a new dict, so what a caller does to it never reaches the cache.
        """
        overall = asyncio.Semaphore(self.settings.scrape_batch_concurrency)
        per_host: dict[str, asyncio.Semaphore] = {}

        async def read(url: str) -> dict[str, Any]:
            gate = per_host.setdefault(self._host_of(url), asyncio.Semaphore(SCRAPE_BATCH_PER_HOST))
            # The host's turn comes first: pages queued behind one host never hold a slot
            # that a page of another host could use.
            async with gate, overall:
                try:
                    page = await self.scrape(
                        url,
                        max_chars=max_chars,
                        archive=archive,
                        links=links,
                        max_links=max_links,
                        recover=recover,
                        lean=lean,
                    )
                except WebToolError as exc:
                    return {"url": url, "error": exc.to_dict()}
                except Exception as exc:  # noqa: BLE001 - one page must not fail the others
                    failure = WebToolError(
                        "internal_error",
                        "The page failed unexpectedly",
                        url=url,
                        detail=type(exc).__name__,
                    )
                    return {"url": url, "error": failure.to_dict()}
            return {**page, "markdown_truncated": False}

        return list(await asyncio.gather(*(read(url) for url in targets)))

    def _host_of(self, url: str) -> str:
        try:
            return (urlsplit(self._request_url(url)).hostname or "").lower()
        except WebToolError:
            return ""

    def _batch_total(self, value: Optional[int]) -> int:
        if value is None:
            return self.settings.scrape_batch_max_total_chars
        if not SCRAPE_BATCH_MIN_TOTAL_CHARS <= int(value) <= SCRAPE_BATCH_MAX_TOTAL_CHARS:
            raise WebToolError(
                "invalid_request",
                f"max_total_chars must be {SCRAPE_BATCH_MIN_TOTAL_CHARS} to "
                f"{SCRAPE_BATCH_MAX_TOTAL_CHARS}",
                detail=f"got {int(value)}",
            )
        return int(value)

    async def _archived_copy(
        self,
        url: str,
        limit: int,
        cause: WebToolError,
    ) -> tuple[LoadedDocument, tuple[str, str], list[dict[str, str]]]:
        """The Internet Archive's newest copy of a page the site would not serve that is readable.

        Its CDX index lists the last copies that answered 200, one per content digest
        (the closest-copy API named whatever was nearest, and for a walled news site that
        was the captcha the archive's crawler met). They are read newest first, at most
        ``_ARCHIVE_MAX_COPIES`` of them and within ``archive_budget_seconds``; the ``id_``
        form of a snapshot's address serves the page as archived, without the archive's
        banner. A copy the challenge check flags (a captcha, a login wall) or with nothing
        to read is skipped and listed, with the reason; the first readable one is the
        answer. With none, the site's error stands, its hint saying why: no copy, walls
        all the way down, an archive that is busy (HTTP 429) or too slow.
        """
        deadline = time.monotonic() + self.settings.archive_budget_seconds
        reader = self._archive or self.fetcher
        try:
            answer = await asyncio.wait_for(
                reader.fetch(_cdx_url(url)), timeout=_remaining(deadline)
            )
            copies = _cdx_copies(answer.html)
        except asyncio.TimeoutError:
            cause.hint = self._archive_budget_hint()
            raise cause from None
        except FetchError as error:
            cause.hint = _archive_unreachable_hint(error.code, error.status_code)
            raise cause from error

        skipped: list[dict[str, str]] = []
        for stamp, original in copies[:_ARCHIVE_MAX_COPIES]:
            snapshot = f"https://web.archive.org/web/{stamp}id_/{original}"
            taken = _taken_at(stamp)
            try:
                document = await asyncio.wait_for(
                    self._load_page(
                        snapshot, max_chars=limit, browser=False, cache=False, fetcher=reader
                    ),
                    timeout=_remaining(deadline),
                )
            except asyncio.TimeoutError:
                cause.hint = self._archive_budget_hint()
                raise cause from None
            except WebToolError as error:
                if error.status_code == 429:
                    cause.hint = _archive_unreachable_hint(error.code, 429)
                    raise cause from error
                skipped.append({"timestamp": taken, "reason": error.detail or error.code})
                continue
            if not document.page.markdown.strip():
                skipped.append({"timestamp": taken, "reason": "empty_content"})
                continue
            if document.pages is None:
                # The copy's links and identity are the page's own, not the archive's.
                document.page = extract_page(
                    document.html,
                    url,
                    max_links=self.settings.max_links,
                    max_chars=limit,
                )
            document.final_url = url
            document.fetched_with = "archive"
            document.warnings.append("archived_copy")
            self.metrics.count("archive_copies_total")
            return document, (taken, snapshot), skipped

        if not copies:
            cause.hint = (
                "The Internet Archive holds no archived copy of this page; "
                "find the story elsewhere with web_search."
            )
        else:
            walls = "; ".join(f"{item['timestamp'][:10]}: {item['reason']}" for item in skipped)
            cause.hint = (
                f"The Internet Archive's newest copies of this page are walls too ({walls}); "
                "find the story elsewhere with web_search."
            )
        raise cause

    def _archive_budget_hint(self) -> str:
        seconds = self.settings.archive_budget_seconds
        return (
            f"The Internet Archive took longer than the time budget ({seconds:g} s, "
            "WEB_TOOLS_ARCHIVE_BUDGET_SECONDS); try archive: true again later, or find the "
            "story elsewhere with web_search."
        )

    async def _recover(
        self,
        fetch_target: str,
        requested: str,
        limit: int,
        cause: WebToolError,
        *,
        archive: bool,
        everything: bool,
    ) -> _Recovery:
        """Read a walled page another way, or raise its error with what was found.

        With ``everything`` false only the Internet Archive is asked (``archive: true`` alone).
        Otherwise, within ``recover_budget_seconds``: the archive, the page's AMP and print
        forms, and its feed are tried together; the text that comes of them, the page's own, is
        the answer (the freshest form that holds nearly as much as the longest, the archive's copy
        first when ``archive`` was asked for). A feed that carries only a summary, or nothing
        found, goes on to another outlet's report of the same story, found by searching the page's
        title (from the wall, else from the address). Whatever the way, the text begins with a line
        that says whose it is. With nothing readable, the wall's error is raised with
        ``alternates`` (candidate addresses, each with its domain, title, date, source type and why)
        and a ``next_action`` the agent can act on.
        """
        if not everything:
            document, archived, skipped = await self._archived_copy(fetch_target, limit, cause)
            only = _Recovery(
                "archive",
                document,
                archived[1],
                taken=archived[0],
                fields={
                    "archived_at": archived[0],
                    "archive_url": archived[1],
                    "archive_skipped": skipped,
                },
            )
            return self._finish_recovery(only, requested, limit, [])

        deadline = time.monotonic() + self.settings.recover_budget_seconds
        tried: dict[str, str] = {}

        async def run(method: str, work: Any) -> list[_Recovery]:
            try:
                made, reason = await asyncio.wait_for(work, timeout=_remaining(deadline))
            except asyncio.TimeoutError:
                tried[method] = "out of time"
                return []
            except Exception as error:  # noqa: BLE001 - one way failing never fails the others
                tried[method] = type(error).__name__
                return []
            tried[method] = "read" if made else reason
            return list(made)

        archived_copies, forms, feeds = await asyncio.gather(
            run("archive", self._try_archive(fetch_target, limit, cause)),
            run("amp/print", self._try_forms(fetch_target, limit, cause)),
            run("feed", self._try_feed(fetch_target, limit, cause)),
        )
        pages = [*archived_copies, *forms]
        chosen = _best_of_the_page(pages, prefer_archive=archive)
        partial_feed: Optional[_Recovery] = None
        if chosen is None and feeds:
            if feeds[0].complete:
                chosen = feeds[0]
            else:
                partial_feed = feeds[0]
        alternates: list[dict[str, Any]] = []
        if chosen is None:
            made_other, alternates, reason = await self._try_other_sources(
                fetch_target, requested, limit, cause, deadline
            )
            tried["other sources"] = "read" if made_other else reason
            chosen = made_other or partial_feed
        else:
            alternates = [
                self._form_alternate(other) for other in [*pages, *feeds] if other is not chosen
            ]
        self.metrics.count("recoveries_total", kind=chosen.kind if chosen else "none")
        if chosen is None:
            summary = "; ".join(
                f"{method}: {tried.get(method, 'not tried')}"
                for method in ("archive", "amp/print", "feed", "other sources")
            )
            cause.hint = (
                "The page is walled and no other way of reading it gave a text "
                f"({summary}). Read the story elsewhere: see alternates and next_action."
            )
            cause.extra["alternates"] = alternates
            cause.extra["next_action"] = _next_action(None, requested, alternates)
            cause.extra["recovery_tried"] = [
                {"method": method, "result": tried.get(method, "not tried")}
                for method in ("archive", "amp/print", "feed", "other sources")
            ]
            raise cause
        return self._finish_recovery(chosen, requested, limit, alternates)

    def _finish_recovery(
        self, recovery: _Recovery, requested: str, limit: int, alternates: list[dict[str, Any]]
    ) -> _Recovery:
        """Put on a recovered text the line that says whose it is, and fill in what is said of it."""
        document = recovery.document
        header = recovery_header(
            recovery.kind,
            requested_url=requested,
            source_url=recovery.source_url,
            taken=recovery.taken,
            title=recovery.title or document.page.title,
            complete=recovery.complete,
        )
        room = max(limit - len(header) - 2, 0)
        body = document.page.markdown
        whole = document.page.total_chars if document.page.total_chars is not None else len(body)
        recovery.fields.update(truncated=whole > min(len(body), room), total_chars=whole)
        document.page.markdown = f"{header}\n\n{body[:room]}"
        if recovery.kind == "archive":
            document.warnings.append("archived_copy")
            self.metrics.count("archive_copies_total")
        else:
            document.warnings.append(f"recovered_{recovery.kind}")
        if recovery.kind == "feed" and not recovery.complete:
            document.warnings.append("feed_summary_only")
        recovery.alternates = alternates[: self.settings.recover_max_alternates]
        recovery.next_action = _next_action(recovery, requested, recovery.alternates)
        return recovery

    @staticmethod
    def _form_alternate(recovery: _Recovery) -> dict[str, Any]:
        page = recovery.document.page
        why = {
            "archive": f"the Internet Archive's copy of the same page, taken {recovery.taken}",
            "amp": "the site's AMP version of the same page",
            "print": "the site's print version of the same page",
            "feed": "the page's entry in the site's feed"
            + ("" if recovery.complete else " (a summary only)"),
        }.get(recovery.kind, "another form of the same page")
        return {
            "url": recovery.source_url,
            "domain": host_of(recovery.source_url),
            "title": page.title,
            "date": page.dates.date if page.dates is not None else None,
            "source_type": classify_source_type(recovery.source_url),
            "why": why,
        }

    async def _try_archive(
        self, fetch_target: str, limit: int, cause: WebToolError
    ) -> tuple[list[_Recovery], str]:
        probe = WebToolError(
            cause.code,
            str(cause),
            url=cause.url,
            status_code=cause.status_code,
            detail=cause.detail,
        )
        try:
            document, archived, skipped = await self._archived_copy(fetch_target, limit, probe)
        except WebToolError as failed:
            return [], _archive_reason(failed.hint or "")
        return [
            _Recovery(
                "archive",
                document,
                archived[1],
                taken=archived[0],
                fields={
                    "archived_at": archived[0],
                    "archive_url": archived[1],
                    "archive_skipped": skipped,
                },
            )
        ], ""

    async def _try_forms(
        self, fetch_target: str, limit: int, cause: WebToolError
    ) -> tuple[list[_Recovery], str]:
        """The page's AMP and print forms: the wall's own ``amphtml`` link and the usual spellings."""
        candidates = amp_candidates(fetch_target, cause.body)

        async def read(kind: str, address: str) -> Optional[_Recovery]:
            try:
                document = await self._load_page(
                    address, max_chars=limit, browser=False, cache=False
                )
            except WebToolError:
                return None
            if len(document.page.markdown.strip()) < MIN_RECOVERED_CHARS:
                return None
            document.final_url = fetch_target
            document.fetched_with = kind
            return _Recovery(kind, document, address)

        read_all = [
            made
            for made in await asyncio.gather(*(read(kind, address) for kind, address in candidates))
            if made is not None
        ]
        best: dict[str, _Recovery] = {}
        for made in read_all:
            held = best.get(made.kind)
            if held is None or len(made.document.page.markdown) > len(held.document.page.markdown):
                best[made.kind] = made
        return list(best.values()), "none of the usual forms is served"

    async def _try_feed(
        self, fetch_target: str, limit: int, cause: WebToolError
    ) -> tuple[list[_Recovery], str]:
        """The page's entry in the site's RSS or Atom feed, when a feed carries it."""
        title = wall_title(cause.body) or title_from_url(fetch_target)

        async def read(feed_url: str) -> Optional[_Recovery]:
            try:
                fetched = await self.fetcher.fetch(feed_url)
            except Exception:  # noqa: BLE001 - a feed that is not there is not an error
                return None
            item = find_feed_item(parse_feed(fetched.html), fetch_target, title)
            if item is None:
                return None
            html, complete = feed_document_html(item)
            page = extract_page(
                html, fetch_target, max_links=self.settings.max_links, max_chars=limit
            )
            if len(page.markdown.strip()) < MIN_FEED_CHARS:
                return None
            return _Recovery(
                "feed",
                LoadedDocument(
                    final_url=fetch_target,
                    status_code=200,
                    content_type="text/html",
                    html=html,
                    page=page,
                    fetched_with="feed",
                    timings={},
                ),
                feed_url,
                complete=complete,
            )

        found = [
            made
            for made in await asyncio.gather(
                *(read(feed_url) for feed_url in feed_candidates(fetch_target, cause.body))
            )
            if made is not None
        ]
        found.sort(key=lambda made: (not made.complete, -len(made.document.page.markdown)))
        return found[:1], "no feed carries the page"

    async def _try_other_sources(
        self,
        fetch_target: str,
        requested: str,
        limit: int,
        cause: WebToolError,
        deadline: float,
    ) -> tuple[Optional[_Recovery], list[dict[str, Any]], str]:
        """Another outlet's report of the same story: search the page's title away from its site.

        The title is what the wall still shows, else what the address spells. Results of another
        site whose title holds the story's words are the candidates, outlets and official pages
        first; the first two are read, the first that reads is the answer, the rest are
        ``alternates`` with why each is offered.
        """
        title = wall_title(cause.body) or title_from_url(fetch_target)
        if not title:
            return None, [], "the address and the wall give no title to search"
        site = host_of(fetch_target)
        try:
            found = await asyncio.wait_for(
                self.search(
                    title,
                    depth="fast",
                    limit=10,
                    exclude_sites=[site] if site else None,
                    resolve_dates=False,
                ),
                timeout=_remaining(deadline),
            )
        except (WebToolError, asyncio.TimeoutError):
            return None, [], "no search provider answered"
        matches: list[tuple[float, dict[str, Any]]] = []
        for item in found.get("results", []):
            if same_site(item["url"], fetch_target) or item.get("kind") in {"front", "listing"}:
                continue
            share = same_story(title, item["title"])
            if share is not None:
                matches.append((share, item))
        if not matches:
            return None, [], "no other site has the story's title"
        order = {"official": 0, "news": 1, "academic": 2, "reference": 3}
        matches.sort(
            key=lambda pair: (order.get(pair[1]["source_type"], 9), -pair[0], -(pair[1]["score"]))
        )
        alternates = [
            {
                "url": item["url"],
                "domain": item["domain"],
                "title": item["title"],
                "date": item.get("date"),
                "source_type": item["source_type"],
                "why": f"{share:.0%} of this story's title words, on {item['domain']}",
            }
            for share, item in matches[: self.settings.recover_max_alternates]
        ]
        readable = [pair for pair in matches if pair[1]["source_type"] not in _NOT_A_REPORT]
        for _, item in readable[:2]:
            try:
                document = await asyncio.wait_for(
                    self._load_page(item["url"], max_chars=limit, browser=False),
                    timeout=_remaining(deadline),
                )
            except (WebToolError, asyncio.TimeoutError):
                continue
            if len(document.page.markdown.strip()) < MIN_RECOVERED_CHARS:
                continue
            for alternate in alternates:
                if alternate["url"] == item["url"]:
                    alternate["why"] += " (read)"
            # The page's own title: a search result's is cut by the provider with "...".
            return (
                _Recovery(
                    "other_source",
                    document,
                    document.final_url,
                    title=(document.page.title or item["title"])[:160],
                ),
                alternates,
                "",
            )
        return None, alternates, "the candidates could not be read either"

    async def extract(
        self,
        url: str,
        schema: Mapping[str, Any],
        *,
        max_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        requested = self._canonical(url)
        fetch_target = self._request_url(url)
        validated = self._validate_schema(schema)
        budget = self._extract_budget(max_chars)
        document = await self._load_page(
            fetch_target,
            max_chars=self.settings.markdown_max_chars,
        )

        started = time.perf_counter()
        structured = extract_structured(
            document.html,
            document.final_url,
            validated,
            max_items=self.settings.extract_max_items,
            dates=document.page.dates,
        )
        extract_ms = (time.perf_counter() - started) * 1000
        self.metrics.observe("extraction", extract_ms, operation="structured")
        total_ms = document.timings.get("total_ms", 0.0) + extract_ms
        self.metrics.observe("total", total_ms, operation="extract")
        self.metrics.count("extracts_total")

        timings = dict(document.timings)
        timings["extract_ms"] = round(extract_ms, 2)
        timings["total_ms"] = round(total_ms, 2)
        payload: dict[str, Any] = {
            "url": document.final_url,
            "requested_url": requested,
            "title": document.page.title,
            "description": document.page.description,
            "canonical_url": document.page.canonical_url,
            "status_code": document.status_code,
            "data": structured.data,
            "provenance": structured.provenance,
            "errors": structured.errors,
            "fetched_with": document.fetched_with,
            "cache_hit": document.cache_hit,
            "timings": timings,
            "warnings": list(document.warnings),
            "truncated": False,
        }
        payload.update(_date_fields(document.page.dates))
        if document.cache_hit:
            payload["cached_timings"] = dict(document.cached_timings)
        cut = fit_extract_payload(payload, budget)
        payload["truncated"] = structured.truncated or cut
        return payload

    async def map(  # noqa: A003 - public API name
        self,
        url: str,
        *,
        include_subdomains: bool = False,
        search: Optional[str] = None,
        include_patterns: Optional[list[str]] = None,
        exclude_patterns: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        base = self._canonical(url)
        includes = list(include_patterns or [])
        excludes = list(exclude_patterns or [])
        max_urls = min(
            max(limit or self.settings.default_map_limit, 1),
            self.settings.max_map_limit,
        )
        entries, sitemaps, errors = await self._read_sitemaps(base, include_subdomains)

        try:
            page_fetch = await self.fetcher.fetch(self._request_url(url))
            page = extract_page(
                page_fetch.html,
                page_fetch.final_url,
                max_links=self.settings.max_links,
                max_chars=self.settings.markdown_max_chars,
            )
            for link in page.links:
                entries.setdefault(
                    link.url,
                    {"url": link.url, "lastmod": None, "source": "links"},
                )
            # The base is listed once its page was read: a host that does not
            # resolve, or a page that is not there, is no URL of the site.
            entries.setdefault(base, {"url": base, "lastmod": None, "source": "base"})
        except Exception as exc:  # noqa: BLE001 - sitemap results may still be useful
            errors.append(self._safe_error(base, exc))

        matched = [
            target
            for target in entries
            if url_in_scope(target, base, include_subdomains)
            and matches_url_filters(target, includes, excludes)
            and (not search or matches_search_filter(target, search))
        ]
        urls = matched[:max_urls]

        self.metrics.count("maps_total")
        return {
            "url": base,
            "urls": urls,
            "total": len(matched),
            "returned": len(urls),
            "truncated": len(matched) > len(urls),
            "entries": [entries[target] for target in urls],
            "sitemaps": sitemaps,
            "errors": errors,
        }

    async def _read_sitemaps(
        self, base: str, include_subdomains: bool
    ) -> tuple[dict[str, dict[str, Any]], list[str], list[dict[str, Any]]]:
        """The URLs of a site's sitemaps, as ``web_map`` and a seeded crawl read them.

        The ``Sitemap:`` lines of robots.txt and ``/sitemap.xml``, and the sitemaps an index
        names (at most ``max_sitemaps``, in scope of *base*). Returns the entries by
        canonical URL (``source: sitemap``), the sitemaps read, and the errors met: one
        sitemap that cannot be read never fails the rest.
        """
        origin = _origin(base)
        errors: list[dict[str, Any]] = []
        entries: dict[str, dict[str, Any]] = {}
        sitemaps: list[str] = []

        candidates: list[str] = []
        robots_url = origin + "/robots.txt"
        try:
            robots = await self.fetcher.fetch(robots_url)
            for line in robots.html.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    candidate = line.split(":", 1)[1].strip()
                    if candidate:
                        candidates.append(candidate)
        except Exception as exc:  # noqa: BLE001 - robots is optional for mapping
            errors.append(self._safe_error(robots_url, exc))

        candidates.append(origin + "/sitemap.xml")
        queue: list[str] = []
        seen_sitemaps: set[str] = set()
        for candidate in candidates:
            try:
                normalized = canonicalize_url(candidate)
            except UnsafeUrlError:
                continue
            if normalized not in seen_sitemaps:
                seen_sitemaps.add(normalized)
                queue.append(normalized)

        while queue and len(sitemaps) < self.settings.max_sitemaps:
            sitemap_url = queue.pop(0)
            if not url_in_scope(sitemap_url, base, include_subdomains):
                continue
            try:
                fetched = await self.fetcher.fetch(sitemap_url)
                kind, parsed = parse_sitemap_xml(fetched.html.encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - one sitemap must not fail the map
                errors.append(self._safe_error(sitemap_url, exc))
                continue

            sitemaps.append(sitemap_url)
            if kind == "urlset":
                for item in parsed:
                    try:
                        target = canonicalize_url(item["url"])
                    except UnsafeUrlError:
                        continue
                    entries.setdefault(
                        target,
                        {"url": target, "lastmod": item.get("lastmod"), "source": "sitemap"},
                    )
            elif kind == "sitemapindex":
                for item in parsed:
                    try:
                        child = canonicalize_url(item["url"])
                    except UnsafeUrlError:
                        continue
                    if child not in seen_sitemaps and url_in_scope(child, base, include_subdomains):
                        seen_sitemaps.add(child)
                        queue.append(child)

        return entries, sitemaps, errors

    async def _sitemap_seeds(
        self, base: str, options: CrawlOptions
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """The pages a site's sitemaps list, for a crawl that asked to be seeded from them."""
        entries, _, errors = await self._read_sitemaps(base, options.include_subdomains)
        return list(entries), errors

    async def start_crawl(
        self,
        url: str,
        options: Optional[Mapping[str, Any] | Any] = None,
    ) -> dict[str, Any]:
        base = canonical_target(url)
        resolved = CrawlOptions.from_dict(options, self.settings)
        manager = self._crawl_manager()
        snapshot = await manager.create(base, resolved, upgraded=base != self._canonical(url))
        self.metrics.count("crawls_started_total")
        return snapshot

    async def get_crawl(self, job_id: str) -> dict[str, Any]:
        return await self._crawl_manager().get(job_id)

    async def crawl_results(
        self,
        job_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await self._crawl_manager().results(job_id, offset=offset, limit=limit)

    async def cancel_crawl(self, job_id: str) -> dict[str, Any]:
        return await self._crawl_manager().cancel(job_id)

    async def list_crawls(self) -> list[dict[str, Any]]:
        return await self._crawl_manager().list_jobs()

    async def metrics_snapshot(self) -> dict[str, Any]:
        cache = await self.cache.snapshot()
        return {
            "latency": self.metrics.tracker.snapshot(),
            "cache": cache,
            "page_cache": self._pages.snapshot(),
            "browser": self.browser.snapshot() if self.browser is not None else {"enabled": False},
            "search_providers": {
                name: {
                    "consecutive_failures": health.consecutive_failures,
                    "cooldown_remaining_s": round(health.retry_after(), 2),
                }
                for name, health in self._provider_health.items()
            },
        }

    def _crawl_manager(self) -> CrawlManager:
        if self._crawl is None:
            robots = (
                RobotsPolicy(
                    self.fetcher,
                    cache_ttl_seconds=self.settings.robots_cache_ttl_seconds,
                )
                if self.settings.respect_robots
                else None
            )
            store = (
                CrawlStore(Path(self.settings.data_dir) / "crawl-jobs.sqlite3")
                if self.settings.data_dir
                else None
            )
            self._crawl = CrawlManager(
                self._crawl_page,
                self.settings,
                robots=robots,
                seeder=self._sitemap_seeds,
                store=store,
            )
        return self._crawl

    async def _crawl_page(self, url: str) -> dict[str, Any]:
        limit = self.settings.crawl_content_limit_chars
        document = await self._load_page(url, max_chars=limit, cache=False)
        final_url = document.final_url
        page = document.page
        secure = upgrade_to_https(final_url)
        if (
            url.startswith("https://")
            and secure != final_url
            and urlsplit(final_url).hostname == urlsplit(url).hostname
        ):
            # The site redirected an https request to http on the same host
            # (quotes.toscrape.com does it for every trailing slash). Read the
            # page as its https self, so its links stay https and the same
            # content hashes the same whichever scheme reached it.
            final_url = secure
            if document.html:
                page = extract_page(
                    document.html,
                    final_url,
                    max_links=self.settings.max_links,
                    max_chars=limit,
                )
        return {
            "url": final_url,
            "title": page.title,
            "description": page.description,
            "markdown": page.markdown,
            "status_code": document.status_code,
            "content_hash": content_hash(page.markdown),
            "fetched_with": document.fetched_with,
            "links": [link.url for link in page.links],
        }

    async def _load_page(
        self,
        url: str,
        *,
        max_chars: int,
        browser: bool = True,
        cache: bool = True,
        fetcher: Optional[Any] = None,
    ) -> LoadedDocument:
        """Read a page, from the page cache when it was fetched a moment ago.

        The cache holds the fetched document (see ``PageCache``), so each call parses it with
        its own cap. A page is stored only when it read well: errors, challenge pages (they
        are errors), PDFs, a page with nothing to read, a failed browser render and anything
        read with ``cache=False`` (archived copies, crawled pages) never are.
        """
        key = self._page_key(url) if cache else None
        if key is not None:
            started = time.perf_counter()
            stored = self._pages.get(key)
            if stored is not None:
                self.metrics.count("page_cache_hits_total")
                return self._document_from_cache(stored, max_chars, started)
        document = await self._fetch_document(
            url, max_chars=max_chars, browser=browser, fetcher=fetcher
        )
        if key is not None and _storable(document):
            self._pages.put(
                key,
                CachedDocument(
                    final_url=document.final_url,
                    status_code=document.status_code,
                    content_type=document.content_type,
                    html=document.html,
                    fetched_with=document.fetched_with,
                    warnings=tuple(document.warnings),
                    timings=dict(document.timings),
                ),
            )
        return document

    @staticmethod
    def _page_key(url: str) -> Optional[str]:
        try:
            return canonicalize_url(url)
        except UnsafeUrlError:
            return None

    def _document_from_cache(
        self, stored: CachedDocument, max_chars: int, started: float, *, offset: int = 0
    ) -> LoadedDocument:
        """A stored document read again: parsed for this call, its timings this call's own."""
        parsing = time.perf_counter()
        page = extract_page(
            stored.html,
            stored.final_url,
            max_links=self.settings.max_links,
            max_chars=max_chars,
            offset=offset,
        )
        extract_ms = (time.perf_counter() - parsing) * 1000
        self.metrics.observe("extraction", extract_ms, mode="cached")
        return LoadedDocument(
            final_url=stored.final_url,
            status_code=stored.status_code,
            content_type=stored.content_type,
            html=stored.html,
            page=page,
            fetched_with=stored.fetched_with,
            warnings=list(stored.warnings),
            timings={
                "network_ms": 0.0,
                "http_ms": 0.0,
                "extract_ms": round(extract_ms, 2),
                "browser_ms": 0.0,
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            cache_hit=True,
            cached_timings=dict(stored.timings),
        )

    async def _fetch_document(
        self,
        url: str,
        *,
        max_chars: int,
        browser: bool = True,
        fetcher: Optional[Any] = None,
    ) -> LoadedDocument:
        """Fetch and read a page; ``browser=False`` never opens a headless browser for it.

        *fetcher* is what fetches it when it is not ``self.fetcher`` (web.archive.org's own).
        """
        started = time.perf_counter()
        try:
            fetched = await (fetcher or self.fetcher).fetch(url)
        except FetchError as exc:
            if (
                browser
                and exc.code == "blocked"
                and exc.detail == "access_blocked"
                and self.browser is not None
            ):
                try:
                    return await self._render_document(
                        url,
                        max_chars,
                        warnings=["http_access_blocked"],
                    )
                except WebToolError as render_error:
                    if render_error.code == "blocked":
                        raise
                    # The browser got nowhere either: report what the HTTP fetch saw.
                    raise self._translate_fetch_error(exc) from render_error
            raise self._translate_fetch_error(exc) from exc

        self.metrics.observe("http_fetch", fetched.total_ms, mode="http")
        self.metrics.observe("http_network", fetched.network_ms, mode="http")
        if fetched.content_type == PDF_CONTENT_TYPE:
            return await self._pdf_document(fetched, max_chars, started)

        extraction_started = time.perf_counter()
        page = extract_page(
            fetched.html,
            fetched.final_url,
            max_links=self.settings.max_links,
            max_chars=max_chars,
        )
        extract_ms = (time.perf_counter() - extraction_started) * 1000
        self.metrics.observe("extraction", extract_ms, mode="html")

        document = LoadedDocument(
            final_url=fetched.final_url,
            status_code=fetched.status_code,
            content_type=fetched.content_type,
            html=fetched.html,
            page=page,
            fetched_with="http",
            warnings=[],
            timings={
                "network_ms": round(fetched.network_ms, 2),
                "http_ms": round(fetched.total_ms, 2),
                "extract_ms": round(extract_ms, 2),
                "browser_ms": 0.0,
            },
        )

        if (
            browser
            and self.browser is not None
            and needs_browser_render(
                fetched.html,
                page.markdown,
                min_chars=self.settings.browser_render_min_chars,
            )
        ):
            browser_started = time.perf_counter()
            try:
                rendered = await self.browser.render(fetched.final_url)
            except Exception as exc:  # noqa: BLE001 - keep usable HTTP content
                browser_ms = (time.perf_counter() - browser_started) * 1000
                self.metrics.observe("browser_render", browser_ms, outcome="failed")
                document.timings["browser_ms"] = round(browser_ms, 2)
                code = getattr(exc, "code", type(exc).__name__)
                document.warnings.append(f"browser_render_failed:{code}")
            else:
                browser_ms = (time.perf_counter() - browser_started) * 1000
                self.metrics.observe("browser_render", browser_ms, outcome="ok")
                document.timings["browser_ms"] = round(browser_ms, 2)
                document = self._merge_rendered(document, rendered, max_chars)

        if not document.page.markdown.strip():
            document.warnings.append("empty_content")

        document.timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return document

    async def _pdf_document(
        self,
        fetched: FetchResult,
        max_chars: int,
        started: float,
    ) -> LoadedDocument:
        extraction_started = time.perf_counter()
        try:
            # pypdf is pure Python: keep it off the event loop that serves other calls.
            pdf = await asyncio.wait_for(
                asyncio.to_thread(extract_pdf, fetched.content, max_chars=max_chars),
                timeout=self.settings.fetch_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise WebToolError(
                "timeout",
                "PDF text extraction timed out",
                url=fetched.final_url,
                status_code=fetched.status_code,
                detail="pdf_extract_timeout",
            ) from exc
        except PdfError as exc:
            raise WebToolError(
                exc.code,
                str(exc),
                url=fetched.final_url,
                status_code=fetched.status_code,
                detail=exc.detail,
            ) from exc
        extract_ms = (time.perf_counter() - extraction_started) * 1000
        self.metrics.observe("extraction", extract_ms, mode="pdf")

        try:
            canonical = canonicalize_url(fetched.final_url)
        except UnsafeUrlError:
            canonical = fetched.final_url
        page = ParsedPage(
            title=pdf.title,
            description=pdf.description,
            canonical_url=canonical,
            markdown=pdf.markdown,
            metadata={"canonical_url": canonical, "pdf": pdf.metadata},
        )
        warnings = list(pdf.warnings)
        if not pdf.markdown.strip():
            warnings.append("empty_content")
        return LoadedDocument(
            final_url=fetched.final_url,
            status_code=fetched.status_code,
            content_type=PDF_CONTENT_TYPE,
            html="",
            page=page,
            fetched_with="http",
            warnings=warnings,
            timings={
                "network_ms": round(fetched.network_ms, 2),
                "http_ms": round(fetched.total_ms, 2),
                "extract_ms": round(extract_ms, 2),
                "browser_ms": 0.0,
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            pages=pdf.pages,
        )

    async def _render_document(
        self,
        url: str,
        max_chars: int,
        *,
        warnings: Optional[list[str]] = None,
    ) -> LoadedDocument:
        if self.browser is None:
            raise WebToolError("browser_unavailable", "Headless browser is disabled", url=url)

        started = time.perf_counter()
        try:
            rendered = await self.browser.render(url)
        except (BrowserUnavailableError, BrowserRenderError) as exc:
            raise self._translate_browser_error(exc, url) from exc
        browser_ms = (time.perf_counter() - started) * 1000
        self.metrics.observe("browser_render", browser_ms, outcome="ok")

        interstitial = detect_interstitial(
            rendered.html,
            rendered.final_url,
            rendered.status_code or 200,
        )
        if interstitial:
            raise WebToolError(
                "blocked",
                interstitial_message(interstitial),
                url=url,
                status_code=rendered.status_code or None,
                detail=interstitial,
                body=rendered.html[:30_000],
            )

        extraction_started = time.perf_counter()
        page = extract_page(
            rendered.html,
            rendered.final_url,
            max_links=self.settings.max_links,
            max_chars=max_chars,
        )
        extract_ms = (time.perf_counter() - extraction_started) * 1000
        self.metrics.observe("extraction", extract_ms, mode="browser")
        total_ms = (time.perf_counter() - started) * 1000
        document = LoadedDocument(
            final_url=rendered.final_url,
            status_code=rendered.status_code,
            content_type="text/html",
            html=rendered.html,
            page=page,
            fetched_with="browser",
            warnings=list(warnings or []),
            timings={
                "network_ms": 0.0,
                "http_ms": 0.0,
                "extract_ms": round(extract_ms, 2),
                "browser_ms": round(browser_ms, 2),
                "total_ms": round(total_ms, 2),
            },
        )
        if not page.markdown.strip():
            document.warnings.append("empty_content")
        return document

    def _merge_rendered(
        self,
        document: LoadedDocument,
        rendered: BrowserPage,
        max_chars: int,
    ) -> LoadedDocument:
        interstitial = detect_interstitial(
            rendered.html,
            rendered.final_url,
            rendered.status_code or 200,
        )
        if interstitial:
            raise WebToolError(
                "blocked",
                interstitial_message(interstitial),
                url=document.final_url,
                status_code=rendered.status_code or None,
                detail=interstitial,
                body=rendered.html[:30_000],
            )

        extraction_started = time.perf_counter()
        page = extract_page(
            rendered.html,
            rendered.final_url,
            max_links=self.settings.max_links,
            max_chars=max_chars,
        )
        extract_ms = (time.perf_counter() - extraction_started) * 1000
        self.metrics.observe("extraction", extract_ms, mode="browser")

        if len(page.markdown) < len(document.page.markdown):
            return document

        timings = dict(document.timings)
        timings["extract_ms"] = round(timings.get("extract_ms", 0.0) + extract_ms, 2)
        return LoadedDocument(
            final_url=rendered.final_url,
            status_code=rendered.status_code,
            content_type="text/html",
            html=rendered.html,
            page=page,
            fetched_with="browser",
            warnings=list(document.warnings),
            timings=timings,
        )

    def _page_payload(
        self,
        document: LoadedDocument,
        requested: str,
        *,
        links: bool = True,
        max_links: Optional[int] = None,
        content_id: Optional[str] = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        # The page's own links, without menus, banners, footers, and sidebars,
        # capped; map and crawl still see every link. ``links=False`` returns none,
        # and says nothing was left out: the caller asked for none.
        content_links = [link for link in document.page.links if not link.chrome]
        cap = self.settings.scrape_max_links if max_links is None else max_links
        shown = content_links[:cap] if links else []
        metadata, metadata_cut = trim_metadata(document.page.metadata)
        payload: dict[str, Any] = {
            "url": document.final_url,
            "requested_url": requested,
            "title": document.page.title,
            "description": document.page.description,
            "canonical_url": document.page.canonical_url,
            "markdown": document.page.markdown,
            "status_code": document.status_code,
            "content_type": document.content_type,
            "links": [{"url": link.url, "text": link.text, "kind": link.kind} for link in shown],
            "links_truncated": links and len(shown) < len(document.page.links),
            "metadata": metadata,
            "metadata_truncated": metadata_cut,
            "content_hash": content_hash(document.page.markdown),
            "fetched_with": document.fetched_with,
            "cache_hit": False,
            "warnings": list(document.warnings),
            "timings": dict(document.timings),
        }
        payload.update(_date_fields(document.page.dates))
        if document.pages is not None:
            payload["pages"] = document.pages
        # Whether the Markdown is the whole of the page, and how to read the rest: from where the
        # next part starts, and (for a page the page cache keeps) the id to ask for it with.
        total = document.page.total_chars
        end = offset + len(document.page.markdown)
        truncated = total > end if total is not None else "pdf_truncated" in document.warnings
        payload["truncated"] = truncated
        if total is not None:
            payload["total_chars"] = total
        if offset:
            payload["offset"] = offset
        if truncated and total is not None:
            payload["next_offset"] = end
            if content_id:
                payload["content_id"] = content_id
        return payload

    def _links_limit(self, value: Optional[int]) -> int:
        """How many links a scrape lists: the setting, or the request's own, held to both ceilings."""
        if value is None:
            return self.settings.scrape_max_links
        if not 0 <= int(value) <= SCRAPE_MAX_LINKS_CEILING:
            raise WebToolError(
                "invalid_request",
                f"max_links must be 0 to {SCRAPE_MAX_LINKS_CEILING}",
                detail=f"got {int(value)}",
            )
        return min(int(value), self.settings.max_links)

    def _markdown_limit(self, value: Optional[int]) -> int:
        if value is None:
            return self.settings.markdown_max_chars
        if int(value) < MIN_MARKDOWN_CHARS:
            raise WebToolError(
                "invalid_request",
                f"max_chars must be at least {MIN_MARKDOWN_CHARS}",
                detail=f"got {int(value)}",
            )
        return min(int(value), self.settings.markdown_max_chars)

    def _canonical(self, url: str) -> str:
        try:
            return canonicalize_url(url)
        except UnsafeUrlError as exc:
            raise WebToolError("unsafe_target", str(exc), url=url) from exc

    def _request_url(self, url: str) -> str:
        try:
            return normalize_request_url(url)
        except UnsafeUrlError as exc:
            raise WebToolError("unsafe_target", str(exc), url=url) from exc

    def _extract_budget(self, value: Optional[int]) -> int:
        if value is None:
            return self.settings.extract_max_chars
        if int(value) < EXTRACT_MAX_CHARS_FLOOR:
            raise WebToolError(
                "invalid_request",
                f"max_chars must be at least {EXTRACT_MAX_CHARS_FLOOR}",
                detail=f"got {int(value)}",
            )
        return min(int(value), EXTRACT_MAX_CHARS_CEILING)

    def _validate_schema(self, schema: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(schema, Mapping) or not schema:
            raise WebToolError("invalid_request", "Schema must be a non-empty object")
        if len(schema) > self.settings.max_extract_fields:
            raise WebToolError(
                "invalid_request",
                f"Schema exceeds {self.settings.max_extract_fields} fields",
            )
        validated: dict[str, Any] = {}
        for field_name, spec in schema.items():
            if isinstance(spec, str):
                validated[str(field_name)] = spec
                continue
            if not isinstance(spec, Mapping):
                raise WebToolError(
                    "invalid_request",
                    f"Schema field {field_name!r} must be a selector string or object",
                )
            items = spec.get("max_items")
            if items is not None and (
                isinstance(items, bool)
                or not isinstance(items, int)
                or not 1 <= items <= EXTRACT_MAX_ITEMS_CEILING
            ):
                raise WebToolError(
                    "invalid_request",
                    f"Schema field {field_name!r}: max_items must be 1 to "
                    f"{EXTRACT_MAX_ITEMS_CEILING}",
                )
            offset = spec.get("offset")
            if offset is not None and (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= EXTRACT_OFFSET_CEILING
            ):
                raise WebToolError(
                    "invalid_request",
                    f"Schema field {field_name!r}: offset must be 0 to {EXTRACT_OFFSET_CEILING}",
                )
            validated[str(field_name)] = dict(spec)
        return validated

    @staticmethod
    def _translate_fetch_error(exc: FetchError) -> WebToolError:
        return WebToolError(
            exc.code,
            str(exc),
            url=exc.url,
            status_code=exc.status_code,
            detail=exc.detail,
            body=exc.body,
        )

    @staticmethod
    def _translate_browser_error(
        exc: BrowserUnavailableError | BrowserRenderError,
        url: str,
    ) -> WebToolError:
        if isinstance(exc, BrowserUnavailableError):
            return WebToolError("browser_unavailable", str(exc), url=url)
        code = getattr(exc, "code", "browser_unavailable")
        return WebToolError(code, str(exc), url=url)

    @staticmethod
    def _safe_error(url: str, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, WebToolError):
            payload = exc.to_dict()
            payload["url"] = payload.get("url") or url
            return payload
        if isinstance(exc, FetchError):
            return {
                "code": exc.code,
                "message": str(exc),
                "url": exc.url or url,
                "status_code": exc.status_code,
                "detail": exc.detail,
            }
        return {
            "code": "internal_error",
            "message": type(exc).__name__,
            "url": url,
            "status_code": None,
            "detail": None,
        }


# Failures an archived copy can answer: the page exists but the site would not
# serve it now. A host that does not resolve or a refused target cannot.
_ARCHIVABLE = frozenset({"blocked", "http_error", "timeout", "network_error"})
_ARCHIVE_HINT = (
    "Ask web_scrape again with archive: true for the Internet Archive's latest copy "
    "(it may be days old), or find the same story elsewhere with web_search."
)


_RECOVER_OFF_HINT = (
    "This page is walled and recover is false. Ask again without it, or with archive: true for "
    "the Internet Archive's copy, or find the story elsewhere with web_search."
)
_RECOVER_SETTING_HINT = (
    "This page is walled and the server does not read walled pages another way "
    "(WEB_TOOLS_SCRAPE_RECOVER=off). Ask with archive: true for the Internet Archive's copy, "
    "or find the story elsewhere with web_search."
)
# A feed's text must hold at least this to be given as the page.
MIN_FEED_CHARS = 120
# Results of these kinds are listed as alternates, never read as another outlet's report of the
# story: a post, a forum thread, a repository, an aggregator's copy.
_NOT_A_REPORT = frozenset({"social", "community", "code", "aggregator"})


def _best_of_the_page(pages: list[_Recovery], *, prefer_archive: bool) -> Optional[_Recovery]:
    """Of the page's own forms that read, the one to give: the freshest of those nearly as long as
    the longest (an AMP or print form, then the archive's copy); the archive's first if asked."""
    if not pages:
        return None
    if prefer_archive:
        for page in pages:
            if page.kind == "archive":
                return page
    longest = max(len(page.document.page.markdown) for page in pages)
    for kind in ("amp", "print", "archive"):
        for page in pages:
            if page.kind == kind and len(page.document.page.markdown) >= 0.8 * longest:
                return page
    return pages[0]


def _archive_reason(hint: str) -> str:
    lowered = hint.lower()
    if "no archived copy" in lowered:
        return "no archived copy"
    if "walls too" in lowered:
        return "its newest copies are walls too"
    if "429" in lowered:
        return "the archive is rate limiting"
    if "longer than the time budget" in lowered:
        return "the archive took too long"
    return "the archive did not answer"


def _next_action(
    recovery: Optional[_Recovery], requested: str, alternates: list[dict[str, Any]]
) -> str:
    """One sentence an agent can act on, after a walled page was (or was not) read another way."""
    site = host_of(requested)
    best = alternates[0] if alternates else None
    if recovery is None:
        if best is not None:
            return (
                f"{site} would not serve the page and no other way of reading it worked: scrape "
                f"{best['url']} ({best['domain']}), the nearest report of the story, or another of "
                "alternates."
            )
        return (
            f"{site} would not serve the page and no other way of reading it worked, and no other "
            "site was found with the story: search for it with different words (web_search), or "
            "ask the owner to open the page."
        )
    if recovery.kind == "archive":
        return (
            f"This is the Internet Archive's copy taken {recovery.taken}, not the live page at "
            f"{site}: check the date before relying on it"
            + (f", or cross-check with {best['url']}." if best else ".")
        )
    if recovery.kind in {"amp", "print"}:
        return (
            f"This is {site}'s own {recovery.kind.upper() if recovery.kind == 'amp' else 'print'} "
            "form of the page: its text is the page's, without the live page's extras."
        )
    if recovery.kind == "feed":
        if recovery.complete:
            return f"This is the article as {site}'s feed carries it, without the page's extras."
        return (
            f"{site}'s feed carries only a summary of the article: scrape "
            + (f"{best['url']}" if best else "another source")
            + " for the rest, or ask again later."
        )
    other = next((item for item in alternates if item["url"] != recovery.source_url), None)
    return (
        f"The text is {host_of(recovery.source_url)}'s report, not {site}'s: cross-check it with "
        + (f"{other['url']} or " if other else "")
        + f"scrape {requested} again later."
    )


# The Internet Archive: how many of the newest copies of a walled page are read, and how many
# the CDX index is asked for (it collapses repeats of one digest, so they differ in content).
_ARCHIVE_MAX_COPIES = 4
_ARCHIVE_COPIES_ASKED = 8


def _cdx_url(url: str) -> str:
    """The CDX query for the last copies of ``url`` that answered 200, one per digest."""
    query = urlencode(
        [
            ("url", url),
            ("output", "json"),
            ("filter", "statuscode:200"),
            ("collapse", "digest"),
            ("limit", f"-{_ARCHIVE_COPIES_ASKED}"),
            ("fl", "timestamp,original,statuscode,digest"),
        ],
        safe=":/,",
    )
    return "https://web.archive.org/cdx/search/cdx?" + query


def _cdx_copies(body: str) -> list[tuple[str, str]]:
    """``(timestamp, original)`` of each copy a CDX answer lists, the newest first.

    The answer is a header row and then the copies, the oldest first; nothing found is an
    empty body or a header alone.
    """
    if not (body or "").strip():
        return []
    try:
        rows = json.loads(body)
    except ValueError:
        return []
    copies = [
        (str(row[0]), str(row[1]))
        for row in (rows[1:] if isinstance(rows, list) else [])
        if isinstance(row, list)
        and len(row) >= 2
        and re.fullmatch(r"\d{14}", str(row[0]))
        and str(row[1]).startswith(("http://", "https://"))
    ]
    return copies[::-1]


def _taken_at(stamp: str) -> str:
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()


def _remaining(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.01)


def _archive_unreachable_hint(code: str, status_code: Optional[int]) -> str:
    if status_code == 429:
        return (
            "The Internet Archive answered HTTP 429 (too many requests); try archive: true "
            "again later, or find the story elsewhere with web_search."
        )
    status = f" HTTP {status_code}" if status_code else ""
    return (
        f"The Internet Archive did not answer ({code}{status}); try archive: true "
        "again later, or find the story elsewhere with web_search."
    )


def _storable(document: LoadedDocument) -> bool:
    """Whether a document read well enough for the page cache to keep it."""
    return (
        document.pages is None  # not a PDF
        and document.fetched_with in {"http", "browser"}  # not an archived copy
        and bool(document.html)
        and not any(
            warning == "empty_content" or warning.startswith("browser_render_failed")
            for warning in document.warnings
        )
    )


# A compact search result: these and nothing else, apart from what a read asked for.
_COMPACT_RESULT_KEYS = (
    "title",
    "url",
    "domain",
    "date",
    "date_confidence",
    "source_type",
    "snippet",
    "score",
)
_READ_KEYS = ("content", "content_truncated", "content_error")
# What debug adds to each compact result: how the date was read and the parts of the score.
_DEBUG_RESULT_KEYS = (
    "source",
    "year",
    "recency",
    "date_source",
    "date_conflicts",
    "date_modified",
    "age_days",
    "freshness",
    "kind",
    "angles",
    "relevance",
    "bm25",
)
_DEBUG_RUN_KEYS = (
    "requested_provider",
    "depth",
    "facets",
    "queries",
    "timings",
    "dates",
    "cached_timings",
    "cached_at",
)


def _present_search(answer: dict[str, Any], *, compact: bool, debug: bool) -> dict[str, Any]:
    """A search answer as the caller asked for it.

    Full (``compact`` false) is the whole answer. Compact is, per result, ``title, url, domain,
    date, date_confidence, source_type, snippet`` and ``score`` (and ``content`` or
    ``content_error`` where a read asked for them), and a small ``run`` block that keeps what a
    reader needs to trust the list: the ``provider`` that answered, the ``warnings`` and the
    ``fallback`` taken. A result whose date has conflicts is named in ``warnings`` by its position
    (``date_conflict:2``). ``debug`` adds ``facets``, ``queries``, ``timings`` and the rest in a
    ``debug`` block, and the date's source and the parts of the score under each result's own.
    """
    if not compact:
        return answer
    warnings = list(answer.get("warnings") or [])
    results: list[dict[str, Any]] = []
    for position, item in enumerate(answer.get("results") or [], start=1):
        row = {key: item.get(key) for key in _COMPACT_RESULT_KEYS}
        row.update({key: item[key] for key in _READ_KEYS if key in item})
        if item.get("date_conflicts"):
            warnings.append(f"date_conflict:{position}")
        if debug:
            row["debug"] = {key: item[key] for key in _DEBUG_RESULT_KEYS if key in item}
        results.append(row)
    run: dict[str, Any] = {
        "provider": answer.get("provider"),
        "warnings": warnings,
        "fallback": _run_fallback(answer),
    }
    if answer.get("cache_hit"):
        run["cache_hit"] = True
    shaped: dict[str, Any] = {"query": answer.get("query"), "results": results, "run": run}
    if debug:
        shaped["debug"] = {key: answer[key] for key in _DEBUG_RUN_KEYS if key in answer}
    return shaped


def _run_fallback(answer: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The fallback the search took, from its first angle that took one: who gave way, who stood in, why."""
    for report in answer.get("queries") or []:
        record = report.get("fallback")
        if record:
            return {key: record.get(key) for key in ("from", "to", "reason", "tried")}
    return None


# How many content ids the service remembers the page of, to say what to scrape again when one expires.
_CONTENT_URLS_KEPT = 256


def _lean_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A page's answer without its metadata blocks (``lean``): the text, and what is needed to
    trust and to continue it.

    Left out: description, canonical address, status, metadata, content hash, timings and cache
    fields, and the links unless some were asked for. Kept: url and title, the date and how far it
    is trusted, the Markdown, whether it
    was cut and where to read on (``next_offset``, ``content_id``), and, when they are the case,
    the conflicts of the date, a fetch that was not plain HTTP, the PDF page count, warnings, and
    everything that says the text was recovered another way and whose it is.
    """
    lean: dict[str, Any] = {
        key: payload[key]
        for key in (
            "url",
            "title",
            "date",
            "date_confidence",
            "markdown",
            "truncated",
            "total_chars",
            "offset",
            "next_offset",
            "content_id",
            "pages",
        )
        if key in payload
    }
    if payload.get("date_conflicts"):
        lean["date_conflicts"] = payload["date_conflicts"]
    if payload.get("links"):
        # Asked for by name (links, max_links): a lean answer has none otherwise.
        lean["links"] = payload["links"]
        lean["links_truncated"] = payload.get("links_truncated", False)
    if payload.get("fetched_with") not in (None, "http"):
        lean["fetched_with"] = payload["fetched_with"]
    recovered = payload.get("recovered_from")
    if recovered:
        lean["recovered_from"] = recovered
        if recovered.get("kind") == "other_source":
            lean["requested_url"] = payload.get("requested_url")
        if payload.get("alternates"):
            lean["alternates"] = payload["alternates"]
        lean["next_action"] = payload.get("next_action")
    if payload.get("warnings"):
        lean["warnings"] = payload["warnings"]
    return lean


def _date_fields(dates: Optional[DateResolution]) -> dict[str, Any]:
    """The date fields of a page's answer: no date is said, with a confidence of 0, never left out."""
    return (dates or DateResolution()).fields()


def _site_operators(sites: list[str], excluded: list[str]) -> str:
    """``site:`` operators for the provider's query: ``(site:a.com OR site:b.com) -site:c.com``."""
    parts: list[str] = []
    if len(sites) == 1:
        parts.append(f"site:{sites[0]}")
    elif sites:
        parts.append("(" + " OR ".join(f"site:{site}" for site in sites) + ")")
    parts.extend(f"-site:{site}" for site in excluded)
    return " ".join(parts)


def _parameters_key(options: SearchOptions, sites: list[str], excluded: list[str]) -> str:
    """The search parameters as part of a cache key; the order of the domains does not matter."""
    return "|".join(
        [
            options.region or "",
            options.language or "",
            ",".join(sorted(sites)),
            ",".join(sorted(excluded)),
            options.date_from.isoformat() if options.date_from else "",
            options.date_to.isoformat() if options.date_to else "",
        ]
    )


def _passes_filters(
    candidate: DiscoveryCandidate,
    sites: list[str],
    excluded: list[str],
    options: SearchOptions,
) -> bool:
    """The post-filters of a search: the providers' operators are asked for, not trusted."""
    result = candidate.result
    if sites and not url_on_domains(result.url, sites):
        return False
    if excluded and url_on_domains(result.url, excluded):
        return False
    if options.date_from or options.date_to:
        published, _ = infer_publication_date(result.title, result.snippet, result.url)
        return within_date_range(published, options.date_from, options.date_to)
    return True


def _for_cache(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as the cache keeps it: stamped with when it was made."""
    stored = dict(payload)
    stored["cached_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return stored


def _from_cache(cached: Mapping[str, Any], started: float) -> dict[str, Any]:
    """A cached payload with this call's own timings.

    The stages of the call that filled the cache (its network, search and
    ranking time) move under ``cached_timings``; ``timings`` holds the lookup
    and this call's real total, so a cache hit never reports the old cost.
    """
    payload = dict(cached)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    payload["cached_timings"] = dict(cached.get("timings") or {})
    payload["timings"] = {"cache_lookup_ms": elapsed_ms, "total_ms": elapsed_ms}
    payload["cache_hit"] = True
    return payload


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
