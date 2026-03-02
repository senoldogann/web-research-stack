"""REST API for the web scraper and LLM-facing research tools."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

import web_scraper.research_agent as research_module
from web_scraper import __version__
from web_scraper.api_models import (
    BatchScrapeRequest,
    DataTableRow,
    LegacyResearchResponse,
    ResearchCitation,
    ResearchMetadata,
    ResearchSource,
    ResearchToolRequest,
    ScrapeRequest,
    ToolDescriptor,
    ToolsManifest,
    WebResearchResponse,
    _coerce_str,
)
from web_scraper.api_runtime import (
    CircuitBreaker,
    CircuitBreakerOpen,
    ConcurrencyGate,
    InMemoryTTLCache,
    MetricsRegistry,
    RateLimitExceeded,
    ResearchHistoryStore,
    SlidingWindowRateLimiter,
    stable_hash,
)
from web_scraper.async_scrapers import WebScraperAsync
from web_scraper.config import Config, config
from web_scraper.content_safety import sanitize_scraped_text, summarize_snippet
from web_scraper.scrapers import ScrapedData, WebScraper

logger = logging.getLogger("web_scraper.api")


class JsonFormatter(logging.Formatter):
    """Structured JSON logs without leaking raw user prompts."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in (
            "trace_id",
            "path",
            "method",
            "status_code",
            "duration_ms",
            "client",
            "query_hash",
            "query_length",
            "cached",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True)


def configure_logging(level: str) -> None:
    """Configure JSON logging once for the process.

    Uvicorn's access logger writes a plain-text line *and* propagates to root,
    which would double-log every request.  We silence it here because our
    RequestLoggingMiddleware already emits a structured JSON line per request.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level.upper())

    # Prevent uvicorn's access log from emitting a second plain-text line.
    # Our middleware already covers request logging in structured JSON.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False


def _extract_api_key(request: Request) -> Optional[str]:
    header_key = request.headers.get("X-API-Key")
    if header_key:
        return header_key.strip()

    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()

    return None


async def authenticate_request(request: Request) -> str:
    """Validate API keys when configured and create a stable caller id."""
    settings: Config = request.app.state.settings
    api_key = _extract_api_key(request)

    if settings.api_keys:
        if not api_key or api_key not in settings.api_keys:
            raise HTTPException(status_code=401, detail="Missing or invalid API key")
        subject = stable_hash(api_key)[:16]
    else:
        subject = request.client.host if request.client else "anonymous"

    request.state.auth_subject = subject
    return subject


async def apply_rate_limit(request: Request, subject: str, limit: int) -> None:
    """Apply per-route rate limiting."""
    limiter: SlidingWindowRateLimiter = request.app.state.rate_limiter
    key = f"{request.url.path}:{subject}"
    remaining, window = await limiter.check(key, limit=limit)
    request.state.rate_limit_remaining = remaining
    request.state.rate_limit_window = window


def require_access(limit_attr: str) -> Callable[[Request], Awaitable[str]]:
    """Create a dependency that authenticates and rate-limits a request."""

    async def dependency(request: Request) -> str:
        subject = await authenticate_request(request)
        settings: Config = request.app.state.settings
        await apply_rate_limit(request, subject, getattr(settings, limit_attr))
        return subject

    return dependency


require_general_access = require_access("api_rate_limit_per_minute")
require_scrape_access = require_access("api_scrape_rate_limit_per_minute")
require_research_access = require_access("api_research_rate_limit_per_minute")


def _request_duration_ms(request: Request) -> float:
    started = getattr(request.state, "started_at", None)
    if started is None:
        return 0.0
    return round((time.perf_counter() - started) * 1000, 2)


def _build_cache_key(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return stable_hash(f"{prefix}:{encoded}")


def _apply_security_headers(response, request: Request):
    """Attach baseline security headers to API responses."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")

    content_type = response.headers.get("content-type", "").lower()
    if (
        request.url.path.startswith("/api/")
        and "cache-control" not in {key.lower() for key in response.headers.keys()}
        and content_type.startswith("application/json")
    ):
        response.headers["Cache-Control"] = "no-store"

    return response


