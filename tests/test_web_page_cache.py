"""A short-lived document cache at the fetch layer: scrape and extract of one page fetch it once."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_web_service import ARTICLE_HTML, SHELL_HTML, FakeBrowser, FakeFetcher, _html_result

from web_scraper.api_models import WebExtractResponse
from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.browser import BrowserPage
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.pagecache import CachedDocument, PageCache
from web_scraper.web_tools.service import WebToolsService, _cdx_url
from web_scraper.web_tools.settings import WebToolsSettings

URL = "https://example.com/article"
SCHEMA = {"title": "h1", "intro": "main p"}
LONG_HTML = (
    "<html><head><title>Long</title></head><body><main><h1>Long read</h1><p>"
    + ("A sentence of the long article body. " * 400)
    + "</p></main></body></html>"
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(
    fetcher: FakeFetcher,
    *,
    cache: PageCache | None = None,
    browser: FakeBrowser | None = None,
    **settings: Any,
) -> WebToolsService:
    # These tests count fetches; the missing-page check's probe has a file of its own.
    settings.setdefault("soft_404_check", False)
    return WebToolsService(
        settings=WebToolsSettings(search_provider="fake", **settings),
        fetcher=fetcher,  # type: ignore[arg-type]
        browser=browser,  # type: ignore[arg-type]
        search_adapters={},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=50),
        latency=LatencyTracker(),
        page_cache=cache,
    )


def _entries(service: WebToolsService) -> int:
    return service._pages.snapshot()["entries"]


class TestOneFetch:
    @pytest.mark.asyncio
    async def test_scrape_then_extract_fetches_once(self) -> None:
        fetcher = FakeFetcher({URL: _html_result(URL, ARTICLE_HTML, network_ms=180.0)})
        service = _service(fetcher)

        scraped = await service.scrape(URL)
        extracted = await service.extract(URL, SCHEMA)

        assert fetcher.calls == [URL]
        assert scraped["cache_hit"] is False
        assert extracted["cache_hit"] is True
        assert extracted["data"]["title"] == "Useful article"
        # The timings do not pretend to fetch: no network, and the stored fetch is named.
        assert extracted["timings"]["network_ms"] == 0.0
        assert extracted["timings"]["http_ms"] == 0.0
        assert extracted["timings"]["browser_ms"] == 0.0
        assert extracted["cached_timings"]["network_ms"] == 180.0

    @pytest.mark.asyncio
    async def test_extract_then_scrape_fetches_once(self) -> None:
        fetcher = FakeFetcher({URL: _html_result(URL, ARTICLE_HTML, network_ms=180.0)})
        service = _service(fetcher)

        first = await service.extract(URL, SCHEMA)
        scraped = await service.scrape(URL)

        assert fetcher.calls == [URL]
        assert first["cache_hit"] is False and first["timings"]["network_ms"] == 180.0
        assert scraped["markdown"].startswith("# Useful article")
        assert scraped["cache_hit"] is False  # the answer was made now, from the stored page
        assert scraped["timings"]["network_ms"] == 0.0 and scraped["timings"]["http_ms"] == 0.0

    @pytest.mark.asyncio
    async def test_each_call_reads_the_stored_page_with_its_own_cap(self) -> None:
        fetcher = FakeFetcher({URL: _html_result(URL, LONG_HTML)})
        service = _service(fetcher)

        short = await service.scrape(URL, max_chars=500)
        long = await service.scrape(URL, max_chars=5_000)

        assert fetcher.calls == [URL]
        assert len(short["markdown"]) <= 500 and len(long["markdown"]) > 4_000

    @pytest.mark.asyncio
    async def test_the_page_is_keyed_by_its_canonical_url(self) -> None:
        fetcher = FakeFetcher({URL: _html_result(URL, ARTICLE_HTML)})
        service = _service(fetcher)

        await service.scrape(URL)
        again = await service.extract(URL + "#top", SCHEMA)

        assert fetcher.calls == [URL] and again["cache_hit"] is True

    @pytest.mark.asyncio
    async def test_a_browser_rendered_page_is_stored_as_rendered(self) -> None:
        rendered = (
            "<html><head><title>Rendered</title></head><body><main><h1>Rendered title</h1><p>"
            + ("Rendered content. " * 30)
            + "</p></main></body></html>"
        )
        fetcher = FakeFetcher({URL: _html_result(URL, SHELL_HTML)})
        browser = FakeBrowser({URL: BrowserPage(rendered, URL, 200, "chromium")})
        service = _service(fetcher, browser=browser)

        scraped = await service.scrape(URL)
        extracted = await service.extract(URL, {"title": "h1"})

        assert scraped["fetched_with"] == "browser"
        assert extracted["data"]["title"] == "Rendered title"
        assert extracted["cache_hit"] is True and extracted["fetched_with"] == "browser"
        assert fetcher.calls == [URL] and browser.calls == [URL]

    @pytest.mark.asyncio
    async def test_extraction_answers_the_same_from_the_stored_page(self) -> None:
        service = _service(FakeFetcher({URL: _html_result(URL, ARTICLE_HTML)}))

        first = await service.extract(URL, SCHEMA)
        second = await service.extract(URL, SCHEMA)

        assert second["cache_hit"] is True
        assert second["data"] == first["data"] and second["provenance"] == first["provenance"]

    def test_the_extract_response_model_says_cache_hit(self) -> None:
        answer = WebExtractResponse(
            url=URL,
            requested_url=URL,
            title="T",
            description="",
            canonical_url=URL,
            status_code=200,
            data={},
            provenance={},
            errors=[],
            fetched_with="http",
            timings={"total_ms": 1.0},
            cache_hit=True,
            cached_timings={"network_ms": 3.0},
        )

        assert answer.model_dump()["cache_hit"] is True
        assert WebExtractResponse.model_json_schema()["properties"]["cache_hit"]


class TestLifetime:
    @pytest.mark.asyncio
    async def test_it_lives_for_the_ttl(self) -> None:
        clock = Clock()
        cache = PageCache(ttl_seconds=300, max_entries=8, max_bytes=1_000_000, clock=clock)
        fetcher = FakeFetcher({URL: _html_result(URL, ARTICLE_HTML)})
        service = _service(fetcher, cache=cache)

        await service.extract(URL, SCHEMA)
        clock.now += 299
        await service.extract(URL, SCHEMA)
        assert fetcher.calls == [URL]

        clock.now += 2  # 301 s since it was stored
        await service.extract(URL, SCHEMA)
        assert fetcher.calls == [URL, URL]

    def test_the_ttl_is_a_setting_of_300_seconds_and_zero_turns_it_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WEB_TOOLS_PAGE_CACHE_TTL_SECONDS", raising=False)
        assert WebToolsSettings().page_cache_ttl_seconds == 300
        assert WebToolsSettings.from_env().page_cache_ttl_seconds == 300
        monkeypatch.setenv("WEB_TOOLS_PAGE_CACHE_TTL_SECONDS", "45")
        assert WebToolsSettings.from_env().page_cache_ttl_seconds == 45

    @pytest.mark.asyncio
    async def test_a_ttl_of_zero_caches_nothing(self) -> None:
        fetcher = FakeFetcher({URL: _html_result(URL, ARTICLE_HTML)})
        service = _service(fetcher, page_cache_ttl_seconds=0)

        await service.extract(URL, SCHEMA)
        await service.extract(URL, SCHEMA)

        assert fetcher.calls == [URL, URL] and _entries(service) == 0


class TestBounds:
    def _doc(self, size: int) -> CachedDocument:
        return CachedDocument(
            final_url=URL,
            status_code=200,
            content_type="text/html",
            html="x" * size,
            fetched_with="http",
            warnings=(),
            timings={},
        )

    def test_it_is_bounded_in_count_oldest_out_first(self) -> None:
        clock = Clock()
        cache = PageCache(ttl_seconds=300, max_entries=2, max_bytes=1_000_000, clock=clock)

        for name in ("a", "b", "c"):
            clock.now += 1
            assert cache.put(name, self._doc(10)) is True

        assert cache.get("a") is None
        assert cache.get("b") is not None and cache.get("c") is not None

    def test_a_hit_makes_an_entry_the_youngest(self) -> None:
        cache = PageCache(ttl_seconds=300, max_entries=2, max_bytes=1_000_000, clock=Clock())
        cache.put("a", self._doc(10))
        cache.put("b", self._doc(10))
        cache.get("a")

        cache.put("c", self._doc(10))

        assert cache.get("a") is not None and cache.get("b") is None

    def test_it_is_bounded_in_bytes(self) -> None:
        cache = PageCache(ttl_seconds=300, max_entries=50, max_bytes=2_500, clock=Clock())

        cache.put("a", self._doc(1_000))
        cache.put("b", self._doc(1_000))
        cache.put("c", self._doc(1_000))  # 3,000 bytes: the oldest goes

        assert cache.get("a") is None
        assert cache.snapshot()["bytes"] <= 2_500 and cache.snapshot()["entries"] == 2

    def test_a_page_larger_than_the_whole_budget_is_not_kept(self) -> None:
        cache = PageCache(ttl_seconds=300, max_entries=50, max_bytes=2_500, clock=Clock())
        cache.put("a", self._doc(1_000))

        kept = cache.put("big", self._doc(3_000))

        assert kept is False and cache.get("big") is None
        assert cache.get("a") is not None  # and it did not push the others out

    def test_the_bounds_are_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = WebToolsSettings()
        assert (settings.page_cache_max_entries, settings.page_cache_max_bytes) == (64, 20_000_000)
        monkeypatch.setenv("WEB_TOOLS_PAGE_CACHE_MAX_ENTRIES", "5")
        monkeypatch.setenv("WEB_TOOLS_PAGE_CACHE_MAX_BYTES", "123456")
        loaded = WebToolsSettings.from_env()
        assert (loaded.page_cache_max_entries, loaded.page_cache_max_bytes) == (5, 123_456)

    @pytest.mark.asyncio
    async def test_the_service_keeps_to_them(self) -> None:
        urls = [f"https://example.com/page-{n}" for n in range(3)]
        fetcher = FakeFetcher({url: _html_result(url, ARTICLE_HTML) for url in urls})
        service = _service(fetcher, page_cache_max_entries=2)

        for url in urls:
            await service.extract(url, SCHEMA)
        await service.extract(urls[0], SCHEMA)  # the first was pushed out: fetched again
        await service.extract(urls[2], SCHEMA)  # the last is still there

        assert fetcher.calls == [*urls, urls[0]]
        assert _entries(service) == 2


class TestNeverCached:
    @pytest.mark.asyncio
    async def test_an_error_is_not_kept_and_the_next_call_tries_again(self) -> None:
        fetcher = FakeFetcher({URL: FetchError("http_error", "boom", url=URL, status_code=503)})
        service = _service(fetcher)

        with pytest.raises(WebToolError):
            await service.extract(URL, SCHEMA)
        fetcher.pages[URL] = _html_result(URL, ARTICLE_HTML)
        recovered = await service.extract(URL, SCHEMA)
        await service.extract(URL, SCHEMA)

        assert recovered["data"]["title"] == "Useful article"
        assert fetcher.calls == [URL, URL]  # a failure, then one success that is kept

    @pytest.mark.asyncio
    async def test_a_challenge_page_is_not_kept(self) -> None:
        wall = FetchError(
            "blocked", "Target returned a captcha interstitial", url=URL, detail="captcha"
        )
        fetcher = FakeFetcher({URL: wall})
        service = _service(fetcher)

        for _ in range(2):
            with pytest.raises(WebToolError):
                await service.extract(URL, SCHEMA)

        assert fetcher.calls == [URL, URL] and _entries(service) == 0

    @pytest.mark.asyncio
    async def test_a_page_with_nothing_to_read_is_not_kept(self) -> None:
        empty = "<html><head><title>x</title></head><body></body></html>"
        fetcher = FakeFetcher({URL: _html_result(URL, empty)})
        service = _service(fetcher)

        await service.extract(URL, {"title": "title"})
        await service.extract(URL, {"title": "title"})

        assert fetcher.calls == [URL, URL] and _entries(service) == 0

    @pytest.mark.asyncio
    async def test_a_failed_browser_render_is_not_kept(self) -> None:
        fetcher = FakeFetcher({URL: _html_result(URL, SHELL_HTML)})
        browser = FakeBrowser({URL: RuntimeError("boom")})
        service = _service(fetcher, browser=browser)

        await service.scrape(URL)

        assert _entries(service) == 0

    @pytest.mark.asyncio
    async def test_an_archived_copy_is_not_kept(self) -> None:
        stamp = "20261021093000"
        snapshot = f"https://web.archive.org/web/{stamp}id_/{URL}"
        rows = [["timestamp", "original", "statuscode", "digest"], [stamp, URL, "200", "D"]]
        cdx = _cdx_url(URL)
        fetcher = FakeFetcher(
            {
                URL: FetchError("blocked", "captcha", url=URL, status_code=401, detail="captcha"),
                cdx: FetchResult(cdx, cdx, 200, "application/json", json.dumps(rows)),
                snapshot: _html_result(snapshot, ARTICLE_HTML),
            }
        )
        service = _service(fetcher)

        archived = await service.scrape(URL, archive=True)

        assert archived["fetched_with"] == "archive"
        assert _entries(service) == 0
        with pytest.raises(WebToolError):  # the live page is still walled: no copy answers for it
            await service.extract(URL, SCHEMA)

    @pytest.mark.asyncio
    async def test_a_pdf_is_not_kept(self) -> None:
        pdf = FetchResult(URL, URL, 200, "application/pdf", "", content=b"%PDF-1.4 not a real pdf")
        service = _service(FakeFetcher({URL: pdf}))

        with pytest.raises(WebToolError):
            await service.scrape(URL)

        assert _entries(service) == 0


@pytest.mark.asyncio
async def test_the_stats_show_the_page_cache() -> None:
    service = _service(FakeFetcher({URL: _html_result(URL, ARTICLE_HTML)}))
    await service.extract(URL, SCHEMA)
    await service.extract(URL, SCHEMA)

    snapshot = await service.metrics_snapshot()

    assert snapshot["page_cache"]["entries"] == 1
    assert snapshot["page_cache"]["hits"] == 1 and snapshot["page_cache"]["misses"] == 1
    assert snapshot["page_cache"]["ttl_seconds"] == 300
