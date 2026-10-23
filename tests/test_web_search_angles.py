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
    service = _service(search, search_provider_concurrency=3, search_angle_budget_ms=300)

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