def _serialize_scraped_result(result: ScrapedData, payload: ScrapeRequest) -> dict[str, Any]:
    response_data = {
        "url": result.url,
        "title": result.title,
        "content": sanitize_scraped_text(
            result.content, max_chars=config.scraper_max_raw_text_chars
        ),
        "status_code": result.status_code,
        "response_time": result.response_time,
        "error": result.error,
    }

    if payload.include_metadata:
        response_data["metadata"] = result.metadata
    if payload.include_links:
        response_data["links"] = result.links
    if payload.include_images:
        response_data["images"] = result.images

    return response_data


def _map_sources(
    raw_sources: list[dict[str, Any]],
    include_source_content: bool,
    max_chars: int,
) -> tuple[list[ResearchCitation], list[ResearchSource]]:
    citations: list[ResearchCitation] = []
    sources: list[ResearchSource] = []

    for source in raw_sources:
        sanitized_content = sanitize_scraped_text(source.get("content") or "", max_chars=max_chars)
        research_source = ResearchSource(
            source=source.get("source", "unknown"),
            url=source.get("url", ""),
            title=source.get("title", ""),
            content=sanitized_content if include_source_content and sanitized_content else None,
            relevance_score=float(source.get("relevance_score", 0.0) or 0.0),
            error=source.get("error"),
            source_tier=int(source.get("source_tier", 5) or 5),
            publication_date=source.get("publication_date"),
        )
        sources.append(research_source)

        if source.get("error"):
            continue

        citations.append(
            ResearchCitation(
                source=research_source.source,
                url=research_source.url,
                title=research_source.title,
                relevance_score=research_source.relevance_score,
                snippet=summarize_snippet(sanitized_content),
                source_tier=research_source.source_tier,
                publication_date=research_source.publication_date,
            )
        )

    return citations, sources


def _map_research_payload(
    raw_payload: dict[str, Any],
    request_payload: ResearchToolRequest,
    trace_id: str,
    model_name: str,
    response_ms: float,
    cached: bool,
    settings: Config,
) -> WebResearchResponse:
    citations, sources = _map_sources(
        raw_sources=raw_payload.get("sources", []),
        include_source_content=request_payload.include_source_content,
        max_chars=settings.max_source_content_chars,
    )
    metadata = ResearchMetadata(
        model=model_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        sources_checked=int(raw_payload.get("sources_checked", 0)),
        sources_succeeded=int(raw_payload.get("sources_succeeded", 0)),
        cached=cached,
        trace_id=trace_id,
        response_ms=response_ms,
    )

    summary = _coerce_str(
        raw_payload.get("summary", "") or raw_payload.get("executive_summary", "") or ""
    )
    return WebResearchResponse(
        query=raw_payload.get("query", request_payload.query),
        answer=summary,
        summary=summary,
        key_findings=list(raw_payload.get("key_findings", [])),
        detailed_analysis=_coerce_str(raw_payload.get("detailed_analysis", "")),
        recommendations=_coerce_str(raw_payload.get("recommendations", "")),
        executive_summary=_coerce_str(raw_payload.get("executive_summary", "")),
        data_table=[
            DataTableRow(**row) if isinstance(row, dict) else row
            for row in raw_payload.get("data_table", [])
        ],
        conflicts_uncertainty=list(raw_payload.get("conflicts_uncertainty", [])),
        confidence_level=raw_payload.get("confidence_level", "Medium") or "Medium",
        confidence_reason=raw_payload.get("confidence_reason", "") or "",
        citations=citations,
        sources=sources,
        metadata=metadata,
    )


