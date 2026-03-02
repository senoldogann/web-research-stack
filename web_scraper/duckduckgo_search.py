"""DuckDuckGo Search integration - privacy-friendly, less bot protection."""

import asyncio
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from web_scraper.config import config
from web_scraper.stealth import HeaderFactory


class DuckDuckGoSearcher:
    """Search DuckDuckGo and extract top results."""

    BASE_URL = "https://html.duckduckgo.com/html"
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.2; rv:122.0) Gecko/20100101 Firefox/122.0",
    ]

    # Date filter parameters for DuckDuckGo
    # df = date filter: d=day, w=week, m=month, y=year
    DATE_FILTERS = {
        "day": "d",
        "week": "w",
        "month": "m",
        "year": "y",
    }

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
        import random

        headers = HeaderFactory.get_headers()
        headers["User-Agent"] = random.choice(self.USER_AGENTS)

        results = []
        async with requests.AsyncSession(
            timeout=config.duckduckgo_request_timeout_seconds,
            impersonate=HeaderFactory.get_impersonate_target(),
        ) as client:
            # Step 1: Get initial VQD token to bypass bot protection
            resp = await client.get(
                f"https://duckduckgo.com/?q={quote_plus(query)}", headers=headers
            )
            vqd_match = re.search(r"vqd=([\d-]+)", resp.text)

            # Fallback data if VQD extraction fails, prepare params for GET
            params = {
                "q": query,
                "kl": "us-en",
            }
            if vqd_match:
                params["vqd"] = vqd_match.group(1)

            # Add date filter if specified (e.g., df=y for last year)
            if date_filter and date_filter in self.DATE_FILTERS:
                params["df"] = self.DATE_FILTERS[date_filter]

            # Step 2: Extract results via pagination loop
            while len(results) < num_results:
                response = await client.get(
                    self.BASE_URL,
                    params=params,
                    headers=headers,
                )

                # If bot detected, HTML endpoint returns 202 instead of 200
                if response.status_code == 202:
                    break

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

                # Preserve date filter across pagination
                if date_filter and date_filter in self.DATE_FILTERS:
                    next_params["df"] = self.DATE_FILTERS[date_filter]

                params = next_params
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
                snippet = snippet_elem.get_text(strip=True)

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
