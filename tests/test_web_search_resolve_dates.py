"""A search that dates its results from their pages: resolve_dates, strict_dates, and the head read."""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from typing import Optional

import httpx
import pytest
from test_web_service import FakeFetcher, FakeSearch, _html_result, _service

from web_scraper.api_models import WebSearchRequest
from web_scraper.mcp_server import _search
from web_scraper.web_tools.content import resolve_html_dates
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult, SafeFetcher
from web_scraper.web_tools.pagecache import CachedDocument
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.settings import WebToolsSettings

NEWS = "AI news this week"
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _page(published: Optional[str] = None, *, modified: Optional[str] = None) -> str:
    meta = ""
    if published:
        meta += f'<meta property="article:published_time" content="{published}T08:00:00Z">'
    if modified:
        meta += f'<meta property="article:modified_time" content="{modified}T08:00:00Z">'
    return f"<html><head><title>AI week</title>{meta}</head><body><main><h1>AI week</h1><p>Words.</p></main></body></html>"


class HeadFetcher:
    """Answers ``fetch_head`` with recorded pages; a page may be slow or fail."""

    def __init__(
        self,
        pages: dict[str, str],
        *,
        stall: Optional[dict[str, float]] = None,
        errors: Optional[dict[str, Exception]] = None,
    ) -> None:
        self.pages = pages
        self.stall = stall or {}
        self.errors = errors or {}
        self.heads: list[tuple[str, int]] = []
        self.in_flight = 0
        self.peak = 0

    async def fetch_head(
        self, url: str, *, max_bytes: int = 65_536, head_cap: Optional[int] = None
    ) -> FetchResult:
        self.heads.append((url, max_bytes))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.stall.get(url, 0.0))
            if url in self.errors:
                raise self.errors[url]
            if url not in self.pages:
                raise FetchError("http_error", "not found", url=url, status_code=404)
            return _html_result(url, self.pages[url])
        finally:
            self.in_flight -= 1


def _results(*urls: str) -> list[SearchResult]:
    return [
        SearchResult(f"AI news this week {index}", url, "AI news this week from the labs.")
        for index, url in enumerate(urls)
    ]


class OptionsSearch(FakeSearch):
    """A provider that takes the options of a call (a date range) as the real ones do."""

    async def search(  # type: ignore[override]
        self, query: str, limit: int, *, date_filter: Optional[str] = None, options: object = None
    ) -> list[SearchResult]:
        return await super().search(query, limit, date_filter=date_filter)


def _search_service(fetcher: object, urls: tuple[str, ...], **settings: object):
    base: dict[str, object] = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": [],
    }
    base.update(settings)
    return _service(
        fetcher,  # type: ignore[arg-type]
        search=OptionsSearch(_results(*urls)),
        settings=WebToolsSettings(**base),  # type: ignore[arg-type]
    )


ART_A = "https://a.example/ai-labs-announce-new-models-for-the-week"
ART_B = "https://b.example/ai-news-roundup-of-the-labs-this-week"
ART_C = "https://c.example/the-labs-ai-news-in-brief-for-this-week"


