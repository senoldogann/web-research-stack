"""Dependency-free stdio MCP server exposing the local web tools to Dot."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Callable, Coroutine, Optional

from pydantic import BaseModel, Field, ValidationError

from web_scraper import __version__
from web_scraper.api_models import (
    WebCrawlRequest,
    WebExtractRequest,
    WebMapRequest,
    WebScrapeRequest,
    WebSearchRequest,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.service import WebToolsService

logger = logging.getLogger("web_scraper.mcp")

_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
_SERVER_INFO = {"name": "web-research", "version": __version__}
_TOOL_CONCURRENCY = 8


class _ArgumentError(ValueError):
    """A tool argument that does not match the published schema."""


class _JobIdArguments(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=128)


class _CrawlResultsArguments(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=1000)


async def _search(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = WebSearchRequest.model_validate(arguments)
    return await service.search(
        request.query,
        limit=request.limit,
        provider=request.provider,
        depth=request.depth,
    )


async def _scrape(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = WebScrapeRequest.model_validate(arguments)
    return await service.scrape(request.url, max_chars=request.max_chars)


async def _map(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = WebMapRequest.model_validate(arguments)
    return await service.map(
        request.url,
        include_subdomains=request.include_subdomains,
        search=request.search,
        include_patterns=request.include_patterns,
        exclude_patterns=request.exclude_patterns,
        limit=request.limit,
    )


async def _extract(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = WebExtractRequest.model_validate(arguments)
    return await service.extract(request.url, request.extraction_schema)


async def _crawl_start(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = WebCrawlRequest.model_validate(arguments)
    options = request.options.model_dump(exclude_none=True) if request.options else None
    return await service.start_crawl(request.url, options)


async def _crawl_status(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = _JobIdArguments.model_validate(arguments)
    return await service.get_crawl(request.job_id)


async def _crawl_results(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = _CrawlResultsArguments.model_validate(arguments)
    return await service.crawl_results(
        request.job_id,
        offset=request.offset,
        limit=request.limit,
    )


async def _crawl_cancel(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = _JobIdArguments.model_validate(arguments)
    return await service.cancel_crawl(request.job_id)


Handler = Callable[[WebToolsService, dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


def _annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> dict[str, Any]:
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def _tool_specs() -> list[tuple[dict[str, Any], Handler]]:
    return [
        (
            {
                "name": "web_search",
                "description": (
                    "Discover result URLs with a replaceable search provider. depth=discover "
                    "(default) runs base, current-year, and historical queries; depth=deep adds "
                    "an official or primary-source angle; depth=fast sends one query. Ranking "
                    "uses field-weighted BM25, weighted cross-angle consensus, source authority, "
                    "inferred recency, and near-duplicate/domain diversity. Explicit freshness "
                    "queries get provider date filters; responses are cached per query and a "
                    "blocking provider is cooled down. Results carry source_type, year, recency, "
                    "angles, relevance, bm25, score, and cache_hit, with coverage facets. Search "
                    "discovers URLs only; use web_scrape to read a result."
                ),
                "inputSchema": WebSearchRequest.model_json_schema(),
                "annotations": _annotations(
                    read_only=True, destructive=False, idempotent=True, open_world=True
                ),
            },
            _search,
        ),
        (
            {
                "name": "web_scrape",
                "description": (
                    "Fetch one public URL HTTP-first and return clean Markdown, title, "
                    "description, canonical URL, status, links, metadata, content hash, and "
                    "stage timings. A shared headless browser is used only for JavaScript "
                    "shells or access interstitials; CAPTCHA and login walls fail fast."
                ),
                "inputSchema": WebScrapeRequest.model_json_schema(),
                "annotations": _annotations(
                    read_only=True, destructive=False, idempotent=True, open_world=True
                ),
            },
            _scrape,
        ),
        (
            {
                "name": "web_map",
                "description": (
                    "List URLs from robots.txt sitemaps, nested sitemap indexes, and page "
                    "links with domain scope, substring search, include/exclude glob filters, "
                    "and a limit."
                ),
                "inputSchema": WebMapRequest.model_json_schema(),
                "annotations": _annotations(
                    read_only=True, destructive=False, idempotent=True, open_world=True
                ),
            },
            _map,
        ),
        (
            {
                "name": "web_extract",
                "description": (
                    "Extract structured fields from one page with CSS selectors. Each field "
                    "reports how it was found, its evidence, and its source URL; missing "
                    "required fields are reported, never invented."
                ),
                "inputSchema": WebExtractRequest.model_json_schema(by_alias=True),
                "annotations": _annotations(
                    read_only=True, destructive=False, idempotent=True, open_world=True
                ),
            },
            _extract,
        ),
        (
            {
                "name": "web_crawl_start",
                "description": (
                    "Queue a background crawl and return its job id immediately. Pages are "
                    "deduplicated by canonical URL and content hash; robots.txt, domain scope, "
                    "depth/page limits, filters, and per-host limits apply."
                ),
                "inputSchema": WebCrawlRequest.model_json_schema(),
                "annotations": _annotations(
                    read_only=False, destructive=False, idempotent=False, open_world=True
                ),
            },
            _crawl_start,
        ),
        (
            {
                "name": "web_crawl_status",
                "description": "Read crawl progress, counters, and collected errors by job id.",
                "inputSchema": _JobIdArguments.model_json_schema(),
                "annotations": _annotations(
                    read_only=True, destructive=False, idempotent=True, open_world=False
                ),
            },
            _crawl_status,
        ),
        (
            {
                "name": "web_crawl_results",
                "description": "Read one page of completed crawl results with offset and limit.",
                "inputSchema": _CrawlResultsArguments.model_json_schema(),
                "annotations": _annotations(
                    read_only=True, destructive=False, idempotent=True, open_world=False
                ),
            },
            _crawl_results,
        ),
        (
            {
                "name": "web_crawl_cancel",
                "description": "Cancel a queued or in-flight crawl and its pending page tasks.",
                "inputSchema": _JobIdArguments.model_json_schema(),
                "annotations": _annotations(
                    read_only=False, destructive=True, idempotent=True, open_world=False
                ),
            },
            _crawl_cancel,
        ),
    ]


def _tools() -> list[dict[str, Any]]:
    return [spec for spec, _ in _tool_specs()]


def _text_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "isError": False,
    }


def _error_result(code: str, message: str, **extra: Any) -> dict[str, Any]:
    error = {"code": code, "message": message}
    error.update(extra)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"error": error}, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "isError": True,
    }


def _dispatch_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        fields = []
        for item in exc.errors()[:8]:
            location = ".".join(str(part) for part in item.get("loc", ()))
            fields.append({"field": location, "message": item.get("msg", "invalid value")})
        return _error_result("invalid_arguments", "Tool arguments failed validation", fields=fields)
    if isinstance(exc, _ArgumentError):
        return _error_result("invalid_arguments", str(exc))
    if isinstance(exc, WebToolError):
        return _error_result(
            exc.code,
            str(exc),
            url=exc.url,
            status_code=exc.status_code,
            detail=exc.detail,
        )
    logger.warning("mcp_tool_failed error=%s", type(exc).__name__)
    return _error_result("internal_error", "The tool failed unexpectedly")


class McpServer:
    """Minimal MCP server: initialize, ping, tools/list, tools/call, notifications."""

    def __init__(self, service: WebToolsService) -> None:
        self.service = service
        self._handlers: dict[str, Handler] = {
            spec["name"]: handler for spec, handler in _tool_specs()
        }
        self._initialized = False
        self._write_lock = asyncio.Lock()
        self._request_slots = asyncio.Semaphore(_TOOL_CONCURRENCY)
        self._inflight: dict[str, asyncio.Task[None]] = {}

    async def handle_message(self, message: Any) -> Optional[dict[str, Any]]:
        if not isinstance(message, dict):
            return self._error_response(None, -32600, "Invalid Request")

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str):
            if request_id is None:
                return None
            return self._error_response(request_id, -32600, "Invalid Request")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if request_id is None:
                return None
            return self._error_response(request_id, -32602, "Invalid params")

        if request_id is None:
            if method == "notifications/cancelled":
                self._cancel(params.get("requestId"))
            return None

        if method == "initialize":
            return self._response(request_id, self._initialize_result(params))
        if method == "ping":
            return self._response(request_id, {})
        if method == "tools/list":
            return self._response(
                request_id,
                {"tools": _tools()},
            )
        if method == "tools/call":
            return self._response(request_id, await self._call_tool(params))

        return self._error_response(request_id, -32601, f"Method not found: {method}")

    def _initialize_result(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in _PROTOCOL_VERSIONS else _PROTOCOL_VERSIONS[-1]
        self._initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": _SERVER_INFO,
            "instructions": (
                "Local web research tools. Search discovers URLs; scrape, map, and extract "
                "read pages HTTP-first with a shared headless browser fallback; crawl runs "
                "as a background job with explicit status, results, and cancellation."
            ),
        }

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error_result("invalid_arguments", "name and arguments are required")
        handler = self._handlers.get(name)
        if handler is None:
            raise _UnknownTool(name)

        try:
            async with self._request_slots:
                payload = await handler(self.service, arguments)
        except (ValidationError, _ArgumentError, WebToolError) as exc:
            return _dispatch_error(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failures become tool results
            return _dispatch_error(exc)
        return _text_result(payload)

    def _cancel(self, request_id: Any) -> None:
        if request_id is None:
            return
        task = self._inflight.get(_request_key(request_id))
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    async def run(self) -> None:
        """Read newline-delimited JSON-RPC from stdin until EOF."""
        loop = asyncio.get_running_loop()
        pending: set[asyncio.Task[None]] = set()

        while True:
            line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                await self._write(self._error_response(None, -32700, "Parse error"))
                continue

            task = asyncio.create_task(self._handle_and_write(message))
            pending.add(task)
            task.add_done_callback(pending.discard)
            request_id = message.get("id") if isinstance(message, dict) else None
            if request_id is not None:
                key = _request_key(request_id)
                self._inflight[key] = task

                def _forget(_: asyncio.Task[None], key: str = key) -> None:
                    self._inflight.pop(key, None)

                task.add_done_callback(_forget)

        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=5)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)

    async def _handle_and_write(self, message: Any) -> None:
        try:
            response = await self.handle_message(message)
        except asyncio.CancelledError:
            return
        except _UnknownTool as exc:
            request_id = message.get("id") if isinstance(message, dict) else None
            response = self._error_response(request_id, -32601, str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the session
            logger.warning("mcp_request_failed error=%s", type(exc).__name__)
            request_id = message.get("id") if isinstance(message, dict) else None
            response = self._error_response(request_id, -32603, "Internal error")
        if response is not None:
            await self._write(response)

    async def _write(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with self._write_lock:
            sys.stdout.buffer.write(data + b"\n")
            sys.stdout.buffer.flush()


class _UnknownTool(Exception):
    """Raised when tools/call names a tool this server does not expose."""


def _request_key(request_id: Any) -> str:
    return f"{type(request_id).__name__}:{request_id}"


async def _serve() -> None:
    service = WebToolsService.from_env()
    server = McpServer(service)
    try:
        await server.run()
    finally:
        await service.close()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - startup failure goes to the MCP stderr log
        logger.error("mcp_server_failed error=%s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
