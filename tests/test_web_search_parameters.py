"""Search parameters: region, language, sites, exclude_sites, date_from and date_to."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError
from test_duckduckgo_lite import FakeClient, FakeResponse, _html_page

from web_scraper.api_models import WebSearchRequest
from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.duckduckgo_search import DuckDuckGoSearcher
from web_scraper.google_search import GoogleSearcher
from web_scraper.mcp_server import _search
from web_scraper.web_tools.discovery import build_search_angles, within_date_range
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.search import (
    GoogleSearchAdapter,
    SearchOptions,
    SearchResult,
)
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

URL = "https://example.com/"


class Recording:
    """A provider that records what it was asked, with the options, and answers per query."""

    name = "fake"

    def __init__(self, results: Optional[list[SearchResult]] = None) -> None:
        self.results = results if results is not None else []
        self.calls: list[tuple[str, int, Optional[str], Optional[SearchOptions]]] = []

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: Optional[str] = None,
        options: Optional[SearchOptions] = None,
    ) -> list[SearchResult]:
        self.calls.append((query, limit, date_filter, options))
        await asyncio.sleep(0)
        return list(self.results)


def _service(adapter: Recording) -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(
            search_provider="fake",
            search_angle_delay_ms=0,
            search_jitter_min_ms=0,
            search_jitter_max_ms=0,
            search_fallback_providers=[],
        ),
        fetcher=object(),
        search_adapters={"fake": adapter},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


def _mixed() -> list[SearchResult]:
    return [
        SearchResult("Python asyncio docs", "https://docs.python.org/3/library/asyncio.html", ""),
        SearchResult("Python asyncio home", "https://python.org/asyncio", ""),
        SearchResult("Not python asyncio", "https://notpython.org/asyncio", ""),
        SearchResult("Python asyncio evil", "https://python.org.evil.example/asyncio", ""),
        SearchResult("Python asyncio thread", "https://old.reddit.com/r/python/asyncio", ""),
        SearchResult("Python asyncio blog", "https://blog.example.net/asyncio", ""),
    ]


class TestValidation:
    def test_every_parameter_is_optional(self) -> None:
        request = WebSearchRequest(query="python")

        assert request.region is None and request.language is None
        assert request.sites is None and request.exclude_sites is None
        assert request.date_from is None and request.date_to is None

    def test_good_values_are_normalised(self) -> None:
        request = WebSearchRequest(
            query="python",
            region="FI-fi",
            language="pt-BR",
            sites=["Docs.Python.org", "https://python.org/3/", "docs.python.org"],
            exclude_sites=["reddit.com"],
            date_from="2026-10-01",
            date_to="2026-10-20",
        )

        assert request.region == "fi-fi"
        assert request.language == "pt-BR"
        assert request.sites == ["docs.python.org", "python.org"]
        assert request.exclude_sites == ["reddit.com"]
        assert request.date_from == date(2026, 10, 1)
        assert request.date_to == date(2026, 10, 20)

    @pytest.mark.parametrize(
        "bad",
        [
            {"region": "finland"},
            {"region": "f-i"},
            {"region": "fi_fi"},
            {"language": "finnish"},
            {"language": "1"},
            {"language": ""},
            {"sites": ["a.example"] * 5 + ["b.example"]},
            {"sites": ["not a domain"]},
            {"sites": ["localhost"]},
            {"exclude_sites": ["-site:x.com"]},
            {"date_from": "10/05/2026"},
            {"date_to": "yesterday"},
            {"date_from": "2026-10-20", "date_to": "2026-10-01"},
        ],
    )
    def test_bad_values_are_refused(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            WebSearchRequest(query="python", **bad)

    def test_five_sites_are_allowed(self) -> None:
        request = WebSearchRequest(query="python", sites=[f"s{n}.example" for n in range(5)])

        assert len(request.sites or []) == 5

    def test_the_schema_states_the_parameters(self) -> None:
        properties = WebSearchRequest.model_json_schema()["properties"]

        assert {"region", "language", "sites", "exclude_sites", "date_from", "date_to"} <= set(
            properties
        )
        sites = properties["sites"]
        assert any(option.get("maxItems") == 5 for option in sites.get("anyOf", [sites]))

    @pytest.mark.asyncio
    async def test_the_service_refuses_what_the_model_refuses(self) -> None:
        service = _service(Recording())

        for bad in ({"region": "x"}, {"sites": ["a b"]}, {"date_from": "soon"}):
            with pytest.raises(WebToolError) as exc:
                await service.search("python", depth="fast", **bad)
            assert exc.value.code == "invalid_request"

    @pytest.mark.asyncio
    async def test_the_mcp_tool_passes_them_on(self) -> None:
        adapter = Recording(_mixed())

        await _search(
            _service(adapter),
            {
                "query": "python asyncio",
                "depth": "fast",
                "region": "fi-fi",
                "language": "fi",
                "date_from": "2026-10-01",
            },
        )

        options = adapter.calls[0][3]
        assert options == SearchOptions(
            region="fi-fi", language="fi", date_from=date(2026, 10, 1), date_to=None
        )


class TestEveryAngleCarriesThem:
    @pytest.mark.asyncio
    async def test_options_and_sites_reach_every_angle(self) -> None:
        adapter = Recording(_mixed())

        await _service(adapter).search(
            "python asyncio",
            depth="deep",
            region="fi-fi",
            language="fi",
            sites=["python.org", "docs.python.org"],
            exclude_sites=["reddit.com"],
        )

        assert len(adapter.calls) == 4  # base, current, background, primary
        for query, _, _, options in adapter.calls:
            assert query.endswith("(site:python.org OR site:docs.python.org) -site:reddit.com")
            assert options == SearchOptions(region="fi-fi", language="fi")

    @pytest.mark.asyncio
    async def test_one_site_has_no_parentheses(self) -> None:
        adapter = Recording(_mixed())

        await _service(adapter).search("python asyncio", depth="fast", sites=["python.org"])

        assert adapter.calls[0][0] == "python asyncio site:python.org"

    @pytest.mark.asyncio
    async def test_without_parameters_the_calls_are_what_they_were(self) -> None:
        class Plain:
            name = "fake"
            calls: list[tuple[str, int, Optional[str]]] = []

            async def search(
                self, query: str, limit: int, *, date_filter: Optional[str] = None
            ) -> list[SearchResult]:
                self.calls.append((query, limit, date_filter))
                return []

        plain = Plain()
        service = _service(plain)  # type: ignore[arg-type]

        await service.search("python asyncio", depth="discover")

        assert [call[0] for call in plain.calls] == [
            "python asyncio",
            f"python asyncio {date.today().year}",
            "python asyncio history background",
        ]

    @pytest.mark.asyncio
    async def test_the_composed_query_keeps_the_length_limit(self) -> None:
        adapter = Recording(_mixed())
        service = _service(adapter)
        service.settings.max_query_length = 120

        await service.search("python " * 12, depth="deep", sites=["docs.python.org"])

        assert all(len(call[0]) <= 120 for call in adapter.calls)
        assert all(call[0].endswith("site:docs.python.org") for call in adapter.calls)


class TestDateRange:
    def test_the_year_based_angles_are_not_added_against_a_range(self) -> None:
        names = lambda depth, **kw: [  # noqa: E731
            angle.name for angle in build_search_angles("python asyncio", depth, **kw)
        ]

        assert names("discover") == ["base", "current", "background"]
        assert names("discover", explicit_range=True) == ["base"]
        assert names("deep", explicit_range=True) == ["base", "primary"]

    def test_a_range_replaces_the_freshness_cue_of_the_base_angle(self) -> None:
        cue = build_search_angles("latest python release", "fast")
        ranged = build_search_angles("latest python release", "fast", explicit_range=True)

        assert cue[0].date_filter == "year"
        assert ranged[0].date_filter is None

    @pytest.mark.parametrize(
        ("published", "inside"),
        [
            ("2026-10-05", True),
            ("2026-10-01", True),
            ("2026-10-20", True),
            ("2026-09-30", False),
            ("2026-10-21", False),
            ("2026-10", True),  # the month overlaps the range
            ("2026-09", False),
            ("2026-11", False),
            (None, True),  # undated: kept
        ],
    )
    def test_a_known_date_outside_the_range_is_out(
        self, published: Optional[str], inside: bool
    ) -> None:
        assert within_date_range(published, date(2026, 10, 1), date(2026, 10, 20)) is inside

    def test_a_range_may_be_open_on_one_side(self) -> None:
        assert within_date_range("2026-12-01", date(2026, 10, 1), None) is True
        assert within_date_range("2026-09-01", date(2026, 10, 1), None) is False
        assert within_date_range("2020-01-01", None, date(2026, 10, 20)) is True
        assert within_date_range("2026-11-01", None, date(2026, 10, 20)) is False

    @pytest.mark.asyncio
    async def test_results_dated_outside_the_range_are_dropped_and_undated_kept(self) -> None:
        adapter = Recording(
            [
                SearchResult("Asyncio news in range", "https://a.example/2026/10/05/asyncio", ""),
                SearchResult("Asyncio news before", "https://b.example/2026/09/01/asyncio", ""),
                SearchResult("Asyncio news after", "https://c.example/2026/10/22/asyncio", ""),
                SearchResult("Asyncio month", "https://d.example/2026/10/asyncio", ""),
                SearchResult("Asyncio undated", "https://e.example/asyncio", "Event loops."),
                SearchResult("Asyncio snippet", "https://f.example/g", "Sep 3, 2026 asyncio"),
            ]
        )

        response = await _service(adapter).search(
            "asyncio", depth="discover", date_from="2026-10-01", date_to="2026-10-20"
        )

        urls = {item["url"] for item in response["results"]}
        assert urls == {
            "https://a.example/2026/10/05/asyncio",
            "https://d.example/2026/10/asyncio",
            "https://e.example/asyncio",
        }
        assert [call[0] for call in adapter.calls] == ["asyncio"]  # base angle only
        assert adapter.calls[0][2] is None
        assert adapter.calls[0][3] == SearchOptions(
            date_from=date(2026, 10, 1), date_to=date(2026, 10, 20)
        )


class TestSites:
    @pytest.mark.asyncio
    async def test_only_results_on_the_sites_or_their_subdomains_stay(self) -> None:
        response = await _service(Recording(_mixed())).search(
            "python asyncio", depth="fast", sites=["python.org"]
        )

        assert [item["url"] for item in response["results"]] == [
            "https://docs.python.org/3/library/asyncio.html",
            "https://python.org/asyncio",
        ]

    @pytest.mark.asyncio
    async def test_excluded_sites_and_their_subdomains_are_kept_out(self) -> None:
        response = await _service(Recording(_mixed())).search(
            "python asyncio", depth="fast", exclude_sites=["reddit.com", "python.org"]
        )

        assert {item["url"] for item in response["results"]} == {
            "https://notpython.org/asyncio",
            "https://python.org.evil.example/asyncio",
            "https://blog.example.net/asyncio",
        }

    @pytest.mark.asyncio
    async def test_a_site_that_matches_nothing_gives_an_empty_answer(self) -> None:
        response = await _service(Recording(_mixed())).search(
            "python asyncio", depth="fast", sites=["wikipedia.org"]
        )

        assert response["results"] == []
        assert response["warnings"] == ["no_relevant_results"]


class TestCacheKey:
    @pytest.mark.asyncio
    async def test_every_parameter_is_part_of_the_key(self) -> None:
        adapter = Recording(_mixed())
        service = _service(adapter)
        variants: list[dict[str, Any]] = [
            {},
            {"region": "fi-fi"},
            {"language": "fi"},
            {"sites": ["python.org"]},
            {"exclude_sites": ["reddit.com"]},
            {"date_from": "2026-10-01"},
            {"date_to": "2026-10-20"},
        ]

        for variant in variants:
            first = await service.search("python asyncio", depth="fast", **variant)
            again = await service.search("python asyncio", depth="fast", **variant)
            assert first["cache_hit"] is False
            assert again["cache_hit"] is True

        assert len(adapter.calls) == len(variants)

    @pytest.mark.asyncio
    async def test_the_order_of_sites_does_not_matter(self) -> None:
        adapter = Recording(_mixed())
        service = _service(adapter)

        await service.search("python", depth="fast", sites=["a.example", "b.example"])
        again = await service.search("python", depth="fast", sites=["b.example", "a.example"])

        assert again["cache_hit"] is True


class TestDuckDuckGo:
    def test_region_and_range_become_kl_and_df(self) -> None:
        params = DuckDuckGoSearcher()._build_params(
            "python",
            None,
            region="fi-fi",
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 20),
        )

        assert params["kl"] == "fi-fi" and params["bing_market"] == "fi-fi"
        assert params["df"] == "2026-10-01..2026-10-20"

    def test_a_range_open_on_one_side_is_closed_by_today_or_a_distant_start(self) -> None:
        searcher = DuckDuckGoSearcher()

        from_only = searcher._build_params("python", None, date_from=date(2026, 10, 1))
        to_only = searcher._build_params("python", None, date_to=date(2026, 10, 20))

        assert from_only["df"] == f"2026-10-01..{date.today().isoformat()}"
        assert to_only["df"] == "1990-01-01..2026-10-20"

    def test_a_range_beats_a_relative_filter_and_the_settings_stay_the_default(self) -> None:
        searcher = DuckDuckGoSearcher(region="tr-tr")

        ranged = searcher._build_params("python", "year", date_from=date(2026, 10, 1))
        plain = searcher._build_params("python", "year")

        assert ranged["df"].startswith("2026-10-01..")
        assert plain["df"] == "y"
        assert plain["kl"] == "tr-tr"

    @pytest.mark.parametrize(
        ("language", "header"),
        [("fi", "fi,en;q=0.5"), ("en", "en"), ("pt-BR", "pt-BR,pt;q=0.9,en;q=0.5")],
    )
    def test_the_language_becomes_the_accept_language_of_the_call(
        self, language: str, header: str
    ) -> None:
        searcher = DuckDuckGoSearcher(language="tr-TR,tr;q=0.9")

        assert searcher._post_headers(language)["accept-language"] == header
        assert searcher._post_headers()["accept-language"] == "tr-TR,tr;q=0.9"

    @pytest.mark.asyncio
    async def test_a_search_posts_them_on_every_page(self) -> None:
        page = _html_page("https://a.example/", "A", next_form=True)
        client = FakeClient(FakeResponse(page), FakeResponse(_html_page("https://b.example/", "B")))
        searcher = DuckDuckGoSearcher(client=client)

        await searcher.search(
            "python",
            num_results=5,
            region="fi-fi",
            language="fi",
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 20),
        )

        assert len(client.calls) == 2
        for _, _, data, headers in client.calls:
            assert data["kl"] == "fi-fi"
            assert data["df"] == "2026-10-01..2026-10-20"
            assert headers["accept-language"] == "fi,en;q=0.5"


class TestGoogle:
    def test_the_default_url_is_what_it_was(self) -> None:
        query = parse_qs(urlsplit(GoogleSearcher()._build_url("python", 10)).query)

        assert query == {"q": ["python"], "num": ["10"], "hl": ["en"], "gl": ["us"]}

    def test_region_language_and_range_become_gl_hl_and_tbs(self) -> None:
        url = GoogleSearcher()._build_url(
            "python",
            10,
            gl="fi",
            hl="fi",
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 20),
        )

        query = parse_qs(urlsplit(url).query)
        assert query["gl"] == ["fi"] and query["hl"] == ["fi"]
        assert query["tbs"] == ["cdr:1,cd_min:10/1/2026,cd_max:10/20/2026"]

    def test_a_range_open_on_one_side_has_one_bound(self) -> None:
        searcher = GoogleSearcher()

        after = parse_qs(urlsplit(searcher._build_url("p", 10, date_from=date(2026, 3, 9))).query)
        before = parse_qs(urlsplit(searcher._build_url("p", 10, date_to=date(2026, 3, 9))).query)

        assert after["tbs"] == ["cdr:1,cd_min:3/9/2026"]
        assert before["tbs"] == ["cdr:1,cd_max:3/9/2026"]

    @pytest.mark.asyncio
    async def test_the_adapter_turns_the_region_into_a_country_and_passes_the_rest(self) -> None:
        class Searcher:
            def __init__(self) -> None:
                self.kwargs: list[dict[str, Any]] = []

            async def search(self, query: str, num_results: int = 10, **kwargs: Any) -> list[dict]:
                self.kwargs.append(kwargs)
                return [{"title": "T", "url": "https://x.example/", "snippet": ""}]

        searcher = Searcher()
        adapter = GoogleSearchAdapter(searcher)  # type: ignore[arg-type]

        await adapter.search(
            "python",
            5,
            options=SearchOptions(region="fi-fi", language="fi", date_from=date(2026, 10, 1)),
        )
        await adapter.search("python", 5, options=SearchOptions(region="wt-wt"))
        await adapter.search("python", 5)

        assert searcher.kwargs[0] == {
            "gl": "fi",
            "hl": "fi",
            "date_from": date(2026, 10, 1),
            "date_to": None,
        }
        # No region is no country: Google keeps its default.
        assert searcher.kwargs[1].get("gl") is None
        assert searcher.kwargs[2] == {}
