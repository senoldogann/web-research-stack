"""Dot sends every tool's description and schema with each model request: keep them short."""

from __future__ import annotations

import json

from web_scraper.mcp_server import _lean, _tools

# The eight tools were 9,236 characters (compact JSON) at 727ec4f; the round's fourteen items
# added parameters and fields to four of them; round A (1.3.0) adds web_health and the parameters
# of dates, recovery and compact output. A new parameter shortens something else.
BUDGET = 12_400


def test_the_tool_text_stays_within_its_budget() -> None:
    sizes = {
        tool["name"]: len(tool["description"])
        + len(json.dumps(tool["inputSchema"], separators=(",", ":")))
        for tool in _tools()
    }

    assert sum(sizes.values()) <= BUDGET, sizes
    assert sizes["web_search"] <= 3_600 and sizes["web_scrape"] <= 2_700


def test_a_schema_carries_no_title_labels() -> None:
    def titles(node: object, names: bool = False) -> list[str]:
        if isinstance(node, dict):
            found = [] if names or "title" not in node else [str(node["title"])]
            for key, value in node.items():
                found += titles(value, key in {"properties", "$defs"})
            return found
        if isinstance(node, list):
            return [title for item in node for title in titles(item)]
        return []

    assert all(titles(tool["inputSchema"]) == [] for tool in _tools())


def test_lean_keeps_a_property_that_is_called_title() -> None:
    schema = {
        "title": "Request",
        "properties": {"title": {"title": "Title", "type": "string"}},
        "$defs": {"title": {"title": "Odd", "type": "object", "properties": {"title": {}}}},
    }

    assert _lean(schema) == {
        "properties": {"title": {"type": "string"}},
        "$defs": {"title": {"type": "object", "properties": {"title": {}}}},
    }
