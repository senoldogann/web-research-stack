"""Optional search providers that need a key: Brave Search API and Serper.

Each is on only when its key is in the environment (``BRAVE_SEARCH_API_KEY``, ``SERPER_API_KEY``),
and then it is the one a search asks first, DuckDuckGo and the rest staying behind it as the
fallbacks. Without the key the provider does not exist: it is not registered, a call that names it
is told which variable it needs, and ``web_health`` lists it as ``no_key``.

The key is read from the environment once, held in a closure that only builds the request's
header, sent in that header and nowhere else (never in a URL, a log line, an error or an answer),
and not shown by the adapter's ``repr``. A key a provider rejects is reported as ``invalid_key``.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from web_scraper.web_tools.dates import make_signal
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.search import SearchOptions, SearchResult


@dataclass(frozen=True)
class PaidProvider:
    """A provider that needs a key: its name, the variable that holds the key, its pace."""

    name: str
    env: str
    label: str
    # Never called faster than this: Brave's free plan allows one request a second.
    min_gap_ms: int = 0


PAID_PROVIDERS: dict[str, PaidProvider] = {
    "brave": PaidProvider("brave", "BRAVE_SEARCH_API_KEY", "Brave Search API", min_gap_ms=1_100),
    "serper": PaidProvider("serper", "SERPER_API_KEY", "Serper"),
}

_TAGS = re.compile(r"<[^>]+>")
_AGO = re.compile(r"(\d{1,3})\s+(minute|hour|day|week)s?\s+ago", re.IGNORECASE)
_BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
_SERPER_FRESHNESS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


def _plain(text: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", str(text or "")))).strip()


def _age_date(text: str, today: Optional[date] = None) -> str:
    """A date written as "3 days ago" or "Oct 21, 2026" as ``YYYY-MM-DD``; empty for other words."""
    now = today or date.today()
    ago = _AGO.search(text or "")
    if ago:
        amount, unit = int(ago.group(1)), ago.group(2).lower()
        days = {"minute": 0, "hour": 0, "day": amount, "week": 7 * amount}[unit]
        return (now - timedelta(days=days)).isoformat()
    made = make_signal("provider", text, 1.0, today=now)
    return made.label if made is not None else ""


class _KeyedAdapter:
    """What Brave and Serper share: a client, a header with the key, and errors with no secrets in them."""

    name = ""
    label = ""
    headless = False
    min_gap_ms = 0
    endpoint = ""

    def __init__(
        self,
        key: str,
        *,
        timeout: float = 20.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        secret = (key or "").strip()
        if not secret:
            raise ValueError(f"{type(self).__name__} needs a key")
        self._timeout = timeout
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._auth: Callable[[], dict[str, str]] = lambda: self._header(secret)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key=<set>)"

    def _header(self, secret: str) -> dict[str, str]:
        raise NotImplementedError

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=False,
                transport=self._transport,
                trust_env=False,
            )
        return self._client

    async def _call(
        self,
        method: str,
        *,
        params: Optional[dict[str, str]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            response = await self._http().request(
                method, self.endpoint, params=params, json=body, headers=self._auth()
            )
        except httpx.TimeoutException as exc:
            raise WebToolError(
                "timeout", f"{self.label} timed out", detail="search_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise WebToolError(
                "search_failed", f"{self.label} could not be reached", detail=type(exc).__name__
            ) from exc
        status = response.status_code
        if status in (401, 403):
            raise WebToolError(
                "search_failed",
                f"{self.label} did not accept the API key",
                status_code=status,
                detail="invalid_key",
            )
        if status == 429:
            raise WebToolError(
                "search_failed",
                f"{self.label} is limiting the rate of this key",
                status_code=status,
                detail="rate_limited",
            )
        if status >= 400:
            raise WebToolError(
                "search_failed",
                f"{self.label} answered HTTP {status}",
                status_code=status,
                detail=f"http_{status}",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise WebToolError(
                "search_failed", f"{self.label} sent no JSON", detail="bad_response"
            ) from exc

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


class BraveSearchAdapter(_KeyedAdapter):
    """Brave Search API: ``GET /res/v1/web/search`` with the key in ``X-Subscription-Token``."""

    name = "brave"
    label = "Brave Search API"
    min_gap_ms = PAID_PROVIDERS["brave"].min_gap_ms
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def _header(self, secret: str) -> dict[str, str]:
        return {"X-Subscription-Token": secret, "Accept": "application/json"}

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: Optional[str] = None,
        options: Optional[SearchOptions] = None,
    ) -> list[SearchResult]:
        params = {
            "q": " ".join(query.split())[:400],
            "count": str(min(max(limit, 1), 20)),
            "text_decorations": "false",
        }
        if options is not None:
            country = (options.region or "").split("-")[0]
            if country and country != "wt":
                params["country"] = country.upper()
            if options.language:
                params["search_lang"] = options.language.split("-")[0].lower()
            if options.date_from or options.date_to:
                start = options.date_from.isoformat() if options.date_from else "1990-01-01"
                end = options.date_to.isoformat() if options.date_to else date.today().isoformat()
                params["freshness"] = f"{start}to{end}"
        if "freshness" not in params and date_filter in _BRAVE_FRESHNESS:
            params["freshness"] = _BRAVE_FRESHNESS[str(date_filter)]
        data = await self._call("GET", params=params)
        rows = ((data or {}).get("web") or {}).get("results") or []
        results: list[SearchResult] = []
        for row in rows:
            url = str(row.get("url") or "")
            title = _plain(row.get("title"))
            if not url or not title:
                continue
            published = _age_date(str(row.get("page_age") or "")) or _age_date(
                str(row.get("age") or "")
            )
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=_plain(row.get("description"))[:300],
                    source=_source(url, row),
                    published=published,
                )
            )
        return results[:limit]


class SerperSearchAdapter(_KeyedAdapter):
    """Serper (Google results as JSON): ``POST /search`` with the key in ``X-API-KEY``."""

    name = "serper"
    label = "Serper"
    min_gap_ms = PAID_PROVIDERS["serper"].min_gap_ms
    endpoint = "https://google.serper.dev/search"

    def _header(self, secret: str) -> dict[str, str]:
        return {"X-API-KEY": secret, "Content-Type": "application/json"}

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: Optional[str] = None,
        options: Optional[SearchOptions] = None,
    ) -> list[SearchResult]:
        body: dict[str, Any] = {"q": " ".join(query.split()), "num": min(max(limit, 1), 100)}
        if options is not None:
            country = (options.region or "").split("-")[0]
            if country and country != "wt":
                body["gl"] = country.lower()
            if options.language:
                body["hl"] = options.language.split("-")[0].lower()
            if options.date_from or options.date_to:
                start = options.date_from or date(1990, 1, 1)
                end = options.date_to or date.today()
                body["tbs"] = (
                    f"cdr:1,cd_min:{start.month}/{start.day}/{start.year},"
                    f"cd_max:{end.month}/{end.day}/{end.year}"
                )
        if "tbs" not in body and date_filter in _SERPER_FRESHNESS:
            body["tbs"] = _SERPER_FRESHNESS[str(date_filter)]
        data = await self._call("POST", body=body)
        results: list[SearchResult] = []
        for row in (data or {}).get("organic") or []:
            url = str(row.get("link") or "")
            title = _plain(row.get("title"))
            if not url or not title:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=_plain(row.get("snippet"))[:300],
                    source=_source(url, row),
                    published=_age_date(str(row.get("date") or "")),
                )
            )
        return results[:limit]


def _source(url: str, row: Mapping[str, Any]) -> str:
    """The readable name of a result's site: its host without ``www.``, up to the first dot."""
    meta = row.get("meta_url")
    host = str(meta.get("hostname") or "") if isinstance(meta, dict) else ""
    host = (host or urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host.split(".")[0] if host else ""


def build_paid_adapters(
    environment: Optional[Mapping[str, str]] = None,
    *,
    timeout: float = 20.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict[str, Any]:
    """The adapters of the providers whose key is in the environment, in the order they are tried.

    A key that is empty or only spaces is no key.
    """
    env = os.environ if environment is None else environment
    classes: dict[str, type[_KeyedAdapter]] = {
        "brave": BraveSearchAdapter,
        "serper": SerperSearchAdapter,
    }
    adapters: dict[str, Any] = {}
    for name, provider in PAID_PROVIDERS.items():
        key = (env.get(provider.env) or "").strip()
        if key:
            adapters[name] = classes[name](key, timeout=timeout, transport=transport)
    return adapters
