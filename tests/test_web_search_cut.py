"""A cut angle says so: <angle>_query_cut, apart from a refusal's <angle>_query_failed."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Optional

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.providers import ProviderHealth
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

QUERY = "python asyncio"
YEAR = date.today().year


class PerAngle:
    """Answers each angle as told: a refusal, a stall, or results."""

    name = "fake"

    def __init__(
        self, refuse: tuple[str, ...] = (), stall: Optional[dict[str, float]] = None
    ) -> None:
        self.refuse = refuse
        self.stall = stall or {}
        self.calls: list[str] = []

    async def search(
        self, query: str, limit: int, *, date_filter: Optional[str] = None
    ) -> list[SearchResult]:
        self.calls.append(query)
        await asyncio.sleep(self.stall.get(query, 0.0))
        if query in self.refuse:
            raise WebToolError("search_failed", "refused", detail="bot_detected")
        slug = query.replace(" ", "-")
        return [SearchResult(f"{query} guide", f"https://{slug}.example/a", f"About {query}")]


def _service(adapter: PerAngle, **settings: object) -> WebToolsService:
    values: dict[str, object] = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": [],
        "search_provider_concurrency": 4,
        "search_angle_budget_ms": 150,
    }
    values.update(settings)
    return WebToolsService(
        settings=WebToolsSettings(**values),  # type: ignore[arg-type]
        fetcher=object(),
        search_adapters={"fake": adapter},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


@pytest.mark.asyncio
async def test_a_cut_angle_and_a_refused_one_have_their_own_warnings() -> None:
    adapter = PerAngle(
        refuse=(f"{QUERY} {YEAR}",),
        stall={f"{QUERY} history background": 1.0},
    )

    response = await _service(adapter).search(QUERY, depth="deep")

    assert "current_query_failed" in response["warnings"]
    assert "background_query_cut" in response["warnings"]
    assert "background_query_failed" not in response["warnings"]
    assert "current_query_cut" not in response["warnings"]
    by_angle = {report["angle"]: report for report in response["queries"]}
    assert by_angle["current"]["error"]["detail"] == "bot_detected"
    assert by_angle["background"]["error"]["detail"] == "angle_budget"
    assert by_angle["background"]["error"]["code"] == "timeout"
    assert by_angle["primary"]["error"] is None


@pytest.mark.asyncio
async def test_two_cut_angles_are_two_warnings_and_the_rest_is_ranked() -> None:
    adapter = PerAngle(
        stall={f"{QUERY} {YEAR}": 1.0, f"{QUERY} history background": 1.0},
    )

    response = await _service(adapter).search(QUERY, depth="discover")

    assert response["warnings"] == ["current_query_cut", "background_query_cut"]
    assert [item["url"] for item in response["results"]] == ["https://python-asyncio.example/a"]


@pytest.mark.asyncio
async def test_a_cut_starts_no_cooldown_and_a_refusal_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failures: list[str] = []
    original = ProviderHealth.record_failure

    def spy(self: ProviderHealth, base: float, ceiling: float) -> float:
        failures.append("failure")
        return original(self, base, ceiling)

    monkeypatch.setattr(ProviderHealth, "record_failure", spy)

    await _service(PerAngle(stall={f"{QUERY} {YEAR}": 1.0})).search(QUERY, depth="discover")
    assert failures == []  # the search's own limit is no fault of the provider

    await _service(PerAngle(refuse=(f"{QUERY} {YEAR}",))).search(QUERY, depth="discover")
    assert failures == ["failure"]


@pytest.mark.asyncio
async def test_the_base_angle_is_never_cut() -> None:
    adapter = PerAngle(stall={QUERY: 0.4})

    response = await _service(adapter).search(QUERY, depth="discover")

    assert "base_query_cut" not in response["warnings"]
    assert "base_query_failed" not in response["warnings"]
    assert response["queries"][0]["error"] is None and response["queries"][0]["results"] == 1


@pytest.mark.asyncio
async def test_a_budget_of_zero_waits_for_every_angle_and_cuts_none() -> None:
    adapter = PerAngle(stall={f"{QUERY} history background": 0.3})

    response = await _service(adapter, search_angle_budget_ms=0).search(QUERY, depth="discover")

    assert response["warnings"] == []
    assert all(report["error"] is None for report in response["queries"])


@pytest.mark.parametrize(("value", "kept"), [("200000", 120_000), ("-5", 0), ("7500", 7_500)])
def test_the_budget_is_a_setting_kept_between_0_and_120000(
    monkeypatch: pytest.MonkeyPatch, value: str, kept: int
) -> None:
    monkeypatch.setenv("WEB_TOOLS_SEARCH_ANGLE_BUDGET_MS", value)

    assert WebToolsSettings.from_env().search_angle_budget_ms == kept
    assert WebToolsSettings().search_angle_budget_ms == 5_000
