"""A page that blocks the browser's main thread does not hang the render, its slots or the tool.

On 1.4.1 (and before) ``render`` waited on ``page.evaluate`` and ``page.content`` with nothing around them. A page
whose script spins (``while (true) {}``) leaves both unanswered for ever: with the manager's own timeout at 10 s a
render was still going after 40 s, it kept one of the manager's context slots and, under it, one of the MCP server's
eight tool slots; eight such pages and the server answered nothing. Now the render as a whole is bounded (its own
timeout and a margin), the context is closed whatever happened (a renderer that is stuck in a loop dies with its
context), a context that will not close is given up and the browser started again, and the render ends with a
``timeout`` that the service turns into the page it has from the HTTP fetch and a ``browser_render_failed:timeout``
warning. The first half of this file is against fakes of a stuck page; the second is a real headless browser and a
real page that spins, and is skipped where there is none.
"""

from __future__ import annotations

import asyncio
import time
import types
from typing import Any

import pytest
from browser_support import LocalSite, started_browser
from rebinding_support import PUBLIC_A, World, html

from web_scraper.mcp_server import McpServer
from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import BrowserRenderError, HeadlessBrowserManager
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

# The longest any test below waits for something that must already be over; a render that is not is a hang.
HANG = 10.0
LONG = "A page with something to say, and enough of it to be one. " * 5


async def _never() -> None:
    await asyncio.Event().wait()


class HangingPage:
    """A page whose main thread is blocked for good: it navigates, and nothing it is asked afterwards answers."""

    url = "https://page.test/"

    async def goto(self, url: str, wait_until: str, timeout: int) -> Any:
        return types.SimpleNamespace(status=200)

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        await asyncio.sleep(timeout / 1000)
        raise TimeoutError("the page never reached it")

    async def evaluate(self, expression: str, limit: int = 200) -> Any:
        await _never()

    async def content(self) -> str:
        await _never()
        return ""


class WholePage:
    url = "https://page.test/"

    async def goto(self, url: str, wait_until: str, timeout: int) -> Any:
        return types.SimpleNamespace(status=200)

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        return None

    async def evaluate(self, expression: str, limit: int = 200) -> Any:
        return {"chars": len(LONG), "head": LONG[:limit]}

    async def content(self) -> str:
        return f"<html><head><title>Whole</title></head><body><main>{LONG}</main></body></html>"


class Context:
    """A browser context that records that it was closed, and may not close."""

    def __init__(self, page: Any, *, hangs_on_close: bool = False) -> None:
        self.page = page
        self.hangs_on_close = hangs_on_close
        self.closed = 0

    async def route(self, pattern: str, handler: Any) -> None:
        return None

    async def route_web_socket(self, pattern: str, handler: Any) -> None:
        return None

    def on(self, event: str, handler: Any) -> None:
        return None

    async def new_page(self) -> Any:
        return self.page

    async def close(self) -> None:
        self.closed += 1
        if self.hangs_on_close:
            await _never()


class Chrome:
    def __init__(self, *contexts: Context) -> None:
        self.contexts = list(contexts)
        self.made: list[Context] = []
        self.shut_down = False

    def is_connected(self) -> bool:
        return not self.shut_down

    async def new_context(self, **options: Any) -> Context:
        context = self.contexts.pop(0) if len(self.contexts) > 1 else self.contexts[0]
        self.made.append(context)
        return context

    async def close(self) -> None:
        self.shut_down = True


