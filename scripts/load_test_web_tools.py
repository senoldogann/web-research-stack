#!/usr/bin/env python3
"""Controlled, loopback-only load test for the web tools.

All traffic goes to a local fixture server; no external host receives load.
Fixture hostnames are public-looking `.test` names routed to loopback by a
transport in this process; the fetcher's DNS validator is replaced for those
names, while service canonicalisation and tool limits stay active.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import resource
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_scraper.web_tools.fetcher import HostGate, SafeFetcher  # noqa: E402
from web_scraper.web_tools.search import SearchResult  # noqa: E402
from web_scraper.web_tools.service import WebToolsService  # noqa: E402
from web_scraper.web_tools.settings import WebToolsSettings  # noqa: E402

MAX_CRAWL_PAGES = 12
SERVER_DELAY_SECONDS = 0.4


def _html(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "crawl" and len(parts) >= 3:
        job = parts[1]
        page = int(parts[2])
        links = "".join(
            f'<a href="/crawl/{job}/{index}">Page {index}</a> '
            for index in range(page + 1, min(page + 4, MAX_CRAWL_PAGES))
        )
        label = f"crawl {job} page {page}"
    elif parts and parts[0] == "load":
        label = f"load page {parts[-1]}"
        links = ""
    else:
        label = "fixture page"
        links = ""
    paragraphs = " ".join(
        f"Paragraph {index} for {label}: bounded concurrency, canonical caching, "
        f"content extraction, and crawl scheduling are being exercised."
        for index in range(20)
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{label}</title>"
        f"<meta name='description' content='Fixture page for {label}'>"
        f"<link rel='canonical' href='{path}'>"
        "</head><body><article>"
        f"<h1>{label}</h1><p>{paragraphs}</p>{links}"
        "</article></body></html>"
    )


class _Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.total = 0

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.total += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        with self._lock:
            self.active -= 1

    def reset_peak(self) -> None:
        with self._lock:
            self.max_active = self.active

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"active": self.active, "max_active": self.max_active, "total": self.total}


SERVER_METRICS = _Metrics()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        SERVER_METRICS.enter()
        try:
            path = self.path.split("?", 1)[0]
            if path == "/robots.txt":
                body = b"User-agent: *\nAllow: /\n"
                content_type = "text/plain; charset=utf-8"
            else:
                body = _html(path).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            time.sleep(SERVER_DELAY_SECONDS)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            SERVER_METRICS.leave()

    def log_message(self, format: str, *args: Any) -> None:
        return


class TrackingHostGate(HostGate):
    def __init__(self, max_per_host: int, min_interval_ms: int) -> None:
        super().__init__(max_per_host=max_per_host, min_interval_ms=min_interval_ms)
        self.active: defaultdict[str, int] = defaultdict(int)
        self.max_active: defaultdict[str, int] = defaultdict(int)
        self.starts: defaultdict[str, list[float]] = defaultdict(list)

    @asynccontextmanager
    async def acquire(self, host: str):
        async with super().acquire(host):
            self.active[host] += 1
            self.max_active[host] = max(self.max_active[host], self.active[host])
            try:
                yield
            finally:
                self.active[host] -= 1

    async def wait_for_turn(self, host: str) -> None:
        await super().wait_for_turn(host)
        self.starts[host].append(time.monotonic())

    def reset(self) -> None:
        self.active.clear()
        self.max_active.clear()
        self.starts.clear()

    def summary(self) -> dict[str, Any]:
        gaps: list[float] = []
        for starts in self.starts.values():
            gaps.extend(
                (later - earlier) * 1000
                for earlier, later in zip(starts, starts[1:])
            )
        return {
            "max_active_by_host": dict(self.max_active),
            "min_start_gap_ms": round(min(gaps), 2) if gaps else None,
            "hosts_seen": len(self.max_active),
        }


class StubSearchAdapter:
    name = "stub"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: str | None = None,
    ) -> list[SearchResult]:
        await asyncio.sleep(0.25)
        return [
            SearchResult(
                title=f"Result {index} for {query}",
                url=f"https://example.com/{index}",
                snippet="Synthetic result",
                source="stub",
            )
            for index in range(limit)
        ]


class LoopbackTransport(httpx.AsyncBaseTransport):
    """Route public-looking fixture hostnames to the local test server.

    The response keeps the caller's request so links resolve against the
    fixture hostname rather than the loopback address.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        upstream = httpx.Request(
            method=request.method,
            url=request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port),
            headers=request.headers,
            content=request.content,
            extensions=request.extensions,
        )
        response = await self._inner.handle_async_request(upstream)
        body = await response.aread()
        await response.aclose()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=body,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percent / 100 * len(ordered)) - 1))
    return round(ordered[index], 2)