class TestResolveDates:
    async def test_a_news_search_dates_its_results_from_their_pages(self) -> None:
        fetcher = HeadFetcher({ART_A: _page(YESTERDAY), ART_B: _page("2026-02-01")})
        service = _search_service(fetcher, (ART_A, ART_B))

        answer = await service.search(NEWS, depth="fast")

        by_url = {item["url"]: item for item in answer["results"]}
        assert by_url[ART_A]["date"] == YESTERDAY
        assert by_url[ART_A]["date_source"] == "meta:article:published_time"
        assert by_url[ART_A]["date_confidence"] == pytest.approx(0.75)
        assert by_url[ART_A]["freshness"] == "this_week"
        assert by_url[ART_B]["date"] == "2026-02-01" and by_url[ART_B]["freshness"] == "this_year"
        assert {url for url, _ in fetcher.heads} == {ART_A, ART_B}
        assert {size for _, size in fetcher.heads} == {65_536}
        assert answer["dates"]["asked"] == 2 and answer["dates"]["resolved"] == 2
        assert answer["dates"]["read"] == 2 and answer["dates"]["cut"] == 0
        assert answer["timings"]["dates_ms"] >= 0

    async def test_the_results_are_ranked_again_with_the_dates_found(self) -> None:
        pages = {ART_A: _page("2026-02-01"), ART_B: _page(YESTERDAY)}
        service = _search_service(HeadFetcher(pages), (ART_A, ART_B))
        without = await _search_service(HeadFetcher(pages), (ART_A, ART_B)).search(
            NEWS, resolve_dates=False
        )

        answer = await service.search(NEWS)

        # Undated, the provider's order stands; dated, the fresh story goes before the stale one.
        assert [item["url"] for item in without["results"]] == [ART_A, ART_B]
        assert [item["url"] for item in answer["results"]] == [ART_B, ART_A]

    async def test_a_page_that_disagrees_with_its_address_says_so(self) -> None:
        url = "https://a.example/2026/09/22/ai-labs-announce-new-models-for-the-week"
        service = _search_service(HeadFetcher({url: _page(YESTERDAY)}), (url,))

        answer = await service.search(NEWS, depth="fast")

        item = answer["results"][0]
        assert item["date"] == YESTERDAY and item["date_source"] == "meta:article:published_time"
        assert item["date_conflicts"] == [{"source": "url", "value": "2026-09-22"}]
        assert item["date_confidence"] < 0.75

    async def test_pages_that_cannot_be_dated_keep_what_the_result_showed(self) -> None:
        url = "https://a.example/2026/10/20/ai-labs-announce-new-models-for-the-week"
        fetcher = HeadFetcher(
            {url: _page()}, errors={ART_B: FetchError("blocked", "wall", url=ART_B)}
        )
        service = _search_service(fetcher, (url, ART_B))

        answer = await service.search(NEWS, depth="fast")

        by_url = {item["url"]: item for item in answer["results"]}
        assert by_url[url]["date"] == "2026-10-20" and by_url[url]["date_source"] == "url"
        assert by_url[ART_B]["date"] is None and by_url[ART_B]["date_confidence"] == 0.0
        assert answer["dates"]["failed"] == 1 and answer["dates"]["resolved"] == 0
        assert answer["warnings"] == []

    async def test_a_front_page_and_a_listing_are_not_read(self) -> None:
        front, listing = "https://a.example/", "https://b.example/archive/2026/10"
        fetcher = HeadFetcher({ART_C: _page(YESTERDAY)})
        service = _search_service(fetcher, (front, listing, ART_C))

        await service.search(NEWS, depth="fast")

        assert [url for url, _ in fetcher.heads] == [ART_C]

    async def test_only_the_top_results_are_read(self) -> None:
        urls = tuple(
            f"https://site{n}.example/ai-labs-announce-new-models-week-{n}" for n in range(8)
        )
        fetcher = HeadFetcher({url: _page(YESTERDAY) for url in urls})
        service = _search_service(fetcher, urls, search_resolve_dates_top=3)

        await service.search(NEWS, depth="fast", limit=8)

        assert len(fetcher.heads) == 3

    async def test_a_slow_page_is_cut_by_the_budget_and_the_search_goes_on(self) -> None:
        fetcher = HeadFetcher(
            {ART_A: _page(YESTERDAY), ART_B: _page(YESTERDAY)}, stall={ART_B: 5.0}
        )
        service = _search_service(fetcher, (ART_A, ART_B), search_resolve_dates_budget_ms=150)
        started = time.perf_counter()

        answer = await service.search(NEWS, depth="fast")

        assert time.perf_counter() - started < 2.0
        by_url = {item["url"]: item for item in answer["results"]}
        assert by_url[ART_A]["date"] == YESTERDAY and by_url[ART_B]["date"] is None
        assert "resolve_dates_cut:1" in answer["warnings"]
        assert answer["dates"]["cut"] == 1 and answer["dates"]["resolved"] == 1

    async def test_the_pages_are_read_together_and_two_of_one_host_at_a_time(self) -> None:
        same = [f"https://a.example/story-{n}-ai-news-this-week-from-the-labs" for n in (1, 2, 3)]
        other = "https://b.example/ai-news-this-week-story-from-the-labs"
        urls = [*same, other]
        fetcher = HeadFetcher(
            {url: _page(YESTERDAY) for url in urls}, stall=dict.fromkeys(urls, 0.05)
        )
        service = _search_service(fetcher, tuple(urls))

        await service.search(NEWS, depth="fast")

        # Two of a.example and the one of b.example together; the third of a.example waits for a turn.
        assert len(fetcher.heads) == 4 and fetcher.peak == 3

    async def test_a_page_the_page_cache_holds_costs_no_request(self) -> None:
        fetcher = HeadFetcher({ART_B: _page(YESTERDAY)})
        service = _search_service(fetcher, (ART_A, ART_B))
        service._pages.put(
            ART_A,
            CachedDocument(
                final_url=ART_A,
                status_code=200,
                content_type="text/html",
                html=_page("2026-10-01"),
                fetched_with="http",
                warnings=(),
                timings={},
            ),
        )

        answer = await service.search(NEWS, depth="fast")

        assert [url for url, _ in fetcher.heads] == [ART_B]
        assert answer["dates"]["from_page_cache"] == 1
        assert {item["url"]: item["date"] for item in answer["results"]}[ART_A] == "2026-10-01"

    async def test_a_fetcher_with_only_fetch_has_its_page_cut_to_the_size(self) -> None:
        fetcher = FakeFetcher({ART_A: _html_result(ART_A, _page(YESTERDAY))})
        service = _search_service(fetcher, (ART_A,), search_resolve_dates_bytes=4_096)

        answer = await service.search(NEWS, depth="fast")

        assert answer["results"][0]["date"] == YESTERDAY
        assert fetcher.calls == [ART_A]

    async def test_a_fetcher_that_cannot_read_leaves_the_search_as_it_was(self) -> None:
        service = _search_service(object(), (ART_A,))

        answer = await service.search(NEWS, depth="fast")

        assert answer["results"][0]["date"] is None
        assert answer["warnings"] == []


