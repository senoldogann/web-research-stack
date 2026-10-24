"""A redirect does not decide which site a map or a crawl reads.

1.4.0 took whatever host the start page, ``robots.txt`` or a sitemap landed on as the site: an address that forwards
every path to another site made ``web_map`` read that site's robots.txt and sitemaps and return its addresses, a crawl
read its pages, and with ``include_subdomains`` even a host nothing had redirected to. A page that stayed put while only
its robots.txt or sitemap forwarded elsewhere did the same, and so did a start page that was not there.

A landing is the site only for a ``www.`` flip, ``http`` to ``https`` or the same registrable domain. Any other is not:
the answer is the one page, as on 1.3.1, and says ``redirected_to``.

The sites are in memory (``tests/rebinding_support.py``) behind the real fetcher, which follows the redirects.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from rebinding_support import (
    PUBLIC_A,
    PUBLIC_B,
    PUBLIC_C,
    PUBLIC_D,
    PUBLIC_E,
    Response,
    World,
    html,
)
from rest_support import HEADERS, rest_client

from web_scraper.web_tools import urls
from web_scraper.web_tools.crawl import CrawlJob, CrawlOptions
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings
from web_scraper.web_tools.sources import registrable_domain

WORDS = "The home page of a site, with enough words to be a page and not a shell. " * 4


def _page(title: str, *links: str) -> Response:
    anchors = "".join(f'<a href="{href}">link {n}</a> ' for n, href in enumerate(links))
    return html(
        f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{WORDS}</p>"
        f"<p>{anchors}</p></main></body></html>"
    )


def _text(body: str) -> Response:
    return Response(200, {"Content-Type": "text/plain"}, body)


def _xml(body: str) -> Response:
    return Response(200, {"Content-Type": "application/xml"}, '<?xml version="1.0"?>' + body)


def _urlset(*locs: str) -> Response:
    rows = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return _xml(f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>')


def _index(*locs: str) -> Response:
    rows = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return _xml(
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</sitemapindex>'
    )


def _forward(to: str) -> Any:
    """A site that answers every path with a 301 to the same path of another host."""
    return lambda request: Response(301, {"Location": to + request.target}, "")


def _service(**settings: Any) -> WebToolsService:
    base: dict[str, Any] = {
        "browser_enabled": False,
        "soft_404_check": False,
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


def _asked(world: World) -> list[str]:
    """Every request the fake Internet served, as host and path."""
    return [f"{request.headers.get('host')}{request.target}" for request in world.internet.requests]


def _hosts_asked(world: World) -> set[str]:
    return {asked.split("/", 1)[0] for asked in _asked(world)}


async def _crawl(service: WebToolsService, url: str, **options: Any) -> tuple[dict, dict]:
    started = await service.start_crawl(url, {"max_pages": 10, "max_depth": 2, **options})
    for _ in range(300):
        state = await service.get_crawl(started["job_id"])
        if state["status"] not in {"queued", "running"}:
            break
        await asyncio.sleep(0.02)
    return state, await service.crawl_results(started["job_id"], limit=100)


@pytest.fixture
def forwarding(world: World) -> World:
    """``apex.test`` forwards every path to ``other.test``, another site, which has a robots.txt naming sitemaps (one
    on a subdomain nothing redirected to), pages that link to each other, to that subdomain and to a third site."""

    def other(request: Any) -> Response:
        path = request.target.split("?")[0]
        if path == "/robots.txt":
            return _text(
                "User-agent: *\nAllow: /\nSitemap: https://other.test/sitemap_index.xml\n"
                "Sitemap: https://internal.other.test/sitemap.xml\n"
            )
        if path == "/sitemap_index.xml":
            return _index("https://other.test/s1.xml")
        if path == "/s1.xml":
            return _urlset("https://other.test/secret-a", "https://other.test/secret-b")
        if path == "/sitemap.xml":
            return _urlset("https://other.test/only-in-sitemap")
        return _page(
            "Other " + path,
            "https://other.test/p1",
            "https://internal.other.test/admin",
            "https://third.test/x",
        )

    def internal(request: Any) -> Response:
        if request.target.split("?")[0] == "/sitemap.xml":
            return _urlset("https://internal.other.test/admin", "https://internal.other.test/staff")
        return _page("Internal " + request.target)

    for name, address, handler in (
        ("apex.test", PUBLIC_A, _forward("https://other.test")),
        ("other.test", PUBLIC_B, other),
        ("www.other.test", PUBLIC_C, other),
        ("internal.other.test", PUBLIC_D, internal),
        ("third.test", PUBLIC_E, lambda request: _page("Third " + request.target)),
    ):
        world.dns.answers(name, [address])
        world.internet.serve(address, handler)
    return world


class TestAnAddressThatForwardsToAnotherSite:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("subdomains", [False, True])
    async def test_the_map_is_the_one_page_and_says_where_it_forwards(
        self, forwarding: World, subdomains: bool
    ) -> None:
        service = _service()

        try:
            answer = await service.map("https://apex.test", include_subdomains=subdomains)
        finally:
            await service.close()

        # As on 1.3.1: the page that was asked for, and nothing of the other site's.
        assert answer["urls"] == ["https://apex.test/"] and answer["total"] == 1
        assert answer["url"] == "https://apex.test/" and answer["host"] == "apex.test"
        assert answer["sitemaps"] == [] and answer["errors"] == []
        assert answer["redirected_to"] == "https://other.test/"
        assert "redirected_from" not in answer
        asked = _asked(forwarding)
        # Not one of the other site's sitemaps, nor the host nothing redirected to, was read.
        assert not any(path in " ".join(asked) for path in ("sitemap_index", "s1.xml", "secret"))
        assert "internal.other.test" not in _hosts_asked(forwarding)
        assert "third.test" not in _hosts_asked(forwarding)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "options",
        [{}, {"use_sitemap": True}, {"include_subdomains": True}],
        ids=["defaults", "use_sitemap", "include_subdomains"],
    )
    async def test_the_crawl_reads_the_one_page_and_follows_nothing_of_the_other_site(
        self, forwarding: World, options: dict[str, Any]
    ) -> None:
        service = _service()

        try:
            state, results = await _crawl(service, "https://apex.test", **options)
        finally:
            await service.close()

        # The page the address leads to is the one page, as on 1.3.1; its links are another site's.
        assert [page["url"] for page in results["results"]] == ["https://other.test/"]
        assert state["status"] == "completed" and state["stopped_by"] == "done"
        assert state["redirected_to"] == "https://other.test/" == results["redirected_to"]
        assert _hosts_asked(forwarding) <= {"apex.test", "other.test"}
        assert not any(
            "only-in-sitemap" in asked or "secret" in asked for asked in _asked(forwarding)
        )

    def test_the_rest_routes_say_where_the_address_forwards(
        self, forwarding: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service()

        # In a ``with`` the client keeps one event loop for all its requests: the crawl runs in it, between them.
        with rest_client(monkeypatch, service) as client:
            mapped = client.post(
                "/api/v1/web/map", headers=HEADERS, json={"url": "apex.test"}
            ).json()
            started = client.post(
                "/api/v1/web/crawl",
                headers=HEADERS,
                json={"url": "apex.test", "options": {"max_pages": 3}},
            ).json()
            for _ in range(300):
                status = client.get(
                    f"/api/v1/web/crawl/{started['job_id']}", headers=HEADERS
                ).json()
                if status["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            listing = client.get(
                f"/api/v1/web/crawl/{started['job_id']}/results", headers=HEADERS
            ).json()

        assert mapped["redirected_to"] == "https://other.test/" and mapped["urls"] == [
            "https://apex.test/"
        ]
        assert status["status"] == "completed" and status["redirected_to"] == "https://other.test/"
        assert listing["redirected_to"] == "https://other.test/"
        assert [page["url"] for page in listing["results"]] == ["https://other.test/"]

    @pytest.mark.asyncio
    async def test_an_address_that_does_not_forward_says_nothing_of_it(self, world: World) -> None:
        world.dns.answers("plain.test", [PUBLIC_A])
        world.internet.serve(PUBLIC_A, lambda request: _page("Plain"))
        service = _service()

        try:
            mapped = await service.map("https://plain.test")
            state, results = await _crawl(service, "https://plain.test")
        finally:
            await service.close()

        assert "redirected_to" not in mapped and "redirected_from" not in mapped
        assert "redirected_to" not in state and "redirected_to" not in results


class TestAPageThatStaysWhileItsRobotsAndSitemapForward:
    @pytest.fixture
    def stays(self, world: World) -> World:
        """``a.test`` keeps its pages; only its robots.txt and sitemap.xml forward to ``c.test``, another site."""

        def a_site(request: Any) -> Response:
            path = request.target.split("?")[0]
            if path in ("/robots.txt", "/sitemap.xml"):
                return Response(301, {"Location": "https://c.test" + path}, "")
            return _page("A " + path, "https://a.test/own")

        def c_site(request: Any) -> Response:
            path = request.target.split("?")[0]
            if path == "/robots.txt":
                return _text(
                    "Sitemap: https://c.test/sitemap.xml\nSitemap: https://deep.c.test/sitemap.xml\n"
                )
            if path == "/sitemap.xml":
                return _urlset("https://c.test/c-page-1", "https://c.test/c-page-2")
            return _page("C " + path)

        for name, address, handler in (
            ("a.test", PUBLIC_A, a_site),
            ("c.test", PUBLIC_C, c_site),
            ("deep.c.test", PUBLIC_D, c_site),
        ):
            world.dns.answers(name, [address])
            world.internet.serve(address, handler)
        return world

    @pytest.mark.asyncio
    async def test_the_map_is_the_sites_own_pages_and_says_where_the_files_forward(
        self, stays: World
    ) -> None:
        service = _service()

        try:
            answer = await service.map("https://a.test", include_subdomains=True)
        finally:
            await service.close()

        assert sorted(answer["urls"]) == ["https://a.test/", "https://a.test/own"]
        assert answer["host"] == "a.test" and answer["url"] == "https://a.test/"
        assert answer["sitemaps"] == []
        assert answer["redirected_to"] == "https://c.test/robots.txt"
        assert "redirected_from" not in answer
        assert "deep.c.test" not in _hosts_asked(stays)
        assert not any("c-page" in asked for asked in _asked(stays))

    @pytest.mark.asyncio
    async def test_a_crawl_seeded_from_the_sitemap_reads_no_page_of_the_other_site(
        self, stays: World
    ) -> None:
        service = _service()

        try:
            state, results = await _crawl(service, "https://a.test", use_sitemap=True)
        finally:
            await service.close()

        assert sorted(page["url"] for page in results["results"]) == [
            "https://a.test/",
            "https://a.test/own",
        ]
        assert state["progress"]["seeded"] == 0
        assert state["redirected_to"] == "https://c.test/robots.txt"
        assert "deep.c.test" not in _hosts_asked(stays)
        assert not any("c-page" in asked for asked in _asked(stays))


class TestAStartPageThatIsNotThere:
    @pytest.mark.asyncio
    async def test_the_forwarding_files_of_a_site_with_no_page_list_nothing(
        self, world: World
    ) -> None:
        def a_site(request: Any) -> Response:
            path = request.target.split("?")[0]
            if path in ("/robots.txt", "/sitemap.xml"):
                return Response(301, {"Location": "https://c.test" + path}, "")
            return Response(
                404, {"Content-Type": "text/html"}, "<html><body>not here</body></html>"
            )

        def c_site(request: Any) -> Response:
            path = request.target.split("?")[0]
            if path == "/robots.txt":
                return _text("Sitemap: https://c.test/sitemap.xml\n")
            if path == "/sitemap.xml":
                return _urlset("https://c.test/c-page-1")
            return _page("C " + path)

        for name, address, handler in (("a.test", PUBLIC_A, a_site), ("c.test", PUBLIC_C, c_site)):
            world.dns.answers(name, [address])
            world.internet.serve(address, handler)
        service = _service()

        try:
            answer = await service.map("https://a.test")
        finally:
            await service.close()

        assert answer["urls"] == [] and answer["total"] == 0 and answer["sitemaps"] == []
        assert answer["url"] == "https://a.test/" and answer["host"] == "a.test"
        assert [error["status_code"] for error in answer["errors"]] == [404]
        assert answer["redirected_to"] == "https://c.test/robots.txt"
        assert not any("c-page" in asked for asked in _asked(world))


class TestAFileThatLeavesTheSite:
    @pytest.mark.asyncio
    async def test_a_sitemap_an_index_names_that_forwards_elsewhere_is_not_read(
        self, world: World
    ) -> None:
        def a_site(request: Any) -> Response:
            path = request.target.split("?")[0]
            if path == "/robots.txt":
                return _text("Sitemap: https://a.test/index.xml\n")
            if path == "/index.xml":
                return _index("https://a.test/own.xml", "https://a.test/moved.xml")
            if path == "/own.xml":
                return _urlset("https://a.test/p1")
            if path == "/moved.xml":
                return Response(301, {"Location": "https://c.test/moved.xml"}, "")
            if path == "/sitemap.xml":
                return html("not here", 404)
            return _page("A " + path)

        def c_site(request: Any) -> Response:
            # It lists a page of a.test too: a file of another site does not speak for a.test.
            return _urlset("https://c.test/c-page", "https://a.test/spoofed")

        for name, address, handler in (("a.test", PUBLIC_A, a_site), ("c.test", PUBLIC_C, c_site)):
            world.dns.answers(name, [address])
            world.internet.serve(address, handler)
        service = _service()

        try:
            answer = await service.map("https://a.test")
        finally:
            await service.close()

        assert "https://a.test/p1" in answer["urls"]
        assert not any("c-page" in address or "spoofed" in address for address in answer["urls"])
        assert answer["sitemaps"] == ["https://a.test/index.xml", "https://a.test/own.xml"]
        assert answer["redirected_to"] == "https://c.test/moved.xml"

    @pytest.mark.asyncio
    async def test_the_files_of_a_host_the_site_moved_to_that_forward_elsewhere_are_not_read(
        self, world: World
    ) -> None:
        # x.test is www.x.test (the site's own); the robots.txt of www.x.test forwards to another site.
        def www(request: Any) -> Response:
            path = request.target.split("?")[0]
            if path in ("/robots.txt", "/sitemap.xml"):
                return Response(301, {"Location": "https://c.test" + path}, "")
            return _page("X " + path)

        def c_site(request: Any) -> Response:
            if request.target.startswith("/robots.txt"):
                return _text("Sitemap: https://c.test/sitemap.xml\n")
            return _urlset("https://c.test/c-page")

        for name, address, handler in (
            ("x.test", PUBLIC_A, _forward("https://www.x.test")),
            ("www.x.test", PUBLIC_B, www),
            ("c.test", PUBLIC_C, c_site),
        ):
            world.dns.answers(name, [address])
            world.internet.serve(address, handler)
        service = _service()

        try:
            answer = await service.map("https://x.test")
        finally:
            await service.close()

        assert answer["host"] == "www.x.test" and answer["redirected_from"] == "https://x.test/"
        assert answer["urls"] == ["https://www.x.test/"] and answer["sitemaps"] == []
        assert answer["redirected_to"] == "https://c.test/robots.txt"

    def test_a_job_keeps_where_its_address_led_across_a_restart(self) -> None:
        settings = WebToolsSettings(browser_enabled=False)
        job = CrawlJob(
            job_id="j",
            url="https://a.test/",
            options=CrawlOptions.from_dict(None, settings),
            status="completed",
        )
        job.redirected_to = "https://c.test/"

        restored = CrawlJob.from_record({**job.record(), "results": []}, settings)

        assert restored.redirected_to == "https://c.test/"
        assert restored.snapshot()["redirected_to"] == "https://c.test/"


class TestAMoveWithinTheSiteIsStillTheSite:
    """What 1.4.0 got right and must keep: the landing is the site for a ``www.`` flip, ``http`` to ``https`` and the
    same registrable domain."""

    @pytest.fixture
    def moved(self, world: World) -> World:
        pages = {
            "/robots.txt": _text("Sitemap: https://www.example.test/sitemap.xml\n"),
            "/sitemap.xml": _urlset("https://www.example.test/from-sitemap"),
        }

        def www(request: Any) -> Response:
            path = request.target.split("?")[0]
            return pages.get(path) or _page("Example " + path, "https://www.example.test/linked")

        for name, address, handler in (
            ("docs.example.test", PUBLIC_A, _forward("https://www.example.test")),
            ("www.example.test", PUBLIC_B, www),
            ("blog.example.test", PUBLIC_C, lambda request: _page("Blog " + request.target)),
        ):
            world.dns.answers(name, [address])
            world.internet.serve(address, handler)
        return world

    @pytest.mark.asyncio
    async def test_another_host_of_the_same_registrable_domain_is_the_site(
        self, moved: World
    ) -> None:
        service = _service()

        try:
            answer = await service.map("https://docs.example.test")
        finally:
            await service.close()

        assert answer["host"] == "www.example.test" and answer["redirected_from"] == (
            "https://docs.example.test/"
        )
        assert "redirected_to" not in answer
        assert "https://www.example.test/from-sitemap" in answer["urls"]
        assert answer["sitemaps"] == ["https://www.example.test/sitemap.xml"]

    @pytest.mark.asyncio
    async def test_a_crawl_of_it_follows_the_pages_of_the_host_it_landed_on(
        self, moved: World
    ) -> None:
        service = _service()

        try:
            state, results = await _crawl(service, "https://docs.example.test")
        finally:
            await service.close()

        urls_read = [page["url"] for page in results["results"]]
        assert urls_read[0] == "https://www.example.test/"
        assert "https://www.example.test/linked" in urls_read
        assert "redirected_to" not in state


class TestSameSite:
    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("https://example.com/", "https://example.com/"),
            ("http://example.com/", "https://example.com/a"),
            ("https://example.com/", "https://www.example.com/"),
            ("https://www.example.com/", "https://example.com/"),
            ("https://docs.example.com/", "https://www.example.com/"),
            ("https://a.example.com/", "https://b.example.com/"),
            ("https://example.co.uk/", "https://www.example.co.uk/"),
            ("https://a.example.co.uk/", "https://b.example.co.uk/"),
            ("https://example.com.tr/", "https://www.example.com.tr/"),
            ("https://93.184.216.34/", "https://93.184.216.34/x"),
            ("https://[2606:2800:220:1::1]/", "https://[2606:2800:220:1::1]/x"),
            ("https://alice.github.io/", "https://www.alice.github.io/"),
            ("https://a.example.co.za/", "https://b.example.co.za/"),
            ("https://example.com.sg/", "https://www.example.com.sg/"),
            ("https://blog.google/", "https://store.google/"),
        ],
    )
    def test_the_same_host_a_www_flip_or_the_same_registrable_domain(
        self, first: str, second: str
    ) -> None:
        assert urls.same_registrable_domain(first, second) is True

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("https://apex.test/", "https://other.test/"),
            ("https://example.com/", "https://example.org/"),
            ("https://example.com/", "https://example.com.evil.test/"),
            ("https://example.com/", "https://notexample.com/"),
            ("https://example.com/", "https://example.com.cn/"),
            ("https://a.co.uk/", "https://b.co.uk/"),
            ("https://a.com.au/", "https://b.com.au/"),
            ("https://a.com.tr/", "https://b.com.tr/"),
            ("https://a.co.za/", "https://b.co.za/"),
            ("https://a.com.sg/", "https://b.com.sg/"),
            ("https://a.or.jp/", "https://b.or.jp/"),
            ("https://alice.github.io/", "https://bob.github.io/"),
            ("https://a.blogspot.com/", "https://b.blogspot.com/"),
            ("https://a.herokuapp.com/", "https://b.herokuapp.com/"),
            ("https://a.vercel.app/", "https://b.vercel.app/"),
            ("https://93.184.216.34/", "https://93.184.216.35/"),
            ("https://example.com/", "https://93.184.216.34/"),
            ("https://example.com/", ""),
            ("", "https://example.com/"),
        ],
    )
    def test_another_site_is_not(self, first: str, second: str) -> None:
        assert urls.same_registrable_domain(first, second) is False

    @pytest.mark.parametrize(
        ("host", "registrable"),
        [
            ("example.com", "example.com"),
            ("www.example.com", "example.com"),
            ("a.b.example.com", "example.com"),
            ("www.example.co.uk", "example.co.uk"),
            ("example.com.tr", "example.com.tr"),
            ("docs.example.com.au", "example.com.au"),
            ("alice.github.io", "alice.github.io"),
            ("www.alice.github.io", "alice.github.io"),
            ("github.io", "github.io"),
            ("x.b.a.co.za", "a.co.za"),
            ("www.example.com.sg", "example.com.sg"),
            ("shop.example.or.jp", "example.or.jp"),
            ("blog.google", "google"),
            ("localhost", "localhost"),
            ("93.184.216.34", "93.184.216.34"),
            ("2606:2800:220:1::1", "2606:2800:220:1::1"),
        ],
    )
    def test_the_registrable_domain(self, host: str, registrable: str) -> None:
        assert registrable_domain(host) == registrable

    def test_the_scope_of_a_site_is_the_address_asked_for_and_a_landing_of_the_same_site(
        self,
    ) -> None:
        assert urls.site_scopes("https://a.test/", "https://a.test/") == ["https://a.test/"]
        assert urls.site_scopes("https://a.test/", "https://www.a.test/") == [
            "https://a.test/",
            "https://www.a.test/",
        ]
        # Another site's landing is never a scope, whoever hands it over.
        assert urls.site_scopes("https://a.test/", "https://other.test/") == ["https://a.test/"]
        assert urls.site_scopes("https://www.a.test/x", "https://www.a.test/x") == [
            "https://www.a.test/x"
        ]
