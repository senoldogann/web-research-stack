"""A real local site and a real headless browser for the tests that need a page drawn by scripts.

A page that a script draws a second and a half after it loaded cannot be shown with a fake: the question is what
a browser sees and when. ``LocalSite`` is an HTTP server on the loopback (it records what it was asked), the pages
below are what it serves, and ``started_browser`` starts a real headless browser or says why the test cannot run
(Playwright is an extra, and the CI job that runs the suite does not install it): such a test is skipped there
and runs on a machine that has one. Nothing reaches beyond this machine.
"""

from __future__ import annotations

import base64
import hashlib
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import pytest

from web_scraper.web_tools.browser import BrowserUnavailableError, HeadlessBrowserManager

LOOPBACK = "127.0.0.1"

# The text a script draws: long enough to be a page, and to be told from the loader's.
DRAWN = "The content that a script draws one and a half seconds after the page loaded. " * 6
LOADING = "Loading animation while page loads"


def drawn_late(title: str = "Drawn late", seconds: float = 1.5) -> str:
    """A shell whose script draws the page ``seconds`` after it has loaded; its own title is in the HTML."""
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body>"
        f'<div id="root"><div class="loader">{LOADING}</div></div>'
        "<script>setTimeout(function () {"
        "document.getElementById('root').innerHTML = "
        f"'<main><h1>{title}</h1><p>{DRAWN}</p></main>';"
        f"}}, {int(seconds * 1000)});</script></body></html>"
    )


def never_filled(title: str = "Never filled") -> str:
    """A shell whose app never starts."""
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body>"
        f'<div id="root"><div class="loader">{LOADING}</div></div>'
        "<script>/* the app never starts */</script></body></html>"
    )


def complete(title: str = "Complete") -> str:
    """A page that is whole as it arrives, with a script that draws nothing."""
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body>"
        f"<main><h1>{title}</h1><p>{'A page that is complete as it arrives. ' * 12}</p></main>"
        "<script>/* analytics */</script></body></html>"
    )


class LocalSite:
    """An HTTP server on the loopback that serves *pages* by path and records every path asked for (a
    GET, or a POST, which it answers with nothing)."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.seen: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                site.seen.append(self.path)
                body = site.pages.get(self.path)
                data = (body if body is not None else "not found").encode()
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
                site.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self.port: int = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def url(self, path: str) -> str:
        return f"http://{LOOPBACK}:{self.port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class LocalSocket:
    """A web socket server on the loopback: it answers the handshake, sends one text frame (*greeting*) and
    holds the connection open for a while. ``opened`` is the paths of the handshakes it was asked for."""

    def __init__(self, greeting: str = "hello from the socket") -> None:
        self.opened: list[str] = []
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    head += chunk
                lines = head.decode("latin-1").split("\r\n")
                outer.opened.append(lines[0].split(" ")[1])
                headers = {
                    name.strip().lower(): value.strip()
                    for name, _, value in (line.partition(":") for line in lines[1:])
                }
                key = headers.get("sec-websocket-key", "")
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                payload = greeting.encode()
                self.request.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode()
                    + bytes([0x81, len(payload)])
                    + payload
                )
                time.sleep(3)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer((LOOPBACK, 0), Handler)
        self._server.daemon_threads = True
        self.port: int = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def url(self, path: str = "/") -> str:
        return f"ws://{LOOPBACK}:{self.port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


async def started_browser(**settings: Any) -> HeadlessBrowserManager:
    """A headless browser that is running, or the skip of the test that wanted one."""
    manager = HeadlessBrowserManager(preference="auto", **settings)
    try:
        await manager.start()
    except BrowserUnavailableError as exc:
        pytest.skip(f"no headless browser here: {exc}")
    except Exception as exc:  # noqa: BLE001 - whatever stops a browser from starting is a reason to skip
        pytest.skip(f"no headless browser here: {type(exc).__name__}")
    return manager


def host_resolver_rules(*names: str) -> str:
    """The Chrome flag that makes every one of *names* the loopback for the browser: hosts of their own for
    servers that are on the loopback together (the guard judges a host, not a port)."""
    return "--host-resolver-rules=" + ", ".join(f"MAP {name} {LOOPBACK}" for name in names)


def host_resolver_rule(name: str, address: Optional[str] = None) -> str:
    """The Chrome flag that makes *name* the loopback for the browser (it resolves names itself)."""
    return f"--host-resolver-rules=MAP {name} {address or LOOPBACK}"
