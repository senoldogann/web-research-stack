"""Anthropic tool-use example backed by the external web research API."""

from __future__ import annotations

import json
import os

import anthropic
import httpx

TOOLS = [
    {
        "name": "web_research",
        "description": (
            "Use the external web research backend when fresh web evidence is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_sources": {"type": "integer"},
                "deep_mode": {"type": "boolean"},
            },
            "required": ["query"],
        },
    }
]


def call_web_research(tool_input: dict) -> dict:
    """Invoke the deployed web research backend."""
    base_url = os.environ["WEB_RESEARCH_BASE_URL"].rstrip("/")
    api_key = os.environ["WEB_RESEARCH_API_KEY"]
    with httpx.Client(timeout=300.0) as client:
        response = client.post(
            f"{base_url}/api/v1/tools/web-research",
            headers={"X-API-Key": api_key},
            json=tool_input,
        )
        response.raise_for_status()
        return response.json()


def main() -> None:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    initial = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        max_tokens=1200,
        tools=TOOLS,
        messages=[
            {
                "role": "user",
                "content": (
                    "Use web research if needed and summarize the latest "
                    "AI coding-agent ecosystem."
                ),
            }
        ],
    )

    tool_results = []
    for block in initial.content:
        if getattr(block, "type", None) != "tool_use" or block.name != "web_research":
            continue
        result = call_web_research(block.input)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            }
        )

    final = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        max_tokens=1200,
        tools=TOOLS,
        messages=[
            {
                "role": "user",
                "content": (
                    "Use web research if needed and summarize the latest "
                    "AI coding-agent ecosystem."
                ),
            },
            {"role": "assistant", "content": initial.content},
            {"role": "user", "content": tool_results},
        ],
    )
    print(final.content[0].text)


if __name__ == "__main__":
    main()
