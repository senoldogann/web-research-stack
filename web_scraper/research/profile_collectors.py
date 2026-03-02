"""Profile-specific free source collectors for deep research mode.

These collectors only return lightweight search-like candidate objects and
reuse the existing scraping and ranking pipeline.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx


def _clean_html_snippet(snippet: str) -> str:
    """Strip minimal HTML tags from upstream snippets."""
    return re.sub(r"<[^>]+>", "", snippet or "").strip()


async def collect_wikipedia_results(
    search_queries: list[str],
    search_pool_size: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Collect article candidates from Wikipedia search API."""
    if not search_queries:
        return []

    per_query_budget = max(1, min(search_pool_size // max(len(search_queries), 1) + 1, 8))
    seen_urls: set[str] = set()
    merged: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for search_query in search_queries:
            params = {
                "action": "query",
                "list": "search",
                "srsearch": search_query,
                "srlimit": per_query_budget,
                "format": "json",
                "utf8": "1",
            }
            response = await client.get("https://en.wikipedia.org/w/api.php", params=params)
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("query", {}).get("search", []):
                title = str(item.get("title", "")).strip()
                if not title:
                    continue
                url = f"https://en.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                merged.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": _clean_html_snippet(str(item.get("snippet", ""))),
                        "source": "wikipedia",
                        "search_provider": "wikipedia",
                        "search_query": search_query,
                    }
                )
                if len(merged) >= search_pool_size:
                    return merged

    return merged[:search_pool_size]


async def collect_hackernews_results(
    search_queries: list[str],
    search_pool_size: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Collect recency-oriented story candidates from HN Algolia search."""
    if not search_queries:
        return []

    per_query_budget = max(1, min(search_pool_size // max(len(search_queries), 1) + 1, 10))
    seen_urls: set[str] = set()
    merged: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for search_query in search_queries:
            params = {
                "query": search_query,
                "tags": "story",
                "hitsPerPage": per_query_budget,
            }
            response = await client.get("https://hn.algolia.com/api/v1/search", params=params)
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("hits", []):
                story_url = (item.get("url") or item.get("story_url") or "").strip()
                title = (item.get("title") or item.get("story_title") or "").strip()
                if not story_url or not title:
                    continue
                if story_url in seen_urls:
                    continue
                seen_urls.add(story_url)
                merged.append(
                    {
                        "title": title,
                        "url": story_url,
                        "snippet": str(item.get("_highlightResult", {}))[:240],
                        "source": "hackernews",
                        "search_provider": "hackernews",
                        "search_query": search_query,
                        "publication_date": item.get("created_at"),
                    }
                )
                if len(merged) >= search_pool_size:
                    return merged

    return merged[:search_pool_size]


async def collect_arxiv_results(
    search_queries: list[str],
    search_pool_size: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Collect academic candidates from arXiv Atom API."""
    if not search_queries:
        return []

    per_query_budget = max(1, min(search_pool_size // max(len(search_queries), 1) + 1, 8))
    seen_urls: set[str] = set()
    merged: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for search_query in search_queries:
            params = {
                "search_query": f"all:{search_query}",
                "start": 0,
                "max_results": per_query_budget,
            }
            response = await client.get("https://export.arxiv.org/api/query", params=params)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}

            for entry in root.findall("atom:entry", ns):
                entry_id = entry.findtext("atom:id", default="", namespaces=ns).strip()
                title = entry.findtext("atom:title", default="", namespaces=ns).strip()
                summary = entry.findtext("atom:summary", default="", namespaces=ns).strip()
                published: Optional[str] = entry.findtext(
                    "atom:published", default=None, namespaces=ns
                )

                if not entry_id or not title:
                    continue
                if entry_id in seen_urls:
                    continue
                seen_urls.add(entry_id)
                merged.append(
                    {
                        "title": title,
                        "url": entry_id,
                        "snippet": summary[:350],
                        "source": "arxiv",
                        "search_provider": "arxiv",
                        "search_query": search_query,
                        "publication_date": published,
                    }
                )
                if len(merged) >= search_pool_size:
                    return merged

    return merged[:search_pool_size]
