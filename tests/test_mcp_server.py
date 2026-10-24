"""Protocol tests for the stdio MCP server Dot starts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

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
        "web_health",
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


def test_web_health_answers_in_a_real_session_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The session inherits the environment: a key set on this machine would turn a provider on.
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    responses, stderr = _run_session(
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
                "params": {"name": "web_health", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "web_scrape", "arguments": {"url": "http://127.0.0.1:1/"}},
            },
        ]
    )

    health = responses[2]["result"]
    assert health["isError"] is False and len(health["content"]) == 1
    payload = json.loads(health["content"][0]["text"])
    names = {entry["name"]: entry for entry in payload["providers"]}
    assert {"duckduckgo", "duckduckgo_lite", "yahoo", "google"} <= set(names)
    assert names["duckduckgo"]["state"] == "ok" and names["duckduckgo"]["calls"] == 0
    # The keyed providers are off without their key, and the answer names the variable only.
    assert names["brave"] == {"name": "brave", "state": "no_key", "key_env": "BRAVE_SEARCH_API_KEY"}
    # A refused address is one block of JSON error, with no page to put in a block of its own.
    refused = responses[3]["result"]
    assert refused["isError"] is True and len(refused["content"]) == 1
    assert json.loads(refused["content"][0]["text"])["error"]["code"] == "unsafe_target"
    assert "Traceback" not in stderr
