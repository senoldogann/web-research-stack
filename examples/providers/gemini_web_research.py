"""Gemini function-calling example that uses the external web research backend."""

from __future__ import annotations

import os

import httpx
from google import genai
from google.genai import types


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
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="web_research",
                description=(
                    "Use the external web research backend when fresh web evidence "
                    "is required."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING"},
                        "max_sources": {"type": "INTEGER"},
                        "deep_mode": {"type": "BOOLEAN"},
                    },
                    "required": ["query"],
                },
            )
        ]
    )

    initial = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        contents="Use web research if needed and explain the current AI coding-agent landscape.",
        config=types.GenerateContentConfig(tools=[tool]),
    )

    function_responses = []
    for candidate in initial.candidates or []:
        for part in candidate.content.parts:
            function_call = getattr(part, "function_call", None)
            if function_call is None or function_call.name != "web_research":
                continue
            result = call_web_research(dict(function_call.args))
            function_responses.append(
                types.Part.from_function_response(
                    name="web_research",
                    response={"result": result},
                )
            )

    final = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        contents=[
            "Use web research if needed and explain the current AI coding-agent landscape.",
            *function_responses,
        ],
        config=types.GenerateContentConfig(tools=[tool]),
    )
    print(final.text)


if __name__ == "__main__":
    main()
