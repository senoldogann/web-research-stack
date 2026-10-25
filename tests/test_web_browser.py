"""Tests for headless-only browser selection and request guarding."""

from __future__ import annotations

import asyncio
import sys
import time
import types
from typing import Any

import pytest
from rebinding_support import LOOPBACK, PUBLIC_A, World

from web_scraper.playwright_scrapers import PlaywrightScraper
from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import (
    BrowserPage,
    BrowserRenderError,
    BrowserUnavailableError,
    HeadlessBrowserManager,
    RequestGuard,
    browser_launch_candidates,
    browser_request_allowed,
)


def test_auto_candidates_are_always_headless_with_isolated_profile_flags() -> None:
    candidates = browser_launch_candidates("auto")

    assert [candidate.engine for candidate in candidates] == ["chrome", "chromium", "camoufox"]
    for candidate in candidates:
        assert candidate.headless is True
        if candidate.engine in {"chrome", "chromium"}:
            assert candidate.args
            assert "--user-data-dir" not in " ".join(candidate.args)


def test_explicit_camoufox_preference_does_not_include_chrome() -> None:
    candidates = browser_launch_candidates("camoufox")

    assert [candidate.engine for candidate in candidates] == ["camoufox"]


def test_private_and_unsupported_targets_are_blocked() -> None:
    assert browser_request_allowed("https://93.184.216.34/") is True
    assert browser_request_allowed("http://127.0.0.1/admin") is False
    assert browser_request_allowed("http://localhost/admin") is False
    assert browser_request_allowed("file:///etc/passwd") is False
    assert browser_request_allowed("data:text/html,hello") is False


def test_the_address_forms_that_carry_a_private_ipv4_are_blocked() -> None:
    for address in (
        "[::127.0.0.1]",
        "[64:ff9b::7f00:1]",
        "[2002:c0a8:101::1]",
        "[::ffff:10.0.0.1]",
    ):
        assert browser_request_allowed(f"http://{address}/") is False
    assert browser_request_allowed("http://0177.0.0.1/") is False