def current_rss_mb() -> float:
    output = subprocess.check_output(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        text=True,
    ).strip()
    return round(int(output) / 1024, 1)


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if value > 10_000_000:  # macOS reports bytes
        return round(value / 1024 / 1024, 1)
    return round(value / 1024, 1)


async def run_scrapes(
    service: WebToolsService,
    urls: list[str],
    concurrency: int,
) -> dict[str, Any]:
    slots = asyncio.Semaphore(concurrency)

    async def one(url: str) -> dict[str, Any]:
        async with slots:
            started = time.perf_counter()
            try:
                page = await service.scrape(url, max_chars=1500)
            except Exception as exc:  # noqa: BLE001 - load harness records every failure
                return {
                    "ok": False,
                    "code": getattr(exc, "code", type(exc).__name__),
                    "wall_ms": (time.perf_counter() - started) * 1000,
                }
            timings = page["timings"]
            return {
                "ok": True,
                "wall_ms": (time.perf_counter() - started) * 1000,
                "http_ms": timings.get("http_ms", 0.0),
                "extract_ms": timings.get("extract_ms", 0.0),
                "chars": len(page["markdown"]),
            }

    results = await asyncio.gather(*(one(url) for url in urls))
    ok = [item for item in results if item["ok"]]
    failed = [item for item in results if not item["ok"]]
    return {
        "requests": len(results),
        "ok": len(ok),
        "failed": len(failed),
        "errors": dict(Counter(item["code"] for item in failed)),
        "wall_p50_ms": percentile([item["wall_ms"] for item in ok], 50),
        "wall_p95_ms": percentile([item["wall_ms"] for item in ok], 95),
        "http_p50_ms": percentile([item["http_ms"] for item in ok], 50),
        "http_p95_ms": percentile([item["http_ms"] for item in ok], 95),
        "extract_p50_ms": percentile([item["extract_ms"] for item in ok], 50),
        "extract_p95_ms": percentile([item["extract_ms"] for item in ok], 95),
        "markdown_chars_min": min((item["chars"] for item in ok), default=0),
    }


async def run_crawl_load(
    service: WebToolsService,
    port: int,
    *,
    jobs: int,
    pages: int,
) -> dict[str, Any]:
    job_ids: list[str] = []
    started = time.perf_counter()
    for index in range(jobs):
        snapshot = await service.start_crawl(
            f"http://crawl{index}.test/crawl/load{index}/0",
            {
                "max_pages": pages,
                "max_depth": 4,
                "respect_robots": False,
            },
        )
        job_ids.append(snapshot["job_id"])

    deadline = time.monotonic() + 90
    while True:
        snapshots = [await service.get_crawl(job_id) for job_id in job_ids]
        if all(item["status"] in {"completed", "cancelled", "failed"} for item in snapshots):
            break
        if time.monotonic() >= deadline:
            for job_id in job_ids:
                await service.cancel_crawl(job_id)
            break
        await asyncio.sleep(0.2)

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "jobs": jobs,
        "pages_per_job": pages,
        "wall_ms": round(elapsed_ms, 2),
        "statuses": dict(Counter(item["status"] for item in snapshots)),
        "fetched_per_job": [item["progress"]["fetched"] for item in snapshots],
        "failed_per_job": [item["progress"]["failed"] for item in snapshots],
        "skipped_per_job": [item["progress"]["skipped"] for item in snapshots],
        "duplicates_per_job": [item["progress"]["duplicates"] for item in snapshots],
    }


