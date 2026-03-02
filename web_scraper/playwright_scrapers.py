"""Playwright-based scraper for JavaScript-rendered pages."""

import asyncio
import random
import re
import time
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from web_scraper.config import config
from web_scraper.content_safety import preserve_code_blocks
from web_scraper.network_safety import UnsafeTargetError, validate_outbound_url
from web_scraper.scrapers import ScrapedData

# Cloudflare challenge fingerprints
_CF_SIGNATURES = (
    "just a moment...",
    "cf-browser-verification",
    "cloudflare-challenge",
)


class PlaywrightScraper:
    """Web scraper using Playwright for JavaScript-rendered pages."""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        timeout: float = 30000,
        user_agent: Optional[str] = None,
        headless: bool = True,
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ) -> None:
        """Initialize the Playwright scraper.

        Args:
            timeout: Page load timeout in milliseconds.
            user_agent: Custom user agent string.
            headless: Run browser in headless mode.
            viewport_width: Browser viewport width.
            viewport_height: Browser viewport height.
        """
        self.timeout = timeout
        self.user_agent = user_agent or self.DEFAULT_USER_AGENT
        self.headless = headless
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self._playwright: Optional[Any] = None  # set in __aenter__
        self._browser: Optional[Any] = None
        self._context: Optional[Any] = None
        self._page: Optional[Any] = None

    async def __aenter__(self) -> "PlaywrightScraper":
        """Async context manager entry."""
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import stealth_async
        except ImportError as e:
            msg = (
                "Playwright dependencies not found. "
                'Install with: pip install -e ".[playwright]" && playwright install chromium'
            )
            raise ImportError(msg) from e

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, args=["--no-sandbox"]
        )

        # Create context with anti-detection settings
        self._context = await self._browser.new_context(
            viewport={"width": self.viewport_width, "height": self.viewport_height},
            user_agent=self.user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            permissions=["geolocation"],
        )

        self._page = await self._context.new_page()

        # Apply comprehensive stealth evasions (navigator, webgl, user-agent, etc.)
        await stealth_async(self._page)

        self._page.set_default_timeout(self.timeout)

        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """Async context manager exit."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scrape(self, url: str) -> ScrapedData:
        """Scrape content from a URL using Playwright.

        Args:
            url: The URL to scrape.

        Returns:
            ScrapedData object with extracted content.
        """
        try:
            validate_outbound_url(url)
        except UnsafeTargetError as e:
            return self._error_result(url, str(e))

        try:
            start_time = time.time()

            # Do not use networkidle/load — Cloudflare keeps sockets open indefinitely.
            response = await self._page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout
            )

            html = await self._bypass_cloudflare_if_needed()
            response_time = time.time() - start_time
            status_code = response.status if response else 0

            soup = BeautifulSoup(html, "lxml")
            return ScrapedData(
                url=self._page.url,
                title=self._extract_title(soup),
                content=self._extract_content(soup),
                metadata=self._extract_metadata(soup),
                links=self._extract_links(soup, url),
                images=self._extract_images(soup, url),
                status_code=status_code,
                response_time=response_time,
            )

        except Exception as e:  # noqa: BLE001
            return self._error_result(url, f"Playwright error: {e!s}")

    async def scrape_with_screenshot(
        self,
        url: str,
        output_path: str = "screenshot.png",
    ) -> ScrapedData:
        """Scrape and take a full-page screenshot.

        Args:
            url: The URL to scrape.
            output_path: Path to save screenshot.

        Returns:
            ScrapedData object with extracted content.
        """
        try:
            validate_outbound_url(url)
        except UnsafeTargetError as e:
            return self._error_result(url, str(e))

        try:
            start_time = time.time()

            response = await self._page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout
            )

            html = await self._bypass_cloudflare_if_needed()
            response_time = time.time() - start_time

            # Screenshot after potential challenge resolution
            await self._page.screenshot(path=output_path, full_page=True)

            status_code = response.status if response else 0
            soup = BeautifulSoup(html, "lxml")
            return ScrapedData(
                url=self._page.url,
                title=self._extract_title(soup),
                content=self._extract_content(soup),
                metadata=self._extract_metadata(soup),
                links=self._extract_links(soup, url),
                images=self._extract_images(soup, url),
                status_code=status_code,
                response_time=response_time,
                screenshot_path=output_path,
            )

        except Exception as e:  # noqa: BLE001
            return self._error_result(url, f"Playwright error: {e!s}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _bypass_cloudflare_if_needed(self) -> str:
        """Return page HTML, pausing to simulate human input if a CF challenge is detected."""
        raw_html = await self._page.content()

        if any(sig in raw_html.lower() for sig in _CF_SIGNATURES):
            # Simulate human mouse movements to trigger Turnstile token generation.
            for _ in range(5):
                x = random.randint(100, 700)  # noqa: S311 — non-crypto mouse coords
                y = random.randint(100, 500)  # noqa: S311
                await self._page.mouse.move(x, y)
                await asyncio.sleep(1.5)
            raw_html = await self._page.content()

        return raw_html

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
        # Remove unwanted elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        # Convert <pre>/<code> to markdown fences BEFORE get_text() strips them
        preserve_code_blocks(soup)

        main = soup.find("main") or soup.find("article") or soup.find("body")
        raw_text = (
            main.get_text(separator="\n", strip=True)
            if main
            else soup.get_text(separator="\n", strip=True)
        )

        # Normalize whitespace but protect code blocks
        import re as _re

        _fence_re = _re.compile(r"(```[\w]*\n.*?\n```)", _re.DOTALL)
        code_blocks: list[str] = []

        def _save(m: _re.Match) -> str:
            code_blocks.append(m.group(0))
            return f"\x02CB{len(code_blocks) - 1}\x02"

        text = _fence_re.sub(_save, raw_text)

        text = re.sub(r"\n\s*\n", "\n\n", text)
        text = re.sub(r"^\s+|\s+$", "", text, flags=re.MULTILINE)
        text = re.sub(r" {2,}", " ", text)

        # Restore code blocks
        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x02CB{i}\x02", block)

        return text[: config.scraper_max_raw_text_chars]

    def _extract_metadata(self, soup: BeautifulSoup) -> dict:
        """Extract meta tags and page metadata."""
        metadata: dict = {}

        for meta in soup.find_all("meta"):
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
        links: dict = {"internal": [], "external": []}
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

        return links

    def _extract_images(self, soup: BeautifulSoup, base_url: str) -> list:
        """Extract image URLs from the page."""
        images = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                images.append({"url": urljoin(base_url, src), "alt": img.get("alt", "")})
                if len(images) >= config.max_images:
                    break

        return images

    def _get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        return urlparse(url).netloc

    def _error_result(self, url: str, error: str) -> ScrapedData:
        """Create an error ScrapedData."""
        return ScrapedData(url=url, title="", content="", error=error, status_code=0)
