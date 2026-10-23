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
