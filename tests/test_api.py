"""API contract tests for LLM-facing research endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from web_scraper.api import create_app
from web_scraper.config import config
from web_scraper.research_agent import ResearchReport, ResearchResult


class DummyResearchAgent:
    """Deterministic agent used to test the API contract."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.model = kwargs.get("model")

    def is_available(self) -> bool:
        return True

    async def research(
        self,
        query: str,
        max_sources: int | None = None,
        deep_mode: bool = False,
    ) -> ResearchReport:
        source = ResearchResult(
            source="docs",
            url="https://example.com/article",
            title="Example Article",
            content="A cited source about the query.",
            relevance_score=0.91,
        )
        return ResearchReport(
            query=query,
            sources=[source],
            summary="A compact answer for model consumption.",
            key_findings=["Finding 1", "Finding 2"],
            detailed_analysis="Long form analysis.",
            recommendations="Use the cited facts in the final answer.",
            sources_checked=max_sources or 1,
            sources_succeeded=1,
        )

    async def research_stream(
        self,
        query: str,
        max_sources: int | None = None,
        deep_mode: bool = False,
    ):
        yield 'data: {"type":"status","message":"starting"}\n\n'
        yield f'data: {{"type":"result","data":{{"query":"{query}","summary":"done"}}}}\n\n'


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config, "api_keys", ["test-key"], raising=False)
    monkeypatch.setattr(config, "api_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_scrape_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_research_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_allowed_origins", ["http://localhost:3000"], raising=False)
    monkeypatch.setattr(config, "api_trusted_hosts", ["testserver"], raising=False)
    monkeypatch.setattr(config, "ollama_host", "http://ollama.local", raising=False)
    monkeypatch.setattr(config, "default_research_model", "demo-model", raising=False)
    monkeypatch.setattr(config, "history_db_path", None, raising=False)
    monkeypatch.setattr("web_scraper.research_agent.ResearchAgent", DummyResearchAgent)
    return TestClient(create_app(config))


def test_tools_manifest_lists_web_research_tool(client: TestClient) -> None:
    response = client.get("/api/v1/tools", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["tools"][0]["name"] == "web_research"
    assert payload["tools"][0]["method"] == "POST"
    assert payload["tools"][0]["path"] == "/api/v1/tools/web-research"
    assert "input_schema" in payload["tools"][0]


def test_web_research_requires_api_key(client: TestClient) -> None:
    response = client.post("/api/v1/tools/web-research", json={"query": "latest ai agents"})

    assert response.status_code == 401


def test_web_research_returns_structured_payload(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tools/web-research",
        headers={"X-API-Key": "test-key"},
        json={"query": "latest ai agents", "max_sources": 3},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "latest ai agents"
    assert payload["answer"] == "A compact answer for model consumption."
    assert payload["citations"][0]["url"] == "https://example.com/article"
    assert payload["metadata"]["sources_checked"] == 3
    assert payload["metadata"]["sources_succeeded"] == 1


def test_web_research_validates_query_length(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tools/web-research",
        headers={"X-API-Key": "test-key"},
        json={"query": "hi"},
    )

    assert response.status_code == 422


def test_web_research_accepts_up_to_fifty_sources(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tools/web-research",
        headers={"X-API-Key": "test-key"},
        json={"query": "latest ai agents", "max_sources": 50, "deep_mode": True},
    )

    assert response.status_code == 200


def test_web_research_stream_returns_sse(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tools/web-research/stream",
        headers={"X-API-Key": "test-key"},
        json={"query": "latest ai agents"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"type":"status","message":"starting"}' in response.text


def test_rate_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "api_keys", ["test-key"], raising=False)
    monkeypatch.setattr(config, "api_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_research_rate_limit_per_minute", 1, raising=False)
    monkeypatch.setattr(config, "api_trusted_hosts", ["testserver"], raising=False)
    monkeypatch.setattr(config, "history_db_path", None, raising=False)
    monkeypatch.setattr("web_scraper.research_agent.ResearchAgent", DummyResearchAgent)

    with TestClient(create_app(config)) as local_client:
        first = local_client.post(
            "/api/v1/tools/web-research",
            headers={"X-API-Key": "test-key"},
            json={"query": "latest ai agents"},
        )
        second = local_client.post(
            "/api/v1/tools/web-research",
            headers={"X-API-Key": "test-key"},
            json={"query": "another query"},
        )

    assert first.status_code == 200
    assert second.status_code == 429


def test_request_body_size_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "api_keys", ["test-key"], raising=False)
    monkeypatch.setattr(config, "api_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_research_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_max_request_bytes", 120, raising=False)
    monkeypatch.setattr(config, "history_db_path", None, raising=False)
    monkeypatch.setattr("web_scraper.research_agent.ResearchAgent", DummyResearchAgent)

    with TestClient(create_app(config)) as local_client:
        response = local_client.post(
            "/api/v1/tools/web-research",
            headers={"X-API-Key": "test-key"},
            json={"query": "x" * 200},
        )

    assert response.status_code == 413


def test_trusted_host_middleware_allows_expected_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "api_keys", ["test-key"], raising=False)
    monkeypatch.setattr(config, "api_trusted_hosts", ["allowed.test"], raising=False)
    monkeypatch.setattr(config, "history_db_path", None, raising=False)
    monkeypatch.setattr("web_scraper.research_agent.ResearchAgent", DummyResearchAgent)

    with TestClient(create_app(config), base_url="http://allowed.test") as local_client:
        response = local_client.get("/api/v1/health")

    assert response.status_code == 200
