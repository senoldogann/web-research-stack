"""Tests for background crawl jobs, robots handling, and cancellation."""

from __future__ import annotations

import asyncio

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.crawl import CrawlOptions
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchResult
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_PRIVATE = "User-agent: *\nDisallow: /private\n"


class MapFetcher:
    def __init__(self, pages: dict[str, FetchResult | Exception], delay: float = 0.0) -> None:
        self.pages = pages
        self.delay = delay
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        value = self.pages.get(url)
        if value is None:
            raise WebToolError("http_error", "not found", url=url, status_code=404)
        if isinstance(value, Exception):
            raise value
        return value


def _page(url: str, body: str, links: list[str]) -> FetchResult:
    anchors = "".join(f'<a href="{link}">link</a>' for link in links)
    html = f"<html><head><title>{url}</title></head><body><main><p>{body}</p>{anchors}</main></body></html>"
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type="text/html",
        html=html,
        network_ms=1.0,
        total_ms=2.0,
        attempts=1,
    )


def _service(fetcher: MapFetcher, **settings_overrides) -> WebToolsService:
    overrides = {"crawl_concurrency": 2, **settings_overrides}
    settings = WebToolsSettings(search_provider="duckduckgo", **overrides)
    return WebToolsService(
        settings=settings,
        fetcher=fetcher,  # type: ignore[arg-type]
        browser=None,
        search_adapters={},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


async def _wait_for_status(service: WebToolsService, job_id: str, expected: set[str]) -> dict:
    for _ in range(100):
        snapshot = await service.get_crawl(job_id)
        if snapshot["status"] in expected:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"crawl did not reach {expected}; last={snapshot}")


@pytest.mark.asyncio
async def test_crawl_returns_immediately_and_deduplicates_by_content_hash() -> None:
    base = "https://example.com/"
    robots = FetchResult(
        url=base + "robots.txt",
        final_url=base + "robots.txt",
        status_code=200,
        content_type="text/plain",
        html=ROBOTS_ALLOW,
        network_ms=1,
        total_ms=1,
        attempts=1,
    )
    pages = {
        base + "robots.txt": robots,
        base: _page(base, "root content", ["/a", "/b"]),
        base + "a": _page(base + "a", "same content", ["/c"]),
        base + "b": _page(base + "b", "same content", ["/c"]),
        base + "c": _page(base + "c", "third content", []),
    }
    fetcher = MapFetcher(pages)
    service = _service(
        fetcher,
        max_crawl_pages=10,
        max_crawl_depth=2,
        crawl_concurrency=1,
    )

    started = await service.start_crawl(base)
    assert started["status"] in {"queued", "running"}
    assert started["job_id"]

    snapshot = await _wait_for_status(service, started["job_id"], {"completed", "failed"})

    assert snapshot["status"] == "completed"
    assert snapshot["progress"]["fetched"] == 3
    assert snapshot["progress"]["duplicates"] == 1
    assert snapshot["progress"]["skipped"] == 0
    results = await service.crawl_results(started["job_id"], limit=10)
    urls = [item["url"] for item in results["results"]]
    assert sorted(urls) == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/c",
    ]
    assert results["total"] == 3


@pytest.mark.asyncio
async def test_crawl_respects_depth_and_page_limits() -> None:
    base = "https://example.com/"
    pages = {
        base + "robots.txt": FetchResult(
            base + "robots.txt", base + "robots.txt", 200, "text/plain", ROBOTS_ALLOW, {}, 1, 1, 1
        ),
        base: _page(base, "root", ["/a"]),
        base + "a": _page(base + "a", "a", ["/b"]),
        base + "b": _page(base + "b", "b", []),
    }
    service = _service(MapFetcher(pages), max_crawl_pages=2, max_crawl_depth=5)

    started = await service.start_crawl(base)
    snapshot = await _wait_for_status(service, started["job_id"], {"completed", "failed"})
    results = await service.crawl_results(started["job_id"], limit=10)

    assert snapshot["progress"]["fetched"] == 2
    assert results["total"] == 2
    assert "https://example.com/b" not in [item["url"] for item in results["results"]]


@pytest.mark.asyncio
async def test_crawl_respects_robots_disallow() -> None:
    base = "https://example.com/"
    pages = {
        base + "robots.txt": FetchResult(
            base + "robots.txt",
            base + "robots.txt",
            200,
            "text/plain",
            ROBOTS_PRIVATE,
            {},
            1,
            1,
            1,
        ),
        base: _page(base, "root", ["/private/x", "/public"]),
        base + "public": _page(base + "public", "public", []),
        base + "private/x": _page(base + "private/x", "private", []),
    }
    service = _service(MapFetcher(pages), respect_robots=True)

    started = await service.start_crawl(base)
    await _wait_for_status(service, started["job_id"], {"completed", "failed"})
    results = await service.crawl_results(started["job_id"], limit=10)
    urls = [item["url"] for item in results["results"]]

    assert "https://example.com/public" in urls
    assert "https://example.com/private/x" not in urls


@pytest.mark.asyncio
async def test_crawl_results_are_paginated() -> None:
    base = "https://example.com/"
    pages = {
        base + "robots.txt": FetchResult(
            base + "robots.txt", base + "robots.txt", 200, "text/plain", ROBOTS_ALLOW, {}, 1, 1, 1
        ),
        base: _page(base, "root", ["/a", "/b"]),
        base + "a": _page(base + "a", "a", []),
        base + "b": _page(base + "b", "b", []),
    }
    service = _service(MapFetcher(pages))

    started = await service.start_crawl(base)
    await _wait_for_status(service, started["job_id"], {"completed", "failed"})
    first = await service.crawl_results(started["job_id"], offset=0, limit=2)
    second = await service.crawl_results(started["job_id"], offset=2, limit=2)

    assert len(first["results"]) == 2
    assert first["total"] == 3
    assert len(second["results"]) == 1


@pytest.mark.asyncio
async def test_crawl_can_be_cancelled_while_fetching() -> None:
    base = "https://example.com/"
    fetcher = MapFetcher(
        {
            base + "robots.txt": FetchResult(
                base + "robots.txt",
                base + "robots.txt",
                200,
                "text/plain",
                ROBOTS_ALLOW,
                {},
                1,
                1,
                1,
            ),
            base: _page(base, "root", []),
        },
        delay=1.0,
    )
    service = _service(fetcher)

    started = await service.start_crawl(base)
    await asyncio.sleep(0.05)
    cancelled = await service.cancel_crawl(started["job_id"])
    snapshot = await _wait_for_status(service, started["job_id"], {"cancelled", "completed"})

    assert cancelled["status"] in {"cancelling", "cancelled"}
    assert snapshot["status"] == "cancelled"


@pytest.mark.asyncio
async def test_unknown_job_raises_job_not_found() -> None:
    service = _service(MapFetcher({}))

    with pytest.raises(WebToolError) as exc:
        await service.get_crawl("missing")

    assert exc.value.code == "job_not_found"


@pytest.mark.asyncio
async def test_crawl_options_are_clamped() -> None:
    options = CrawlOptions.from_dict(
        {"max_pages": 10_000, "max_depth": 100},
        settings=WebToolsSettings(max_crawl_pages=100, max_crawl_depth=4),
    )

    assert options.max_pages == 100
    assert options.max_depth == 4
