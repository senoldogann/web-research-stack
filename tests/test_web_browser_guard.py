"""What the browser's guard holds: the requests of a page, the web sockets it opens and the windows it opens.

1.4.1 routed the requests of the one page (``page.route``) and nothing else. A web socket is not a request a route
sees, and a window a page opens is another page, so ``new WebSocket("ws://127.0.0.1:9222/...")`` and
``window.open("http://127.0.0.1:8080/...")`` (its navigation and everything it loaded) reached a host the guard
refuses. Now the context is held, before its first page exists: ``context.route`` (a window is a page of the context),
``context.route_web_socket`` (a socket to an address that may be requested is connected, any other is closed with
1008), and a window is closed as it opens. The first half of this file is against fakes of what Playwright hands the
handlers; the second runs a real headless browser against real local servers (one the guard allows, one it refuses)
and is skipped where there is none.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from browser_support import (
    LocalSite,
    LocalSocket,
    host_resolver_rules,
    started_browser,
)
from rebinding_support import LOOPBACK, PUBLIC_A, World

from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import (
    BrowserUnavailableError,
    HeadlessBrowserManager,
    RequestGuard,
)


class _Socket:
    """What Playwright hands a web socket route handler: the page's socket, to connect or to close."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.done: list[str] = []

    def connect_to_server(self) -> None:
        self.done.append("connect")

    async def close(self, code: Any = None, reason: Any = None) -> None:
        self.done.append(f"close:{code}")


class _Window:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Context:
    """A Playwright browser context that records what is routed on it, in order."""

    def __init__(self, *, sockets: bool = True) -> None:
        self.events: list[str] = []
        self.listeners: list[tuple[str, Any]] = []
        self.page = types.SimpleNamespace(url="https://page.test/")
        if sockets:
            self.route_web_socket = self._route_web_socket

    async def route(self, pattern: str, handler: Any) -> None:
        self.events.append(f"route:{pattern}")

    async def _route_web_socket(self, pattern: str, handler: Any) -> None:
        self.events.append(f"socket:{pattern}")

    def on(self, event: str, handler: Any) -> None:
        self.listeners.append((event, handler))

    async def new_page(self) -> Any:
        self.events.append("page")
        return self.page

    def open(self, window: Any) -> None:
        for event, handler in self.listeners:
            if event == "page":
                handler(window)


class TestAWebSocketIsHeldToTheGuard:
    @pytest.mark.asyncio
    async def test_a_socket_to_an_address_that_may_be_requested_is_connected_and_any_other_is_closed(
        self, world: World
    ) -> None:
        world.dns.answers("public.test", [PUBLIC_A])
        world.dns.answers("private.test", [LOOPBACK])
        guard = RequestGuard()
        sockets = [
            _Socket("wss://public.test/live"),
            _Socket("ws://public.test:8080/live"),
            _Socket("ws://private.test:8080/live"),
            _Socket("ws://127.0.0.1:9222/devtools/browser"),
            _Socket("ws://[::1]:8080/"),
            _Socket("ws://[::ffff:127.0.0.1]/"),
            _Socket("ftp://public.test/"),
            _Socket("wss:///no-host"),
        ]

        for socket in sockets:
            await guard.route_web_socket(socket)

        assert [socket.done for socket in sockets] == [
            ["connect"],
            ["connect"],
            ["close:1008"],
            ["close:1008"],
            ["close:1008"],
            ["close:1008"],
            ["close:1008"],
            ["close:1008"],
        ]

    @pytest.mark.asyncio
    async def test_a_check_that_fails_closes_the_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(url: str) -> bool:
            raise RuntimeError("the check broke")

        monkeypatch.setattr(browser_module, "browser_request_allowed", broken)
        socket = _Socket("wss://public.test/live")

        await RequestGuard().route_web_socket(socket)

        assert socket.done == ["close:1008"]

    @pytest.mark.asyncio
    async def test_a_socket_and_a_request_to_one_host_are_one_verdict(self, world: World) -> None:
        world.dns.answers("public.test", [PUBLIC_A])
        guard = RequestGuard()
        route = types.SimpleNamespace(
            request=types.SimpleNamespace(url="https://public.test/app.js"),
            continue_=lambda: asyncio.sleep(0),
        )

        await guard.route(route)
        await guard.route_web_socket(_Socket("wss://public.test/live"))

        assert world.dns.lookups == {"public.test": 1}