@pytest.fixture(autouse=True)
def _short_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every target is public, and the margins and the wait for a close are a fraction of a second."""
    monkeypatch.setattr(browser_module, "browser_request_allowed", lambda url: True)
    monkeypatch.setattr(browser_module, "_HARD_LIMIT_MARGIN_SECONDS", 0.2)
    monkeypatch.setattr(browser_module, "_CLOSE_TIMEOUT_SECONDS", 0.2)


def _manager(chrome: Chrome, **settings: Any) -> HeadlessBrowserManager:
    manager = HeadlessBrowserManager(post_load_wait_ms=0, settle_stable_ms=0, **settings)
    manager.timeout_seconds = 0.4  # the constructor keeps it to a second or more
    manager._browser = chrome
    manager._engine = "chromium"
    return manager


class TestARenderOfAPageThatBlocksTheBrowser:
    @pytest.mark.asyncio
    async def test_it_ends_with_a_timeout_soon_after_its_own_limit(self) -> None:
        context = Context(HangingPage())
        manager = _manager(Chrome(context))
        began = time.monotonic()

        with pytest.raises(BrowserRenderError) as raised:
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert raised.value.code == "timeout"
        # Its own limit (0.4 s) and the margin (0.2 s), not a second more than the two and a little.
        assert time.monotonic() - began < 2.0
        assert context.closed == 1  # a renderer stuck in a loop dies with its context

    @pytest.mark.asyncio
    async def test_it_frees_its_context_slot_for_the_next_render(self) -> None:
        stuck, whole = Context(HangingPage()), Context(WholePage())
        manager = _manager(Chrome(stuck, whole), max_contexts=1)

        with pytest.raises(BrowserRenderError):
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)
        rendered = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert LONG in rendered.html and manager._active == 0
        assert not manager._context_gate.locked()

    @pytest.mark.asyncio
    async def test_a_context_that_will_not_close_is_given_up_and_the_browser_is_started_again(
        self,
    ) -> None:
        context = Context(HangingPage(), hangs_on_close=True)
        chrome = Chrome(context)
        manager = _manager(chrome)

        with pytest.raises(BrowserRenderError) as raised:
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        # The render ended (with its own error, not with the close that never answered), and the browser that
        # did not answer is shut down: running is false, so the next render starts a new one.
        assert raised.value.code == "timeout"
        assert chrome.shut_down is True and manager._browser is None and not manager.running
        assert manager._active == 0 and not manager._context_gate.locked()

    @pytest.mark.asyncio
    async def test_a_render_that_is_cancelled_still_closes_its_context(self) -> None:
        context = Context(HangingPage())
        manager = _manager(Chrome(context))
        manager.timeout_seconds = 60.0

        task = asyncio.ensure_future(manager.render("https://page.test/"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, HANG)

        assert context.closed == 1 and manager._active == 0

    @pytest.mark.asyncio
    async def test_a_page_that_is_whole_is_not_cut_by_the_bound(self) -> None:
        manager = _manager(Chrome(Context(WholePage())))

        rendered = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert LONG in rendered.html and rendered.settle == "stable"


class TestTheServiceAndTheToolsAroundIt:
    @pytest.fixture
    def service(self, world: World) -> Any:
        world.dns.answers("shell.test", [PUBLIC_A])
        shell = (
            "<html><head><title>Shell</title></head><body><main><p>A few lines of text.</p></main>"
            '<script src="/app.js"></script></body></html>'
        )
        world.internet.serve(PUBLIC_A, lambda request: html(shell))
        chrome = Chrome(Context(HangingPage()))
        manager = _manager(chrome)
        return WebToolsService(
            settings=WebToolsSettings(
                search_provider="fake", soft_404_check=False, respect_robots=False
            ),
            fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
            browser=manager,
            search_adapters={},
        )

    @pytest.mark.asyncio
    async def test_a_scrape_returns_the_page_the_http_fetch_had_and_says_the_browser_timed_out(
        self, service: WebToolsService
    ) -> None:
        try:
            scraped = await asyncio.wait_for(service.scrape("https://shell.test/"), HANG)
        finally:
            await service.close()

        assert scraped["fetched_with"] == "http" and "A few lines of text." in scraped["markdown"]
        assert "browser_render_failed:timeout" in scraped["warnings"]

    @pytest.mark.asyncio
    async def test_more_calls_than_the_server_has_tool_slots_all_end(
        self, service: WebToolsService
    ) -> None:
        server = McpServer(service)
        calls = [
            server._call_tool({"name": "web_scrape", "arguments": {"url": "https://shell.test/"}})
            for _ in range(12)  # the server runs eight at a time; the other four wait for a slot
        ]

        try:
            results = await asyncio.wait_for(asyncio.gather(*calls), 30)
        finally:
            await service.close()

        assert len(results) == 12 and not any(result.get("isError") for result in results)
        assert server._request_slots._value == 8  # every slot is back


class TestARealBrowserAgainstAPageThatSpins:
    """The reviewer's page: complete text, and a script that spins for ever 300 ms after the load."""

    @pytest.fixture
    def site(self) -> Any:
        spinning = (
            "<!doctype html><html><head><title>busy</title></head><body>"
            f"<main><p>{LONG}</p></main>"
            "<script>setTimeout(function () { while (true) {} }, 300)</script></body></html>"
        )
        whole = f"<!doctype html><html><head><title>whole</title></head><body><main><p>{LONG}</p></main></body></html>"
        site = LocalSite({"/busy": spinning, "/whole": whole})
        try:
            yield site
        finally:
            site.close()

    @pytest.mark.asyncio
    async def test_the_render_ends_and_the_same_browser_reads_the_next_page(
        self, site: LocalSite
    ) -> None:
        manager = await started_browser(timeout_seconds=3.0, max_contexts=2)
        try:
            began = time.monotonic()
            with pytest.raises(BrowserRenderError) as raised:
                await asyncio.wait_for(manager.render(site.url("/busy")), 30)
            took = time.monotonic() - began
            after = await asyncio.wait_for(manager.render(site.url("/whole")), 30)
        finally:
            await manager.stop()

        # Its own limit (3 s) and the margin (0.2 s here) and the closing of the context: seconds, not a hang.
        assert raised.value.code == "timeout" and took < 10
        assert LONG in after.html and manager._active == 0
