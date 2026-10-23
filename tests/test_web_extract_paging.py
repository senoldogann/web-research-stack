"""web_extract pages past its cap: offset skips the first N matches of an all field."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError
from test_web_service import FakeFetcher, _html_result, _service

from web_scraper.api_models import WebExtractRequest
from web_scraper.mcp_server import _dispatch_error, _extract, _tools
from web_scraper.web_tools.errors import WebToolError

URL = "https://shop.example/catalogue"
COUNT = 250
LIST_PAGE = (
    "<html><head><title>Catalogue</title></head><body><main><h1>Catalogue</h1><ul>"
    + "".join(f"<li>Item {index:03d}</li>" for index in range(COUNT))
    + "</ul></main></body></html>"
)
LONG_PAGE = (
    "<html><head><title>Long</title></head><body><main><ul>"
    + "".join(f"<li>Entry {index:03d} " + "x" * 400 + "</li>" for index in range(60))
    + "</ul></main></body></html>"
)
NAMED_PAGE = (
    "<html><head><title>Tagged</title>"
    '<meta name="keywords" content="alpha, beta, gamma, delta, epsilon, zeta">'
    "</head><body><main><h1>Tagged</h1></main></body></html>"
)


async def _read(html: str, schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    service = _service(FakeFetcher({URL: _html_result(URL, html)}))
    return await service.extract(URL, schema, **kwargs)


def _field(offset: int | None = None, **extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"selector": "li", "all": True, "max_items": 100, **extra}
    if offset is not None:
        spec["offset"] = offset
    return {"items": spec}


class TestPaging:
    @pytest.mark.asyncio
    async def test_a_list_of_250_is_read_in_three_pages(self) -> None:
        service = _service(FakeFetcher({URL: _html_result(URL, LIST_PAGE)}))
        values: list[str] = []
        offset = 0
        provenance_seen = []

        for _ in range(3):
            page = await service.extract(URL, _field(offset), max_chars=200_000)
            entry = page["provenance"]["items"]
            provenance_seen.append(entry)
            values.extend(page["data"]["items"])
            if "next_offset" not in entry:
                break
            offset = entry["next_offset"]

        assert values == [f"Item {index:03d}" for index in range(COUNT)]
        first, second, third = provenance_seen
        assert (first["offset"], first["returned"], first["total"]) == (0, 100, 250)
        assert first["truncated"] is True and first["next_offset"] == 100
        assert (second["offset"], second["returned"]) == (100, 100)
        assert second["truncated"] is True and second["next_offset"] == 200
        assert (third["offset"], third["returned"], third["total"]) == (200, 50, 250)
        assert third["truncated"] is False and "next_offset" not in third

    @pytest.mark.asyncio
    async def test_the_first_page_without_an_offset_says_where_to_go_on(self) -> None:
        page = await _read(LIST_PAGE, _field(None), max_chars=200_000)

        entry = page["provenance"]["items"]
        assert entry["offset"] == 0 and entry["next_offset"] == 100
        assert page["truncated"] is True

    @pytest.mark.asyncio
    async def test_an_offset_past_the_end_is_an_empty_list_not_a_missing_field(self) -> None:
        page = await _read(LIST_PAGE, _field(300))

        assert page["data"]["items"] == []
        entry = page["provenance"]["items"]
        assert (entry["total"], entry["returned"], entry["offset"]) == (250, 0, 300)
        assert entry["truncated"] is False and "next_offset" not in entry
        assert entry["method"] == "selector" and page["truncated"] is False

    @pytest.mark.asyncio
    async def test_the_last_values_fit_the_page_exactly(self) -> None:
        page = await _read(LIST_PAGE, _field(150, max_items=100))

        entry = page["provenance"]["items"]
        assert entry["returned"] == 100 and entry["truncated"] is False
        assert "next_offset" not in entry

    @pytest.mark.asyncio
    async def test_the_character_cap_moves_next_offset_with_what_it_cut(self) -> None:
        page = await _read(
            LONG_PAGE,
            {"entries": {"selector": "li", "all": True, "max_items": 60, "offset": 10}},
            max_chars=6_000,
        )

        entry = page["provenance"]["entries"]
        shown = len(page["data"]["entries"])
        assert 1 <= shown < 50
        assert page["data"]["entries"][0].startswith("Entry 010")
        assert entry["returned"] == shown and entry["truncated"] is True
        assert entry["next_offset"] == 10 + shown
        assert page["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_field_read_by_its_name_pages_too(self) -> None:
        page = await _read(NAMED_PAGE, {"keywords": {"all": True, "max_items": 2, "offset": 2}})

        assert page["data"]["keywords"] == ["gamma", "delta"]
        entry = page["provenance"]["keywords"]
        assert (entry["total"], entry["returned"], entry["offset"]) == (6, 2, 2)
        assert entry["next_offset"] == 4 and entry["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_field_that_is_not_all_ignores_the_offset(self) -> None:
        page = await _read(LIST_PAGE, {"first": {"selector": "li", "offset": 5}})

        assert page["data"]["first"] == "Item 000"
        assert "offset" not in page["provenance"]["first"]

    @pytest.mark.asyncio
    async def test_each_field_has_its_own_offset(self) -> None:
        page = await _read(
            NAMED_PAGE + "",
            {
                "a": {"all": True, "max_items": 1, "offset": 0, "selector": "h1"},
                "keywords": {"all": True, "max_items": 3, "offset": 3},
            },
        )

        assert page["data"]["keywords"] == ["delta", "epsilon", "zeta"]
        assert page["provenance"]["keywords"]["truncated"] is False


class TestValidation:
    def test_the_request_accepts_an_offset(self) -> None:
        request = WebExtractRequest.model_validate(
            {"url": URL, "schema": {"items": {"selector": "li", "all": True, "offset": 100}}}
        )

        assert request.field_specs()["items"]["offset"] == 100

    @pytest.mark.parametrize("bad", [-1, 1_000_001])
    def test_an_offset_out_of_range_is_refused(self, bad: int) -> None:
        with pytest.raises(ValidationError):
            WebExtractRequest.model_validate(
                {"url": URL, "schema": {"items": {"selector": "li", "all": True, "offset": bad}}}
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [-1, True, "3", 2.5])
    async def test_the_service_refuses_what_the_schema_refuses(self, bad: Any) -> None:
        service = _service(FakeFetcher({URL: _html_result(URL, LIST_PAGE)}))

        with pytest.raises(WebToolError) as exc:
            await service.extract(URL, _field(bad))

        assert exc.value.code == "invalid_request" and "offset" in str(exc.value)

    def test_the_schema_and_the_tool_description_name_max_items_and_offset(self) -> None:
        tool = next(tool for tool in _tools() if tool["name"] == "web_extract")
        field = tool["inputSchema"]["$defs"]["WebExtractField"]["properties"]

        # A client that does not follow $ref still reads both names from the description.
        assert {"max_items", "offset"} <= set(field)
        options = {option.get("type"): option for option in field["max_items"]["anyOf"]}
        assert options["integer"]["maximum"] == 1000
        assert "max_items" in tool["description"] and "offset" in tool["description"]
        assert "next_offset" in tool["description"]

    @pytest.mark.asyncio
    async def test_the_mcp_tool_pages(self) -> None:
        service = _service(FakeFetcher({URL: _html_result(URL, LIST_PAGE)}))

        page = await _extract(
            service,
            {"url": URL, "schema": {"items": {"selector": "li", "all": True, "offset": 240}}},
        )

        assert page["data"]["items"][0] == "Item 240" and len(page["data"]["items"]) == 10
        with pytest.raises(ValidationError) as exc:
            await _extract(
                service,
                {"url": URL, "schema": {"items": {"selector": "li", "all": True, "offset": -5}}},
            )
        error = json.loads(_dispatch_error(exc.value)["content"][0]["text"])["error"]
        assert error["code"] == "invalid_arguments"
