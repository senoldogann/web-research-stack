"""Google Search integration for finding best sources dynamically."""

import re
from typing import Dict, Optional
from urllib.parse import quote_plus, unquote

from bs4 import BeautifulSoup
from curl_cffi import requests

from web_scraper.config import config
from web_scraper.stealth import HeaderFactory


class GoogleSearcher:
    """Search Google and extract top results."""

    BASE_URL = "https://www.google.com/search"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    async def search(self, query: str, num_results: int = 10) -> list[dict]:
        """
        Search Google and return top results.

        Args:
            query: Search query
            num_results: Number of results to return

        Returns:
            List of result dictionaries with title, url, snippet
        """
        encoded_query = quote_plus(query)
        url = f"{self.BASE_URL}?q={encoded_query}&num={num_results}"

        headers = HeaderFactory.get_headers(url)

        async with requests.AsyncSession(
            timeout=config.google_request_timeout_seconds, impersonate="chrome120"
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

        return self._parse_results(response.text)

    def _parse_results(self, html: str) -> list[dict]:
        """Parse Google search results from HTML."""
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Find all search result containers
        for g in soup.find_all("div", class_="g"):
            result = self._extract_result(g)
            if result:
                results.append(result)

        # Alternative: try different selectors
        if not results:
            for container in soup.find_all(["div", "article"], {"class": re.compile("g|result")}):
                result = self._extract_result(container)
                if result:
                    results.append(result)

        return results

    def _extract_result(self, container) -> Optional[Dict]:
        """Extract data from a single result container."""
        try:
            # Extract title
            title_elem = container.find("h3")
            if not title_elem:
                return None
            title = title_elem.get_text(strip=True)

            # Extract URL
            link_elem = container.find("a", href=True)
            if not link_elem:
                return None

            url = link_elem["href"]
            # Clean up Google redirect URLs
            if url.startswith("/url?"):
                match = re.search(r"[?&]url=([^&]+)", url)
                if match:
                    url = unquote(match.group(1))
            elif url.startswith("/"):
                url = f"https://www.google.com{url}"

            # Extract snippet
            snippet = ""
            snippet_elem = container.find("div", {"class": re.compile("VwiC3b|s3v94d|Lyiue")})
            if snippet_elem:
                snippet = snippet_elem.get_text(strip=True)
            else:
                # Try alternative selectors
                for selector in ["span", "div"]:
                    elem = container.find(selector, string=True)
                    if elem and len(elem.get_text(strip=True)) > 50:
                        snippet = elem.get_text(strip=True)
                        break

            # Skip if missing critical data
            if not title or not url or url.startswith("https://www.google.com/search"):
                return None

            return {
                "title": title,
                "url": url,
                "snippet": snippet[:300] if snippet else "",
                "source": self._get_source_name(url),
            }

        except Exception:
            return None

    def _get_source_name(self, url: str) -> str:
        """Extract source name from URL."""
        try:
            from urllib.parse import urlparse

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
            }

            for key, value in source_map.items():
                if key in domain:
                    return value

            return domain.split(".")[0]
        except Exception:
            return "unknown"


async def get_best_sources(query: str, max_sources: int = 5) -> list[dict]:
    """
    Get the best sources for a query from Google search.

    Args:
        query: Search query
        max_sources: Maximum number of sources to return

    Returns:
        List of source dictionaries
    """
    searcher = GoogleSearcher()
    results = await searcher.search(query, num_results=max_sources + 3)

    # Filter and return top results
    valid_results = [
        r for r in results if r.get("url") and not r["url"].startswith("https://www.google.com")
    ]

    return valid_results[:max_sources]
