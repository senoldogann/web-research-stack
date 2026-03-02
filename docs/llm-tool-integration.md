# LLM Tool Integration Guide

This guide explains how to use this backend as a web research tool for any LLM stack, regardless of programming language or model provider.

## Who This Is For

- Teams building agent systems for models without native web search
- Applications that want citations and structured web research over plain HTTP
- Orchestrators that support tool calling, function calling, workflows, or pre-processing steps

## What This Backend Provides

The backend exposes a versioned REST API that can be used as a tool:

- `GET /api/v1/tools`
- `POST /api/v1/tools/web-research`
- `POST /api/v1/tools/web-research/stream`

The `web_research` tool:

- rewrites weak user input into search-ready queries when needed
- searches the web across multiple sources
- scrapes and ranks sources
- preserves code blocks from source pages (for programming queries)
- returns a structured answer with citations and metadata
- provides internationalized status messages (Turkish/English) in streaming mode
- includes resilient JSON parsing with 3-stage recovery for LLM output

## Integration Modes

### 1. Tool-Calling Models

Use this mode if your provider supports tools or function calling.

Flow:

1. Register a `web_research` tool in your agent runtime.
2. When the model asks to call the tool, forward the arguments to this backend.
3. Return the backend response to the model as tool output.
4. Let the model generate the final answer using the tool result.

### 2. Models Without Tool Calling

Use this mode if the model only accepts plain text.

Flow:

1. Your application decides whether web research is needed.
2. Your application calls the backend directly before the final model call.
3. Your application injects the research result into the model prompt or context.
4. The model writes the final answer based on the retrieved evidence.

### 3. Workflow Platforms

Use this mode for tools such as automation runners, internal platforms, or no-code systems.

Flow:

1. Trigger an HTTP request to `POST /api/v1/tools/web-research`.
2. Parse the returned JSON.
3. Pass `answer`, `key_findings`, `citations`, or `sources` to the next workflow step.

## Base Requirements

- A public `HTTPS` URL for the backend in production
- A valid API key sent through `X-API-Key`
- A reachable model runtime behind the backend via `OLLAMA_HOST`
- Reasonable production env values for rate limiting and timeouts
- Provider-specific examples are available in [Provider Examples](../examples/providers/README.md)

## Tool Contract

### Discover Tool Schema

```bash
curl -X GET "https://your-domain.com/api/v1/tools" \
  -H "X-API-Key: YOUR_API_KEY"
```

Typical response:

```json
{
  "tools": [
    {
      "name": "web_research",
      "method": "POST",
      "path": "/api/v1/tools/web-research",
      "stream_path": "/api/v1/tools/web-research/stream",
      "description": "Run multi-source web research with citations.",
      "auth": {
        "type": "api_key",
        "header": "X-API-Key"
      },
      "input_schema": {
        "type": "object",
        "properties": {
          "query": { "type": "string" },
          "max_sources": { "type": "integer" },
          "deep_mode": { "type": "boolean" },
          "research_profile": { "type": "string", "enum": ["technical", "news", "academic"] },
          "model": { "type": "string" },
          "provider": { "type": "string", "enum": ["ollama", "openai"] },
          "include_source_content": { "type": "boolean" }
        },
        "required": ["query"]
      },
      "output_schema": { "..." : "..." },
      "example": { "..." : "..." }
    }
  ]
}
```

### Request Shape

```json
{
  "query": "Latest developments in AI coding agents",
  "max_sources": 5,
  "deep_mode": false,
  "research_profile": "technical",
  "model": "gpt-oss:120b-cloud",
  "provider": "ollama",
  "include_source_content": false,
  "openai_api_key": null
}
```

### Response Shape

