"""Async web scraper for batch operations."""

import asyncio
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from web_scraper.config import config
from web_scraper.content_safety import preserve_code_blocks
from web_scraper.network_safety import UnsafeTargetError, validate_outbound_url
from web_scraper.scrapers import ScrapedData
from web_scraper.stealth import HeaderFactory


class WebScraperAsync:
    """Async web scraper for batch operations."""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        timeout: float = 30.0,
        user_agent: Optional[str] = None,
        follow_redirects: bool = True,
        max_links: int = 100,
        allow_private_networks: Optional[bool] = None,
        max_redirects: Optional[int] = None,
    ):
        """
        Initialize the async web scraper.

        Args:
            timeout: Request timeout in seconds.
            user_agent: Custom user agent string.
            follow_redirects: Whether to follow HTTP redirects.
            max_links: Maximum number of links to extract.
        """
        self.timeout = timeout
        self.user_agent = user_agent or self.DEFAULT_USER_AGENT
        self.follow_redirects = follow_redirects
        self.max_links = max_links
        self.allow_private_networks = (
            config.scraper_allow_private_networks
            if allow_private_networks is None
            else allow_private_networks
        )
        self.max_redirects = (
            config.scraper_max_redirects if max_redirects is None else max_redirects
        )
        self._client: Optional[requests.AsyncSession] = None

    async def __aenter__(self) -> "WebScraperAsync":
        """Async context manager entry."""
        referer = HeaderFactory.get_referer()
        headers = HeaderFactory.get_headers(referer=referer)
        self._client = requests.AsyncSession(
            timeout=self.timeout,
            allow_redirects=False,
            headers=headers,
            impersonate=HeaderFactory.get_impersonate_target(),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.close()

    async def scrape(self, url: str) -> ScrapedData:
        """
        Scrape content from a URL asynchronously.

        Args:
            url: The URL to scrape.

        Returns:
            ScrapedData object with extracted content.
        """
        import time

        start_time = time.time()

        try:
            response = await self._request_with_safe_redirects(url)

            # Cloudflare detection: status code alone is NOT reliable —
            # IUAM (I'm Under Attack Mode) typically returns HTTP 200.
            # Trigger FlareSolverr whenever CF signatures appear in the HTML.
            html_lower = response.text.lower()
            is_cloudflare = (
                "just a moment" in html_lower
                or "cf-browser-verification" in html_lower
                or "cloudflare-challenge" in html_lower
                or "challenge-error-text" in html_lower
                or "enable javascript and cookies to continue" in html_lower
                or "cf-chl-bypass" in html_lower
                or "cf-challenge-body" in html_lower
                or ("ray id" in html_lower and "cloudflare" in html_lower)
                or (response.status_code in (403, 503) and "cloudflare" in html_lower)
            )

            if is_cloudflare:
                response = await self._run_flaresolverr(url)

            response_time = time.time() - start_time

            # Check if PDF
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/pdf" in content_type or url.lower().endswith(".pdf"):
                return await asyncio.to_thread(self._extract_pdf, response, url, start_time)

            soup = BeautifulSoup(response.text, "lxml")

            title = self._extract_title(soup)
            content = self._extract_content(soup)
            metadata = self._extract_metadata(soup)
            links = self._extract_links(soup, url)
            images = self._extract_images(soup, url)

            return ScrapedData(
                url=str(response.url),
                title=title,
                content=content,
                metadata=metadata,
                links=links,
                images=images,
                status_code=response.status_code,
                response_time=response_time,
            )

        except asyncio.TimeoutError as e:
            return self._error_result(url, f"Request timeout: {str(e)}")
        except UnsafeTargetError as e:
            return self._error_result(url, str(e))
        except requests.errors.RequestsError as e:
            return self._error_result(url, f"HTTP error: {str(e)}")
        except Exception as e:
            return self._error_result(url, f"Unexpected error: {str(e)}")

    async def scrape_batch(self, urls: list[str], concurrent: int = 5) -> list[ScrapedData]:
        """
        Scrape multiple URLs concurrently.

        Args:
            urls: List of URLs to scrape.
            concurrent: Number of concurrent requests.

        Returns:
            List of ScrapedData objects.
        """
        semaphore = asyncio.Semaphore(concurrent)

        async def scrape_with_limit(url: str) -> ScrapedData:
            async with semaphore:
                return await self.scrape(url)

        tasks = [scrape_with_limit(url) for url in urls]
        return await asyncio.gather(*tasks)

    async def _run_flaresolverr(self, url: str):
        """Invoke FlareSolverr to bypass Cloudflare challenges (IUAM, Turnstile, etc.)."""
        import os

        flaresolverr_url = os.environ.get(
            "FLARESOLVERR_URL", "http://web-research-flaresolverr:8191/v1"
        )
        # In local dev it might be localhost, so we attempt both if one fails
        urls_to_try = [
            flaresolverr_url,
            "http://localhost:8191/v1",
            "http://web-research-flaresolverr-dev:8191/v1",
        ]

        payload = {"cmd": "request.get", "url": url, "maxTimeout": 110000}

        for fs_url in urls_to_try:
            try:
                if not self._client:
                    self._client = requests.AsyncSession(timeout=120)
                res = await self._client.post(fs_url, json=payload, timeout=120)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("status") == "ok":

                        class MockResponse:
                            def __init__(self, text, url, status_code, headers, content):
                                self.text = text
                                self.url = url
                                self.status_code = status_code
                                self.headers = headers
                                self.content = content

                        return MockResponse(
                            text=data["solution"]["response"],
                            url=data["solution"]["url"],
                            status_code=data["solution"]["status"],
                            headers=data["solution"]["headers"],
                            content=data["solution"]["response"].encode("utf-8"),
                        )
            except Exception:
                continue

        raise Exception(
            "FlareSolverr bypass failed. Please ensure the FlareSolverr container is running on port 8191."
        )

    def _extract_pdf(self, response, url: str, start_time: float) -> ScrapedData:
        import io
        import time

        response_time = time.time() - start_time

        text = ""
        title = "PDF Document"
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(response.content))
            if reader.metadata and reader.metadata.title:
                title = reader.metadata.title

            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"

            if len(text) > config.scraper_max_raw_text_chars:
                text = text[: config.scraper_max_raw_text_chars]

        except Exception as e:
            return self._error_result(url, f"PDF Extraction Error: {str(e)}")

        return ScrapedData(
            url=url,
            title=title,
            content=text,
            metadata={"content_type": "application/pdf"},
            links={"internal": [], "external": []},
            images=[],
            status_code=response.status_code,
            response_time=response_time,
        )

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract page title."""
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title.get("content")

        return "No title found"

    def _extract_content(self, soup: BeautifulSoup) -> str:
        """Extract main content text from the page, preserving code blocks."""
        import re

        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        # Convert <pre>/<code> to markdown fences BEFORE get_text() strips them
        preserve_code_blocks(soup)

        main = soup.find("main") or soup.find("article") or soup.find("body")
        if main:
            text = main.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        text = re.sub(r"\n\s*\n", "\n\n", text)
        limit = config.max_source_content_chars
        return text[:limit] if len(text) > limit else text

    def _extract_metadata(self, soup: BeautifulSoup) -> dict:
        """Extract meta tags and page metadata."""
        metadata = {}

        meta_tags = soup.find_all("meta")
        for meta in meta_tags:
            name = meta.get("name") or meta.get("property")
            content = meta.get("content")
            if name and content:
                metadata[name] = content

        description = soup.find("meta", attrs={"name": "description"})
        if description:
            metadata["description"] = description.get("content", "")

        keywords = soup.find("meta", attrs={"name": "keywords"})
        if keywords:
            metadata["keywords"] = keywords.get("content", "")

        author = soup.find("meta", attrs={"name": "author"})
        if author:
            metadata["author"] = author.get("content", "")

        canonical = soup.find("link", rel="canonical")
        if canonical:
            metadata["canonical_url"] = canonical.get("href", "")

        return metadata

    def _extract_links(self, soup: BeautifulSoup, base_url: str) -> dict:
        """Extract all links from the page."""
        links = {"internal": [], "external": []}
        base_domain = self._get_domain(base_url)

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full_url = urljoin(base_url, href)
            link_domain = self._get_domain(full_url)

            link_info = {
                "url": full_url,
                "text": a.get_text(strip=True)[:100],
                "title": a.get("title", ""),
            }

            if link_domain == base_domain:
                links["internal"].append(link_info)
            else:
                links["external"].append(link_info)

            if len(links["internal"]) + len(links["external"]) >= self.max_links:
                break

        return links

    def _extract_images(self, soup: BeautifulSoup, base_url: str) -> list:
        """Extract image URLs from the page."""
        images = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src:
                full_url = urljoin(base_url, src)
                alt = img.get("alt", "")
                images.append({"url": full_url, "alt": alt})

                if len(images) >= config.max_images:
                    break

        return images

    def _get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        parsed = urlparse(url)
        return parsed.netloc

    async def _request_with_safe_redirects(self, url: str) -> requests.Response:
        """Fetch a URL while validating every redirect hop."""
        current_url = validate_outbound_url(
            url,
            allow_private_networks=self.allow_private_networks,
        )
        redirect_count = 0

        while True:
            response = await self._client.get(current_url)
            is_redirect = response.status_code in (301, 302, 303, 307, 308)
            if not self.follow_redirects or not is_redirect:
                return response

            location = response.headers.get("location")
            if not location:
                return response

            redirect_count += 1
            if redirect_count > self.max_redirects:
                raise ValueError(f"Exceeded safe redirect limit of {self.max_redirects}")

            current_url = validate_outbound_url(
                urljoin(str(response.url), location),
                allow_private_networks=self.allow_private_networks,
            )

    def _error_result(self, url: str, error: str) -> ScrapedData:
        """Create an error result."""
        return ScrapedData(
            url=url,
            title="",
            content="",
            error=error,
            status_code=0,
        )
