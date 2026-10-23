"""max_chars has one minimum: the schema states it and every layer keeps it."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from test_web_service import FakeFetcher, _service

from web_scraper.api_models import WebScrapeRequest
from web_scraper.mcp_server import _dispatch_error, _scrape
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.settings import MIN_MARKDOWN_CHARS


def _minimums(schema: dict) -> list[int]:
    options = schema.get("anyOf", [schema])
    return [option["minimum"] for option in options if "minimum" in option]


def test_the_schema_states_the_minimum_the_service_applies() -> None:
    schema = WebScrapeRequest.model_json_schema()["properties"]["max_chars"]

    assert MIN_MARKDOWN_CHARS == 500
    assert _minimums(schema) == [MIN_MARKDOWN_CHARS]


@pytest.mark.parametrize("value", [1, MIN_MARKDOWN_CHARS - 1])
def test_a_limit_under_the_minimum_is_refused(value: int) -> None:
    with pytest.raises(ValidationError):
        WebScrapeRequest(url="https://example.com/", max_chars=value)


def test_the_minimum_itself_is_accepted() -> None:
    request = WebScrapeRequest(url="https://example.com/", max_chars=MIN_MARKDOWN_CHARS)

    assert request.max_chars == MIN_MARKDOWN_CHARS


@pytest.mark.asyncio
async def test_the_mcp_tool_refuses_max_chars_1_as_invalid_arguments() -> None:
    service = _service(FakeFetcher({}))

    with pytest.raises(ValidationError) as exc:
        await _scrape(service, {"url": "https://example.com/", "max_chars": 1})

    error = json.loads(_dispatch_error(exc.value)["content"][0]["text"])["error"]
    assert error["code"] == "invalid_arguments"
    assert error["fields"][0]["field"] == "max_chars"


@pytest.mark.asyncio
async def test_the_service_refuses_what_the_schema_refuses() -> None:
    fetcher = FakeFetcher({})
    service = _service(fetcher)

    with pytest.raises(WebToolError) as exc:
        await service.scrape("https://example.com/", max_chars=1)

    assert exc.value.code == "invalid_request"
    assert exc.value.http_status == 422
    assert fetcher.calls == []
