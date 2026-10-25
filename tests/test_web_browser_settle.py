"""A page is read when it has settled, not after a fixed 500 ms.

1.3.1 read a page 500 ms after DOMContentLoaded, so a page whose script draws its content a second or three later
(senoldogan.dev's blog: 4,366 characters after four seconds, "Loading animation while page loads" after half
one) came back as the shell. Now, after the load event, the network is waited for to go quiet (bounded), then the
visible text until it has stopped growing for about 600 ms and is more than a loading placeholder, no sooner than
``post_load_wait_ms`` and no later than ``settle_max_ms`` (and never past ``browser_timeout_seconds`` for the whole
render). The first half of this file runs the waiting against a fake page and a fake clock, so each rule is tested
alone and at once; the second runs a real headless browser against a real local server, and is skipped where there
is none.
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from browser_support import (
    DRAWN,
    LOADING,
    LocalSite,
    complete,
    drawn_late,
    host_resolver_rule,
    never_filled,
    started_browser,
)

from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import BrowserRenderError, HeadlessBrowserManager
from web_scraper.web_tools.content import extract_page

LONG = "A page with something to say, and enough of it to be one. " * 5


class Clock:
    """Time that moves only when the code under test sleeps or waits, so a test of eight seconds takes none."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class FakePage:
    """A Playwright page whose text is the text of the moment: *timeline* is (seconds after the load, text)."""

    def __init__(
        self,
        clock: Clock,
        timeline: list[tuple[float, str]],
        *,
        idle_after: float = 0.0,
        unreadable_reads: int = 0,
        content_failures: int = 0,
    ) -> None:
        self.clock = clock
        self.timeline = sorted(timeline)
        self.idle_after = idle_after
        self.unreadable_reads = unreadable_reads
        self.content_failures = content_failures
        self.loaded_at = clock.now
        self.routes: list[tuple[str, Any]] = []
        self.states: list[tuple[str, int]] = []
        self.url = "https://page.test/"

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, wait_until: str, timeout: int) -> Any:
        self.loaded_at = self.clock.now
        return types.SimpleNamespace(status=200)

    def text(self) -> str:
        age = self.clock.now - self.loaded_at
        shown = ""
        for at, value in self.timeline:
            if age >= at:
                shown = value
        return shown

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        self.states.append((state, timeout))
        if state == "networkidle":
            needed = self.idle_after - (self.clock.now - self.loaded_at)
            if needed > timeout / 1000:
                self.clock.now += timeout / 1000
                raise TimeoutError("the network did not go quiet")
            self.clock.now += max(needed, 0.0)

    async def evaluate(self, expression: str, limit: int = 200) -> dict[str, Any]:
        if self.unreadable_reads:
            self.unreadable_reads -= 1
            raise RuntimeError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        shown = self.text()
        return {"chars": len(shown), "head": shown[:limit]}

    async def content(self) -> str:
        if self.content_failures:
            self.content_failures -= 1
            raise RuntimeError("the page is navigating and changing the content")
        return f"<html><head><title>T</title></head><body><main>{self.text()}</main></body></html>"


class FakeContext:
    """A Playwright browser context: what the guard is held to is routed on it, before its page is made."""

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.routes: list[tuple[str, Any]] = []
        self.socket_routes: list[tuple[str, Any]] = []
        self.listeners: list[tuple[str, Any]] = []
        self.made_page_after: list[str] = []

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def route_web_socket(self, pattern: str, handler: Any) -> None:
        self.socket_routes.append((pattern, handler))

    def on(self, event: str, handler: Any) -> None:
        self.listeners.append((event, handler))

    async def new_page(self) -> FakePage:
        self.made_page_after = [f"route:{len(self.routes)}", f"sockets:{len(self.socket_routes)}"]
        return self.page

    async def close(self) -> None:
        return None


class FakeChrome:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.contexts: list[FakeContext] = []

    def is_connected(self) -> bool:
        return True

    async def new_context(self, **options: Any) -> FakeContext:
        self.contexts.append(FakeContext(self.page))
        return self.contexts[-1]


@pytest.fixture(autouse=True)
def _every_target_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "browser_request_allowed", lambda url: True)


