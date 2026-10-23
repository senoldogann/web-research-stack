"""Search provider adapters for URL discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from web_scraper.duckduckgo_search import (
    DuckDuckGoBlocked,
    DuckDuckGoLiteSearcher,
    DuckDuckGoSearcher,
    close_duckduckgo_session,
)
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
    # True for a provider that drives a browser: slow when cold, so a quick search asks the
    # plain-HTTP providers first. An adapter that does not say is plain HTTP.
    headless: bool

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
    label = "DuckDuckGo"
    headless = False
    searcher_class: type[DuckDuckGoSearcher] = DuckDuckGoSearcher

    def __init__(
        self,
        searcher: DuckDuckGoSearcher | None = None,
        *,
        region: str = "wt-wt",
        language: str = "en-US,en;q=0.9",
    ) -> None:
        self._searcher = searcher or self.searcher_class(
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
                f"{self.label} refused the search",
                detail="bot_detected",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface a stable error
            raise WebToolError(
                "search_failed",
                f"{self.label} search failed",
                detail=type(exc).__name__,
            ) from exc
        return _from_dicts(raw)

    async def aclose(self) -> None:
        await close_duckduckgo_session()


class DuckDuckGoLiteSearchAdapter(DuckDuckGoSearchAdapter):
    """DuckDuckGo's lite page over plain HTTP: the provider to ask before a browser."""

    name = "duckduckgo_lite"
    label = "DuckDuckGo lite"
    searcher_class = DuckDuckGoLiteSearcher


class GoogleSearchAdapter:
    """Google search constrained to windowless backends (Camoufox/headless Chrome)."""

    name = "google"
    headless = True

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
    if normalized == "duckduckgo_lite":
        return DuckDuckGoLiteSearchAdapter()
    if normalized == "google":
        return GoogleSearchAdapter()
    raise WebToolError(
        "invalid_request",
        f"Unknown search provider: {normalized}",
        detail="Use 'duckduckgo', 'duckduckgo_lite' or 'google'",
    )
