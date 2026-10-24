"""Brave Search API and Serper: each on only with its key, used first, DuckDuckGo behind them."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Callable

import httpx
import pytest
from test_web_provider_health import Fake, _wall
from test_web_search_resilience import _service

from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.paid_search import (
    PAID_PROVIDERS,
    BraveSearchAdapter,
    SerperSearchAdapter,
    build_paid_adapters,
)
from web_scraper.web_tools.search import SearchOptions
from web_scraper.web_tools.service import WebToolsService

SECRET = "sk-live-0123456789abcdefSECRET"
TODAY = date.today()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
THREE_DAYS = (TODAY - timedelta(days=3)).isoformat()

BRAVE_ANSWER = {
    "type": "search",
    "web": {
        "results": [
            {
                "title": "OpenAI signs <strong>Google Cloud</strong> deal",
                "url": "https://www.reuters.com/business/openai-signs-google-cloud-deal/",
                "description": "OpenAI will sell <strong>models</strong> &amp; more on Google Cloud.",
                "page_age": f"{YESTERDAY}T08:30:00",
                "age": "yesterday",
                "meta_url": {"hostname": "www.reuters.com"},
            },
            {
                "title": "Model page",
                "url": "https://openai.com/index/gpt-6/",
                "description": "Meet the model.",
                "age": "3 days ago",
            },
            {"title": "", "url": "https://skipped.example/", "description": "no title"},
        ]
    },
}
SERPER_ANSWER = {
    "organic": [
        {
            "title": "OpenAI signs Google Cloud deal",
            "link": "https://www.reuters.com/business/openai-google-cloud-2026-10-21/",
            "snippet": "OpenAI will sell models on Google Cloud.",
            "date": "Oct 21, 2026",
            "position": 1,
        },
        {
            "title": "Model page",
            "link": "https://openai.com/index/gpt-6/",
            "snippet": "Meet the model.",
            "date": "3 days ago",
            "position": 2,
        },
    ]
}


def _brave(handler: Callable[[httpx.Request], httpx.Response]) -> BraveSearchAdapter:
    return BraveSearchAdapter(SECRET, transport=httpx.MockTransport(handler))


def _serper(handler: Callable[[httpx.Request], httpx.Response]) -> SerperSearchAdapter:
    return SerperSearchAdapter(SECRET, transport=httpx.MockTransport(handler))


def _json(payload: object, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=payload)


class TestOnlyWithAKey:
    def test_no_key_no_adapter(self) -> None:
        assert build_paid_adapters({}) == {}
        assert build_paid_adapters({"BRAVE_SEARCH_API_KEY": "", "SERPER_API_KEY": "   "}) == {}

    def test_each_key_turns_on_its_own_adapter_and_brave_comes_first(self) -> None:
        only_serper = build_paid_adapters({"SERPER_API_KEY": SECRET})
        both = build_paid_adapters({"SERPER_API_KEY": SECRET, "BRAVE_SEARCH_API_KEY": SECRET})

        assert list(only_serper) == ["serper"]
        assert list(both) == ["brave", "serper"]
        assert [PAID_PROVIDERS[name].env for name in PAID_PROVIDERS] == [
            "BRAVE_SEARCH_API_KEY",
            "SERPER_API_KEY",
        ]

    @pytest.mark.asyncio
    async def test_the_service_from_the_environment_registers_only_what_has_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        monkeypatch.setenv("SERPER_API_KEY", SECRET)
        service = WebToolsService.from_env()
        try:
            assert set(service.search_adapters) == {
                "duckduckgo",
                "duckduckgo_lite",
                "yahoo",
                "google",
                "serper",
            }
            assert service._primary_provider(None) == "serper"
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_without_keys_nothing_changes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        service = WebToolsService.from_env()
        try:
            assert service._primary_provider(None) == "duckduckgo"
            assert service._fallback_chain("duckduckgo") == ["duckduckgo_lite", "yahoo", "google"]
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_a_call_that_names_a_provider_without_its_key_is_told_which_variable(
        self,
    ) -> None:
        service = _service(Fake("duckduckgo"))

        with pytest.raises(WebToolError) as exc:
            await service.search("oulu weather", provider="brave")

        assert exc.value.code == "invalid_request" and exc.value.detail == "no_key"
        assert "BRAVE_SEARCH_API_KEY" in str(exc.value)

    def test_an_empty_key_is_refused(self) -> None:
        with pytest.raises(ValueError):
            BraveSearchAdapter("  ")


class TestBrave:
    @pytest.mark.asyncio
    async def test_it_asks_with_the_key_in_a_header_and_never_in_the_address(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=BRAVE_ANSWER)

        adapter = _brave(handler)

        results = await adapter.search(
            "openai google cloud",
            5,
            date_filter="week",
            options=SearchOptions(region="fi-fi", language="fi"),
        )

        request = seen[0]
        assert request.method == "GET" and request.url.host == "api.search.brave.com"
        assert request.headers["X-Subscription-Token"] == SECRET
        assert SECRET not in str(request.url)
        params = dict(request.url.params)
        assert params["q"] == "openai google cloud" and params["count"] == "5"
        assert (
            params["freshness"] == "pw"
            and params["country"] == "FI"
            and params["search_lang"] == "fi"
        )
        assert params["text_decorations"] == "false"
        assert [item.title for item in results] == [
            "OpenAI signs Google Cloud deal",
            "Model page",
        ]
        await adapter.aclose()

    @pytest.mark.asyncio
    async def test_a_date_range_is_a_freshness_range(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=BRAVE_ANSWER)

        await _brave(handler).search(
            "x", 5, options=SearchOptions(date_from=date(2026, 10, 1), date_to=date(2026, 10, 20))
        )

        assert dict(seen[0].url.params)["freshness"] == "2026-10-01to2026-10-20"

    @pytest.mark.asyncio
    async def test_it_reads_titles_snippets_sources_and_the_dates_the_provider_gave(self) -> None:
        results = await _brave(_json(BRAVE_ANSWER)).search("x", 5)

        assert results[0].snippet == "OpenAI will sell models & more on Google Cloud."
        assert results[0].source == "reuters"
        assert results[0].published == YESTERDAY  # page_age
        assert results[1].published == THREE_DAYS  # "3 days ago"
        assert results[1].source == "openai"

    @pytest.mark.parametrize(
        ("status", "detail"),
        [(401, "invalid_key"), (403, "invalid_key"), (429, "rate_limited"), (500, "http_500")],
    )
    @pytest.mark.asyncio
    async def test_a_refusal_has_its_own_detail(self, status: int, detail: str) -> None:
        adapter = _brave(_json({"error": "no"}, status))

        with pytest.raises(WebToolError) as exc:
            await adapter.search("x", 5)

        assert exc.value.code == "search_failed" and exc.value.detail == detail
        assert exc.value.status_code == status

    @pytest.mark.asyncio
    async def test_a_timeout_and_a_page_that_is_not_json_are_failures(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(WebToolError) as timed_out:
            await _brave(slow).search("x", 5)
        with pytest.raises(WebToolError) as not_json:
            await _brave(lambda request: httpx.Response(200, text="<html>")).search("x", 5)

        assert (timed_out.value.code, timed_out.value.detail) == ("timeout", "search_timeout")
        assert not_json.value.detail == "bad_response"


class TestSerper:
    @pytest.mark.asyncio
    async def test_it_posts_the_query_with_the_key_in_a_header(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=SERPER_ANSWER)

        results = await _serper(handler).search(
            "openai google cloud",
            8,
            date_filter="day",
            options=SearchOptions(region="us-en", language="en"),
        )

        request = seen[0]
        assert request.method == "POST" and request.url.host == "google.serper.dev"
        assert request.headers["X-API-KEY"] == SECRET and SECRET not in str(request.url)
        body = json.loads(request.content)
        assert body == {
            "q": "openai google cloud",
            "num": 8,
            "gl": "us",
            "hl": "en",
            "tbs": "qdr:d",
        }
        assert [item.url for item in results] == [
            "https://www.reuters.com/business/openai-google-cloud-2026-10-21/",
            "https://openai.com/index/gpt-6/",
        ]
        assert results[0].published == "2026-10-21" and results[1].published == THREE_DAYS

    @pytest.mark.asyncio
    async def test_a_date_range_is_a_custom_range(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=SERPER_ANSWER)

        await _serper(handler).search(
            "x", 5, options=SearchOptions(date_from=date(2026, 10, 1), date_to=date(2026, 10, 20))
        )

        assert json.loads(seen[0].content)["tbs"] == "cdr:1,cd_min:10/1/2026,cd_max:10/20/2026"

    @pytest.mark.asyncio
    async def test_a_rejected_key_is_invalid_key(self) -> None:
        with pytest.raises(WebToolError) as exc:
            await _serper(_json({"message": "Unauthorized"}, 401)).search("x", 5)

        assert exc.value.detail == "invalid_key"


class TestTheKeyStaysSecret:
    def _hostile(self, request: httpx.Request) -> httpx.Response:
        # A server (or a proxy) that echoes the request back in an error.
        return httpx.Response(500, text=f"boom {dict(request.headers)} {request.url}")

    @pytest.mark.asyncio
    async def test_no_error_repr_or_answer_holds_the_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        brave = BraveSearchAdapter(SECRET, transport=httpx.MockTransport(self._hostile))
        serper = SerperSearchAdapter(SECRET, transport=httpx.MockTransport(self._hostile))
        ddg = Fake("duckduckgo")
        service = _service(
            ddg,
            brave,
            serper,
            search_fallback_providers=[],
        )

        answer = await service.search("oulu weather", depth="fast")
        errors = []
        for adapter in (brave, serper):
            with pytest.raises(WebToolError) as exc:
                await adapter.search("x", 5)
            errors.append(exc.value)

        everything = json.dumps(
            {
                "answer": answer,
                "health": service.health(),
                "errors": [error.to_dict() for error in errors],
                "text": [str(error) for error in errors],
                "repr": [repr(brave), repr(serper), repr(vars(brave)), repr(vars(serper))],
                "log": caplog.text,
                "metrics": await service.metrics_snapshot(),
            },
            default=str,
        )
        assert SECRET not in everything
        assert answer["provider"] == "duckduckgo"

    def test_the_adapter_shows_that_a_key_is_set_and_not_which(self) -> None:
        assert repr(BraveSearchAdapter(SECRET)) == "BraveSearchAdapter(key=<set>)"

    @pytest.mark.asyncio
    async def test_the_health_of_a_keyed_provider_names_no_key(self) -> None:
        brave = _brave(_json({"error": "no"}, 401))
        service = _service(Fake("duckduckgo"), brave)

        await service.search("oulu weather", depth="fast")
        health = service.health()

        entry = next(item for item in health["providers"] if item["name"] == "brave")
        assert entry["last_error"]["detail"] == "invalid_key" and "key_env" not in entry
        assert SECRET not in json.dumps(health)


class TestTheKeyedProviderGoesFirst:
    def _stack(self, brave_handler: Callable[[httpx.Request], httpx.Response]) -> Any:
        ddg, lite = Fake("duckduckgo"), Fake("duckduckgo_lite")
        brave = _brave(brave_handler)
        service = _service(ddg, lite, brave, search_fallback_providers=["duckduckgo_lite"])
        return service, ddg, lite

    @pytest.mark.asyncio
    async def test_a_search_asks_brave_first_and_duckduckgo_is_not_asked(self) -> None:
        service, ddg, lite = self._stack(_json(BRAVE_ANSWER))

        answer = await service.search("openai google cloud deal", depth="fast")

        assert answer["provider"] == "brave" and answer["requested_provider"] == "brave"
        assert ddg.calls == [] and lite.calls == []
        assert answer["warnings"] == []
        assert answer["results"][0]["date"] == YESTERDAY
        assert answer["results"][0]["date_source"] == "provider"

    @pytest.mark.asyncio
    async def test_duckduckgo_stays_behind_it_as_a_fallback(self) -> None:
        service, ddg, lite = self._stack(_json({"error": "no"}, 401))

        answer = await service.search("oulu weather", depth="fast")

        assert answer["provider"] == "duckduckgo" and answer["requested_provider"] == "brave"
        assert "provider_fallback" in answer["warnings"]
        assert answer["queries"][0]["fallback"]["reason"] == "invalid_key"
        assert [item["provider"] for item in answer["queries"][0]["fallback"]["tried"]] == [
            "brave",
            "duckduckgo",
        ]

    @pytest.mark.asyncio
    async def test_after_a_wall_or_three_refusals_brave_is_skipped_for_ten_minutes(self) -> None:
        service, ddg, lite = self._stack(_json({"error": "no"}, 401))

        for query in ("one", "two", "three", "four"):
            await service.search(query, depth="fast")

        brave = service._provider_health_for("brave")
        assert brave.calls == 3 and brave.cooling_down()
        assert service.health()["order"][-1] == "brave"

    @pytest.mark.asyncio
    async def test_a_call_that_names_a_provider_asks_it_first_and_brave_after(self) -> None:
        service, ddg, lite = self._stack(_json(BRAVE_ANSWER))
        ddg.error = _wall()
        lite.error = _wall()

        answer = await service.search(
            "openai google cloud deal", provider="duckduckgo", depth="fast"
        )

        assert ddg.calls and lite.calls
        assert answer["provider"] == "brave" and answer["requested_provider"] == "duckduckgo"

    @pytest.mark.asyncio
    async def test_brave_is_never_paced_faster_than_a_call_a_second(self) -> None:
        service, ddg, lite = self._stack(_json(BRAVE_ANSWER))

        brave_gate = service._provider_gate_for("brave")
        ddg_gate = service._provider_gate_for("duckduckgo")

        assert brave_gate._min_gap >= 1.1 and ddg_gate._min_gap == 0.0

    @pytest.mark.asyncio
    async def test_serper_comes_after_brave_and_before_duckduckgo(self) -> None:
        ddg = Fake("duckduckgo")
        brave = _brave(_json({"error": "no"}, 500))
        serper = _serper(_json(SERPER_ANSWER))
        service = _service(ddg, brave, serper, search_fallback_providers=[])

        answer = await service.search("openai google cloud deal", depth="fast")

        assert answer["provider"] == "serper" and ddg.calls == []
        assert [item["provider"] for item in answer["queries"][0]["fallback"]["tried"]] == [
            "brave",
            "serper",
        ]
        assert service.health()["order"] == ["brave", "serper", "duckduckgo"]

    @pytest.mark.asyncio
    async def test_the_results_carry_what_the_providers_dated(self) -> None:
        service, *_ = self._stack(_json(BRAVE_ANSWER))

        answer = await service.search("openai google cloud deal", depth="fast")

        by_domain = {item["domain"]: item for item in answer["results"]}
        assert by_domain["reuters.com"]["date"] == YESTERDAY
        assert by_domain["openai.com"]["date"] == THREE_DAYS
        assert by_domain["openai.com"]["date_source"] == "provider"
        assert by_domain["openai.com"]["date_confidence"] == pytest.approx(0.45)

    @pytest.mark.asyncio
    async def test_a_day_in_the_address_stands_and_the_providers_other_day_is_a_conflict(
        self,
    ) -> None:
        answer_with_day = {
            "web": {
                "results": [
                    {
                        "title": "OpenAI signs Google Cloud deal",
                        "url": "https://www.reuters.com/business/openai-google-cloud-2026-10-21/",
                        "description": "OpenAI sells models on Google Cloud.",
                        "page_age": f"{YESTERDAY}T08:30:00",
                    }
                ]
            }
        }
        service, *_ = self._stack(_json(answer_with_day))

        answer = await service.search("openai google cloud deal", depth="fast")

        item = answer["results"][0]
        assert item["date"] == "2026-10-21" and item["date_source"] == "url"
        assert item["date_conflicts"] == [{"source": "provider", "value": YESTERDAY}]


def test_the_module_that_holds_the_key_has_no_logging_in_it() -> None:
    import web_scraper.web_tools.paid_search as module

    with open(module.__file__, encoding="utf-8") as source:
        code = source.read()
    assert "logging" not in code and "print(" not in code
