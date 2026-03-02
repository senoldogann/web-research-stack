"""Tests for the web scraper package."""

import asyncio

import pytest

from web_scraper.async_scrapers import WebScraperAsync
from web_scraper.scrapers import ScrapedData, WebScraper


class TestScrapedData:
    """Tests for ScrapedData class."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        data = ScrapedData(
            url="https://example.com",
            title="Test Title",
            content="Test content",
            metadata={"key": "value"},
            links={"internal": [], "external": []},
            images=[],
            status_code=200,
            response_time=0.5,
        )

        result = data.to_dict()

        assert result["url"] == "https://example.com"
        assert result["title"] == "Test Title"
        assert result["content"] == "Test content"
        assert result["metadata"] == {"key": "value"}
        assert result["status_code"] == 200
        assert result["response_time"] == 0.5

    def test_to_dict_with_error(self):
        """Test conversion with error field."""
        data = ScrapedData(
            url="https://example.com",
            title="",
            content="",
            error="Test error",
            status_code=0,
        )

        result = data.to_dict()

        assert result["error"] == "Test error"
        assert result["status_code"] == 0


class TestWebScraper:
    """Tests for WebScraper class."""

    def test_init(self):
        """Test scraper initialization."""
        scraper = WebScraper(timeout=10.0, max_links=50)

        assert scraper.timeout == 10.0
        assert scraper.max_links == 50
        assert scraper.follow_redirects is True

    def test_init_custom_user_agent(self):
        """Test scraper with custom user agent."""
        custom_ua = "CustomBot/1.0"
        scraper = WebScraper(user_agent=custom_ua)

        assert scraper.user_agent == custom_ua

    def test_init_default_values(self):
        """Test scraper with default values."""
        scraper = WebScraper()

        assert scraper.timeout == 30.0
        assert scraper.max_links == 100
        assert scraper.follow_redirects is True
        assert scraper.user_agent is not None

    def test_error_result(self):
        """Test error result creation."""
        scraper = WebScraper()
        result = scraper._error_result("https://example.com", "Test error")

        assert result.url == "https://example.com"
        assert result.error == "Test error"
        assert result.title == ""
        assert result.content == ""
        assert result.status_code == 0

    def test_get_domain(self):
        """Test domain extraction from URL."""
        scraper = WebScraper()

        assert scraper._get_domain("https://example.com/path") == "example.com"
        assert scraper._get_domain("http://sub.example.com") == "sub.example.com"
        assert scraper._get_domain("https://example.com:8080") == "example.com:8080"

    def test_scrape_blocks_unsafe_redirect(self, monkeypatch):
        """Test that redirects into loopback hosts are rejected."""
        scraper = WebScraper()

        class DummyResponse:
            def __init__(self, url):
                self.status_code = 302
                self.headers = {"location": "http://127.0.0.1/private"}
                self.url = url

        class DummyClient:
            def get(self, url):
                return DummyResponse(url)

        scraper._client = DummyClient()
        monkeypatch.setattr(
            "web_scraper.scrapers.validate_outbound_url",
            lambda url, allow_private_networks=False: (
                url
                if "127.0.0.1" not in url
                else (_ for _ in ()).throw(ValueError("Blocked unsafe target"))
            ),
        )

        result = scraper.scrape("https://example.com/start")

        assert result.error is not None
        assert "unsafe" in result.error.lower()

    def test_async_scrape_rejects_private_target(self):
        """Test async scraper rejects private destinations before fetching."""

        async def run_test():
            async with WebScraperAsync() as scraper:
                return await scraper.scrape("http://127.0.0.1/health")

        result = asyncio.run(run_test())

        assert result.error is not None
        assert "unsafe" in result.error.lower()


class TestContentExtraction:
    """Tests for content extraction methods."""

    def test_extract_title_from_empty_soup(self):
        """Test title extraction with no title tag."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        scraper = WebScraper()

        title = scraper._extract_title(soup)

        assert title == "No title found"

    def test_extract_title_from_title_tag(self):
        """Test title extraction from title tag."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><head><title>My Title</title></head></html>", "lxml")
        scraper = WebScraper()

        title = scraper._extract_title(soup)

        assert title == "My Title"

    def test_extract_title_from_og(self):
        """Test title extraction from Open Graph tag."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<html><head><meta property="og:title" content="OG Title"></head></html>', "lxml"
        )
        scraper = WebScraper()

        title = scraper._extract_title(soup)

        assert title == "OG Title"

    def test_extract_metadata(self):
        """Test metadata extraction."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            """
            <html>
                <head>
                    <meta name="description" content="Desc">
                    <meta name="keywords" content="test, demo">
                    <meta name="author" content="Author">
                    <link rel="canonical" href="https://example.com/canonical">
                </head>
            </html>
            """,
            "lxml",
        )
        scraper = WebScraper()

        metadata = scraper._extract_metadata(soup)

        assert metadata.get("description") == "Desc"
        assert metadata.get("keywords") == "test, demo"
        assert metadata.get("author") == "Author"
        assert "canonical" in metadata.get("canonical_url", "")

    def test_extract_links(self):
        """Test link extraction."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            """
            <html>
                <body>
                    <a href="/internal">Internal</a>
                    <a href="https://other.com">External</a>
                </body>
            </html>
            """,
            "lxml",
        )
        scraper = WebScraper()

        links = scraper._extract_links(soup, "https://example.com")

        assert "internal" in links
        assert "external" in links
        assert len(links["internal"]) >= 1
        assert len(links["external"]) >= 1

    def test_extract_images(self):
        """Test image extraction."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            """
            <html>
                <body>
                    <img src="image1.jpg" alt="Image 1">
                    <img data-src="image2.jpg" alt="Image 2">
                </body>
            </html>
            """,
            "lxml",
        )
        scraper = WebScraper()

        images = scraper._extract_images(soup, "https://example.com")

        assert len(images) >= 2
        assert images[0]["alt"] == "Image 1"

    def test_extract_content(self):
        """Test content extraction."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            """
            <html>
                <body>
                    <h1>Hello World</h1>
                    <p>Test content</p>
                </body>
            </html>
            """,
            "lxml",
        )
        scraper = WebScraper()

        content = scraper._extract_content(soup)

        assert "Hello World" in content
        assert "Test content" in content

    def test_extract_content_removes_scripts(self):
        """Test that script tags are removed from content."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            """
            <html>
                <body>
                    <script>alert('test');</script>
                    <p>Real content</p>
                </body>
            </html>
            """,
            "lxml",
        )
        scraper = WebScraper()

        content = scraper._extract_content(soup)

        assert "alert" not in content
        assert "Real content" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