class TestTheWholeContextIsHeld:
    @pytest.mark.asyncio
    async def test_requests_and_sockets_are_routed_on_the_context_before_a_page_exists(
        self,
    ) -> None:
        context = _Context()
        guard = RequestGuard()

        await guard.hold(context)
        await context.new_page()

        assert context.events == ["route:**/*", "socket:**/*", "page"]

    @pytest.mark.asyncio
    async def test_a_playwright_that_cannot_hold_sockets_is_not_used(self) -> None:
        context = _Context(sockets=False)

        with pytest.raises(BrowserUnavailableError, match="web sockets"):
            await RequestGuard().hold(context)

        assert context.events == []  # nothing was half held

    @pytest.mark.asyncio
    async def test_a_render_with_such_a_playwright_is_refused_and_frees_its_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(browser_module, "browser_request_allowed", lambda url: True)
        context = _Context(sockets=False)
        closed: list[bool] = []

        async def close() -> None:
            closed.append(True)

        context.close = close  # type: ignore[attr-defined]

        class Chrome:
            def is_connected(self) -> bool:
                return True

            async def new_context(self, **options: Any) -> _Context:
                return context

        manager = HeadlessBrowserManager(max_contexts=1)
        manager._browser = Chrome()
        manager._engine = "chromium"

        with pytest.raises(BrowserUnavailableError):
            await manager.render("https://page.test/")

        assert closed == [True] and manager._active == 0
        assert not manager._context_gate.locked()  # the slot is free again


class TestAWindowAPageOpensIsClosed:
    @pytest.mark.asyncio
    async def test_a_popup_is_closed_and_the_page_is_not(self) -> None:
        context = _Context()
        guard = RequestGuard()
        page = await context.new_page()
        guard.close_popups(context, page)
        popup, second = _Window(), _Window()
        page_window = types.SimpleNamespace(closed=False)

        context.open(popup)
        context.open(second)
        context.open(page)  # the page itself is not a popup
        for _ in range(3):
            await asyncio.sleep(0)

        assert popup.closed and second.closed and page_window.closed is False
        assert guard._closing == set()  # the tasks have run and are let go


