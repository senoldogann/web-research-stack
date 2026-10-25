"""A page the server read again after a wall is judged as any other page is.

1.4.2 reads a page once more in a browser (Chrome, and then Camoufox) when its site's bot check answers the HTTP
read with a wall: a captcha, an access block, HTTP 401, 403 or 429. What that browser read is the page, as far as the
tools go. So the rules that hold for a page read over HTTP hold for it: the address a map or a crawl landed on is the
site only if it is the same site (``tests/test_web_site_boundary.py``), and a page the site gives for any address says
``soft_404`` whoever read it first (``tests/test_web_missing_pages.py``). The sites are in memory and walled to the
HTTP read; the browser is a fake that answers as the stealth engine does, with a page.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from rebinding_support import PUBLIC_A, PUBLIC_B, Response, World

from web_scraper.web_tools.browser import BrowserPage
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

TEXT = (
    "A page with something of its own to say, and enough of it to be a page and not a shell. " * 4
)


def _page(title: str, *links: str) -> str:
    anchors = "".join(f'<a href="{href}">link {n}</a> ' for n, href in enumerate(links))
    return (
        f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{TEXT}</p>"
        f"<p>{anchors}</p></main></body></html>"
    )


class Camoufox:
    """The stealth engine as the service sees it: it can be asked, and it answers a page or a failure."""

    has_stealth = True

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def stop(self) -> None:
        return None

    async def render(self, url: str, *, post_load_wait_ms: Any = None) -> BrowserPage:
        self.calls.append(url)
        html, final_url = self.answer(url)
        return BrowserPage(html=html, final_url=final_url, status_code=200, engine="camoufox")


def _walled(world: World, name: str, address: str) -> None:
    """A site whose bot check turns the HTTP read away, for every address."""
    world.dns.answers(name, [address])
    world.internet.serve(
        address,
        lambda request: Response(
            403, {"Content-Type": "text/html"}, "<html><body>Access denied</body></html>"
        ),
    )


def _service(browser: Camoufox, **settings: Any) -> WebToolsService:
    base: dict[str, Any] = {
        "search_provider": "fake",
        "respect_robots": False,
        "crawl_concurrency": 1,
        "soft_404_check": False,
    }
    base.update(settings)
    return WebToolsService(
        settings=WebToolsSettings(**base),
        fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
        browser=browser,  # type: ignore[arg-type]
        search_adapters={},
    )


async def _crawl(service: WebToolsService, url: str, **options: Any) -> tuple[dict, dict]:
    started = await service.start_crawl(url, {"max_pages": 10, "max_depth": 2, **options})
    for _ in range(300):
        state = await service.get_crawl(started["job_id"])
        if state["status"] not in {"queued", "running"}:
            break
        await asyncio.sleep(0.02)
    return state, await service.crawl_results(started["job_id"], limit=100)


class TestTheLandingOfAPageReadAfterAWall:
    @pytest.fixture
    def forwarding(self, world: World) -> Camoufox:
        """apex.test turns the HTTP read away; Camoufox is let in, and lands on other.test, another site."""
        _walled(world, "apex.test", PUBLIC_A)
        return Camoufox(
            lambda url: (
                _page("Other", "https://other.test/p1", "https://internal.other.test/admin"),
                "https://other.test/",
            )
        )

    @pytest.mark.asyncio
    async def test_a_map_is_the_one_page_and_says_where_the_address_led(
        self, forwarding: Camoufox
    ) -> None:
        service = _service(forwarding)

        try:
            answer = await service.map("https://apex.test", include_subdomains=True)
        finally:
            await service.close()

        assert answer["urls"] == ["https://apex.test/"] and answer["host"] == "apex.test"
        assert answer["redirected_to"] == "https://other.test/" and "redirected_from" not in answer
        assert forwarding.calls == ["https://apex.test/"]  # the page was read again once, there

    @pytest.mark.asyncio
    async def test_a_crawl_reads_the_one_page_and_follows_nothing_of_the_other_site(
        self, forwarding: Camoufox
    ) -> None:
        service = _service(forwarding)

        try:
            state, results = await _crawl(service, "https://apex.test", include_subdomains=True)
        finally:
            await service.close()

        assert [page["url"] for page in results["results"]] == ["https://other.test/"]
        assert state["redirected_to"] == "https://other.test/"
        assert forwarding.calls == ["https://apex.test/"]

    @pytest.mark.asyncio
    async def test_the_same_site_is_still_the_site(self, world: World) -> None:
        _walled(world, "docs.example.test", PUBLIC_B)
        browser = Camoufox(
            lambda url: (
                _page("Example", "https://www.example.test/more"),
                "https://www.example.test/",
            )
        )
        service = _service(browser)

        try:
            answer = await service.map("https://docs.example.test")
        finally:
            await service.close()

        assert answer["host"] == "www.example.test" and answer["redirected_from"] == (
            "https://docs.example.test/"
        )
        assert "redirected_to" not in answer and "https://www.example.test/more" in answer["urls"]


class TestAPageReadAfterAWallIsJudgedForBeingTheSitesAnswerToAnyAddress:
    HOME = _page("Acme Studio")

    @pytest.fixture
    def catch_all(self, world: World) -> Camoufox:
        """soft.test turns the HTTP read away for every address; Camoufox is given its home page for every one."""
        _walled(world, "soft.test", PUBLIC_A)
        return Camoufox(lambda url: (self.HOME, url))

    @pytest.mark.asyncio
    async def test_a_scrape_says_soft_404_and_that_the_wall_was_met(
        self, catch_all: Camoufox
    ) -> None:
        service = _service(catch_all, soft_404_check=True)

        try:
            answer = await service.scrape("https://soft.test/blog/lost")
        finally:
            await service.close()

        assert answer["fetched_with"] == "browser"
        assert "http_access_blocked" in answer["warnings"] and "soft_404" in answer["warnings"]
        assert answer["looks_missing"] is True
        # The page, and the probe of the missing-page check (a path that cannot exist), both read after the wall.
        assert catch_all.calls[0] == "https://soft.test/blog/lost"
        assert len(catch_all.calls) == 2 and "web-research-probe-" in catch_all.calls[1]

    @pytest.mark.asyncio
    async def test_a_page_a_map_read_first_after_a_wall_is_judged_when_it_is_scraped(
        self, catch_all: Camoufox
    ) -> None:
        service = _service(catch_all, soft_404_check=True)

        try:
            await service.map("https://soft.test/blog/lost")
            answer = await service.scrape("https://soft.test/blog/lost")
        finally:
            await service.close()

        assert "soft_404" in answer["warnings"] and answer["looks_missing"] is True
        assert catch_all.calls.count("https://soft.test/blog/lost") == 1  # not read a third time

    @pytest.mark.asyncio
    async def test_a_page_with_something_of_its_own_is_not_taken_for_the_answer(
        self, world: World
    ) -> None:
        _walled(world, "soft.test", PUBLIC_A)
        browser = Camoufox(
            lambda url: (
                _page("A post about websites | Acme Studio")
                if url.endswith("/blog/post-1")
                else self.HOME,
                url,
            )
        )
        service = _service(browser, soft_404_check=True)

        try:
            answer = await service.scrape("https://soft.test/blog/post-1")
        finally:
            await service.close()

        assert "soft_404" not in answer["warnings"] and "looks_missing" not in answer
