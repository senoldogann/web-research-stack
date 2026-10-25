"""The Camoufox that the Google search uses is held to the rules of the one that reads a page after a bot check.

1.4.4 (pull request #13) made Google the first fallback of a search and read it in a Camoufox of its own
(``web_scraper/stealth_camoufox.py``): one browser for the process, one context that every search reuses. That
browser was started with Camoufox's defaults (the download of what it lacks in the middle of a search, its calls to
Mozilla and Google on), its pages were not held to the address guard, and nothing bounded what a search did with a
page, so a page that stopped answering kept the one lock every search takes.

Now it is started from what is on the machine (what it lacks is fetched in the background, and the search is answered
by the next provider), its context is held to the guard before its first page exists, a page is read only if it ended
at an address the guard allows, the read is bounded, and the windows a page opens are closed. The fakes below stand for
Playwright and for the browser; ``tests/test_web_browser_camoufox.py`` has a real Camoufox on the loopback.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_web_browser_stealth import FakeFetch, Installed

from web_scraper import google_search as google_module
from web_scraper import stealth_camoufox as stealth
from web_scraper.google_search import GoogleSearchBlocked, GoogleSearcher
from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import BrowserUnavailableError

HANG = 10.0
RESULT = {"title": "A result", "href": "https://example.test/one", "snippet": "Something."}


class Page:
    """A Playwright page of the search, as far as the search touches it."""

    def __init__(
        self,
        *,
        url: str = "https://www.google.com/search?q=x",
        results: Any = None,
        hang: bool = False,
    ) -> None:
        self.url = url
        self.results = [RESULT] if results is None else results
        self.hang = hang
        self.closed = False
        self.visited: list[str] = []
        self.evaluated = 0
        self.popup_handlers: list[Any] = []
        self.blank_fails = False

    def on(self, event: str, handler: Any) -> None:
        if event == "popup":
            self.popup_handlers.append(handler)

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, wait_until: str = "load", timeout: int = 0) -> None:
        if url == "about:blank" and self.blank_fails:
            raise RuntimeError("the page did not answer")
        self.visited.append(url)

    async def wait_for_selector(self, selector: str, timeout: int) -> None:
        return None

    async def wait_for_function(self, expression: str, timeout: int) -> None:
        return None

    async def evaluate(self, script: str) -> Any:
        if self.hang:
            await (
                asyncio.Event().wait()
            )  # a page that blocks the browser: no call to it is ever answered
        self.evaluated += 1
        return self.results

    async def content(self) -> str:
        return "<html></html>"

    async def close(self) -> None:
        self.closed = True


class Context:
    def __init__(self, *, fail_on: str = "") -> None:
        self.events: list[str] = []
        self.cookies: list[dict[str, str]] = []
        self.pages: list[Page] = []
        self.closed = False
        self.fail_on = fail_on
        self.next_page: Any = Page

    async def route(self, pattern: str, handler: Any) -> None:
        self.events.append("route")

    async def route_web_socket(self, pattern: str, handler: Any) -> None:
        self.events.append("route_web_socket")
        if self.fail_on == "route_web_socket":
            raise RuntimeError("this Playwright cannot route web sockets")

    async def add_cookies(self, cookies: list[dict[str, str]]) -> None:
        self.events.append("add_cookies")
        self.cookies = cookies

    async def new_page(self) -> Page:
        self.events.append("new_page")
        page = self.next_page()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class Browser:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.events: list[str] = []
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self) -> Context:
        self.events.append("new_context")
        return self.context


class Manager:
    def __init__(self) -> None:
        self.exited = 0
        self.hang_on_exit = False

    async def __aexit__(self, *args: Any) -> None:
        self.exited += 1
        if self.hang_on_exit:
            await asyncio.Event().wait()


class Start:
    """``start_camoufox`` as the search finds it: what it is asked, and what it starts."""

    def __init__(self, context: Context) -> None:
        self.context = context
        self.calls: list[bool] = []
        self.browsers: list[Browser] = []
        self.managers: list[Manager] = []
        self.raises: Exception | None = None
        self.delay = 0.0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start = self

        async def start_camoufox(*, fetch: bool) -> tuple[Manager, Browser]:
            start.calls.append(fetch)
            if start.delay:
                await asyncio.sleep(start.delay)
            if start.raises is not None:
                raise start.raises
            manager, browser = Manager(), Browser(start.context)
            start.managers.append(manager)
            start.browsers.append(browser)
            return manager, browser

        monkeypatch.setattr(browser_module, "start_camoufox", start_camoufox)


@pytest.fixture(autouse=True)
def _a_search_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module keeps one browser for the process; a test starts from none, with a guard that refuses the
    loopback and allows what is public, and short limits."""
    for key in ("browser", "manager", "context", "spare", "idle"):
        monkeypatch.setitem(stealth._CAMOUFOX_STATE, key, None)
    monkeypatch.setitem(stealth._CAMOUFOX_STATE, "last", 0.0)
    monkeypatch.setitem(stealth._CAMOUFOX_STATE, "held", set())
    for name in ("_CAMOUFOX_LOCK", "_START_LOCK", "_GUARD"):
        monkeypatch.setattr(stealth, name, None)
    monkeypatch.setattr(stealth, "_LAST_REASON", "")
    monkeypatch.setattr(stealth, "_CLOSE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        browser_module, "browser_request_allowed", lambda url: "127.0.0.1" not in url
    )
    monkeypatch.delenv("WEB_TOOLS_BROWSER_STEALTH_FETCH", raising=False)


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> Start:
    start = Start(Context())
    start.install(monkeypatch)
    return start