class TestARealBrowserAgainstARealSite:
    """A page on a site the guard allows tries a socket and a window to one it refuses. The sites are local
    servers, each under a name of its own (the guard judges a host); the guard is told which names may be
    requested."""

    ALLOWED, REFUSED, LIVE = "allowed.test", "refused.test", "live.test"

    @pytest.fixture
    def refused(self) -> Any:
        site = LocalSite(
            {
                "/popup": (
                    "<html><body>popup<img src='/popup-image'>"
                    "<script>fetch('/popup-fetch').catch(function () {});</script></body></html>"
                )
            }
        )
        yield site
        site.close()

    def _browser_with(self, monkeypatch: pytest.MonkeyPatch, **allowed: int) -> None:
        """Every name reaches the loopback; only the requests to *allowed* (name to port) may be made."""
        prefixes = tuple(f"http://{name}:{port}/" for name, port in allowed.items())
        monkeypatch.setattr(
            browser_module, "browser_request_allowed", lambda url: url.startswith(prefixes)
        )
        monkeypatch.setattr(
            browser_module,
            "_CHROMIUM_ARGS",
            (
                *browser_module._CHROMIUM_ARGS,
                host_resolver_rules(self.ALLOWED, self.REFUSED, self.LIVE),
            ),
        )

    @pytest.mark.asyncio
    async def test_a_socket_and_a_window_to_a_refused_host_reach_nothing(
        self, refused: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = (
            "<!doctype html><html><head><title>Guard</title></head><body><main><h1>Guard</h1>"
            f"<p>{'A page that is complete as it arrives. ' * 12}</p>"
            '<p id="socket">socket-pending</p><p id="window">window-pending</p></main><script>'
            f"var s = new WebSocket('ws://{self.REFUSED}:{refused.port}/socket');"
            "s.onclose = function (e) {"
            "document.getElementById('socket').textContent = 'socket-closed-' + e.code; };"
            f"var w = window.open('http://{self.REFUSED}:{refused.port}/popup');"
            "setTimeout(function () {"
            "document.getElementById('window').textContent = 'window-closed-' + (w ? w.closed : 'none');"
            "}, 800);</script></body></html>"
        )
        site = LocalSite({"/": page})
        self._browser_with(monkeypatch, **{self.ALLOWED: site.port})
        manager = await started_browser(post_load_wait_ms=1_500)
        try:
            rendered = await manager.render(f"http://{self.ALLOWED}:{site.port}/")
        finally:
            await manager.stop()
            site.close()

        # The socket was closed by the guard (1008), not refused by a host that was never asked, and the
        # window was closed; nothing, not the handshake, the window's page nor what it loads, reached the host.
        assert "socket-closed-1008" in rendered.html
        assert "window-closed-true" in rendered.html
        assert refused.seen == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("guarded", [True, False], ids=["guarded", "control-unguarded"])
    async def test_every_other_way_a_script_can_ask_for_a_host_is_held_when_it_is_refused(
        self, refused: Any, monkeypatch: pytest.MonkeyPatch, guarded: bool
    ) -> None:
        # What 1.4.1 already held with a route on the page, held now by the route on the context: a fetch, an
        # image, a beacon, a frame, an event source, an XHR, a worker's fetch, a form posted to a new window and
        # a prefetch link. A change that lost the route would show here, and the control (the same page, the
        # host allowed) shows that the page does make them.
        target = f"http://{self.REFUSED}:{refused.port}"
        worker = f"fetch('{target}/worker').catch(function () {{}});"
        page = (
            "<!doctype html><html><head><title>Vectors</title></head><body><main><h1>Vectors</h1>"
            f"<p>{'A page that is complete as it arrives. ' * 12}</p></main><script>"
            f"var T = '{target}';"
            "fetch(T + '/fetch').catch(function () {});"
            "new Image().src = T + '/image';"
            "navigator.sendBeacon(T + '/beacon', 'x');"
            "var f = document.createElement('iframe'); f.src = T + '/frame'; document.body.appendChild(f);"
            "try { new EventSource(T + '/events'); } catch (e) {}"
            "var x = new XMLHttpRequest(); x.open('GET', T + '/xhr'); try { x.send(); } catch (e) {}"
            f'try {{ new Worker(URL.createObjectURL(new Blob(["{worker}"]))); }} catch (e) {{}}'
            "var form = document.createElement('form'); form.method = 'POST'; form.action = T + '/form';"
            "form.target = '_blank'; document.body.appendChild(form); try { form.submit(); } catch (e) {}"
            "var l = document.createElement('link'); l.rel = 'prefetch'; l.href = T + '/prefetch';"
            "document.head.appendChild(l);</script></body></html>"
        )
        site = LocalSite({"/": page})
        allowed = {self.ALLOWED: site.port}
        if not guarded:
            allowed[self.REFUSED] = refused.port
        self._browser_with(monkeypatch, **allowed)
        manager = await started_browser(post_load_wait_ms=1_500)
        try:
            await manager.render(f"http://{self.ALLOWED}:{site.port}/")
        finally:
            await manager.stop()
            site.close()

        if guarded:
            assert refused.seen == []
        else:
            asked = set(refused.seen)
            wanted = {"/fetch", "/image", "/beacon", "/frame", "/events", "/xhr", "/worker"}
            assert wanted <= asked, asked

    @pytest.mark.asyncio
    async def test_a_socket_to_a_host_that_may_be_requested_still_connects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = LocalSocket("hello from the socket")
        page = (
            "<!doctype html><html><head><title>Live</title></head><body><main><h1>Live</h1>"
            f"<p>{'A page that is complete as it arrives. ' * 12}</p>"
            '<p id="said">nothing yet</p></main><script>'
            f"var s = new WebSocket('ws://{self.LIVE}:{live.port}/live');"
            "s.onmessage = function (e) {"
            "document.getElementById('said').textContent = 'said: ' + e.data; };</script></body></html>"
        )
        site = LocalSite({"/": page})
        self._browser_with(monkeypatch, **{self.ALLOWED: site.port, self.LIVE: live.port})
        manager = await started_browser(post_load_wait_ms=1_500)
        try:
            rendered = await manager.render(f"http://{self.ALLOWED}:{site.port}/")
        finally:
            await manager.stop()
            site.close()
            live.close()

        assert "said: hello from the socket" in rendered.html
        assert live.opened == ["/live"]
