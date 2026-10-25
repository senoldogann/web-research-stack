"""Search provider adapters for URL discovery."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional, Protocol

from web_scraper.duckduckgo_search import (
    DuckDuckGoBlocked,
    DuckDuckGoLiteSearcher,
    DuckDuckGoSearcher,
    close_duckduckgo_session,
)
from web_scraper.google_search import GoogleSearchBlocked, GoogleSearcher
from web_scraper.web_tools.errors import WebToolError
from web_scraper.yahoo_search import (
    YahooBlocked,
    YahooHttpError,
    YahooSearcher,
    close_yahoo_session,
)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    # A date the provider gave as a field of its own (Brave's page_age, Serper's date), as it
    # wrote it; empty for the providers that give none.
    published: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }
        if self.published:
            payload["published"] = self.published
        return payload


@dataclass(frozen=True)
class SearchOptions:
    """What a caller asks of one search beyond its words; each unset one is the setting's default."""

    region: Optional[str] = None
    language: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None

    def __bool__(self) -> bool:
        return any((self.region, self.language, self.date_from, self.date_to))


_REGION_RE = re.compile(r"[a-z]{2}-[a-z]{2}")
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")
_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}")


def normalize_region(value: str) -> str:
    """A DuckDuckGo region such as ``fi-fi``, ``us-en`` or ``wt-wt`` (no region)."""
    region = str(value).strip().lower()
    if not _REGION_RE.fullmatch(region):
        raise ValueError("region must look like fi-fi, us-en or wt-wt (country-language)")
    return region


def normalize_language(value: str) -> str:
    """A language tag such as ``fi``, ``en`` or ``pt-BR``."""
    language = str(value).strip()
    if not _LANGUAGE_RE.fullmatch(language):
        raise ValueError("language must be a language tag such as fi, en or pt-BR")
    primary, *rest = language.split("-")
    tags = [primary.lower()]
    for tag in rest:
        tags.append(
            tag.upper()
            if len(tag) == 2 and tag.isalpha()
            else tag.title()
            if len(tag) == 4 and tag.isalpha()
            else tag.lower()
        )
    return "-".join(tags)


def normalize_domains(values: Optional[Iterable[str]], label: str, *, limit: int = 5) -> list[str]:
    """Domains as ``example.com``: lower-cased, scheme, path and a leading ``www.`` dropped."""
    given = [str(value) for value in (values or [])]
    if len(given) > limit:
        raise ValueError(f"{label} takes at most {limit} domains")
    domains: list[str] = []
    for value in given:
        text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value.strip().lower())
        text = re.split(r"[/?#]", text, maxsplit=1)[0].rstrip(".")
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"{label}: {value!r} is not a domain") from None
        text = text.removeprefix("www.")
        if not _DOMAIN_RE.fullmatch(text):
            raise ValueError(f"{label}: {value!r} is not a domain such as example.com")
        if text not in domains:
            domains.append(text)
    return domains


def parse_iso_date(value: Any, label: str = "date") -> date:
    """An ISO date (``2026-10-01``) from a date or a string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{label} must be an ISO date such as 2026-10-01") from None


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
        options: Optional[SearchOptions] = None,
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
        options: Optional[SearchOptions] = None,
    ) -> list[SearchResult]:
        extra: dict[str, Any] = {}
        if options:
            extra = {
                "region": options.region,
                "language": options.language,
                "date_from": options.date_from,
                "date_to": options.date_to,
            }
        try:
            raw = await self._searcher.search(
                query,
                num_results=limit,
                date_filter=date_filter,
                **extra,
            )
        except DuckDuckGoBlocked as exc:
            raise WebToolError(
                "search_failed",
                f"{self.label} refused the search",
                detail="bot_detected",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface a stable error
            raise _failure(self.label, exc) from exc
        return _from_dicts(raw)

    async def aclose(self) -> None:
        await close_duckduckgo_session()


class DuckDuckGoLiteSearchAdapter(DuckDuckGoSearchAdapter):
    """DuckDuckGo's lite page over plain HTTP: the provider to ask before a browser."""

    name = "duckduckgo_lite"
    label = "DuckDuckGo lite"
    searcher_class = DuckDuckGoLiteSearcher