def _render_with(page: FakePage, clock: Clock, **settings: Any) -> Any:
    manager = HeadlessBrowserManager(**settings)
    manager._browser = FakeChrome(page)
    manager._engine = "chromium"
    manager._clock = clock
    manager._sleep = clock.sleep
    return manager, manager.render("https://page.test/")


class TestWaitingForAPageToSettle:
    @pytest.mark.asyncio
    async def test_content_a_script_draws_after_one_and_a_half_seconds_is_read(self) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, LOADING), (1.5, LONG)])
        _, rendering = _render_with(page, clock)

        rendered = await rendering

        assert LONG in rendered.html and LOADING not in rendered.html
        assert rendered.settle == "stable" and rendered.text_chars == len(LONG)
        # The text appeared at 1.5 s and was then watched for 600 ms (and looked at every 100 ms).
        assert 2_100 <= rendered.settled_ms <= 2_400

    @pytest.mark.asyncio
    async def test_a_shell_that_never_fills_is_waited_for_up_to_the_cap_and_no_longer(self) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, LOADING)])
        _, rendering = _render_with(page, clock, settle_max_ms=3_000)

        rendered = await rendering

        assert LOADING in rendered.html
        assert rendered.settle == "cap"
        assert 2_900 <= rendered.settled_ms <= 3_100

    @pytest.mark.asyncio
    async def test_nothing_is_waited_for_past_the_renders_own_timeout(self) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, "")])
        _, rendering = _render_with(page, clock, timeout_seconds=2, settle_max_ms=60_000)

        rendered = await rendering

        # browser_timeout_seconds bounds the whole render: the wait ends before it, with time to read the page.
        assert rendered.settle == "cap" and rendered.settled_ms <= 1_600

    @pytest.mark.asyncio
    async def test_post_load_wait_ms_is_the_least_it_waits(self) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, LONG)])
        _, rendering = _render_with(page, clock, post_load_wait_ms=1_500)

        assert (await rendering).settled_ms >= 1_500

        clock2 = Clock()
        _, quicker = _render_with(
            FakePage(clock2, [(0.0, LONG)]), clock2, post_load_wait_ms=0, settle_stable_ms=600
        )
        assert 600 <= (await quicker).settled_ms < 900

    @pytest.mark.asyncio
    async def test_text_that_is_still_growing_is_waited_for(self) -> None:
        clock = Clock()
        steps = [(0.0, "a " * 130), (0.4, "a " * 160), (0.8, "a " * 190), (1.2, "a " * 220)]
        page = FakePage(clock, steps)
        _, rendering = _render_with(page, clock)

        rendered = await rendering

        assert rendered.text_chars == len("a " * 220)
        # It stopped growing at 1.2 s; 600 ms of that is what ends the wait.
        assert 1_750 <= rendered.settled_ms <= 2_050 and rendered.settle == "stable"

    @pytest.mark.asyncio
    async def test_a_short_page_that_says_something_of_its_own_does_not_wait_for_the_cap(
        self,
    ) -> None:
        clock = Clock()
        page = FakePage(
            clock, [(0.0, "Thank you for your message. We will reply within two days.")]
        )
        _, rendering = _render_with(page, clock)

        rendered = await rendering

        assert rendered.settle == "stable" and rendered.settled_ms < 1_500

    @pytest.mark.asyncio
    async def test_a_cookie_notice_above_a_loader_is_still_a_shell_that_is_waited_for(self) -> None:
        clock = Clock()
        page = FakePage(
            clock,
            [
                (
                    0.0,
                    f"Cookie Settings\nWe use cookies to enhance your browsing experience.\n{LOADING}",
                )
            ],
        )
        _, rendering = _render_with(page, clock, settle_max_ms=2_000)

        assert (await rendering).settle == "cap"

    @pytest.mark.asyncio
    async def test_the_network_is_waited_for_a_bounded_time_and_a_page_that_never_rests_is_read(
        self,
    ) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, LONG)], idle_after=3_600.0)
        _, rendering = _render_with(page, clock)

        rendered = await rendering

        waited = dict(page.states)
        assert 0 < waited["networkidle"] <= 3_000
        assert rendered.settle == "stable" and rendered.settled_ms < 4_500

    @pytest.mark.asyncio
    async def test_a_page_that_is_navigating_is_asked_again(self) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, LONG)], unreadable_reads=4, content_failures=2)
        _, rendering = _render_with(page, clock)

        rendered = await rendering

        assert LONG in rendered.html and rendered.settle == "stable"

    @pytest.mark.asyncio
    async def test_a_page_whose_content_cannot_be_read_is_a_navigation_failure(self) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, LONG)], content_failures=10)
        _, rendering = _render_with(page, clock)

        with pytest.raises(BrowserRenderError) as exc:
            await rendering

        assert exc.value.code == "navigation_failed"

    @pytest.mark.asyncio
    async def test_every_request_of_the_page_is_still_held_to_the_guard(self) -> None:
        clock = Clock()
        page = FakePage(clock, [(0.0, LONG)])
        manager, rendering = _render_with(page, clock)

        await rendering

        # On the context, not the page (a window the page opens is a page of the context), and before the page
        # exists; its web sockets too, which a route does not see.
        (context,) = manager._browser.contexts
        assert context.routes == [("**/*", manager._guard.route)]
        assert context.socket_routes == [("**/*", manager._guard.route_web_socket)]
        assert context.made_page_after == ["route:1", "sockets:1"]
        assert page.routes == []
        assert [event for event, _ in context.listeners] == ["page"]

    @pytest.mark.asyncio
    async def test_a_target_that_is_not_public_is_refused_before_any_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(browser_module, "browser_request_allowed", lambda url: False)
        clock = Clock()
        page = FakePage(clock, [(0.0, LONG)])
        manager, rendering = _render_with(page, clock)

        with pytest.raises(BrowserRenderError) as exc:
            await rendering

        assert exc.value.code == "unsafe_target" and page.states == []
        assert clock.now == 1_000.0


