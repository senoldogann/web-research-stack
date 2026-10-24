"""A page the site gives for an address it has nothing at (a soft 404) says so.

senoldogan.dev answers ``/bu-sayfa-yok-12345`` with a 200: the home page's ``<title>``, no canonical link and the
same JavaScript shell as every page. ``web_extract`` returned the home page's title and description as the fields
of a page that does not exist. Now a page that may be a fallback (not the root, and not naming itself by a canonical
link, or titled as the root is) is compared with what its host answers for a path that cannot exist, asked once
per host for the session, through the same loader (so a page drawn by scripts is compared as it was drawn); the
page that is that answer is marked: ``warnings`` holds ``soft_404``, the answer says ``looks_missing`` and a
``hint``, and ``web_extract`` reads no field from it.

The sites here are in memory (``tests/rebinding_support.py``): every request they get is recorded, so the cost of
the check, the request it adds, is counted.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
from rebinding_support import PUBLIC_A, PUBLIC_B, Response, World, html

from web_scraper.web_tools.browser import BrowserPage
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.missing import (
    SAME_WORDS,
    fingerprint,
    probe_url,
    same_page,
    suspect,
    title_key,
)
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

HOME_TITLE = "Acme Studio — Web and mobile software"
WELCOME = "Acme Studio designs, builds and maintains web and mobile software for small businesses. "


def _page(title: str, body: str, *, canonical: Optional[str] = None, scripts: bool = False) -> str:
    link = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    script = '<script src="/app.js"></script>' if scripts else ""
    return (
        f"<html><head><title>{title}</title>{link}</head>"
        f"<body><main><h1>{title}</h1><p>{body}</p></main>{script}</body></html>"
    )


HOME = _page(HOME_TITLE, WELCOME * 4, canonical="https://soft.test/")
POST = _page(
    "A post about websites | Acme Studio",
    "Seven points that decide whether a small business website brings enquiries. " * 4,
    canonical="https://soft.test/blog/post-1",
)
ABOUT = _page("About | Acme Studio", "Acme Studio is a studio of two people in Oulu. " * 5)


class Site:
    """A host of the fake network: what it serves by path, and every path it was asked for."""

    def __init__(
        self,
        world: World,
        name: str,
        address: str,
        pages: dict[str, str],
        *,
        fallback: Optional[str] = None,
        status_of_the_rest: int = 200,
    ) -> None:
        self.name, self.asked = name, []
        self.pages, self.fallback, self.status_of_the_rest = pages, fallback, status_of_the_rest
        world.dns.answers(name, [address])
        world.internet.serve(address, self._handler)

    def _handler(self, request: Any) -> Response:
        self.asked.append(request.target)
        if request.target in self.pages:
            return html(self.pages[request.target])
        if self.fallback is not None:
            return html(self.fallback, self.status_of_the_rest)
        return html("Not found", 404)

    def url(self, path: str) -> str:
        return f"https://{self.name}{path}"

    @property
    def probes(self) -> list[str]:
        return [path for path in self.asked if path.startswith("/web-research-probe-")]


def _service(**settings: Any) -> WebToolsService:
    base: dict[str, Any] = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": [],
        "search_resolve_dates": "off",
        "respect_robots": False,
        "browser_enabled": False,
    }
    base.update(settings)
    return WebToolsService(
        settings=WebToolsSettings(**base),
        fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
        browser=None,
        search_adapters={},
    )


@pytest.fixture
def soft(world: World) -> Site:
    """A site that answers every address it has no page for with its home page."""
    return Site(
        world,
        "soft.test",
        PUBLIC_A,
        {"/": HOME, "/blog/post-1": POST, "/about": ABOUT},
        fallback=HOME,
    )


class TestAPageTheSiteGivesForAnyAddress:
    @pytest.mark.asyncio
    async def test_scrape_says_it_may_not_exist(self, soft: Site) -> None:
        service = _service()

        try:
            answer = await service.scrape(soft.url("/bu-sayfa-yok-12345"))
        finally:
            await service.close()

        assert answer["status_code"] == 200
        assert "soft_404" in answer["warnings"] and answer["looks_missing"] is True
        assert "answers every address with the same page" in answer["hint"]
        # What was read is still said to be what the site gave, with the title it gave.
        assert answer["title"] == HOME_TITLE and "Acme Studio designs" in answer["markdown"]

    @pytest.mark.asyncio
    async def test_extract_reads_no_field_from_the_fallback_and_says_the_page_may_not_exist(
        self, soft: Site
    ) -> None:
        service = _service()

        try:
            answer = await service.extract(
                soft.url("/bu-sayfa-yok-12345"),
                {"title": {"required": True}, "description": {}, "headline": {}},
            )
        finally:
            await service.close()

        assert answer["data"] == {"title": None, "description": None, "headline": None}
        assert {entry["method"] for entry in answer["provenance"].values()} == {"not_found"}
        # No field is read from the fallback, the answer's own title and description included.
        assert (
            answer["title"] == "" and answer["description"] == "" and answer["canonical_url"] == ""
        )
        assert answer["date"] is None and answer["date_confidence"] == 0.0
        assert "soft_404" in answer["warnings"] and answer["looks_missing"] is True
        assert "may not exist" in answer["hint"]
        assert [error["code"] for error in answer["errors"]] == ["required_field_missing"]

    @pytest.mark.asyncio
    async def test_extract_of_the_same_page_before_this_round_returned_the_home_pages_title_twice(
        self, soft: Site
    ) -> None:
        # What the owner saw, kept as the contrast: the same fallback read with the check off.
        service = _service(soft_404_check=False)

        try:
            answer = await service.extract(soft.url("/bu-sayfa-yok-12345"), {"title": {}})
        finally:
            await service.close()

        assert answer["data"]["title"] == HOME_TITLE and "looks_missing" not in answer

    @pytest.mark.asyncio
    async def test_the_lean_view_and_a_batch_carry_it_too(self, soft: Site) -> None:
        service = _service()

        try:
            lean = await service.scrape(soft.url("/nowhere"), lean=True)
            batch = await service.scrape_many([soft.url("/nowhere-1"), soft.url("/blog/post-1")])
        finally:
            await service.close()

        assert lean["looks_missing"] is True and "hint" in lean and "soft_404" in lean["warnings"]
        missing, real = batch["pages"]
        assert missing["looks_missing"] is True
        assert "looks_missing" not in real and real["warnings"] == []

    @pytest.mark.asyncio
    async def test_a_page_read_again_from_the_page_cache_is_judged_the_same(
        self, soft: Site
    ) -> None:
        service = _service()

        try:
            await service.scrape(soft.url("/nowhere"))
            again = await service.extract(soft.url("/nowhere"), {"title": {}})
        finally:
            await service.close()

        assert again["cache_hit"] is True and again["looks_missing"] is True
        assert again["data"] == {"title": None}
        assert soft.asked.count("/nowhere") == 1 and len(soft.probes) == 1


class TestWhatTheCheckCosts:
    @pytest.mark.asyncio
    async def test_a_page_that_names_itself_costs_no_request(self, soft: Site) -> None:
        service = _service()

        try:
            answer = await service.scrape(soft.url("/blog/post-1"))
        finally:
            await service.close()

        assert answer["warnings"] == [] and "looks_missing" not in answer
        assert soft.asked == ["/blog/post-1"]

    @pytest.mark.asyncio
    async def test_the_root_is_never_judged(self, soft: Site) -> None:
        service = _service()

        try:
            answer = await service.scrape(soft.url("/"))
        finally:
            await service.close()

        assert answer["warnings"] == [] and soft.asked == ["/"]

    @pytest.mark.asyncio
    async def test_a_page_with_no_canonical_costs_one_request_to_its_host_and_is_not_taken_for_missing(
        self, soft: Site, world: World
    ) -> None:
        service = _service()

        try:
            answer = await service.scrape(soft.url("/about"))
        finally:
            await service.close()

        assert answer["warnings"] == [] and "looks_missing" not in answer
        assert soft.asked[0] == "/about" and len(soft.probes) == 1 and len(soft.asked) == 2
        # The extra request is to the same host, through the same checks: the one address the fetcher
        # resolved and checked, and nothing else.
        assert set(world.internet.dials) == {(PUBLIC_A, 443)}
        assert world.internet.private_dials == [] and world.internet.named_dials == []

    @pytest.mark.asyncio
    async def test_the_probe_is_read_once_per_host_and_kept_for_the_session(
        self, soft: Site
    ) -> None:
        service = _service()

        try:
            batch = await service.scrape_many([soft.url(f"/gone-{n}") for n in range(4)])
            later = await service.scrape(soft.url("/gone-later"))
            real = await service.scrape(soft.url("/about"))
        finally:
            await service.close()

        assert all(page["looks_missing"] for page in batch["pages"]) and later["looks_missing"]
        assert real["warnings"] == []
        # Six pages, one probe: the pages that wanted it together waited for it, the rest read it kept.
        assert len(soft.probes) == 1

    @pytest.mark.asyncio
    async def test_the_probe_path_is_a_random_one_of_the_same_host(self, soft: Site) -> None:
        first, second = probe_url("https://soft.test"), probe_url("https://soft.test/")

        assert first != second
        assert first.startswith("https://soft.test/web-research-probe-")
        assert second.startswith("https://soft.test/web-research-probe-")

    @pytest.mark.asyncio
    async def test_the_check_can_be_turned_off_and_then_asks_nothing(self, soft: Site) -> None:
        service = _service(soft_404_check=False)

        try:
            answer = await service.scrape(soft.url("/nowhere"))
        finally:
            await service.close()

        assert answer["warnings"] == [] and soft.asked == ["/nowhere"]


class TestAHostThatSaysWhenAPageIsNotThere:
    @pytest.mark.asyncio
    async def test_a_404_for_the_probe_is_an_honest_host_and_nothing_is_flagged(
        self, world: World
    ) -> None:
        honest = Site(
            world,
            "honest.test",
            PUBLIC_B,
            {"/about": ABOUT, "/team": ABOUT.replace("About", "Team")},
        )
        service = _service()

        try:
            first = await service.scrape(honest.url("/about"))
            second = await service.scrape(honest.url("/team"))
        finally:
            await service.close()

        assert first["warnings"] == [] and second["warnings"] == []
        assert len(honest.probes) == 1  # asked once, answered 404, and believed

    @pytest.mark.asyncio
    async def test_a_page_that_is_missing_on_an_honest_host_is_a_404_as_it_was(
        self, world: World
    ) -> None:
        honest = Site(world, "honest.test", PUBLIC_B, {"/about": ABOUT})
        service = _service()

        try:
            with pytest.raises(Exception) as exc:
                await service.scrape(honest.url("/nowhere"))
        finally:
            await service.close()

        assert getattr(exc.value, "status_code", None) == 404
        assert honest.probes == []  # no page was read, so there was nothing to judge

    @pytest.mark.asyncio
    async def test_a_probe_that_fails_tells_nothing_and_is_asked_again_after_five_minutes(
        self, world: World
    ) -> None:
        broken = Site(
            world,
            "broken.test",
            PUBLIC_B,
            {"/about": ABOUT, "/team": ABOUT},
            fallback="oops",
            status_of_the_rest=500,
        )
        service = _service()

        try:
            first = await service.scrape(broken.url("/about"))
            second = await service.scrape(broken.url("/team"))
            assert len(broken.probes) == 1  # a probe that told nothing is not asked again at once
            [record] = service._fallbacks.values()
            assert record.kind == "unknown"
            record.at -= 301.0
            # (a third page of the same host asks again)
            broken.pages["/third"] = ABOUT
            third = await service.scrape(broken.url("/third"))
        finally:
            await service.close()

        assert first["warnings"] == second["warnings"] == third["warnings"] == []
        assert len(broken.probes) == 2


class TestAPageThatNamesItselfAndIsTitledAsTheRoot:
    """A framework that writes the canonical link from the address it was asked for: the fallback names itself.
    What gives it away is its title, the root's, so the root this session has read is remembered by its title."""

    @staticmethod
    def _framework(world: World) -> Site:
        def fallback_for(path: str) -> str:
            return _page(HOME_TITLE, WELCOME * 4, canonical=f"https://framework.test{path}")

        site = Site(
            world, "framework.test", PUBLIC_B, {"/": HOME.replace("soft.test", "framework.test")}
        )
        original = site._handler

        def handler(request: Any) -> Response:
            if request.target not in site.pages:
                site.asked.append(request.target)
                return html(fallback_for(request.target))
            return original(request)

        world.internet.serve(PUBLIC_B, handler)
        return site

    @pytest.mark.asyncio
    async def test_the_root_read_earlier_makes_such_a_page_suspect_and_it_is_found_out(
        self, world: World
    ) -> None:
        site = self._framework(world)
        service = _service()

        try:
            await service.scrape(site.url("/"))
            answer = await service.scrape(site.url("/nothing-here"))
        finally:
            await service.close()

        assert answer["looks_missing"] is True and len(site.probes) == 1

    @pytest.mark.asyncio
    async def test_without_the_root_a_page_that_names_itself_is_taken_at_its_word(
        self, world: World
    ) -> None:
        site = self._framework(world)
        service = _service()

        try:
            answer = await service.scrape(site.url("/nothing-here"))
        finally:
            await service.close()

        assert "looks_missing" not in answer and site.probes == []


