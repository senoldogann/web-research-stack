"""Protocol tests for the stdio MCP server Dot starts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_session(requests: list[dict]) -> tuple[dict[int, dict], str]:
    payload = "".join(json.dumps(request) + "\n" for request in requests)
    completed = subprocess.run(
        [sys.executable, "-m", "web_scraper.mcp_server"],
        cwd=ROOT,
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    responses: dict[int, dict] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        if "id" in message:
            responses[message["id"]] = message
    return responses, completed.stderr


def test_initialize_list_and_ping_without_protocol_noise() -> None:
    responses, stderr = _run_session(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
    )

    assert responses[1]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[1]["result"]["serverInfo"]["name"] == "web-research"
    assert "tools" in responses[1]["result"]["capabilities"]
    names = {tool["name"] for tool in responses[2]["result"]["tools"]}
    assert names == {
        "web_search",
        "web_scrape",
        "web_map",
        "web_extract",
        "web_crawl_start",
        "web_crawl_status",
        "web_crawl_results",
        "web_crawl_cancel",
    }
    by_name = {tool["name"]: tool for tool in responses[2]["result"]["tools"]}
    assert by_name["web_search"]["annotations"]["readOnlyHint"] is True
    assert "depth" in by_name["web_search"]["inputSchema"]["properties"]
    assert "historical" in by_name["web_search"]["description"]
    assert by_name["web_crawl_start"]["annotations"]["readOnlyHint"] is False
    assert by_name["web_scrape"]["inputSchema"]["properties"]["url"]["type"] == "string"
    assert responses[3]["result"] == {}
    assert "Traceback" not in stderr


def test_discover_probe_falls_back_for_legacy_clients() -> None:
    responses, _ = _run_session(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"supportedVersions": ["2026-07-28"]},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
        ]
    )

    assert responses[1]["error"]["code"] == -32601
    assert responses[2]["result"]["protocolVersion"] == "2025-11-25"


def test_invalid_tool_arguments_are_tool_errors_not_crashes() -> None:
    responses, _ = _run_session(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "web_search", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "not_a_tool", "arguments": {}},
            },
        ]
    )

    invalid = responses[2]["result"]
    assert invalid["isError"] is True
    error = json.loads(invalid["content"][0]["text"])
    assert error["error"]["code"] == "invalid_arguments"
    assert responses[3]["error"]["code"] == -32601