class TestARealBrowserAgainstARealSite:
    """What a browser sees and when: a local server on the loopback, a real headless browser."""

    @pytest.fixture
    def site(self) -> Any:
        site = LocalSite(
            {
                "/late": drawn_late(),
                "/never": never_filled(),
                "/complete": complete(),
            }
        )
        try:
            yield site
        finally:
            site.close()

    @pytest.fixture(autouse=True)
    def _the_loopback_is_allowed_to_this_test_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The guard refuses the loopback, rightly; the site of this test is the only one it is lifted for.
        monkeypatch.setattr(
            browser_module,
            "browser_request_allowed",
            lambda url: url.startswith("http://127.0.0.1:"),
        )

    @pytest.mark.asyncio
    async def test_a_page_is_read_when_it_has_settled_and_not_before(self, site: LocalSite) -> None:
        manager = await started_browser(post_load_wait_ms=500, settle_max_ms=2_500)
        try:
            late = await manager.render(site.url("/late"))
            never = await manager.render(site.url("/never"))
            whole = await manager.render(site.url("/complete"))
            # What 1.3.1 did, said in this version's terms: wait 500 ms and no more.
            manager.settle_max_ms, manager.settle_stable_ms, manager.shell_chars = 0, 0, 20
            old_way = await manager.render(site.url("/late"))
        finally:
            await manager.stop()

        # A page whose script draws after 1.5 s is read with its content...
        page = extract_page(late.html, late.final_url)
        assert DRAWN.strip() in page.markdown and LOADING not in page.markdown
        assert page.title == "Drawn late"
        assert (
            late.settle == "stable" and late.settled_ms >= 600
        )  # (the text was watched for 600 ms)
        # ...where the fixed wait read the shell,
        assert LOADING in extract_page(old_way.html, old_way.final_url).markdown
        # a shell that never fills is waited for up to the cap and then read as what it is,
        assert never.settle == "cap" and never.settled_ms >= 2_400
        assert LOADING in extract_page(never.html, never.final_url).markdown
        # and a page that is whole as it arrives does not wait for the cap.
        assert whole.settle == "stable"  # it ended before the cap of 2.5 s: it did not wait for it

    @pytest.mark.asyncio
    async def test_the_loopback_is_not_reachable_without_the_exception_of_these_tests(
        self, site: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.undo()  # the guard as it ships
        manager = HeadlessBrowserManager()

        with pytest.raises(BrowserRenderError) as exc:
            await manager.render(site.url("/complete"))

        assert exc.value.code == "unsafe_target" and site.seen == []


def test_the_chrome_flag_that_maps_a_name_to_the_loopback() -> None:
    assert host_resolver_rule("late.test") == "--host-resolver-rules=MAP late.test 127.0.0.1"
