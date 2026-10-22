"""Tests for search provider pacing, Google fallback, and per-provider cooldown."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.providers import ProviderGate, ProviderHealth
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

QUERIES = [
    "oulu weather",
    "python asyncio",
    "finnish tax card",
    "eu ai act timeline",
    "rust borrow checker",
    "kela housing allowance",
]


def _results(query: str, source: str) -> list[SearchResult]:
    slug = query.replace(" ", "-")
    return [
        SearchResult(f"{query} guide", f"https://{source}.example/{slug}/a", f"About {query}"),
        SearchResult(f"{query} notes", f"https://{source}.example/{slug}/b", f"More {query}"),
    ]


class OverlapRefusingSearch:
    """Refuses a call that starts while another is in flight, as DuckDuckGo did."""

    name = "duckduckgo"

    def __init__(self, delay: float = 0.005) -> None:
        self.delay = delay
        self.in_flight = 0
        self.calls = 0
        self.refused = 0

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        self.calls += 1
        if self.in_flight:
            self.refused += 1
            raise WebToolError("search_failed", "refused", detail="bot_detected")
        self.in_flight += 1
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return _results(query, "ddg")


class ScriptedSearch:
    """Answers with results, or raises / stalls as told."""

    def __init__(
        self,
        name: str,
        *,
        error: Optional[WebToolError] = None,
        stall: float = 0.0,
    ) -> None:
        self.name = name
        self.error = error
        self.stall = stall
        self.calls: list[str] = []

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        self.calls.append(query)
        if self.stall:
            await asyncio.sleep(self.stall)
        if self.error is not None:
            raise self.error
        return _results(query, self.name)


def _bot_detected() -> WebToolError:
    return WebToolError("search_failed", "DuckDuckGo refused the search", detail="bot_detected")


def _service(*adapters: object, **overrides: object) -> WebToolsService:
    values: dict[str, object] = {
        "search_provider": "duckduckgo",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
    }
    values.update(overrides)
    return WebToolsService(
        settings=WebToolsSettings(**values),  # type: ignore[arg-type]
        fetcher=object(),
        search_adapters={adapter.name: adapter for adapter in adapters},  # type: ignore[attr-defined]
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


class TestProviderPacing:
    @pytest.mark.asyncio
    async def test_parallel_searches_reach_the_provider_one_at_a_time(self) -> None:
        ddg = OverlapRefusingSearch()
        service = _service(ddg)

        responses = await asyncio.gather(*(service.search(query) for query in QUERIES))

        assert ddg.refused == 0
        assert ddg.calls == 18  # six searches, three discover angles each
        for response in responses:
            assert response["provider"] == "duckduckgo"
            assert response["warnings"] == []
            assert all(report["error"] is None for report in response["queries"])

    @pytest.mark.asyncio
    async def test_unpaced_parallel_searches_are_refused(self) -> None:
        # The same provider without the per-provider slot: what live DuckDuckGo did.
        ddg = OverlapRefusingSearch()
        service = _service(ddg, search_provider_concurrency=6)

        await asyncio.gather(
            *(service.search(query) for query in QUERIES),
            return_exceptions=True,
        )

        assert ddg.refused > 0

    @pytest.mark.asyncio
    async def test_gate_spaces_calls_by_the_jitter_range(self) -> None:
        gate = ProviderGate(concurrency=1, min_gap_ms=40, max_gap_ms=60)
        starts: list[float] = []

        async def call() -> None:
            async with gate.turn():
                await gate.pace()
                starts.append(time.monotonic())
                gate.finish()

        await asyncio.gather(call(), call(), call())

        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        assert all(gap >= 0.035 for gap in gaps)

    def test_settings_default_to_one_call_and_250_to_500_ms(self) -> None:
        settings = WebToolsSettings()

        assert settings.search_provider_concurrency == 1
        assert (settings.search_jitter_min_ms, settings.search_jitter_max_ms) == (250, 500)
        assert settings.search_fallback_provider == "google"
        assert settings.search_provider_cooldown_seconds == 5

    def test_settings_read_pacing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER_CONCURRENCY", "2")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_JITTER_MIN_MS", "100")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_JITTER_MAX_MS", "50")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDER", "none")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER_COOLDOWN_SECONDS", "7")

        settings = WebToolsSettings.from_env()

        assert settings.search_provider_concurrency == 2
        assert (settings.search_jitter_min_ms, settings.search_jitter_max_ms) == (100, 100)
        assert settings.search_fallback_provider == ""
        assert settings.search_provider_cooldown_seconds == 7


class TestGoogleFallback:
    @pytest.mark.asyncio
    async def test_bot_detected_query_is_answered_by_google(self) -> None:
        ddg = ScriptedSearch("duckduckgo", error=_bot_detected())
        google = ScriptedSearch("google")
        service = _service(ddg, google)

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "google"
        assert response["requested_provider"] == "duckduckgo"
        assert [item["url"] for item in response["results"]] == [
            "https://google.example/oulu-weather/a",
            "https://google.example/oulu-weather/b",
        ]
        assert response["warnings"] == ["provider_fallback"]
        report = response["queries"][0]
        assert report["provider"] == "google"
        assert report["error"] is None
        assert report["fallback"] == {
            "from": "duckduckgo",
            "to": "google",
            "reason": "bot_detected",
            "error": None,
        }
        assert google.calls == ["oulu weather"]

    @pytest.mark.asyncio
    async def test_timed_out_query_is_answered_by_google(self) -> None:
        ddg = ScriptedSearch("duckduckgo", stall=1.0)
        google = ScriptedSearch("google")
        service = _service(ddg, google)
        service.settings.search_timeout_seconds = 0.05

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "google"
        assert response["queries"][0]["fallback"]["reason"] == "search_timeout"

    @pytest.mark.asyncio
    async def test_fallback_gets_its_own_longer_timeout(self) -> None:
        # A cold headless Google took 36 s live, past the 30 s call timeout.
        ddg = ScriptedSearch("duckduckgo", error=_bot_detected())
        google = ScriptedSearch("google", stall=0.2)
        service = _service(ddg, google)
        service.settings.search_timeout_seconds = 0.05
        service.settings.search_fallback_timeout_seconds = 1.0

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "google"
        assert WebToolsSettings().search_fallback_timeout_seconds == 60.0

    @pytest.mark.asyncio
    async def test_fallback_is_tried_once_per_query(self) -> None:
        ddg = ScriptedSearch("duckduckgo", error=_bot_detected())
        google = ScriptedSearch(
            "google",
            error=WebToolError("search_failed", "Google refused the search", detail="captcha"),
        )
        service = _service(ddg, google)

        with pytest.raises(WebToolError) as exc:
            await service.search("oulu weather", depth="fast")

        assert exc.value.detail == "bot_detected"
        assert len(ddg.calls) == 1
        assert len(google.calls) == 1

    @pytest.mark.asyncio
    async def test_failed_fallback_is_reported_on_the_angle(self) -> None:
        class BaseOnlyRefused(ScriptedSearch):
            async def search(self, query: str, limit: int) -> list[SearchResult]:
                self.calls.append(query)
                if query == "asyncio":
                    raise _bot_detected()
                return _results(query, self.name)

        ddg = BaseOnlyRefused("duckduckgo")
        google = ScriptedSearch(
            "google",
            error=WebToolError("search_failed", "Google refused the search", detail="captcha"),
        )
        service = _service(ddg, google, search_provider_cooldown_seconds=0)

        response = await service.search("asyncio", limit=5)

        base = response["queries"][0]
        assert base["error"]["detail"] == "bot_detected"
        assert base["fallback"]["error"]["detail"] == "captcha"
        assert response["provider"] == "duckduckgo"
        assert "base_query_failed" in response["warnings"]
        assert "provider_fallback" not in response["warnings"]

    @pytest.mark.asyncio
    async def test_no_fallback_without_a_registered_google_adapter(self) -> None:
        ddg = ScriptedSearch("duckduckgo", error=_bot_detected())
        service = _service(ddg)

        with pytest.raises(WebToolError) as exc:
            await service.search("oulu weather", depth="fast")

        assert exc.value.detail == "bot_detected"

    @pytest.mark.asyncio
    async def test_google_as_requested_provider_has_no_fallback(self) -> None:
        ddg = ScriptedSearch("duckduckgo")
        google = ScriptedSearch("google", error=_bot_detected())
        service = _service(ddg, google)

        with pytest.raises(WebToolError):
            await service.search("oulu weather", provider="google", depth="fast")

        assert ddg.calls == []


class TestProviderCooldown:
    @pytest.mark.asyncio
    async def test_cooling_provider_is_skipped_for_the_fallback(self) -> None:
        ddg = ScriptedSearch("duckduckgo", error=_bot_detected())
        google = ScriptedSearch("google")
        service = _service(ddg, google)

        await service.search("oulu weather", depth="fast")
        ddg.error = None
        response = await service.search("python asyncio", depth="fast")

        assert ddg.calls == ["oulu weather"]
        assert response["provider"] == "google"
        assert response["queries"][0]["fallback"]["reason"] == "provider_cooldown"
        snapshot = await service.metrics_snapshot()
        assert snapshot["search_providers"]["duckduckgo"]["consecutive_failures"] == 1
        assert 0 < snapshot["search_providers"]["duckduckgo"]["cooldown_remaining_s"] <= 5
        assert snapshot["search_providers"]["google"]["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_queued_calls_behind_a_failure_go_to_the_fallback(self) -> None:
        class RefuseFirst(ScriptedSearch):
            async def search(self, query: str, limit: int) -> list[SearchResult]:
                self.calls.append(query)
                await asyncio.sleep(0.01)
                if len(self.calls) == 1:
                    raise _bot_detected()
                return _results(query, self.name)

        ddg = RefuseFirst("duckduckgo")
        google = ScriptedSearch("google")
        service = _service(ddg, google)

        responses = await asyncio.gather(
            *(service.search(query, depth="fast") for query in QUERIES)
        )

        assert len(ddg.calls) == 1
        assert {response["provider"] for response in responses} == {"google"}

    @pytest.mark.asyncio
    async def test_cooling_provider_is_still_asked_when_the_fallback_fails(self) -> None:
        ddg = ScriptedSearch("duckduckgo", error=_bot_detected())
        google = ScriptedSearch("google", error=_bot_detected())
        service = _service(ddg, google)
        with pytest.raises(WebToolError):
            await service.search("oulu weather", depth="fast")

        ddg.error = None
        google.error = None
        google_health = service._provider_health_for("google")
        google_health.cooldown_until = 0.0
        google.error = WebToolError("search_failed", "Google refused", detail="captcha")
        response = await service.search("python asyncio", depth="fast")

        assert response["provider"] == "duckduckgo"
        assert response["queries"][0]["fallback"]["reason"] == "provider_cooldown"
        assert response["queries"][0]["fallback"]["error"]["detail"] == "captcha"

    def test_cooldown_starts_at_the_base_and_doubles_to_the_cap(self) -> None:
        health = ProviderHealth()

        cooldowns = [health.record_failure(5, 30) for _ in range(5)]

        assert cooldowns == [5, 10, 20, 30, 30]
        assert health.cooling_down()
        health.record_success()
        assert health.consecutive_failures == 0
        assert not health.cooling_down()

    def test_zero_base_disables_the_cooldown(self) -> None:
        health = ProviderHealth()

        assert health.record_failure(0, 300) == 0.0
        assert not health.cooling_down()
