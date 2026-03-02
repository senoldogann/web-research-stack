# Provider Examples

These examples show how to connect external provider tool-calling flows to this backend.

## Required Environment Variables

- `WEB_RESEARCH_BASE_URL`
- `WEB_RESEARCH_API_KEY`

Provider-specific examples also require:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

## Files

- `openai_web_research.py`
- `anthropic_web_research.py`
- `gemini_web_research.py`

Each example follows the same pattern:

1. Register a `web_research` tool/function with the provider.
2. When the model asks for the tool, call this backend over HTTP.
3. Return the backend JSON as tool output.
4. Let the model produce the final answer with citations.

