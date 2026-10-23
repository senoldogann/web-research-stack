"""Tests for the web tools service layer using in-process fakes."""

from __future__ import annotations

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.browser import BrowserPage
from web_scraper.web_tools.fetcher import FetchError, FetchResult
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

ARTICLE_HTML = """
<html><head><title>Example article</title>
<meta name="description" content="Article description">
</head><body><main><h1>Useful article</h1>
<p>This page explains the topic in enough depth to be useful.</p>
</main></body></html>
"""

SHELL_HTML = """
<html><body><div id="root"></div><script src="/app.js"></script></body></html>
"""

RENDERED_HTML = (
    """
<html><head><title>Rendered article</title></head><body><main>
<h1>Rendered title</h1><p>"""
    + ("Rendered content. " * 30)
    + """</p>
</main></body></html>
"""
)

PRODUCT_HTML = """
<html><head><title>Product</title>
<meta property="og:title" content="OG product">
<script type="application/ld+json">
{"@type":"Product","name":"Widget","offers":{"price":"19.99"}}
</script></head><body><main><h1>Widget</h1></main></body></html>
"""


class FakeFetcher:
    def __init__(self, pages: dict[str, FetchResult | Exception]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        value = self.pages.get(url)
        if value is None:
            raise FetchError("http_error", "not found", url=url, status_code=404)
        if isinstance(value, Exception):
            raise value
        return value


class FakeBrowser:
    def __init__(self, pages: dict[str, BrowserPage | Exception]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def render(self, url: str, *, post_load_wait_ms: int | None = None) -> BrowserPage:
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


class FakeSearch:
    name = "fake"

    def __init__(self, results: list[SearchResult] | Exception) -> None:
        self.results = results
        self.calls: list[tuple[str, int]] = []

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: str | None = None,
    ) -> list[SearchResult]:
        self.calls.append((query, limit))
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


def _html_result(url: str, html: str, *, network_ms: float = 12.0) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type="text/html",
        html=html,
        network_ms=network_ms,
        total_ms=network_ms + 1.0,
        attempts=1,
    )