class TestWhenItReads:
    URLS = (ART_A,)

    async def test_a_query_that_asks_for_nothing_fresh_reads_nothing(self) -> None:
        fetcher = HeadFetcher({ART_A: _page(YESTERDAY)})
        service = _search_service(fetcher, self.URLS)

        answer = await service.search("python asyncio tutorial ai labs", depth="fast")

        assert fetcher.heads == [] and "dates" not in answer

    async def test_a_call_may_ask_for_it_and_may_refuse_it(self) -> None:
        pages = {ART_A: _page(YESTERDAY)}
        asked = HeadFetcher(pages)
        refused = HeadFetcher(pages)

        await _search_service(asked, self.URLS).search(
            "python asyncio tutorial ai labs", depth="fast", resolve_dates=True
        )
        await _search_service(refused, self.URLS).search(NEWS, depth="fast", resolve_dates=False)

        assert len(asked.heads) == 1 and refused.heads == []

    async def test_a_date_range_reads_and_keeps_only_what_the_pages_put_inside_it(self) -> None:
        fetcher = HeadFetcher({ART_A: _page("2026-10-02"), ART_B: _page("2026-06-01")})
        service = _search_service(fetcher, (ART_A, ART_B))

        answer = await service.search(
            "ai labs announce new models",
            depth="fast",
            date_from="2026-09-01",
            date_to="2026-10-31",
        )

        assert [item["url"] for item in answer["results"]] == [ART_A]
        assert len(fetcher.heads) == 2

    async def test_the_setting_off_reads_nothing_whatever_a_call_says(self) -> None:
        fetcher = HeadFetcher({ART_A: _page(YESTERDAY)})
        service = _search_service(fetcher, self.URLS, search_resolve_dates="off")

        quiet = await service.search(NEWS, depth="fast")
        asked = await service.search("ai labs again", depth="fast", resolve_dates=True)

        assert fetcher.heads == []
        assert "resolve_dates_disabled" not in quiet["warnings"]
        assert "resolve_dates_disabled" in asked["warnings"]

    async def test_the_setting_on_reads_for_every_search(self) -> None:
        fetcher = HeadFetcher({ART_A: _page(YESTERDAY)})
        service = _search_service(fetcher, self.URLS, search_resolve_dates="on")

        await service.search("python asyncio tutorial ai labs", depth="fast")

        assert len(fetcher.heads) == 1

    def test_the_settings_are_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WEB_TOOLS_SEARCH_RESOLVE_DATES", "OFF")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_RESOLVE_DATES_TOP", "3")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_RESOLVE_DATES_BUDGET_MS", "2500")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_RESOLVE_DATES_BYTES", "100")

        settings = WebToolsSettings.from_env()

        assert settings.search_resolve_dates == "off"
        assert settings.search_resolve_dates_top == 3
        assert settings.search_resolve_dates_budget_ms == 2_500
        assert settings.search_resolve_dates_bytes == 4_096  # not under the size of a head
        defaults = WebToolsSettings()
        assert (defaults.search_resolve_dates, defaults.search_resolve_dates_bytes) == (
            "auto",
            65_536,
        )
        assert (defaults.search_resolve_dates_top, defaults.search_resolve_dates_budget_ms) == (
            6,
            4_000,
        )
        # A head is read on to its end up to a cap (cloud.google.com's are 170 KB).
        assert defaults.search_resolve_dates_head_cap == 262_144
        monkeypatch.setenv("WEB_TOOLS_SEARCH_RESOLVE_DATES_HEAD_CAP", "10")
        assert (
            WebToolsSettings.from_env().search_resolve_dates_head_cap == 4_096
        )  # never under the read


