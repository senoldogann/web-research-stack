"""A chain of fallbacks: an HTTP provider before the headless browser, each with its own cooldown."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings


class Scripted:
    """Answers with results, or refuses as told; ``headless`` marks the browser-driven one."""

    def __init__(
        self,
        name: str,
        *,
        refuse: Optional[str] = None,
        headless: bool = False,
        stall: float = 0.0,
    ) -> None:
        self.name = name
        self.refuse = refuse
        self.headless = headless
        self.stall = stall
        self.calls: list[str] = []

    async def search(
        self, query: str, limit: int, *, date_filter: Optional[str] = None
    ) -> list[SearchResult]:
        self.calls.append(query)
        if self.stall:
            await asyncio.sleep(self.stall)
        if self.refuse is not None:
            raise WebToolError("search_failed", f"{self.name} refused", detail=self.refuse)
        slug = query.replace(" ", "-")
        return [SearchResult(f"{query} guide", f"https://{self.name}.example/{slug}", query)]


def _service(*adapters: Scripted, **overrides: object) -> WebToolsService:
    values: dict[str, object] = {
        "search_provider": "duckduckgo",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": ["duckduckgo_lite", "google"],
    }
    values.update(overrides)
    return WebToolsService(
        settings=WebToolsSettings(**values),  # type: ignore[arg-type]
        fetcher=object(),
        search_adapters={adapter.name: adapter for adapter in adapters},  # type: ignore[misc]
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


def _trio(**kinds: str) -> tuple[Scripted, Scripted, Scripted]:
    return (
        Scripted("duckduckgo", refuse=kinds.get("ddg")),
        Scripted("duckduckgo_lite", refuse=kinds.get("lite")),
        Scripted("google", refuse=kinds.get("google"), headless=True),
    )


class TestSettings:
    def test_the_default_chain_is_lite_then_google(self) -> None:
        assert WebToolsSettings().search_fallback_providers == ["duckduckgo_lite", "google"]

    def test_the_chain_is_read_from_its_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS", " Google , duckduckgo_lite,google "
        )

        assert WebToolsSettings.from_env().search_fallback_providers == [
            "google",
            "duckduckgo_lite",
        ]

    def test_the_old_single_setting_is_still_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS", raising=False)
        monkeypatch.setenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDER", "google")

        assert WebToolsSettings.from_env().search_fallback_providers == ["google"]

    def test_the_chain_wins_over_the_old_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS", "duckduckgo_lite")
        monkeypatch.setenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDER", "google")

        assert WebToolsSettings.from_env().search_fallback_providers == ["duckduckgo_lite"]

    @pytest.mark.parametrize("value", ["none", "off", ""])
    def test_none_turns_the_chain_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS", value)

        assert WebToolsSettings.from_env().search_fallback_providers == []

    def test_the_old_setting_off_turns_the_chain_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS", raising=False)
        monkeypatch.setenv("WEB_TOOLS_SEARCH_FALLBACK_PROVIDER", "none")

        assert WebToolsSettings.from_env().search_fallback_providers == []

    @pytest.mark.asyncio
    async def test_the_service_registers_the_lite_adapter(self) -> None:
        service = WebToolsService.from_env()
        try:
            assert set(service.search_adapters) == {"duckduckgo", "duckduckgo_lite", "google"}
            assert service.search_adapters["google"].headless is True
        finally:
            await service.close()


class TestTheChain:
    @pytest.mark.asyncio
    async def test_the_first_fallback_that_answers_ends_the_chain(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected")
        service = _service(ddg, lite, google)

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "duckduckgo_lite"
        assert response["requested_provider"] == "duckduckgo"
        assert response["warnings"] == ["provider_fallback"]
        assert google.calls == []
        assert response["queries"][0]["fallback"] == {
            "from": "duckduckgo",
            "to": "duckduckgo_lite",
            "reason": "bot_detected",
            "error": None,
            "tried": [
                {"provider": "duckduckgo", "status": "failed", "reason": "bot_detected"},
                {"provider": "duckduckgo_lite", "status": "answered", "reason": None},
            ],
        }

    @pytest.mark.asyncio
    async def test_a_refusing_fallback_passes_the_search_down_the_chain(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected", lite="bot_detected")
        service = _service(ddg, lite, google)

        response = await service.search("oulu weather", depth="discover")

        assert response["provider"] == "google"
        base = response["queries"][0]["fallback"]
        assert base["to"] == "google" and base["error"] is None
        assert [(item["provider"], item["status"]) for item in base["tried"]] == [
            ("duckduckgo", "failed"),
            ("duckduckgo_lite", "failed"),
            ("google", "answered"),
        ]
        assert base["tried"][1]["reason"] == "bot_detected"

    @pytest.mark.asyncio
    async def test_a_cooling_provider_is_skipped_without_a_call(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected", lite="bot_detected")
        service = _service(ddg, lite, google)
        await service.search("oulu weather", depth="fast")
        ddg.refuse = lite.refuse = None

        response = await service.search("python asyncio", depth="fast")

        assert (len(ddg.calls), len(lite.calls)) == (1, 1)
        assert response["provider"] == "google"
        record = response["queries"][0]["fallback"]
        assert record["reason"] == "provider_cooldown"
        assert record["tried"] == [
            {"provider": "duckduckgo", "status": "skipped", "reason": "provider_cooldown"},
            {"provider": "duckduckgo_lite", "status": "skipped", "reason": "provider_cooldown"},
            {"provider": "google", "status": "answered", "reason": None},
        ]

    @pytest.mark.asyncio
    async def test_each_provider_keeps_its_own_gate_and_cooldown(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected")
        service = _service(ddg, lite, google)

        await service.search("oulu weather", depth="fast")

        assert service._provider_gate_for("duckduckgo") is not service._provider_gate_for(
            "duckduckgo_lite"
        )
        providers = (await service.metrics_snapshot())["search_providers"]
        assert providers["duckduckgo"]["consecutive_failures"] == 1
        assert providers["duckduckgo"]["cooldown_remaining_s"] > 0
        assert providers["duckduckgo_lite"]["consecutive_failures"] == 0
        assert providers["duckduckgo_lite"]["cooldown_remaining_s"] == 0

    @pytest.mark.asyncio
    async def test_a_name_without_an_adapter_is_left_out_of_the_chain(self) -> None:
        ddg, _, google = _trio(ddg="bot_detected")
        service = _service(ddg, google)

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "google"
        assert [item["provider"] for item in response["queries"][0]["fallback"]["tried"]] == [
            "duckduckgo",
            "google",
        ]

    @pytest.mark.asyncio
    async def test_every_provider_failing_reports_the_last_error_and_who_was_tried(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected", lite="rate", google="captcha")
        service = _service(ddg, lite, google)

        with pytest.raises(WebToolError) as exc:
            await service.search("oulu weather", depth="fast")

        assert exc.value.detail == "bot_detected"
        assert (len(ddg.calls), len(lite.calls), len(google.calls)) == (1, 1, 1)

    @pytest.mark.asyncio
    async def test_the_chain_off_leaves_the_primary_alone(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected")
        service = _service(ddg, lite, google, search_fallback_providers=[])

        with pytest.raises(WebToolError):
            await service.search("oulu weather", depth="fast")

        assert lite.calls == [] and google.calls == []


class TestFast:
    @pytest.mark.asyncio
    async def test_fast_takes_the_http_provider_before_the_browser(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected")
        # Configured the other way round, fast still asks the HTTP provider first.
        service = _service(
            ddg, lite, google, search_fallback_providers=["google", "duckduckgo_lite"]
        )

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "duckduckgo_lite"
        assert google.calls == []

    @pytest.mark.asyncio
    async def test_fast_asks_the_browser_only_after_every_http_provider_failed(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected", lite="bot_detected")
        service = _service(ddg, lite, google)

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "google"
        assert [len(ddg.calls), len(lite.calls), len(google.calls)] == [1, 1, 1]

    @pytest.mark.asyncio
    async def test_fast_does_not_wait_for_a_cooldown_or_the_browser(self) -> None:
        ddg, lite, google = _trio(ddg="bot_detected")
        google.stall = 5.0  # a cold headless browser
        service = _service(ddg, lite, google, search_provider_cooldown_seconds=300)
        await service.search("first search", depth="fast")  # starts ddg's cooldown
        started = time.perf_counter()

        response = await service.search("second search", depth="fast")

        assert time.perf_counter() - started < 1.0
        assert response["provider"] == "duckduckgo_lite"
        assert ddg.calls == ["first search"]
        assert google.calls == []
