"""Several pages in one call: web_scrape takes urls (2 to 10) in place of url."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_web_service import _html_result, _service

from web_scraper.api import create_app
from web_scraper.api_models import WebScrapeRequest
from web_scraper.config import config
from web_scraper.mcp_server import _scrape, _tools
from web_scraper.web_tools.budget import json_size
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult
from web_scraper.web_tools.settings import WebToolsSettings


def _article(url: str, chars: int = 300) -> FetchResult:
    body = ("A sentence of the page. " * (chars // 24 + 1))[:chars]
    html = f"<html><head><title>{url}</title></head><body><main><h1>Page</h1><p>{body}</p></main></body></html>"
    return _html_result(url, html)


class TrackingFetcher:
    """Serves pages after a delay and records how many fetches were in flight."""

    def __init__(
        self,
        pages: dict[str, FetchResult | Exception],
        *,
        delays: Optional[dict[str, float]] = None,
        delay: float = 0.0,
    ) -> None:
        self.pages = pages
        self.delays = delays or {}
        self.delay = delay
        self.calls: list[str] = []
        self.in_flight = 0
        self.peak = 0
        self._by_host: dict[str, int] = defaultdict(int)
        self.peak_by_host: dict[str, int] = defaultdict(int)

    async def fetch(self, url: str) -> FetchResult:
        host = urlsplit(url).hostname or ""
        self.calls.append(url)
        self.in_flight += 1
        self._by_host[host] += 1
        self.peak = max(self.peak, self.in_flight)
        self.peak_by_host[host] = max(self.peak_by_host[host], self._by_host[host])
        try:
            await asyncio.sleep(self.delays.get(url, self.delay))
            value = self.pages.get(url)
            if value is None:
                raise FetchError("http_error", "not found", url=url, status_code=404)
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.in_flight -= 1
            self._by_host[host] -= 1


def _urls(count: int, *, hosts: int = 0) -> list[str]:
    if hosts:
        return [f"https://host{n % hosts}.example/page-{n}" for n in range(count)]
    return [f"https://site{n}.example/page" for n in range(count)]


def _service_for(fetcher: TrackingFetcher, **settings: Any):
    values = {"search_provider": "fake", **settings}
    return _service(fetcher, settings=WebToolsSettings(**values))  # type: ignore[arg-type]


class TestRequest:
    def test_urls_in_place_of_url(self) -> None:
        request = WebScrapeRequest(urls=["example.com/a", "https://example.com/b"])

        assert request.url is None
        assert request.urls == ["https://example.com/a", "https://example.com/b"]

    @pytest.mark.parametrize(
        "bad",
        [
            {},
            {"url": "https://a.example/", "urls": ["https://b.example/", "https://c.example/"]},
            {"urls": ["https://a.example/"]},
            {"urls": [f"https://s{n}.example/" for n in range(11)]},
            {"urls": ["https://a.example/", "x"]},
            {"urls": ["https://a.example/", "https://b.example/"], "max_total_chars": 10},
        ],
    )
    def test_exactly_one_of_them_and_two_to_ten_pages(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            WebScrapeRequest(**bad)

    def test_ten_pages_and_the_single_form_are_accepted(self) -> None:
        assert len(WebScrapeRequest(urls=_urls(10)).urls or []) == 10
        assert WebScrapeRequest(url="https://a.example/").urls is None

    def test_the_schema_keeps_url_a_string_and_bounds_urls(self) -> None:
        schema = WebScrapeRequest.model_json_schema()
        properties = schema["properties"]

        assert properties["url"]["type"] == "string"
        assert (properties["urls"]["minItems"], properties["urls"]["maxItems"]) == (2, 10)
        assert "url" not in schema.get("required", [])
        assert "anyOf" not in schema and "oneOf" not in schema  # no model takes these on top
        assert "max_total_chars" in properties

    def test_the_tool_description_says_what_urls_does(self) -> None:
        scrape = next(tool for tool in _tools() if tool["name"] == "web_scrape")

        assert "urls" in scrape["description"] and "max_total_chars" in scrape["description"]


class TestAnswer:
    @pytest.mark.asyncio
    async def test_the_pages_come_back_in_input_order(self) -> None:
        urls = _urls(4)
        fetcher = TrackingFetcher(
            {url: _article(url) for url in urls},
            delays={urls[0]: 0.12, urls[1]: 0.08, urls[2]: 0.04, urls[3]: 0.0},
        )

        result = await _service_for(fetcher).scrape_many(urls)

        assert set(result) == {"pages", "timings"}
        assert [page["url"] for page in result["pages"]] == urls
        assert all("markdown" in page for page in result["pages"])
        assert result["timings"]["total_ms"] > 0

    @pytest.mark.asyncio
    async def test_a_failed_page_never_fails_the_others(self) -> None:
        urls = _urls(4)
        pages: dict[str, FetchResult | Exception] = {
            urls[0]: _article(urls[0]),
            urls[2]: FetchError(
                "blocked",
                "Target returned a captcha interstitial",
                url=urls[2],
                status_code=401,
                detail="captcha",
            ),
            urls[3]: _article(urls[3]),
        }  # urls[1] is not there: a 404

        result = await _service_for(TrackingFetcher(pages)).scrape_many(urls)

        first, missing, walled, last = result["pages"]
        assert "markdown" in first and "markdown" in last
        assert set(missing) == {"url", "error"} and missing["url"] == urls[1]
        assert missing["error"]["code"] == "http_error"
        assert missing["error"]["status_code"] == 404
        assert set(walled) == {"url", "error"}
        assert walled["error"]["code"] == "blocked" and walled["error"]["detail"] == "captcha"
        # The envelope keeps its hint, and says what to do next: the page was tried every way.
        assert "walled" in walled["error"]["hint"] and walled["error"]["next_action"]
        assert walled["error"]["alternates"] == []

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_is_an_internal_error_entry(self) -> None:
        urls = _urls(2)
        pages: dict[str, FetchResult | Exception] = {
            urls[0]: RuntimeError("boom"),
            urls[1]: _article(urls[1]),
        }

        result = await _service_for(TrackingFetcher(pages)).scrape_many(urls)

        assert result["pages"][0]["error"]["code"] == "internal_error"
        assert "markdown" in result["pages"][1]

    @pytest.mark.asyncio
    async def test_links_archive_and_max_chars_apply_to_every_page(self) -> None:
        urls = _urls(2)
        service = _service_for(TrackingFetcher({url: _article(url, 3000) for url in urls}))

        result = await service.scrape_many(urls, links=False, max_chars=500)

        for page in result["pages"]:
            assert page["links"] == [] and page["links_truncated"] is False
            assert len(page["markdown"]) <= 500

    @pytest.mark.asyncio
    async def test_the_single_url_answer_is_unchanged(self) -> None:
        url = "https://site0.example/page"
        service = _service_for(TrackingFetcher({url: _article(url)}))

        single = await service.scrape(url)

        assert "markdown_truncated" not in single and "pages" not in single
        assert single["markdown"].startswith("# Page")

    @pytest.mark.asyncio
    async def test_the_count_is_checked_by_the_service_too(self) -> None:
        service = _service_for(TrackingFetcher({}))

        for urls in ([], ["https://a.example/"], _urls(11)):
            with pytest.raises(WebToolError) as exc:
                await service.scrape_many(urls)
            assert exc.value.code == "invalid_request"


class TestLimits:
    @pytest.mark.asyncio
    async def test_four_pages_at_a_time_by_default(self) -> None:
        urls = _urls(10)
        fetcher = TrackingFetcher({url: _article(url) for url in urls}, delay=0.05)

        await _service_for(fetcher).scrape_many(urls)

        assert 2 <= fetcher.peak <= 4
        assert fetcher.peak == 4

    @pytest.mark.asyncio
    async def test_the_concurrency_is_a_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEB_TOOLS_SCRAPE_BATCH_CONCURRENCY", raising=False)
        assert WebToolsSettings.from_env().scrape_batch_concurrency == 4
        monkeypatch.setenv("WEB_TOOLS_SCRAPE_BATCH_CONCURRENCY", "2")
        assert WebToolsSettings.from_env().scrape_batch_concurrency == 2

        urls = _urls(8)
        fetcher = TrackingFetcher({url: _article(url) for url in urls}, delay=0.05)
        await _service_for(fetcher, scrape_batch_concurrency=2).scrape_many(urls)

        assert fetcher.peak == 2

    @pytest.mark.asyncio
    async def test_two_at_a_time_per_host(self) -> None:
        urls = _urls(8, hosts=1)
        fetcher = TrackingFetcher({url: _article(url) for url in urls}, delay=0.05)

        await _service_for(fetcher).scrape_many(urls)

        assert fetcher.peak_by_host["host0.example"] == 2
        assert fetcher.peak == 2

    @pytest.mark.asyncio
    async def test_two_hosts_share_the_four(self) -> None:
        urls = _urls(8, hosts=2)
        fetcher = TrackingFetcher({url: _article(url) for url in urls}, delay=0.05)

        await _service_for(fetcher).scrape_many(urls)

        assert max(fetcher.peak_by_host.values()) == 2
        assert fetcher.peak == 4

    @pytest.mark.asyncio
    async def test_the_whole_answer_stays_under_the_total_and_each_cut_says_so(self) -> None:
        urls = _urls(5)
        pages = {url: _article(url, 30_000) for url in urls[:3]}
        pages[urls[3]] = _article(urls[3], 400)
        pages[urls[4]] = FetchError("http_error", "gone", url=urls[4], status_code=410)
        service = _service_for(TrackingFetcher(pages))

        result = await service.scrape_many(urls, max_total_chars=40_000)

        assert json_size(result) <= 40_000
        big = result["pages"][:3]
        assert all(page["markdown_truncated"] is True for page in big)
        assert all(page["markdown"].endswith("…") for page in big)
        lengths = [len(page["markdown"]) for page in big]
        assert max(lengths) - min(lengths) <= 2  # the cut is shared, not taken from one page
        assert result["pages"][3]["markdown_truncated"] is False
        assert len(result["pages"][3]["markdown"]) > 300
        assert "error" in result["pages"][4]

    @pytest.mark.asyncio
    async def test_sixty_thousand_is_the_default_total(self) -> None:
        urls = _urls(4)
        service = _service_for(TrackingFetcher({url: _article(url, 25_000) for url in urls}))

        result = await service.scrape_many(urls)

        assert WebToolsSettings().scrape_batch_max_total_chars == 60_000
        assert json_size(result) <= 60_000
        assert all(page["markdown_truncated"] for page in result["pages"])

    @pytest.mark.asyncio
    async def test_pages_that_fit_are_not_touched(self) -> None:
        urls = _urls(3)
        service = _service_for(TrackingFetcher({url: _article(url, 2000) for url in urls}))

        result = await service.scrape_many(urls)

        assert [page["markdown_truncated"] for page in result["pages"]] == [False] * 3
        assert all(len(page["markdown"]) > 1900 for page in result["pages"])

    @pytest.mark.asyncio
    async def test_links_and_metadata_give_way_when_the_markdown_cannot(self) -> None:
        urls = _urls(3)
        anchors = "".join(f'<a href="/p/{n}">Link number {n} of the page</a>' for n in range(50))
        html = f"<html><body><main><p>Short text of a page here.</p>{anchors}</main></body></html>"
        service = _service_for(TrackingFetcher({url: _html_result(url, html) for url in urls}))

        result = await service.scrape_many(urls, max_total_chars=5_000)

        assert json_size(result) <= 5_000
        assert all(page["links"] == [] and page["links_truncated"] for page in result["pages"])

    @pytest.mark.asyncio
    async def test_a_cut_never_reaches_the_cache(self) -> None:
        urls = _urls(2)
        service = _service_for(TrackingFetcher({url: _article(url, 30_000) for url in urls}))

        batch = await service.scrape_many(urls, max_total_chars=10_000)
        single = await service.scrape(urls[0])

        assert batch["pages"][0]["markdown_truncated"] is True
        assert len(single["markdown"]) > 20_000
        assert "markdown_truncated" not in single
        assert single["cache_hit"] is True

    @pytest.mark.asyncio
    async def test_each_page_has_its_cache_entry(self) -> None:
        urls = _urls(3)
        fetcher = TrackingFetcher({url: _article(url) for url in urls})
        service = _service_for(fetcher)

        await service.scrape_many(urls)
        again = await service.scrape_many(urls)

        assert len(fetcher.calls) == 3
        assert all(page["cache_hit"] for page in again["pages"])


class TestTools:
    @pytest.mark.asyncio
    async def test_the_mcp_tool_takes_urls(self) -> None:
        urls = _urls(3)
        service = _service_for(TrackingFetcher({url: _article(url) for url in urls}))

        result = await _scrape(service, {"urls": urls, "links": False})

        assert [page["url"] for page in result["pages"]] == urls
        with pytest.raises(ValidationError):
            await _scrape(service, {"urls": urls, "url": urls[0]})
        with pytest.raises(ValidationError):
            await _scrape(service, {})

    def test_the_rest_route_takes_urls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        urls = _urls(3)
        for name, value in (
            ("api_keys", ["test-key"]),
            ("api_rate_limit_per_minute", 100),
            ("api_web_rate_limit_per_minute", 100),
            ("api_allowed_origins", []),
            ("api_trusted_hosts", ["testserver"]),
            ("history_db_path", None),
        ):
            monkeypatch.setattr(config, name, value, raising=False)
        app = create_app(config)
        pages: dict[str, FetchResult | Exception] = {url: _article(url) for url in urls[:2]}
        app.state.web_tools = _service_for(TrackingFetcher(pages))
        client = TestClient(app)
        headers = {"X-API-Key": "test-key"}

        response = client.post("/api/v1/web/scrape", headers=headers, json={"urls": urls})
        single = client.post("/api/v1/web/scrape", headers=headers, json={"url": urls[0]})
        both = client.post(
            "/api/v1/web/scrape", headers=headers, json={"url": urls[0], "urls": urls}
        )

        assert response.status_code == 200
        body = response.json()
        assert [page["url"] for page in body["pages"]] == urls
        assert body["pages"][0]["markdown"].startswith("# Page")
        assert body["pages"][2]["error"]["code"] == "http_error"
        assert single.status_code == 200 and single.json()["markdown"].startswith("# Page")
        assert both.status_code == 422
        assert json.dumps(body)  # serialisable as a whole