class YahooSearchAdapter:
    """Yahoo Search over plain HTTP: the provider between DuckDuckGo lite and the browser.

    It takes the relative date filter (a day, a week, a month) and the language of a call; a
    region and a date range it does not (the service's own date filter drops results dated
    outside a range all the same).
    """

    name = "yahoo"
    headless = False

    def __init__(
        self, searcher: YahooSearcher | None = None, *, language: str = "en-US,en;q=0.9"
    ) -> None:
        self._searcher = searcher or YahooSearcher(language=language)

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: Optional[str] = None,
        options: Optional[SearchOptions] = None,
    ) -> list[SearchResult]:
        extra: dict[str, Any] = (
            {"language": options.language} if options and options.language else {}
        )
        try:
            raw = await self._searcher.search(
                query, num_results=limit, date_filter=date_filter, **extra
            )
        except YahooBlocked as exc:
            raise WebToolError(
                "search_failed", "Yahoo refused the search", detail="bot_detected"
            ) from exc
        except YahooHttpError as exc:
            raise WebToolError(
                "search_failed",
                f"Yahoo answered HTTP {exc.status}",
                status_code=exc.status,
                detail=f"http_{exc.status}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface a stable error
            raise _failure("Yahoo", exc) from exc
        return _from_dicts(raw)

    async def aclose(self) -> None:
        await close_yahoo_session()


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
        options: Optional[SearchOptions] = None,
    ) -> list[SearchResult]:
        extra: dict[str, Any] = {}
        if options:
            country = (options.region or "").split("-")[0]
            extra = {
                "gl": country if country and country != "wt" else None,
                "hl": options.language,
                "date_from": options.date_from,
                "date_to": options.date_to,
            }
        if date_filter:
            extra["date_filter"] = date_filter
        # No warm-up before the first search: it opened Google in a page of its own and closed it, so the
        # search then made a page again (about 3 s each in Camoufox), and a warm-up that ran past its time
        # shut down the browser the search was about to use. The search's own page is kept for the next.
        try:
            raw = await self._searcher.search(query, num_results=limit, **extra)
        except GoogleSearchBlocked as exc:
            said = str(exc)
            wall = any(
                mark in said.lower()
                for mark in ("bot check", "captcha", "unusual traffic", "consent")
            )
            raise WebToolError(
                "search_failed",
                f"Google refused the search: {said[:160]}",
                detail="bot_detected" if wall else said[:160],
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface a stable error
            raise WebToolError(
                "search_failed",
                "Google search failed",
                detail=type(exc).__name__,
            ) from exc
        return _from_dicts(raw)

    async def warm_up(self) -> None:
        await self._searcher.warm_up()

    async def aclose(self) -> None:
        await self._searcher.close_camoufox()


def _failure(label: str, exc: Exception) -> WebToolError:
    """A provider's exception as a stable error: a timeout, an HTTP status, or its class name.

    A status that refuses (403, 429, 999) is a wall (``bot_detected``); any other error status is
    ``http_<status>``; an exception that says nothing of that keeps its class name.
    """
    name = type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in name.lower():
        return WebToolError("timeout", f"{label} timed out", detail="search_timeout")
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and status >= 400:
        if status in (403, 429, 999):
            return WebToolError(
                "search_failed",
                f"{label} refused the search",
                status_code=status,
                detail="bot_detected",
            )
        return WebToolError(
            "search_failed",
            f"{label} answered HTTP {status}",
            status_code=status,
            detail=f"http_{status}",
        )
    return WebToolError("search_failed", f"{label} search failed", detail=name)


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
                published=str(item.get("published") or ""),
            )
        )
    return results


def build_search_adapter(name: str) -> SearchAdapter:
    normalized = (name or "duckduckgo").strip().lower()
    if normalized == "duckduckgo":
        return DuckDuckGoSearchAdapter()
    if normalized == "duckduckgo_lite":
        return DuckDuckGoLiteSearchAdapter()
    if normalized == "yahoo":
        return YahooSearchAdapter()
    if normalized == "google":
        return GoogleSearchAdapter()
    raise WebToolError(
        "invalid_request",
        f"Unknown search provider: {normalized}",
        detail="Use 'duckduckgo', 'duckduckgo_lite', 'yahoo' or 'google'",
    )