async def run_cancel_test(service: WebToolsService, port: int, jobs: int = 4) -> dict[str, Any]:
    job_ids: list[str] = []
    for index in range(jobs):
        snapshot = await service.start_crawl(
            f"http://cancel{index}.test/crawl/cancel{index}/0",
            {
                "max_pages": MAX_CRAWL_PAGES,
                "max_depth": 4,
                "respect_robots": False,
            },
        )
        job_ids.append(snapshot["job_id"])

    await asyncio.sleep(1.0)
    started = time.perf_counter()
    for job_id in job_ids[:2]:
        await service.cancel_crawl(job_id)

    deadline = time.monotonic() + 15
    cancelled: list[dict[str, Any]] = []
    while True:
        cancelled = [await service.get_crawl(job_id) for job_id in job_ids[:2]]
        if all(item["status"] in {"cancelled", "failed", "completed"} for item in cancelled):
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    cancel_ms = (time.perf_counter() - started) * 1000

    for job_id in job_ids:
        await service.cancel_crawl(job_id)

    drain_started = time.monotonic()
    deadline = time.monotonic() + 2
    while SERVER_METRICS.snapshot()["active"] > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    drain_ms = (time.monotonic() - drain_started) * 1000
    final = [await service.get_crawl(job_id) for job_id in job_ids]
    return {
        "jobs": jobs,
        "cancelled_jobs": 2,
        "cancel_to_terminal_ms": round(cancel_ms, 2),
        "statuses": [item["status"] for item in cancelled],
        "all_statuses": [item["status"] for item in final],
        "server_drain_ms": round(drain_ms, 2),
        "server_active_after_cancel": SERVER_METRICS.snapshot()["active"],
    }


