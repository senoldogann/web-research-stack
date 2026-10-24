"""web_search can read its top results: read (0 to 5), read_chars, content and content_error."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_web_api import HEADERS, FakeWebTools
from test_web_scrape_batch import TrackingFetcher, _article
from test_web_service import FakeSearch, _service

from web_scraper.api import create_app
from web_scraper.api_models import WebSearchRequest, WebSearchResponse
from web_scraper.config import config
from web_scraper.mcp_server import _search, _tools
from web_scraper.web_tools.budget import json_size
from web_scraper.web_tools.fetcher import FetchError, FetchResult
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService, _cdx_url
from web_scraper.web_tools.settings import WebToolsSettings

QUERY = "python asyncio tutorial"
URLS = [f"https://site{n}.example/asyncio-tutorial" for n in range(6)]


def _results(count: int = 6) -> list[SearchResult]:
    return [
        SearchResult(f"Python asyncio tutorial {n}", URLS[n], "A tutorial of asyncio in Python.")
        for n in range(count)
    ]


def _build(
    pages: dict[str, FetchResult | Exception], **settings: Any
) -> tuple[WebToolsService, TrackingFetcher, FakeSearch]:
    fetcher = TrackingFetcher(pages)
    search = FakeSearch(_results())
    values = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        **settings,
    }
    service = _service(fetcher, search=search, settings=WebToolsSettings(**values))  # type: ignore[arg-type]
    return service, fetcher, search


def _all_pages(chars: int = 600) -> dict[str, FetchResult | Exception]:
    return {url: _article(url, chars) for url in URLS}


class TestRequest:
    def test_the_defaults_read_nothing(self) -> None:
        request = WebSearchRequest(query=QUERY)

        assert request.read == 0 and request.read_chars is None and request.archive is False

    @pytest.mark.parametrize(
        "bad", [{"read": 6}, {"read": -1}, {"read_chars": 499}, {"read_chars": 20_001}]
    )
    def test_the_bounds(self, bad: dict[str, int]) -> None:
        with pytest.raises(ValidationError):
            WebSearchRequest(query=QUERY, **bad)

    def test_the_edges_are_accepted(self) -> None:
        request = WebSearchRequest(query=QUERY, read=5, read_chars=20_000)

        assert (request.read, request.read_chars) == (5, 20_000)

    def test_the_description_says_that_reading_costs_seconds(self) -> None:
        search = next(tool for tool in _tools() if tool["name"] == "web_search")
        read = search["inputSchema"]["properties"]["read"]

        assert "read" in search["description"] and "seconds" in search["description"]
        assert "seconds" in read["description"]
        assert read["maximum"] == 5 and read["minimum"] == 0


class TestAnswer:
    @pytest.mark.asyncio
    async def test_read_zero_keeps_todays_answer_exactly(self) -> None:
        service, fetcher, _ = _build(_all_pages())

        plain = await service.search(QUERY, depth="fast")
        zero = await service.search(QUERY, depth="fast", read=0)

        assert fetcher.calls == []
        assert all(
            not {"content", "content_error", "content_truncated"} & set(r) for r in plain["results"]
        )
        assert set(plain["timings"]) == {"search_ms", "ranking_ms", "total_ms", "angle_base_ms"}
        assert zero["results"] == plain["results"]

    @pytest.mark.asyncio
    async def test_the_top_n_results_are_read_in_order(self) -> None:
        service, fetcher, _ = _build(_all_pages())

        answer = await service.search(QUERY, depth="fast", limit=5, read=3)

        results = answer["results"]
        assert [r["url"] for r in results] == URLS[:5]  # the order of the search is kept
        assert sorted(fetcher.calls) == sorted(URLS[:3])
        for item in results[:3]:
            assert item["content"].startswith("# Page")
            assert "content_error" not in item
        for item in results[3:]:
            assert not {"content", "content_error", "content_truncated"} & set(item)
        assert answer["timings"]["read_ms"] > 0
        assert answer["timings"]["total_ms"] >= answer["timings"]["read_ms"]

    @pytest.mark.asyncio
    async def test_a_page_that_fails_is_a_content_error_and_the_rest_are_read(self) -> None:
        pages = _all_pages()
        pages[URLS[1]] = FetchError("http_error", "gone", url=URLS[1], status_code=404)
        pages[URLS[2]] = FetchError(
            "blocked",
            "Target returned a captcha interstitial",
            url=URLS[2],
            status_code=401,
            detail="captcha",
        )
        service, _, _ = _build(pages)

        answer = await service.search(QUERY, depth="fast", read=3)

        first, gone, walled = answer["results"][:3]
        assert "content" in first and "content_error" not in first
        assert "content" not in gone and gone["content_error"]["code"] == "http_error"
        assert gone["content_error"]["status_code"] == 404
        assert walled["content_error"]["code"] == "blocked"
        assert "archive: true" in walled["content_error"]["hint"]

    @pytest.mark.asyncio
    async def test_a_walled_page_is_not_retried_from_the_archive_unless_asked(self) -> None:
        stamp = "20261021093000"
        snapshot = f"https://web.archive.org/web/{stamp}id_/{URLS[0]}"
        rows = [["timestamp", "original", "statuscode", "digest"], [stamp, URLS[0], "200", "D"]]
        pages: dict[str, FetchResult | Exception] = {
            URLS[0]: FetchError(
                "blocked", "captcha", url=URLS[0], status_code=401, detail="captcha"
            ),
            _cdx_url(URLS[0]): FetchResult(
                _cdx_url(URLS[0]), _cdx_url(URLS[0]), 200, "application/json", json.dumps(rows)
            ),
            snapshot: _article(snapshot),
        }
        service, fetcher, _ = _build(pages)

        without = await service.search(QUERY, depth="fast", limit=1, read=1)
        assert without["results"][0]["content_error"]["code"] == "blocked"
        assert fetcher.calls == [URLS[0]]

        with_archive = await service.search(
            QUERY + " a", depth="fast", limit=1, read=1, archive=True
        )
        content = with_archive["results"][0]["content"]
        # The archive's copy says it is the archive's before it says anything of the page.
        assert content.startswith("> Recovered from the Internet Archive's copy of ")
        assert "\n\n# Page" in content
        assert snapshot in fetcher.calls

    @pytest.mark.asyncio
    async def test_read_chars_bounds_each_content_and_four_thousand_is_the_default(self) -> None:
        service, _, _ = _build({url: _article(url, 10_000) for url in URLS})

        default = await service.search(QUERY, depth="fast", read=2)
        small = await service.search(QUERY + " b", depth="fast", read=2, read_chars=600)

        assert [len(r["content"]) for r in default["results"][:2]] == [4000, 4000]
        assert default["results"][0]["content_truncated"] is True
        assert [len(r["content"]) for r in small["results"][:2]] == [600, 600]

    @pytest.mark.asyncio
    async def test_a_short_page_is_whole_and_not_truncated(self) -> None:
        service, _, _ = _build(_all_pages(700))

        answer = await service.search(QUERY, depth="fast", read=1)

        assert 600 < len(answer["results"][0]["content"]) < 800
        assert answer["results"][0]["content_truncated"] is False

    @pytest.mark.asyncio
    async def test_the_whole_answer_stays_bounded(self) -> None:
        service, _, _ = _build({url: _article(url, 30_000) for url in URLS})

        answer = await service.search(QUERY, depth="fast", limit=6, read=5, read_chars=20_000)

        assert json_size(answer) <= 60_000
        read = answer["results"][:5]
        assert all(item["content_truncated"] is True for item in read)
        assert all(item["content"].endswith("…") for item in read)
        lengths = [len(item["content"]) for item in read]
        assert max(lengths) - min(lengths) <= 2

    @pytest.mark.asyncio
    async def test_the_bound_is_a_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEB_TOOLS_SEARCH_READ_MAX_TOTAL_CHARS", raising=False)
        assert WebToolsSettings.from_env().search_read_max_total_chars == 60_000
        monkeypatch.setenv("WEB_TOOLS_SEARCH_READ_MAX_TOTAL_CHARS", "25000")
        assert WebToolsSettings.from_env().search_read_max_total_chars == 25_000


class TestCache:
    @pytest.mark.asyncio
    async def test_the_search_is_cached_without_the_reading_and_pages_are_read_once(self) -> None:
        service, fetcher, search = _build(_all_pages())

        first = await service.search(QUERY, depth="fast", read=2)
        again = await service.search(QUERY, depth="fast", read=2)
        wider = await service.search(QUERY, depth="fast", read=3)

        assert first["cache_hit"] is False and again["cache_hit"] is True
        assert len(search.calls) == 1  # the provider was asked once, whatever read said
        assert sorted(fetcher.calls) == sorted(URLS[:3])  # each page fetched once
        assert [("content" in r) for r in again["results"][:3]] == [True, True, False]
        assert [("content" in r) for r in wider["results"][:4]] == [True, True, True, False]

    @pytest.mark.asyncio
    async def test_a_search_with_no_read_never_gets_what_another_read(self) -> None:
        service, _, _ = _build(_all_pages())

        await service.search(QUERY, depth="fast", read=3)
        plain = await service.search(QUERY, depth="fast")

        assert plain["cache_hit"] is True
        assert all("content" not in item for item in plain["results"])
        assert "read_ms" not in plain["timings"]

    @pytest.mark.asyncio
    async def test_another_read_chars_is_another_reading_of_the_one_fetched_page(self) -> None:
        service, fetcher, _ = _build({url: _article(url, 10_000) for url in URLS})

        long = await service.search(QUERY, depth="fast", read=1, read_chars=8000)
        short = await service.search(QUERY, depth="fast", read=1, read_chars=1000)

        assert len(long["results"][0]["content"]) == 8000
        assert len(short["results"][0]["content"]) == 1000
        assert len(fetcher.calls) == 1  # the page cache holds the page, each read cuts its own


class TestTools:
    @pytest.mark.asyncio
    async def test_the_mcp_tool_takes_read(self) -> None:
        service, fetcher, _ = _build(_all_pages())

        answer = await _search(service, {"query": QUERY, "depth": "fast", "read": 2})

        assert [("content" in r) for r in answer["results"][:3]] == [True, True, False]
        assert len(fetcher.calls) == 2

    def test_the_rest_route_forwards_read_only_when_it_is_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        fake_tools = FakeWebTools()
        app.state.web_tools = fake_tools
        client = TestClient(app)
        headers = {**HEADERS, "X-API-Key": "test-key"}

        client.post("/api/v1/web/search", headers=headers, json={"query": "hello"})
        client.post(
            "/api/v1/web/search",
            headers=headers,
            json={"query": "hello", "read": 2, "read_chars": 1000, "archive": True},
        )

        assert "read" not in fake_tools.calls[0][1]
        assert fake_tools.calls[1][1]["read"] == 2
        assert fake_tools.calls[1][1]["read_chars"] == 1000
        assert fake_tools.calls[1][1]["archive"] is True

    def test_the_response_model_keeps_content_and_adds_nothing_when_there_is_none(self) -> None:
        base = {"title": "T", "url": "https://a.example/", "snippet": ""}
        read = {**base, "content": "# T", "content_truncated": False}
        failed = {**base, "content_error": {"code": "blocked", "hint": "try archive"}}

        answer = WebSearchResponse(
            query="q",
            provider="fake",
            results=[base, read, failed],
            timings={"total_ms": 1.0},
        ).model_dump()

        assert "content" not in answer["results"][0] and "content_error" not in answer["results"][0]
        assert answer["results"][1]["content"] == "# T"
        assert answer["results"][2]["content_error"]["code"] == "blocked"
        schema = WebSearchResponse.model_json_schema()["$defs"]["WebSearchResult"]["properties"]
        assert {"content", "content_truncated", "content_error"} <= set(schema)