```json
{
  "query": "Latest developments in AI coding agents",
  "answer": "Structured research answer.",
  "summary": "Structured research answer.",
  "key_findings": [
    "Finding 1",
    "Finding 2"
  ],
  "detailed_analysis": "Long-form analysis.",
  "recommendations": "Actionable next steps.",
  "executive_summary": "High-level executive summary.",
  "data_table": [
    {
      "metric": "Market Size",
      "value": "$200B",
      "source": "Gartner",
      "date": "2026"
    }
  ],
  "conflicts_uncertainty": [
    "Source A says X while Source B says Y"
  ],
  "confidence_level": "High",
  "confidence_reason": "Multiple authoritative sources agree.",
  "citation_audit": {
    "total_citations": 4,
    "supported_citations": 4,
    "weak_citations": 0,
    "faithfulness_score": 1.0
  },
  "citations": [
    {
      "source": "docs",
      "url": "https://example.com/article",
      "title": "Example Article",
      "relevance_score": 0.91,
      "snippet": "Short supporting excerpt.",
      "source_tier": 2,
      "publication_date": "2026-01-15"
    }
  ],
  "sources": [
    {
      "source": "docs",
      "url": "https://example.com/article",
      "title": "Example Article",
      "content": null,
      "relevance_score": 0.91,
      "error": null,
      "source_tier": 2,
      "publication_date": "2026-01-15"
    }
  ],
  "metadata": {
    "model": "gpt-oss:120b-cloud",
    "generated_at": "2026-03-02T12:00:00+00:00",
    "sources_checked": 5,
    "sources_succeeded": 4,
    "cached": false,
    "trace_id": "trace-id",
    "response_ms": 9123.55,
    "query_hash": "a1b2c3d4"
  }
}
```

> **Note:** For programming-related queries, `answer`, `detailed_analysis`, and `key_findings` may contain markdown fenced code blocks.

## HTTP Examples

### cURL

```bash
curl -X POST "https://your-domain.com/api/v1/tools/web-research" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "query": "What changed recently in OpenAI agent tooling?",
    "deep_mode": true,
    "max_sources": 20
  }'
```

### JavaScript / TypeScript

```ts
const response = await fetch("https://your-domain.com/api/v1/tools/web-research", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": process.env.WEB_RESEARCH_API_KEY!,
  },
  body: JSON.stringify({
    query: "What changed recently in OpenAI agent tooling?",
    deep_mode: true,
    max_sources: 20,
  }),
});

if (!response.ok) {
  throw new Error(`Research failed: ${response.status}`);
}

const result = await response.json();
console.log(result.answer);
console.log(result.citations);
```

### Python

```python
import httpx

payload = {
    "query": "What changed recently in OpenAI agent tooling?",
    "deep_mode": True,
    "max_sources": 20,
}

headers = {
    "Content-Type": "application/json",
    "X-API-Key": "YOUR_API_KEY",
}

with httpx.Client(timeout=300.0) as client:
    response = client.post(
        "https://your-domain.com/api/v1/tools/web-research",
        json=payload,
        headers=headers,
    )
    response.raise_for_status()
    result = response.json()

print(result["answer"])
for citation in result["citations"]:
    print(citation["title"], citation["url"])
```

## Streaming Mode

If your app wants progress updates, use:

- `POST /api/v1/tools/web-research/stream`

This endpoint returns `text/event-stream`.

Common event types:

- `status`
- `source_start`
- `source_complete`
- `result`

### JavaScript SSE Reader Pattern

```ts
const response = await fetch("https://your-domain.com/api/v1/tools/web-research/stream", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "X-API-Key": process.env.WEB_RESEARCH_API_KEY!,
  },
  body: JSON.stringify({
    query: "Compare the latest AI agent frameworks",
    deep_mode: true,
  }),
});

const reader = response.body!.getReader();
const decoder = new TextDecoder();
let buffer = "";

while (true) {
  const { value, done } = await reader.read();
  if (done) break;

  buffer += decoder.decode(value, { stream: true });
  const lines = buffer.split("\n");
  buffer = lines.pop() ?? "";

  for (const line of lines) {
    if (!line.startsWith("data: ")) continue;
    const event = JSON.parse(line.slice(6));
    console.log(event.type, event);
  }
}
```

## Provider Integration Pattern

The provider does not need native web search support. Your application acts as the tool runner.

### OpenAI-Compatible Tool Loop

Use this pattern with providers that let the model request structured tool calls.

1. Define a tool named `web_research`.
2. Use the backend request schema as the tool input schema.
3. When the model emits a tool call, call this backend.
4. Return the backend JSON to the model as tool output.
5. Ask the model to produce the final user-facing answer.

