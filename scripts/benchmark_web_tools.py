#!/usr/bin/env python3
"""Cold-vs-cached benchmark for the local web research tools.

Search, HTTP fetch, HTML extraction, and headless browser rendering are timed
separately. The HTTP suite records whether a browser was started; a passing
run means ordinary pages never paid for one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_scraper.api_runtime import InMemoryTTLCache  # noqa: E402
from web_scraper.web_tools.service import WebToolsService  # noqa: E402
from web_scraper.web_tools.settings import WebToolsSettings  # noqa: E402

DEFAULT_URLS = [
    "https://docs.python.org/3/",
    "https://docs.python.org/3/library/json.html",
    "https://developer.mozilla.org/en-US/docs/Web/HTTP",
    "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
    "https://news.ycombinator.com/",
    "https://www.bbc.com/news",
    "https://fastapi.tiangolo.com/",
    "https://doc.rust-lang.org/book/",
    "https://www.sqlite.org/docs.html",
    "https://quotes.toscrape.com/",
    "https://books.toscrape.com/",
    "https://lobste.rs/",
    "https://lwn.net/",
    "https://docs.pydantic.dev/latest/",
    "https://peps.python.org/pep-0008/",
    "https://www.djangoproject.com/",
    "https://flask.palletsprojects.com/",
    "https://www.typescriptlang.org/docs/",
    "https://react.dev/",
    "https://vuejs.org/guide/introduction.html",
]
SEARCH_QUERIES = [
    "python asyncio tutorial",
    "fastapi dependency injection",
    "playwright headless chromium",
    "sqlite full text search",
]
BROWSER_URL = "https://quotes.toscrape.com/js/"
MIN_HTTP_URLS = 20


def _milliseconds(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _percentile(ordered: list[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


def _summary(samples: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    values = sorted(
        float(sample[key]) for sample in samples if isinstance(sample.get(key), (int, float))
    )
    if not values:
        return None
    return {
        "count": len(values),
        "min_ms": round(values[0], 2),
        "p50_ms": round(_percentile(values, 0.50), 2),
        "p95_ms": round(_percentile(values, 0.95), 2),
        "avg_ms": round(sum(values) / len(values), 2),
        "max_ms": round(values[-1], 2),
    }


def _error(exc: BaseException) -> dict[str, Any]:
    return {
        "code": getattr(exc, "code", type(exc).__name__),
        "message": str(exc),
        "status_code": getattr(exc, "status_code", None),
    }


async def _measure_scrape(service: WebToolsService, url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        payload = await service.scrape(url)
    except Exception as exc:  # noqa: BLE001 - a benchmark records failures
        return {
            "url": url,
            "wall_ms": _milliseconds((time.perf_counter() - started) * 1000),
            "error": _error(exc),
        }

    timings = payload.get("timings", {})
    total_ms = timings.get("total_ms")
    network_ms = timings.get("network_ms")
    http_ms = timings.get("http_ms")
    app_ms = None
    if isinstance(total_ms, (int, float)) and isinstance(http_ms, (int, float)):
        app_ms = round(float(total_ms) - float(http_ms), 2)

    return {
        "url": url,
        "wall_ms": _milliseconds((time.perf_counter() - started) * 1000),
        "requested_url": payload.get("requested_url"),
        "final_url": payload.get("url"),
        "status_code": payload.get("status_code"),
        "fetched_with": payload.get("fetched_with"),
        "cache_hit": payload.get("cache_hit"),
        "markdown_chars": len(payload.get("markdown") or ""),
        "content_hash": payload.get("content_hash"),
        "warnings": payload.get("warnings", []),
        "network_ms": _milliseconds(network_ms),
        "http_ms": _milliseconds(timings.get("http_ms")),
        "extract_ms": _milliseconds(timings.get("extract_ms")),
        "browser_ms": _milliseconds(timings.get("browser_ms", 0.0)),
        "total_ms": _milliseconds(total_ms),
        "app_ms": app_ms,
        "cache_lookup_ms": _milliseconds(timings.get("cache_lookup_ms", 0.0)),
    }


async def _measure_search(
    service: WebToolsService,
    queries: list[str],
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for query in queries:
        started = time.perf_counter()
        try:
            payload = await service.search(query, limit=5)
        except Exception as exc:  # noqa: BLE001 - a benchmark records failures
            samples.append(
                {
                    "query": query,
                    "wall_ms": _milliseconds((time.perf_counter() - started) * 1000),
                    "error": _error(exc),
                }
            )
            continue
        samples.append(
            {
                "query": query,
                "wall_ms": _milliseconds((time.perf_counter() - started) * 1000),
                "provider": payload.get("provider"),
                "result_count": len(payload.get("results") or []),
                "search_ms": _milliseconds(payload.get("timings", {}).get("search_ms")),
                "total_ms": _milliseconds(payload.get("timings", {}).get("total_ms")),
                "urls": [item.get("url") for item in payload.get("results") or []],
            }
        )
    return samples


def _reset_cache(service: WebToolsService) -> None:
    service.cache = InMemoryTTLCache(
        ttl_seconds=service.settings.cache_ttl_seconds,
        max_entries=service.settings.cache_max_entries,
    )


def _successful(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [sample for sample in samples if "error" not in sample]


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    urls = DEFAULT_URLS if not args.urls else [u.strip() for u in args.urls.split(",") if u.strip()]
    settings = WebToolsSettings.from_env()
    service = WebToolsService.from_env()

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "urls": len(urls),
            "min_http_urls": MIN_HTTP_URLS,
            "settings": {
                "search_provider": settings.search_provider,
                "fetch_timeout_seconds": settings.fetch_timeout_seconds,
                "fetch_retries": settings.fetch_retries,
                "max_response_bytes": settings.max_response_bytes,
                "host_concurrency": settings.host_concurrency,
                "host_min_interval_ms": settings.host_min_interval_ms,
                "cache_ttl_seconds": settings.cache_ttl_seconds,
                "browser_preference": settings.browser_preference,
                "browser_render_min_chars": settings.browser_render_min_chars,
            },
        },
        "definitions": {
            "cold": "empty scrape cache; shared HTTP client and connection pool remain warm",
            "cached": "second scrape of the same canonical URL from the in-process TTL cache",
            "network_ms": "final HTTP hop exchange and body read (target + transport)",
            "http_ms": "whole fetch call including redirect hops, retries, and per-host gate",
            "extract_ms": "HTML-to-Markdown extraction",
            "app_ms": "scrape total_ms minus http_ms (extraction and local service overhead)",
            "wall_ms": "perf_counter around the public service call",
        },
    }

    try:
        if not args.skip_search:
            search_samples = await _measure_search(service, SEARCH_QUERIES)
            report["search"] = {
                "samples": search_samples,
                "failures": sum(1 for s in search_samples if "error" in s),
                "wall_ms": _summary(search_samples, "wall_ms"),
                "search_ms": _summary(search_samples, "search_ms"),
            }
        else:
            report["search"] = {"skipped": True}

        running_before = bool(service.browser and service.browser.running)
        engine_before = service.browser.engine if service.browser is not None else None
        cold_samples = [await _measure_scrape(service, url) for url in urls]
        running_after = bool(service.browser and service.browser.running)
        engine_after = service.browser.engine if service.browser is not None else None

        cold_ok = _successful(cold_samples)
        cached_samples = [await _measure_scrape(service, sample["url"]) for sample in cold_ok]
        cached_ok = _successful(cached_samples)
        cached_hits = sum(1 for sample in cached_ok if sample.get("cache_hit") is True)
        browser_started = (
            running_before or engine_before is not None or running_after or engine_after is not None
        )

        cold_summary = {
            "wall_ms": _summary(cold_ok, "wall_ms"),
            "network_ms": _summary(cold_ok, "network_ms"),
            "http_ms": _summary(cold_ok, "http_ms"),
            "extract_ms": _summary(cold_ok, "extract_ms"),
            "total_ms": _summary(cold_ok, "total_ms"),
            "app_ms": _summary(cold_ok, "app_ms"),
        }
        cached_summary = {
            "wall_ms": _summary(cached_ok, "wall_ms"),
            "cache_lookup_ms": _summary(cached_ok, "cache_lookup_ms"),
        }
        cold_wall = (cold_summary["wall_ms"] or {}).get("p50_ms")
        cached_wall = (cached_summary["wall_ms"] or {}).get("p50_ms")
        speedup = round(cold_wall / cached_wall, 1) if cold_wall and cached_wall else None

        report["http"] = {
            "urls": urls,
            "cold": {
                "samples": cold_samples,
                "successes": len(cold_ok),
                "failures": len(cold_samples) - len(cold_ok),
                "summary": cold_summary,
            },
            "cached": {
                "checked": len(cached_ok),
                "cache_hits": cached_hits,
                "failures": len(cached_samples) - len(cached_ok),
                "summary": cached_summary,
            },
            "cache_speedup_p50": speedup,
            "browser_verification": {
                "running_before": running_before,
                "engine_before": engine_before,
                "running_after": running_after,
                "engine_after": engine_after,
                "started_during_http_suite": browser_started,
                "passed": not browser_started,
            },
        }

        if args.skip_browser:
            report["browser"] = {"skipped": True}
        elif service.browser is None:
            report["browser"] = {"skipped": "browser_disabled"}
        else:
            _reset_cache(service)
            cold_browser = await _measure_scrape(service, BROWSER_URL)
            engine_after_cold = service.browser.engine
            running_after_cold = service.browser.running

            warm_samples: list[dict[str, Any]] = []
            for _ in range(3):
                _reset_cache(service)
                warm_samples.append(await _measure_scrape(service, BROWSER_URL))
            engine_after_warm = service.browser.engine
            running_after_warm = service.browser.running

            browser_ok = (
                "error" not in cold_browser
                and all("error" not in sample for sample in warm_samples)
                and cold_browser.get("fetched_with") == "browser"
                and all(sample.get("fetched_with") == "browser" for sample in warm_samples)
            )
            report["browser"] = {
                "url": BROWSER_URL,
                "cold_start": cold_browser,
                "warm_samples": warm_samples,
                "engine_after_cold": engine_after_cold,
                "engine_after_warm": engine_after_warm,
                "worker_reused": bool(
                    running_after_cold
                    and running_after_warm
                    and engine_after_cold == engine_after_warm
                ),
                "cold_browser_ms": cold_browser.get("browser_ms"),
                "warm_browser_ms": _summary(warm_samples, "browser_ms"),
                "warm_wall_ms": _summary(warm_samples, "wall_ms"),
                "warm_extract_ms": _summary(warm_samples, "extract_ms"),
                "passed": browser_ok,
            }

        report["service_metrics"] = await service.metrics_snapshot()
    finally:
        await service.close()

    http_report = report.get("http", {})
    cold_successes = http_report.get("cold", {}).get("successes", 0)
    browser_ok = report.get("browser", {}).get("passed", True) or args.skip_browser
    report["checks"] = {
        "http_urls_measured": cold_successes,
        "http_minimum_met": cold_successes >= MIN_HTTP_URLS,
        "browser_absent_during_http": http_report.get("browser_verification", {}).get(
            "passed", False
        ),
        "browser_suite_passed": browser_ok,
        "all_cached_hits": http_report.get("cached", {}).get("cache_hits", 0)
        == http_report.get("cached", {}).get("checked", -1),
    }
    report["checks"]["passed"] = all(report["checks"].values())
    return report


def _print_summary(report: dict[str, Any], output: Path) -> None:
    search = report.get("search", {})
    if not search.get("skipped"):
        wall = search.get("wall_ms") or {}
        print(
            f"search        n={wall.get('count', 0)} "
            f"p50={wall.get('p50_ms')}ms p95={wall.get('p95_ms')}ms"
        )

    http = report["http"]
    cold = http["cold"]["summary"]
    cached = http["cached"]["summary"]
    wall = cold["wall_ms"] or {}
    network = cold["network_ms"] or {}
    extract = cold["extract_ms"] or {}
    print(
        f"HTTP cold     {http['cold']['successes']}/{len(http['urls'])} ok  "
        f"wall p50={wall.get('p50_ms')}ms p95={wall.get('p95_ms')}ms  "
        f"network p50={network.get('p50_ms')}ms  "
        f"extract p50={extract.get('p50_ms')}ms  "
        f"app p50={(cold['app_ms'] or {}).get('p50_ms')}ms"
    )
    print(
        f"HTTP cached   {http['cached']['cache_hits']}/{http['cached']['checked']} hits  "
        f"wall p50={(cached['wall_ms'] or {}).get('p50_ms')}ms  "
        f"speedup={http.get('cache_speedup_p50')}x"
    )
    browser = report.get("browser", {})
    if not browser.get("skipped"):
        warm_browser = browser.get("warm_browser_ms") or {}
        print(
            f"browser       engine={report.get('browser', {}).get('engine_after_warm')}  "
            f"cold={browser.get('cold_browser_ms')}ms "
            f"warm p50={warm_browser.get('p50_ms')}ms p95={warm_browser.get('p95_ms')}ms  "
            f"worker_reused={browser.get('worker_reused')}"
        )
    verification = http["browser_verification"]
    print(
        "browser during HTTP suite: "
        f"{'NOT STARTED' if verification['passed'] else 'STARTED - FAIL'}"
    )
    checks = report["checks"]
    print(
        f"checks        {sum(1 for v in checks.values() if v)}/{len(checks)} passed  "
        f"({checks['http_urls_measured']} URLs measured)"
    )
    print(f"saved         {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--urls",
        help="comma-separated URL list to replace the default 20",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "benchmarks" / "web_tools_benchmark.json"),
        help="JSON report path",
    )
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(_run(args))
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _print_summary(report, output)
    return 0 if report["checks"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
