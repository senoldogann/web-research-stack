"""A page is returned only if it ended at an address the guard allows.

The guard is asked about the first address of a request. A redirect the browser follows is not asked about again
(Chrome and Firefox both: measured below), so a page whose allowed address answers ``302 Location:
http://127.0.0.1:8080/...`` was rendered from the loopback and handed to the caller as the page. The request to the
refused address is still made - the guard does not hold a redirect, and ``docs/web-tools.md`` says so - but what it
answered is now not returned: the render ends with ``unsafe_target``. The first half is against a fake page; the
second is a real headless browser and real local servers, and is skipped where there is none.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from browser_support import LocalSite, host_resolver_rules, started_browser
from test_web_browser_hang import Chrome, Context, WholePage

from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import BrowserRenderError, HeadlessBrowserManager

LONG = "A page with something to say, and enough of it to be one. " * 5


@pytest.fixture(autouse=True)
def _loopback_is_refused_everything_else_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser_module, "browser_request_allowed", lambda url: "127.0.0.1" not in url
    )


def _manager(page: Any) -> HeadlessBrowserManager:
    manager = HeadlessBrowserManager(post_load_wait_ms=0, settle_stable_ms=0)
    manager._browser = Chrome(Context(page))
    manager._engine = "chromium"
    return manager


class TestAgainstAFakePage:
    @pytest.mark.asyncio
    async def test_a_page_that_ended_at_an_address_the_guard_refuses_is_not_returned(self) -> None:
        page = WholePage()
        page.url = (
            "http://127.0.0.1:8080/secret"  # an allowed address answered a redirect to the loopback
        )

        with pytest.raises(BrowserRenderError) as raised:
            await asyncio.wait_for(_manager(page).render("https://page.test/"), 10)

        assert raised.value.code == "unsafe_target"

    @pytest.mark.asyncio
    async def test_a_page_that_ended_at_another_allowed_address_is_returned_there(self) -> None:
        page = WholePage()
        page.url = "https://www.page.test/moved"

        rendered = await asyncio.wait_for(_manager(page).render("https://page.test/"), 10)

        assert rendered.final_url == "https://www.page.test/moved" and LONG in rendered.html

    @pytest.mark.asyncio
    @pytest.mark.parametrize("final", ["about:blank", "chrome-error://chromewebdata/"])
    async def test_a_page_that_is_not_a_page_of_any_host_is_not_judged_by_one(
        self, final: str
    ) -> None:
        page = WholePage()
        page.url = final

        rendered = await asyncio.wait_for(_manager(page).render("https://page.test/"), 10)

        assert rendered.final_url == final


class TestARealBrowserAgainstARealRedirect:
    ALLOWED, REFUSED = "allowed.test", "refused.test"

    @pytest.fixture
    def sites(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """allowed.test answers /forward with a 302 to refused.test/secret; its home page also fetches /forward."""
        refused = LocalSite({"/secret": "owner-only"})
        target = f"http://{self.REFUSED}:{refused.port}/secret"
        home = (
            "<!doctype html><html><head><title>Home</title></head><body><main>"
            f"<p>{LONG}</p></main><script>fetch('/forward').catch(function () {{}});</script></body></html>"
        )
        allowed = LocalSite({"/": home})
        original = allowed._server.RequestHandlerClass

        class Forward(original):  # type: ignore[valid-type, misc]
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/forward":
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().do_GET()

        allowed._server.RequestHandlerClass = Forward
        prefix = f"http://{self.ALLOWED}:{allowed.port}/"
        monkeypatch.setattr(
            browser_module, "browser_request_allowed", lambda url: url.startswith(prefix)
        )
        monkeypatch.setattr(
            browser_module,
            "_CHROMIUM_ARGS",
            (*browser_module._CHROMIUM_ARGS, host_resolver_rules(self.ALLOWED, self.REFUSED)),
        )
        yield allowed, refused
        allowed.close()
        refused.close()

    @pytest.mark.asyncio
    async def test_a_navigation_that_is_redirected_to_a_refused_address_returns_no_page(
        self, sites: Any
    ) -> None:
        allowed, refused = sites
        manager = await started_browser(post_load_wait_ms=300)
        try:
            with pytest.raises(BrowserRenderError) as raised:
                await manager.render(f"http://{self.ALLOWED}:{allowed.port}/forward")
        finally:
            await manager.stop()

        # The browser followed the redirect (the guard does not hold one): the refused host was asked. What it
        # answered, "owner-only", is not what the caller gets.
        assert raised.value.code == "unsafe_target"
        assert refused.seen == ["/secret"]

    @pytest.mark.asyncio
    async def test_the_page_itself_is_returned_when_it_stays_where_it_is(self, sites: Any) -> None:
        allowed, _ = sites
        manager = await started_browser(post_load_wait_ms=300)
        try:
            rendered = await manager.render(f"http://{self.ALLOWED}:{allowed.port}/")
        finally:
            await manager.stop()

        assert LONG in rendered.html and rendered.final_url.endswith(f":{allowed.port}/")

    @pytest.mark.xfail(
        strict=False,
        reason="the guard is asked about the first address of a request; a redirect is not asked about again",
    )
    @pytest.mark.asyncio
    async def test_a_scripts_fetch_that_is_redirected_to_a_refused_address_reaches_nothing(
        self, sites: Any
    ) -> None:
        # Written as the guard should hold it; it fails today, on purpose. The page above fetches /forward, an
        # allowed address, and the browser follows its redirect to the refused host.
        allowed, refused = sites
        manager = await started_browser(post_load_wait_ms=300)
        try:
            await manager.render(f"http://{self.ALLOWED}:{allowed.port}/")
        finally:
            await manager.stop()

        assert refused.seen == []