### Anthropic-Compatible Tool Loop

Use the same pattern:

1. Register `web_research` as a tool.
2. When the model asks for the tool, map tool arguments to the backend request.
3. Return the JSON result.
4. Let the model finish the answer using the returned evidence.

### Gemini-Compatible Tool Loop

Use the same pattern:

1. Define a function or tool that matches the backend schema.
2. Invoke the backend when the model requests that function.
3. Return the structured result to the model.

### Self-Hosted or Local Models

If your local model does not support tool calling:

1. Detect when the user request needs external knowledge.
2. Call the backend first.
3. Inject the result into the prompt as trusted retrieved context.

## Provider-Agnostic Pseudocode

```text
tool_schema = GET /api/v1/tools
register web_research in your agent runtime

model_response = call_model(user_message, tools=[web_research])

if model_response requests web_research:
    tool_args = model_response.tool_args
    tool_result = POST /api/v1/tools/web-research with tool_args
    final_response = call_model(
        original_messages + tool_result_as_tool_message
    )
else:
    final_response = model_response
```

## Recommended Usage Rules

- Use `deep_mode=false` for fast factual lookups and standard research
- Use `deep_mode=true` for comparisons, due diligence, market scans, technical investigations, or long-form analysis
- Use `research_profile` to steer deep-mode source collection toward the right domain (`technical`, `news`, or `academic`)
- Let the backend choose `max_sources` automatically unless your product has a strict cost or latency budget
- Set `include_source_content=true` only if the downstream system really needs raw source text
- Monitor `citation_audit.faithfulness_score` in production; scores below `0.5` indicate a response that may contain unsupported citations

## Research Profile Behavior

When `deep_mode=true`, the `research_profile` field selects dedicated OSS collectors that run in parallel alongside DuckDuckGo/Google:

| Profile | Extra Sources |
|---------|---------------|
| `technical` (default) | Wikipedia + StackExchange |
| `news` | HackerNews Algolia + Reuters/BBC/AP/AlJazeera RSS feeds |
| `academic` | arXiv + PubMed E-utilities |

In normal mode the profile field is accepted but the extra collectors are not triggered.

## Citation Faithfulness Audit

Every response includes a `citation_audit` object:

```json
{
  "total_citations": 4,
  "supported_citations": 3,
  "weak_citations": 1,
  "faithfulness_score": 0.75
}
```

Use `faithfulness_score` to detect potentially hallucinated citations before presenting results to end-users. A score of `1.0` means all `[N]` markers are backed by keyword evidence in the referenced source.

## Query Rewrite Behavior

The backend now includes a pre-retrieval query understanding stage.

This means:

- conversational or vague user input can still retrieve good results
- typo-heavy or weakly phrased prompts can be normalized before search
- the system may generate multiple safe search variants for the same user request

The rewrite stage is controlled by:

- `RESEARCH_ENABLE_QUERY_REWRITE`
- `RESEARCH_QUERY_REWRITE_MAX_VARIANTS`
- `RESEARCH_QUERY_REWRITE_TIMEOUT_SECONDS`

## Production Checklist

- Serve the backend behind `HTTPS`
- Replace local development API keys with strong secrets
- Keep `API_ALLOWED_ORIGINS` narrow for browser clients
- Put the backend behind a reverse proxy or load balancer
- Ensure the backend can reach the configured `OLLAMA_HOST`
- Tune `API_RESEARCH_RATE_LIMIT_PER_MINUTE` and research timeouts for your workload
- Monitor `response_ms`, `sources_checked`, and upstream failure rates

## Local Development Mapping

Backend env:

- [`.env`](../.env)

Frontend proxy env:

- [`web-ui/.env.local`](../web-ui/.env.local)

The local defaults currently assume:

- backend at `http://localhost:8000`
- model runtime at `http://localhost:11434`

## Minimal Implementation Checklist

1. Deploy the backend publicly over `HTTPS`.
2. Set a real `API_KEYS` value.
3. Confirm `GET /api/v1/tools` works from your app.
4. Register `web_research` in your agent runtime or workflow engine.
5. Call `POST /api/v1/tools/web-research` on tool invocation.
6. Return `answer`, `citations`, and `metadata` to your model or application.
