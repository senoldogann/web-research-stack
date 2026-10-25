"""DuckDuckGo's lite endpoint, and the request both endpoints get answered to.

Live probes of 2026-10-23 (fixture: one lite search of ``python asyncio tutorial``):
a lite POST of ``q`` and ``kl`` alone, and an html GET of ``q`` and ``kl`` alone, were answered
HTTP 202 with a duck-picking challenge; the same searches posted with the fields DuckDuckGo's own
form carries (``s``, ``o``, ``api``, ``vqd``, ``bing_market``) were answered 200 with results,
in one request, without fetching the vqd token from duckduckgo.com first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from web_scraper.duckduckgo_search import (
    DuckDuckGoBlocked,
    DuckDuckGoLiteSearcher,
    DuckDuckGoSearcher,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.search import (
    DuckDuckGoLiteSearchAdapter,
    build_search_adapter,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ddg_lite_python_asyncio_tutorial.html"
LITE_URL = "https://lite.duckduckgo.com/lite/"
HTML_URL = "https://html.duckduckgo.com/html/"
FORM_FIELDS = {"q", "s", "o", "api", "vqd", "kl", "bing_market"}


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Stands for the shared browser-like session; records every request."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []

    async def post(
        self, url: str, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> FakeResponse:
        self.calls.append(("POST", url, dict(data or {}), dict(headers or {})))
        return self.responses.pop(0)

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:  # pragma: no cover - a failure
        self.calls.append(("GET", url, {}, {}))
        raise AssertionError("a search is one POST of the form: no GET of a token page")


def _lite_page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _html_page(url: str, title: str, *, next_form: bool = False) -> str:
    form = (
        '<form action="/html/" method="post"><input type="hidden" name="q" value="x">'
        '<input type="hidden" name="s" value="10"><input type="hidden" name="vqd" value="4-1">'
        "</form>"
        if next_form
        else ""
    )
    return (
        f'<div class="result"><a class="result__a" href="{url}">{title}</a>'
        f'<a class="result__snippet">About {title}</a></div>{form}'
    )


class TestLiteParser:
    def test_it_reads_the_ten_organic_results_of_the_live_fixture(self) -> None:
        results, _ = DuckDuckGoLiteSearcher()._parse_results(_lite_page())

        assert len(results) == 10
        first, second = results[0], results[1]
        assert first["title"] == "Python's asyncio: A Hands-On Walkthrough - Real Python"
        assert first["url"] == "https://realpython.com/async-io-python/"
        assert first["snippet"].startswith("Python's asyncio library enables you to write conc")
        assert first["source"] == "realpython"
        assert second["url"] == "https://www.geeksforgeeks.org/python/asyncio-in-python/"
        assert second["snippet"].startswith("Asyncio is used as a foundation for multiple Python")
        assert results[-1]["url"] == "https://www.w3schools.com/python/ref_module_asyncio.asp"
        assert all(item["snippet"] for item in results)

    def test_it_leaves_the_sponsored_result_and_duckduckgo_links_out(self) -> None:
        results, _ = DuckDuckGoLiteSearcher()._parse_results(_lite_page())

        assert not any(
            "udemy" in item["url"] or "duckduckgo.com" in item["url"] for item in results
        )
        assert "Learn Data Science online" not in " ".join(item["snippet"] for item in results)

    def test_it_finds_the_form_of_the_next_page(self) -> None:
        _, next_payload = DuckDuckGoLiteSearcher()._parse_results(_lite_page())

        assert next_payload == {
            "q": "python asyncio tutorial",
            "s": "10",
            "nextParams": "",
            "v": "",
            "o": "json",
            "dc": "12",
            "api": "d.js",
            "vqd": "",
            "kl": "wt-wt",
        }

    def test_a_page_without_results_is_empty(self) -> None:
        results, next_payload = DuckDuckGoLiteSearcher()._parse_results(
            "<html><body></body></html>"
        )

        assert results == []
        assert next_payload is None

    def test_the_date_shown_beside_a_result_s_address_is_its_published_date(self) -> None:
        # Five of the ten show one (span.timestamp, 2025-07-30T00:00:00.0000000); it was dropped, so a result of
        # DuckDuckGo had no date unless its address or snippet held one.
        results, _ = DuckDuckGoLiteSearcher()._parse_results(_lite_page())

        dated = {item["url"]: item["published"] for item in results if item["published"]}
        assert dated == {
            "https://realpython.com/async-io-python/": "2025-07-30",
            "https://www.geeksforgeeks.org/python/asyncio-in-python/": "2025-07-23",
            "https://codesamplez.com/programming/python-asyncio-tutorial": "2025-02-10",
            "https://docs.python.org/3/howto/a-conceptual-overview-of-asyncio.html": "2026-10-21",
            "https://www.datacamp.com/tutorial/python-async-programming": "2025-12-08",
        }
        assert all(item["published"] == "" for item in results if item["url"] not in dated)


class TestTheRequest:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("searcher_class", "url", "page"),
        [
            (DuckDuckGoSearcher, HTML_URL, _html_page("https://a.example/", "A")),
            (DuckDuckGoLiteSearcher, LITE_URL, _lite_page()),
        ],
    )
    async def test_a_search_is_one_post_of_the_fields_the_form_sends(
        self, searcher_class: type[DuckDuckGoSearcher], url: str, page: str
    ) -> None:
        client = FakeClient(FakeResponse(page))
        searcher = searcher_class(region="fi-fi", language="fi-FI,fi;q=0.9", client=client)

        results = await searcher.search("python asyncio tutorial", num_results=3)

        assert len(results) >= 1
        assert len(client.calls) == 1
        method, called, data, headers = client.calls[0]
        assert (method, called) == ("POST", url)
        assert set(data) >= FORM_FIELDS
        assert data["q"] == "python asyncio tutorial"
        assert (data["s"], data["o"], data["api"], data["vqd"]) == ("0", "json", "d.js", "")
        assert data["kl"] == "fi-fi"
        assert data["bing_market"] == "fi-fi"
        origin = url.rsplit("/", 2)[0]
        assert headers["origin"] == origin
        assert headers["referer"].startswith(origin)
        assert headers["accept-language"] == "fi-FI,fi;q=0.9"

    @pytest.mark.asyncio
    async def test_the_headers_are_the_impersonated_browsers_own(self) -> None:
        # A random user agent over a Chrome TLS fingerprint, and sec-ch-ua hints of another
        # version and system, are what a bot detector reads as a script; the impersonated
        # browser sends a coherent set by itself, so the searcher adds none of them.
        client = FakeClient(FakeResponse(_lite_page()))

        await DuckDuckGoLiteSearcher(client=client).search("python asyncio tutorial", 3)

        sent = {name.lower() for name in client.calls[0][3]}
        assert not {"user-agent", "sec-ch-ua", "sec-ch-ua-platform", "accept"} & sent

    @pytest.mark.asyncio
    async def test_a_second_page_is_the_forms_own_next_payload(self) -> None:
        second = _html_page("https://b.example/", "B")
        client = FakeClient(FakeResponse(_lite_page()), FakeResponse(second))
        searcher = DuckDuckGoLiteSearcher(client=client)

        results = await searcher.search("python asyncio tutorial", num_results=12)

        assert len(client.calls) == 2
        page_two = client.calls[1][2]
        assert page_two["s"] == "10" and page_two["dc"] == "12"
        assert page_two["q"] == "python asyncio tutorial"
        assert page_two["kl"] == "wt-wt"
        assert len(results) >= 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize("searcher_class", [DuckDuckGoSearcher, DuckDuckGoLiteSearcher])
    async def test_a_challenge_is_a_refusal(self, searcher_class: type[DuckDuckGoSearcher]) -> None:
        client = FakeClient(FakeResponse("<html>duck challenge</html>", status_code=202))

        with pytest.raises(DuckDuckGoBlocked):
            await searcher_class(client=client).search("anything", 5)


class TestTheLiteAdapter:
    def test_it_is_a_provider_of_its_own(self) -> None:
        adapter = build_search_adapter("duckduckgo_lite")

        assert isinstance(adapter, DuckDuckGoLiteSearchAdapter)
        assert adapter.name == "duckduckgo_lite"
        assert getattr(adapter, "headless", False) is False

    @pytest.mark.asyncio
    async def test_a_refusal_is_search_failed_bot_detected(self) -> None:
        class Blocked:
            async def search(self, query: str, num_results: int = 10, date_filter=None):
                raise DuckDuckGoBlocked("challenge")

        with pytest.raises(WebToolError) as exc:
            await DuckDuckGoLiteSearchAdapter(Blocked()).search("query", 5)

        assert exc.value.code == "search_failed"
        assert exc.value.detail == "bot_detected"
        assert "lite" in str(exc.value)

    @pytest.mark.asyncio
    async def test_it_returns_search_results(self) -> None:
        client = FakeClient(FakeResponse(_lite_page()))
        adapter = DuckDuckGoLiteSearchAdapter(DuckDuckGoLiteSearcher(client=client))

        results = await adapter.search("python asyncio tutorial", 5)

        assert [item.url for item in results][:2] == [
            "https://realpython.com/async-io-python/",
            "https://www.geeksforgeeks.org/python/asyncio-in-python/",
        ]
        assert len(results) == 5