class TestStrictDates:
    async def test_it_drops_what_has_no_date_and_says_how_many(self) -> None:
        pages = {ART_A: _page(YESTERDAY), ART_C: _page("2026-10-20")}
        service = _search_service(HeadFetcher(pages), (ART_A, ART_B, ART_C))

        answer = await service.search(NEWS, depth="fast", strict_dates=True)

        assert {item["url"] for item in answer["results"]} == {ART_A, ART_C}
        assert all(item["date_confidence"] >= 0.5 for item in answer["results"])
        assert "strict_dates_dropped:1" in answer["warnings"]
        assert answer["dates"]["strict"] is True and answer["dates"]["dropped"] == 1

    async def test_it_drops_a_date_that_is_only_a_guess(self) -> None:
        url = "https://a.example/2026/10/20/ai-labs-announce-new-models-for-the-week"
        service = _search_service(HeadFetcher({url: _page()}), (url,))

        answer = await service.search(NEWS, depth="fast", strict_dates=True)

        assert answer["results"] == []
        assert "strict_dates_dropped:1" in answer["warnings"]
        # The provider answered with results: an empty list is the filter's doing, not the query's.
        assert "no_relevant_results" not in answer["warnings"]

    async def test_it_reads_further_down_to_keep_the_limit(self) -> None:
        urls = tuple(
            f"https://site{n}.example/ai-labs-announce-new-models-week-{n}" for n in range(6)
        )
        # The first two cannot be dated; the next four can.
        pages = {url: _page(YESTERDAY) for url in urls[2:]}
        service = _search_service(HeadFetcher(pages), urls)

        answer = await service.search(NEWS, depth="fast", strict_dates=True, limit=3)

        assert len(answer["results"]) == 3
        assert all(item["date"] == YESTERDAY for item in answer["results"])
        assert "strict_dates_dropped:2" in answer["warnings"]

    async def test_without_it_the_undated_stay(self) -> None:
        service = _search_service(HeadFetcher({ART_A: _page(YESTERDAY)}), (ART_A, ART_B))

        answer = await service.search(NEWS, depth="fast")

        assert len(answer["results"]) == 2
        assert not any(warning.startswith("strict_dates") for warning in answer["warnings"])

    async def test_the_cache_keeps_the_two_apart(self) -> None:
        service = _search_service(HeadFetcher({ART_A: _page(YESTERDAY)}), (ART_A, ART_B))

        strict = await service.search(NEWS, depth="fast", strict_dates=True)
        plain = await service.search(NEWS, depth="fast")

        assert len(strict["results"]) == 1 and len(plain["results"]) == 2
        assert plain["cache_hit"] is False


