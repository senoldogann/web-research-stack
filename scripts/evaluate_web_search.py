#!/usr/bin/env python3
"""Measure ranked search relevance on a small labelled query set.

The labels are expected domains, not a search engine's positions: a result is
relevant when its host is one of the query's expected domains or a subdomain of
one. The report keeps per-query nDCG@5, MRR, official-source hits and freshness,
plus the provider's warnings, so a ranking change can be compared with the last
run instead of judged by the top result alone.

``--provider`` (once or more) measures providers side by side, each alone (its
fallback chain off), and ``chain`` the server as it runs (the configured
provider and its fallbacks): per provider, how many queries it answered, how
long they took (median and 95th percentile), and the same scores. A query row
may carry ``options`` for the search (``sites``, ``date_from``, ``depth``,
``strict_dates`` and the others of ``_ROW_OPTIONS``) and ``fresh_days``: the
query is about now, and a result of that many days at most is wanted among
the first three (``fresh_at_3``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
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
# What a query row may ask of the search besides its words, passed to WebToolsService.search as it is (and ``depth``,
# which replaces the run's).
_ROW_OPTIONS = (
    "sites",
    "exclude_sites",
    "date_from",
    "date_to",
    "strict_dates",
    "resolve_dates",
    "region",
    "language",
    "primary_first",
)
# The provider that is the server as it runs: the configured one and its fallbacks.
CHAIN = "chain"


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
    service: WebToolsService,
    row: dict[str, Any],
    *,
    limit: int,
    depth: str,
    provider: str | None = None,
) -> dict[str, Any]:
    """Search once, waiting out a short provider cooldown instead of scoring it as a miss."""
    options = dict(row.get("options") or {})
    unknown = set(options) - {*_ROW_OPTIONS, "depth"}
    if unknown:
        raise SystemExit(f"{row['query']!r}: options it does not know: {sorted(unknown)}")
    kwargs = {key: options[key] for key in _ROW_OPTIONS if key in options}
    kwargs["depth"] = options.get("depth", depth)
    if provider and provider != CHAIN:
        kwargs["provider"] = provider
    try:
        return await service.search(row["query"], limit=limit, **kwargs)
    except WebToolError as error:
        wait = _cooldown_wait(error)
        if wait is None:
            raise
        print(f"cooling down {wait:.0f}s before retrying {row['query']!r}")
        await asyncio.sleep(wait + 1)
        return await service.search(row["query"], limit=limit, **kwargs)


async def _evaluate(
    rows: list[dict[str, Any]], args: argparse.Namespace, provider: str | None
) -> dict[str, Any]:
    """Run every row through a service of its own; *provider* alone (its fallbacks off), the chain, or as set."""
    alone = bool(provider) and provider != CHAIN
    saved = os.environ.get("WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS")
    if alone:
        os.environ["WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS"] = "none"
    try:
        service = WebToolsService.from_env()
    finally:
        if saved is None:
            os.environ.pop("WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS", None)
        else:
            os.environ["WEB_TOOLS_SEARCH_FALLBACK_PROVIDERS"] = saved
    label = f"[{provider}] " if provider else ""
    current_year = datetime.now().year
    answered: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(rows):
            if index and args.delay > 0:
                await asyncio.sleep(args.delay)
            started = time.perf_counter()
            try:
                payload = await _search(
                    service, row, limit=args.limit, depth=args.depth, provider=provider
                )
            except WebToolError as error:
                failures.append(
                    {
                        "query": row["query"],
                        "error": error.to_dict(),
                        "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
                print(f"{label}FAIL  {row['query']}  {error.code}: {error}")
                continue
            except Exception as error:  # noqa: BLE001 - a benchmark reports provider faults
                failures.append(
                    {
                        "query": row["query"],
                        "error": {"code": type(error).__name__, "message": str(error)},
                        "wall_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
                print(f"{label}FAIL  {row['query']}  {type(error).__name__}: {error}")
                continue

            metrics = score_results(
                row,
                payload["results"],
                k=args.k,
                current_year=current_year,
            )
            entry = {
                "query": row["query"],
                "answered_by": payload.get("provider"),
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
            if metrics.get("fresh_at_3") is not None:
                freshness_text += f" fresh@3={int(metrics['fresh_at_3'])}"
            warnings = ",".join(entry["warnings"])
            print(
                f"{label}{metrics['ndcg_at_5']:.3f}  {metrics['mrr']:.3f}  "
                f"official={int(metrics['official_hit_at_3'])}{freshness_text}  "
                f"by={entry['answered_by']}  {row['query']}  {warnings}"
            )
    finally:
        await service.close()

    walls = sorted(item["wall_ms"] for item in [*answered, *failures])
    by: dict[str, int] = {}
    for item in answered:
        by[str(item["answered_by"])] = by.get(str(item["answered_by"]), 0) + 1
    return {
        "provider": provider or service.settings.search_provider,
        "alone": alone,
        "summary": summarize([item["metrics"] for item in answered], failures=len(failures)),
        "wall_ms_median": round(statistics.median(walls), 1) if walls else None,
        "wall_ms_p95": round(walls[min(len(walls) - 1, int(0.95 * len(walls)))], 1)
        if walls
        else None,
        "answered_by": by,
        "results": answered,
        "failures": failures,
    }


def _table(sections: list[dict[str, Any]]) -> str:
    """The providers side by side, one line each."""
    lines = [
        "| provider | answered | median ms | p95 ms | nDCG@5 | MRR | fresh@3 | dated@5 | answered by |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for section in sections:
        summary = section["summary"]
        lines.append(
            f"| {section['provider']}{'' if section['alone'] else ' (chain)'} "
            f"| {summary['answered']}/{summary['queries']} "
            f"| {section['wall_ms_median']} | {section['wall_ms_p95']} "
            f"| {summary['mean_ndcg_at_5']} | {summary['mean_mrr']} "
            f"| {summary['fresh_at_3']} | {summary['mean_dated_at_5']} "
            f"| {', '.join(f'{name} {count}' for name, count in section['answered_by'].items())} |"
        )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    rows = _load_queries(args.queries)
    if args.query:
        rows = [row for row in rows if args.query.casefold() in row["query"].casefold()]
        if not rows:
            raise SystemExit(f"no fixture query matches {args.query!r}")

    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "depth": args.depth,
        "limit": args.limit,
        "k": args.k,
        "fixture": str(args.queries),
    }
    if args.provider:
        sections = [await _evaluate(rows, args, provider) for provider in args.provider]
        report: dict[str, Any] = {**base, "providers": sections}
        answered = any(section["results"] for section in sections)
        table = _table(sections)
    else:
        section = await _evaluate(rows, args, None)
        report = {
            **base,
            "provider": section["provider"],
            "summary": section["summary"],
            "results": section["results"],
            "failures": section["failures"],
        }
        answered = bool(section["results"])
        table = json.dumps(section["summary"], ensure_ascii=False)
    if args.output is not None:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.output}")
    print(table)
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
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help=f"Measure this provider alone (its fallbacks off); {CHAIN!r} is the configured one with its "
        "fallbacks. Once or more: the report holds each, side by side.",
    )
    args = parser.parse_args()
    if args.no_write:
        args.output = None
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
