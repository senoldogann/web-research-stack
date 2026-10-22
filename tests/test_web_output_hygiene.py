"""Scrape output hygiene: no navigation dumps, a capped link list, chrome-free Markdown."""

from __future__ import annotations

import asyncio
import json

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.content import extract_page, html_to_markdown
from web_scraper.web_tools.fetcher import FetchError, FetchResult
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

BASE = "https://city.example/"


def _city_page(content_links: int) -> str:
    """A municipal front page: a mega menu, a footer, a sidebar, and some content."""
    menu = "".join(f'<li><a href="/menu/{index}">Menu item {index}</a></li>' for index in range(60))
    footer = "".join(f'<a href="/footer/{index}">Footer {index}</a>' for index in range(20))
    content = "".join(
        f'<p><a href="/news/{index}">News story {index}</a></p>' for index in range(content_links)
    )
    return (
        "<html><head><title>City</title></head><body>"
        f'<header><a href="/">Home</a></header><nav><ul>{menu}</ul></nav>'
        f'<div role="navigation"><a href="/aria/menu">ARIA menu</a></div>'
        f"<main><h1>Welcome</h1>{content}"
        '<aside><a href="/sidebar">Sidebar link</a></aside>'
        '<div role="complementary"><a href="/related">Related</a> related reading</div>'
        "</main>"
        f'<div role="contentinfo"><a href="/aria/footer">ARIA footer</a></div>'
        f"<footer>{footer}</footer></body></html>"
    )


class PageFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(url, url, 200, "text/html", self.pages[url], {}, 1.0, 2.0, 1)


def _service(pages: dict[str, str], **overrides: object) -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(respect_robots=False, **overrides),  # type: ignore[arg-type]
        fetcher=PageFetcher(pages),
        browser=None,
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


@pytest.mark.asyncio
async def test_scrape_lists_at_most_50_content_links_and_says_it_cut() -> None:
    result = await _service({BASE: _city_page(80)}).scrape(BASE)

    urls = [link["url"] for link in result["links"]]
    assert len(urls) == 50
    assert result["links_truncated"] is True
    assert urls[0] == "https://city.example/news/0"
    assert not any("/menu/" in url or "/footer/" in url or "/aria/" in url for url in urls)
    assert "https://city.example/sidebar" not in urls
    assert "https://city.example/related" not in urls


@pytest.mark.asyncio
async def test_page_without_chrome_or_overflow_is_not_truncated() -> None:
    html = '<html><body><main><a href="/a">A</a><a href="/b">B</a></main></body></html>'

    result = await _service({BASE: html}).scrape(BASE)

    assert [link["url"] for link in result["links"]] == [
        "https://city.example/a",
        "https://city.example/b",
    ]
    assert result["links_truncated"] is False


@pytest.mark.asyncio
async def test_leaving_out_menu_links_is_reported_even_under_the_cap() -> None:
    result = await _service({BASE: _city_page(3)}).scrape(BASE)

    assert len(result["links"]) == 3
    assert result["links_truncated"] is True


@pytest.mark.asyncio
async def test_scrape_link_cap_is_configurable() -> None:
    result = await _service({BASE: _city_page(80)}, scrape_max_links=5).scrape(BASE)

    assert len(result["links"]) == 5


@pytest.mark.asyncio
async def test_navigation_heavy_page_no_longer_dumps_its_menus() -> None:
    result = await _service({BASE: _city_page(10)}).scrape(BASE)

    links_bytes = len(json.dumps(result["links"], separators=(",", ":")).encode())
    response_bytes = len(json.dumps(result, separators=(",", ":")).encode())
    assert links_bytes < 1_000
    assert response_bytes < 3_000


def test_markdown_leaves_out_aria_navigation_banner_footer_and_sidebar() -> None:
    markdown = html_to_markdown(_city_page(2), BASE)

    assert "News story 1" in markdown
    assert "ARIA menu" not in markdown
    assert "ARIA footer" not in markdown
    assert "Related" not in markdown
    assert "Sidebar link" not in markdown
    assert "Menu item" not in markdown


def test_extracted_links_keep_chrome_links_for_map_and_crawl() -> None:
    page = extract_page(_city_page(2), BASE)

    by_url = {link.url: link for link in page.links}
    assert by_url["https://city.example/menu/0"].chrome is True
    assert by_url["https://city.example/aria/footer"].chrome is True
    assert by_url["https://city.example/news/1"].chrome is False


@pytest.mark.asyncio
async def test_map_still_lists_navigation_links() -> None:
    pages = {
        BASE: _city_page(2),
        BASE + "robots.txt": "",
    }

    class MapFetcher(PageFetcher):
        async def fetch(self, url: str) -> FetchResult:
            if url == BASE + "sitemap.xml":
                raise FetchError("http_error", "no sitemap", url=url, status_code=404)
            return await super().fetch(url)

    service = _service(pages)
    service.fetcher = MapFetcher(pages)

    result = await service.map(BASE, limit=500)

    assert "https://city.example/menu/0" in result["urls"]
    assert "https://city.example/news/1" in result["urls"]


@pytest.mark.asyncio
async def test_crawl_still_follows_pagination_inside_nav() -> None:
    first = (
        "<html><body><main><p>First page of quotes.</p></main>"
        '<nav><a href="/page/2">Next</a></nav></body></html>'
    )
    second = "<html><body><main><p>Second page of quotes.</p></main></body></html>"
    service = _service({BASE: first, BASE + "page/2": second}, crawl_concurrency=1)

    started = await service.start_crawl(BASE)
    for _ in range(200):
        snapshot = await service.get_crawl(started["job_id"])
        if snapshot["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    results = await service.crawl_results(started["job_id"])

    assert [item["url"] for item in results["results"]] == [BASE, BASE + "page/2"]


def test_settings_default_and_env_for_the_link_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert WebToolsSettings().scrape_max_links == 50
    assert WebToolsSettings(scrape_max_links=900, max_links=500).scrape_max_links == 500

    monkeypatch.setenv("WEB_TOOLS_SCRAPE_MAX_LINKS", "20")

    assert WebToolsSettings.from_env().scrape_max_links == 20
