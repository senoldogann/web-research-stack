"""web_extract answers within a bound: max_items per field, max_chars overall."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError
from test_web_service import FakeFetcher, _html_result, _service

from web_scraper.api_models import WebExtractRequest
from web_scraper.mcp_server import _dispatch_error, _extract

URL = "https://en.wikipedia.org/wiki/Eiffel_Tower"
LINKS = 1500
# Like the Wikipedia article of the repro: about 1,500 links with long paths.
LINK_PAGE = (
    "<html><head><title>Eiffel Tower</title></head><body><main><h1>Eiffel Tower</h1>"
    + "".join(
        f'<a href="/wiki/Special:WhatLinksHere/Article_{index}_{"x" * 40}">Link {index}</a>'
        for index in range(LINKS)
    )
    + "</main></body></html>"
)
TEXT_PAGE = (
    "<html><head><title>Long</title></head><body><main><h1>Long read</h1><p>"
    + ("A sentence of the long article body. " * 4000)
    + "</p></main></body></html>"
)


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


async def _extract_from(html: str, schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    service = _service(FakeFetcher({URL: _html_result(URL, html)}))
    return await service.extract(URL, schema, **kwargs)


@pytest.mark.asyncio
async def test_the_repro_answers_within_20000_characters_and_says_it_cut() -> None:
    result = await _extract_from(
        LINK_PAGE, {"links": {"selector": "a", "attribute": "href", "all": True}}
    )

    assert _size(result) <= 20_000
    assert result["truncated"] is True
    assert len(result["data"]["links"]) == 100
    assert result["data"]["links"][0].startswith("https://en.wikipedia.org/wiki/Special:")
    provenance = result["provenance"]["links"]
    assert provenance["method"] == "selector"
    assert provenance["selector"] == "a"
    assert provenance["source_url"] == URL
    assert provenance["total"] == LINKS
    assert provenance["returned"] == 100
    assert provenance["truncated"] is True


@pytest.mark.asyncio
async def test_max_items_sets_how_many_values_a_field_returns() -> None:
    result = await _extract_from(
        LINK_PAGE, {"links": {"selector": "a", "attribute": "href", "all": True, "max_items": 5}}
    )

    assert len(result["data"]["links"]) == 5
    assert result["provenance"]["links"]["total"] == LINKS
    assert result["provenance"]["links"]["returned"] == 5


@pytest.mark.asyncio
async def test_a_long_text_value_is_cut_to_the_response_cap() -> None:
    result = await _extract_from(TEXT_PAGE, {"heading": "h1", "body": "main p"})

    assert _size(result) <= 20_000
    assert result["truncated"] is True
    assert result["data"]["heading"] == "Long read"
    assert result["data"]["body"].startswith("A sentence of the long article body.")
    assert result["data"]["body"].endswith("…")
    assert result["provenance"]["body"]["truncated"] is True
    assert "truncated" not in result["provenance"]["heading"]


@pytest.mark.asyncio
async def test_max_chars_sets_the_response_cap() -> None:
    result = await _extract_from(
        LINK_PAGE,
        {"links": {"selector": "a", "attribute": "href", "all": True, "max_items": 1000}},
        max_chars=5_000,
    )

    assert _size(result) <= 5_000
    assert result["truncated"] is True
    assert 1 <= len(result["data"]["links"]) < 1000
    assert result["provenance"]["links"]["returned"] == len(result["data"]["links"])
    assert result["provenance"]["links"]["total"] == LINKS


@pytest.mark.asyncio
async def test_a_small_extraction_is_untouched() -> None:
    result = await _extract_from(LINK_PAGE, {"heading": "h1"})

    assert result["truncated"] is False
    assert result["data"] == {"heading": "Eiffel Tower"}
    assert set(result["provenance"]["heading"]) == {"method", "selector", "evidence", "source_url"}


def test_the_request_schema_states_both_limits() -> None:
    schema = WebExtractRequest.model_json_schema(by_alias=True)

    assert "max_chars" in schema["properties"]
    field = schema["$defs"]["WebExtractField"]["properties"]
    assert "max_items" in field
    assert {"selector", "attribute", "all", "type", "required"} <= set(field)


@pytest.mark.parametrize("bad", [{"max_items": 0}, {"max_items": 1001}])
def test_a_max_items_out_of_range_is_refused(bad: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        WebExtractRequest.model_validate(
            {"url": URL, "schema": {"links": {"selector": "a", "all": True, **bad}}}
        )


@pytest.mark.asyncio
async def test_the_mcp_tool_passes_both_limits_and_refuses_bad_ones() -> None:
    service = _service(FakeFetcher({URL: _html_result(URL, LINK_PAGE)}))

    result = await _extract(
        service,
        {
            "url": URL,
            "schema": {
                "links": {"selector": "a", "attribute": "href", "all": True, "max_items": 3}
            },
            "max_chars": 4_000,
        },
    )
    assert len(result["data"]["links"]) == 3
    assert _size(result) <= 4_000

    with pytest.raises(ValidationError) as exc:
        await _extract(service, {"url": URL, "schema": {"h": "h1"}, "max_chars": 10})
    error = json.loads(_dispatch_error(exc.value)["content"][0]["text"])["error"]
    assert error["code"] == "invalid_arguments"