class TestWhatIsCheckedWhenAPageIsRendered:
    """The browser resolves names itself and cannot be pinned: a page is rendered only if its host resolves to
    public addresses only at the moment of the check, and so is every request the page makes."""

    @pytest.mark.asyncio
    async def test_a_page_whose_host_resolves_to_a_private_address_is_not_rendered(
        self, world: World
    ) -> None:
        world.dns.answers("rebind.test", [LOOPBACK])
        manager = HeadlessBrowserManager()

        with pytest.raises(BrowserRenderError) as exc:
            await manager.render(f"http://rebind.test:{world.local.port}/admin")

        assert exc.value.code == "unsafe_target"
        assert (
            world.local.seen == [] and manager.running is False
        )  # refused before a browser was started

    @pytest.mark.asyncio
    async def test_a_name_that_rebinds_is_refused_at_the_second_look(self, world: World) -> None:
        # The check is made when the render is asked for and again for each request of the page, so a name that
        # answers differently to the browser's own lookup after the first is caught by the second.
        world.dns.rebinds("rebind.test", PUBLIC_A)
        manager = HeadlessBrowserManager()

        assert await manager._guard.allowed_now("http://rebind.test/") is True
        assert await manager._guard.allowed_now("http://rebind.test/") is False

    @pytest.mark.asyncio
    async def test_the_check_does_not_block_the_event_loop(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def slow(url: str) -> bool:
            time.sleep(0.2)
            return False

        monkeypatch.setattr(browser_module, "browser_request_allowed", slow)
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(tick())
        try:
            with pytest.raises(BrowserRenderError):
                await HeadlessBrowserManager().render("http://slow.test/")
        finally:
            ticker.cancel()

        assert ticks >= 5

    @pytest.mark.asyncio
    async def test_a_check_that_fails_is_a_refusal(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(url: str) -> bool:
            raise RuntimeError("the resolver fell over")

        monkeypatch.setattr(browser_module, "browser_request_allowed", broken)

        assert await HeadlessBrowserManager()._guard.allowed_now("http://example.test/") is False

    @pytest.mark.asyncio
    async def test_an_allow_is_kept_for_a_short_time_and_a_refusal_for_a_long_one(
        self, world: World
    ) -> None:
        world.dns.answers("good.test", [PUBLIC_A])
        world.dns.answers("bad.test", [LOOPBACK])
        manager = HeadlessBrowserManager()

        assert await manager._guard.allowed("http://good.test/a") is True
        assert await manager._guard.allowed("http://bad.test/a") is False
        # Within the time they are kept, the host is not looked up again.
        assert await manager._guard.allowed("http://good.test/b") is True
        assert await manager._guard.allowed("http://bad.test/b") is False
        assert world.dns.lookups == {"good.test": 1, "bad.test": 1}

        kept = {
            host: entry.expires_at - time.monotonic()
            for host, entry in manager._guard._hosts.items()
        }
        assert kept["good.test"] <= browser_module._ALLOWED_KEPT_SECONDS
        assert kept["bad.test"] > browser_module._ALLOWED_KEPT_SECONDS
        assert browser_module._ALLOWED_KEPT_SECONDS <= 60


class _Route:
    """What Playwright hands a route handler: the request, and what can be done with it."""

    def __init__(self, url: str) -> None:
        self.request = types.SimpleNamespace(url=url)
        self.done: list[str] = []

    async def continue_(self) -> None:
        self.done.append("continue")

    async def abort(self, reason: str) -> None:
        self.done.append(f"abort:{reason}")


class TestTheGuardOfAPage:
    @pytest.mark.asyncio
    async def test_a_request_to_a_public_host_goes_on_and_one_to_a_private_host_is_aborted(
        self, world: World
    ) -> None:
        world.dns.answers("public.test", [PUBLIC_A])
        world.dns.answers("private.test", [LOOPBACK])
        guard = RequestGuard()
        routes = [
            _Route("https://public.test/app.js"),
            _Route("http://private.test/pixel"),
            _Route("http://127.0.0.1:8080/"),
            _Route("http://[::127.0.0.1]/"),
            _Route("file:///etc/passwd"),
        ]

        for route in routes:
            await guard.route(route)

        assert [route.done for route in routes] == [
            ["continue"],
            ["abort:blockedbyclient"],
            ["abort:blockedbyclient"],
            ["abort:blockedbyclient"],
            ["abort:blockedbyclient"],
        ]

    @pytest.mark.asyncio
    async def test_a_hosts_verdict_is_kept_briefly_and_the_host_is_looked_at_again_after(
        self, world: World
    ) -> None:
        world.dns.rebinds("rebind.test", PUBLIC_A)
        guard = RequestGuard()
        first, soon, later = (
            _Route("http://rebind.test/a"),
            _Route("http://rebind.test/b"),
            _Route("http://rebind.test/c"),
        )

        await guard.route(first)
        await guard.route(soon)  # within the time a yes is kept: not looked up again
        guard._hosts["rebind.test"].expires_at = 0.0  # the time has passed
        await guard.route(later)  # looked up again: the name answers the loopback now

        assert first.done == ["continue"] and soon.done == ["continue"]
        assert later.done == ["abort:blockedbyclient"]
        assert world.dns.lookups == {"rebind.test": 2}

    @pytest.mark.asyncio
    async def test_the_table_of_verdicts_does_not_grow_without_bound(self, world: World) -> None:
        world.dns.answers("fresh.test", [PUBLIC_A])
        guard = RequestGuard()
        for index in range(browser_module._HOSTS_KEPT_MAX):
            guard._hosts[f"old{index}.test"] = browser_module._AllowedHost(True, expires_at=0.0)

        assert await guard.allowed("http://fresh.test/") is True

        assert set(guard._hosts) == {"fresh.test"}  # the verdicts that had run out are gone


class _Page:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any]] = []

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def set_default_timeout(self, timeout: float) -> None:
        return None


class _Context:
    """A Playwright browser context that records what is routed on it and when its page was made."""

    def __init__(self, page: _Page, *, sockets: bool = True) -> None:
        self.page = page
        self.routes: list[tuple[str, Any]] = []
        self.socket_routes: list[tuple[str, Any]] = []
        self.listeners: list[tuple[str, Any]] = []
        self.routed_before_the_page = False
        if sockets:
            self.route_web_socket = self._route_web_socket

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def _route_web_socket(self, pattern: str, handler: Any) -> None:
        self.socket_routes.append((pattern, handler))

    def on(self, event: str, handler: Any) -> None:
        self.listeners.append((event, handler))

    async def new_page(self) -> _Page:
        self.routed_before_the_page = bool(self.routes and self.socket_routes)
        return self.page


def _fake_playwright(monkeypatch: pytest.MonkeyPatch, context: _Context) -> list[str]:
    """Playwright as the older scraper imports it, around *context*; the list says what was shut down."""
    log: list[str] = []

    class Browser:
        async def new_context(self, **options: Any) -> _Context:
            assert options["service_workers"] == "block"
            return context

        async def close(self) -> None:
            log.append("browser closed")

    class Started:
        chromium = types.SimpleNamespace(launch=lambda **options: _launched(Browser()))

        async def stop(self) -> None:
            log.append("playwright stopped")

    async def _launched(browser: Browser) -> Browser:
        return browser

    async def stealth(page: Any) -> None:
        return None

    async def start() -> Started:
        return Started()

    api = types.ModuleType("playwright.async_api")
    api.async_playwright = lambda: types.SimpleNamespace(start=start)  # type: ignore[attr-defined]
    stealth_module = types.ModuleType("playwright_stealth")
    stealth_module.stealth_async = stealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    monkeypatch.setitem(sys.modules, "playwright_stealth", stealth_module)
    return log


