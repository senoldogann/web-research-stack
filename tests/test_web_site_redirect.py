"""A site whose address redirects is the site at the host it lands on.

``web_map("https://senoldogan.dev")`` returned one URL: the apex answers every path with a 308 to
``www.senoldogan.dev``, and the sitemap it lead to listed ``www`` URLs, which were dropped as another host's. The
same address given as ``https://www.senoldogan.dev`` gave 42. Now the host an address lands on is the site's own:
its sitemaps are read there and its URLs kept, the answer names the host (``host``, and ``redirected_from`` when the
address moved), and a crawl of the apex follows the pages of ``www`` instead of ending at the first.

The sites are in memory (``tests/rebinding_support.py``) behind the real fetcher, which follows the redirects.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
from rebinding_support import PUBLIC_A, PUBLIC_B, PUBLIC_C, Response, World, html
from rest_support import HEADERS, rest_client

from web_scraper.web_tools import urls
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

HOME = (
    "<html><head><title>Senol Dogan</title><link rel='canonical' href='https://www.apex.test/'></head>"
    "<body><main><p>{words}</p><a href='/about'>About</a><a href='/blog/en'>Blog</a>"
    "<a href='https://other.example/out'>Out</a></main></body></html>"
)
WORDS = "An independent software studio from Oulu, Finland. " * 4


def _sitemap(*urls: tuple[str, Optional[str]]) -> str:
    rows = "".join(
        f"<url><loc>{url}</loc>" + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "") + "</url>"
        for url, lastmod in urls
    )
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'


def _redirect(to: str, status: int = 308) -> Response:
    return Response(status, {"Location": to}, "")


def _xml(body: str) -> Response:
    return Response(200, {"Content-Type": "application/xml"}, body)


def _service(**settings: Any) -> WebToolsService:
    base: dict[str, Any] = {
        "browser_enabled": False,
        "respect_robots": False,
        "crawl_concurrency": 1,
    }
    base.update(settings)
    return WebToolsService(
        settings=WebToolsSettings(**base),
        fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
        browser=None,
        search_adapters={},
    )


@pytest.fixture
def apex(world: World) -> dict[str, list[str]]:
    """``apex.test`` redirects every path to ``www.apex.test``, which has the sitemap, the robots.txt and the pages."""
    asked: dict[str, list[str]] = {"apex": [], "www": []}
    world.dns.answers("apex.test", [PUBLIC_A])
    world.dns.answers("www.apex.test", [PUBLIC_B])
    world.dns.answers("blog.apex.test", [PUBLIC_C])

    def at_apex(request: Any) -> Response:
        asked["apex"].append(request.target)
        return _redirect(f"https://www.apex.test{request.target}")

    pages = {
        "/": html(HOME.format(words=WORDS)),
        "/about": html(HOME.format(words="About the studio. " * 8)),
        "/blog/en/post-1": html(HOME.format(words="The first post, about websites. " * 8)),
        "/robots.txt": Response(
            200,
            {"Content-Type": "text/plain"},
            "User-agent: *\nSitemap: https://www.apex.test/sitemap.xml\n",
        ),
        "/sitemap.xml": _xml(
            _sitemap(
                ("https://www.apex.test/", "2026-10-01"),
                ("https://www.apex.test/about", "2026-09-15"),
                ("https://www.apex.test/blog/en/post-1", "2026-10-20"),
                ("https://blog.apex.test/sibling", "2026-10-21"),
                ("https://other.example/elsewhere", None),
            )
        ),
    }

    def at_www(request: Any) -> Response:
        asked["www"].append(request.target)
        return pages.get(request.target) or html("not found", 404)

    world.internet.serve(PUBLIC_A, at_apex)
    world.internet.serve(PUBLIC_B, at_www)
    return asked


class TestMap:
    @pytest.mark.asyncio
    async def test_the_apex_that_redirects_to_www_is_mapped_at_www(self, apex: Any) -> None:
        service = _service()

        try:
            answer = await service.map("https://apex.test")
        finally:
            await service.close()

        # Before: one URL, the apex itself, with every www URL dropped as another host's.
        assert answer["urls"] == [
            "https://www.apex.test/",
            "https://www.apex.test/about",
            "https://www.apex.test/blog/en/post-1",
            "https://www.apex.test/blog/en",
        ]
        assert answer["host"] == "www.apex.test" and answer["url"] == "https://www.apex.test/"
        assert answer["redirected_from"] == "https://apex.test/"
        assert answer["sitemaps"] == ["https://www.apex.test/sitemap.xml"]
        assert answer["errors"] == [] and answer["truncated"] is False
        # What the owner praised stays: the sitemap's lastmod.
        lastmods = {entry["url"]: entry["lastmod"] for entry in answer["entries"]}
        assert lastmods["https://www.apex.test/"] == "2026-10-01"
        assert lastmods["https://www.apex.test/blog/en/post-1"] == "2026-10-20"
        assert {entry["source"] for entry in answer["entries"]} == {"sitemap", "links"}

    @pytest.mark.asyncio
    async def test_the_address_that_is_the_site_already_is_mapped_as_it_was(
        self, apex: Any
    ) -> None:
        service = _service()

        try:
            answer = await service.map("https://www.apex.test")
        finally:
            await service.close()

        assert answer["host"] == "www.apex.test" and "redirected_from" not in answer
        assert answer["total"] == 4 and apex["apex"] == []  # the apex was never asked

    @pytest.mark.asyncio
    async def test_a_sibling_subdomain_is_kept_only_when_subdomains_are_asked_for(
        self, apex: Any
    ) -> None:
        service = _service()

        try:
            without = await service.map("https://apex.test")
            with_them = await service.map("https://apex.test", include_subdomains=True)
        finally:
            await service.close()

        assert "https://blog.apex.test/sibling" not in without["urls"]
        # The site's siblings are the apex's subdomains, not the www host's.
        assert "https://blog.apex.test/sibling" in with_them["urls"]
        assert "https://other.example/elsewhere" not in with_them["urls"]

    @pytest.mark.asyncio
    async def test_http_that_redirects_to_https_is_the_same_site_and_says_where_it_went(
        self, world: World
    ) -> None:
        world.dns.answers("plain.test", [PUBLIC_A])

        def handler(request: Any) -> Response:
            if request.target == "/sitemap.xml":
                return _xml(_sitemap(("https://plain.test/docs", None)))
            return html(HOME.format(words=WORDS).replace("www.apex.test", "plain.test"))

        world.internet.serve(PUBLIC_A, handler)
        service = _service()

        try:
            answer = await service.map("http://plain.test")
        finally:
            await service.close()

        assert "https://plain.test/docs" in answer["urls"] and answer["host"] == "plain.test"
        assert answer["url"] == "http://plain.test/"  # this server answers on http as it is asked

    @pytest.mark.asyncio
    async def test_a_site_whose_page_fails_but_whose_sitemap_answers_at_the_new_host_is_mapped_there(
        self, world: World, apex: Any
    ) -> None:
        # www's home page is down; its robots.txt and sitemap answer.
        original = world.internet.handlers[PUBLIC_B]

        def handler(request: Any) -> Response:
            if request.target == "/":
                return html("down", 500)
            return original(request)

        world.internet.serve(PUBLIC_B, handler)
        service = _service()

        try:
            answer = await service.map("https://apex.test")
        finally:
            await service.close()

        assert answer["host"] == "www.apex.test"
        assert "https://www.apex.test/about" in answer["urls"]
        assert [error["status_code"] for error in answer["errors"]] == [500]
        assert answer["errors"][0]["url"] == "https://www.apex.test/"  # where the 500 came from

    @pytest.mark.asyncio
    async def test_the_rest_route_keeps_the_host_and_where_it_came_from(
        self, apex: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = rest_client(monkeypatch, _service())

        response = client.post(
            "/api/v1/web/map", headers=HEADERS, json={"url": "apex.test", "limit": 10}
        )

        body = response.json()
        assert response.status_code == 200
        assert body["host"] == "www.apex.test" and body["redirected_from"] == "https://apex.test/"
        assert "https://www.apex.test/about" in body["urls"]


class TestCrawl:
    @pytest.mark.asyncio
    async def test_a_crawl_of_the_apex_follows_the_pages_of_www(self, apex: Any) -> None:
        service = _service()

        try:
            started = await service.start_crawl(
                "https://apex.test", {"max_pages": 10, "max_depth": 2}
            )
            for _ in range(200):
                snapshot = await service.get_crawl(started["job_id"])
                if snapshot["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            results = await service.crawl_results(started["job_id"])
        finally:
            await service.close()

        # Before: the start page and nothing else (its links were www's, the crawl's scope was the apex's).
        urls = [page["url"] for page in results["results"]]
        assert urls[0] == "https://www.apex.test/"
        assert "https://www.apex.test/about" in urls
        assert snapshot["status"] == "completed"
        # The start page is not read twice, as a link to itself, and nothing off the site is.
        assert urls.count("https://www.apex.test/") == 1
        assert not any("other.example" in url for url in urls)

    @pytest.mark.asyncio
    async def test_a_crawl_seeded_from_the_sitemap_reads_the_sitemap_at_the_host_it_lands_on(
        self, apex: Any
    ) -> None:
        service = _service()

        try:
            started = await service.start_crawl(
                "https://apex.test", {"max_pages": 10, "max_depth": 0, "use_sitemap": True}
            )
            for _ in range(200):
                snapshot = await service.get_crawl(started["job_id"])
                if snapshot["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            results = await service.crawl_results(started["job_id"])
        finally:
            await service.close()

        urls = [page["url"] for page in results["results"]]
        assert snapshot["progress"]["seeded"] >= 2
        assert "https://www.apex.test/blog/en/post-1" in urls  # a page only the sitemap lists

    def test_the_scope_of_a_site_that_moved(self) -> None:
        assert urls.site_scopes("https://a.test/", "https://a.test/", False) == ["https://a.test/"]
        assert urls.site_scopes("https://a.test/", "https://www.a.test/", False) == [
            "https://a.test/",
            "https://www.a.test/",
        ]
        # With subdomains the apex is already one of them: the www host adds nothing it lacked.
        assert urls.site_scopes("https://a.test/", "https://www.a.test/", True) == [
            "https://a.test/",
            "https://www.a.test/",
        ]
        assert urls.site_scopes("https://www.a.test/x", "https://www.a.test/x", True) == [
            "https://www.a.test/x",
            "https://a.test/",
        ]
