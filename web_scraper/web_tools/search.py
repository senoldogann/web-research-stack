"""Search provider adapters for URL discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from web_scraper.duckduckgo_search import DuckDuckGoBlocked, DuckDuckGoSearcher
from web_scraper.google_search import GoogleSearchBlocked, GoogleSearcher
from web_scraper.web_tools.errors import WebToolError


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }


class SearchAdapter(Protocol):
    name: str

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: Optional[str] = None,
    ) -> list[SearchResult]: ...


class DuckDuckGoSearchAdapter:
    """Privacy-friendly HTML search over plain HTTP, no browser."""

    name = "duckduckgo"

    def __init__(
        self,
        searcher: DuckDuckGoSearcher | None = None,
        *,
        region: str = "wt-wt",
        language: str = "en-US,en;q=0.9",
    ) -> None:
        self._searcher = searcher or DuckDuckGoSearcher(
            region=region,
            language=language,
        )

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: Optional[str] = None,
    ) -> list[SearchResult]:
        try:
            raw = await self._searcher.search(
                query,
                num_results=limit,
                date_filter=date_filter,
            )
        except DuckDuckGoBlocked as exc:
            raise WebToolError(
                "search_failed",
                "DuckDuckGo refused the search",
                detail="bot_detected",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface a stable error
            raise WebToolError(
                "search_failed",
                "DuckDuckGo search failed",
                detail=type(exc).__name__,
            ) from exc
        return _from_dicts(raw)


class GoogleSearchAdapter:
    """Google search constrained to windowless backends (Camoufox/headless Chrome)."""

    name = "google"

    def __init__(self, searcher: GoogleSearcher | None = None) -> None:
        self._searcher = searcher or GoogleSearcher(
            headless=True,
            allow_visible_browser=False,
        )

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: Optional[str] = None,
    ) -> list[SearchResult]:
        # The legacy Google searcher has no date-filter parameter.
        try:
            raw = await self._searcher.search(query, num_results=limit)
        except GoogleSearchBlocked as exc:
            raise WebToolError(
                "search_failed", "Google refused the search", detail=str(exc)
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface a stable error
            raise WebToolError(
                "search_failed",
                "Google search failed",
                detail=type(exc).__name__,
            ) from exc
        return _from_dicts(raw)


def _from_dicts(raw: list[dict]) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in raw:
        url = str(item.get("url") or "")
        if not url:
            continue
        results.append(
            SearchResult(
                title=str(item.get("title") or ""),
                url=url,
                snippet=str(item.get("snippet") or ""),
                source=str(item.get("source") or ""),
            )
        )
    return results


def build_search_adapter(name: str) -> SearchAdapter:
    normalized = (name or "duckduckgo").strip().lower()
    if normalized == "duckduckgo":
        return DuckDuckGoSearchAdapter()
    if normalized == "google":
        return GoogleSearchAdapter()
    raise WebToolError(
        "invalid_request",
        f"Unknown search provider: {normalized}",
        detail="Use 'duckduckgo' or 'google'",
    )