class TestTheHeadRead:
    BIG = (
        "<html><head><title>T</title><meta property='article:published_time' content='2026-10-21'></head><body>"
        + "<p>x</p>" * 100_000
        + "</body></html>"
    )

    def _fetcher(self, handler, validator=None) -> SafeFetcher:
        return SafeFetcher(
            transport=httpx.MockTransport(handler),
            validator=validator or (lambda url, allow_private_networks=False: url),
            retry_attempts=3,
            backoff_seconds=0,
        )

    async def test_it_reads_the_first_bytes_of_a_long_page_and_is_not_too_large(self) -> None:
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, headers={"content-type": "text/html"}, text=self.BIG)

        async with self._fetcher(handler) as fetcher:
            result = await fetcher.fetch_head("https://example.org/story", max_bytes=65_536)

        assert 65_000 < len(result.html) <= 65_536
        assert "article:published_time" in result.html
        assert calls == ["https://example.org/story"]

    @staticmethod
    def _heavy_head(*, ends: bool = True, filler: int = 110_000) -> str:
        """A head like cloud.google.com's: inline scripts first, the structured data in the middle."""
        ld = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-10-21"}</script>'
        script = "<script>" + "x" * filler + "</script>"
        head = f"<html><head><title>T</title>{script}{ld}{script}" + ("</head>" if ends else "")
        return head + "<body>" + "<p>x</p>" * 40_000 + "</body></html>"

    async def test_a_head_that_has_not_ended_is_read_on_to_its_end(self) -> None:
        page = self._heavy_head()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page)

        async with self._fetcher(handler) as fetcher:
            plain = await fetcher.fetch_head("https://example.org/a", max_bytes=65_536)
            wide = await fetcher.fetch_head(
                "https://example.org/a", max_bytes=65_536, head_cap=262_144
            )

        # 64 KB of it is scripts and no date; the head, read to its end, holds the date.
        assert "datePublished" not in plain.html and len(plain.html) == 65_536
        assert "datePublished" in wide.html and wide.html.endswith("</head>")
        assert len(wide.html) < 262_144 and "<body>" not in wide.html
        assert resolve_html_dates(wide.html, "https://example.org/a").date == "2026-10-21"

    async def test_a_head_that_ends_early_is_not_read_past_the_size(self) -> None:
        page = "<html><head><title>T</title></head><body>" + "<p>x</p>" * 40_000 + "</body></html>"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page)

        async with self._fetcher(handler) as fetcher:
            result = await fetcher.fetch_head(
                "https://example.org/a", max_bytes=65_536, head_cap=262_144
            )

        assert len(result.html) == 65_536

    async def test_a_head_that_never_ends_is_cut_at_the_cap(self) -> None:
        page = self._heavy_head(ends=False, filler=400_000)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page)

        async with self._fetcher(handler) as fetcher:
            result = await fetcher.fetch_head(
                "https://example.org/a", max_bytes=65_536, head_cap=150_000
            )

        assert len(result.html) == 150_000

    async def test_a_long_page_is_still_too_large_for_a_whole_read(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=self.BIG)

        fetcher = SafeFetcher(
            transport=httpx.MockTransport(handler),
            validator=lambda url, allow_private_networks=False: url,
            max_response_bytes=10_000,
        )
        async with fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch("https://example.org/story")
            head = await fetcher.fetch_head("https://example.org/story", max_bytes=4_000)

        assert exc.value.code == "too_large"
        assert len(head.html) == 4_000

    async def test_it_does_not_retry(self) -> None:
        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(503, headers={"content-type": "text/html"}, text="busy")

        async with self._fetcher(handler) as fetcher:
            with pytest.raises(FetchError):
                await fetcher.fetch_head("https://example.org/story")

        assert calls["count"] == 1

    async def test_it_follows_a_redirect_only_to_a_target_that_passes_the_check(self) -> None:
        from web_scraper.network_safety import UnsafeTargetError

        def validator(url: str, allow_private_networks: bool = False) -> str:
            if "127.0.0.1" in url:
                raise UnsafeTargetError("blocked")
            return url

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

        async with self._fetcher(handler, validator) as fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch_head("https://example.org/story")

        assert exc.value.code == "unsafe_target"

    async def test_a_wall_is_still_a_wall_and_a_pdf_is_not_a_page(self) -> None:
        async def wall(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={"content-type": "text/html"},
                text="<html><body>Access denied</body></html>",
            )

        async def pdf(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7 ..."
            )

        async with self._fetcher(wall) as fetcher:
            with pytest.raises(FetchError) as walled:
                await fetcher.fetch_head("https://example.org/story")
        async with self._fetcher(pdf) as fetcher:
            document = await fetcher.fetch_head("https://example.org/story.pdf")

        assert walled.value.code == "blocked"
        assert document.content_type == "application/pdf" and document.html == ""


class TestTheParameters:
    def test_the_request_takes_both(self) -> None:
        request = WebSearchRequest.model_validate(
            {"query": NEWS, "resolve_dates": True, "strict_dates": True}
        )

        assert request.resolve_dates is True and request.strict_dates is True
        assert WebSearchRequest.model_validate({"query": NEWS}).resolve_dates is None

    async def test_the_mcp_tool_passes_them_on_only_when_they_are_set(self) -> None:
        seen: list[dict] = []

        class Spy:
            async def search(self, query: str, **kwargs: object) -> dict:
                seen.append(kwargs)
                return {}

        await _search(Spy(), {"query": NEWS})  # type: ignore[arg-type]
        await _search(Spy(), {"query": NEWS, "resolve_dates": False, "strict_dates": True})  # type: ignore[arg-type]

        assert "resolve_dates" not in seen[0] and "strict_dates" not in seen[0]
        assert seen[1]["resolve_dates"] is False and seen[1]["strict_dates"] is True

    async def test_an_unknown_value_is_still_an_error(self) -> None:
        with pytest.raises(WebToolError):
            await _search_service(object(), (ART_A,)).search(NEWS, depth="nope")
