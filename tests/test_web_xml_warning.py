"""An XML document is read with the XML parser: the HTML one warns (XMLParsedAsHTMLWarning) on it."""

from __future__ import annotations

import warnings

import pytest
from bs4 import XMLParsedAsHTMLWarning
from test_web_service import FakeFetcher, _service

from web_scraper.web_tools.content import detect_interstitial, extract_page, extract_structured
from web_scraper.web_tools.fetcher import FetchResult

URL = "https://news.example/feed"
SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://news.example/a</loc></url></urlset>"
)
RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>'
    "<item><title>One</title><link>https://news.example/1</link></item></channel></rss>"
)
ATOM = (
    '<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
    "<title>Atom feed</title><entry><title>First</title></entry></feed>"
)
XHTML = (
    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Page</title>'
    "</head><body><main><p>Some words of an XHTML page.</p></main></body></html>"
)


def _warned(caught: list[warnings.WarningMessage]) -> list[str]:
    return [
        str(item.message)[:60]
        for item in caught
        if issubclass(item.category, XMLParsedAsHTMLWarning)
    ]


@pytest.mark.parametrize("document", [SITEMAP, RSS, ATOM], ids=["sitemap", "rss", "atom"])
def test_an_xml_document_raises_no_warning_on_any_path(document: str) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # No content type, or one that calls it HTML: a server may send either.
        detect_interstitial(document, URL, 200)
        detect_interstitial(document, URL, 200, content_type="text/html")
        detect_interstitial(document, URL, 200, content_type="application/xml")
        extract_page(document, URL)
        extract_structured(document, URL, {"title": "title"})

    assert _warned(caught) == []


def test_an_xml_document_is_still_read() -> None:
    page = extract_page(RSS, URL)
    result = extract_structured(RSS, URL, {"first": "item > title", "name": "channel > title"})

    assert page.title == "Feed"
    assert result.data == {"first": "One", "name": "Feed"}


def test_an_xhtml_page_is_still_a_web_page() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        page = extract_page(XHTML, "https://news.example/page")

    assert _warned(caught) == []
    assert page.title == "Page"
    assert "Some words of an XHTML page." in page.markdown


@pytest.mark.asyncio
async def test_scraping_a_feed_raises_no_warning() -> None:
    fetched = FetchResult(URL, URL, 200, "application/rss+xml", RSS)
    service = _service(FakeFetcher({URL: fetched}))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await service.scrape(URL)

    assert _warned(caught) == []
    assert result["title"] == "Feed"
