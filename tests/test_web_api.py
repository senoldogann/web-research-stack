"""Contract tests for the versioned web tool REST endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from web_scraper.api import create_app
from web_scraper.config import config
from web_scraper.web_tools.errors import WebToolError


class FakeWebTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("search", {"query": query, **kwargs}))
        return {
            "query": query,
            "provider": kwargs.get("provider") or "duckduckgo",
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com/",
                    "snippet": "Example snippet",
                    "source": "duckduckgo",
                }
            ],
            "timings": {"search_ms": 1.0, "total_ms": 2.0},
        }

    async def scrape(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("scrape", {"url": url, **kwargs}))
        return {
            "url": url,
            "requested_url": url,
            "title": "Example",
            "description": "",
            "canonical_url": url,
            "markdown": "# Example",
            "status_code": 200,
            "content_type": "text/html",
            "links": [],
            "metadata": {},
            "content_hash": "abc",
            "fetched_with": "http",
            "cache_hit": False,
            "warnings": [],
            "timings": {"total_ms": 3.0},
        }

    async def map(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("map", {"url": url, **kwargs}))
        return {
            "url": url,
            "urls": [url],
            "total": 1,
            "entries": [{"url": url, "lastmod": None, "source": "base"}],
            "sitemaps": [],
            "errors": [],
        }

    async def extract(self, url: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("extract", {"url": url, "schema": schema}))
        return {
            "url": url,
            "requested_url": url,
            "title": "Example",
            "description": "",
            "canonical_url": url,
            "status_code": 200,
            "data": {"name": "Example"},
            "provenance": {"name": {"method": "selector"}},
            "errors": [],
            "fetched_with": "http",
            "timings": {"total_ms": 4.0},
            "warnings": [],
        }

    async def start_crawl(self, url: str, options: Any = None) -> dict[str, Any]:
        self.calls.append(("start_crawl", {"url": url, "options": options}))
        return {
            "job_id": "job-1",
            "url": url,
            "status": "queued",
            "options": {"max_pages": 5, "max_depth": 1},
            "progress": {"fetched": 0, "total_results": 0},
            "errors": [],
        }

    async def get_crawl(self, job_id: str) -> dict[str, Any]:
        self.calls.append(("get_crawl", {"job_id": job_id}))
        return {
            "job_id": job_id,
            "url": "https://example.com/",
            "status": "running",
            "progress": {"fetched": 1},
        }

    async def crawl_results(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("crawl_results", {"job_id": job_id, **kwargs}))
        return {
            "job_id": job_id,
            "status": "completed",
            "total": 1,
            "offset": kwargs.get("offset", 0),
            "limit": kwargs.get("limit", 20),
            "results": [{"url": "https://example.com/"}],
            "errors": [],
            "duplicates": [],
        }

    async def cancel_crawl(self, job_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_crawl", {"job_id": job_id}))
        return {"job_id": job_id, "url": "https://example.com/", "status": "cancelled"}

    async def metrics_snapshot(self) -> dict[str, Any]:
        return {"latency": {}, "cache": {"entries": 0}, "browser": {"enabled": False}}


@pytest.fixture
def fake_tools() -> FakeWebTools:
    return FakeWebTools()


@pytest.fixture
def web_client(monkeypatch: pytest.MonkeyPatch, fake_tools: FakeWebTools) -> TestClient:
    monkeypatch.setattr(config, "api_keys", ["test-key"], raising=False)
    monkeypatch.setattr(config, "api_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_web_rate_limit_per_minute", 100, raising=False)
    monkeypatch.setattr(config, "api_allowed_origins", [], raising=False)
    monkeypatch.setattr(config, "api_trusted_hosts", ["testserver"], raising=False)
    monkeypatch.setattr(config, "history_db_path", None, raising=False)
    app = create_app(config)
    app.state.web_tools = fake_tools
    return TestClient(app)


HEADERS = {"X-API-Key": "test-key"}


def test_web_routes_require_auth(web_client: TestClient) -> None:
    assert web_client.post("/api/v1/web/search", json={"query": "hello"}).status_code == 401
    assert web_client.get("/api/v1/web/stats").status_code == 401


def test_manifest_lists_each_web_tool(web_client: TestClient) -> None:
    response = web_client.get("/api/v1/tools", headers=HEADERS)

    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["tools"]}
    assert {
        "web_research",
        "web_search",
        "web_scrape",
        "web_map",
        "web_extract",
        "web_crawl_start",
        "web_crawl_status",
        "web_crawl_results",
        "web_crawl_cancel",
    } <= names


def test_search_route_forwards_query_and_limit(
    web_client: TestClient, fake_tools: FakeWebTools
) -> None:
    response = web_client.post(
        "/api/v1/web/search",
        headers=HEADERS,
        json={"query": "example", "limit": 3, "provider": "google"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["url"] == "https://example.com/"
    assert fake_tools.calls == [
        ("search", {"query": "example", "limit": 3, "provider": "google", "depth": None})
    ]


def test_search_route_forwards_depth(web_client: TestClient, fake_tools: FakeWebTools) -> None:
    response = web_client.post(
        "/api/v1/web/search",
        headers=HEADERS,
        json={"query": "example", "depth": "fast"},
    )

    assert response.status_code == 200
    assert fake_tools.calls == [
        ("search", {"query": "example", "limit": None, "provider": None, "depth": "fast"})
    ]


def test_search_manifest_exposes_depth_and_facets(
    web_client: TestClient,
) -> None:
    response = web_client.get("/api/v1/tools", headers=HEADERS)
    tools = {tool["name"]: tool for tool in response.json()["tools"]}

    search = tools["web_search"]
    assert "depth" in search["input_schema"]["properties"]
    assert "facets" in search["output_schema"]["properties"]
    assert "source_type" in search["output_schema"]["$defs"]["WebSearchResult"]["properties"]


def test_scrape_route_forwards_markdown_limit(
    web_client: TestClient, fake_tools: FakeWebTools
) -> None:
    response = web_client.post(
        "/api/v1/web/scrape",
        headers=HEADERS,
        json={"url": "https://example.com", "max_chars": 1200},
    )

    assert response.status_code == 200
    assert response.json()["markdown"] == "# Example"
    assert fake_tools.calls == [("scrape", {"url": "https://example.com", "max_chars": 1200})]


def test_map_route_forwards_filters(web_client: TestClient, fake_tools: FakeWebTools) -> None:
    response = web_client.post(
        "/api/v1/web/map",
        headers=HEADERS,
        json={
            "url": "https://example.com/",
            "search": "docs",
            "include_patterns": ["*/docs/*"],
            "limit": 10,
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert fake_tools.calls[0][0] == "map"
    assert fake_tools.calls[0][1]["search"] == "docs"
    assert fake_tools.calls[0][1]["limit"] == 10


def test_extract_route_accepts_schema_alias(
    web_client: TestClient, fake_tools: FakeWebTools
) -> None:
    response = web_client.post(
        "/api/v1/web/extract",
        headers=HEADERS,
        json={"url": "https://example.com/", "schema": {"name": "h1"}},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"name": "Example"}
    assert fake_tools.calls == [
        ("extract", {"url": "https://example.com/", "schema": {"name": "h1"}})
    ]


def test_crawl_lifecycle_routes(web_client: TestClient, fake_tools: FakeWebTools) -> None:
    started = web_client.post(
        "/api/v1/web/crawl",
        headers=HEADERS,
        json={"url": "https://example.com/", "options": {"max_pages": 5, "max_depth": 1}},
    )
    assert started.status_code == 202
    assert started.json()["job_id"] == "job-1"

    status = web_client.get("/api/v1/web/crawl/job-1", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["status"] == "running"

    results = web_client.get(
        "/api/v1/web/crawl/job-1/results",
        headers=HEADERS,
        params={"offset": 0, "limit": 5},
    )
    assert results.status_code == 200
    assert results.json()["results"][0]["url"] == "https://example.com/"

    cancelled = web_client.post("/api/v1/web/crawl/job-1/cancel", headers=HEADERS)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    assert [name for name, _ in fake_tools.calls] == [
        "start_crawl",
        "get_crawl",
        "crawl_results",
        "cancel_crawl",
    ]


def test_web_tool_errors_are_mapped_to_http(
    web_client: TestClient, fake_tools: FakeWebTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise WebToolError(
            "blocked",
            "Target returned a captcha interstitial",
            url="https://example.com/",
            status_code=403,
            detail="captcha",
        )

    monkeypatch.setattr(fake_tools, "scrape", blocked)
    response = web_client.post(
        "/api/v1/web/scrape",
        headers=HEADERS,
        json={"url": "https://example.com/"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "blocked"
    assert payload["error"]["detail"] == "captcha"
    assert payload["trace_id"]


def test_stats_returns_runtime_metrics(web_client: TestClient) -> None:
    response = web_client.get("/api/v1/web/stats", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["cache"]["entries"] == 0
