"""Dependency-free stdio MCP server exposing the local web tools to Dot."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, Callable, Coroutine, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
_SERVER_NAME = "web-research"
_TOOL_CONCURRENCY = 8
# The search parameters a request may add; each is passed on only when it is set.
_SEARCH_PARAMETERS = {
    "region",
    "language",
    "sites",
    "exclude_sites",
    "date_from",
    "date_to",
    "resolve_dates",
    "primary_first",
}
_SEARCH_READ_PARAMETERS = {"read", "read_chars", "archive", "strict_dates", "debug"}
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_GIT_TIMEOUT_SECONDS = 1


def _git_commit(root: Path, run: Callable[..., Any]) -> Optional[str]:
    """The short commit of the checkout this package runs from, None when it is not one."""
    if not (root / ".git").exists():
        return None
    try:
        done = run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = (done.stdout or "").strip()
    if done.returncode != 0 or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return None
    return commit


def build_server_info(
    root: Optional[Path] = None, *, run: Callable[..., Any] = subprocess.run
) -> dict[str, str]:
    """``serverInfo``: the version, and ``+g<short sha>`` when the package runs from a git checkout.

    Read once, at start, with git given one second: the owner can tell which code is live
    from the version a client reports and from the line the server writes to its log.
    """
    commit = _git_commit(root or _PROJECT_ROOT, run)
    return {"name": _SERVER_NAME, "version": f"{__version__}+g{commit}" if commit else __version__}


def startup_line(info: Mapping[str, str], python: Optional[str] = None) -> str:
    """The one line written to stderr at start: name, version, commit and Python."""
    version = info["version"]
    commit = version.partition("+g")[2] or "unknown"
    return (
        f"{info['name']} mcp server started: version={version} commit={commit} "
        f"python={python or platform.python_version()}"
    )


def announce(info: Mapping[str, str], stream: Optional[IO[str]] = None) -> None:
    print(startup_line(info), file=stream or sys.stderr, flush=True)


class _ArgumentError(ValueError):
    """A tool argument that does not match the published schema."""


class _JobIdArguments(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=128)


class _CrawlResultsArguments(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=1000)


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _health(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    _NoArguments.model_validate(arguments)
    return service.health()


def _compact(on: bool) -> dict[str, Any]:
    """``compact`` for the service, passed on only when it is on (the service answers in full)."""
    return {"compact": True} if on else {}


async def _search(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = WebSearchRequest.model_validate(arguments)
    return await service.search(
        request.query,
        limit=request.limit,
        provider=request.provider,
        depth=request.depth,
        # A model pays for every character it reads: the answer is compact unless it says not.
        **_compact(request.compact is not False),
        **request.model_dump(include=_SEARCH_PARAMETERS, exclude_none=True),
        **request.model_dump(include=_SEARCH_READ_PARAMETERS, exclude_defaults=True),
    )


async def _scrape(service: WebToolsService, arguments: dict[str, Any]) -> dict[str, Any]:
    request = WebScrapeRequest.model_validate(arguments)
    if request.content_id:
        return await service.scrape_content(
            request.content_id,
            offset=request.offset,
            max_chars=request.max_chars,
            lean=request.lean is not False,
        )
    options = request.service_options()
    if request.urls:
        return await service.scrape_many(
            request.urls,
            max_chars=request.max_chars,
            archive=request.archive,
            max_total_chars=request.max_total_chars,
            **options,
        )
    return await service.scrape(
        str(request.url), max_chars=request.max_chars, archive=request.archive, **options
    )


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
    return await service.extract(request.url, request.field_specs(), max_chars=request.max_chars)


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
                    "Discover result URLs. depth: fast (one query), discover (default: base, "
                    "current-year and historical angles), deep (adds an official or "
                    "primary-source angle, which gets the longer time budget). Ranked by BM25, "
                    "cross-angle consensus, source authority and freshness, cached per query; "
                    "official sites and mainstream news lead news queries, aggregators rank "
                    "low. The answer is compact: per result title, url, domain, date, "
                    "date_confidence (0 to 1; a result whose dates conflict is named in "
                    "run.warnings), source_type (official, academic, news, press_release, "
                    "social, reference, code, community, aggregator, other), snippet, score, and "
                    "run {provider, warnings, fallback}; debug adds facets, queries, timings and "
                    "score parts, compact: false returns everything. A refused or timed-out "
                    "provider falls to the next: Brave or Serper first when their key is set, "
                    "then DuckDuckGo, its lite page, Yahoo, Google (web_health says who is "
                    "cooling). No relevant result: results is empty and warnings holds "
                    "no_relevant_results. region, language, sites, exclude_sites, date_from and "
                    "date_to narrow it. resolve_dates reads the heads of the top pages for their "
                    "dates (default on for news and date ranges); strict_dates drops results "
                    "with no date or a low date_confidence, and warnings says how many. read N "
                    "(0 to 5) also reads the top N pages: content or content_error; it costs "
                    "seconds. Search discovers URLs only; web_scrape reads one."
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
                    "canonical URL, status, content links (50; max_links 0 to 200, links: "
                    "false for none), metadata, date with "
                    "date_source, date_confidence and date_conflicts (every date signal of the "
                    "page weighed), content hash and timings; the Markdown follows the JSON as a "
                    "block of its own. max_chars cuts it: truncated, total_chars, next_offset "
                    "and content_id then let content_id with offset read the rest from the "
                    "cache (5 minutes, no request); lean returns only the text, date and cut "
                    "info. urls (2 to 10) reads pages in one call, max_total_chars (60,000) "
                    "bounding the answer. A headless browser is used only for JavaScript shells "
                    "and access interstitials. A walled page (captcha, 401/403/429, bot wall) is "
                    "read another way: the Internet Archive's copy, the site's AMP or print "
                    "form, its feed, or another outlet's report found by title. recovered_from "
                    "says which, the text starts with a line saying whose it is, alternates and "
                    "next_action follow; if nothing reads, the error carries them. archive: "
                    "true alone asks only for the archive; recover: false for none. A PDF is "
                    "Markdown with its page count in pages."
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
                    "and a limit. total counts every matching URL, returned those in urls, "
                    "and truncated says the limit cut some. A site no request reached lists "
                    "nothing and says why in errors."
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
                    "Extract structured fields from one page with CSS selectors, or by name: "
                    "a field with no selector (headline, author, published_at, description, "
                    "site_name, keywords, lead, ...) is read from the page's JSON-LD, meta "
                    "tags and HTML conventions, and dates come back in ISO 8601. Each field "
                    "reports how it was found, its evidence, and its source URL; missing "
                    "required fields are reported, never invented. A field with all: true "
                    "returns up to max_items matches (100 by default); offset skips the first N "
                    "to page on (provenance gives total, returned, offset, truncated, and "
                    "next_offset when values were left out). The whole answer stays under "
                    "max_chars (20,000 by default), its truncated flag set when anything was cut."
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
                "name": "web_health",
                "description": (
                    "Each search provider's state in this process: ok, degraded, cooling "
                    "(cooldown_remaining_s) or no_key (key_env names the variable), its calls, "
                    "block_rate (captchas and refusals), mean_latency_ms and last_error; order "
                    "is the order the next search asks them in. Cheap: no request is made."
                ),
                "inputSchema": _NoArguments.model_json_schema(),
                "annotations": _annotations(
                    read_only=True, destructive=False, idempotent=True, open_world=False
                ),
            },
            _health,
        ),
        (
            {
                "name": "web_crawl_start",
                "description": (
                    "Queue a background crawl and return its job id immediately. Pages are "
                    "deduplicated by canonical URL and content hash; robots.txt, domain scope, "
                    "depth/page limits, filters, and per-host limits apply. use_sitemap seeds "
                    "it from the site's sitemaps, pages no link reaches."
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


def _without_null(node: dict[str, Any]) -> dict[str, Any]:
    """``{"anyOf": [X, {"type": "null"}], "default": null}`` as X: an optional argument is one a
    caller leaves out, and a model told that null is allowed would send it."""
    options = node.get("anyOf")
    if isinstance(options, list) and len(options) == 2:
        kept = [option for option in options if option != {"type": "null"}]
        if len(kept) == 1:
            rest = {key: value for key, value in node.items() if key != "anyOf"}
            if rest.get("default") is None:
                rest.pop("default", None)
            return {**kept[0], **rest}
    return node


def _lean(node: Any, names: bool = False) -> Any:
    """A JSON schema without its ``title`` labels, which only repeat a property's name, and
    without the null that an optional argument also allows.

    Dot sends every tool's schema with each model request, so what says nothing is left out.
    ``names`` marks a mapping of property names, whose keys are kept whatever they are.
    """
    if isinstance(node, dict):
        node = _without_null(node)
        return {
            key: _lean(value, key in {"properties", "$defs"})
            for key, value in node.items()
            if names or key != "title"
        }
    if isinstance(node, list):
        return [_lean(item) for item in node]
    return node


def _tools() -> list[dict[str, Any]]:
    return [{**spec, "inputSchema": _lean(spec["inputSchema"])} for spec, _ in _tool_specs()]


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


def _scrape_result(payload: dict[str, Any]) -> dict[str, Any]:
    """A page's answer with its Markdown in a text block of its own, after the JSON of the rest.

    A client that keeps a long answer in a file (Dot does, past a size) wrote the whole answer as
    one line, the page's newlines escaped inside the JSON string; with the page in a block of its
    own the file is the JSON on its first line and then the page as Markdown with its own line
    breaks. The JSON says ``(below)`` where the Markdown was, and a batch marks each page's block
    with a line that names it.
    """
    pages = payload.get("pages")
    if isinstance(pages, list):
        total = sum(1 for page in pages if _has_markdown(page))
        heads: list[Any] = []
        texts: list[str] = []
        for page in pages:
            if not _has_markdown(page):
                heads.append(page)
                continue
            number = len(texts) + 1
            heads.append({**page, "markdown": f"(page {number} of {total}, below)"})
            texts.append(
                f"<!-- page {number} of {total}: {page.get('url')} -->\n{page['markdown']}"
            )
        return _text_blocks({**payload, "pages": heads}, texts)
    if _has_markdown(payload):
        return _text_blocks({**payload, "markdown": "(below)"}, [payload["markdown"]])
    return _text_result(payload)


def _has_markdown(page: Any) -> bool:
    return (
        isinstance(page, dict) and isinstance(page.get("markdown"), str) and bool(page["markdown"])
    )


def _text_blocks(head: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    first = json.dumps(head, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text} for text in [first, *texts]],
        "isError": False,
    }


# The tools whose answer is more than one block of text: how each shapes its answer.
_RESULT_SHAPES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "web_scrape": _scrape_result,
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
        said = exc.to_dict()
        extra = {
            key: value
            for key, value in said.items()
            if key not in {"code", "message", "url", "status_code", "detail"}
        }
        return _error_result(
            exc.code,
            str(exc),
            url=exc.url,
            status_code=exc.status_code,
            detail=exc.detail,
            **extra,
        )
    logger.warning("mcp_tool_failed error=%s", type(exc).__name__)
    return _error_result("internal_error", "The tool failed unexpectedly")


class McpServer:
    """Minimal MCP server: initialize, ping, tools/list, tools/call, notifications."""

    def __init__(
        self, service: WebToolsService, server_info: Optional[Mapping[str, str]] = None
    ) -> None:
        self.service = service
        self._server_info = dict(server_info or {"name": _SERVER_NAME, "version": __version__})
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
            "serverInfo": dict(self._server_info),
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
        return _RESULT_SHAPES.get(name, _text_result)(payload)

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
    info = await asyncio.to_thread(build_server_info)
    announce(info)
    service = WebToolsService.from_env()
    server = McpServer(service, server_info=info)
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