class TestTheOlderPlaywrightScraper:
    """It checked the address it was given and nothing the page then asked for."""

    @pytest.mark.asyncio
    async def test_its_page_is_routed_through_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _Page()
        context = _Context(page)
        _fake_playwright(monkeypatch, context)

        async with PlaywrightScraper() as scraper:
            # The context is held (a window the page opens is a page of it), its web sockets too, and
            # all of it before the page is made.
            assert context.routes == [("**/*", scraper._guard.route)]
            assert context.socket_routes == [("**/*", scraper._guard.route_web_socket)]
            assert context.routed_before_the_page is True
            assert [event for event, _ in context.listeners] == ["page"]
            assert page.routes == []

    @pytest.mark.asyncio
    async def test_a_playwright_that_cannot_hold_sockets_is_not_used_and_leaves_nothing_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _Context(_Page(), sockets=False)
        log = _fake_playwright(monkeypatch, context)

        with pytest.raises(BrowserUnavailableError, match="web sockets"):
            async with PlaywrightScraper():
                raise AssertionError("the scraper must not be entered")

        # An __aenter__ that raises has no __aexit__: the browser it had started is shut down by it.
        assert log == ["browser closed", "playwright stopped"] and context.routes == []

    @pytest.mark.asyncio
    async def test_it_still_refuses_an_address_it_is_given_before_it_opens_a_page(
        self, world: World
    ) -> None:
        scraper = PlaywrightScraper()

        for url in ("http://127.0.0.1/", "http://[64:ff9b::7f00:1]/", "http://0177.0.0.1/"):
            result = await scraper.scrape(url)
            assert result.error is not None and "unsafe" in result.error.lower()


@pytest.fixture
def camoufox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``import camoufox`` works (the package is an extra, and the CI job does not install it)."""
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))


class TestStealthEvasion:
    def test_manager_has_stealth_when_camoufox_is_installed(
        self, camoufox_importable: None
    ) -> None:
        manager = HeadlessBrowserManager()
        assert manager.has_stealth is True

    def test_manager_has_no_stealth_without_camoufox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "camoufox", None)  # an import of it fails
        manager = HeadlessBrowserManager()
        assert manager.has_stealth is False

    def test_manager_snapshot_reports_stealth_capabilities(self, camoufox_importable: None) -> None:
        manager = HeadlessBrowserManager()
        snap = manager.snapshot()
        assert "has_stealth" in snap
        assert snap["has_stealth"] is True
        assert "stealth_active" in snap
        assert snap["stealth_active"] is False

    @pytest.mark.asyncio
    async def test_render_stealth_refuses_unsafe_target_before_starting_engine(self) -> None:
        manager = HeadlessBrowserManager()
        with pytest.raises(BrowserRenderError) as exc:
            await manager.render_stealth("http://127.0.0.1:8080/private")
        assert exc.value.code == "unsafe_target"

    @pytest.mark.asyncio
    async def test_primary_engine_escalates_to_stealth_on_interstitial_challenge(
        self, monkeypatch: pytest.MonkeyPatch, camoufox_importable: None
    ) -> None:
        manager = HeadlessBrowserManager()
        manager._engine = "chrome"
        manager._browser = types.SimpleNamespace(is_connected=lambda: True)
        monkeypatch.setattr(
            manager._guard, "allowed_now", lambda url: asyncio.sleep(0, result=True)
        )

        stealth_called = False

        async def fake_render_stealth(url: str, **kwargs: Any) -> BrowserPage:
            nonlocal stealth_called
            stealth_called = True
            return browser_module.BrowserPage(
                html="<html><body><h1>Solved Title</h1><p>Real content after challenge</p></body></html>",
                final_url=url,
                status_code=200,
                engine="camoufox",
            )

        async def fake_render_with_browser(
            browser: Any, engine: str, url: str, **kwargs: Any
        ) -> BrowserPage:
            return browser_module.BrowserPage(
                html='<html><body><div class="cf-turnstile">Verify you are human</div></body></html>',
                final_url=url,
                status_code=403,
                engine="chrome",
            )

        monkeypatch.setattr(manager, "render_stealth", fake_render_stealth)
        monkeypatch.setattr(manager, "_render_with_browser", fake_render_with_browser)

        res = await manager.render("http://example.com/protected")
        assert stealth_called is True
        assert res.engine == "camoufox"
        assert "Real content" in res.html
