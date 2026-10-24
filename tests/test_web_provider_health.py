"""Providers that heal: a record per provider, cooldowns after a wall, healthy ones first, web_health."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import pytest
from test_web_search_resilience import _results, _service

from web_scraper.mcp_server import McpServer
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.providers import (
    COOLING,
    ERROR,
    GOOD,
    HTTP_ERROR,
    POOR,
    SHAKY,
    TIMEOUT,
    WALL,
    ProviderHealth,
    failure_kind,
)
from web_scraper.web_tools.search import SearchResult, YahooSearchAdapter
from web_scraper.web_tools.settings import WebToolsSettings
from web_scraper.yahoo_search import YahooHttpError

MINUTE = 60.0


class Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _error(
    detail: str, *, code: str = "search_failed", status: Optional[int] = None
) -> WebToolError:
    return WebToolError(code, "failed", detail=detail, status_code=status)


class Fake:
    """A provider that answers or fails as told, and counts its calls."""

    def __init__(self, name: str, *, headless: bool = False) -> None:
        self.name = name
        self.headless = headless
        self.calls: list[str] = []
        self.error: Optional[Exception] = None
        self.stall = 0.0

    async def search(
        self, query: str, limit: int, *, date_filter: Optional[str] = None
    ) -> list[SearchResult]:
        self.calls.append(query)
        if self.stall:
            await asyncio.sleep(self.stall)
        if self.error is not None:
            raise self.error
        return _results(query, self.name)


def _wall() -> WebToolError:
    return _error("bot_detected")


class TestWhatAFailureIs:
    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (_error("bot_detected"), WALL),
            (_error("captcha"), WALL),
            (_error("access_blocked"), WALL),
            (WebToolError("blocked", "wall", detail="captcha"), WALL),
            (_error("anything", status=429), WALL),
            (_error("anything", status=403), WALL),
            (_error("search_timeout", code="timeout"), TIMEOUT),
            (WebToolError("timeout", "slow"), TIMEOUT),
            (_error("http_503", status=503), HTTP_ERROR),
            (_error("rate_limited", status=429), HTTP_ERROR),
            (_error("invalid_key", status=401), HTTP_ERROR),
            (_error("anything", status=500), HTTP_ERROR),
            (_error("RuntimeError"), ERROR),
            (_error("ParserError"), ERROR),
        ],
    )
    def test_a_failure_is_a_wall_a_timeout_an_http_error_or_another_exception(
        self, error: WebToolError, kind: str
    ) -> None:
        assert failure_kind(error) == kind


class TestTheCooldown:
    def _health(self) -> tuple[ProviderHealth, Clock]:
        clock = Clock()
        return ProviderHealth(clock=clock), clock

    def test_a_wall_starts_a_cooldown_of_ten_minutes_at_once(self) -> None:
        health, clock = self._health()

        started = health.record_failure(600, 3600, kind=WALL, limit=3, error=_wall())

        assert started == 600 and health.cooling_down()
        assert health.retry_after() == pytest.approx(600)
        clock.advance(599)
        assert health.cooling_down()
        clock.advance(2)
        assert not health.cooling_down()

    @pytest.mark.parametrize("kind", [TIMEOUT, HTTP_ERROR, ERROR])
    def test_other_failures_start_one_when_the_third_comes_in_a_row(self, kind: str) -> None:
        health, _ = self._health()

        first = health.record_failure(600, 3600, kind=kind, limit=3)
        second = health.record_failure(600, 3600, kind=kind, limit=3)
        assert (first, second) == (0.0, 0.0) and not health.cooling_down()
        third = health.record_failure(600, 3600, kind=kind, limit=3)

        assert third == 600 and health.cooling_down()

    def test_an_answer_between_failures_starts_the_count_again(self) -> None:
        health, _ = self._health()
        health.record_failure(600, 3600, kind=ERROR, limit=3)
        health.record_failure(600, 3600, kind=ERROR, limit=3)
        health.record_success(40.0)

        assert health.record_failure(600, 3600, kind=ERROR, limit=3) == 0.0
        assert health.consecutive_failures == 1

    def test_a_provider_that_comes_back_and_is_refused_cools_down_twice_as_long(self) -> None:
        health, clock = self._health()
        cooldowns = []
        for _ in range(5):
            cooldowns.append(health.record_failure(600, 3600, kind=WALL, limit=3))
            clock.advance(health.retry_after() + 1)

        assert cooldowns == [600, 1200, 2400, 3600, 3600]

    def test_an_answer_ends_it_all(self) -> None:
        health, clock = self._health()
        health.record_failure(600, 3600, kind=WALL, limit=3)
        clock.advance(601)

        health.record_success(30.0)

        assert (health.consecutive_failures, health.trips, health.cooldown_until) == (0, 0, 0.0)
        assert health.record_failure(600, 3600, kind=WALL, limit=3) == 600

    def test_a_base_of_zero_turns_cooldowns_off(self) -> None:
        health, _ = self._health()

        assert health.record_failure(0, 3600, kind=WALL, limit=1) == 0.0
        assert not health.cooling_down()

    def test_the_old_call_still_means_every_failure_cools(self) -> None:
        health, _ = self._health()

        assert [health.record_failure(5, 30) for _ in range(5)] == [5, 10, 20, 30, 30]


class TestStanding:
    def test_a_provider_with_a_short_record_is_good_whatever_it_says(self) -> None:
        health = ProviderHealth(clock=Clock())
        for _ in range(3):
            health.record_failure(0, 0, kind=ERROR)

        assert health.success_rate(1800) is None and health.standing() == GOOD

    def test_a_provider_with_no_calls_or_mostly_answers_is_good(self) -> None:
        health = ProviderHealth(clock=Clock())
        assert health.standing() == GOOD
        for _ in range(8):
            health.record_success(10.0)
        health.record_failure(600, 3600, kind=ERROR, limit=3)
        assert health.success_rate(1800) == pytest.approx(8 / 9)
        assert health.standing() == GOOD

    def test_a_provider_that_answers_half_is_shaky_and_one_that_mostly_fails_is_poor(self) -> None:
        shaky, poor = ProviderHealth(clock=Clock()), ProviderHealth(clock=Clock())
        for ok in (True, False, True, False):
            (shaky.record_success if ok else lambda: shaky.record_failure(0, 0, kind=ERROR))()
        for ok in (True, False, False, False):
            (poor.record_success if ok else lambda: poor.record_failure(0, 0, kind=ERROR))()

        assert (shaky.standing(), poor.standing()) == (SHAKY, POOR)

    def test_old_calls_fall_out_of_the_window(self) -> None:
        clock = Clock()
        health = ProviderHealth(clock=clock)
        for _ in range(4):
            health.record_failure(0, 0, kind=ERROR)
        assert health.standing(1800) == POOR

        clock.advance(1801)

        assert health.standing(1800) == GOOD

    def test_a_cooling_provider_is_cooling_and_comes_back_on_probation(self) -> None:
        health, clock = ProviderHealth(clock=Clock()), None
        clock = health.clock
        health.record_success(10.0)
        health.record_failure(600, 3600, kind=WALL, limit=3)
        assert health.standing() == COOLING

        clock.advance(601)  # type: ignore[attr-defined]

        # The cooldown was the penalty: the probe after it starts with a clean record.
        assert health.standing() == GOOD and health.consecutive_failures == 1
        assert health.record_failure(600, 3600, kind=WALL, limit=3) == 1200


class TestTheReport:
    def test_the_report_says_state_calls_block_rate_latency_and_the_last_error(self) -> None:
        clock = Clock()
        health = ProviderHealth(clock=clock)
        health.record_success(100.0)
        health.record_success(300.0)
        clock.advance(30)
        health.record_failure(600, 3600, kind=WALL, limit=3, error=_wall(), latency_ms=50.0)
        clock.advance(10)

        report = health.report()

        assert report["state"] == "cooling" and report["cooldown_remaining_s"] == pytest.approx(590)
        assert report["calls"] == 3 and report["ok"] == 2 and report["failures"] == 1
        assert report["block_rate"] == pytest.approx(0.333, abs=0.001)
        assert report["mean_latency_ms"] == pytest.approx(150.0)
        assert report["last_error"] == {
            "kind": "wall",
            "code": "search_failed",
            "detail": "bot_detected",
            "status_code": None,
            "seconds_ago": 10.0,
        }
        assert report["last_ok_seconds_ago"] == 40.0

    def test_a_provider_never_called_reports_nothing_to_report(self) -> None:
        report = ProviderHealth().report()

        assert report["state"] == "ok" and report["calls"] == 0
        assert report["last_error"] is None and report["mean_latency_ms"] is None
        assert "cooldown_remaining_s" not in report

    def test_a_provider_that_answers_half_of_its_calls_is_degraded(self) -> None:
        health = ProviderHealth()
        for _ in range(2):
            health.record_success(1.0)
            health.record_failure(600, 3600, kind=ERROR, limit=3)

        assert health.report()["state"] == "degraded"


class TestAProviderThatWalls:
    """The service walks the chain, skips a provider that is cooling, and asks it again when it is over."""

    def _trio(self) -> tuple[Fake, Fake, Fake]:
        return Fake("duckduckgo"), Fake("duckduckgo_lite"), Fake("yahoo")

    def _service(self, *adapters: Fake, **overrides: object):
        return _service(
            *adapters,
            search_fallback_providers=["duckduckgo_lite", "yahoo"],
            **overrides,
        )

    @pytest.mark.asyncio
    async def test_after_a_wall_the_provider_is_not_asked_again_for_ten_minutes(self) -> None:
        ddg, lite, yahoo = self._trio()
        ddg.error = _wall()
        service = self._service(ddg, lite, yahoo)

        first = await service.search("oulu weather", depth="fast")
        for query in ("python asyncio", "rust borrow checker", "finnish tax card"):
            later = await service.search(query, depth="fast")

        assert len(ddg.calls) == 1  # the wall, and not one call more
        assert first["provider"] == later["provider"] == "duckduckgo_lite"
        assert later["queries"][0]["fallback"]["tried"][0] == {
            "provider": "duckduckgo",
            "status": "skipped",
            "reason": "provider_cooldown",
        }
        health = service.health()
        by_name = {entry["name"]: entry for entry in health["providers"]}
        assert by_name["duckduckgo"]["state"] == "cooling"
        assert 590 < by_name["duckduckgo"]["cooldown_remaining_s"] <= 600
        assert by_name["duckduckgo"]["block_rate"] == 1.0
        assert health["order"] == ["duckduckgo_lite", "yahoo", "duckduckgo"]

    @pytest.mark.asyncio
    async def test_when_the_cooldown_is_over_the_provider_is_asked_again_and_heals(self) -> None:
        ddg, lite, yahoo = self._trio()
        ddg.error = _wall()
        service = self._service(ddg, lite, yahoo)
        await service.search("oulu weather", depth="fast")
        ddg.error = None
        service._provider_health_for("duckduckgo").cooldown_until = time.monotonic() - 1

        response = await service.search("python asyncio", depth="fast")

        assert len(ddg.calls) == 2 and response["provider"] == "duckduckgo"
        assert "provider_fallback" not in response["warnings"]
        health = service._provider_health_for("duckduckgo")
        assert (health.consecutive_failures, health.trips) == (0, 0) and not health.cooling_down()
        assert service.health()["order"][0] == "duckduckgo"

    @pytest.mark.asyncio
    async def test_a_probe_that_is_walled_again_starts_a_longer_cooldown(self) -> None:
        ddg, lite, yahoo = self._trio()
        ddg.error = _wall()
        service = self._service(ddg, lite, yahoo)
        await service.search("oulu weather", depth="fast")
        service._provider_health_for("duckduckgo").cooldown_until = time.monotonic() - 1

        await service.search("python asyncio", depth="fast")

        assert len(ddg.calls) == 2
        assert 1190 < service._provider_health_for("duckduckgo").retry_after() <= 1200

    @pytest.mark.asyncio
    async def test_a_cooldown_that_is_not_the_default_is_the_one_set(self) -> None:
        ddg, lite, yahoo = self._trio()
        ddg.error = _wall()
        service = self._service(ddg, lite, yahoo, search_provider_cooldown_seconds=90)

        await service.search("oulu weather", depth="fast")

        assert 80 < service._provider_health_for("duckduckgo").retry_after() <= 90

    @pytest.mark.asyncio
    async def test_one_timeout_cools_nothing_and_the_third_in_a_row_does(self) -> None:
        ddg, lite, yahoo = self._trio()
        ddg.stall = 1.0
        service = self._service(ddg, lite, yahoo)
        service.settings.search_timeout_seconds = 0.05

        await service.search("one", depth="fast")
        assert not service._provider_health_for("duckduckgo").cooling_down()
        await service.search("two", depth="fast")
        await service.search("three", depth="fast")

        assert service._provider_health_for("duckduckgo").cooling_down()
        report = service.health()["providers"][0]
        assert report["last_error"]["kind"] == "timeout" and report["block_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_a_cut_angle_is_no_failure_of_the_provider(self) -> None:
        ddg, lite, yahoo = self._trio()
        service = self._service(ddg, lite, yahoo)

        await service.search("oulu weather", depth="fast")

        report = service.health()["providers"][0]
        assert report["calls"] == 1 and report["failures"] == 0 and report["last_error"] is None


class TestTheOrderOfAsking:
    def _service(self, *adapters: Fake, **overrides: object):
        return _service(
            *adapters,
            search_fallback_providers=["duckduckgo_lite", "yahoo", "google"],
            **overrides,
        )

    def _record(self, service: Any, name: str, *outcomes: bool) -> None:
        health = service._provider_health_for(name)
        for ok in outcomes:
            if ok:
                health.record_success(10.0)
            else:
                health.record_failure(600, 3600, kind=ERROR, limit=3)

    def test_healthier_providers_go_first_and_ties_keep_the_configured_order(self) -> None:
        names = ("duckduckgo", "duckduckgo_lite", "yahoo", "google")
        service = self._service(*(Fake(name, headless=name == "google") for name in names))
        self._record(service, "duckduckgo_lite", True, False, True, False)  # shaky
        self._record(service, "yahoo", True, True, True)  # good

        assert service._fallback_chain("duckduckgo") == ["yahoo", "google", "duckduckgo_lite"]

    def test_a_cooling_provider_keeps_its_place_and_is_skipped_there(self) -> None:
        names = ("duckduckgo", "duckduckgo_lite", "yahoo", "google")
        service = self._service(*(Fake(name, headless=name == "google") for name in names))
        service._provider_health_for("duckduckgo_lite").record_failure(600, 3600, kind=WALL)
        self._record(service, "yahoo", True, False, True, False)  # shaky

        # lite stays in its slot; google (good) takes yahoo's, and shaky yahoo comes after it.
        assert service._fallback_chain("duckduckgo") == ["duckduckgo_lite", "google", "yahoo"]

    def test_a_quick_search_asks_the_providers_without_a_browser_first(self) -> None:
        names = ("duckduckgo", "duckduckgo_lite", "yahoo", "google")
        service = self._service(*(Fake(name, headless=name == "google") for name in names))
        service.settings.search_fallback_providers = ["google", "duckduckgo_lite", "yahoo"]

        assert service._fallback_chain("duckduckgo") == ["google", "duckduckgo_lite", "yahoo"]
        assert service._fallback_chain("duckduckgo", fast=True) == [
            "duckduckgo_lite",
            "yahoo",
            "google",
        ]

    @pytest.mark.asyncio
    async def test_the_provider_asked_first_gives_way_to_one_in_better_standing(self) -> None:
        ddg, lite = Fake("duckduckgo"), Fake("duckduckgo_lite")
        service = self._service(ddg, lite)
        self._record(
            service, "duckduckgo", False, False, True, False, False, True
        )  # poor, not cooling

        response = await service.search("oulu weather", depth="fast")

        assert ddg.calls == [] and response["provider"] == "duckduckgo_lite"
        record = response["queries"][0]["fallback"]
        assert record["tried"][0] == {
            "provider": "duckduckgo",
            "status": "skipped",
            "reason": "provider_unhealthy",
        }

    @pytest.mark.asyncio
    async def test_a_poor_provider_is_still_asked_when_nothing_else_answers(self) -> None:
        ddg, lite = Fake("duckduckgo"), Fake("duckduckgo_lite")
        lite.error = _error("http_500", status=500)
        service = self._service(ddg, lite)
        self._record(service, "duckduckgo", False, False, True, False, False, True)

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "duckduckgo" and len(ddg.calls) == 1


class TestYahooFailsInItsOwnWay:
    class Searcher:
        def __init__(self, error: Exception) -> None:
            self.error = error

        async def search(self, query: str, **kwargs: object) -> list[dict]:
            raise self.error

    @pytest.mark.asyncio
    async def test_an_http_error_status_is_a_failure_with_its_status_not_a_wall(self) -> None:
        adapter = YahooSearchAdapter(self.Searcher(YahooHttpError(503)))  # type: ignore[arg-type]

        with pytest.raises(WebToolError) as exc:
            await adapter.search("x", 5)

        assert exc.value.status_code == 503 and exc.value.detail == "http_503"
        assert failure_kind(exc.value) == HTTP_ERROR

    @pytest.mark.asyncio
    async def test_a_bare_runtime_error_is_a_failure_and_the_search_goes_on(self) -> None:
        yahoo = YahooSearchAdapter(self.Searcher(RuntimeError("Yahoo answered HTTP 500")))  # type: ignore[arg-type]
        ddg = Fake("duckduckgo")
        ddg.error = _wall()
        lite = Fake("duckduckgo_lite")
        lite.error = _wall()
        google = Fake("google", headless=True)
        service = _service(
            ddg,
            lite,
            yahoo,
            google,
            search_fallback_providers=["duckduckgo_lite", "yahoo", "google"],
        )

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "google"
        tried = {item["provider"]: item for item in response["queries"][0]["fallback"]["tried"]}
        assert tried["yahoo"] == {"provider": "yahoo", "status": "failed", "reason": "RuntimeError"}
        health = service.health()
        by_name = {entry["name"]: entry for entry in health["providers"]}
        assert by_name["yahoo"]["last_error"]["kind"] == "error"
        assert by_name["yahoo"]["state"] == "ok" and by_name["yahoo"]["failures"] == 1
        assert by_name["duckduckgo"]["state"] == "cooling"

    @pytest.mark.asyncio
    async def test_three_http_errors_in_a_row_cool_it_down(self) -> None:
        yahoo = YahooSearchAdapter(self.Searcher(YahooHttpError(500)))  # type: ignore[arg-type]
        lite = Fake("duckduckgo_lite")
        service = _service(
            yahoo, lite, search_provider="yahoo", search_fallback_providers=["duckduckgo_lite"]
        )

        for query in ("one", "two", "three"):
            await service.search(query, depth="fast")

        assert service._provider_health_for("yahoo").cooling_down()
        assert service.health()["providers"][0]["last_error"]["detail"] == "http_500"


class TestWebHealth:
    @pytest.mark.asyncio
    async def test_it_lists_every_provider_with_its_state(self) -> None:
        ddg, lite = Fake("duckduckgo"), Fake("duckduckgo_lite")
        service = _service(ddg, lite, search_fallback_providers=["duckduckgo_lite"])
        ddg.error = _wall()
        await service.search("oulu weather", depth="fast")

        health = service.health()

        by_name = {entry["name"]: entry for entry in health["providers"]}
        assert by_name["duckduckgo"]["state"] == "cooling"
        assert by_name["duckduckgo"]["last_error"]["detail"] == "bot_detected"
        assert by_name["duckduckgo_lite"]["state"] == "ok"
        assert by_name["duckduckgo_lite"]["calls"] == 1 and by_name["duckduckgo_lite"]["ok"] == 1
        assert by_name["duckduckgo_lite"]["mean_latency_ms"] >= 0
        # A provider that needs a key and has none says so, and which variable it reads.
        assert by_name["brave"] == {
            "name": "brave",
            "state": "no_key",
            "key_env": "BRAVE_SEARCH_API_KEY",
        }
        assert by_name["serper"]["state"] == "no_key"
        assert health["calls"] == 2
        assert health["cooldown_seconds"] == 600 and health["failures_before_cooldown"] == 3

    @pytest.mark.asyncio
    async def test_it_makes_no_request_and_is_fast(self) -> None:
        ddg = Fake("duckduckgo")
        service = _service(ddg)

        started = time.perf_counter()
        for _ in range(200):
            service.health()

        assert ddg.calls == [] and time.perf_counter() - started < 1.0

    @pytest.mark.asyncio
    async def test_it_is_an_mcp_tool_with_no_arguments(self) -> None:
        ddg = Fake("duckduckgo")
        server = McpServer(_service(ddg))

        listing = await server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(tool for tool in listing["result"]["tools"] if tool["name"] == "web_health")
        called = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "web_health", "arguments": {}},
            }
        )
        refused = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "web_health", "arguments": {"verbose": True}},
            }
        )

        assert (
            tool["inputSchema"]["properties"] == {} and tool["annotations"]["readOnlyHint"] is True
        )
        payload = json.loads(called["result"]["content"][0]["text"])
        assert [entry["name"] for entry in payload["providers"]][0] == "duckduckgo"
        assert refused["result"]["isError"] is True


class TestSettings:
    def test_the_defaults_are_ten_minutes_an_hour_three_failures_and_fifteen_seconds(self) -> None:
        settings = WebToolsSettings()

        assert settings.search_provider_cooldown_seconds == 600
        assert settings.search_provider_max_cooldown_seconds == 3600
        assert settings.search_provider_failure_limit == 3
        assert settings.search_primary_budget_ms == 15_000

    def test_the_environment_sets_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER_COOLDOWN_SECONDS", "120")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER_MAX_COOLDOWN_SECONDS", "60")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_PROVIDER_FAILURE_LIMIT", "0")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_PRIMARY_BUDGET_MS", "9000")

        settings = WebToolsSettings.from_env()

        assert settings.search_provider_cooldown_seconds == 120
        assert settings.search_provider_max_cooldown_seconds == 120  # never under the cooldown
        assert settings.search_provider_failure_limit == 1
        assert settings.search_primary_budget_ms == 9_000
