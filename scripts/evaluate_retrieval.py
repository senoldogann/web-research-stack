"""Run a lightweight retrieval benchmark against the search collection layer."""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urlsplit

from web_scraper.config import config
from web_scraper.research_agent import ResearchAgent

DEFAULT_BENCHMARK_QUERIES = [
    "latest ai coding agents",
    "python httpx async client timeout best practices",
    "openai responses api tool calling guide",
    "turkiye yapay zeka regulasyonlari 2026",
]


def _default_search_pool_size(deep_mode: bool, target_count: int) -> int:
    """Mirror runtime search-pool sizing for offline evaluation."""
    return min(
        max(
            target_count
            + (
                config.research_search_pool_extra_deep
                if deep_mode
                else config.research_search_pool_extra_normal
            ),
            config.research_deep_min_sources if deep_mode else target_count,
        ),
        config.research_deep_max_sources,
    )


async def _evaluate_query(
    agent: ResearchAgent,
    query: str,
    deep_mode: bool,
    use_query_rewrite: bool,
    search_pool_size: int | None,
) -> dict:
    """Evaluate retrieval quality for a single query."""
    cleaned_query = agent._clean_query_text(query)
    if use_query_rewrite:
        search_context = await agent._prepare_search_queries(cleaned_query, deep_mode=deep_mode)
        normalized_query = search_context["normalized_query"] or cleaned_query
        search_queries = search_context["search_queries"] or [normalized_query]
    else:
        normalized_query = cleaned_query
        search_queries = [cleaned_query]

    target_count = (
        config.research_default_deep_source_target
        if deep_mode
        else config.research_default_normal_source_target
    )
    effective_search_pool_size = search_pool_size or _default_search_pool_size(
        deep_mode=deep_mode,
        target_count=target_count,
    )

    search_collection = await agent._collect_search_results(
        query=normalized_query,
        search_queries=search_queries,
        search_pool_size=effective_search_pool_size,
        target_count=target_count,
    )
    results = search_collection["results"]
    unique_domains = sorted(
        {urlsplit(result.get("url", "")).netloc.lower() for result in results if result.get("url")}
    )

    return {
        "query": query,
        "normalized_query": normalized_query,
        "search_queries": search_queries,
        "result_count": len(results),
        "unique_domain_count": len(unique_domains),
        "providers_used": search_collection["providers_used"],
        "fallback_used": search_collection["fallback_used"],
        "top_results": [
            {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "provider": result.get("search_provider", "unknown"),
                "source": result.get("source", "unknown"),
            }
            for result in results[:5]
        ],
    }


async def _async_main(args: argparse.Namespace) -> int:
    """Execute the retrieval benchmark and print a machine-readable summary."""
    queries = args.queries or DEFAULT_BENCHMARK_QUERIES
    agent = ResearchAgent()
    evaluations = []

    for query in queries:
        evaluations.append(
            await _evaluate_query(
                agent=agent,
                query=query,
                deep_mode=args.deep_mode,
                use_query_rewrite=args.use_query_rewrite,
                search_pool_size=args.search_pool_size,
            )
        )

    summary = {
        "query_count": len(evaluations),
        "average_result_count": round(
            sum(item["result_count"] for item in evaluations) / max(len(evaluations), 1),
            2,
        ),
        "average_unique_domain_count": round(
            sum(item["unique_domain_count"] for item in evaluations) / max(len(evaluations), 1),
            2,
        ),
        "fallback_trigger_count": sum(1 for item in evaluations if item["fallback_used"]),
        "providers_seen": sorted(
            {provider for item in evaluations for provider in item["providers_used"]}
        ),
        "results": evaluations,
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        dest="queries",
        action="append",
        help="Benchmark a specific query. Repeat to evaluate multiple queries.",
    )
    parser.add_argument(
        "--deep-mode",
        action="store_true",
        help="Use the deep-search target sizing rules.",
    )
    parser.add_argument(
        "--use-query-rewrite",
        action="store_true",
        help="Include the model-backed query rewrite step in the benchmark.",
    )
    parser.add_argument(
        "--search-pool-size",
        type=int,
        help="Override the number of candidates collected before reranking.",
    )
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
