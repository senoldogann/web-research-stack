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


# ---------------------------------------------------------------------------
# StackExchange — technical profile deep-mode supplement
# ---------------------------------------------------------------------------

_SE_SITES: list[str] = [
    "stackoverflow",
    "unix",
    "superuser",
    "serverfault",
    "softwareengineering",
]


async def collect_stackexchange_results(
    search_queries: list[str],
    search_pool_size: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Collect high-quality Q&A candidates from StackExchange sites (no API key needed)."""
    if not search_queries:
        return []

    per_query_budget = max(1, min(search_pool_size // max(len(search_queries), 1) + 1, 5))
    seen_urls: set[str] = set()
    merged: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for search_query in search_queries:
            for site in _SE_SITES:
                if len(merged) >= search_pool_size:
                    break
                params = {
                    "intitle": search_query,
                    "order": "desc",
                    "sort": "votes",
                    "site": site,
                    "pagesize": per_query_budget,
                    "filter": "withbody",
                }
                try:
                    response = await client.get(
                        "https://api.stackexchange.com/2.3/search/advanced",
                        params=params,
                    )
                    response.raise_for_status()
                    items = response.json().get("items", [])
                    for item in items:
                        link = (item.get("link") or "").strip()
                        title = (item.get("title") or "").strip()
                        if not link or not title:
                            continue
                        if link in seen_urls:
                            continue
                        seen_urls.add(link)
                        merged.append(
                            {
                                "title": title,
                                "url": link,
                                "snippet": _clean_html_snippet((item.get("body") or "")[:300]),
                                "source": "stackexchange",
                                "search_provider": "stackexchange",
                                "search_query": search_query,
                                "publication_date": None,
                            }
                        )
                        if len(merged) >= search_pool_size:
                            break
                except Exception:  # noqa: BLE001
                    continue

    return merged[:search_pool_size]


# ---------------------------------------------------------------------------
# PubMed E-utilities — academic profile deep-mode supplement
# ---------------------------------------------------------------------------

_PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


async def collect_pubmed_results(
    search_queries: list[str],
    search_pool_size: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Collect biomedical candidates from PubMed E-utilities (no API key needed).

    Two-step pipeline: esearch → PMIDs, efetch → XML metadata.
    """
    if not search_queries:
        return []

    per_query_budget = max(1, min(search_pool_size // max(len(search_queries), 1) + 1, 8))
    seen_pmids: set[str] = set()
    merged: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for search_query in search_queries:
            if len(merged) >= search_pool_size:
                break
            try:
                esearch_params = {
                    "db": "pubmed",
                    "term": search_query,
                    "retmax": per_query_budget,
                    "retmode": "json",
                    "sort": "relevance",
                }
                resp = await client.get(_PUBMED_ESEARCH, params=esearch_params)
                resp.raise_for_status()
                pmids: list[str] = resp.json().get("esearchresult", {}).get("idlist", [])

                if not pmids:
                    continue

                efetch_params = {
                    "db": "pubmed",
                    "id": ",".join(pmids),
                    "retmode": "xml",
                    "rettype": "abstract",
                }
                summary_resp = await client.get(_PUBMED_EFETCH, params=efetch_params)
                summary_resp.raise_for_status()

                root = ET.fromstring(summary_resp.text)
                for article in root.iter("PubmedArticle"):
                    pmid_el = article.find(".//PMID")
                    pmid = pmid_el.text.strip() if (pmid_el is not None and pmid_el.text) else None
                    if not pmid or pmid in seen_pmids:
                        continue

                    title_el = article.find(".//ArticleTitle")
                    title = (
                        "".join(title_el.itertext()).strip()
                        if title_el is not None
                        else f"PubMed {pmid}"
                    )

                    ab_el = article.find(".//AbstractText")
                    snippet = "".join(ab_el.itertext())[:300].strip() if ab_el is not None else ""

                    pub_year_el = article.find(".//PubDate/Year")
                    pub_year = pub_year_el.text if pub_year_el is not None else None

                    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                    seen_pmids.add(pmid)
                    merged.append(
                        {
                            "title": title,
                            "url": url,
                            "snippet": snippet,
                            "source": "pubmed",
                            "search_provider": "pubmed",
                            "search_query": search_query,
                            "publication_date": pub_year,
                        }
                    )
                    if len(merged) >= search_pool_size:
                        break
            except Exception:  # noqa: BLE001
                continue

    return merged[:search_pool_size]


# ---------------------------------------------------------------------------
# RSS/Atom feeds — news profile deep-mode supplement
# ---------------------------------------------------------------------------

_NEWS_FEEDS: list[str] = [
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.apnews.com/rss/apf-topnews",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

_ATOM_NS = "http://www.w3.org/2005/Atom"


def _parse_rss_items(xml_text: str) -> list[dict[str, Any]]:
    """Parse RSS 2.0 or Atom 1.0 feed into normalised item dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[dict[str, Any]] = []
    tag = root.tag.lower()

    if "rss" in tag or root.find("channel") is not None:
        # RSS 2.0
        channel_el = root.find("channel")
        channel = channel_el if channel_el is not None else root
        for item_el in channel.findall("item"):

            def _text(el_name: str, _item_el: Any = item_el) -> str:
                el = _item_el.find(el_name)
                return "".join(el.itertext()).strip() if el is not None else ""

            title = _text("title")
            link = _text("link")
            desc = _text("description")
            pub_date = _text("pubDate")
            if title and link:
                items.append(
                    {
                        "title": title,
                        "url": link,
                        "snippet": _clean_html_snippet(desc[:300]),
                        "publication_date": pub_date or None,
                    }
                )
    else:
        # Atom 1.0
        ns = {"atom": _ATOM_NS}
        for entry in root.findall("atom:entry", ns):

            def _atext(el_name: str, _entry: Any = entry) -> str:
                el = _entry.find(el_name, ns)
                return "".join(el.itertext()).strip() if el is not None else ""

            title = _atext("atom:title")
            link_el = entry.find("atom:link", ns)
            link = (link_el.get("href") or "").strip() if link_el is not None else ""
            summary = _atext("atom:summary")
            updated = _atext("atom:updated")
            if title and link:
                items.append(
                    {
                        "title": title,
                        "url": link,
                        "snippet": _clean_html_snippet(summary[:300]),
                        "publication_date": updated or None,
                    }
                )

    return items


async def collect_rss_feed_results(
    search_queries: list[str],
    search_pool_size: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Collect recent news from curated RSS/Atom feeds, filtered by query keywords."""
    if not search_queries:
        return []

    query_keywords: set[str] = set()
    for q in search_queries:
        for word in re.split(r"[\s,;]+", q.lower()):
            if len(word) > 3:
                query_keywords.add(word)

    seen_urls: set[str] = set()
    merged: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        for feed_url in _NEWS_FEEDS:
            if len(merged) >= search_pool_size:
                break
            try:
                resp = await client.get(feed_url)
                resp.raise_for_status()
                feed_items = _parse_rss_items(resp.text)
                for item in feed_items:
                    url = item["url"]
                    if not url or url in seen_urls:
                        continue
                    haystack = (item["title"] + " " + item.get("snippet", "")).lower()
                    if query_keywords and not any(kw in haystack for kw in query_keywords):
                        continue
                    seen_urls.add(url)
                    merged.append(
                        {
                            **item,
                            "source": "rss",
                            "search_provider": "rss",
                            "search_query": search_queries[0],
                        }
                    )
                    if len(merged) >= search_pool_size:
                        break
            except Exception:  # noqa: BLE001
                continue

    return merged[:search_pool_size]
