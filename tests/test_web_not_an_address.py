"""Text that is no web address says so, with an address to give instead.

``web_scrape("not a url at all")`` became ``https://not a url at all`` (the request model prefixed every text with
``https://``) and failed as a DNS error ("Could not resolve host not%20a%20url%20at%20all"). Now a text that is no
host keeps what it was, and every tool says ``invalid_request``: it is not a web address, and what one looks like.
An address that is one and must not be used keeps the wording of the safety refusals and its ``unsafe_target``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from rebinding_support import PUBLIC_A, html
from rest_support import HEADERS, rest_client

from web_scraper.api_models import (
    WebCrawlRequest,
    WebExtractRequest,
    WebMapRequest,
    WebScrapeRequest,
    normalize_url,
)
from web_scraper.mcp_server import McpServer
from web_scraper.web_tools import urls
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

NOT_AN_ADDRESS = [
    "not a url at all",
    "foo",
    "hello world.com",
    "https://",
    "https://not a url",
    "https://exa%6dple.com/",
    "http://[::1",
]
UNSAFE = [
    ("http://localhost:3000", "Blocked unsafe target: localhost is not allowed"),
    ("http://127.0.0.1/admin", "Blocked unsafe target: non-public IP addresses are not allowed"),
    ("http://[::1]/x", "Blocked unsafe target: non-public IP addresses are not allowed"),
    (
        "https://127.1/",
        "Blocked unsafe target: IP addresses must be written as four decimal numbers",
    ),
    ("ftp://example.com/file", "Blocked unsafe target: only http and https URLs are allowed"),
    ("file:///etc/passwd", "Blocked unsafe target: only http and https URLs are allowed"),
    ("javascript:alert(1)", "Blocked unsafe target: only http and https URLs are allowed"),
    ("mailto:someone@example.com", "Blocked unsafe target: only http and https URLs are allowed"),
    (
        "https://user:secret@example.com/",
        "Blocked unsafe target: URLs with embedded credentials are not allowed",
    ),
]


def _service() -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(
            browser_enabled=False, soft_404_check=False, respect_robots=False
        ),
        fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
        browser=None,
        search_adapters={},
    )


class TestTheService:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", NOT_AN_ADDRESS)
    async def test_every_tool_says_it_is_not_a_web_address_and_what_one_looks_like(
        self, text: str
    ) -> None:
        service = _service()
        calls: list[Any] = [
            service.scrape(text),
            service.extract(text, {"title": {}}),
            service.map(text),
            service.start_crawl(text),
        ]
        errors: list[WebToolError] = []
        try:
            for call in calls:
                with pytest.raises(WebToolError) as exc:
                    await call
                errors.append(exc.value)
        finally:
            await service.close()

        for error in errors:
            assert error.code == "invalid_request" and error.http_status == 422
            assert "is not a web address" in str(error)
            assert urls.EXAMPLE_ADDRESS in str(error)
            assert "Blocked unsafe target" not in str(error)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("text", "wording"), UNSAFE)
    async def test_an_address_that_must_not_be_used_keeps_the_wording_of_the_refusal(
        self, text: str, wording: str
    ) -> None:
        service = _service()

        try:
            with pytest.raises(WebToolError) as exc:
                await service.scrape(text)
        finally:
            await service.close()

        assert exc.value.code == "unsafe_target" and exc.value.http_status == 400
        assert str(exc.value) == wording

    @pytest.mark.asyncio
    async def test_a_host_without_its_scheme_is_answered_with_the_address_it_was_meant_to_be(
        self,
    ) -> None:
        service = _service()

        try:
            with pytest.raises(WebToolError) as exc:
                await service.scrape("senoldogan.dev/blog")
        finally:
            await service.close()

        assert (
            str(exc.value)
            == '"senoldogan.dev/blog" is not a web address. Did you mean https://senoldogan.dev/blog?'
        )

    @pytest.mark.asyncio
    async def test_a_bad_address_in_a_batch_fails_that_page_and_not_the_others(
        self, world: Any
    ) -> None:
        world.dns.answers("ok.test", [PUBLIC_A])
        world.internet.serve(
            PUBLIC_A,
            lambda request: html(
                "<html><body><main>" + "Fine page. " * 30 + "</main></body></html>"
            ),
        )
        service = _service()

        try:
            answer = await service.scrape_many(["https://ok.test/a", "not a url at all"])
        finally:
            await service.close()

        good, bad = answer["pages"]
        assert good["status_code"] == 200
        assert bad["url"] == "not a url at all" and bad["error"]["code"] == "invalid_request"
        assert "is not a web address" in bad["error"]["message"]

    def test_the_text_is_quoted_on_one_line_and_not_longer_than_eighty_characters(self) -> None:
        error = urls.not_a_web_address("line one\nline two " + "x" * 200)

        assert isinstance(error, urls.NotAWebAddressError) and "\n" not in str(error)
        assert str(error).startswith('"line one line two ') and "..." in str(error)
        assert len(str(error).split('" is not')[0]) <= 82


class TestTheModelsThatTookEveryTextForAHost:
    @pytest.mark.parametrize(
        ("text", "normalized"),
        [
            ("senoldogan.dev", "https://senoldogan.dev"),
            ("  senoldogan.dev/blog?a=b  ", "https://senoldogan.dev/blog?a=b"),
            ("example.com:8080/x", "https://example.com:8080/x"),
            ("localhost:3000", "https://localhost:3000"),  # refused later as what it is
            ("127.1", "https://127.1"),  # refused later as what it is
            ("[::1]/x", "https://[::1]/x"),
            ("http://example.com", "http://example.com"),
            ("HTTPS://Example.com/A", "HTTPS://Example.com/A"),
            ("not a url at all", "not a url at all"),
            ("foo", "foo"),
            ("ftp://example.com/f", "ftp://example.com/f"),
            ("file:///etc/passwd", "file:///etc/passwd"),
            ("mailto:a@b.co", "mailto:a@b.co"),
        ],
    )
    def test_a_bare_host_gets_its_scheme_and_nothing_else_is_made_into_one(
        self, text: str, normalized: str
    ) -> None:
        assert normalize_url(text) == normalized

    def test_every_tool_takes_the_same_rule(self) -> None:
        assert (
            WebScrapeRequest.model_validate({"url": "senoldogan.dev"}).url
            == "https://senoldogan.dev"
        )
        assert (
            WebScrapeRequest.model_validate({"url": "not a url at all"}).url == "not a url at all"
        )
        assert WebScrapeRequest.model_validate({"urls": ["a.example", "not a url"]}).urls == [
            "https://a.example",
            "not a url",
        ]
        assert (
            WebMapRequest.model_validate({"url": "senoldogan.dev"}).url == "https://senoldogan.dev"
        )
        assert WebCrawlRequest.model_validate({"url": "not a url"}).url == "not a url"
        assert (
            WebExtractRequest.model_validate({"url": "not a url", "schema": {"a": "b"}}).url
            == "not a url"
        )

    def test_the_helpers(self) -> None:
        assert (
            urls.has_scheme("https://x")
            and urls.has_scheme("FTP://x")
            and urls.has_scheme("mailto:a@b.co")
        )
        assert not urls.has_scheme("example.com:8080") and not urls.has_scheme("not a url")
        assert urls.bare_host_address("example.com") == "https://example.com"
        assert urls.bare_host_address("a b.com") is None and urls.bare_host_address("foo") is None
        assert urls.bare_host_address("https://example.com") is None

    def test_a_real_address_is_still_one(self) -> None:
        assert (
            urls.canonicalize_url("https://Example.com/a/?utm_source=x&b=1")
            == "https://example.com/a?b=1"
        )
        assert urls.normalize_request_url("http://example.com") == "http://example.com/"
        assert urls.canonicalize_url("https://bücher.example/") == "https://xn--bcher-kva.example/"
        assert urls.canonicalize_url("https://a_b.example/") == "https://a_b.example/"


class TestOverMcp:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("web_scrape", {"url": "not a url at all"}),
            ("web_scrape", {"urls": ["not a url at all", "also not a url"]}),
            ("web_map", {"url": "not a url at all"}),
            ("web_extract", {"url": "not a url at all", "schema": {"title": {}}}),
            ("web_crawl_start", {"url": "not a url at all"}),
        ],
    )
    async def test_the_model_is_told_it_is_not_a_web_address_with_an_example(
        self, tool: str, arguments: dict[str, Any]
    ) -> None:
        server = McpServer(_service())

        try:
            result = await server._call_tool({"name": tool, "arguments": arguments})
        finally:
            await server.service.close()

        payload = json.loads(result["content"][0]["text"])
        if tool == "web_scrape" and "urls" in arguments:
            # A batch fails page by page: each says so.
            assert result["isError"] is False
            errors = [page["error"] for page in payload["pages"]]
        else:
            assert result["isError"] is True
            errors = [payload["error"]]
        for error in errors:
            assert error["code"] == "invalid_request"
            assert (
                "is not a web address" in error["message"]
                and urls.EXAMPLE_ADDRESS in error["message"]
            )

    @pytest.mark.asyncio
    async def test_a_refusal_over_mcp_keeps_its_code(self) -> None:
        server = McpServer(_service())

        try:
            result = await server._call_tool(
                {"name": "web_scrape", "arguments": {"url": "http://localhost:3000/"}}
            )
        finally:
            await server.service.close()

        error = json.loads(result["content"][0]["text"])["error"]
        assert result["isError"] is True and error["code"] == "unsafe_target"
        assert error["message"] == "Blocked unsafe target: localhost is not allowed"

    @pytest.mark.asyncio
    async def test_a_bare_host_is_still_read(self, world: Any) -> None:
        world.dns.answers("bare.test", [PUBLIC_A])
        world.internet.serve(
            PUBLIC_A,
            lambda request: html("<html><body><main>" + "A page. " * 40 + "</main></body></html>"),
        )
        server = McpServer(_service())

        try:
            result = await server._call_tool(
                {"name": "web_scrape", "arguments": {"url": "bare.test"}}
            )
        finally:
            await server.service.close()

        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["url"] == "https://bare.test/"


class TestOverRest:
    def test_the_route_answers_422_with_the_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = rest_client(monkeypatch, _service())

        response = client.post(
            "/api/v1/web/scrape", headers=HEADERS, json={"url": "not a url at all"}
        )

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "invalid_request" and "is not a web address" in error["message"]
        assert urls.EXAMPLE_ADDRESS in error["message"]

    def test_a_refused_address_is_still_a_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = rest_client(monkeypatch, _service())

        response = client.post(
            "/api/v1/web/map", headers=HEADERS, json={"url": "http://localhost:8080/"}
        )

        assert response.status_code == 400 and response.json()["error"]["code"] == "unsafe_target"
