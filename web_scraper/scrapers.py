"""Core scraping logic for web content extraction."""

import io
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from web_scraper.config import config
from web_scraper.content_safety import preserve_code_blocks
from web_scraper.network_safety import UnsafeTargetError, validate_outbound_url
from web_scraper.stealth import HeaderFactory


@dataclass
class ScrapedData:
    """Structured data extracted from a web page."""

    url: str
    title: str
    content: str
    metadata: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    images: list = field(default_factory=list)
    status_code: int = 200
    response_time: float = 0.0
    error: Optional[str] = None
    screenshot_path: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "metadata": self.metadata,
            "links": self.links,
            "images": self.images,
            "status_code": self.status_code,
            "response_time": self.response_time,
            "error": self.error,
        }


class WebScraper:
    """Main web scraper class for extracting content from URLs."""

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
        Initialize the web scraper.

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
        self._client: Optional[requests.Session] = None

    def __enter__(self) -> "WebScraper":
        """Context manager entry - create HTTP client."""
        referer = HeaderFactory.get_referer()
        headers = HeaderFactory.get_headers(referer=referer)
        self._client = requests.Session(
            timeout=self.timeout,
            allow_redirects=False,
            headers=headers,
            impersonate=HeaderFactory.get_impersonate_target(),
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - close HTTP client."""
        if self._client:
            self._client.close()

    def scrape(self, url: str) -> ScrapedData:
        """
        Scrape content from a URL.

        Args:
            url: The URL to scrape.

        Returns:
            ScrapedData object with extracted content.
        """
        import time

        start_time = time.time()

        try:
            response = self._get_page(url)

            # Check for Cloudflare block more aggressively
            html_lower = response.text.lower()
            is_cloudflare = response.status_code in (403, 503) and (
                "just a moment..." in html_lower
                or "cf-browser-verification" in html_lower
                or "cloudflare-challenge" in html_lower
                or "challenge-error-text" in html_lower
                or "enable javascript and cookies to continue" in html_lower
            )

            if is_cloudflare:
                response = self._run_flaresolverr(url)

            response_time = time.time() - start_time

            # Check if PDF
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/pdf" in content_type or url.lower().endswith(".pdf"):
                return self._extract_pdf(response, url, start_time)

            soup = BeautifulSoup(response.text, "lxml")

            title = self._extract_title(soup)
            content = self._extract_content(soup, clean=True)
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

        except UnsafeTargetError as e:
            return self._error_result(url, str(e))
        except requests.errors.RequestsError as e:
            return self._error_result(url, f"HTTP error: {str(e)}")
        except Exception as e:
            return self._error_result(url, f"Unexpected error: {str(e)}")

    def _run_flaresolverr(self, url: str):
        """Fallback to FlareSolverr if Cloudflare Turnstile blocks the native scraper."""
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
                    self._client = requests.Session(timeout=120)
                res = self._client.post(fs_url, json=payload, timeout=120)
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

    def _get_page(self, url: str) -> requests.Response:
        """Fetch the page content."""
        if not self._client:
            headers = HeaderFactory.get_headers(url)
            self._client = requests.Session(
                timeout=self.timeout,
                allow_redirects=False,
                headers=headers,
                impersonate="chrome120",
            )
        return self._request_with_safe_redirects(url)

    def _request_with_safe_redirects(self, url: str) -> requests.Response:
        """Fetch a URL while validating every redirect hop."""
        current_url = validate_outbound_url(
            url,
            allow_private_networks=self.allow_private_networks,
        )
        redirect_count = 0

        while True:
            response = self._client.get(current_url)
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

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract page title."""
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title.get("content")

        return "No title found"

    def _extract_content(self, soup: BeautifulSoup, clean: bool = True) -> str:
        """
        Extract main content text from the page, preserving code blocks.

        Args:
            soup: BeautifulSoup object.
            clean: Whether to remove HTML tags and clean content.

        Returns:
            Cleaned text content with code blocks in markdown fences.
        """
        # Remove unwanted elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        # Convert <pre>/<code> to markdown fences BEFORE get_text() strips them
        preserve_code_blocks(soup)

        # Find main content area
        main = soup.find("main") or soup.find("article") or soup.find("body")
        if main:
            text = main.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        # Clean up whitespace and normalize (but protect code blocks)
        # Extract code blocks first
        code_blocks: list[str] = []
        import re as _re

        _fence_re = _re.compile(r"(```[\w]*\n.*?\n```)", _re.DOTALL)

        def _save(m: _re.Match) -> str:
            code_blocks.append(m.group(0))
            return f"\x02CB{len(code_blocks) - 1}\x02"

        text = _fence_re.sub(_save, text)

        text = re.sub(r"\n\s*\n", "\n\n", text)
        text = re.sub(r"^\s+|\s+$", "", text, flags=re.MULTILINE)
        text = re.sub(r" {2,}", " ", text)

        # Restore code blocks
        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x02CB{i}\x02", block)

        # Limit content length
        if len(text) > config.max_source_content_chars:
            text = text[: config.max_source_content_chars]

        return text

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

    def _error_result(self, url: str, error: str) -> ScrapedData:
        """Create an error result."""
        return ScrapedData(
            url=url,
            title="",
            content="",
            error=error,
            status_code=0,
        )
