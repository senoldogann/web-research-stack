#!/usr/bin/env python3
"""Measure ranked search relevance on a small labelled query set.

The labels are expected domains, not a search engine's positions: a result is
relevant when its host is one of the query's expected domains or a subdomain of
one. The report keeps per-query nDCG@5, MRR, official-source hits and freshness,
plus the provider's warnings, so a ranking change can be compared with the last
run instead of judged by the top result alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_scraper.web_tools.errors import WebToolError  # noqa: E402
from web_scraper.web_tools.relevance import score_results, summarize  # noqa: E402
from web_scraper.web_tools.service import WebToolsService  # noqa: E402

DEFAULT_QUERIES = ROOT / "benchmarks" / "search_relevance_queries.json"
DEFAULT_OUTPUT = ROOT / "benchmarks" / "web_search_relevance.json"
DEFAULT_LIMIT = 8
DEFAULT_K = 5
# A cooldown longer than this is reported instead of waited out; the benchmark should not stall for minutes.
MAX_COOLDOWN_WAIT = 120.0
_COOLDOWN_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s")


def _load_queries(path: Path) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{path} must hold a non-empty list of query rows")
    return rows


def _result_row(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": result.get("title", ""),
        "url": result.get("url", ""),
        "domain": result.get("domain", ""),
        "source_type": result.get("source_type", "other"),
        "year": result.get("year"),
        "recency": result.get("recency", "unknown"),
        "score": result.get("score", 0.0),
    }


def _cooldown_wait(error: WebToolError) -> float | None:
    """The seconds the provider asked the caller to wait, when it is a short cooldown."""
    match = _COOLDOWN_RE.search(str(error))
    if error.code != "provider_cooldown" or match is None:
        return None
    wait = float(match.group(1))
    return wait if wait <= MAX_COOLDOWN_WAIT else None


async def _search(
    service: WebToolsService, query: str, *, limit: int, depth: str
) -> dict[str, Any]:
    """Search once, waiting out a short provider cooldown instead of scoring it as a miss."""
    try:
        return await service.search(query, limit=limit, depth=depth)
    except WebToolError as error:
        wait = _cooldown_wait(error)
        if wait is None:
            raise
        print(f"cooling down {wait:.0f}s before retrying {query!r}")
        await asyncio.sleep(wait + 1)
        return await service.search(query, limit=limit, depth=depth)


async def _run(args: argparse.Namespace) -> int:
    rows = _load_queries(args.queries)
    if args.query:
        rows = [row for row in rows if args.query.casefold() in row["query"].casefold()]
        if not rows:
            raise SystemExit(f"no fixture query matches {args.query!r}")

    service = WebToolsService.from_env()
    current_year = datetime.now().year
    answered: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(rows):
            if index and args.delay > 0:
                await asyncio.sleep(args.delay)
            started = time.perf_counter()
            try:
                payload = await _search(service, row["query"], limit=args.limit, depth=args.depth)
            except WebToolError as error:
                failures.append(
                    {
                        "query": row["query"],
                        "error": error.to_dict(),
                        "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
                print(f"FAIL  {row['query']}  {error.code}: {error}")
                continue
            except Exception as error:  # noqa: BLE001 - a benchmark reports provider faults
                failures.append(
                    {
                        "query": row["query"],
                        "error": {"code": type(error).__name__, "message": str(error)},
                        "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
                print(f"FAIL  {row['query']}  {type(error).__name__}: {error}")
                continue

            metrics = score_results(
                row,
                payload["results"],
                k=args.k,
                current_year=current_year,
            )
            entry = {
                "query": row["query"],
                "metrics": metrics,
                "warnings": payload.get("warnings", []),
                "cache_hit": payload.get("cache_hit", False),
                "candidate_count": payload.get("facets", {}).get("total_candidates", 0),
                "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                "top": [_result_row(result) for result in payload["results"][: args.k]],
            }
            answered.append(entry)
            freshness = metrics["freshness_hit_at_5"]
            freshness_text = "" if freshness is None else f" fresh={int(freshness)}"
            warnings = ",".join(entry["warnings"])
            print(
                f"{metrics['ndcg_at_5']:.3f}  {metrics['mrr']:.3f}  "
                f"official={int(metrics['official_hit_at_3'])}{freshness_text}  "
                f"{row['query']}  {warnings}"
            )
    finally:
        await service.close()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": service.settings.search_provider,
        "depth": args.depth,
        "limit": args.limit,
        "k": args.k,
        "fixture": str(args.queries),
        "summary": summarize([item["metrics"] for item in answered], failures=len(failures)),
        "results": answered,
        "failures": failures,
    }
    if args.output is not None:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.output}")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if answered else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--no-write", action="store_true", help="Print the report without writing it."
    )
    parser.add_argument(
        "--query", default="", help="Run only fixture queries containing this text."
    )
    parser.add_argument("--depth", choices=["fast", "discover", "deep"], default="discover")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between live queries.")
    args = parser.parse_args()
    if args.no_write:
        args.output = None
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
