"""Crawl results stay on https and hold each page once, whatever scheme reached it."""

from __future__ import annotations

import asyncio

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.crawl import canonical_target
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchResult
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings
from web_scraper.web_tools.urls import upgrade_to_https

SITE = "quotes.example"
QUOTES = "<p>The world as we have created it is a process of our thinking.</p>"


def _html(title: str, body: str, links: list[str]) -> str:
    anchors = "".join(f'<a href="{link}">link</a>' for link in links)
    return (
        f"<html><head><title>{title}</title></head><body><main>{body}{anchors}</main></body></html>"
    )


class RedirectingFetcher:
    """Serves pages by request URL; ``final`` fakes where redirects ended."""

    def __init__(self, pages: dict[str, tuple[str, str]], refuse_https: bool = False) -> None:
        self.pages = pages
        self.refuse_https = refuse_https
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if self.refuse_https and url.startswith("https://"):
            raise WebToolError("network_error", "Network error: ConnectError", url=url)
        if url not in self.pages:
            raise WebToolError("http_error", "not found", url=url, status_code=404)
        final, html = self.pages[url]
        return FetchResult(url, final, 200, "text/html", html, {}, 1.0, 2.0, 1)


def _service(fetcher: RedirectingFetcher) -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(
            respect_robots=False,
            crawl_concurrency=1,
            max_crawl_pages=20,
            max_crawl_depth=3,
        ),
        fetcher=fetcher,
        browser=None,
        search_adapters={},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


async def _crawl(service: WebToolsService, url: str) -> tuple[dict, dict]:
    started = await service.start_crawl(url)
    for _ in range(200):
        snapshot = await service.get_crawl(started["job_id"])
        if snapshot["status"] in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    results = await service.crawl_results(started["job_id"], limit=50)
    return snapshot, results


def test_canonical_target_upgrades_http_but_keeps_explicit_ports() -> None:
    assert canonical_target("http://quotes.example/") == "https://quotes.example/"
    assert canonical_target("https://quotes.example/a/") == "https://quotes.example/a"
    assert upgrade_to_https("http://quotes.example:8080/a") == "http://quotes.example:8080/a"


@pytest.mark.asyncio
async def test_downgrade_redirects_neither_leak_http_nor_duplicate_pages() -> None:
    # quotes.toscrape.com answers https://.../page/1 with 308 http://.../page/1/,
    # and page 1 is the home page again.
    home = _html(
        "Quotes", QUOTES, ["/page/1/", "/author/einstein", f"http://{SITE}/author/einstein"]
    )
    author = _html("Einstein", "<p>Born in 1879 in Ulm.</p>", ["/"])
    fetcher = RedirectingFetcher(
        {
            f"https://{SITE}/": (f"https://{SITE}/", home),
            f"https://{SITE}/page/1": (f"http://{SITE}/page/1/", home),
            f"https://{SITE}/author/einstein": (f"http://{SITE}/author/einstein/", author),
        }
    )

    snapshot, results = await _crawl(_service(fetcher), f"http://{SITE}/")

    urls = [item["url"] for item in results["results"]]
    assert snapshot["status"] == "completed"
    assert snapshot["url"] == f"https://{SITE}/"
    assert urls == [f"https://{SITE}/", f"https://{SITE}/author/einstein/"]
    assert not any(url.startswith("http://") for url in urls)
    # The redirected page is read as its https self: its relative links stay https.
    assert f"(https://{SITE}/)" in results["results"][1]["markdown"]
    assert f"(http://{SITE}/)" not in results["results"][1]["markdown"]
    assert len({item["content_hash"] for item in results["results"]}) == len(urls)
    assert results["duplicates"] == [
        {"url": f"https://{SITE}/page/1", "duplicate_of": f"https://{SITE}/"}
    ]
    assert fetcher.calls.count(f"https://{SITE}/author/einstein") == 1
    assert not any(call.startswith("http://") for call in fetcher.calls)


@pytest.mark.asyncio
async def test_host_without_https_is_crawled_over_http() -> None:
    home = _html("Old site", "<p>Plain HTTP only.</p>", [f"http://{SITE}/about"])
    about = _html("About", "<p>About this old site.</p>", ["/"])
    fetcher = RedirectingFetcher(
        {
            f"http://{SITE}/": (f"http://{SITE}/", home),
            f"http://{SITE}/about": (f"http://{SITE}/about", about),
        },
        refuse_https=True,
    )

    snapshot, results = await _crawl(_service(fetcher), f"http://{SITE}/")

    assert snapshot["status"] == "completed"
    assert [item["url"] for item in results["results"]] == [
        f"http://{SITE}/",
        f"http://{SITE}/about",
    ]
    assert results["errors"] == []
    # https was tried once for the seed; later links went straight to http.
    assert [call for call in fetcher.calls if call.startswith("https://")] == [f"https://{SITE}/"]