def _map_research_report(
    report: research_module.ResearchReport,
    request_payload: ResearchToolRequest,
    trace_id: str,
    model_name: str,
    response_ms: float,
    cached: bool,
    settings: Config,
) -> WebResearchResponse:
    raw_payload = {
        "query": report.query,
        "summary": report.summary,
        "executive_summary": report.executive_summary,
        "key_findings": report.key_findings,
        "detailed_analysis": report.detailed_analysis,
        "recommendations": report.recommendations,
        "data_table": report.data_table,
        "conflicts_uncertainty": report.conflicts_uncertainty,
        "confidence_level": report.confidence_level,
        "confidence_reason": report.confidence_reason,
        "sources": [
            {
                "source": source.source,
                "url": source.url,
                "title": source.title,
                "content": source.content,
                "relevance_score": source.relevance_score,
                "source_tier": source.source_tier,
                "publication_date": source.publication_date,
                "error": source.error,
            }
            for source in report.sources
        ],
        "sources_checked": report.sources_checked,
        "sources_succeeded": report.sources_succeeded,
    }
    return _map_research_payload(
        raw_payload=raw_payload,
        request_payload=request_payload,
        trace_id=trace_id,
        model_name=model_name,
        response_ms=response_ms,
        cached=cached,
        settings=settings,
    )


def _map_legacy_response(payload: WebResearchResponse) -> LegacyResearchResponse:
    return LegacyResearchResponse(
        query=payload.query,
        summary=payload.summary,
        executive_summary=payload.executive_summary,
        key_findings=payload.key_findings,
        detailed_analysis=payload.detailed_analysis,
        recommendations=payload.recommendations,
        data_table=payload.data_table,
        conflicts_uncertainty=payload.conflicts_uncertainty,
        confidence_level=payload.confidence_level,
        confidence_reason=payload.confidence_reason,
        sources=payload.sources,
        sources_checked=payload.metadata.sources_checked,
        sources_succeeded=payload.metadata.sources_succeeded,
    )


def _tool_manifest(settings: Config) -> ToolsManifest:
    auth_info = {
        "type": "api_key",
        "header": "X-API-Key",
        "bearer_supported": True,
        "required": bool(settings.api_keys),
    }
    return ToolsManifest(
        tools=[
            ToolDescriptor(
                name="web_research",
                description=(
                    "Search the web across multiple sources, synthesize findings, and return "
                    "citations for LLMs that do not have native browsing."
                ),
                method="POST",
                path="/api/v1/tools/web-research",
                stream_path="/api/v1/tools/web-research/stream",
                auth=auth_info,
                input_schema=ResearchToolRequest.model_json_schema(),
                output_schema=WebResearchResponse.model_json_schema(),
                example={
                    "query": "Latest developments in AI coding agents",
                    "max_sources": 5,
                    "deep_mode": False,
                    "include_source_content": False,
                },
            )
        ]
    )


def _build_research_agent(
    settings: Config,
    requested_model: Optional[str],
    provider: str = "ollama",
    openai_api_key: Optional[str] = None,
):
    model_name = requested_model or settings.default_research_model
    agent = research_module.ResearchAgent(
        model=model_name,
        host=settings.ollama_host,
        max_concurrent=settings.research_max_concurrent_sources,
        timeout_per_source=settings.research_timeout_per_source,
        provider=provider,
        openai_api_key=openai_api_key,
    )
    return agent, model_name


