"""A search's angles after the base run together, through the provider's gate."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest
from test_web_search_resilience import OverlapRefusingSearch, _results, _service

from web_scraper.web_tools.search import SearchResult

QUERY = "python asyncio"
ANGLES = ["base", "current", "background", "primary"]
DELAY = 0.2


class TimedSearch:
    """Answers after a delay per angle and records when each call ran."""

    name = "duckduckgo"

    def __init__(self, delays: Optional[dict[str, float]] = None) -> None:
        self.delays = delays or {}
        self.in_flight = 0
        self.peak = 0
        self.spans: dict[str, tuple[float, float]] = {}

    async def search(
        self, query: str, limit: int, *, date_filter: Optional[str] = None
    ) -> list[SearchResult]:
        angle = _angle_of(query)
        started = time.perf_counter()
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delays.get(angle, DELAY))
        finally:
            self.in_flight -= 1
        self.spans[angle] = (started, time.perf_counter())
        # Every angle shares one URL, so the merge order decides its snippet.
        return [
            SearchResult(f"{query} shared", "https://ddg.example/shared", f"Seen by {angle}"),
            *_results(query, "ddg"),
        ]


def _angle_of(query: str) -> str:
    if query.endswith("official documentation") or query.endswith("primary sources"):
        return "primary"
    if query.endswith("history background"):
        return "background"
    if query != QUERY:
        return "current"
    return "base"


@pytest.mark.asyncio
async def test_the_angles_after_the_base_run_together() -> None:
    search = TimedSearch()
    service = _service(search, search_provider_concurrency=3)

    started = time.perf_counter()
    result = await service.search(QUERY, depth="deep")
    elapsed = time.perf_counter() - started

    assert [query["angle"] for query in result["queries"]] == ANGLES
    # Sequential angles took 4 x 0.2 s; the base and then the other three take 2 x 0.2 s.
    assert elapsed < 3 * DELAY, elapsed
    assert search.peak == 3
    base_ended = search.spans["base"][1]
    assert all(search.spans[angle][0] >= base_ended for angle in ANGLES[1:])


@pytest.mark.asyncio
async def test_the_gate_still_lets_one_call_through_at_a_time() -> None:
    ddg = OverlapRefusingSearch(delay=0.01)
    service = _service(ddg)

    result = await service.search(QUERY, depth="deep")

    assert ddg.refused == 0
    assert ddg.calls == 4
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_results_reports_and_timings_keep_the_angle_order() -> None:
    in_order = _service(TimedSearch(), search_provider_concurrency=3)
    # The last angle answers first and the first non-base angle last.
    reversed_service = _service(
        TimedSearch({"current": 0.3, "background": 0.2, "primary": 0.05}),
        search_provider_concurrency=3,
    )

    expected = await in_order.search(QUERY, depth="deep")
    result = await reversed_service.search(QUERY, depth="deep")

    assert [query["angle"] for query in result["queries"]] == ANGLES
    assert [query["new"] for query in result["queries"]] == [
        query["new"] for query in expected["queries"]
    ]
    assert [name for name in result["timings"] if name.startswith("angle_")] == [
        f"angle_{angle}_ms" for angle in ANGLES
    ]
    assert result["results"] == expected["results"]
    assert result["timings"]["angle_primary_ms"] < result["timings"]["angle_current_ms"]


@pytest.mark.asyncio
async def test_an_angle_past_the_budget_is_cut_without_blaming_the_provider() -> None:
    # The primary angle stalls as a refused one does while the Google fallback starts.
    search = TimedSearch({"primary": 5.0})
    # A primary budget of 0 gives the primary angle no more than the side angles have.
    service = _service(
        search,
        search_provider_concurrency=3,
        search_angle_budget_ms=300,
        search_primary_budget_ms=0,
    )

    started = time.perf_counter()
    result = await service.search(QUERY, depth="deep")
    elapsed = time.perf_counter() - started

    assert elapsed < DELAY + 0.3 + 0.2, elapsed
    reports = {query["angle"]: query for query in result["queries"]}
    assert reports["primary"]["error"]["code"] == "timeout"
    assert reports["primary"]["error"]["detail"] == "angle_budget"
    assert reports["current"]["error"] is None and reports["background"]["error"] is None
    assert "primary_query_cut" in result["warnings"]
    assert "primary_query_failed" not in result["warnings"]  # that is for a refusal
    assert result["results"]
    # A cut is the search's own limit, not a provider failure: no cooldown follows.
    assert service._provider_health_for("duckduckgo").consecutive_failures == 0


@pytest.mark.asyncio
async def test_the_base_angle_is_never_cut() -> None:
    search = TimedSearch({"base": 0.5})
    service = _service(search, search_provider_concurrency=3, search_angle_budget_ms=300)

    result = await service.search(QUERY, depth="deep")

    assert [query["error"] for query in result["queries"]] == [None] * 4


@pytest.mark.asyncio
async def test_a_zero_budget_waits_for_every_angle() -> None:
    search = TimedSearch({"primary": 0.6})
    service = _service(search, search_provider_concurrency=3, search_angle_budget_ms=0)

    result = await service.search(QUERY, depth="deep")

    assert [query["error"] for query in result["queries"]] == [None] * 4


def test_the_budget_defaults_to_five_seconds_and_reads_its_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from web_scraper.web_tools.settings import WebToolsSettings

    assert WebToolsSettings().search_angle_budget_ms == 5_000
    monkeypatch.setenv("WEB_TOOLS_SEARCH_ANGLE_BUDGET_MS", "0")
    assert WebToolsSettings.from_env().search_angle_budget_ms == 0


class RecordingSearch(TimedSearch):
    """TimedSearch that also records the order the angles were asked in."""

    def __init__(self, delays: Optional[dict[str, float]] = None) -> None:
        super().__init__(delays)
        self.asked: list[str] = []

    async def search(
        self, query: str, limit: int, *, date_filter: Optional[str] = None
    ) -> list[SearchResult]:
        self.asked.append(_angle_of(query))
        return await super().search(query, limit, date_filter=date_filter)


@pytest.mark.asyncio
async def test_the_primary_angle_is_asked_before_the_side_angles() -> None:
    search = RecordingSearch({"base": 0.0, "current": 0.0, "background": 0.0, "primary": 0.0})
    service = _service(search)  # a gate of one call at a time

    result = await service.search(QUERY, depth="deep")

    assert search.asked == ["base", "primary", "current", "background"]
    # The answer keeps the angles' order whichever was asked first.
    assert [query["angle"] for query in result["queries"]] == ANGLES


@pytest.mark.asyncio
async def test_the_primary_angle_goes_on_after_the_side_angles_are_cut() -> None:
    # The side angles stall; the primary-source angle is slower than the side budget and quicker
    # than its own: it is the one that is waited for.
    search = RecordingSearch({"current": 5.0, "background": 5.0, "primary": 0.7})
    service = _service(
        search,
        search_provider_concurrency=3,
        search_angle_budget_ms=250,
        search_primary_budget_ms=3_000,
    )

    started = time.perf_counter()
    result = await service.search(QUERY, depth="deep")
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, elapsed
    reports = {query["angle"]: query for query in result["queries"]}
    assert reports["primary"]["error"] is None and reports["primary"]["results"] > 0
    assert reports["current"]["error"]["detail"] == "angle_budget"
    assert reports["background"]["error"]["detail"] == "angle_budget"
    assert "current_query_cut" in result["warnings"]
    assert "background_query_cut" in result["warnings"]
    # Which was cut is said, and the primary angle is not among them.
    assert "primary_query_cut" not in result["warnings"]
    assert "(0.25 s)" in reports["current"]["error"]["message"]
    assert service._provider_health_for("duckduckgo").consecutive_failures == 0


@pytest.mark.asyncio
async def test_the_primary_angle_is_cut_at_its_own_budget_and_says_so() -> None:
    search = RecordingSearch({"base": 0.0, "current": 0.0, "background": 0.0, "primary": 5.0})
    service = _service(
        search,
        search_provider_concurrency=3,
        search_angle_budget_ms=200,
        search_primary_budget_ms=600,
    )

    started = time.perf_counter()
    result = await service.search(QUERY, depth="deep")
    elapsed = time.perf_counter() - started

    assert 0.5 < elapsed < 1.5, elapsed
    reports = {query["angle"]: query for query in result["queries"]}
    assert reports["primary"]["error"]["detail"] == "angle_budget"
    assert "(0.6 s)" in reports["primary"]["error"]["message"]
    assert result["warnings"] == ["primary_query_cut"]
    assert result["results"]


@pytest.mark.asyncio
async def test_a_primary_budget_under_the_side_budget_is_the_side_budget() -> None:
    search = RecordingSearch({"primary": 5.0})
    service = _service(
        search,
        search_provider_concurrency=3,
        search_angle_budget_ms=300,
        search_primary_budget_ms=100,
    )

    started = time.perf_counter()
    await service.search(QUERY, depth="deep")

    assert time.perf_counter() - started < 1.0


@pytest.mark.asyncio
async def test_a_zero_side_budget_waits_for_every_angle_whatever_the_primary_budget_is() -> None:
    search = RecordingSearch({"primary": 0.4, "current": 0.4})
    service = _service(
        search,
        search_provider_concurrency=3,
        search_angle_budget_ms=0,
        search_primary_budget_ms=100,
    )

    result = await service.search(QUERY, depth="deep")

    assert [query["error"] for query in result["queries"]] == [None] * 4


def test_the_primary_budget_is_fifteen_seconds_and_reads_its_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from web_scraper.web_tools.settings import WebToolsSettings

    assert WebToolsSettings().search_primary_budget_ms == 15_000
    monkeypatch.setenv("WEB_TOOLS_SEARCH_PRIMARY_BUDGET_MS", "8000")
    assert WebToolsSettings.from_env().search_primary_budget_ms == 8_000
