"""Tests for honest empty searches: no unrelated results to fill the page."""

from __future__ import annotations

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.discovery import (
    MIN_RESULT_SCORE,
    DiscoveryCandidate,
    has_relevant_result,
    rank_candidates,
)
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

NONSENSE = "qzxvbnmp flurbulator 9237"
UNRELATED = [
    SearchResult("Best pizza in Oulu", "https://food.example/pizza", "Top restaurants"),
    SearchResult("Weather today", "https://weather.example/today", "Sunny and cold"),
    SearchResult("Form 9237 instructions", "https://forms.example/9237", "Filing form 9237"),
    SearchResult("Used cars", "https://cars.example/used", "Cheap cars near you"),
    SearchResult("Learn Python", "https://python.example/learn", "A tutorial"),
]


class StaticSearch:
    """Answers every query with the same results, or only the base query."""

    name = "fake"

    def __init__(self, results: list[SearchResult], *, only: str = "") -> None:
        self.results = results
        self.only = only

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        if self.only and query != self.only:
            return []
        return list(self.results)


def _service(results: list[SearchResult], *, only: str = "") -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(
            search_provider="fake",
            search_angle_delay_ms=0,
            search_jitter_min_ms=0,
            search_jitter_max_ms=0,
        ),
        fetcher=object(),
        search_adapters={"fake": StaticSearch(results, only=only)},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


def _candidate(url: str, title: str, rank: int = 0) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        result=SearchResult(title=title, url=url, snippet="", source="fake"),
        angle_ranks={"base": rank},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", ["fast", "discover", "deep"])
async def test_nonsense_query_returns_no_results_and_says_so(depth: str) -> None:
    response = await _service(UNRELATED).search(NONSENSE, depth=depth)

    assert response["results"] == []
    assert response["warnings"] == ["no_relevant_results"]
    assert response["facets"]["total_candidates"] == 5


@pytest.mark.asyncio
async def test_provider_answering_nothing_is_an_empty_result_not_an_error() -> None:
    response = await _service([]).search(NONSENSE, depth="fast")

    assert response["results"] == []
    assert response["warnings"] == ["no_relevant_results"]
    assert response["queries"][0]["results"] == 0


def test_a_shared_number_alone_is_not_relevance() -> None:
    items = [
        {"title": "Form 9237 instructions", "snippet": "", "url": "https://forms.example/9237"}
    ]

    assert not has_relevant_result(NONSENSE, items)
    assert has_relevant_result("9237", items)
    assert has_relevant_result("form 9237", items)


def test_low_scoring_relevant_result_is_dropped_beside_better_ones() -> None:
    query = "finnish housing allowance rules"
    weak = _candidate("https://blog.example.net/lapland", "Housing in Lapland", rank=15)
    strong = _candidate("https://kela.example/housing", "Finnish housing allowance rules")

    results = rank_candidates(query, [weak, strong], 5, year_now=2026)

    assert [item["url"] for item in results] == [strong.result.url]


def test_low_scoring_relevant_result_is_kept_when_nothing_scores_higher() -> None:
    query = "finnish housing allowance rules"
    weak = _candidate("https://blog.example.net/lapland", "Housing in Lapland", rank=15)

    results = rank_candidates(query, [weak], 5, year_now=2026)

    assert [item["url"] for item in results] == [weak.result.url]
    assert 0 < results[0]["relevance"] < 1
    assert results[0]["score"] < MIN_RESULT_SCORE


@pytest.mark.asyncio
async def test_only_weak_matches_are_flagged_as_low_relevance() -> None:
    junk = [
        SearchResult(f"Unrelated page {index}", f"https://junk.example/{index}", "Nothing")
        for index in range(15)
    ]
    weak = SearchResult("Housing in Lapland", "https://blog.example.net/lapland", "")

    query = "finnish housing allowance rules"

    response = await _service([*junk, weak], only=query).search(query)

    assert [item["url"] for item in response["results"]] == [weak.url]
    assert response["warnings"] == ["low_relevance_results"]


@pytest.mark.asyncio
async def test_fast_keeps_provider_order_but_drops_unrelated_results() -> None:
    weak = SearchResult("Housing in Lapland", "https://blog.example.net/lapland", "")

    response = await _service([*UNRELATED, weak]).search(
        "finnish housing allowance rules", depth="fast"
    )

    assert [item["url"] for item in response["results"]] == [weak.url]


@pytest.mark.asyncio
async def test_partial_match_in_another_language_is_still_a_result() -> None:
    results = [
        SearchResult("Vuokra-asunnot Oulu", "https://asunnot.example/oulu", "Vapaat asunnot"),
    ]

    response = await _service(results).search("oulu kiralık daire", depth="fast")

    assert [item["url"] for item in response["results"]] == ["https://asunnot.example/oulu"]
    assert "no_relevant_results" not in response["warnings"]
