"""OpenAI Responses API example that delegates web search to this backend tool."""

from __future__ import annotations

import json
import os

import httpx
from openai import OpenAI

WEB_RESEARCH_TOOL = {
    "type": "function",
    "name": "web_research",
    "description": (
        "Use the external web research backend when the model needs current web evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_sources": {"type": "integer"},
            "deep_mode": {"type": "boolean"},
        },
        "required": ["query"],
    },
}


def call_web_research(arguments: dict) -> dict:
    """Invoke the deployed web research backend."""
    base_url = os.environ["WEB_RESEARCH_BASE_URL"].rstrip("/")
    api_key = os.environ["WEB_RESEARCH_API_KEY"]
    with httpx.Client(timeout=300.0) as client:
        response = client.post(
            f"{base_url}/api/v1/tools/web-research",
            headers={"X-API-Key": api_key},
            json=arguments,
        )
        response.raise_for_status()
        return response.json()


def main() -> None:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5"),
        input="Use web research if needed and explain the latest AI coding-agent landscape.",
        tools=[WEB_RESEARCH_TOOL],
    )

    tool_outputs = []
    for item in response.output:
        if getattr(item, "type", None) != "function_call" or item.name != "web_research":
            continue

        arguments = json.loads(item.arguments)
        result = call_web_research(arguments)
        tool_outputs.append(
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": json.dumps(result),
            }
        )

    final = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5"),
        previous_response_id=response.id,
        input=tool_outputs,
    )
    print(final.output_text)


if __name__ == "__main__":
    main()