async def _record_history(
    request: Request,
    *,
    route: str,
    query: str,
    model: Optional[str],
    success: bool,
    cached: bool,
    error: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    store: ResearchHistoryStore = request.app.state.history_store
    await asyncio.to_thread(
        store.record,
        route=route,
        query_hash=stable_hash(query),
        query_length=len(query),
        model=model,
        success=success,
        cached=cached,
        response_ms=_request_duration_ms(request),
        trace_id=request.state.trace_id,
        error=error,
        metadata=metadata,
    )


async def _run_research(
    request: Request,
    payload: ResearchToolRequest,
    route_name: str,
) -> WebResearchResponse:
    settings: Config = request.app.state.settings
    cache: InMemoryTTLCache = request.app.state.cache
    metrics: MetricsRegistry = request.app.state.metrics
    breaker: CircuitBreaker = request.app.state.circuit_breaker
    gate: ConcurrencyGate = request.app.state.concurrency_gate

    cache_key = _build_cache_key(route_name, payload.model_dump(mode="json"))
    cached_value = await cache.get(cache_key)
    if cached_value is not None:
        metrics.increment("web_scraper_cache_hits_total", endpoint=route_name)
        cached_response = WebResearchResponse.model_validate(cached_value)
        refreshed = cached_response.model_copy(
            update={
                "metadata": cached_response.metadata.model_copy(
                    update={
                        "cached": True,
                        "trace_id": request.state.trace_id,
                        "response_ms": _request_duration_ms(request),
                    }
                )
            }
        )
        await _record_history(
            request,
            route=route_name,
            query=payload.query,
            model=refreshed.metadata.model,
            success=True,
            cached=True,
            metadata={
                "sources_checked": refreshed.metadata.sources_checked,
                "sources_succeeded": refreshed.metadata.sources_succeeded,
            },
        )
        return refreshed

    breaker.ensure_available()
    agent, model_name = _build_research_agent(
        settings,
        payload.model,
        provider=getattr(payload, "provider", "ollama"),
        openai_api_key=getattr(payload, "openai_api_key", None),
    )

    available = await asyncio.to_thread(agent.is_available)
    if not available:
        breaker.record_failure("ollama_unavailable")
        metrics.increment(
            "web_scraper_upstream_failures_total", endpoint=route_name, reason="unavailable"
        )
        await _record_history(
            request,
            route=route_name,
            query=payload.query,
            model=model_name,
            success=False,
            cached=False,
            error="Research backend unavailable",
        )
        raise HTTPException(status_code=503, detail="Research backend unavailable")

    try:
        async with gate.acquire():
            report = await agent.research(
                payload.query,
                max_sources=payload.max_sources,
                deep_mode=payload.deep_mode,
            )
    except Exception as exc:
        breaker.record_failure(type(exc).__name__)
        metrics.increment(
            "web_scraper_upstream_failures_total",
            endpoint=route_name,
            reason=type(exc).__name__,
        )
        await _record_history(
            request,
            route=route_name,
            query=payload.query,
            model=model_name,
            success=False,
            cached=False,
            error="Research backend failed",
            metadata={"exception": type(exc).__name__},
        )
        logger.exception(
            "research_failed",
            extra={
                "trace_id": request.state.trace_id,
                "path": route_name,
                "query_hash": stable_hash(payload.query)[:16],
                "query_length": len(payload.query),
            },
        )
        raise HTTPException(status_code=502, detail="Research backend failed") from exc

    breaker.record_success()
    response_payload = _map_research_report(
        report=report,
        request_payload=payload,
        trace_id=request.state.trace_id,
        model_name=model_name,
        response_ms=_request_duration_ms(request),
        cached=False,
        settings=settings,
    )
    await cache.set(cache_key, response_payload.model_dump(mode="json"))
    await _record_history(
        request,
        route=route_name,
        query=payload.query,
        model=model_name,
        success=True,
        cached=False,
        metadata={
            "sources_checked": response_payload.metadata.sources_checked,
            "sources_succeeded": response_payload.metadata.sources_succeeded,
        },
    )
    return response_payload


async def _stream_research(
    request: Request,
    payload: ResearchToolRequest,
    route_name: str,
    legacy: bool,
):
    settings: Config = request.app.state.settings
    cache: InMemoryTTLCache = request.app.state.cache
    metrics: MetricsRegistry = request.app.state.metrics
    breaker: CircuitBreaker = request.app.state.circuit_breaker
    gate: ConcurrencyGate = request.app.state.concurrency_gate

    cache_key = _build_cache_key(route_name, payload.model_dump(mode="json"))
    cached_value = await cache.get(cache_key)
    if cached_value is not None:
        metrics.increment("web_scraper_cache_hits_total", endpoint=route_name)
        cached_response = WebResearchResponse.model_validate(cached_value).model_copy(
            update={
                "metadata": WebResearchResponse.model_validate(cached_value).metadata.model_copy(
                    update={
                        "cached": True,
                        "trace_id": request.state.trace_id,
                        "response_ms": _request_duration_ms(request),
                    }
                )
            }
        )
        payload_to_send = (
            _map_legacy_response(cached_response).model_dump(mode="json")
            if legacy
            else cached_response.model_dump(mode="json")
        )
        await _record_history(
            request,
            route=route_name,
            query=payload.query,
            model=cached_response.metadata.model,
            success=True,
            cached=True,
            metadata={
                "sources_checked": cached_response.metadata.sources_checked,
                "sources_succeeded": cached_response.metadata.sources_succeeded,
            },
        )
        yield f"data: {json.dumps({'type': 'status', 'message': 'Serving cached research result.'}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'result', 'data': payload_to_send}, ensure_ascii=False)}\n\n"
        return

    breaker.ensure_available()
    agent, model_name = _build_research_agent(
        settings,
        payload.model,
        provider=getattr(payload, "provider", "ollama"),
        openai_api_key=getattr(payload, "openai_api_key", None),
    )
    available = await asyncio.to_thread(agent.is_available)
    if not available:
        breaker.record_failure("ollama_unavailable")
        await _record_history(
            request,
            route=route_name,
            query=payload.query,
            model=model_name,
            success=False,
            cached=False,
            error="Research backend unavailable",
        )
        raise HTTPException(status_code=503, detail="Research backend unavailable")

    try:
        async with gate.acquire():
            async for chunk in agent.research_stream(
                payload.query,
                max_sources=payload.max_sources,
                deep_mode=payload.deep_mode,
            ):
                if await request.is_disconnected():
                    logger.info("client_disconnected", extra={"trace_id": request.state.trace_id})
                    break

                if not chunk.startswith("data: "):
                    yield chunk
                    continue

                data_str = chunk[6:].strip()
                if not data_str:
                    continue

                try:
                    parsed = json.loads(data_str)
                except json.JSONDecodeError:
                    yield chunk
                    continue

                if parsed.get("type") != "result":
                    yield chunk
                    continue

                mapped_payload = _map_research_payload(
                    raw_payload=parsed.get("data", {}),
                    request_payload=payload,
                    trace_id=request.state.trace_id,
                    model_name=model_name,
                    response_ms=_request_duration_ms(request),
                    cached=False,
                    settings=settings,
                )
                breaker.record_success()
                await cache.set(cache_key, mapped_payload.model_dump(mode="json"))
                await _record_history(
                    request,
                    route=route_name,
                    query=payload.query,
                    model=model_name,
                    success=True,
                    cached=False,
                    metadata={
                        "sources_checked": mapped_payload.metadata.sources_checked,
                        "sources_succeeded": mapped_payload.metadata.sources_succeeded,
                    },
                )
                payload_to_send = (
                    _map_legacy_response(mapped_payload).model_dump(mode="json")
                    if legacy
                    else mapped_payload.model_dump(mode="json")
                )
                yield f"data: {json.dumps({'type': 'result', 'data': payload_to_send}, ensure_ascii=False)}\n\n"
    except HTTPException:
        raise
    except Exception as exc:
        breaker.record_failure(type(exc).__name__)
        await _record_history(
            request,
            route=route_name,
            query=payload.query,
            model=model_name,
            success=False,
            cached=False,
            error="Research stream failed",
            metadata={"exception": type(exc).__name__},
        )
        logger.exception(
            "research_stream_failed",
            extra={
                "trace_id": request.state.trace_id,
                "path": route_name,
                "query_hash": stable_hash(payload.query)[:16],
                "query_length": len(payload.query),
            },
        )
        yield f"data: {json.dumps({'type': 'error', 'message': 'Research stream failed. Please check your model configuration and API key.'}, ensure_ascii=False)}\n\n"


def create_app(settings: Optional[Config] = None) -> FastAPI:
    """Create the FastAPI app with runtime services attached."""
    app_settings = settings or config
    configure_logging(app_settings.log_level)

    app = FastAPI(
        title="Web Scraper API",
        description="Versioned scraping and research API for LLM tool integrations.",
        version=__version__,
    )

    app.state.settings = app_settings
    app.state.rate_limiter = SlidingWindowRateLimiter(app_settings.api_rate_limit_per_minute)
    app.state.cache = InMemoryTTLCache(
        ttl_seconds=app_settings.cache_ttl_seconds,
        max_entries=app_settings.cache_max_entries,
    )
    app.state.circuit_breaker = CircuitBreaker(
        failure_threshold=app_settings.circuit_breaker_failure_threshold,
        recovery_seconds=app_settings.circuit_breaker_recovery_seconds,
    )
    app.state.metrics = MetricsRegistry()
    app.state.history_store = ResearchHistoryStore(app_settings.history_db_path)
    app.state.concurrency_gate = ConcurrencyGate(app_settings.api_max_concurrent_requests)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.api_allowed_origins,  # fail closed: set API_ALLOWED_ORIGINS; empty list blocks all cross-origin requests
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Trace-ID"],
        expose_headers=["X-Trace-ID", "X-RateLimit-Remaining", "X-RateLimit-Window"],
    )
    if app_settings.api_trusted_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=app_settings.api_trusted_hosts,
        )

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):
        request.state.trace_id = request.headers.get("X-Trace-ID") or str(uuid.uuid4())
        request.state.started_at = time.perf_counter()

        if request.method in {"POST", "PUT", "PATCH"}:
            max_request_bytes = app_settings.api_max_request_bytes
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > max_request_bytes:
                        return _apply_security_headers(
                            JSONResponse(
                                status_code=413,
                                content={
                                    "error": "Request body too large",
                                    "max_request_bytes": max_request_bytes,
                                    "trace_id": request.state.trace_id,
                                },
                            ),
                            request,
                        )
                except ValueError:
                    return _apply_security_headers(
                        JSONResponse(
                            status_code=400,
                            content={
                                "error": "Invalid Content-Length header",
                                "trace_id": request.state.trace_id,
                            },
                        ),
                        request,
                    )

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = _request_duration_ms(request)
            app.state.metrics.increment(
                "web_scraper_requests_total",
                endpoint=request.url.path,
                method=request.method,
                status="500",
            )
            logger.exception(
                "unhandled_request_error",
                extra={
                    "trace_id": request.state.trace_id,
                    "path": request.url.path,
                    "method": request.method,
                    "duration_ms": duration_ms,
                    "client": request.client.host if request.client else "unknown",
                },
            )
            return _apply_security_headers(
                JSONResponse(
                    status_code=500,
                    content={"error": "Internal server error", "trace_id": request.state.trace_id},
                ),
                request,
            )

        duration_ms = _request_duration_ms(request)
        response.headers["X-Trace-ID"] = request.state.trace_id
        if hasattr(request.state, "rate_limit_remaining"):
            response.headers["X-RateLimit-Remaining"] = str(request.state.rate_limit_remaining)
        if hasattr(request.state, "rate_limit_window"):
            response.headers["X-RateLimit-Window"] = str(request.state.rate_limit_window)

        app.state.metrics.increment(
            "web_scraper_requests_total",
            endpoint=request.url.path,
            method=request.method,
            status=str(response.status_code),
        )
        concurrency = await app.state.concurrency_gate.snapshot()
        app.state.metrics.set_gauge("web_scraper_active_requests", concurrency["active"])
        logger.info(
            "request_complete",
            extra={
                "trace_id": request.state.trace_id,
                "path": request.url.path,
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else "unknown",
            },
        )
        return _apply_security_headers(response, request)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": "Validation failed",
                "details": exc.errors(),
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": detail,
                "details": exc.detail if not isinstance(exc.detail, str) else None,
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded):
        response = JSONResponse(
            status_code=429,
            content={
                "error": "Rate limit exceeded",
                "retry_after_seconds": exc.retry_after,
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(CircuitBreakerOpen)
    async def circuit_breaker_exception_handler(request: Request, exc: CircuitBreakerOpen):
        response = JSONResponse(
            status_code=503,
            content={
                "error": "Research backend temporarily unavailable",
                "retry_after_seconds": exc.retry_after,
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.get("/", tags=["Root"])
    async def root() -> dict[str, Any]:
        return {
            "name": "Web Scraper API",
            "version": __version__,
            "docs": "/docs",
            "endpoints": {
                "health": "/api/v1/health",
                "metrics": "/api/v1/metrics",
                "tools": "/api/v1/tools",
                "scrape": "/api/v1/scrape",
                "research_tool": "/api/v1/tools/web-research",
            },
        }

    @app.get("/health", tags=["Health"])
    @app.get("/api/v1/health", tags=["Health"])
    async def health_check() -> dict[str, Any]:
        gate_snapshot = await app.state.concurrency_gate.snapshot()
        history_snapshot = app.state.history_store.snapshot()
        cache_snapshot = await app.state.cache.snapshot()
        breaker_snapshot = app.state.circuit_breaker.snapshot()
        agent, model_name = _build_research_agent(app_settings, None)
        ollama_available = await asyncio.to_thread(agent.is_available)
        status_label = (
            "healthy" if ollama_available and not breaker_snapshot["open"] else "degraded"
        )

        return {
            "status": status_label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dependencies": {
                "ollama": {
                    "available": ollama_available,
                    "host": app_settings.ollama_host,
                    "model": model_name,
                },
                "circuit_breaker": breaker_snapshot,
                "cache": cache_snapshot,
                "history_store": history_snapshot,
                "concurrency": gate_snapshot,
            },
        }

    @app.get("/api/v1/metrics", tags=["Monitoring"])
    async def metrics_endpoint(_: str = Depends(require_general_access)) -> PlainTextResponse:
        return PlainTextResponse(app.state.metrics.render_prometheus(), media_type="text/plain")

    @app.get("/api/v1/tools", tags=["Tools"])
    async def tools_manifest(_: str = Depends(require_general_access)) -> dict[str, Any]:
        return _tool_manifest(app_settings).model_dump(mode="json")

    @app.post("/api/v1/scrape", tags=["Scraping"])
    async def scrape_url(
        payload: ScrapeRequest,
        _: str = Depends(require_scrape_access),
    ) -> dict[str, Any]:
        with WebScraper(
            timeout=payload.timeout or app_settings.timeout,
            max_links=payload.max_links or app_settings.max_links,
        ) as scraper:
            result = await asyncio.to_thread(scraper.scrape, payload.url)

        if result.error:
            raise HTTPException(status_code=400, detail=result.error)

        return _serialize_scraped_result(result, payload)

    @app.post("/api/v1/scrape/batch", tags=["Scraping"])
    async def scrape_batch(
        payload: BatchScrapeRequest,
        _: str = Depends(require_scrape_access),
    ) -> dict[str, Any]:
        async with app.state.concurrency_gate.acquire():
            async with WebScraperAsync(timeout=payload.timeout or app_settings.timeout) as scraper:
                results = await scraper.scrape_batch(
                    payload.urls,
                    payload.max_concurrent or app_settings.api_max_concurrent_requests,
                )

        serialized = [
            {
                "url": result.url,
                "title": result.title,
                "content": sanitize_scraped_text(
                    result.content, max_chars=config.scraper_max_raw_text_chars
                ),
                "status_code": result.status_code,
                "error": result.error,
            }
            for result in results
        ]
        return {"results": serialized, "count": len(serialized)}

    @app.post("/api/v1/tools/web-research", tags=["Research"], response_model=WebResearchResponse)
    async def research_tool(
        payload: ResearchToolRequest,
        request: Request,
        _: str = Depends(require_research_access),
    ) -> WebResearchResponse:
        return await _run_research(request, payload, route_name="/api/v1/tools/web-research")

    @app.post("/api/v1/tools/web-research/stream", tags=["Research"])
    async def research_tool_stream(
        payload: ResearchToolRequest,
        request: Request,
        _: str = Depends(require_research_access),
    ) -> StreamingResponse:
        return StreamingResponse(
            _stream_research(
                request,
                payload,
                route_name="/api/v1/tools/web-research/stream",
                legacy=False,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/research", tags=["Research"], response_model=LegacyResearchResponse)
    @app.post("/api/research", tags=["Research"], response_model=LegacyResearchResponse)
    async def legacy_research(
        payload: ResearchToolRequest,
        request: Request,
        _: str = Depends(require_research_access),
    ) -> LegacyResearchResponse:
        response_payload = await _run_research(request, payload, route_name="/api/v1/research")
        return _map_legacy_response(response_payload)

    @app.post("/api/v1/research/stream", tags=["Research"])
    @app.post("/api/research/stream", tags=["Research"])
    async def legacy_research_stream(
        payload: ResearchToolRequest,
        request: Request,
        _: str = Depends(require_research_access),
    ) -> StreamingResponse:
        return StreamingResponse(
            _stream_research(
                request,
                payload,
                route_name="/api/v1/research/stream",
                legacy=True,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.api_host, port=config.api_port)
