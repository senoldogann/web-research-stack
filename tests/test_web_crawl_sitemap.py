"""A crawl seeded from the site's sitemaps: use_sitemap reaches pages no link reaches."""

from __future__ import annotations

import pytest
from test_web_crawl import MapFetcher, _page, _service, _wait_for_status

from web_scraper.api_models import WebCrawlOptions
from web_scraper.mcp_server import _crawl_start
from web_scraper.web_tools.crawl import CrawlOptions
from web_scraper.web_tools.fetcher import FetchResult
from web_scraper.web_tools.settings import WebToolsSettings

BASE = "https://example.com/"
SITEMAP = BASE + "sitemap.xml"


def _text(url: str, body: str, content_type: str = "text/plain") -> FetchResult:
    return FetchResult(url, url, 200, content_type, body, {}, 1, 1, 1)


def _urlset(*urls: str) -> str:
    entries = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    )


def _site(sitemap_urls: list[str], **extra: FetchResult) -> dict[str, FetchResult]:
    """A site whose home links to /a only; the sitemap lists pages no link reaches."""
    pages = {
        BASE + "robots.txt": _text(
            BASE + "robots.txt", f"User-agent: *\nDisallow: /private\nSitemap: {SITEMAP}\n"
        ),
        SITEMAP: _text(SITEMAP, _urlset(*sitemap_urls), "application/xml"),
        BASE: _page(BASE, "home", ["/a"]),
        BASE + "a": _page(BASE + "a", "page a", []),
        BASE + "hidden1": _page(BASE + "hidden1", "hidden one", ["/deep"]),
        BASE + "hidden2": _page(BASE + "hidden2", "hidden two", []),
        BASE + "deep": _page(BASE + "deep", "deep, linked from a hidden page", []),
        BASE + "private/x": _page(BASE + "private/x", "private", []),
        "https://other.com/z": _page("https://other.com/z", "elsewhere", []),
        "https://blog.example.com/b": _page("https://blog.example.com/b", "a subdomain", []),
    }
    pages.update(extra)
    return pages


LISTED = [
    BASE + "hidden1",
    BASE + "hidden2",
    BASE + "a",
    BASE + "private/x",
    "https://other.com/z",
    "https://blog.example.com/b",
]


async def _crawl(fetcher: MapFetcher, options: dict | None = None, **settings) -> dict:
    service = _service(fetcher, max_crawl_pages=50, max_crawl_depth=3, **settings)
    started = await service.start_crawl(BASE, options)
    snapshot = await _wait_for_status(service, started["job_id"], {"completed", "failed"})
    results = await service.crawl_results(started["job_id"], limit=100)
    return {"snapshot": snapshot, "urls": [item["url"] for item in results["results"]]}


@pytest.mark.asyncio
async def test_without_use_sitemap_the_sitemap_is_not_read_and_hidden_pages_are_missed() -> None:
    fetcher = MapFetcher(_site(LISTED))

    crawl = await _crawl(fetcher, {"max_pages": 20})

    assert sorted(crawl["urls"]) == [BASE, BASE + "a"]
    assert SITEMAP not in fetcher.calls
    assert crawl["snapshot"]["options"]["use_sitemap"] is False
    assert crawl["snapshot"]["progress"]["seeded"] == 0


@pytest.mark.asyncio
async def test_use_sitemap_reaches_pages_no_link_reaches() -> None:
    fetcher = MapFetcher(_site(LISTED))

    crawl = await _crawl(fetcher, {"max_pages": 20, "use_sitemap": True})

    assert BASE + "hidden1" in crawl["urls"] and BASE + "hidden2" in crawl["urls"]
    assert crawl["snapshot"]["status"] == "completed"
    assert crawl["snapshot"]["options"]["use_sitemap"] is True


