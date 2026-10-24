"""Yahoo Search over plain HTTP: a provider between DuckDuckGo lite and the headless browser.

When DuckDuckGo refuses (it did both of its endpoints after about 30 calls in 35 minutes from
one address), the only provider left was Google through a browser, 25 s when cold. A live probe
(2026-10-23) of Yahoo, ``search.yahoo.com/search?p=...``, through an impersonating curl_cffi
session answered HTTP 200 in about a second with parseable results; Mojeek, probed a minute
later, answered 403 ("your network appears to be sending automated queries"), so only Yahoo is
here.

Yahoo wraps each result's address in a redirect (``r.search.yahoo.com/.../RU=<encoded
address>/RK=...``), which this unwraps; it may send the first request through a cookie
dance on its own host (``/_bv/v.gif``) and back, which is followed while it stays on
``https://search.yahoo.com``; a redirect anywhere else (its consent page included) is a refusal.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from web_scraper.duckduckgo_search import SharedSession, accept_language


class YahooBlocked(RuntimeError):
    """Yahoo refused the search: a refusing status, a consent page, or a challenge."""


class YahooHttpError(RuntimeError):
    """Yahoo answered an error status that is not a refusal (a 5xx, a 404): its failure, not a wall."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Yahoo answered HTTP {status}")
        self.status = status


_SESSION = SharedSession()


async def close_yahoo_session() -> None:
    """Close the shared client (at the end of the process)."""
    await _SESSION.close()


class YahooSearcher:
    """Search Yahoo and extract the organic results."""

    BASE_URL = "https://search.yahoo.com/search"
    HOST = "search.yahoo.com"
    # Yahoo's own time filter (btf): a past day, week or month; it has none for a year.
    DATE_FILTERS = {"day": "d", "week": "w", "month": "m"}
    MAX_QUERY_LENGTH = 499
    MAX_PAGES = 3
    MAX_REDIRECTS = 4
    PAGE_DELAY_SECONDS = 0.3
    _REFUSING_STATUSES = frozenset({403, 429, 999})
    _CHALLENGE_PHRASES = (
        "unusual traffic",
        "verify you are human",
        "are you a robot",
        "captcha",
        "automated queries",
    )

    def __init__(self, *, language: str = "en-US,en;q=0.9", client: Any = None) -> None:
        self.language = language or "en-US,en;q=0.9"
        # A client with ``get`` (tests give one); by default the process's shared session.
        self._client = client

    async def search(
        self,
        query: str,
        num_results: int = 10,
        date_filter: Optional[str] = None,
        *,
        language: Optional[str] = None,
    ) -> List[Dict]:
        """Search Yahoo and return its organic results: title, url, snippet, source.

        Pages are followed through the ``Next`` link Yahoo gives (at most ``MAX_PAGES``) until
        ``num_results`` are in. Raises ``YahooBlocked`` when Yahoo refuses.
        """
        query = " ".join((query or "").split())[: self.MAX_QUERY_LENGTH]
        if not query:
            return []
        client = self._client or await _SESSION.get()
        headers = {"accept-language": accept_language(language) if language else self.language}
        params: Optional[Dict[str, str]] = {"p": query, "n": "10", "ei": "UTF-8"}
        if date_filter in self.DATE_FILTERS:
            params["fr2"] = "time"
            params["btf"] = self.DATE_FILTERS[date_filter]

        results: List[Dict] = []
        seen: set = set()
        url = self.BASE_URL
        for page in range(self.MAX_PAGES):
            response = await self._get(client, url, params, headers)
            page_results, next_url = self._parse_results(response.text)
            if not page_results:
                if page == 0:
                    self._refuse_a_challenge(response.text)
                break
            for item in page_results:
                if item["url"] not in seen:
                    seen.add(item["url"])
                    results.append(item)
            if len(results) >= num_results or next_url is None:
                break
            url, params = next_url, None  # the link carries its own query
            await asyncio.sleep(self.PAGE_DELAY_SECONDS)
        return results[:num_results]

    async def _get(
        self, client: Any, url: str, params: Optional[Dict[str, str]], headers: Dict[str, str]
    ) -> Any:
        """One page: redirects are followed here, and only while they stay on Yahoo's search host."""
        for _ in range(self.MAX_REDIRECTS + 1):
            response = await client.get(url, params=params, headers=headers, allow_redirects=False)
            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                target = urljoin(url, location) if location else ""
                if not self._on_search_host(target):
                    raise YahooBlocked("Yahoo sent the search somewhere else (a consent page?)")
                url, params = target, None
                continue
            if status in self._REFUSING_STATUSES:
                raise YahooBlocked(f"Yahoo answered HTTP {status}")
            if status >= 400:
                raise YahooHttpError(status)
            return response
        raise YahooBlocked("Yahoo redirected the search too many times")

    def _on_search_host(self, url: str) -> bool:
        parts = urlparse(url)
        return parts.scheme == "https" and (parts.hostname or "").lower() == self.HOST

    def _refuse_a_challenge(self, text: str) -> None:
        lowered = (text or "").lower()
        if any(phrase in lowered for phrase in self._CHALLENGE_PHRASES):
            raise YahooBlocked("Yahoo answered with a challenge page")

    def _parse_results(self, html: str) -> Tuple[List[Dict], Optional[str]]:
        """The organic results of a page (``div.algo-sr``) and the link of the next page."""
        soup = BeautifulSoup(html, "lxml")
        results: List[Dict] = []
        for container in soup.select("div.algo-sr"):
            link = container.select_one("div.compTitle a[href]") or container.find("a", href=True)
            if link is None:
                continue
            url = self._target(link.get("href", ""))
            heading = container.select_one("h3.title") or container.find("h3")
            title = re.sub(r"\s+", " ", (heading or link).get_text(" ", strip=True)).strip()
            if not url or not title:
                continue
            snippet = ""
            text = container.select_one("div.compText p") or container.select_one("div.compText")
            if text is not None:
                snippet = re.sub(r"\s+", " ", text.get_text("", strip=False)).strip()[:300]
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "source": self._get_source_name(url),
                }
            )
        next_link = soup.select_one("a.next[href]")
        next_url = urljoin(self.BASE_URL, next_link["href"]) if next_link else None
        if next_url is not None and not self._on_search_host(next_url):
            next_url = None
        return results, next_url

    @staticmethod
    def _target(href: str) -> Optional[str]:
        """The address a result leads to: Yahoo's redirect unwrapped, never a Yahoo page."""
        href = (href or "").strip()
        wrapped = re.search(r"/RU=([^/]+)/RK=", href)
        target = unquote(wrapped.group(1)) if wrapped else href
        if not target.startswith(("http://", "https://")):
            return None
        host = (urlparse(target).hostname or "").lower()
        if host == "search.yahoo.com" or host.endswith(".search.yahoo.com"):
            return None
        return target

    @staticmethod
    def _get_source_name(url: str) -> str:
        """The readable name of a result's site: its host without ``www.``, up to the first dot."""
        try:
            domain = urlparse(url).netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            return domain.split(".")[0]
        except Exception:  # noqa: BLE001
            return "unknown"