class TestAPageDrawnByScriptsIsComparedAsItWasDrawn:
    """The HTTP answer is one shell for every address; what tells a page from the fallback is what the browser
    draws, so the probe is read as the page was: through the same loader, in the browser."""

    SHELL = (
        "<html><head><title>App</title></head><body><div id='root'>Loading animation while page loads"
        "</div><script src='/app.js'></script></body></html>"
    )

    class Browser:
        def __init__(self, drawn: dict[str, str], default: str) -> None:
            self.drawn, self.default, self.calls = drawn, default, []

        async def stop(self) -> None:
            return None

        async def render(self, url: str, *, post_load_wait_ms: Optional[int] = None) -> BrowserPage:
            self.calls.append(url)
            path = "/" + url.split("/", 3)[-1]
            return BrowserPage(self.drawn.get(path, self.default), url, 200, "chromium")

    def _service(self, browser: Any) -> WebToolsService:
        service = _service(browser_enabled=True)
        service.browser = browser
        return service

    @pytest.mark.asyncio
    async def test_a_page_that_draws_something_of_its_own_is_not_taken_for_the_fallback(
        self, world: World
    ) -> None:
        Site(world, "app.test", PUBLIC_B, {}, fallback=self.SHELL)
        real = _page(
            "App",
            "Pricing: three plans, each with its own limits and its own price. " * 4,
            scripts=True,
        )
        not_found = _page(
            "App", "Nothing lives at this address. Go back to the home page. ", scripts=True
        )
        browser = self.Browser({"/pricing": real}, default=not_found)
        service = self._service(browser)

        try:
            pricing = await service.scrape("https://app.test/pricing")
            nowhere = await service.scrape("https://app.test/nowhere")
        finally:
            await service.close()

        assert "Pricing" in pricing["markdown"] and "looks_missing" not in pricing
        assert nowhere["looks_missing"] is True and "soft_404" in nowhere["warnings"]

    @pytest.mark.asyncio
    async def test_two_shells_that_never_fill_are_the_same_page_and_the_page_may_not_exist(
        self, world: World
    ) -> None:
        # What senoldogan.dev answers for a missing page: the home page's title and a shell that stays one.
        Site(world, "app.test", PUBLIC_B, {}, fallback=self.SHELL)
        browser = self.Browser({}, default=self.SHELL)
        service = self._service(browser)

        try:
            answer = await service.scrape("https://app.test/bu-sayfa-yok-12345")
        finally:
            await service.close()

        assert answer["warnings"] == ["rendered_shell", "soft_404"]
        assert answer["looks_missing"] is True
        assert len(browser.calls) == 2  # the page, and the probe, read as the page was

    @pytest.mark.asyncio
    async def test_a_real_page_that_stays_a_shell_with_its_own_title_is_a_shell_not_a_missing_page(
        self, world: World
    ) -> None:
        # A page the server knows has its own title in the HTML; the fallback has the site's.
        own = self.SHELL.replace("<title>App</title>", "<title>Seven things to check</title>")
        Site(world, "app.test", PUBLIC_B, {"/post": own}, fallback=self.SHELL)
        # The browser draws the page as the server titled it: the shell, with the page's own title.
        browser = self.Browser({"/post": own}, default=self.SHELL)
        service = self._service(browser)

        try:
            answer = await service.scrape("https://app.test/post")
        finally:
            await service.close()

        assert answer["warnings"] == ["rendered_shell"] and "looks_missing" not in answer


