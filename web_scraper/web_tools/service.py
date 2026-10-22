"""Service layer for search, scrape, map, extract, and crawl jobs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
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
    DiscoveryCandidate,
    annotate_candidate,
    build_facets,
    build_search_angles,
    rank_candidates,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, HostGate, SafeFetcher
from web_scraper.web_tools.metrics import LatencyTracker, WebToolsMetrics
from web_scraper.web_tools.robots import RobotsPolicy
from web_scraper.web_tools.search import (
    DuckDuckGoSearchAdapter,
    GoogleSearchAdapter,
    SearchAdapter,
    SearchResult,
)
from web_scraper.web_tools.settings import WebToolsSettings
from web_scraper.web_tools.urls import (
    UnsafeUrlError,
    canonicalize_url,
    content_hash,
    matches_search_filter,
    matches_url_filters,
    normalize_request_url,
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
            "duckduckgo": DuckDuckGoSearchAdapter(),
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
        adapter = self.search_adapters.get(provider_name)
        if adapter is None:
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

        started = time.perf_counter()
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

        for index, angle in enumerate(angles):
            if index and self.settings.search_angle_delay_ms > 0:
                await asyncio.sleep(self.settings.search_angle_delay_ms / 1000)
            angle_started = time.perf_counter()
            try:
                raw_results = await adapter.search(angle.query, angle_limit)
            except WebToolError as exc:
                error: Optional[WebToolError] = exc
            except Exception as exc:  # noqa: BLE001 - provider boundary
                error = WebToolError(
                    "search_failed",
                    "Search provider failed",
                    detail=type(exc).__name__,
                )
            else:
                error = None
                contributed = 0
                for rank, item in enumerate(raw_results):
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
                        contributed += 1
                    else:
                        existing.angle_ranks.setdefault(angle.name, rank)
                        if len(item.snippet) > len(existing.result.snippet):
                            existing.result = SearchResult(
                                title=item.title or existing.result.title,
                                url=target,
                                snippet=item.snippet,
                                source=item.source or existing.result.source,
                            )
                query_reports.append(
                    {
                        "angle": angle.name,
                        "query": angle.query,
                        "results": len(raw_results),
                        "new": contributed,
                        "error": None,
                    }
                )
            finally:
                angle_ms = (time.perf_counter() - angle_started) * 1000
                angle_timings[angle.name] = round(angle_ms, 2)
                self.metrics.observe(
                    "search_angle",
                    angle_ms,
                    provider=provider_name,
                    angle=angle.name,
                )

            if error is not None:
                if first_error is None:
                    first_error = error
                query_reports.append(
                    {
                        "angle": angle.name,
                        "query": angle.query,
                        "results": 0,
                        "new": 0,
                        "error": error.to_dict(),
                    }
                )
                if angle.name != "base":
                    warnings.append(f"{angle.name}_query_failed")

        if not candidates:
            if first_error is not None:
                raise first_error
            raise WebToolError("search_failed", "Search provider returned no usable results")

        if any(
            report["angle"] == "base" and report["error"] is not None for report in query_reports
        ):
            warnings.append("base_query_failed")

        search_ms = (time.perf_counter() - started) * 1000
        ranking_started = time.perf_counter()
        if resolved_depth == "fast":
            results = [annotate_candidate(cleaned, candidate) for candidate in candidates.values()][
                :resolved_limit
            ]
        else:
            results = rank_candidates(cleaned, candidates.values(), resolved_limit)
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
        return {
            "query": cleaned,
            "provider": provider_name,
            "depth": resolved_depth,
            "results": results,
            "facets": facets,
            "queries": query_reports,
            "warnings": warnings,
            "timings": timings,
        }

    async def scrape(self, url: str, *, max_chars: Optional[int] = None) -> dict[str, Any]:
        requested = self._canonical(url)
        fetch_target = self._request_url(url)
        limit = self._markdown_limit(max_chars)
        cache_key = f"scrape:{requested}:{limit}"

        cache_started = time.perf_counter()
        cached = await self.cache.get(cache_key)
        if cached is not None:
            cache_ms = (time.perf_counter() - cache_started) * 1000
            self.metrics.count("cache_hits_total", operation="scrape")
            payload = dict(cached)
            payload["timings"] = dict(cached["timings"])
            payload["timings"]["cache_lookup_ms"] = round(cache_ms, 2)
            payload["cache_hit"] = True
            return payload

        started = time.perf_counter()
        document = await self._load_page(fetch_target, max_chars=limit)
        payload = self._page_payload(document, requested)
        total_ms = (time.perf_counter() - started) * 1000
        payload["timings"]["total_ms"] = round(total_ms, 2)
        self.metrics.observe("total", total_ms, operation="scrape")
        self.metrics.count("scrapes_total", mode=payload["fetched_with"])
        await self.cache.set(cache_key, payload)
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
        except Exception as exc:  # noqa: BLE001 - sitemap results may still be useful
            errors.append(self._safe_error(base, exc))

        entries.setdefault(base, {"url": base, "lastmod": None, "source": "base"})

        urls: list[str] = []
        for target, _entry in entries.items():
            if not url_in_scope(target, base, include_subdomains):
                continue
            if not matches_url_filters(target, includes, excludes):
                continue
            if search and not matches_search_filter(target, search):
                continue
            urls.append(target)
            if len(urls) >= max_urls:
                break

        self.metrics.count("maps_total")
        return {
            "url": base,
            "urls": urls,
            "total": len(urls),
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
        snapshot = await manager.create(base, resolved)
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
        document = await self._load_page(
            url,
            max_chars=self.settings.crawl_content_limit_chars,
        )
        return {
            "url": document.final_url,
            "title": document.page.title,
            "description": document.page.description,
            "markdown": document.page.markdown,
            "status_code": document.status_code,
            "content_hash": content_hash(document.page.markdown),
            "fetched_with": document.fetched_with,
            "links": [link.url for link in document.page.links],
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
                return await self._render_document(url, max_chars, warnings=["http_access_blocked"])
            raise self._translate_fetch_error(exc) from exc

        self.metrics.observe("http_fetch", fetched.total_ms, mode="http")
        self.metrics.observe("http_network", fetched.network_ms, mode="http")

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

        interstitial = detect_interstitial(rendered.html, rendered.final_url)
        if interstitial:
            raise WebToolError(
                "blocked",
                f"Target returned a {interstitial} interstitial",
                url=url,
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
        interstitial = detect_interstitial(rendered.html, rendered.final_url)
        if interstitial:
            raise WebToolError(
                "blocked",
                f"Target returned a {interstitial} interstitial",
                url=document.final_url,
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
        return {
            "url": document.final_url,
            "requested_url": requested,
            "title": document.page.title,
            "description": document.page.description,
            "canonical_url": document.page.canonical_url,
            "markdown": document.page.markdown,
            "status_code": document.status_code,
            "content_type": document.content_type,
            "links": [
                {"url": link.url, "text": link.text, "kind": link.kind}
                for link in document.page.links
            ],
            "metadata": document.page.metadata,
            "content_hash": content_hash(document.page.markdown),
            "fetched_with": document.fetched_with,
            "cache_hit": False,
            "warnings": list(document.warnings),
            "timings": dict(document.timings),
        }

    def _markdown_limit(self, value: Optional[int]) -> int:
        if value is None:
            return self.settings.markdown_max_chars
        return min(max(int(value), 500), self.settings.markdown_max_chars)

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


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
