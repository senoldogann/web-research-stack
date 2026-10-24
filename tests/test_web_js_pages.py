"""Pages that need JavaScript: read again in the browser, waited for, and said to be a shell when they stay one.

On 1.3.1 senoldogan.dev's blog pages came back as "Loading animation while page loads" (34 characters): the
browser had been asked, but it read the page half a second after it loaded and 1.3.1 kept the HTTP shell when the
browser's text was the shorter. Now:

* a page whose readable text is under 200 characters while its HTML has scripts (a loading placeholder, "Please
  enable JavaScript", an empty ``#root``, ``#app`` or ``#__next``, or a few lines a script replaces) is read again
  in the browser, which waits for it to settle (``test_web_browser_settle.py``);
* a page that is still a shell after that says so (``warnings: ["rendered_shell"]`` and a ``hint``), and one that
  could not be read in a browser says ``needs_javascript``: neither is a confident short page, and neither is kept
  in the caches;
* scrape, extract, a crawl's pages and a map's start page read a page through one loader, so that two tools never
  disagree on one page.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
from browser_support import (
    DRAWN,
    LOADING,
    LocalSite,
    drawn_late,
    host_resolver_rule,
    never_filled,
    started_browser,
)
from rebinding_support import PUBLIC_A, Response, World, html

from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools import content
from web_scraper.web_tools.browser import BrowserPage, BrowserRenderError, BrowserUnavailableError
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

SCRIPT = '<script src="/app.js"></script>'
LONG = "A page with something to say, and enough of it to be one. " * 5


def _shell(body: str = "", *, title: str = "The page") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}{SCRIPT}</body></html>"


def _drawn(text: str = DRAWN, *, title: str = "The page") -> str:
    return (
        f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{text}</p>"
        f"</main>{SCRIPT}</body></html>"
    )


class TestWhatIsAShell:
    @pytest.mark.parametrize(
        ("html_text", "text"),
        [
            (_shell('<div id="root"></div>'), ""),
            (_shell('<div id="app"></div>'), ""),
            (_shell('<div id="__next"></div>'), ""),
            (_shell(f'<div id="__next"><div class="loader">{LOADING}</div></div>'), LOADING),
            (_shell("<main><p>Loading...</p></main>"), "Loading..."),
            (
                _shell("<main><p>Please enable JavaScript to view this page.</p></main>"),
                "Please enable JavaScript to view this page.",
            ),
            (
                _shell("<main><p>You need to enable JavaScript to run this app.</p></main>"),
                "You need to enable JavaScript to run this app.",
            ),
            (_shell("<main><p>Yükleniyor</p></main>"), "Yükleniyor"),
            (_shell("<main><h1>Hi</h1></main>"), "Hi"),
            # A few lines a script is about to replace: under 200 characters, with scripts.
            (
                _shell("<main><p>" + "A few lines of text. " * 5 + "</p></main>"),
                "A few lines of text. " * 5,
            ),
        ],
    )
    def test_a_page_with_little_text_and_scripts_is_read_again_in_the_browser(
        self, html_text: str, text: str
    ) -> None:
        assert content.needs_browser_render(html_text, text) is True

    @pytest.mark.parametrize(
        ("html_text", "text"),
        [
            (_shell("<main><p>" + LONG + "</p></main>"), LONG),  # a page
            (
                "<html><body><div id='root'></div></body></html>",
                "",
            ),  # no script: nothing will draw it
            ("<html><body><p>Hi</p></body></html>", "Hi"),
            ("<html><body><p>Loading...</p></body></html>", "Loading..."),
        ],
    )
    def test_a_page_with_enough_text_or_without_scripts_is_not(
        self, html_text: str, text: str
    ) -> None:
        assert content.needs_browser_render(html_text, text) is False

    def test_the_bound_is_the_setting(self) -> None:
        text = "A few lines of text. " * 5  # 104 characters
        page = _shell("<main><p>" + text + "</p></main>")

        assert content.needs_browser_render(page, text, min_chars=200) is True
        assert content.needs_browser_render(page, text, min_chars=100) is False

    @pytest.mark.parametrize(
        "text",
        [
            "Loading...",
            "Loading…",
            "Loading animation while page loads",
            "Please wait while the page is loading",
            "Please enable JavaScript to view this page",
            "JavaScript is disabled in your browser",
            "Sayfa yükleniyor, lütfen bekleyin",
        ],
    )
    def test_a_placeholder_says_it_is_loading_or_needs_javascript(self, text: str) -> None:
        assert content.is_placeholder_text(text) is True
        assert content.shell_text(text) == "loading_placeholder"

    @pytest.mark.parametrize(
        "text",
        [
            "Loading docks and how they work",  # a word of its own in it
            "Welcome to our site, where we make small useful things for small teams of people",
            "How to enable JavaScript in Chrome: open the settings, then Privacy and security, then "
            "Site settings, and switch the JavaScript option on for the sites that need it.",
        ],
    )
    def test_a_text_with_something_of_its_own_to_say_is_not_a_placeholder(self, text: str) -> None:
        assert content.is_placeholder_text(text) is False

    def test_a_loading_line_inside_a_short_text_makes_it_a_shell(self) -> None:
        text = f"### Cookie Settings\n\nWe use cookies to enhance your browsing experience.\n\n{LOADING}"
        html_text = _shell("<main>x</main>")

        assert content.shell_text(text) == "loading_placeholder"
        assert content.shell_reason(html_text, text) == "loading_placeholder"

    def test_the_reason_a_page_is_a_shell(self) -> None:
        assert content.shell_reason(_shell('<div id="root"></div>'), "") == "empty_app_root"
        assert content.shell_reason(_shell("<main><p>Hi</p></main>"), "Hi") == "almost_empty"
        assert content.shell_reason(_shell("<main><p>" + LONG + "</p></main>"), LONG) is None
        # A short page that says something of its own is short, not a shell.
        short = "Thank you for your message. We will reply within two business days."
        assert content.shell_reason(_shell(f"<main><p>{short}</p></main>"), short) is None
        # No script, no shell: nothing is about to draw the page.
        assert content.shell_reason("<html><body><p>Hi</p></body></html>", "Hi") is None


class ScriptedBrowser:
    """A browser that answers each address with the page a test gave it, and counts the reads."""

    def __init__(self, pages: dict[str, Any]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def stop(self) -> None:
        return None

    async def render(self, url: str, *, post_load_wait_ms: Optional[int] = None) -> BrowserPage:
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return BrowserPage(
            value, url, 200, "chromium", settled_ms=1_800.0, text_chars=0, settle="stable"
        )


def _service(browser: Optional[ScriptedBrowser], **settings: Any) -> WebToolsService:
    base: dict[str, Any] = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": [],
        "search_resolve_dates": "off",
        "respect_robots": False,
        "soft_404_check": False,  # the loader is the subject here; test_web_missing_pages.py has the probe
    }
    base.update(settings)
    return WebToolsService(
        settings=WebToolsSettings(**base),
        fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
        browser=browser,  # type: ignore[arg-type]
        search_adapters={},
    )


def _site(world: World, pages: dict[str, str]) -> list[str]:
    """Serve *pages* (by path) at a public name of the fake network; the paths asked for are returned."""
    world.dns.answers("app.test", [PUBLIC_A])
    asked: list[str] = []

    def handler(request: Any) -> Response:
        asked.append(request.target)
        page = pages.get(request.target)
        return html(page, 200) if page is not None else html("not found", 404)

    world.internet.serve(PUBLIC_A, handler)
    return asked


URL = "https://app.test/post"


class TestAShellIsReadAgainInTheBrowser:
    @pytest.mark.asyncio
    async def test_a_page_drawn_by_scripts_is_read_in_the_browser_and_has_its_content(
        self, world: World
    ) -> None:
        asked = _site(world, {"/post": _shell(f'<div id="root">{LOADING}</div>', title="The post")})
        browser = ScriptedBrowser({URL: _drawn(title="The post")})
        service = _service(browser)

        try:
            answer = await service.scrape(URL)
        finally:
            await service.close()

        assert answer["fetched_with"] == "browser" and browser.calls == [URL]
        assert DRAWN.strip() in answer["markdown"] and LOADING not in answer["markdown"]
        assert answer["warnings"] == [] and "hint" not in answer and "looks_missing" not in answer
        assert answer["timings"]["settle_ms"] == 1_800.0 and "browser_ms" in answer["timings"]
        assert asked == ["/post"]

    @pytest.mark.asyncio
    async def test_a_page_with_enough_text_is_not_read_in_the_browser(self, world: World) -> None:
        _site(world, {"/post": _drawn(LONG)})
        browser = ScriptedBrowser({})
        service = _service(browser)

        try:
            answer = await service.scrape(URL)
        finally:
            await service.close()

        assert answer["fetched_with"] == "http" and browser.calls == []

    @pytest.mark.asyncio
    async def test_a_page_that_is_still_a_shell_says_so_and_is_not_a_confident_short_page(
        self, world: World
    ) -> None:
        _site(world, {"/post": _shell(f'<div id="root">{LOADING}</div>')})
        browser = ScriptedBrowser({URL: _shell(f'<div id="root">{LOADING}</div>')})
        service = _service(browser)

        try:
            answer = await service.scrape(URL)
            lean = await service.scrape(URL, lean=True)
        finally:
            await service.close()

        assert answer["warnings"] == ["rendered_shell"]
        assert "drawn by scripts" in answer["hint"] and "BROWSER_SETTLE_MAX_MS" in answer["hint"]
        assert "looks_missing" not in answer
        assert lean["warnings"] == ["rendered_shell"] and lean["hint"] == answer["hint"]
        # It is not kept: asked again, the page is read again, in the browser too (it may have filled).
        assert len(browser.calls) == 2

    @pytest.mark.asyncio
    async def test_a_shell_is_not_kept_by_the_page_cache_either(self, world: World) -> None:
        asked = _site(world, {"/post": _shell(f'<div id="root">{LOADING}</div>')})
        browser = ScriptedBrowser({URL: _shell(f'<div id="root">{LOADING}</div>')})
        service = _service(browser)

        try:
            first = await service.extract(URL, {"title": {}})
            second = await service.extract(URL, {"title": {}})
        finally:
            await service.close()

        assert first["warnings"] == second["warnings"] == ["rendered_shell"]
        assert (
            asked == ["/post", "/post"]
            and first["cache_hit"] is False
            and second["cache_hit"] is False
        )

    @pytest.mark.asyncio
    async def test_a_page_the_browser_filled_is_kept_for_the_next_call(self, world: World) -> None:
        asked = _site(world, {"/post": _shell(f'<div id="root">{LOADING}</div>')})
        browser = ScriptedBrowser({URL: _drawn()})
        service = _service(browser)

        try:
            await service.scrape(URL)
            extracted = await service.extract(URL, {"headline": {}})
        finally:
            await service.close()

        assert asked == ["/post"] and browser.calls == [URL]
        assert extracted["cache_hit"] is True and extracted["warnings"] == []

    @pytest.mark.asyncio
    async def test_a_shell_no_browser_read_says_it_needs_javascript(self, world: World) -> None:
        _site(world, {"/post": _shell('<div id="root"></div>')})
        service = _service(None, browser_enabled=False)

        try:
            answer = await service.scrape(URL)
        finally:
            await service.close()

        # (an empty page says empty_content as it always did)
        assert answer["warnings"] == ["empty_content", "needs_javascript"]
        assert "no browser read it" in answer["hint"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            BrowserUnavailableError("none"),
            BrowserRenderError("timeout", "Browser navigation failed"),
        ],
    )
    async def test_a_browser_that_fails_leaves_the_http_shell_flagged(
        self, world: World, failure: Exception
    ) -> None:
        _site(world, {"/post": _shell('<div id="root"></div>')})
        service = _service(ScriptedBrowser({URL: failure}))

        try:
            answer = await service.scrape(URL)
        finally:
            await service.close()

        assert any(warning.startswith("browser_render_failed") for warning in answer["warnings"])
        assert "needs_javascript" in answer["warnings"]

    @pytest.mark.asyncio
    async def test_a_page_a_search_reads_that_is_a_shell_says_so_in_its_content(
        self, world: World
    ) -> None:
        _site(world, {"/post": _shell(f'<div id="root">{LOADING}</div>')})
        service = _service(ScriptedBrowser({URL: _shell(f'<div id="root">{LOADING}</div>')}))
        answer = {"results": [{"url": URL, "title": "x"}], "timings": {"total_ms": 1.0}}

        try:
            await service._read_results(answer, (1, 4_000), archive=False)
        finally:
            await service.close()

        assert answer["results"][0]["content_warnings"] == ["rendered_shell"]


class TestOneLoader:
    """The same page is the same for every tool: what the browser drew is what each of them reads."""

    @pytest.mark.asyncio
    async def test_scrape_extract_crawl_and_map_see_the_page_the_browser_drew(
        self, world: World
    ) -> None:
        shell = _shell(f'<div id="root">{LOADING}</div>', title="The post")
        asked = _site(world, {"/post": shell, "/": shell})
        drawn = (
            "<html><head><title>The post</title></head><body><main><h1>The post</h1>"
            f"<p>{DRAWN}</p><a href='/more'>More</a></main><script src='/app.js'></script></body></html>"
        )
        browser = ScriptedBrowser({URL: drawn, "https://app.test/": drawn})
        service = _service(browser, max_crawl_depth=0)

        try:
            scraped = await service.scrape(URL)
            extracted = await service.extract(URL, {"headline": {}})
            mapped = await service.map(URL)
            job = await service.start_crawl(URL, {"max_pages": 1, "max_depth": 0})
            for _ in range(100):
                crawl = await service.get_crawl(job["job_id"])
                if crawl["status"] != "running" and crawl["status"] != "queued":
                    break
                await asyncio.sleep(0.02)
            crawled = await service.crawl_results(job["job_id"])
        finally:
            await service.close()

        assert DRAWN.strip() in scraped["markdown"]
        assert extracted["data"]["headline"] == "The post"
        # The map's links are those of the page as the browser drew it.
        assert "https://app.test/more" in mapped["urls"]
        # The crawl's page is the drawn one, in the same words.
        [page] = crawled["results"]
        assert DRAWN.strip() in page["markdown"] and page["fetched_with"] == "browser"
        # One HTTP read for scrape, extract and map (the page cache holds it); the crawl reads for itself.
        assert asked.count("/post") == 2

    @pytest.mark.asyncio
    async def test_a_crawl_page_that_is_a_shell_says_so_as_a_scrape_does(
        self, world: World
    ) -> None:
        shell = _shell(f'<div id="root">{LOADING}</div>', title="The post")
        _site(world, {"/post": shell})
        service = _service(ScriptedBrowser({URL: shell}))

        try:
            job = await service.start_crawl(URL, {"max_pages": 1, "max_depth": 0})
            for _ in range(100):
                crawl = await service.get_crawl(job["job_id"])
                if crawl["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            crawled = await service.crawl_results(job["job_id"])
        finally:
            await service.close()

        [page] = crawled["results"]
        assert page["warnings"] == ["rendered_shell"]


class TestTheSettingsAreTheServersOwn:
    def test_the_defaults(self) -> None:
        settings = WebToolsSettings()

        assert settings.browser_post_load_wait_ms == 500
        assert settings.browser_settle_max_ms == 8_000
        assert settings.browser_settle_stable_ms == 600
        assert settings.browser_render_min_chars == 200
        assert settings.soft_404_check is True

    def test_the_environment_sets_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_TOOLS_BROWSER_SETTLE_MAX_MS", "12000")
        monkeypatch.setenv("WEB_TOOLS_BROWSER_SETTLE_STABLE_MS", "900")
        monkeypatch.setenv("WEB_TOOLS_SOFT_404_CHECK", "off")

        settings = WebToolsSettings.from_env()

        assert settings.browser_settle_max_ms == 12_000
        assert settings.browser_settle_stable_ms == 900
        assert settings.soft_404_check is False

    def test_they_reach_the_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_TOOLS_BROWSER_SETTLE_MAX_MS", "5000")
        monkeypatch.setenv("WEB_TOOLS_BROWSER_SETTLE_STABLE_MS", "700")
        monkeypatch.setenv("WEB_TOOLS_BROWSER_RENDER_MIN_CHARS", "300")
        monkeypatch.setenv("WEB_TOOLS_BROWSER_POST_LOAD_WAIT_MS", "250")

        service = WebToolsService.from_env()

        assert service.browser is not None
        assert (service.browser.settle_max_ms, service.browser.settle_stable_ms) == (5_000, 700)
        assert (service.browser.shell_chars, service.browser.post_load_wait_ms) == (300, 250)


class TestARealBrowserThroughTheService:
    """``web_scrape`` of a page a script draws after 1.5 s, end to end: the fetcher is the real pinned one over
    the in-memory network, the browser is a real headless one, and the site is a real server on the loopback
    that the browser reaches by a name it is told to resolve to it."""

    @pytest.fixture
    def site(self) -> Any:
        site = LocalSite({"/late": drawn_late("Late post"), "/never": never_filled("Never post")})
        try:
            yield site
        finally:
            site.close()

    def _in_memory_copy(self, world: World, site: LocalSite) -> None:
        """The same pages at the name's public address in the fake network (what the HTTP fetch reads)."""
        world.dns.answers("late.test", [PUBLIC_A])
        world.internet.serve(
            PUBLIC_A,
            lambda request: (
                html(site.pages[request.target])
                if request.target in site.pages
                else html("not found", 404)
            ),
        )

    @pytest.mark.asyncio
    async def test_a_page_a_script_draws_after_one_and_a_half_seconds_is_scraped_with_its_content(
        self, world: World, site: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            browser_module,
            "_CHROMIUM_ARGS",
            (*browser_module._CHROMIUM_ARGS, host_resolver_rule("late.test")),
        )
        self._in_memory_copy(world, site)
        browser = await started_browser(post_load_wait_ms=500, settle_max_ms=3_000)
        service = WebToolsService(
            settings=WebToolsSettings(
                search_provider="fake",
                soft_404_check=False,
                respect_robots=False,
                browser_settle_max_ms=3_000,
            ),
            fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
            browser=browser,
            search_adapters={},
        )
        late = f"http://late.test:{site.port}/late"
        never = f"http://late.test:{site.port}/never"

        try:
            drawn = await service.scrape(late)
            stuck = await service.scrape(never)
        finally:
            await service.close()

        assert drawn["fetched_with"] == "browser" and drawn["title"] == "Late post"
        assert DRAWN.strip() in drawn["markdown"] and LOADING not in drawn["markdown"]
        assert drawn["warnings"] == [] and drawn["timings"]["settle_ms"] >= 1_500
        # The shell that never fills is a shell, and says so.
        assert stuck["warnings"] == ["rendered_shell"] and LOADING in stuck["markdown"]
        assert "hint" in stuck
