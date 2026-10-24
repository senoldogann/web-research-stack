"""Yahoo as a plain-HTTP provider between DuckDuckGo lite and the headless browser.

Live probes of 2026-10-23, one a minute after the other, through an impersonating curl_cffi
session: Yahoo (``search.yahoo.com/search?p=``) answered HTTP 200 with parseable results (the
fixture below is that answer, scripts and styles stripped); Mojeek (``mojeek.com/search?q=``)
answered HTTP 403, "your network appears to be sending automated queries", so it is not added.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from test_web_search_chain import Scripted, _service

from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.search import (
    YahooSearchAdapter,
    build_search_adapter,
)
from web_scraper.web_tools.settings import DEFAULT_FALLBACK_PROVIDERS, WebToolsSettings
from web_scraper.yahoo_search import YahooBlocked, YahooSearcher

FIXTURE = Path(__file__).parent / "fixtures" / "yahoo_python_asyncio_tutorial.html"
SEARCH_URL = "https://search.yahoo.com/search"


class FakeResponse:
    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        headers: Optional[dict[str, str]] = None,
        url: str = SEARCH_URL,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url


class FakeClient:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], dict[str, str], bool]] = []

    async def get(
        self,
        url: str,
        params: Optional[dict[str, str]] = None,
        headers: Optional[dict[str, str]] = None,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        self.calls.append((url, dict(params or {}), dict(headers or {}), allow_redirects))
        return self.responses.pop(0)


def _fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _page(*urls: str) -> str:
    items = "".join(
        f'<div class="dd algo algo-sr"><div class="compTitle"><a href="{url}">'
        f'<h3 class="title"><span>Title {index}</span></h3></a></div>'
        f'<div class="compText"><p>Snippet {index}</p></div></div>'
        for index, url in enumerate(urls)
    )
    return f"<html><body>{items}</body></html>"


class TestParser:
    def test_it_reads_the_organic_results_of_the_live_fixture(self) -> None:
        results, _ = YahooSearcher()._parse_results(_fixture())

        assert len(results) == 7
        first = results[0]
        assert first["title"] == "Python's asyncio: A Hands-On Walkthrough – Real Python"
        assert first["url"] == "https://realpython.com/async-io-python/"
        assert first["snippet"].startswith(
            "Jul 30, 2025 · In this tutorial, you’ll learn how Python asyncio works"
        )
        assert first["source"] == "realpython"
        assert results[-1]["url"] == "https://superfastpython.com/python-asyncio/"
        assert [item["url"] for item in results[3:5]] == [
            "https://docs.python.org/3/library/asyncio.html",
            "https://docs.python.org/3/howto/a-conceptual-overview-of-asyncio.html",
        ]
        assert all(item["title"] and item["snippet"] for item in results)

    def test_a_result_is_never_a_yahoo_address(self) -> None:
        results, _ = YahooSearcher()._parse_results(_fixture())

        assert not any("yahoo.com" in item["url"] for item in results)

    def test_it_finds_the_next_page(self) -> None:
        _, next_url = YahooSearcher()._parse_results(_fixture())

        assert next_url is not None and next_url.startswith("https://search.yahoo.com/search;")
        assert "b=8" in next_url and "p=python+asyncio+tutorial" in next_url

    def test_a_page_without_results_has_no_results_and_no_next_page(self) -> None:
        assert YahooSearcher()._parse_results("<html><body></body></html>") == ([], None)

    @pytest.mark.parametrize(
        ("href", "target"),
        [
            (
                "https://r.search.yahoo.com/_ylt=A;_ylu=B/RV=2/RE=1793962389/RO=10/"
                "RU=https%3a%2f%2fa.example%2fx%3fq%3d1%26r%3d2/RK=2/RS=abc-",
                "https://a.example/x?q=1&r=2",
            ),
            ("https://b.example/direct", "https://b.example/direct"),
            ("/relative/path", None),
            ("javascript:void(0)", None),
            ("", None),
            ("https://search.yahoo.com/search?p=again", None),
        ],
    )
    def test_the_target_is_unwrapped_from_yahoos_redirect(
        self, href: str, target: Optional[str]
    ) -> None:
        assert YahooSearcher._target(href) == target


class TestRequest:
    @pytest.mark.asyncio
    async def test_a_search_is_one_get_with_the_browsers_own_headers(self) -> None:
        client = FakeClient(FakeResponse(_fixture()))
        searcher = YahooSearcher(language="fi-FI,fi;q=0.9", client=client)

        results = await searcher.search("python asyncio tutorial", num_results=5)

        assert len(results) == 5 and len(client.calls) == 1
        url, params, headers, follow = client.calls[0]
        assert url == SEARCH_URL
        assert params["p"] == "python asyncio tutorial" and params["ei"] == "UTF-8"
        assert follow is False  # the searcher follows only what stays on Yahoo
        assert {name.lower() for name in headers} == {"accept-language"}
        assert headers["accept-language"] == "fi-FI,fi;q=0.9"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("name", "param"), [("day", "d"), ("week", "w"), ("month", "m")])
    async def test_a_relative_date_filter_is_btf(self, name: str, param: str) -> None:
        client = FakeClient(FakeResponse(_fixture()))

        await YahooSearcher(client=client).search("news", 3, date_filter=name)

        assert client.calls[0][1]["btf"] == param

    @pytest.mark.asyncio
    async def test_a_year_has_no_filter_of_its_own(self) -> None:
        client = FakeClient(FakeResponse(_fixture()))

        await YahooSearcher(client=client).search("news", 3, date_filter="year")

        assert "btf" not in client.calls[0][1]

    @pytest.mark.asyncio
    async def test_a_second_page_is_the_link_yahoo_gave(self) -> None:
        second = _page("https://x.example/1", "https://realpython.com/async-io-python/")
        client = FakeClient(FakeResponse(_fixture()), FakeResponse(second))

        results = await YahooSearcher(client=client).search("python asyncio tutorial", 12)

        assert len(client.calls) == 2
        assert client.calls[1][0].startswith("https://search.yahoo.com/search;")
        assert client.calls[1][1] == {}  # the link carries its own query
        urls = [item["url"] for item in results]
        assert len(urls) == len(set(urls)) == 8  # the repeat is not counted twice
        assert urls[-1] == "https://x.example/1"

    @pytest.mark.asyncio
    async def test_it_stops_at_three_pages(self) -> None:
        pages = [FakeResponse(_fixture()) for _ in range(5)]
        client = FakeClient(*pages)

        await YahooSearcher(client=client).search("python asyncio tutorial", 50)

        assert len(client.calls) == 3


class TestRefusals:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [403, 429, 999])
    async def test_a_refusing_status_is_a_refusal(self, status: int) -> None:
        client = FakeClient(FakeResponse("denied", status_code=status))

        with pytest.raises(YahooBlocked):
            await YahooSearcher(client=client).search("anything", 5)

    @pytest.mark.asyncio
    async def test_a_redirect_that_stays_on_yahoo_is_followed(self) -> None:
        dance = FakeResponse("", 302, {"location": "/_bv/v.gif?orig=%2Fsearch%3Fp%3Dx"})
        back = FakeResponse("", 302, {"location": "https://search.yahoo.com/search?p=x"})
        client = FakeClient(dance, back, FakeResponse(_fixture()))

        results = await YahooSearcher(client=client).search("x", 5)

        assert len(results) == 5 and len(client.calls) == 3
        assert client.calls[1][0] == "https://search.yahoo.com/_bv/v.gif?orig=%2Fsearch%3Fp%3Dx"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "location",
        [
            "https://consent.yahoo.com/v2/collectConsent?sessionId=1",
            "https://evil.example/steal",
            "http://search.yahoo.com/search?p=x",
        ],
    )
    async def test_a_redirect_to_a_consent_page_or_elsewhere_is_a_refusal(
        self, location: str
    ) -> None:
        client = FakeClient(FakeResponse("", 302, {"location": location}), FakeResponse(_fixture()))

        with pytest.raises(YahooBlocked):
            await YahooSearcher(client=client).search("x", 5)

        assert len(client.calls) == 1  # nothing off Yahoo was asked for

    @pytest.mark.asyncio
    async def test_too_many_redirects_are_a_refusal(self) -> None:
        loop = [FakeResponse("", 302, {"location": "/search?p=x"}) for _ in range(8)]

        with pytest.raises(YahooBlocked):
            await YahooSearcher(client=FakeClient(*loop)).search("x", 5)

    @pytest.mark.asyncio
    async def test_a_page_with_no_results_that_says_unusual_traffic_is_a_refusal(self) -> None:
        wall = FakeResponse("<html><body>Our systems detected unusual traffic.</body></html>")

        with pytest.raises(YahooBlocked):
            await YahooSearcher(client=FakeClient(wall)).search("x", 5)

    @pytest.mark.asyncio
    async def test_a_page_with_no_results_and_nothing_wrong_is_just_empty(self) -> None:
        empty = FakeResponse("<html><body><p>We did not find results for x.</p></body></html>")

        assert await YahooSearcher(client=FakeClient(empty)).search("x", 5) == []


class TestAdapterAndChain:
    def test_it_is_a_provider_of_its_own(self) -> None:
        adapter = build_search_adapter("yahoo")

        assert isinstance(adapter, YahooSearchAdapter)
        assert adapter.name == "yahoo" and adapter.headless is False

    @pytest.mark.asyncio
    async def test_it_returns_search_results(self) -> None:
        adapter = YahooSearchAdapter(YahooSearcher(client=FakeClient(FakeResponse(_fixture()))))

        results = await adapter.search("python asyncio tutorial", 3)

        assert [item.url for item in results] == [
            "https://realpython.com/async-io-python/",
            "https://www.geeksforgeeks.org/python/asyncio-in-python/",
            "https://codesamplez.com/programming/python-asyncio-tutorial",
        ]
        assert results[0].source == "realpython" and results[0].snippet.startswith("Jul 30, 2025")

    @pytest.mark.asyncio
    async def test_a_refusal_is_search_failed_bot_detected(self) -> None:
        adapter = YahooSearchAdapter(
            YahooSearcher(client=FakeClient(FakeResponse("no", status_code=429)))
        )

        with pytest.raises(WebToolError) as exc:
            await adapter.search("x", 5)

        assert exc.value.code == "search_failed" and exc.value.detail == "bot_detected"
        assert "Yahoo" in str(exc.value)

    @pytest.mark.asyncio
    async def test_any_other_failure_keeps_its_class_name(self) -> None:
        class Boom:
            async def search(self, *args: Any, **kwargs: Any) -> list[dict]:
                raise RuntimeError("boom")

        with pytest.raises(WebToolError) as exc:
            await YahooSearchAdapter(Boom()).search("x", 5)  # type: ignore[arg-type]

        assert exc.value.detail == "RuntimeError"

    def test_the_default_chain_is_lite_then_yahoo_then_google(self) -> None:
        assert DEFAULT_FALLBACK_PROVIDERS == ("duckduckgo_lite", "yahoo", "google")
        assert WebToolsSettings().search_fallback_providers == [
            "duckduckgo_lite",
            "yahoo",
            "google",
        ]

    @pytest.mark.asyncio
    async def test_yahoo_answers_when_both_duckduckgo_endpoints_refuse(self) -> None:
        ddg = Scripted("duckduckgo", refuse="bot_detected")
        lite = Scripted("duckduckgo_lite", refuse="bot_detected")
        yahoo = Scripted("yahoo")
        google = Scripted("google", headless=True)
        service = _service(
            ddg,
            lite,
            yahoo,
            google,
            search_fallback_providers=["duckduckgo_lite", "yahoo", "google"],
        )

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "yahoo" and google.calls == []
        record = response["queries"][0]["fallback"]
        assert [(item["provider"], item["status"]) for item in record["tried"]] == [
            ("duckduckgo", "failed"),
            ("duckduckgo_lite", "failed"),
            ("yahoo", "answered"),
        ]
        assert service._provider_gate_for("yahoo") is not service._provider_gate_for("google")
        health = (await service.metrics_snapshot())["search_providers"]
        assert health["yahoo"]["consecutive_failures"] == 0
        assert health["duckduckgo_lite"]["consecutive_failures"] == 1

    @pytest.mark.asyncio
    async def test_a_cooling_yahoo_is_skipped_for_the_browser(self) -> None:
        ddg = Scripted("duckduckgo", refuse="bot_detected")
        lite = Scripted("duckduckgo_lite", refuse="bot_detected")
        yahoo = Scripted("yahoo", refuse="bot_detected")
        google = Scripted("google", headless=True)
        service = _service(
            ddg,
            lite,
            yahoo,
            google,
            search_fallback_providers=["duckduckgo_lite", "yahoo", "google"],
        )

        await service.search("first", depth="fast")  # all three refuse; the browser answers
        response = await service.search("second", depth="fast")

        assert response["provider"] == "google"
        assert [(len(ddg.calls), len(lite.calls), len(yahoo.calls))] == [(1, 1, 1)]
        assert [item["status"] for item in response["queries"][0]["fallback"]["tried"]] == [
            "skipped",
            "skipped",
            "skipped",
            "answered",
        ]

    @pytest.mark.asyncio
    async def test_fast_asks_yahoo_before_the_browser_whatever_the_configured_order(self) -> None:
        ddg = Scripted("duckduckgo", refuse="bot_detected")
        yahoo = Scripted("yahoo")
        google = Scripted("google", headless=True)
        service = _service(ddg, yahoo, google, search_fallback_providers=["google", "yahoo"])

        response = await service.search("oulu weather", depth="fast")

        assert response["provider"] == "yahoo" and google.calls == []

    @pytest.mark.asyncio
    async def test_the_server_registers_it_and_closes_its_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_scraper.web_tools.service import WebToolsService

        # A key in the environment turns a provider on (tests/test_web_paid_search.py).
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        service = WebToolsService.from_env()
        try:
            assert set(service.search_adapters) == {
                "duckduckgo",
                "duckduckgo_lite",
                "yahoo",
                "google",
            }
            assert service.search_adapters["yahoo"].headless is False
        finally:
            await service.close()