def _service(
    fetcher: FakeFetcher,
    *,
    browser: FakeBrowser | None = None,
    search: FakeSearch | None = None,
    cache: InMemoryTTLCache | None = None,
    settings: WebToolsSettings | None = None,
) -> WebToolsService:
    resolved_settings = settings or WebToolsSettings(
        search_provider="fake",
        search_angle_delay_ms=0,
        search_jitter_min_ms=0,
        search_jitter_max_ms=0,
    )
    adapters = {search.name: search} if search is not None else {}
    return WebToolsService(
        settings=resolved_settings,
        fetcher=fetcher,  # type: ignore[arg-type]
        browser=browser,  # type: ignore[arg-type]
        search_adapters=adapters,
        cache=cache or InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


@pytest.mark.asyncio
async def test_scrape_returns_markdown_metadata_and_caches_by_url() -> None:
    url = "https://example.com/article"
    fetcher = FakeFetcher({url: _html_result(url, ARTICLE_HTML)})
    service = _service(fetcher)

    first = await service.scrape(url)
    second = await service.scrape(url)

    assert first["title"] == "Example article"
    assert first["markdown"].startswith("# Useful article")
    assert first["description"] == "Article description"
    assert first["canonical_url"] == url
    assert first["fetched_with"] == "http"
    assert first["cache_hit"] is False
    assert first["content_hash"]
    assert first["timings"]["network_ms"] == 12.0
    assert first["timings"]["extract_ms"] >= 0
    assert second["cache_hit"] is True
    assert len(fetcher.calls) == 1


@pytest.mark.asyncio
async def test_scrape_uses_headless_browser_only_for_script_shells() -> None:
    url = "https://example.com/app"
    fetcher = FakeFetcher({url: _html_result(url, SHELL_HTML)})
    browser = FakeBrowser({url: BrowserPage(RENDERED_HTML, url, 200, "chromium")})
    service = _service(fetcher, browser=browser)

    result = await service.scrape(url)

    assert result["fetched_with"] == "browser"
    assert result["title"] == "Rendered article"
    assert result["markdown"].startswith("# Rendered title")
    assert browser.calls == [url]
    assert result["timings"]["browser_ms"] >= 0


@pytest.mark.asyncio
async def test_scrape_does_not_open_a_browser_for_captcha() -> None:
    url = "https://example.com/blocked"
    fetcher = FakeFetcher(
        {
            url: FetchError(
                "blocked",
                "Target returned a captcha interstitial",
                url=url,
                status_code=200,
                detail="captcha",
            )
        }
    )
    browser = FakeBrowser({})
    service = _service(fetcher, browser=browser)

    from web_scraper.web_tools.errors import WebToolError

    with pytest.raises(WebToolError) as exc:
        await service.scrape(url)

    assert exc.value.code == "blocked"
    assert browser.calls == []


@pytest.mark.asyncio
async def test_browser_failure_keeps_partial_http_content_with_warning() -> None:
    url = "https://example.com/app"
    fetcher = FakeFetcher({url: _html_result(url, SHELL_HTML)})
    browser = FakeBrowser({url: RuntimeError("boom")})
    service = _service(fetcher, browser=browser)

    result = await service.scrape(url)

    assert result["fetched_with"] == "http"
    assert any("browser" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_search_canonicalises_deduplicates_and_drops_unsafe_results() -> None:
    search = FakeSearch(
        [
            SearchResult("One", "https://EXAMPLE.com/a/", "first query", "example"),
            SearchResult("Duplicate", "https://example.com/a", "dup query", "example"),
            SearchResult("Unsafe", "http://127.0.0.1/admin", "bad query", "local"),
            SearchResult("Two", "https://example.com/b", "second query", "example"),
        ]
    )
    service = _service(FakeFetcher({}), search=search)

    result = await service.search("query", limit=5, depth="fast")

    assert result["provider"] == "fake"
    assert [item["url"] for item in result["results"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert search.calls == [("query", 5)]


@pytest.mark.asyncio
async def test_ttl_cache_honours_a_per_entry_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(
        "web_scraper.api_runtime.time.monotonic",
        lambda: now[0],
    )
    cache = InMemoryTTLCache(ttl_seconds=60, max_entries=4)
    await cache.set("short", "value", ttl_seconds=5)

    now[0] += 6.0

    assert await cache.get("short") is None


@pytest.mark.asyncio
async def test_search_cache_returns_repeat_query_without_provider_call() -> None:
    search = FakeSearch([SearchResult("One", "https://example.com/a", "asyncio", "example")])
    service = _service(FakeFetcher({}), search=search)

    first = await service.search("asyncio", limit=5, depth="fast")
    second = await service.search("asyncio", limit=5, depth="fast")

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert search.calls == [("asyncio", 5)]
    assert second["timings"]["cache_lookup_ms"] >= 0


@pytest.mark.asyncio
async def test_search_cache_is_scoped_by_depth() -> None:
    search = FakeSearch([SearchResult("One", "https://example.com/a", "asyncio", "example")])
    service = _service(FakeFetcher({}), search=search)

    await service.search("asyncio", limit=5, depth="fast")
    await service.search("asyncio", limit=5, depth="discover")

    assert len(search.calls) == 4


@pytest.mark.asyncio
async def test_extract_returns_values_provenance_and_page_metadata() -> None:
    url = "https://example.com/product"
    fetcher = FakeFetcher({url: _html_result(url, PRODUCT_HTML)})
    service = _service(fetcher)

    result = await service.extract(
        url,
        {
            "name": {"type": "string"},
            "price": {"type": "number"},
            "heading": "h1",
        },
    )

    assert result["data"]["name"] == "Widget"
    assert result["data"]["price"] == 19.99
    assert result["data"]["heading"] == "Widget"
    assert result["provenance"]["name"]["method"] == "json_ld"
    assert result["title"] == "Product"
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_map_combines_sitemaps_and_page_links_with_scope_filters() -> None:
    base = "https://example.com/"
    robots = _html_result(
        base + "robots.txt", "User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
    )
    sitemap = FetchResult(
        url=base + "sitemap.xml",
        final_url=base + "sitemap.xml",
        status_code=200,
        content_type="application/xml",
        html="""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/from-sitemap</loc><lastmod>2026-01-01</lastmod></url>
        <url><loc>https://other.example.net/out</loc></url>
        </urlset>""",
        network_ms=3.0,
        total_ms=4.0,
        attempts=1,
    )
    page = _html_result(
        base,
        '<html><body><main><a href="/from-link">Link</a>'
        '<a href="https://other.example.net/x">Out</a></main></body></html>',
    )
    fetcher = FakeFetcher(
        {
            base + "robots.txt": robots,
            base + "sitemap.xml": sitemap,
            base: page,
        }
    )
    service = _service(fetcher)

    result = await service.map(base, limit=50)

    assert "https://example.com/from-sitemap" in result["urls"]
    assert "https://example.com/from-link" in result["urls"]
    assert base in result["urls"]
    assert "https://other.example.net/out" not in result["urls"]
    assert result["sitemaps"] == ["https://example.com/sitemap.xml"]


@pytest.mark.asyncio
async def test_map_applies_search_filter_and_limit() -> None:
    base = "https://example.com/"
    page = _html_result(
        base,
        "<html><body><main>"
        '<a href="/docs/a">A</a><a href="/docs/b">B</a><a href="/blog/c">C</a>'
        "</main></body></html>",
    )
    fetcher = FakeFetcher(
        {
            base + "robots.txt": _html_result(base + "robots.txt", ""),
            base + "sitemap.xml": FetchError("http_error", "no sitemap", status_code=404),
            base: page,
        }
    )
    service = _service(fetcher)

    result = await service.map(base, search="docs", limit=1)

    assert result["urls"] == ["https://example.com/docs/a"]
    # total counts every match the limit cut; returned counts what came back.
    assert result["total"] == 2
    assert result["returned"] == 1
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_map_of_an_unreachable_site_lists_nothing() -> None:
    base = "https://this-domain-definitely-does-not-exist-xyz123.com/"
    unresolved = FetchError(
        "dns_error",
        "Could not resolve host this-domain-definitely-does-not-exist-xyz123.com",
        detail="this-domain-definitely-does-not-exist-xyz123.com",
    )
    fetcher = FakeFetcher(
        {
            base + "robots.txt": unresolved,
            base + "sitemap.xml": unresolved,
            base: unresolved,
        }
    )
    service = _service(fetcher)

    result = await service.map(base)

    assert result["urls"] == []
    assert result["entries"] == []
    assert result["total"] == 0
    assert result["returned"] == 0
    assert result["truncated"] is False
    assert [error["code"] for error in result["errors"]] == ["dns_error"] * 3
    assert [error["url"] for error in result["errors"]] == [
        base + "robots.txt",
        base + "sitemap.xml",
        base,
    ]


@pytest.mark.asyncio
async def test_map_lists_the_base_only_once_its_page_was_read() -> None:
    base = "https://example.com/missing"
    sitemap = FetchResult(
        url="https://example.com/sitemap.xml",
        final_url="https://example.com/sitemap.xml",
        status_code=200,
        content_type="application/xml",
        html="""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/missing/child</loc></url>
        </urlset>""",
    )
    fetcher = FakeFetcher(
        {
            "https://example.com/robots.txt": _html_result("https://example.com/robots.txt", ""),
            "https://example.com/sitemap.xml": sitemap,
        }
    )
    service = _service(fetcher)

    result = await service.map(base)

    assert result["urls"] == ["https://example.com/missing/child"]
    assert result["total"] == 1
    assert [error["status_code"] for error in result["errors"]] == [404]


def test_latency_tracker_reports_p50_and_p95() -> None:
    tracker = LatencyTracker()
    for value in (10.0, 20.0, 30.0, 40.0, 100.0):
        tracker.record("search", value)

    snapshot = tracker.snapshot()

    assert snapshot["search"]["count"] == 5
    assert snapshot["search"]["p50_ms"] == 30.0
    assert snapshot["search"]["p95_ms"] == 100.0
    assert snapshot["search"]["max_ms"] == 100.0


def test_settings_read_web_tool_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_TOOLS_FETCH_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("WEB_TOOLS_BROWSER_PREFERENCE", "camoufox")
    monkeypatch.setenv("WEB_TOOLS_MAX_CRAWL_PAGES", "42")
    monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setenv("WEB_TOOLS_SEARCH_DEPTH", "fast")
    monkeypatch.setenv("WEB_TOOLS_SEARCH_ANGLE_DELAY_MS", "0")

    settings = WebToolsSettings.from_env()

    assert settings.fetch_timeout_seconds == 7.5
    assert settings.browser_preference == "camoufox"
    assert settings.max_crawl_pages == 42
    assert settings.search_provider == "duckduckgo"
    assert settings.search_default_depth == "fast"
    assert settings.search_angle_delay_ms == 0


def test_settings_read_search_locale_cache_and_cooldown_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_TOOLS_SEARCH_REGION", "tr-tr")
    monkeypatch.setenv("WEB_TOOLS_SEARCH_LANGUAGE", "tr-TR,tr;q=0.9")
    monkeypatch.setenv("WEB_TOOLS_SEARCH_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER_COOLDOWN_SECONDS", "10")
    monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS", "60")

    settings = WebToolsSettings.from_env()

    assert settings.search_region == "tr-tr"
    assert settings.search_language == "tr-TR,tr;q=0.9"
    assert settings.search_cache_ttl_seconds == 120
    assert settings.search_provider_cooldown_seconds == 10
    assert settings.search_provider_max_cooldown_seconds == 60


def test_settings_fall_back_to_discover_for_unknown_depth() -> None:
    settings = WebToolsSettings(search_default_depth="turbo")

    assert settings.search_default_depth == "discover"
