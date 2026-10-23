"""DuckDuckGo Search integration - privacy-friendly, less bot protection.

Two endpoints over plain HTTP, the html one and the lite one. Both answer HTTP 202 with a
duck-picking challenge to a request that does not look like their own form's: a live probe
(2026-10-23) of ``q`` and ``kl`` alone, as a GET to the html endpoint and as a POST to the
lite one, was refused, while a POST of the fields the form itself sends (``s``, ``o``,
``api``, ``vqd``, ``bing_market``) was answered with results, in one request. So a search is
one POST of those fields, from one long-lived client that keeps its cookies and sends the
headers of the browser it impersonates, none of them overridden.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from web_scraper.config import config
from web_scraper.stealth import HeaderFactory


class DuckDuckGoBlocked(RuntimeError):
    """DuckDuckGo answered with its bot-detection response."""


class _SharedSession:
    """The one browser-like HTTP client every DuckDuckGo call of the process goes through.

    A client made for each search sent a new TLS fingerprint, no cookies and a fresh
    connection every time, and asked the JavaScript page for a token first. This one is made
    once per event loop, impersonates one Chrome, and keeps DuckDuckGo's cookies.
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def get(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._session is None or self._loop is not loop:
            self._session = requests.AsyncSession(
                timeout=config.duckduckgo_request_timeout_seconds,
                impersonate=HeaderFactory.stable_impersonate_target(),
            )
            self._loop = loop
        return self._session

    async def close(self) -> None:
        session, self._session, self._loop = self._session, None, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - a client of a finished loop cannot be closed
                pass


_SESSION = _SharedSession()


async def close_duckduckgo_session() -> None:
    """Close the shared client (at the end of the process)."""
    await _SESSION.close()


class DuckDuckGoSearcher:
    """Search DuckDuckGo's html endpoint and extract top results."""

    BASE_URL = "https://html.duckduckgo.com/html/"
    ORIGIN = "https://html.duckduckgo.com"

    # Date filter parameters for DuckDuckGo
    # df = date filter: d=day, w=week, m=month, y=year
    DATE_FILTERS = {
        "day": "d",
        "week": "w",
        "month": "m",
        "year": "y",
    }
    MAX_QUERY_LENGTH = 499

    def __init__(
        self,
        *,
        region: str = "wt-wt",
        language: str = "en-US,en;q=0.9",
        client: Any = None,
    ) -> None:
        self.region = region or "wt-wt"
        self.language = language or "en-US,en;q=0.9"
        # A client with ``post`` (tests give one); by default the process's shared session.
        self._client = client

    def _build_headers(self) -> Dict[str, str]:
        # The impersonated browser sends its own user agent, client hints and accept
        # headers, and they agree with its TLS fingerprint; overriding any of them with a
        # random one made the request incoherent. Only the locale is ours.
        return {"accept-language": self.language}

    def _post_headers(self) -> Dict[str, str]:
        """What a browser adds to the submit of the endpoint's own form."""
        headers = self._build_headers()
        headers["origin"] = self.ORIGIN
        headers["referer"] = self.ORIGIN + "/"
        headers["sec-fetch-site"] = "same-origin"
        return headers

    def _normalise_query(self, query: str) -> str:
        return " ".join((query or "").split())[: self.MAX_QUERY_LENGTH]

    def _build_params(
        self,
        query: str,
        date_filter: Optional[str] = None,
        *,
        vqd: Optional[str] = None,
    ) -> Dict[str, str]:
        """The fields of the endpoint's own form: a request without them is a bot's."""
        params = {
            "q": self._normalise_query(query),
            "s": "0",
            "o": "json",
            "api": "d.js",
            "vqd": vqd or "",
            "kl": self.region,
            "bing_market": self.region,
        }
        if date_filter in self.DATE_FILTERS:
            params["df"] = self.DATE_FILTERS[date_filter]
        return params

    async def search(
        self,
        query: str,
        num_results: int = 10,
        date_filter: Optional[str] = None,
    ) -> List[Dict]:
        """
        Search DuckDuckGo and return top results.

        Args:
            query: Search query
            num_results: Number of results to return
            date_filter: Optional date filter - "day", "week", "month", or "year"
                         Use "year" for current topic queries to get fresh results

        Returns:
            List of result dictionaries with title, url, snippet
        """
        query = self._normalise_query(query)
        if not query:
            return []

        client = self._client or await _SESSION.get()
        headers = self._post_headers()
        params = self._build_params(query, date_filter)
        results: List[Dict] = []

        # One POST of the form per page; the next page's form comes with the answer.
        while len(results) < num_results:
            response = await client.post(self.BASE_URL, data=params, headers=headers)

            # If bot detected, the endpoint returns 202 instead of 200
            if response.status_code == 202:
                raise DuckDuckGoBlocked("DuckDuckGo returned a bot-detection page")

            response.raise_for_status()

            page_results, next_params = self._parse_results(response.text)

            if not page_results:
                break

            # avoid duplicates, add new results
            seen_urls = {r["url"] for r in results}
            for res in page_results:
                if res["url"] not in seen_urls:
                    results.append(res)
                    seen_urls.add(res["url"])

            if not next_params or len(results) >= num_results:
                break

            params = dict(next_params)
            params["q"] = query
            params["kl"] = self.region
            if date_filter and date_filter in self.DATE_FILTERS:
                params["df"] = self.DATE_FILTERS[date_filter]
            await asyncio.sleep(config.duckduckgo_request_delay_seconds)

        return results[:num_results]

    def _parse_results(self, html: str) -> Tuple[List[Dict], Optional[Dict]]:
        """Parse DuckDuckGo search results from HTML and extract pagination data."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Find all result containers
        for result in soup.find_all("div", class_="result"):
            parsed = self._extract_result(result)
            if parsed:
                results.append(parsed)

        return results, self._extract_next_page_payload(soup)

    def _extract_next_page_payload(self, soup: BeautifulSoup) -> Optional[Dict]:
        """Extract the hidden form payload needed to request the next results page."""
        for form in soup.find_all("form"):
            hidden_inputs = form.find_all("input", type="hidden")
            field_names = {input_tag.get("name") for input_tag in hidden_inputs}
            if "s" not in field_names or "vqd" not in field_names:
                continue

            payload = {
                input_tag.get("name"): input_tag.get("value", "")
                for input_tag in hidden_inputs
                if input_tag.get("name")
            }
            if payload:
                return payload

        return None

    def _extract_result(self, container) -> Optional[Dict]:
        """Extract data from a single result container."""
        try:
            # Extract title and URL
            title_elem = container.find("a", class_="result__a")
            if not title_elem:
                return None

            title = title_elem.get_text(strip=True)
            url = title_elem.get("href", "")

            # Clean up URL (DuckDuckGo uses redirects)
            if url.startswith("//"):
                url = f"https:{url}"
            elif url.startswith("/"):
                url = f"https://duckduckgo.com{url}"

            # Try to decode DuckDuckGo's redirect URLs
            if "duckduckgo.com/l/?" in url or "duckduckgo.com/y.js" in url:
                url = self._decode_ddg_url(url)

            # Extract snippet
            snippet = ""
            snippet_elem = container.find("a", class_="result__snippet")
            if snippet_elem:
                snippet = re.sub(r"\s+", " ", snippet_elem.get_text("", strip=False)).strip()

            # Skip if missing critical data
            if not title or not url:
                return None

            # Skip DuckDuckGo internal URLs (including any un-decoded redirects)
            if "duckduckgo.com" in url:
                return None

            return {
                "title": title,
                "url": url,
                "snippet": snippet[:300] if snippet else "",
                "source": self._get_source_name(url),
            }

        except Exception:
            return None

    def _decode_ddg_url(self, url: str) -> str:
        """Decode DuckDuckGo redirect URL to get actual URL."""
        try:
            # Extract the actual URL from DuckDuckGo's redirect
            if "uddg=" in url:
                match = re.search(r"uddg=([^&]+)", url)
                if match:
                    return unquote(match.group(1))

            # Alternative pattern
            if "?uddg=" in url:
                parts = url.split("?uddg=")
                if len(parts) > 1:
                    return unquote(parts[1].split("&")[0])

            return url
        except Exception:
            return url

    def _get_source_name(self, url: str) -> str:
        """Extract source name from URL."""
        try:
            domain = urlparse(url).netloc.lower()

            # Remove www.
            if domain.startswith("www."):
                domain = domain[4:]

            # Map to readable names
            source_map = {
                "reddit.com": "reddit",
                "github.com": "github",
                "stackoverflow.com": "stackoverflow",
                "medium.com": "medium",
                "dev.to": "devto",
                "news.ycombinator.com": "hackernews",
                "arxiv.org": "arxiv",
                "wikipedia.org": "wikipedia",
                "youtube.com": "youtube",
                "twitter.com": "twitter",
                "x.com": "twitter",
                "openai.com": "openai",
                "anthropic.com": "anthropic",
                "gemini.google.com": "google-gemini",
                "ai.google": "google-ai",
            }

            for key, value in source_map.items():
                if key in domain:
                    return value

            return domain.split(".")[0]
        except Exception:
            return "unknown"


class DuckDuckGoLiteSearcher(DuckDuckGoSearcher):
    """DuckDuckGo's lite endpoint: its own form, and a table of results instead of divs.

    A provider of its own beside the html one: the same service, another page, so a refusal
    of the one is not always the other's.
    """

    BASE_URL = "https://lite.duckduckgo.com/lite/"
    ORIGIN = "https://lite.duckduckgo.com"

    def _parse_results(self, html: str) -> Tuple[List[Dict], Optional[Dict]]:
        """Read the lite table: a row with the link, then one with its snippet.

        Sponsored results (rows of class ``result-sponsored``, links through
        duckduckgo.com/y.js) are left out.
        """
        soup = BeautifulSoup(html, "lxml")
        results: List[Dict] = []
        current: Optional[Dict] = None

        for row in soup.find_all("tr"):
            if row.find("tr") is not None:  # a row that only holds a table
                continue
            if "result-sponsored" in (row.get("class") or []):
                current = None
                continue
            link = row.find("a", class_="result-link")
            if link is not None:
                current = self._extract_link(link)
                if current is not None:
                    results.append(current)
                continue
            snippet = row.find("td", class_="result-snippet")
            if snippet is not None and current is not None and not current["snippet"]:
                text = re.sub(r"\s+", " ", snippet.get_text("", strip=False)).strip()
                current["snippet"] = text[:300]

        return results, self._extract_next_page_payload(soup)

    def _extract_link(self, link) -> Optional[Dict]:
        title = re.sub(r"\s+", " ", link.get_text("", strip=False)).strip()
        url = link.get("href", "")
        if url.startswith("//"):
            url = f"https:{url}"
        if "duckduckgo.com/l/?" in url or "duckduckgo.com/y.js" in url:
            url = self._decode_ddg_url(url)
        if not title or not url.startswith("http") or "duckduckgo.com" in url:
            return None
        return {"title": title, "url": url, "snippet": "", "source": self._get_source_name(url)}


async def get_best_sources_ddg(
    query: str,
    max_sources: Optional[int] = None,
    date_filter: Optional[str] = None,
) -> List[Dict]:
    """
    Get the best sources for a query from DuckDuckGo search.

    Args:
        query: Search query
        max_sources: Number of results to return
        date_filter: Optional date filter - "day", "week", "month", or "year"
                     Use "year" for current topic queries to get fresh results

    Returns:
        List of source dictionaries
    """
    searcher = DuckDuckGoSearcher()
    target_results = max_sources or config.research_default_normal_source_target
    results = await searcher.search(
        query,
        num_results=target_results,
        date_filter=date_filter,
    )

    # Filter and return top results
    valid_results = [r for r in results if r.get("url")]

    return valid_results[:target_results]
