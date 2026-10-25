"""Google's own Chrome paths are held to the address guard, as its Camoufox is.

``GoogleSearcher`` starts the installed Chrome itself when the search's Camoufox cannot start
(``_search_headless_chrome``) and, for the research endpoints that allow a window, keeps one Chrome tab for every
search (``_search_chrome``). Until 1.4.7 neither held its context to the address guard nor looked where the page
ended: a security review had a script of that page fetch a private address, and the private server was hit. These
tests run the installed Chrome against two servers on the loopback, one that stands for Google and one for an address
the guard refuses, and are skipped where there is no Chrome. Nothing reaches beyond this machine.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from browser_support import LocalSite

from web_scraper import google_search as google_module
from web_scraper import stealth_camoufox as stealth
from web_scraper.google_search import GoogleBackendMissing, GoogleSearchBlocked, GoogleSearcher
from web_scraper.web_tools import browser as browser_module


@pytest.fixture(autouse=True)
def _a_guard_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stealth, "_GUARD", None)
    monkeypatch.setitem(google_module._HEADLESS_STATE, "blocked_until", 0.0)
    # A CI runner's first Chrome can take longer than the 15 s the search gives it ("Chrome did not start in time" on
    # one job of three, 2026-10-25): the test gives it a minute, which says nothing of the search's own wait.
    waits = GoogleSearcher._wait_for_cdp

    async def patient(port: int, timeout_s: float = 60.0) -> None:
        await waits(port, timeout_s=60.0)

    monkeypatch.setattr(GoogleSearcher, "_wait_for_cdp", staticmethod(patient))
    if GoogleSearcher()._chrome_binary() is None:
        pytest.skip("no installed Chrome here")


@pytest.fixture
def private() -> Any:
    site = LocalSite({"/probe": "<html>private</html>"})
    yield site
    site.close()


def _google(monkeypatch: pytest.MonkeyPatch, private: LocalSite) -> LocalSite:
    """A results page on ``localhost`` whose script asks the private server on ``127.0.0.1``: two host names, so
    two verdicts (the guard keeps one per name). Only the results page may be requested."""
    results = "".join(
        f"<div class='g'><a href='/r{index}'><h3>Result {index}</h3></a>"
        f"<div class='VwiC3b'>About result {index}.</div></div>"
        for index in range(5)
    )
    page = (
        f"<html><body><div id='rso'>{results}</div><script>"
        f"fetch('http://127.0.0.1:{private.port}/probe', {{mode: 'no-cors'}}).catch(function () {{}});"
        f"var i = new Image(); i.src = 'http://127.0.0.1:{private.port}/image';"
        "</script></body></html>"
    )
    # The test site serves a page by its whole path, the query too: the one GoogleSearcher asks for "query".
    site = LocalSite({"/search?q=query&num=5&hl=en&gl=us": page})
    monkeypatch.setattr(GoogleSearcher, "BASE_URL", f"http://localhost:{site.port}/search")
    allowed = f"http://localhost:{site.port}/"
    monkeypatch.setattr(
        browser_module, "browser_request_allowed", lambda url: url.startswith(allowed)
    )
    return site


class TestTheHeadlessChrome:
    @pytest.mark.asyncio
    async def test_a_script_of_the_page_cannot_reach_an_address_the_guard_refuses(
        self, monkeypatch: pytest.MonkeyPatch, private: LocalSite
    ) -> None:
        google = _google(monkeypatch, private)
        try:
            results = await GoogleSearcher()._search_headless_chrome("query", 5)
        finally:
            google.close()

        assert any(path.startswith("/search?q=query") for path in google.seen)
        assert [result["title"] for result in results] == [f"Result {i}" for i in range(5)]
        assert private.seen == []  # 1.4.6: ["/probe", "/image"]

    @pytest.mark.asyncio
    async def test_a_page_that_ended_at_an_address_the_guard_refuses_is_not_read(
        self, monkeypatch: pytest.MonkeyPatch, private: LocalSite
    ) -> None:
        google = _google(monkeypatch, private)
        evaluated: list[str] = []

        async def refused(page: Any) -> bool:
            evaluated.append(page.url)
            return False

        monkeypatch.setattr(google_module, "landing_allowed", refused)
        try:
            with pytest.raises(GoogleSearchBlocked, match="not allowed"):
                await GoogleSearcher()._search_headless_chrome("query", 5)
        finally:
            google.close()

        assert evaluated and evaluated[0].startswith(f"http://localhost:{google.port}/search")

    @pytest.mark.asyncio
    async def test_a_context_the_guard_cannot_hold_is_not_searched_in(
        self, monkeypatch: pytest.MonkeyPatch, private: LocalSite
    ) -> None:
        google = _google(monkeypatch, private)

        async def cannot_hold(context: Any) -> None:
            raise browser_module.BrowserUnavailableError("this Playwright cannot route web sockets")

        monkeypatch.setattr(google_module, "hold_context", cannot_hold)
        try:
            # Said, not an empty list: an empty list is a page read clean, which would say Google walls nothing.
            with pytest.raises(GoogleBackendMissing, match="address guard"):
                await GoogleSearcher()._search_headless_chrome("query", 5)
        finally:
            google.close()

        assert google.seen == [] and private.seen == []

    @pytest.mark.asyncio
    async def test_googles_bot_check_is_a_wall_and_is_remembered_for_the_hour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1.4.8 kept the hour and returned an empty list: a Mac whose only browser this is asked Google twice more.
        check = (
            "<html><body><h1>Our systems have detected unusual traffic</h1>"
            "<form id='captcha-form'></form></body></html>"
        )
        site = LocalSite({"/search?q=query&num=5&hl=en&gl=us": check})
        monkeypatch.setattr(GoogleSearcher, "BASE_URL", f"http://localhost:{site.port}/search")
        allowed = f"http://localhost:{site.port}/"
        monkeypatch.setattr(
            browser_module, "browser_request_allowed", lambda url: url.startswith(allowed)
        )
        try:
            with pytest.raises(GoogleSearchBlocked, match="bot check") as refused:
                await GoogleSearcher()._search_headless_chrome("query", 5)
        finally:
            site.close()

        assert refused.value.wall is True
        assert google_module._HEADLESS_STATE["blocked_until"] > time.monotonic()


class TestTheChromeTabOfEverySearch:
    @pytest.mark.asyncio
    async def test_its_context_is_held_before_the_tab_exists(
        self, monkeypatch: pytest.MonkeyPatch, private: LocalSite
    ) -> None:
        google = _google(monkeypatch, private)
        searcher = GoogleSearcher(headless=True, allow_visible_browser=True)
        try:
            results = await searcher._search_chrome("query", 5)
        finally:
            await searcher.close_chrome()
            google.close()

        assert len(results) == 5
        assert private.seen == []