async def _run() -> dict[str, Any]:
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    settings = WebToolsSettings(
        global_concurrency=8,
        host_concurrency=2,
        host_min_interval_ms=200,
        cache_ttl_seconds=1,
        cache_max_entries=32,
        browser_enabled=False,
        default_crawl_pages=MAX_CRAWL_PAGES,
        max_crawl_pages=100,
        default_crawl_depth=4,
        max_crawl_depth=4,
        crawl_concurrency=4,
        crawl_job_ttl_seconds=60,
        max_crawl_jobs=32,
    )
    gate = TrackingHostGate(max_per_host=2, min_interval_ms=200)
    fetcher = SafeFetcher(
        timeout_seconds=10,
        max_response_bytes=2_000_000,
        max_redirects=0,
        retry_attempts=0,
        max_concurrency=8,
        host_gate=gate,
        transport=LoopbackTransport(port),
        validator=lambda url, **_: url,
    )
    service = WebToolsService(
        settings=settings,
        fetcher=fetcher,
        browser=None,
        search_adapters={"stub": StubSearchAdapter()},
    )

    rss_start = current_rss_mb()
    peak_start = peak_rss_mb()
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture": {
            "hosts": "fixture .test hostnames routed to 127.0.0.1",
            "server_delay_ms": SERVER_DELAY_SECONDS * 1000,
            "validator_override": "load-harness only",
            "browser": "disabled",
        },
        "settings": {
            "global_concurrency": settings.global_concurrency,
            "host_concurrency": settings.host_concurrency,
            "host_min_interval_ms": settings.host_min_interval_ms,
            "crawl_concurrency": settings.crawl_concurrency,
        },
        "rss_mb_start": rss_start,
        "peak_rss_mb_start": peak_start,
    }

    try:
        warmup = await service.scrape("http://load.test/load/warmup", max_chars=1500)
        summary["warmup"] = {
            "status": warmup["status_code"],
            "fetched_with": warmup["fetched_with"],
            "markdown_chars": len(warmup["markdown"]),
        }

        SERVER_METRICS.reset_peak()
        gate.reset()
        single_urls = [
            f"http://load.test/load/single{i}" for i in range(60)
        ]
        summary["scrape_single_host"] = await run_scrapes(
            service, single_urls, concurrency=64
        )
        summary["scrape_single_host"]["server"] = SERVER_METRICS.snapshot()
        summary["scrape_single_host"]["gate"] = gate.summary()

        SERVER_METRICS.reset_peak()
        gate.reset()
        multi_urls = [
            f"http://load{host}.test/load/multi{host}_{index}"
            for host in range(1, 9)
            for index in range(20)
        ]
        summary["scrape_multi_host"] = await run_scrapes(
            service, multi_urls, concurrency=160
        )
        summary["scrape_multi_host"]["server"] = SERVER_METRICS.snapshot()
        summary["scrape_multi_host"]["gate"] = gate.summary()

        SERVER_METRICS.reset_peak()
        gate.reset()
        summary["crawl"] = await run_crawl_load(
            service, port, jobs=6, pages=MAX_CRAWL_PAGES
        )
        summary["crawl"]["server"] = SERVER_METRICS.snapshot()
        summary["crawl"]["gate"] = gate.summary()

        SERVER_METRICS.reset_peak()
        gate.reset()
        summary["cancellation"] = await run_cancel_test(service, port, jobs=4)
        summary["cancellation"]["gate"] = gate.summary()

        search_started = time.perf_counter()
        searches = await asyncio.gather(
            *(
                service.search("load test", provider="stub", limit=5)
                for _ in range(32)
            )
        )
        summary["search_stub"] = {
            "requests": len(searches),
            "concurrency": 32,
            "wall_ms": round((time.perf_counter() - search_started) * 1000, 2),
            "all_ok": all(len(item["results"]) == 5 for item in searches),
            "adapter_delay_ms": 250,
        }

        summary["rss_mb_end"] = current_rss_mb()
        summary["peak_rss_mb_end"] = peak_rss_mb()
        summary["rss_mb_growth"] = round(summary["rss_mb_end"] - rss_start, 1)
        summary["fixture_server_total_requests"] = SERVER_METRICS.snapshot()["total"]
        summary["checks"] = {
            "single_host_max_2": summary["scrape_single_host"]["server"]["max_active"] <= 2,
            "multi_host_max_8": summary["scrape_multi_host"]["server"]["max_active"] <= 8,
            "crawl_max_8": summary["crawl"]["server"]["max_active"] <= 8,
            "single_host_no_errors": summary["scrape_single_host"]["failed"] == 0,
            "multi_host_no_errors": summary["scrape_multi_host"]["failed"] == 0,
            "crawl_no_failures": all(
                value == 0 for value in summary["crawl"]["failed_per_job"]
            ),
            "crawl_fetched_all_pages": all(
                value == summary["crawl"]["pages_per_job"]
                for value in summary["crawl"]["fetched_per_job"]
            ),
            "cancelled_terminal": all(
                status == "cancelled" for status in summary["cancellation"]["statuses"]
            ),
            "cancelled_server_drained": summary["cancellation"]["server_active_after_cancel"] == 0,
            "search_stub_all_ok": summary["search_stub"]["all_ok"],
        }
        return summary
    finally:
        await service.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(ROOT / "benchmarks" / "web_tools_load_test.json"),
        help="JSON report path",
    )
    args = parser.parse_args()

    report = asyncio.run(_run())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    single = report["scrape_single_host"]
    multi = report["scrape_multi_host"]
    crawl = report["crawl"]
    cancellation = report["cancellation"]
    search = report["search_stub"]
    print(
        f"single-host  {single['ok']}/{single['requests']} ok  "
        f"p50={single['wall_p50_ms']}ms p95={single['wall_p95_ms']}ms  "
        f"peak={single['server']['max_active']}"
    )
    print(
        f"multi-host   {multi['ok']}/{multi['requests']} ok  "
        f"p50={multi['wall_p50_ms']}ms p95={multi['wall_p95_ms']}ms  "
        f"peak={multi['server']['max_active']}"
    )
    fetched = sum(crawl["fetched_per_job"])
    print(
        f"crawl        {crawl['statuses'].get('completed', 0)}/{crawl['jobs']} jobs  "
        f"{fetched}/{crawl['jobs'] * crawl['pages_per_job']} pages  "
        f"wall={crawl['wall_ms']}ms"
    )
    print(
        f"cancellation {cancellation['cancel_to_terminal_ms']}ms to terminal  "
        f"drained={cancellation['server_drain_ms']}ms"
    )
    print(
        f"search       {search['requests']} concurrent in {search['wall_ms']}ms "
        f"(adapter {search['adapter_delay_ms']}ms)"
    )
    passed = sum(1 for value in report["checks"].values() if value)
    print(f"checks       {passed}/{len(report['checks'])} passed")
    print(f"saved        {output}")
    return 0 if passed == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
