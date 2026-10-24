"""Compact output: a search a model can afford, a lean scrape, the rest of a cut page by content_id."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_web_api import HEADERS, FakeWebTools
from test_web_service import FakeFetcher, FakeSearch, _html_result, _service

from web_scraper.api import create_app
from web_scraper.api_models import WebScrapeRequest, WebScrapeResponse, WebSearchRequest
from web_scraper.config import config
from web_scraper.mcp_server import McpServer, _scrape, _search
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.pagecache import PageCache
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.settings import WebToolsSettings

COMPACT_KEYS = {
    "title",
    "url",
    "domain",
    "date",
    "date_confidence",
    "source_type",
    "snippet",
    "score",
}
LONG = "https://example.org/the-long-report-on-the-labs-this-week"
SHORT = "https://example.org/a-short-note-on-the-labs-this-week"


def _long_page(paragraphs: int = 400) -> str:
    body = "".join(
        f"<p>Paragraph {n} of the long report: the labs shipped models this week.</p>"
        for n in range(paragraphs)
    )
    return (
        "<html><head><title>The long report</title>"
        '<meta property="article:published_time" content="2026-10-21T08:00:00Z"></head>'
        f"<body><main><article><h1>The long report</h1>{body}</article></main></body></html>"
    )


def _settings(**overrides: object) -> WebToolsSettings:
    values: dict[str, object] = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": [],
        "search_resolve_dates": "off",
        "soft_404_check": False,  # these tests count requests; see test_web_missing_pages.py
    }
    values.update(overrides)
    return WebToolsSettings(**values)  # type: ignore[arg-type]


def _results() -> list[SearchResult]:
    return [
        SearchResult(
            "AI news this week: the labs ship models",
            "https://www.reuters.com/technology/ai-labs-ship-models-2026-10-21/",
            "Oct 21, 2026 · The labs shipped new models this week.",
        ),
        SearchResult(
            "This week in AI, a digest of the labs",
            "https://digest.example/2026/10/22/this-week-in-ai-digest-of-the-labs",
            "Yesterday — a digest of what the labs shipped this week.",
        ),
        SearchResult(
            "Labs AI news, the weekly page",
            "https://digest.example/ai-news-this-week",
            "The labs, this week, in AI news.",
        ),
    ]


def _search_service(**overrides: object):
    return _service(
        FakeFetcher({}),
        search=FakeSearch(_results()),
        settings=_settings(**overrides),
    )


@pytest.fixture
def tools() -> FakeWebTools:
    return FakeWebTools()


@pytest.fixture
def rest(monkeypatch: pytest.MonkeyPatch, tools: FakeWebTools) -> TestClient:
    monkeypatch.setattr(config, "api_keys", ["test-key"], raising=False)
    monkeypatch.setattr(config, "api_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_web_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_allowed_origins", [], raising=False)
    monkeypatch.setattr(config, "api_trusted_hosts", ["testserver"], raising=False)
    monkeypatch.setattr(config, "history_db_path", None, raising=False)
    app = create_app(config)
    app.state.web_tools = tools
    return TestClient(app)


class TestACompactSearch:
    async def test_each_result_holds_the_eight_fields_and_nothing_else(self) -> None:
        service = _search_service()

        answer = await service.search("AI news this week", depth="fast", compact=True)

        assert set(answer) == {"query", "results", "run"}
        assert answer["query"] == "AI news this week"
        assert len(answer["results"]) == 3
        for item in answer["results"]:
            assert set(item) == COMPACT_KEYS
        first = answer["results"][0]
        assert first["domain"] == "reuters.com" and first["source_type"] == "news"
        assert first["date"] == "2026-10-21" and first["date_confidence"] > 0
        assert isinstance(first["score"], float)

    async def test_the_run_block_keeps_provider_warnings_and_fallback(self) -> None:
        service = _search_service()

        answer = await service.search("AI news this week", depth="fast", compact=True)

        assert answer["run"] == {"provider": "fake", "warnings": [], "fallback": None}

    async def test_a_fallback_the_search_took_is_in_the_run_block(self) -> None:
        from test_web_provider_health import Fake, _wall

        ddg, lite = Fake("duckduckgo"), Fake("duckduckgo_lite")
        ddg.error = _wall()
        from test_web_search_resilience import _service as resilient

        service = resilient(ddg, lite, search_fallback_providers=["duckduckgo_lite"])

        answer = await service.search("oulu weather", depth="fast", compact=True)

        run = answer["run"]
        assert run["provider"] == "duckduckgo_lite" and run["warnings"] == ["provider_fallback"]
        assert run["fallback"] == {
            "from": "duckduckgo",
            "to": "duckduckgo_lite",
            "reason": "bot_detected",
            "tried": [
                {"provider": "duckduckgo", "status": "failed", "reason": "bot_detected"},
                {"provider": "duckduckgo_lite", "status": "answered", "reason": None},
            ],
        }

    async def test_debug_adds_the_facets_queries_timings_and_score_parts(self) -> None:
        service = _search_service()

        answer = await service.search("AI news this week", depth="fast", compact=True, debug=True)

        assert set(answer) == {"query", "results", "run", "debug"}
        assert {"facets", "queries", "timings", "depth", "requested_provider"} <= set(
            answer["debug"]
        )
        assert answer["debug"]["queries"][0]["angle"] == "base"
        assert answer["debug"]["facets"]["total_candidates"] == 3
        first = answer["results"][0]
        assert COMPACT_KEYS <= set(first) and set(first) - COMPACT_KEYS == {"debug"}
        assert {"bm25", "relevance", "freshness", "kind", "angles", "date_source"} <= set(
            first["debug"]
        )

    async def test_not_compact_is_the_whole_answer_as_it_always_was(self) -> None:
        service = _search_service()

        answer = await service.search("AI news this week", depth="fast")

        assert {"facets", "queries", "timings", "warnings", "provider", "cache_hit"} <= set(answer)
        assert {"bm25", "relevance", "freshness", "kind", "angles", "year"} <= set(
            answer["results"][0]
        )
        # debug alone changes nothing of a full answer: it holds everything already.
        louder = await service.search("AI news this week", depth="fast", debug=True)
        assert (
            set(louder) == set(answer) | {"cached_timings", "cached_at"}
            and louder["results"] == answer["results"]
        )

    async def test_the_cache_holds_the_whole_answer_whichever_shape_was_asked_for(self) -> None:
        service = _search_service()

        compact = await service.search("AI news this week", depth="fast", compact=True)
        full = await service.search("AI news this week", depth="fast")
        again = await service.search("AI news this week", depth="fast", compact=True)

        assert "facets" not in compact and "facets" in full
        assert full["cache_hit"] is True and again["run"]["cache_hit"] is True

    async def test_a_compact_answer_is_a_fraction_of_the_full_one(self) -> None:
        service = _search_service()

        compact = await service.search("AI news this week", compact=True)
        full = await service.search("AI news this week")

        small = len(json.dumps(compact, separators=(",", ":")))
        big = len(json.dumps(full, separators=(",", ":")))
        assert small < big * 0.6, (small, big)

    async def test_a_result_with_conflicting_dates_is_named_in_the_warnings(self) -> None:
        results = [
            SearchResult(
                "Story with two dates",
                "https://a.example/2026/09/22/story-with-two-dates-this-week",
                "Oct 21, 2026 · a story about the labs this week.",
            ),
            SearchResult(
                "Plain story", "https://b.example/plain-story-labs-week", "Labs this week."
            ),
        ]
        service = _service(FakeFetcher({}), search=FakeSearch(results), settings=_settings())

        answer = await service.search("labs story this week", depth="fast", compact=True)

        assert answer["run"]["warnings"] == ["date_conflict:1"]
        assert set(answer["results"][0]) == COMPACT_KEYS  # the conflict is not a ninth field
        debug = await service.search("labs story this week", depth="fast", compact=True, debug=True)
        assert debug["results"][0]["debug"]["date_conflicts"]

    async def test_what_a_read_asked_for_stays_in_a_compact_result(self) -> None:
        page = "https://www.reuters.com/technology/ai-labs-ship-models-2026-10-21"
        fetcher = FakeFetcher({page: _html_result(page, _long_page(3))})
        service = _service(fetcher, search=FakeSearch(_results()), settings=_settings())

        answer = await service.search("AI news this week", depth="fast", compact=True, read=2)

        first, second = answer["results"][0], answer["results"][1]
        assert "content" in first and "content_error" in second
        assert COMPACT_KEYS <= set(first)


class TestTheCallsThatAskForIt:
    async def test_the_mcp_tool_is_compact_unless_it_says_not(self) -> None:
        service = _search_service()

        compact = await _search(service, {"query": "AI news this week", "depth": "fast"})
        full = await _search(
            service, {"query": "AI news this week", "depth": "fast", "compact": False}
        )
        debug = await _search(
            service, {"query": "AI news this week", "depth": "fast", "debug": True}
        )

        assert set(compact) == {"query", "results", "run"}
        assert "facets" in full and "queries" in full
        assert "debug" in debug and "facets" in debug["debug"]

    def test_the_request_takes_both(self) -> None:
        request = WebSearchRequest.model_validate({"query": "x", "compact": False, "debug": True})

        assert request.compact is False and request.debug is True
        assert WebSearchRequest.model_validate({"query": "x"}).compact is None

    def test_the_rest_route_answers_in_full_unless_asked_for_the_compact_shape(
        self, rest: TestClient, tools: FakeWebTools
    ) -> None:
        plain = rest.post("/api/v1/web/search", headers=HEADERS, json={"query": "example"})
        compact = rest.post(
            "/api/v1/web/search", headers=HEADERS, json={"query": "example", "compact": True}
        )

        assert plain.status_code == 200 and "timings" in plain.json()
        assert compact.status_code == 200
        assert tools.calls[0] == (
            "search",
            {"query": "example", "limit": None, "provider": None, "depth": None},
        )
        assert tools.calls[1][1]["compact"] is True


class TestALeanScrape:
    async def test_it_holds_the_text_and_what_is_needed_to_trust_and_continue_it(self) -> None:
        service = _service(FakeFetcher({LONG: _html_result(LONG, _long_page(5))}))

        page = await service.scrape(LONG, lean=True)

        assert set(page) == {
            "url",
            "title",
            "date",
            "date_confidence",
            "markdown",
            "truncated",
            "total_chars",
        }
        assert page["date"] == "2026-10-21" and page["truncated"] is False
        assert page["markdown"].startswith("# The long report")
        assert page["total_chars"] == len(page["markdown"])

    async def test_the_full_answer_is_unchanged_and_the_cache_holds_it(self) -> None:
        service = _service(FakeFetcher({LONG: _html_result(LONG, _long_page(5))}))

        lean = await service.scrape(LONG, lean=True)
        full = await service.scrape(LONG)

        assert {"metadata", "links", "timings", "content_hash", "canonical_url"} <= set(full)
        assert full["cache_hit"] is True and lean["markdown"] == full["markdown"]

    async def test_links_stay_out_unless_they_are_asked_for(self) -> None:
        html = _long_page(2).replace(
            "</article>", '<p><a href="/next">Next story</a></p></article>'
        )
        service = _service(FakeFetcher({LONG: _html_result(LONG, html)}))

        quiet = WebScrapeRequest.model_validate({"url": LONG, "lean": True}).service_options()
        asked = WebScrapeRequest.model_validate(
            {"url": LONG, "lean": True, "links": True}
        ).service_options()
        without = await service.scrape(LONG, **quiet)
        with_links = await service.scrape(LONG, **asked)

        assert quiet == {"lean": True, "links": False} and asked == {"lean": True}
        assert "links" not in without
        assert with_links["links"][0]["url"] == "https://example.org/next"

    async def test_a_recovered_page_stays_honest_when_lean(self) -> None:
        from test_web_scrape_recover import AMP, URL, _article, _service_for, _wall

        service, _ = _service_for({URL: _wall(), AMP: _html_result(AMP, _article())})

        page = await service.scrape(URL, lean=True)

        assert page["recovered_from"]["kind"] == "amp" and page["fetched_with"] == "amp"
        assert page["next_action"] and page["markdown"].startswith("> Recovered from the AMP")
        assert "metadata" not in page and "timings" not in page

    async def test_a_batch_can_be_lean(self) -> None:
        service = _service(
            FakeFetcher(
                {LONG: _html_result(LONG, _long_page(3)), SHORT: _html_result(SHORT, _long_page(2))}
            )
        )

        answer = await service.scrape_many([LONG, SHORT], lean=True)

        assert [set(page) for page in answer["pages"]] == [
            {
                "url",
                "title",
                "date",
                "date_confidence",
                "markdown",
                "truncated",
                "total_chars",
                "markdown_truncated",
            }
        ] * 2

    def test_the_rest_model_takes_a_lean_answer(self) -> None:
        lean = {"url": LONG, "title": "T", "markdown": "# T", "truncated": False, "total_chars": 3}

        response = WebScrapeResponse(**lean)

        assert response.metadata == {} and response.links == [] and response.status_code == 0


class TestTheRestOfACutPage:
    def _service(self, **overrides: object):
        fetcher = FakeFetcher({LONG: _html_result(LONG, _long_page())})
        return _service(fetcher, settings=_settings(**overrides)), fetcher

    async def test_a_cut_page_says_so_and_how_to_read_on(self) -> None:
        service, _ = self._service()

        page = await service.scrape(LONG, max_chars=5_000)

        assert page["truncated"] is True and len(page["markdown"]) == 5_000
        assert page["total_chars"] > 25_000
        assert page["next_offset"] == 5_000
        assert page["content_id"].startswith("c-") and len(page["content_id"]) == 14

    async def test_a_page_that_fits_has_nothing_left_and_no_id(self) -> None:
        service, _ = self._service()

        page = await service.scrape(LONG, max_chars=200_000)

        assert page["truncated"] is False and "next_offset" not in page and "content_id" not in page

    async def test_the_rest_comes_from_the_cache_part_by_part_and_is_the_same_text(self) -> None:
        service, fetcher = self._service()
        whole = (await service.scrape(LONG, max_chars=200_000))["markdown"]
        first = await service.scrape(LONG, max_chars=5_000, links=False)
        calls = len(fetcher.calls)

        text = first["markdown"]
        content_id, offset = first["content_id"], first["next_offset"]
        parts = 0
        while True:
            part = await service.scrape_content(content_id, offset=offset, max_chars=5_000)
            assert part["offset"] == offset
            text += part["markdown"]
            parts += 1
            if not part["truncated"]:
                assert "next_offset" not in part and "content_id" not in part
                break
            assert part["next_offset"] == offset + len(part["markdown"])
            assert part["content_id"] == content_id
            offset = part["next_offset"]

        assert text == whole and parts >= 5
        assert len(fetcher.calls) == calls  # the page was fetched once, never again
        assert part["total_chars"] == len(whole)

    async def test_a_content_read_is_lean_unless_it_says_not(self) -> None:
        service, _ = self._service()
        first = await service.scrape(LONG, max_chars=5_000)

        lean = await service.scrape_content(first["content_id"], offset=5_000, max_chars=1_000)
        full = await service.scrape_content(
            first["content_id"], offset=5_000, max_chars=1_000, lean=False
        )

        assert set(lean) == {
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
        }
        assert {"metadata", "timings", "content_hash"} <= set(full) and full["cache_hit"] is True
        assert full["markdown"] == lean["markdown"]

    async def test_an_offset_past_the_end_is_refused(self) -> None:
        service, _ = self._service()
        first = await service.scrape(LONG, max_chars=5_000)

        with pytest.raises(WebToolError) as exc:
            await service.scrape_content(first["content_id"], offset=10_000_000)

        assert exc.value.code == "invalid_request" and "total_chars" in exc.value.detail

    async def test_an_id_the_cache_no_longer_holds_says_which_page_to_scrape_again(self) -> None:
        service, _ = self._service()
        clock = [0.0]
        service._pages = PageCache(
            ttl_seconds=300, max_entries=8, max_bytes=20_000_000, clock=lambda: clock[0]
        )
        first = await service.scrape(LONG, max_chars=5_000)

        clock[0] = 301.0
        with pytest.raises(WebToolError) as exc:
            await service.scrape_content(first["content_id"], offset=5_000)

        assert exc.value.code == "content_expired" and exc.value.http_status == 410
        assert exc.value.url == LONG and LONG in exc.value.hint
        with pytest.raises(WebToolError) as unknown:
            await service.scrape_content("c-000000000000")
        assert (
            unknown.value.code == "content_expired"
            and "Scrape the page again" in unknown.value.hint
        )

    async def test_an_answer_cached_longer_than_its_page_does_not_repeat_a_dead_id(self) -> None:
        service, fetcher = self._service()
        clock = [0.0]
        service._pages = PageCache(
            ttl_seconds=300, max_entries=8, max_bytes=20_000_000, clock=lambda: clock[0]
        )
        first = await service.scrape(LONG, max_chars=5_000)

        clock[0] = 301.0  # the page cache let go of the page; the answer cache has not
        second = await service.scrape(LONG, max_chars=5_000)

        assert len(fetcher.calls) == 2 and second["cache_hit"] is False
        assert second["content_id"] == first["content_id"]  # the same page text, the same name
        part = await service.scrape_content(second["content_id"], offset=5_000, max_chars=500)
        assert part["markdown"]

    async def test_the_id_changes_with_the_pages_text(self) -> None:
        fetcher = FakeFetcher({LONG: _html_result(LONG, _long_page(400))})
        service = _service(fetcher, settings=_settings(page_cache_ttl_seconds=300))
        first = await service.scrape(LONG, max_chars=5_000)

        fetcher.pages[LONG] = _html_result(LONG, _long_page(401))
        service._pages = PageCache(ttl_seconds=300, max_entries=8, max_bytes=20_000_000)
        service.cache = type(service.cache)(ttl_seconds=60, max_entries=10)
        second = await service.scrape(LONG, max_chars=5_000)

        assert first["content_id"] != second["content_id"]

    async def test_no_page_cache_no_id(self) -> None:
        service, _ = self._service(page_cache_ttl_seconds=0)

        page = await service.scrape(LONG, max_chars=5_000)

        assert (
            page["truncated"] is True and "content_id" not in page and page["next_offset"] == 5_000
        )

    def test_the_request_asks_for_one_of_a_page_pages_or_content(self) -> None:
        assert (
            WebScrapeRequest.model_validate({"content_id": "c-0123456789ab", "offset": 5000}).offset
            == 5000
        )
        for bad in (
            {},
            {"url": LONG, "content_id": "c-0123456789ab"},
            {"urls": [LONG, SHORT], "content_id": "c-0123456789ab"},
            {"content_id": "not-an-id"},
            {"url": LONG, "offset": 10},
            {"content_id": "c-0123456789ab", "offset": -1},
        ):
            with pytest.raises(ValueError):
                WebScrapeRequest.model_validate(bad)

    async def test_the_mcp_tool_reads_on_by_content_id(self) -> None:
        service, fetcher = self._service()
        first = await _scrape(service, {"url": LONG, "max_chars": 5_000, "lean": True})

        part = await _scrape(
            service,
            {"content_id": first["content_id"], "offset": first["next_offset"], "max_chars": 5_000},
        )

        assert part["offset"] == 5_000 and part["markdown"] and len(fetcher.calls) == 1
        assert "metadata" not in part

    def test_the_rest_route_reads_on_by_content_id(self, rest: TestClient) -> None:
        class Tools(FakeWebTools):
            async def scrape_content(self, content_id: str, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(("scrape_content", {"content_id": content_id, **kwargs}))
                return {
                    "url": LONG,
                    "title": "T",
                    "markdown": "part",
                    "truncated": False,
                    "total_chars": 4,
                }

        tools = Tools()
        rest.app.state.web_tools = tools  # type: ignore[attr-defined]

        response = rest.post(
            "/api/v1/web/scrape",
            headers=HEADERS,
            json={"content_id": "c-0123456789ab", "offset": 5000},
        )

        assert response.status_code == 200 and response.json()["markdown"] == "part"
        assert tools.calls == [
            (
                "scrape_content",
                {"content_id": "c-0123456789ab", "offset": 5000, "max_chars": None, "lean": True},
            )
        ]


class TestThePageInItsOwnBlock:
    """A long answer a client keeps in a file was one line with the page's newlines escaped inside JSON."""

    async def _call(self, service: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        server = McpServer(service)
        response = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "web_scrape", "arguments": arguments},
            }
        )
        assert response is not None
        return response["result"]

    async def test_the_page_is_a_text_block_of_its_own_with_its_line_breaks(self) -> None:
        service = _service(FakeFetcher({LONG: _html_result(LONG, _long_page(30))}))
        direct = await service.scrape(LONG)

        result = await self._call(service, {"url": LONG})

        assert result["isError"] is False and len(result["content"]) == 2
        head, page = (block["text"] for block in result["content"])
        assert "\n" not in head and json.loads(head)["markdown"] == "(below)"
        assert json.loads(head)["title"] == "The long report"
        assert page == direct["markdown"] and page.count("\n") > 50
        # What a client that joins the blocks with a newline writes to its file:
        written = "\n".join([head, page])
        assert written.splitlines()[0] == head
        assert written.splitlines()[1] == "# The long report"
        assert len(written.splitlines()) == 1 + len(page.splitlines())

    async def test_a_batch_marks_each_pages_block(self) -> None:
        service = _service(
            FakeFetcher(
                {LONG: _html_result(LONG, _long_page(3)), SHORT: _html_result(SHORT, _long_page(2))}
            )
        )

        result = await self._call(service, {"urls": [LONG, SHORT, "https://missing.example/x"]})

        blocks = [block["text"] for block in result["content"]]
        head = json.loads(blocks[0])
        assert [page.get("markdown") for page in head["pages"][:2]] == [
            "(page 1 of 2, below)",
            "(page 2 of 2, below)",
        ]
        assert "error" in head["pages"][2] and len(blocks) == 3
        assert blocks[1].startswith(f"<!-- page 1 of 2: {LONG} -->\n# The long report")
        assert blocks[2].startswith(f"<!-- page 2 of 2: {SHORT} -->\n")

    async def test_a_lean_page_and_a_content_read_are_blocks_too(self) -> None:
        service = _service(FakeFetcher({LONG: _html_result(LONG, _long_page())}))

        first = await self._call(service, {"url": LONG, "max_chars": 5_000, "lean": True})
        head = json.loads(first["content"][0]["text"])
        rest = await self._call(
            service,
            {"content_id": head["content_id"], "offset": head["next_offset"], "max_chars": 3_000},
        )

        assert set(head) == {
            "url",
            "title",
            "date",
            "date_confidence",
            "markdown",
            "truncated",
            "total_chars",
            "next_offset",
            "content_id",
        }
        assert len(first["content"][1]["text"]) == 5_000
        assert json.loads(rest["content"][0]["text"])["offset"] == 5_000
        assert len(rest["content"][1]["text"]) == 3_000

    async def test_an_error_stays_one_block_of_json(self) -> None:
        service = _service(FakeFetcher({}))

        result = await self._call(service, {"url": "https://missing.example/x"})

        assert result["isError"] is True and len(result["content"]) == 1
        assert json.loads(result["content"][0]["text"])["error"]["code"] == "http_error"

    async def test_the_other_tools_are_still_one_block(self) -> None:
        service = _search_service()
        server = McpServer(service)

        response = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "web_search",
                    "arguments": {"query": "AI news this week", "depth": "fast"},
                },
            }
        )

        assert response is not None and len(response["result"]["content"]) == 1
        assert set(json.loads(response["result"]["content"][0]["text"])) == {
            "query",
            "results",
            "run",
        }
