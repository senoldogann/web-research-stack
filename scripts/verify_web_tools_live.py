#!/usr/bin/env python3
"""Live checks for the web tools' reliability fixes, against the real internet.

The unit tests never leave this machine; this script is the network half. It
runs the acceptance checks of the reliability fixes and prints one line per
check, then a PASS/FAIL summary:

- scrape: five large pages that used to fail as false captchas
- pdf: an arXiv PDF comes back as Markdown with a page count
- search: six discover searches in parallel, and a forced DuckDuckGo refusal
  answered by Google
- empty: a made-up query returns no results and says so
- crawl: quotes.toscrape.com stays on https with no page twice
- links: an ouka.fi scrape lists at most 50 links (its size is reported)
- matrix: with ``--urls FILE``, scrape every URL in the file and count blocks

Run it by hand; it sends real requests to DuckDuckGo, Google, and the sites
above:

    .venv/bin/python scripts/verify_web_tools_live.py
    .venv/bin/python scripts/verify_web_tools_live.py --only search,empty
    .venv/bin/python scripts/verify_web_tools_live.py --only matrix --urls urls.txt
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_scraper.web_tools.errors import WebToolError  # noqa: E402
from web_scraper.web_tools.search import SearchResult  # noqa: E402
from web_scraper.web_tools.service import WebToolsService  # noqa: E402

FALSE_CAPTCHA_URLS = [
    "https://en.wikipedia.org/wiki/Oulu",
    "https://github.com/senoldogann",
    "https://www.reddit.com/r/programming/",
    "https://www.instagram.com/nasa/",
    "https://www.verkkokauppa.com/",
]
PDF_URL = "https://arxiv.org/pdf/1706.03762"
PARALLEL_QUERIES = [
    "oulu weather october",
    "python asyncio taskgroup",
    "finland tax card 2026",
    "eu ai act timeline",
    "rust borrow checker explained",
    "kela housing allowance students",
]
NONSENSE_QUERY = "qzxvbnmp flurbulator 9237"
CRAWL_URL = "https://quotes.toscrape.com/"
LINK_URLS = ["https://www.ouka.fi/", "https://www.ouka.fi/tietoja-ja-tilastoja"]
SIZE_TARGET_BYTES = 6_000


class RefusingDuckDuckGo:
    """Stands in for DuckDuckGo answering with its bot-detection page."""

    name = "duckduckgo"

    async def search(
        self, query: str, limit: int, *, date_filter: Optional[str] = None
    ) -> list[SearchResult]:
        raise WebToolError("search_failed", "DuckDuckGo refused the search", detail="bot_detected")


def _size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


def _error(exc: WebToolError) -> dict[str, Any]:
    return {"code": exc.code, "detail": exc.detail, "status": exc.status_code}


async def check_scrape(service: WebToolsService, args: argparse.Namespace) -> dict[str, Any]:
    rows = []
    for url in FALSE_CAPTCHA_URLS:
        try:
            result = await service.scrape(url)
        except WebToolError as exc:
            rows.append({"url": url, "ok": False, **_error(exc)})
            continue
        rows.append(
            {
                "url": url,
                "ok": True,
                "via": result["fetched_with"],
                "markdown_chars": len(result["markdown"]),
            }
        )
    return {"passed": all(row["ok"] for row in rows), "rows": rows}


async def check_pdf(service: WebToolsService, args: argparse.Namespace) -> dict[str, Any]:
    try:
        result = await service.scrape(PDF_URL)
    except WebToolError as exc:
        return {"passed": False, "error": _error(exc)}
    found = "Attention Is All You Need" in result["markdown"]
    return {
        "passed": found and result.get("pages", 0) > 0,
        "content_type": result["content_type"],
        "pages": result.get("pages"),
        "title_found": found,
        "markdown_chars": len(result["markdown"]),
        "warnings": result["warnings"],
    }


async def check_search(service: WebToolsService, args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()

    async def one(query: str) -> dict[str, Any]:
        try:
            response = await service.search(query)
        except WebToolError as exc:
            return {"query": query, "ok": False, **_error(exc)}
        reports = response["queries"]
        return {
            "query": query,
            "ok": bool(response["results"]),
            "provider": response["provider"],
            "results": len(response["results"]),
            "angle_errors": [r["error"]["detail"] for r in reports if r["error"]],
            "fallbacks": [r["fallback"]["reason"] for r in reports if r.get("fallback")],
        }

    rows = await asyncio.gather(*(one(query) for query in PARALLEL_QUERIES))
    elapsed = round(time.perf_counter() - started, 1)
    refusals = sum(row.get("fallbacks", []).count("bot_detected") for row in rows)
    angle_errors = sum(len(row.get("angle_errors", [])) for row in rows)

    forced = WebToolsService.from_env()
    forced.search_adapters["duckduckgo"] = RefusingDuckDuckGo()
    try:
        response = await forced.search("helsinki university admissions", depth="fast")
        fallback = {"provider": response["provider"], "results": len(response["results"])}
    except WebToolError as exc:
        fallback = {"provider": None, **_error(exc)}
    finally:
        await forced.close()

    return {
        "passed": all(row["ok"] for row in rows)
        and angle_errors == 0
        and fallback["provider"] == "google",
        "parallel_seconds": elapsed,
        "failed_angles": angle_errors,
        "duckduckgo_refusals_answered_by_fallback": refusals,
        "rows": rows,
        "forced_refusal": fallback,
    }


async def check_empty(service: WebToolsService, args: argparse.Namespace) -> dict[str, Any]:
    try:
        response = await service.search(NONSENSE_QUERY)
    except WebToolError as exc:
        return {"passed": False, "error": _error(exc)}
    return {
        "passed": response["results"] == [] and "no_relevant_results" in response["warnings"],
        "results": len(response["results"]),
        "warnings": response["warnings"],
        "candidates": response["facets"]["total_candidates"],
    }


async def check_crawl(service: WebToolsService, args: argparse.Namespace) -> dict[str, Any]:
    started = await service.start_crawl(CRAWL_URL, {"max_pages": 30, "max_depth": 2})
    snapshot = started
    for _ in range(1_200):
        snapshot = await service.get_crawl(started["job_id"])
        if snapshot["status"] in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.25)
    results = (await service.crawl_results(started["job_id"], limit=100))["results"]
    urls = [item["url"] for item in results]
    pages = collections.Counter(url.split("://", 1)[1].rstrip("/") for url in urls)
    hashes = collections.Counter(item["content_hash"] for item in results)
    http_urls = [url for url in urls if url.startswith("http://")]
    twice = [page for page, count in pages.items() if count > 1]
    same_content = [digest for digest, count in hashes.items() if count > 1]
    return {
        "passed": snapshot["status"] == "completed"
        and bool(urls)
        and not http_urls
        and not twice
        and not same_content,
        "status": snapshot["status"],
        "results": len(urls),
        "http_urls": http_urls[:5],
        "same_page_twice": twice[:5],
        "same_content_twice": len(same_content),
    }


async def check_links(service: WebToolsService, args: argparse.Namespace) -> dict[str, Any]:
    rows = []
    for url in LINK_URLS:
        for max_chars in (None, 1_500):
            try:
                result = await service.scrape(url, max_chars=max_chars)
            except WebToolError as exc:
                rows.append({"url": url, "ok": False, **_error(exc)})
                continue
            rows.append(
                {
                    "url": url,
                    "ok": True,
                    "max_chars": max_chars,
                    "links": len(result["links"]),
                    "links_truncated": result["links_truncated"],
                    "links_bytes": _size(result["links"]),
                    "response_bytes": _size(result),
                }
            )
    return {
        # The link cap is the check; the size target is reported, not enforced.
        "passed": all(row["ok"] and row["links"] <= 50 for row in rows),
        "size_target_bytes": SIZE_TARGET_BYTES,
        "within_size_target": [
            (row["url"], row["max_chars"])
            for row in rows
            if row["ok"] and row["response_bytes"] < SIZE_TARGET_BYTES
        ],
        "rows": rows,
    }


async def check_matrix(service: WebToolsService, args: argparse.Namespace) -> dict[str, Any]:
    if not args.urls:
        return {"passed": True, "skipped": "pass --urls FILE to run the matrix"}
    urls = [
        line.strip()
        for line in Path(args.urls).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    gate = asyncio.Semaphore(3)

    async def one(url: str) -> dict[str, Any]:
        started = time.perf_counter()
        async with gate:
            try:
                result = await asyncio.wait_for(service.scrape(url, max_chars=1_500), timeout=70)
            except WebToolError as exc:
                row: dict[str, Any] = {"url": url, "ok": False, **_error(exc)}
            except asyncio.TimeoutError:
                row = {"url": url, "ok": False, "code": "timeout", "detail": "70 s"}
            else:
                row = {
                    "url": url,
                    "ok": True,
                    "status": result["status_code"],
                    "via": result["fetched_with"],
                    "markdown_chars": len(result["markdown"]),
                }
        row["ms"] = round((time.perf_counter() - started) * 1000)
        return row

    rows = await asyncio.gather(*(one(url) for url in urls))
    blocked = collections.Counter(row.get("detail") for row in rows if row.get("code") == "blocked")
    return {
        "passed": True,
        "scraped": sum(row["ok"] for row in rows),
        "total": len(rows),
        "blocked": dict(blocked),
        "rows": rows,
    }


CHECKS: dict[str, Callable[[WebToolsService, argparse.Namespace], Awaitable[dict[str, Any]]]] = {
    "scrape": check_scrape,
    "pdf": check_pdf,
    "search": check_search,
    "empty": check_empty,
    "crawl": check_crawl,
    "links": check_links,
    "matrix": check_matrix,
}


async def _run(args: argparse.Namespace) -> int:
    names = [name.strip() for name in args.only.split(",")] if args.only else list(CHECKS)
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        print(f"unknown checks: {', '.join(unknown)}", file=sys.stderr)
        return 2

    report: dict[str, Any] = {}
    service = WebToolsService.from_env()
    try:
        for name in names:
            started = time.perf_counter()
            try:
                outcome = await CHECKS[name](service, args)
            except Exception as exc:  # noqa: BLE001 - one check must not hide the others
                outcome = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            outcome["seconds"] = round(time.perf_counter() - started, 1)
            report[name] = outcome
            print(json.dumps({name: outcome}, ensure_ascii=False), flush=True)
    finally:
        await service.close()
        try:
            from web_scraper.google_search import GoogleSearcher

            await GoogleSearcher().close_camoufox()
        except Exception:  # noqa: BLE001 - nothing to close when Google never ran
            pass

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print()
    for name, outcome in report.items():
        print(f"{'PASS' if outcome['passed'] else 'FAIL'}  {name}")
    return 0 if all(outcome["passed"] for outcome in report.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", help=f"comma-separated checks: {', '.join(CHECKS)}")
    parser.add_argument("--urls", help="file of URLs for the matrix check, one per line")
    parser.add_argument("--json", help="also write the full report to this file")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
