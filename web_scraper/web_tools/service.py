"""Service layer for search, scrape, map, extract, and crawl jobs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from web_scraper.api_runtime import InMemoryTTLCache, MetricsRegistry
from web_scraper.web_tools.browser import (
    BrowserPage,
    BrowserRenderError,
    BrowserUnavailableError,
    HeadlessBrowserManager,
)
from web_scraper.web_tools.content import (
    ParsedPage,
    detect_interstitial,
    extract_page,
    extract_structured,
    needs_browser_render,
    parse_sitemap_xml,
)
from web_scraper.web_tools.crawl import CrawlManager, CrawlOptions, canonical_target
from web_scraper.web_tools.discovery import (
    DEPTHS,
    MIN_RESULT_RELEVANCE,
    DiscoveryCandidate,
    annotate_candidates,
    build_facets,
    build_search_angles,
    has_relevant_result,
    rank_candidates,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult, HostGate, SafeFetcher
from web_scraper.web_tools.metrics import LatencyTracker, WebToolsMetrics
from web_scraper.web_tools.pdf import PDF_CONTENT_TYPE, PdfError, extract_pdf
from web_scraper.web_tools.providers import ProviderGate, ProviderHealth
from web_scraper.web_tools.robots import RobotsPolicy
from web_scraper.web_tools.search import (
    DuckDuckGoSearchAdapter,
    GoogleSearchAdapter,
    SearchAdapter,
    SearchResult,
)
from web_scraper.web_tools.settings import MIN_MARKDOWN_CHARS, WebToolsSettings
from web_scraper.web_tools.urls import (
    UnsafeUrlError,
    canonicalize_url,
    content_hash,
    matches_search_filter,
    matches_url_filters,
    normalize_request_url,
    upgrade_to_https,
    url_in_scope,
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
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher
        self.browser = browser
        self.search_adapters = dict(search_adapters or {})
        self.cache = cache or InMemoryTTLCache(
            ttl_seconds=settings.cache_ttl_seconds,
            max_entries=settings.cache_max_entries,
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

        cache_key = (
            f"search:{provider_name}:{resolved_depth}:{resolved_limit}:"
            f"{self.settings.search_region}:{self.settings.search_language}:"
            f"{cleaned.casefold()}"
        )
        started = time.perf_counter()
        cached = await self.cache.get(cache_key)
        if cached is not None:
            self.metrics.count("cache_hits_total", operation="search")
            return _from_cache(cached, started)

        angles = build_search_angles(
            cleaned,
            resolved_depth,
            max_length=self.settings.max_query_length,
        )
        candidates: dict[str, DiscoveryCandidate] = {}
        query_reports: list[dict[str, Any]] = []
        angle_timings: dict[str, float] = {}
        warnings: list[str] = []
        first_error: Optional[WebToolError] = None
        answered_by: list[str] = []
        responded: list[str] = []
        fallback_answered = False

        for index, angle in enumerate(angles):
            if index and self.settings.search_angle_delay_ms > 0:
                await asyncio.sleep(self.settings.search_angle_delay_ms / 1000)
            angle_started = time.perf_counter()
            outcome = await self._search_angle(
                provider_name,
                angle.query,
                angle_limit,
                angle.date_filter,
            )
            angle_ms = (time.perf_counter() - angle_started) * 1000
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

        if any(
            report["angle"] == "base" and report["error"] is not None for report in query_reports
        ):
            warnings.append("base_query_failed")
        if fallback_answered:
            warnings.append("provider_fallback")

        search_ms = (time.perf_counter() - started) * 1000
        ranking_started = time.perf_counter()
        if resolved_depth == "fast":
            # Provider order, minus results that share nothing with the query.
            annotated = annotate_candidates(cleaned, candidates.values())
            relevant = [item for item in annotated if item["relevance"] > 0]
            results = relevant[:resolved_limit] if has_relevant_result(cleaned, relevant) else []
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
        await self.cache.set(
            cache_key,
            _for_cache(payload),
            ttl_seconds=self.settings.search_cache_ttl_seconds,
        )
        return payload

    async def _search_angle(
        self,
        primary: str,
        query: str,
        limit: int,
        date_filter: Optional[str] = None,
    ) -> _AngleOutcome:
        """Ask the primary provider; on any failure, ask the fallback once.

        A primary that is cooling down after a recent failure is skipped in
        favour of the fallback. If that fallback fails too, the primary is
        still asked: a cooldown only steers calls, it never fails a search.
        """
        fallback = self._fallback_for(primary)
        if fallback is None:
            try:
                results = await self._call_provider(primary, query, limit, date_filter)
            except WebToolError as exc:
                return _AngleOutcome(primary, error=exc)
            return _AngleOutcome(primary, results)

        primary_error: Optional[WebToolError] = None
        try:
            results = await self._call_provider(
                primary, query, limit, date_filter, yield_to=fallback
            )
        except _ProviderCoolingDown:
            reason = "provider_cooldown"
        except WebToolError as exc:
            if self._provider_health_for(fallback).cooling_down():
                return _AngleOutcome(primary, error=exc)
            primary_error, reason = exc, exc.detail or exc.code
        else:
            return _AngleOutcome(primary, results)

        record: dict[str, Any] = {"from": primary, "to": fallback, "reason": reason, "error": None}
        self.metrics.count("search_fallbacks_total", provider=fallback)
        try:
            # A cold headless browser (Google) can take longer than a plain HTTP search.
            results = await self._call_provider(
                fallback,
                query,
                limit,
                date_filter,
                timeout=self.settings.search_fallback_timeout_seconds,
            )
        except WebToolError as exc:
            record["error"] = exc.to_dict()
            if primary_error is not None:
                return _AngleOutcome(primary, error=primary_error, fallback=record)
            try:
                results = await self._call_provider(primary, query, limit, date_filter)
            except WebToolError as last:
                return _AngleOutcome(primary, error=last, fallback=record)
            return _AngleOutcome(primary, results, fallback=record)
        return _AngleOutcome(fallback, results, fallback=record)

    async def _call_provider(
        self,
        name: str,
        query: str,
        limit: int,
        date_filter: Optional[str] = None,
        *,
        yield_to: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> list[SearchResult]:
        """One paced provider call; failures update only this provider's health.

        With ``yield_to`` set, a provider that is cooling down raises
        ``_ProviderCoolingDown`` instead of being called, as long as the
        provider it would yield to is not cooling down itself.
        """
        adapter = self.search_adapters[name]
        health = self._provider_health_for(name)
        gate = self._provider_gate_for(name)

        def should_yield() -> bool:
            return (
                yield_to is not None
                and health.cooling_down()
                and not self._provider_health_for(yield_to).cooling_down()
            )

        if should_yield():
            raise _ProviderCoolingDown(name)
        async with gate.turn():
            # A call queued behind a failure re-checks before spending its turn.
            if should_yield():
                raise _ProviderCoolingDown(name)
            await gate.pace()
            try:
                results = await asyncio.wait_for(
                    adapter.search(query, limit, date_filter=date_filter),
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

    def _fallback_for(self, primary: str) -> Optional[str]:
        name = self.settings.search_fallback_provider
        if not name or name == primary or name not in self.search_adapters:
            return None
        return name

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

    async def scrape(self, url: str, *, max_chars: Optional[int] = None) -> dict[str, Any]:
        requested = self._canonical(url)
        fetch_target = self._request_url(url)
        limit = self._markdown_limit(max_chars)
        cache_key = f"scrape:{requested}:{limit}"

        started = time.perf_counter()
        cached = await self.cache.get(cache_key)
        if cached is not None:
            self.metrics.count("cache_hits_total", operation="scrape")
            return _from_cache(cached, started)

        document = await self._load_page(fetch_target, max_chars=limit)
        payload = self._page_payload(document, requested)
        total_ms = (time.perf_counter() - started) * 1000
        payload["timings"]["total_ms"] = round(total_ms, 2)
        self.metrics.observe("total", total_ms, operation="scrape")
        self.metrics.count("scrapes_total", mode=payload["fetched_with"])
        await self.cache.set(cache_key, _for_cache(payload))
        return payload

    async def extract(self, url: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        requested = self._canonical(url)
        fetch_target = self._request_url(url)
        validated = self._validate_schema(schema)
        document = await self._load_page(
            fetch_target,
            max_chars=self.settings.markdown_max_chars,
        )

        started = time.perf_counter()
        structured = extract_structured(document.html, document.final_url, validated)
        extract_ms = (time.perf_counter() - started) * 1000
        self.metrics.observe("extraction", extract_ms, operation="structured")
        total_ms = document.timings.get("total_ms", 0.0) + extract_ms
        self.metrics.observe("total", total_ms, operation="extract")
        self.metrics.count("extracts_total")

        timings = dict(document.timings)
        timings["extract_ms"] = round(extract_ms, 2)
        timings["total_ms"] = round(total_ms, 2)
        return {
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
            "timings": timings,
            "warnings": document.warnings,
        }

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
            )
        return self._crawl

    async def _crawl_page(self, url: str) -> dict[str, Any]:
        limit = self.settings.crawl_content_limit_chars
        document = await self._load_page(url, max_chars=limit)
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

    async def _load_page(self, url: str, *, max_chars: int) -> LoadedDocument:
        started = time.perf_counter()
        try:
            fetched = await self.fetcher.fetch(url)
        except FetchError as exc:
            if (
                exc.code == "blocked"
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

        if self.browser is not None and needs_browser_render(
            fetched.html,
            page.markdown,
            min_chars=self.settings.browser_render_min_chars,
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
                f"Target returned a {interstitial} interstitial",
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
                f"Target returned a {interstitial} interstitial",
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

    def _page_payload(self, document: LoadedDocument, requested: str) -> dict[str, Any]:
        # The page's own links, without menus, banners, footers, and sidebars,
        # capped; map and crawl still see every link.
        content_links = [link for link in document.page.links if not link.chrome]
        shown = content_links[: self.settings.scrape_max_links]
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
            "links_truncated": len(shown) < len(document.page.links),
            "metadata": document.page.metadata,
            "content_hash": content_hash(document.page.markdown),
            "fetched_with": document.fetched_with,
            "cache_hit": False,
            "warnings": list(document.warnings),
            "timings": dict(document.timings),
        }
        if document.pages is not None:
            payload["pages"] = document.pages
        return payload

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