@pytest.mark.asyncio
async def test_seeds_stay_in_scope_and_behind_robots_and_the_filters() -> None:
    fetcher = MapFetcher(_site(LISTED))

    crawl = await _crawl(fetcher, {"max_pages": 20, "use_sitemap": True})

    assert "https://other.com/z" not in fetcher.calls  # another host
    assert "https://blog.example.com/b" not in fetcher.calls  # a subdomain, not asked for
    assert BASE + "private/x" not in fetcher.calls  # robots.txt says no
    assert crawl["snapshot"]["progress"]["skipped"] >= 1

    filtered = await _crawl(
        MapFetcher(_site(LISTED)),
        {"max_pages": 20, "use_sitemap": True, "exclude_patterns": ["*/hidden2"]},
    )
    assert BASE + "hidden2" not in filtered["urls"] and BASE + "hidden1" in filtered["urls"]

    only = await _crawl(
        MapFetcher(_site(LISTED)),
        {"max_pages": 20, "use_sitemap": True, "include_patterns": ["*/hidden*"]},
    )
    assert BASE + "a" not in only["urls"]  # the seed a is filtered out; the home is the start page
    assert BASE + "hidden1" in only["urls"]


@pytest.mark.asyncio
async def test_subdomains_are_seeds_when_asked_for() -> None:
    fetcher = MapFetcher(_site(LISTED))

    crawl = await _crawl(
        fetcher, {"max_pages": 20, "use_sitemap": True, "include_subdomains": True}
    )

    assert "https://blog.example.com/b" in crawl["urls"]
    assert "https://other.com/z" not in fetcher.calls


@pytest.mark.asyncio
async def test_seeds_count_against_max_pages() -> None:
    fetcher = MapFetcher(_site(LISTED))

    crawl = await _crawl(fetcher, {"max_pages": 3, "use_sitemap": True})

    assert crawl["snapshot"]["progress"]["pages_attempted"] == 3
    assert crawl["urls"] == [BASE, BASE + "hidden1", BASE + "hidden2"]  # the sitemap's order
    assert BASE + "a" not in crawl["urls"]  # the home's own link came after the seeds
    assert crawl["snapshot"]["progress"]["seeded"] <= 3


@pytest.mark.asyncio
async def test_link_following_goes_on_from_the_seeds() -> None:
    fetcher = MapFetcher(_site(LISTED))

    crawl = await _crawl(fetcher, {"max_pages": 20, "max_depth": 2, "use_sitemap": True})

    assert BASE + "deep" in crawl["urls"]  # linked only from a page the sitemap listed
    assert BASE + "a" in crawl["urls"]  # linked from the home and listed: crawled once
    assert crawl["urls"].count(BASE + "a") == 1


@pytest.mark.asyncio
async def test_a_sitemap_index_is_followed_as_web_map_follows_it() -> None:
    child = BASE + "sitemap-pages.xml"
    index = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{child}</loc></sitemap></sitemapindex>"
    )
    pages = _site([], **{child: _text(child, _urlset(BASE + "hidden1"), "application/xml")})
    pages[SITEMAP] = _text(SITEMAP, index, "application/xml")

    crawl = await _crawl(MapFetcher(pages), {"max_pages": 20, "use_sitemap": True})

    assert BASE + "hidden1" in crawl["urls"]


@pytest.mark.asyncio
async def test_a_sitemap_that_fails_does_not_fail_the_crawl() -> None:
    pages = _site(LISTED)
    del pages[SITEMAP]  # robots.txt names it, the server says 404

    crawl = await _crawl(MapFetcher(pages), {"max_pages": 20, "use_sitemap": True})

    assert crawl["snapshot"]["status"] == "completed"
    assert sorted(crawl["urls"]) == [BASE, BASE + "a"]
    assert any(error["url"] == SITEMAP for error in crawl["snapshot"]["errors"])


def test_the_option_is_part_of_the_models() -> None:
    assert WebCrawlOptions().use_sitemap is None
    assert WebCrawlOptions(use_sitemap=True).model_dump(exclude_none=True) == {
        "use_sitemap": True,
        "include_subdomains": False,
        "include_patterns": [],
        "exclude_patterns": [],
    }
    options = CrawlOptions.from_dict({"use_sitemap": True}, WebToolsSettings())
    assert options.use_sitemap is True and options.to_dict()["use_sitemap"] is True
    assert CrawlOptions.from_dict(None, WebToolsSettings()).use_sitemap is False


@pytest.mark.asyncio
async def test_the_mcp_tool_takes_use_sitemap() -> None:
    service = _service(MapFetcher(_site(LISTED)), max_crawl_pages=50)

    started = await _crawl_start(
        service, {"url": BASE, "options": {"max_pages": 10, "use_sitemap": True}}
    )

    assert started["options"]["use_sitemap"] is True
