#!/usr/bin/env python3
"""
Advanced Stress Test Script: Simulated 1000-request load (No LLM).
Supports direct Engine testing or FastAPI endpoint testing.
"""

import argparse
import asyncio
import statistics
import time
from typing import Any, Dict

import httpx

from web_scraper.duckduckgo_search import DuckDuckGoSearcher
from web_scraper.scrapers import WebScraper


async def simulate_engine_request(request_id: int, query: str) -> Dict[str, Any]:
    """Internal engine test (Internal Search + Scrape)."""
    start_time = time.monotonic()
    success = False
    error = None

    try:
        searcher = DuckDuckGoSearcher()
        results = await searcher.search(query, num_results=1)
        if results:
            target_url = results[0]["url"]
            with WebScraper(timeout=10.0) as scraper:
                data = scraper.scrape(target_url)
                if not data.error:
                    success = True
                else:
                    error = data.error
        else:
            error = "No search results"
    except Exception as e:
        error = str(e)

    return {
        "id": request_id,
        "duration": time.monotonic() - start_time,
        "success": success,
        "error": error,
    }


async def simulate_api_request(
    request_id: int, query: str, api_url: str, api_key: str
) -> Dict[str, Any]:
    """External API test (FastAPI /api/v1/research/tool)."""
    start_time = time.monotonic()
    success = False
    error = None

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {"X-Load-Test": "true"}
            if api_key:
                headers["X-API-Key"] = api_key

            # Using the tool endpoint which bypasses synthesis if requested
            response = await client.post(
                f"{api_url}/api/v1/tools/web-research",
                json={
                    "query": query,
                    "max_sources": 1,
                    "no_synthesis": True,  # Bypass LLM synthesis for throughput test
                },
                headers=headers,
            )
            if response.status_code == 200:
                success = True
            else:
                error = f"HTTP {response.status_code}: {response.text[:50]}"
    except Exception as e:
        error = str(e)

    return {
        "id": request_id,
        "duration": time.monotonic() - start_time,
        "success": success,
        "error": error,
    }


async def run_stress_test(
    total_requests: int, concurrency: int, api_url: str = None, api_key: str = None
):
    """Runs the stress test with controlled concurrency."""
    mode = "API" if api_url else "Internal Engine"
    print(f"🚀 Starting Stress Test: {total_requests} requests, concurrency={concurrency}")
    print(f"📡 Mode: {mode}")

    queries = [
        "What is the capital of Turkey?",
        "Latest news about space extraction",
        "Python 3.9 vs 3.10 performance",
        "Nvidia H100 benchmarks",
        "How to build a high performance scraper",
    ]

    semaphore = asyncio.Semaphore(concurrency)

    async def wrapped_request(req_id: int):
        async with semaphore:
            query = queries[req_id % len(queries)]
            if api_url:
                return await simulate_api_request(req_id, query, api_url, api_key)
            else:
                return await simulate_engine_request(req_id, query)

    start_time = time.perf_counter()
    tasks = [wrapped_request(i) for i in range(total_requests)]
    results = await asyncio.gather(*tasks)
    total_duration = time.perf_counter() - start_time

    durations = [r["duration"] for r in results]
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]

    print("\n" + "=" * 40)
    print("📊 STRESS TEST RESULTS")
    print("=" * 40)
    print(f"Total Requests:   {total_requests}")
    print(f"Concurrent Limit: {concurrency}")
    print(f"Total Time:       {total_duration:.2f}s")
    print(f"Requests/sec:     {total_requests / total_duration:.2f}")
    print(f"Success Rate:     {len(successes) / total_requests * 100:.1f}%")
    print("-" * 40)
    print(f"Mean Latency:     {statistics.mean(durations):.2f}s")
    if len(durations) > 1:
        print(f"Median Latency:   {statistics.median(durations):.2f}s")
        print(f"P95 Latency:      {statistics.quantiles(durations, n=20)[18]:.2f}s")

    if failures:
        print(f"❌ Failures ({len(failures)}):")
        unique_errors = {}
        for f in failures:
            unique_errors[f["error"]] = unique_errors.get(f["error"], 0) + 1
        sorted_errors = sorted(unique_errors.items(), key=lambda x: x[1], reverse=True)
        for err, count in sorted_errors[:3]:
            print(f"  • {count}x: {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stress test the web scraper engine.")
    parser.add_argument("--total", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument(
        "--api-url", type=str, help="FastAPI root URL (e.g., http://localhost:8000)"
    )
    parser.add_argument("--api-key", type=str, help="API Key for the endpoint")

    args = parser.parse_args()
    try:
        asyncio.run(run_stress_test(args.total, args.concurrency, args.api_url, args.api_key))
    except KeyboardInterrupt:
        print("\nStopping stress test...")