class TestACrawlOfASiteThatAnswersEverything:
    @pytest.mark.asyncio
    async def test_a_page_that_is_the_fallback_is_an_error_of_the_crawl_and_its_links_are_not_followed(
        self, world: World
    ) -> None:
        home = _page(
            HOME_TITLE,
            WELCOME * 4 + '<a href="/gone">Gone</a> <a href="/about">About</a>',
            canonical="https://soft.test/",
        )
        # The fallback links to a page of its own that nothing else links to.
        fallback = _page(
            HOME_TITLE,
            WELCOME * 4 + '<a href="/only-the-fallback-links-here">x</a>',
            canonical="https://soft.test/",
        )
        site = Site(world, "soft.test", PUBLIC_A, {"/": home, "/about": ABOUT}, fallback=fallback)
        service = _service()

        try:
            job = await service.start_crawl("https://soft.test/", {"max_pages": 10, "max_depth": 3})
            for _ in range(200):
                snapshot = await service.get_crawl(job["job_id"])
                if snapshot["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            results = await service.crawl_results(job["job_id"])
        finally:
            await service.close()

        assert [page["url"] for page in results["results"]] == [
            "https://soft.test/",
            "https://soft.test/about",
        ]
        assert [(error["url"], error["code"]) for error in snapshot["errors"]] == [
            ("https://soft.test/gone", "soft_404")
        ]
        assert snapshot["progress"]["failed"] == 1
        assert "/only-the-fallback-links-here" not in site.asked


class TestTheComparison:
    def test_the_title_and_the_text_must_both_be_the_same(self) -> None:
        home = fingerprint("Acme — Home", WELCOME * 4)

        assert same_page(
            home, fingerprint("acme  —  HOME", WELCOME * 4)
        )  # spacing and case are not differences
        assert not same_page(
            home, fingerprint("About | Acme", WELCOME * 4)
        )  # another title: a page of its own
        assert not same_page(
            home, fingerprint("Acme — Home", "A different text altogether, about other things.")
        )

    def test_nine_words_in_ten_alike_is_the_same_text(self) -> None:
        words = [f"word{n}" for n in range(100)]
        left = fingerprint("T", " ".join(words))
        nearly = fingerprint("T", " ".join(words[:95] + ["x1", "x2", "x3", "x4"]))
        less = fingerprint("T", " ".join(words[:80] + [f"other{n}" for n in range(20)]))

        assert same_page(left, nearly) and not same_page(left, less)
        assert SAME_WORDS == 0.9

    def test_a_fallback_that_names_the_address_it_was_asked_for_is_still_the_fallback(self) -> None:
        body = WELCOME * 6
        left = fingerprint(
            "Not found", body + "Nothing at /bu-sayfa-yok-12345.", "/bu-sayfa-yok-12345"
        )
        right = fingerprint(
            "Not found",
            body + "Nothing at /web-research-probe-9f3a6c1e2b7d4a58.",
            "/web-research-probe-9f3a6c1e2b7d4a58",
        )

        assert same_page(left, right)
        # Without the words of the addresses left out the two texts would differ by eight words in twenty.
        assert not same_page(
            fingerprint("Not found", body + "Nothing at /bu-sayfa-yok-12345."),
            fingerprint("Not found", body + "Nothing at /web-research-probe-9f3a6c1e2b7d4a58."),
        )
        assert not same_page(
            left, fingerprint("Not found", "Something else entirely, said briefly.")
        )

    def test_the_text_is_compared_from_its_start(self) -> None:
        start = "alpha beta gamma delta " * 400  # 8,800 characters: more than is compared
        assert same_page(fingerprint("T", start + "one"), fingerprint("T", start + "two"))

    @pytest.mark.parametrize(
        ("final_url", "declared", "title", "root", "expected"),
        [
            ("https://x.test/", "", "T", "", False),  # the root is what a fallback shows
            ("https://x.test/p", "", "T", "", True),  # names no canonical
            ("https://x.test/p", "https://x.test/", "T", "", True),  # names another address
            ("https://x.test/p", "https://x.test/p", "T", "", False),  # names itself
            (
                "https://x.test/p/",
                "https://x.test/p?utm_source=a",
                "T",
                "",
                False,
            ),  # the same page, spelt otherwise
            (
                "https://x.test/p",
                "https://x.test/p",
                "Home",
                title_key("Home"),
                True,
            ),  # titled as the root
            ("https://x.test/p", "https://x.test/p", "Other", title_key("Home"), False),
            ("https://x.test/p", "not a url", "T", "", True),
        ],
    )
    def test_which_pages_may_be_a_fallback(
        self, final_url: str, declared: str, title: str, root: str, expected: bool
    ) -> None:
        assert suspect(final_url, declared, title, root) is expected