class TestTheContextIsHeldBeforeItsFirstPage:
    @pytest.mark.asyncio
    async def test_the_requests_and_the_web_sockets_are_routed_then_the_cookies_set_then_the_page_made(
        self, started: Start
    ) -> None:
        page = await asyncio.wait_for(stealth.get_camoufox_page(), HANG)

        assert page is started.context.pages[0]
        assert started.browsers[0].events == ["new_context"]
        assert started.context.events == ["route", "route_web_socket", "add_cookies", "new_page"]
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_context_the_guard_cannot_hold_is_closed_and_no_page_is_made(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start = Start(Context(fail_on="route_web_socket"))
        start.install(monkeypatch)

        page = await asyncio.wait_for(stealth.get_camoufox_page(), HANG)

        assert page is None and start.context.closed is True
        assert "new_page" not in start.context.events and "add_cookies" not in start.context.events
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_one_context_serves_every_search_and_each_gets_a_page_of_its_own(
        self, started: Start
    ) -> None:
        first = await stealth.get_camoufox_page()
        second = await stealth.get_camoufox_page()

        assert first is not second and len(started.context.pages) == 2
        assert (
            started.browsers[0].events == ["new_context"]
            and started.context.events.count("route") == 1
        )
        assert len(started.calls) == 1
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_the_two_cookies_are_the_constants_in_the_code_and_for_google_only(
        self, started: Start
    ) -> None:
        await stealth.get_camoufox_page()

        assert started.context.cookies == stealth.GOOGLE_CONSENT_COOKIES
        assert {cookie["name"] for cookie in started.context.cookies} == {"CONSENT", "SOCS"}
        assert {cookie["domain"] for cookie in started.context.cookies} == {".google.com"}
        assert started.context.cookies[0]["value"] == "YES+cb"
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_window_a_page_opens_is_closed_and_the_next_search_s_page_is_not(
        self, started: Start
    ) -> None:
        first = await stealth.get_camoufox_page()
        second = await stealth.get_camoufox_page()
        window = Page()

        first.popup_handlers[0](window)  # the first page opens a window
        await asyncio.sleep(0.05)

        assert window.closed is True and first.closed is False and second.closed is False
        await stealth.close_camoufox()


class TestNothingIsDownloadedInASearch:
    @pytest.mark.asyncio
    async def test_a_browser_that_cannot_be_started_means_no_page_and_it_is_said_once(
        self, started: Start, caplog: pytest.LogCaptureFixture
    ) -> None:
        started.raises = BrowserUnavailableError(
            "Camoufox is not started: its browser is not installed"
        )

        with caplog.at_level("WARNING"):
            first = await stealth.get_camoufox_page()
            second = await stealth.get_camoufox_page()

        assert first is None and second is None and len(started.calls) == 2
        assert caplog.text.count("its browser is not installed") == 1  # not at every search

    @pytest.mark.asyncio
    async def test_the_setting_that_forbids_the_fetch_is_passed_on(
        self, started: Start, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await stealth.get_camoufox_page()
        await stealth.close_camoufox()
        monkeypatch.setenv("WEB_TOOLS_BROWSER_STEALTH_FETCH", "false")
        await stealth.get_camoufox_page()

        assert started.calls == [True, False]
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_with_the_real_start_what_is_lacking_is_fetched_in_the_background_and_nothing_is_started(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        fetch = FakeFetch(installed)
        fetch.install(monkeypatch)

        page = await asyncio.wait_for(stealth.get_camoufox_page(), HANG)
        again = await asyncio.wait_for(stealth.get_camoufox_page(), HANG)

        assert page is None and again is None
        assert (
            fetch.calls == 1 and installed.started == []
        )  # one fetch, and no browser started to fetch it
        fetch.release.set()
        await asyncio.wait_for(browser_module.camoufox_fetch.task, HANG)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_two_searches_at_once_start_one_browser(self, started: Start) -> None:
        started.delay = 0.1

        pages = await asyncio.wait_for(
            asyncio.gather(stealth.get_camoufox_page(), stealth.get_camoufox_page()), HANG
        )

        assert len(started.calls) == 1 and all(page is not None for page in pages)
        assert started.context.events.count("route") == 1
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_browser_that_has_gone_is_started_again(self, started: Start) -> None:
        await stealth.get_camoufox_page()
        started.browsers[0].connected = False

        page = await stealth.get_camoufox_page()

        assert page is not None and len(started.calls) == 2 and started.managers[0].exited == 1
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_browser_that_does_not_answer_a_close_is_given_up(self, started: Start) -> None:
        await stealth.get_camoufox_page()
        started.managers[0].hang_on_exit = True
        began = time.monotonic()

        await asyncio.wait_for(stealth.close_camoufox(), HANG)

        assert time.monotonic() - began < 3 and stealth._CAMOUFOX_STATE["browser"] is None


class TestTheSearchIsBoundedAndChecked:
    @pytest.fixture(autouse=True)
    def _short(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(google_module.config, "google_playwright_timeout_seconds", 0.3)
        monkeypatch.setattr(google_module, "_CAMOUFOX_READ_MARGIN_S", 0.3)

    @pytest.mark.asyncio
    async def test_a_page_that_is_read_gives_its_results_and_is_emptied_and_kept_for_the_next(
        self, started: Start
    ) -> None:
        results = await asyncio.wait_for(GoogleSearcher()._search_camoufox("query", 5), HANG)

        page = started.context.pages[0]
        assert [result["url"] for result in results] == ["https://example.test/one"]
        assert "q=query" in page.visited[0] and page.visited[-1] == "about:blank"
        assert page.closed is False and stealth._CAMOUFOX_STATE["spare"] is page
        assert not stealth._CAMOUFOX_STATE["held"]

        # The next search is read in the same page: Camoufox makes no page for it (about 3 s each).
        await asyncio.wait_for(GoogleSearcher()._search_camoufox("second", 5), HANG)

        assert started.context.events.count("new_page") == 1
        assert "q=second" in page.visited[-2] and page.visited[-1] == "about:blank"
        await stealth.close_camoufox()
        assert stealth._CAMOUFOX_STATE["spare"] is None and started.context.closed is True

    @pytest.mark.asyncio
    async def test_a_page_that_ended_at_an_address_the_guard_refuses_is_not_read(
        self, started: Start
    ) -> None:
        started.context.next_page = lambda: Page(url="http://127.0.0.1:8080/owner-only")

        with pytest.raises(GoogleSearchBlocked, match="not allowed"):
            await asyncio.wait_for(GoogleSearcher()._search_camoufox("query", 5), HANG)

        page = started.context.pages[0]
        assert page.evaluated == 0 and page.closed is True  # what the page held was never asked for
        assert stealth._CAMOUFOX_STATE["spare"] is None  # and it is not the next search's page
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_page_that_is_not_on_a_host_is_not_judged_by_one(self, started: Start) -> None:
        started.context.next_page = lambda: Page(url="about:blank")

        results = await asyncio.wait_for(GoogleSearcher()._search_camoufox("query", 5), HANG)

        assert len(results) == 1
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_page_that_stops_answering_ends_the_search_and_frees_the_lock(
        self, started: Start
    ) -> None:
        started.context.next_page = lambda: Page(hang=True)
        began = time.monotonic()

        with pytest.raises(GoogleSearchBlocked, match="in time"):
            await asyncio.wait_for(GoogleSearcher()._search_camoufox("query", 5), HANG)

        assert time.monotonic() - began < 5
        assert started.managers[0].exited == 1  # the browser that held the page is shut down
        assert stealth._CAMOUFOX_STATE["browser"] is None
        assert started.context.pages[0].closed is True and stealth._CAMOUFOX_STATE["spare"] is None
        # The lock every search takes is free, and the next search starts a browser of its own.
        started.context.next_page = Page
        started.context.events.clear()
        results = await asyncio.wait_for(GoogleSearcher()._search_camoufox("query", 5), HANG)
        assert len(results) == 1 and len(started.calls) == 2
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_the_warm_up_is_bounded_too(
        self, started: Start, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stealth, "_WARM_UP_TIMEOUT_SECONDS", 0.3)
        started.delay = 5.0
        began = time.monotonic()

        await asyncio.wait_for(stealth.warm_up_camoufox(), HANG)

        assert time.monotonic() - began < 3

    def test_the_second_launcher_that_nothing_used_is_gone(self) -> None:
        # A Camoufox started there would have had no guard on its pages.
        assert not hasattr(GoogleSearcher, "_camoufox_browser")
        assert not hasattr(GoogleSearcher, "_camoufox_page")
        assert not hasattr(google_module, "_CAMOUFOX_STATE")
        assert "AsyncCamoufox" not in Path(google_module.__file__).read_text(encoding="utf-8")
        assert "AsyncCamoufox" not in Path(stealth.__file__).read_text(encoding="utf-8")

    def test_the_search_module_does_not_import_a_browser_until_it_starts_one(self) -> None:
        source = Path(stealth.__file__).read_text(encoding="utf-8")

        assert "\nfrom web_scraper.web_tools" not in source and "\nimport playwright" not in source
        assert sys.modules["web_scraper.stealth_camoufox"] is stealth


async def _until_the_watch_shut_it_down() -> None:
    """Until the idle watch has shut the browser down and ended. Not until the state says no browser: the state is
    cleared before the closes are awaited, so a test that looked at the state saw the browser before it was closed
    (it failed so on the CI's slower machines)."""
    watch = stealth._CAMOUFOX_STATE["idle"]
    assert watch is not None
    await asyncio.wait({watch}, timeout=HANG)
    assert watch.done() and stealth._CAMOUFOX_STATE["browser"] is None


class TestAPageIsKeptForTheNextSearch:
    """A new page costs Camoufox about 3 s, and a search in a page kept from the last one about half a second
    (measured on 1.4.5 with the real browser). The page that served a whole search is emptied and kept, one at a
    time; any other is closed."""

    @pytest.mark.asyncio
    async def test_of_two_pages_handed_back_one_is_kept_and_the_other_closed(
        self, started: Start
    ) -> None:
        first = await stealth.get_camoufox_page()
        second = await stealth.get_camoufox_page()

        await stealth.release_page(first)
        await stealth.release_page(second)

        assert stealth._CAMOUFOX_STATE["spare"] is first and first.closed is False
        assert second.closed is True and not stealth._CAMOUFOX_STATE["held"]
        assert await stealth.get_camoufox_page() is first  # the kept page, and no new one
        assert started.context.events.count("new_page") == 2
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_page_that_cannot_be_emptied_is_closed(self, started: Start) -> None:
        page = await stealth.get_camoufox_page()
        page.blank_fails = True

        await stealth.release_page(page)

        assert page.closed is True and stealth._CAMOUFOX_STATE["spare"] is None
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_a_page_of_a_browser_shut_down_meanwhile_is_closed_and_not_kept(
        self, started: Start
    ) -> None:
        page = await stealth.get_camoufox_page()
        await stealth.close_camoufox()

        await stealth.release_page(page)

        assert page.closed is True and stealth._CAMOUFOX_STATE["spare"] is None

    @pytest.mark.asyncio
    async def test_a_kept_page_that_was_closed_is_not_handed_out(self, started: Start) -> None:
        page = await stealth.get_camoufox_page()
        await stealth.release_page(page)
        page.closed = True  # its process went

        again = await stealth.get_camoufox_page()

        assert again is not page and started.context.events.count("new_page") == 2
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_the_warm_up_leaves_its_page_for_the_first_search(self, started: Start) -> None:
        await asyncio.wait_for(stealth.warm_up_camoufox(), HANG)
        await asyncio.wait_for(GoogleSearcher()._search_camoufox("query", 5), HANG)

        page = started.context.pages[0]
        assert started.context.events.count("new_page") == 1
        assert page.visited[0] == "https://www.google.com/ncr" and "q=query" in page.visited[2]
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_the_adapter_does_not_warm_up_before_its_first_search(self) -> None:
        # The warm-up made a page of its own and closed it, so the search made a page again; and a warm-up past
        # its time shut down the browser the search was about to use (77 and 80 s for a first search, measured).
        from web_scraper.web_tools.search import GoogleSearchAdapter

        class Searcher:
            def __init__(self) -> None:
                self.warmed = 0

            async def warm_up(self) -> None:
                self.warmed += 1

            async def search(self, query: str, num_results: int = 10, **kwargs: Any) -> list[dict]:
                return [{"title": "T", "url": "https://x.example/", "snippet": ""}]

        searcher = Searcher()
        results = await GoogleSearchAdapter(searcher).search("python", 5)  # type: ignore[arg-type]

        assert len(results) == 1 and searcher.warmed == 0


class TestAnIdleBrowserIsShutDown:
    """The idle time was looked at only when the next search came, so a browser of about 2.5 GB stayed up for as
    long as none came. A watch of its own shuts it down; never while a search holds one of its pages or reads."""

    @pytest.fixture(autouse=True)
    def _short(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stealth, "_CAMOUFOX_IDLE_SECONDS", 0.2)
        monkeypatch.setattr(stealth, "_IDLE_RECHECK_SECONDS", 0.05)

    @pytest.mark.asyncio
    async def test_a_browser_no_search_uses_is_shut_down_without_a_next_search(
        self, started: Start
    ) -> None:
        page = await stealth.get_camoufox_page()
        await stealth.release_page(page)

        await _until_the_watch_shut_it_down()

        assert started.managers[0].exited == 1 and started.context.closed is True
        assert stealth._CAMOUFOX_STATE["spare"] is None and stealth._CAMOUFOX_STATE["idle"] is None

    @pytest.mark.asyncio
    async def test_it_is_not_shut_down_while_a_search_holds_a_page(self, started: Start) -> None:
        page = await stealth.get_camoufox_page()

        await asyncio.sleep(0.6)  # three idle times

        assert stealth._CAMOUFOX_STATE["browser"] is not None and started.managers[0].exited == 0
        await stealth.release_page(page)
        await _until_the_watch_shut_it_down()
        assert started.managers[0].exited == 1

    @pytest.mark.asyncio
    async def test_it_is_not_shut_down_while_a_read_holds_the_lock(self, started: Start) -> None:
        await stealth.release_page(await stealth.get_camoufox_page())

        async with stealth.camoufox_lock():
            await asyncio.sleep(0.6)
            assert stealth._CAMOUFOX_STATE["browser"] is not None

        await _until_the_watch_shut_it_down()
        assert started.managers[0].exited == 1

    @pytest.mark.asyncio
    async def test_a_search_restarts_the_idle_time(
        self, started: Start, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stealth, "_CAMOUFOX_IDLE_SECONDS", 2.0)
        await stealth.release_page(await stealth.get_camoufox_page())
        await asyncio.sleep(1.3)
        await stealth.release_page(await stealth.get_camoufox_page())
        await asyncio.sleep(1.3)  # 2.6 s since the first search, 1.3 s since the last

        assert stealth._CAMOUFOX_STATE["browser"] is not None
        await stealth.close_camoufox()

    @pytest.mark.asyncio
    async def test_closing_the_browser_stops_its_watch_and_the_next_one_is_watched_too(
        self, started: Start
    ) -> None:
        await stealth.release_page(await stealth.get_camoufox_page())
        watch = stealth._CAMOUFOX_STATE["idle"]

        await stealth.close_camoufox()

        assert watch is not None and watch.done() and stealth._CAMOUFOX_STATE["idle"] is None
        await stealth.release_page(await stealth.get_camoufox_page())
        await _until_the_watch_shut_it_down()
        assert len(started.managers) == 2 and started.managers[1].exited == 1
