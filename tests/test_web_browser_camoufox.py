"""A real Camoufox against real local servers: what the guard holds in it, and what it does not.

The tests in ``test_web_browser_stealth.py`` stand a fake in for Playwright and for the camoufox package. These start
the real thing, once it is on the machine: the package ``camoufox``, the browser, the add-on and the model that
``python -m camoufox fetch`` puts there (a test is skipped without them, as the CI job has none; nothing here
downloads). The sites are servers on the loopback, each under a name of its own, which Firefox is told to resolve to
the loopback (``network.dns.localDomains``); the guard is told which names may be requested.

What these measured on 2026-10-25 (camoufox 0.5.8 and its Firefox 156, Playwright 1.62), and what ``docs/web-tools.md``
says: in Camoufox the requests of a page and of its frames are held (a fetch, an image, a beacon, a frame, an event
source, an XHR, a prefetch); a window a page opens is blocked by Firefox when no one clicked, and closed here when it
is not. A web socket, a worker's requests and a redirect the browser follows are **not** held: Playwright runs its
init scripts in Firefox's isolated world, where Camoufox keeps them, so the mock it puts on ``WebSocket`` is never the
page's. Those tests are written as the guard should hold them and marked ``xfail``; they pass the day a Camoufox runs
them in the page's world.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from browser_support import LOOPBACK, LocalSite, LocalSocket

from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import (
    BrowserRenderError,
    HeadlessBrowserManager,
    camoufox_missing,
)

NAMES = ("allowed.test", "refused.test", "live.test")
# Camoufox calls out by itself (its ad blocker fetches filter lists from GitHub, jsDelivr and others within 15 s of
# a start, and the application asked Mozilla and Google for updates before 1.4.3). A test reaches nothing outside
# this Mac: every call that is not to a loopback address or to one of the test names goes to a proxy that is not
# there. (Firefox does not proxy the loopback; the test names are listed so that it does not proxy them either.)
NOWHERE = {"server": "http://127.0.0.1:9", "bypass": ",".join(NAMES)}
LONG = "A page with something to say, and enough of it to be one. " * 5

pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:When using a proxy"
    ),  # the proxy here is the one that is not there
    pytest.mark.skipif(
        bool(camoufox_missing()),
        reason=f"Camoufox cannot start here: {camoufox_missing() or 'it can'}",
    ),
]


def _manager(monkeypatch: pytest.MonkeyPatch, **allowed: int) -> HeadlessBrowserManager:
    """A manager that reads with Camoufox, which resolves the test names to the loopback, and a guard that allows
    only the requests to *allowed* (name to port)."""
    prefixes = tuple(f"http://{name}:{port}/" for name, port in allowed.items())
    monkeypatch.setattr(
        browser_module, "browser_request_allowed", lambda url: url.startswith(prefixes)
    )
    prefs = dict(browser_module._STEALTH_OPTIONS["firefox_user_prefs"])
    prefs["network.dns.localDomains"] = ",".join(NAMES)
    monkeypatch.setitem(browser_module._STEALTH_OPTIONS, "firefox_user_prefs", prefs)
    monkeypatch.setitem(browser_module._STEALTH_OPTIONS, "humanize", False)
    monkeypatch.setitem(browser_module._STEALTH_OPTIONS, "proxy", NOWHERE)
    # A Camoufox that starts on a Mac that is busy with other work takes its time; these tests are not about the
    # time a read may take (the ones that are set their own).
    manager = HeadlessBrowserManager(post_load_wait_ms=1_500, timeout_seconds=90)
    return manager


@pytest.fixture
def refused() -> Any:
    site = LocalSite(
        {
            "/popup": (
                "<html><body>popup<img src='/popup-image'>"
                "<script>fetch('/popup-fetch').catch(function () {});</script></body></html>"
            ),
            "/secret": "owner-only",
        }
    )
    yield site
    site.close()


def _vectors(refused: LocalSite) -> str:
    target = f"http://refused.test:{refused.port}"
    return (
        "<!doctype html><html><head><title>Vectors</title></head><body><main><h1>Vectors</h1>"
        f"<p>{LONG}</p></main><script>"
        f"var T = '{target}';"
        "fetch(T + '/fetch').catch(function () {});"
        "new Image().src = T + '/image';"
        "navigator.sendBeacon(T + '/beacon', 'x');"
        "var f = document.createElement('iframe'); f.src = T + '/frame'; document.body.appendChild(f);"
        "try { new EventSource(T + '/events'); } catch (e) {}"
        "var x = new XMLHttpRequest(); x.open('GET', T + '/xhr'); try { x.send(); } catch (e) {}"
        "var l = document.createElement('link'); l.rel = 'prefetch'; l.href = T + '/prefetch';"
        "document.head.appendChild(l);"
        f"window.open(T + '/popup');</script></body></html>"
    )


class TestWhatTheGuardHoldsInCamoufox:
    @pytest.mark.asyncio
    async def test_the_requests_of_a_page_its_frames_and_its_windows_reach_nothing(
        self, refused: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = LocalSite({"/": _vectors(refused)})
        manager = _manager(monkeypatch, **{"allowed.test": site.port})
        try:
            page = await manager.render_stealth(f"http://allowed.test:{site.port}/")
        finally:
            await manager.stop()
            site.close()

        assert page.engine == "camoufox" and "Vectors" in page.html
        assert refused.seen == []

    @pytest.mark.asyncio
    async def test_the_same_page_does_reach_a_host_that_is_allowed(
        self, refused: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The control: the vectors do make their requests when the guard lets them.
        site = LocalSite({"/": _vectors(refused)})
        manager = _manager(monkeypatch, **{"allowed.test": site.port, "refused.test": refused.port})
        try:
            await manager.render_stealth(f"http://allowed.test:{site.port}/")
        finally:
            await manager.stop()
            site.close()

        # (A beacon is not among them: Firefox sent none in this run, allowed or not.)
        assert {"/fetch", "/image", "/frame", "/events", "/xhr"} <= set(refused.seen)

    @pytest.mark.asyncio
    async def test_a_page_that_ended_at_an_address_the_guard_refuses_is_not_returned(
        self, refused: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = LocalSite({"/": f"<html><body><p>{LONG}</p></body></html>"})
        original = site._server.RequestHandlerClass

        class Forward(original):  # type: ignore[valid-type, misc]
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/forward":
                    self.send_response(302)
                    self.send_header("Location", f"http://refused.test:{refused.port}/secret")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().do_GET()

        site._server.RequestHandlerClass = Forward
        manager = _manager(monkeypatch, **{"allowed.test": site.port})
        try:
            with pytest.raises(BrowserRenderError) as raised:
                await manager.render_stealth(f"http://allowed.test:{site.port}/forward")
        finally:
            await manager.stop()
            site.close()

        # The redirect is followed by the browser and the refused host is asked (the guard does not hold a
        # redirect); what it answered is not returned.
        assert raised.value.code == "unsafe_target"
        assert "/secret" in refused.seen

    @pytest.mark.asyncio
    async def test_a_socket_to_a_host_that_may_be_requested_connects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = LocalSocket("hello from the socket")
        page = (
            "<!doctype html><html><head><title>Live</title></head><body><main>"
            f'<p>{LONG}</p><p id="said">nothing yet</p></main><script>'
            f"var s = new WebSocket('ws://live.test:{live.port}/live');"
            "s.onmessage = function (e) {"
            "document.getElementById('said').textContent = 'said: ' + e.data; };</script></body></html>"
        )
        site = LocalSite({"/": page})
        manager = _manager(monkeypatch, **{"allowed.test": site.port, "live.test": live.port})
        try:
            rendered = await manager.render_stealth(f"http://allowed.test:{site.port}/")
        finally:
            await manager.stop()
            site.close()
            live.close()

        assert "said: hello from the socket" in rendered.html and live.opened == ["/live"]


class TestWhatTheGuardDoesNotHoldInCamoufox:
    """Written as the guard should hold them. They fail today, on purpose, and say why."""

    REASON = (
        "Camoufox runs Playwright's init scripts in Firefox's isolated world, so route_web_socket's mock is never "
        "the page's WebSocket; a worker's requests and a redirect hop are not routed by Playwright in Firefox"
    )

    @pytest.mark.xfail(strict=False, reason=REASON)
    @pytest.mark.asyncio
    async def test_a_web_socket_to_a_refused_host_reaches_nothing(
        self, refused: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = (
            f"<!doctype html><html><head><title>S</title></head><body><main><p>{LONG}</p></main><script>"
            f"new WebSocket('ws://refused.test:{refused.port}/socket');</script></body></html>"
        )
        site = LocalSite({"/": page})
        manager = _manager(monkeypatch, **{"allowed.test": site.port})
        try:
            await manager.render_stealth(f"http://allowed.test:{site.port}/")
        finally:
            await manager.stop()
            site.close()

        assert refused.seen == []

    @pytest.mark.xfail(strict=False, reason=REASON)
    @pytest.mark.asyncio
    async def test_a_workers_requests_reach_nothing(
        self, refused: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = f"fetch('http://refused.test:{refused.port}/worker').catch(function () {{}});"
        page = (
            f"<!doctype html><html><head><title>W</title></head><body><main><p>{LONG}</p></main><script>"
            f'new Worker(URL.createObjectURL(new Blob(["{worker}"])));</script></body></html>'
        )
        site = LocalSite({"/": page})
        manager = _manager(monkeypatch, **{"allowed.test": site.port})
        try:
            await manager.render_stealth(f"http://allowed.test:{site.port}/")
        finally:
            await manager.stop()
            site.close()

        assert refused.seen == []

    @pytest.mark.xfail(strict=False, reason=REASON)
    @pytest.mark.asyncio
    async def test_a_redirect_from_an_allowed_address_reaches_nothing(
        self, refused: LocalSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = (
            f"<!doctype html><html><head><title>R</title></head><body><main><p>{LONG}</p></main><script>"
            "fetch('/forward').catch(function () {});</script></body></html>"
        )
        site = LocalSite({"/": page})
        original = site._server.RequestHandlerClass

        class Forward(original):  # type: ignore[valid-type, misc]
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/forward":
                    self.send_response(302)
                    self.send_header("Location", f"http://refused.test:{refused.port}/secret")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().do_GET()

        site._server.RequestHandlerClass = Forward
        manager = _manager(monkeypatch, **{"allowed.test": site.port})
        try:
            await manager.render_stealth(f"http://allowed.test:{site.port}/")
        finally:
            await manager.stop()
            site.close()

        assert refused.seen == []


class _Wall(BaseHTTPRequestHandler):
    """A site whose bot check turns away a Chrome and lets a Firefox in."""

    seen: list[str] = []
    spin = False

    def do_GET(self) -> None:  # noqa: N802
        agent = self.headers.get("User-Agent", "")
        type(self).seen.append("firefox" if "Firefox" in agent else "other")
        if "Firefox" in agent:
            body = (
                f"<!doctype html><html><head><title>The page</title></head><body><main><h1>The page</h1>"
                f"<p>{LONG}</p></main></body></html>"
            )
            status = 200
        elif type(self).spin:
            body = (
                f"<!doctype html><html><head><title>busy</title></head><body><main><p>{LONG}</p></main>"
                "<script>setTimeout(function () { while (true) {} }, 300)</script></body></html>"
            )
            status = 200
        else:
            body = (
                "<html><head><title>Just a moment</title></head><body>"
                '<div class="cf-turnstile">Verify you are human</div></body></html>'
            )
            status = 403
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def wall() -> Any:
    _Wall.seen, _Wall.spin = [], False
    server = ThreadingHTTPServer((LOOPBACK, 0), _Wall)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    yield server
    server.shutdown()
    server.server_close()


class TestATargetThatEscalatesToCamoufox:
    """The whole path: the first engine (the installed Chrome) is turned away, the page is read again in Camoufox."""

    @staticmethod
    def _both(monkeypatch: pytest.MonkeyPatch, port: int) -> HeadlessBrowserManager:
        prefix = f"http://{LOOPBACK}:{port}/"
        monkeypatch.setattr(
            browser_module, "browser_request_allowed", lambda url: url.startswith(prefix)
        )
        monkeypatch.setitem(browser_module._STEALTH_OPTIONS, "humanize", False)
        monkeypatch.setitem(browser_module._STEALTH_OPTIONS, "proxy", NOWHERE)
        return HeadlessBrowserManager(
            preference="auto", post_load_wait_ms=300, settle_max_ms=1_500, timeout_seconds=30
        )

    @pytest.mark.asyncio
    async def test_a_bot_check_that_turns_chrome_away_is_read_in_camoufox(
        self, wall: ThreadingHTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = self._both(monkeypatch, wall.server_address[1])
        try:
            await manager.start()
            if manager.engine == "camoufox":
                pytest.skip("no Chrome here: the first engine is Camoufox itself")
            page = await asyncio.wait_for(
                manager.render(f"http://{LOOPBACK}:{wall.server_address[1]}/page"), 120
            )
        finally:
            await manager.stop()

        assert page.engine == "camoufox" and page.status_code == 200 and LONG in page.html
        assert _Wall.seen[0] == "other" and "firefox" in _Wall.seen  # Chrome was turned away first

    @pytest.mark.asyncio
    async def test_a_page_that_blocks_chrome_is_read_in_camoufox_within_the_limits(
        self, wall: ThreadingHTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _Wall.spin = True
        manager = self._both(monkeypatch, wall.server_address[1])
        # Chrome waits this long and the margin; Camoufox, whose first read is 5 to 10 s on an idle Mac and
        # more on a busy one, is given a minute (the limit of its read is not what this test is about).
        manager.timeout_seconds = 15.0
        monkeypatch.setattr(browser_module, "_HARD_LIMIT_MARGIN_SECONDS", 0.5)
        read_in_camoufox = manager.render_stealth

        async def render_stealth(url: str, **options: Any) -> Any:
            manager.timeout_seconds = 60.0
            return await read_in_camoufox(url, **options)

        monkeypatch.setattr(manager, "render_stealth", render_stealth)
        try:
            await manager.start()
            if manager.engine == "camoufox":
                pytest.skip("no Chrome here: the first engine is Camoufox itself")
            page = await asyncio.wait_for(
                manager.render(f"http://{LOOPBACK}:{wall.server_address[1]}/page"), 120
            )
        finally:
            await manager.stop()

        assert page.engine == "camoufox" and LONG in page.html
