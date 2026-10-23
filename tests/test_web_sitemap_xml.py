"""A sitemap or feed is read as XML, not as HTML, when it is checked for a challenge."""

from __future__ import annotations

import warnings
from pathlib import Path

import httpx
import pytest
from bs4 import XMLParsedAsHTMLWarning

from web_scraper.web_tools.content import detect_interstitial
from web_scraper.web_tools.fetcher import SafeFetcher

URL = "https://example.com/sitemap.xml"
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/a</loc></url>
</urlset>"""


def _allow(url: str, allow_private_networks: bool = False) -> str:
    return url


def test_a_sitemap_is_read_as_xml_without_a_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", XMLParsedAsHTMLWarning)
        assert detect_interstitial(SITEMAP, URL, 200, content_type="application/xml") is None


@pytest.mark.asyncio
async def test_fetching_a_sitemap_raises_no_xml_warning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/xml"}, text=SITEMAP)

    with warnings.catch_warnings():
        warnings.simplefilter("error", XMLParsedAsHTMLWarning)
        async with SafeFetcher(
            transport=httpx.MockTransport(handler), validator=_allow, retry_attempts=0
        ) as fetcher:
            result = await fetcher.fetch(URL)

    assert result.html == SITEMAP


def test_an_external_entity_in_xml_is_not_read(tmp_path: Path) -> None:
    phrase = "verify you are human"
    secret = tmp_path / "secret.txt"
    secret.write_text(phrase, encoding="utf-8")
    hostile = (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE urlset [<!ENTITY leak SYSTEM "file://{secret}">]>'
        "<urlset><url><loc>&leak;</loc></url></urlset>"
    )
    inline = f'<?xml version="1.0"?><urlset><url><loc>{phrase}</loc></url></urlset>'

    # Read into the document, the file's words would make it a captcha page.
    assert detect_interstitial(inline, URL, 200, content_type="application/xml") == "captcha"
    assert detect_interstitial(hostile, URL, 200, content_type="application/xml") is None
