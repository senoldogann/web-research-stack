"""Service layer for search, scrape, map, extract, and crawl jobs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlencode, urlsplit

from web_scraper.api_runtime import InMemoryTTLCache, MetricsRegistry
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
from web_scraper.web_tools.discovery import (
    DEPTHS,
    MIN_RESULT_RELEVANCE,
    DiscoveryCandidate,
    SearchAngle,
    annotate_candidates,
    build_facets,
    build_search_angles,
    infer_publication_date,
    rank_candidates,
    relevant_results,
    within_date_range,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult, HostGate, SafeFetcher
from web_scraper.web_tools.metrics import LatencyTracker, WebToolsMetrics
from web_scraper.web_tools.pagecache import CachedDocument, PageCache
from web_scraper.web_tools.pdf import PDF_CONTENT_TYPE, PdfError, extract_pdf
from web_scraper.web_tools.providers import ProviderGate, ProviderHealth
from web_scraper.web_tools.robots import RobotsPolicy
from web_scraper.web_tools.search import (
    DuckDuckGoLiteSearchAdapter,
    DuckDuckGoSearchAdapter,
    GoogleSearchAdapter,
    SearchAdapter,
    SearchOptions,
    SearchResult,
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
class _AngleOutcome:
    """What one search angle got: who answered, and how a fallback went."""

    provider: str
    results: list[SearchResult] = field(default_factory=list)
    error: Optional[WebToolError] = None
    fallback: Optional[dict[str, Any]] = None


class _ProviderCoolingDown(Exception):
    """A provider skipped for its cooldown while a healthy fallback exists."""


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
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher
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
            "google": GoogleSearchAdapter(),
        }
        return cls(
            settings=settings,
            fetcher=fetcher,
            browser=browser,
            search_adapters=adapters,
            cache=cache,
            latency=latency,
            metrics_registry=metrics_registry,
        )

    async def close(self) -> None:
        if self._crawl is not None:
            await self._crawl.close()
        for adapter in self.search_adapters.values():
            close_adapter = getattr(adapter, "aclose", None)
            if close_adapter is not None:
                await close_adapter()
        close_fetcher = getattr(self.fetcher, "aclose", None)
        if close_fetcher is not None:
            await close_fetcher()
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
    ) -> dict[str, Any]:
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

        provider_name = (provider or self.settings.search_provider).strip().lower()
        if provider_name not in self.search_adapters:
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

        cache_key = (
            f"search:{provider_name}:{resolved_depth}:{resolved_limit}:"
            f"{self.settings.search_region}:{self.settings.search_language}:"
            f"{_parameters_key(options, site_list, excluded_list)}:"
            f"{cleaned.casefold()}"
        )
        started = time.perf_counter()
        cached = await self.cache.get(cache_key)
        if cached is not None:
            self.metrics.count("cache_hits_total", operation="search")
            answer = _from_cache(cached, started)
            await self._read_results(answer, reading, archive)
            return answer

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
                    warnings.append(f"{angle.name}_query_failed")
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
        ranking_started = time.perf_counter()
        if resolved_depth == "fast":
            # Provider order, minus results that hold no distinctive word of the query.
            annotated = annotate_candidates(cleaned, candidates.values())
            results = relevant_results(cleaned, annotated)[:resolved_limit]
        else:
            results = rank_candidates(cleaned, candidates.values(), resolved_limit)
        if not results:
            # The provider answered, but nothing it returned is about the query.
            warnings.append("no_relevant_results")
        elif all(item["relevance"] < MIN_RESULT_RELEVANCE for item in results):
            warnings.append("low_relevance_results")
        ranking_ms = (time.perf_counter() - ranking_started) * 1000
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
        # The search is cached as it is, without the reading: what is read is the pages' own
        # cache entries, and a search that reads no page never gets one's content.
        await self.cache.set(
            cache_key,
            _for_cache(payload),
            ttl_seconds=self.settings.search_cache_ttl_seconds,
        )
        await self._read_results(payload, reading, archive)
        return payload

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
        pages = await self._read_pages(
            [item["url"] for item in top],
            max_chars=chars,
            archive=archive,
            links=False,
            max_links=None,
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
        """The angles after the base, together, each cut once the angle budget ran out.

        A refused angle can wait out the provider's cooldown or start the headless
        Google fallback (seconds to tens of seconds), so the base's answer is not
        held for it. A cut angle is reported as a ``timeout`` with the detail
        ``angle_budget``; being the search's own limit, it marks no provider as
        failed.
        """
        if not angles:
            return []
        delay = self.settings.search_angle_delay_ms / 1000
        tasks = [
            asyncio.create_task(
                self._timed_angle(provider, angle, limit, stagger=delay * position, options=options)
            )
            for position, angle in enumerate(angles, start=1)
        ]
        budget = self.settings.search_angle_budget_ms / 1000
        try:
            _, pending = await asyncio.wait(tasks, timeout=budget or None)
        finally:
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        answered: list[tuple[_AngleOutcome, float]] = []
        for task in tasks:
            if task in pending:
                cut = WebToolError(
                    "timeout",
                    "The angle ran past the search's time budget",
                    detail="angle_budget",
                )
                answered.append((_AngleOutcome(provider, error=cut), budget * 1000))
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
        except _ProviderCoolingDown:
            reason = "provider_cooldown"
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

        def should_yield() -> bool:
            return (
                bool(yield_to)
                and health.cooling_down()
                and any(not self._provider_health_for(name).cooling_down() for name in yield_to)
            )

        if should_yield():
            raise _ProviderCoolingDown(name)
        async with gate.turn():
            # A call queued behind a failure re-checks before spending its turn.
            if should_yield():
                raise _ProviderCoolingDown(name)
            await gate.pace()
            # An adapter is given the options only when a caller asked for some: with none, the
            # call is what it always was.
            extra: dict[str, Any] = {"options": options} if options else {}
            try:
                results = await asyncio.wait_for(
                    adapter.search(query, limit, date_filter=date_filter, **extra),
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
                failure = WebToolError(
                    "search_failed",
                    "Search provider failed",
                    detail=type(exc).__name__,
                )
            else:
                health.record_success()
                return results
            finally:
                gate.finish()

        health.record_failure(
            self.settings.search_provider_cooldown_seconds,
            self.settings.search_provider_max_cooldown_seconds,
        )
        self.metrics.count("search_provider_failures_total", provider=name)
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
            for name in self.settings.search_fallback_providers
            if name != primary and name in self.search_adapters
        ]
        if fast:
            names.sort(
                key=lambda name: bool(getattr(self.search_adapters[name], "headless", False))
            )
        return names

    def _provider_gate_for(self, name: str) -> ProviderGate:
        gate = self._provider_gates.get(name)
        if gate is None:
            gate = ProviderGate(
                self.settings.search_provider_concurrency,
                self.settings.search_jitter_min_ms,
                self.settings.search_jitter_max_ms,
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
    ) -> dict[str, Any]:
        requested = self._canonical(url)
        fetch_target = self._request_url(url)
        limit = self._markdown_limit(max_chars)
        link_cap = self._links_limit(max_links)
        cache_key = (
            f"scrape:{requested}:{limit}"
            + (":archive" if archive else "")
            + (f":links{link_cap}" if links else ":nolinks")
        )

        started = time.perf_counter()
        cached = await self.cache.get(cache_key)
        if cached is not None:
            self.metrics.count("cache_hits_total", operation="scrape")
            return _from_cache(cached, started)

        archived: Optional[tuple[str, str]] = None
        skipped: list[dict[str, str]] = []
        try:
            document = await self._load_page(fetch_target, max_chars=limit)
        except WebToolError as exc:
            if exc.code not in _ARCHIVABLE:
                raise
            if not archive:
                exc.hint = _ARCHIVE_HINT
                raise
            document, archived, skipped = await self._archived_copy(fetch_target, limit, exc)
        payload = self._page_payload(document, requested, links=links, max_links=link_cap)
        if archived is not None:
            payload["archived_at"], payload["archive_url"] = archived
            payload["archive_skipped"] = skipped
        total_ms = (time.perf_counter() - started) * 1000
        payload["timings"]["total_ms"] = round(total_ms, 2)
        self.metrics.observe("total", total_ms, operation="scrape")
        self.metrics.count("scrapes_total", mode=payload["fetched_with"])
        await self.cache.set(cache_key, _for_cache(payload))
        return payload

    async def scrape_many(
        self,
        urls: Sequence[str],
        *,
        max_chars: Optional[int] = None,
        archive: bool = False,
        links: bool = True,
        max_links: Optional[int] = None,
        max_total_chars: Optional[int] = None,
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
            targets, max_chars=max_chars, archive=archive, links=links, max_links=max_links
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
        try:
            answer = await asyncio.wait_for(
                self.fetcher.fetch(_cdx_url(url)), timeout=_remaining(deadline)
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
                    self._load_page(snapshot, max_chars=limit, browser=False, cache=False),
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
            self._crawl = CrawlManager(
                self._crawl_page,
                self.settings,
                robots=robots,
                seeder=self._sitemap_seeds,
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
        self, url: str, *, max_chars: int, browser: bool = True, cache: bool = True
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
        document = await self._fetch_document(url, max_chars=max_chars, browser=browser)
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
        self, stored: CachedDocument, max_chars: int, started: float
    ) -> LoadedDocument:
        """A stored document read again: parsed for this call, its timings this call's own."""
        parsing = time.perf_counter()
        page = extract_page(
            stored.html,
            stored.final_url,
            max_links=self.settings.max_links,
            max_chars=max_chars,
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
        self, url: str, *, max_chars: int, browser: bool = True
    ) -> LoadedDocument:
        """Fetch and read a page; ``browser=False`` never opens a headless browser for it."""
        started = time.perf_counter()
        try:
            fetched = await self.fetcher.fetch(url)
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
        if document.pages is not None:
            payload["pages"] = document.pages
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
