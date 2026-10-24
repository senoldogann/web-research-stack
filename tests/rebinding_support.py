"""A small network for the tests of DNS rebinding. Nothing in it leaves the machine.

* ``RebindingDNS`` stands where ``socket.getaddrinfo`` is: for the names a test declares it answers a public
  address to the first lookup and the loopback to every later one (or whatever the test scripts); a name it does
  not know does not resolve, and an address literal is answered by the real function, which asks nobody.
* ``FakeInternet`` stands where ``httpcore`` opens a TCP connection. A connection to a public address is served
  in memory by the handler a test gave that address (an HTTP/1.1 exchange, with no TLS in it, though
  ``start_tls`` is honoured and the name it was started for is kept) or refused; a connection to any other address
  is made for real, to the loopback, because that is what the attack is: the code under test must never ask for one.
* ``LocalService`` is the owner-only service on the loopback that the attack aims at. It records what reaches it.

The public addresses are ordinary global addresses that the tests never dial for real.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional, Union

import httpcore

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "93.184.216.35"
PUBLIC_C = "93.184.216.36"
PUBLIC_D = "93.184.216.37"
PUBLIC_E = "93.184.216.38"
LOOPBACK = "127.0.0.1"
OWNER_TOKEN = "sk-internal-0123456789abcdef"
OWNER_PAGE = (
    "<html><head><title>Internal admin</title></head><body><article><h1>Internal admin</h1>"
    + f"<p>OWNER-ONLY SERVICE: api token = {OWNER_TOKEN}; the reactor restart date announced after "
    "the long repair is next spring. </p>" * 8 + "</article></body></html>"
)


class LocalService:
    """An HTTP server on 127.0.0.1 that records every request it gets."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                service.seen.append(f"GET {self.path}  Host: {self.headers.get('Host')}")
                body = OWNER_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer((LOOPBACK, 0), Handler)
        self.port: int = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class RebindingDNS:
    """``socket.getaddrinfo`` for the names of a test, counting the lookups of each."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[list[str]]] = {}
        self.lookups: dict[str, int] = {}
        self._real = socket.getaddrinfo

    def rebinds(self, name: str, public: str, then: str = LOOPBACK) -> None:
        """*name* answers *public* to its first lookup and *then* to every other."""
        self.scripts[name] = [[public], [then]]

    def answers(self, name: str, *answers: list[str]) -> None:
        """*name* answers each of *answers* in turn, the last one for good."""
        self.scripts[name] = [list(answer) for answer in answers]

    def install(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", self.getaddrinfo)

    def getaddrinfo(self, host: Any, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        name = host.decode() if isinstance(host, bytes) else str(host)
        script = self.scripts.get(name)
        if script is not None:
            self.lookups[name] = self.lookups.get(name, 0) + 1
            answer = script[min(self.lookups[name], len(script)) - 1]
            return [self._info(address, port) for address in answer]
        try:
            ipaddress.ip_address(name.strip("[]"))
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, f"{name} is not a name of this test") from None
        return self._real(host, port, *args, **kwargs)

    @staticmethod
    def _info(address: str, port: Any) -> tuple[Any, ...]:
        if ":" in address:
            return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))
        return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))


@dataclass
class Request:
    """A request the fake Internet served."""

    method: str
    target: str
    headers: dict[str, str]
    address: str
    server_hostname: Optional[str] = None


@dataclass
class Response:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: Union[bytes, str] = b""


Handler = Callable[[Request], Response]

_REASONS = {
    200: "OK",
    301: "Moved Permanently",
    302: "Found",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
}


def html(body: str, status: int = 200) -> Response:
    return Response(status, {"Content-Type": "text/html; charset=utf-8"}, body)


class _Exchange(httpcore.AsyncNetworkStream):
    """One connection to a public address: it reads one request and answers it."""

    def __init__(self, internet: FakeInternet, address: str) -> None:
        self._internet = internet
        self._address = address
        self._received = b""
        self._to_send = b""
        self._server_hostname: Optional[str] = None

    async def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        self._received += buffer
        if self._to_send or b"\r\n\r\n" not in self._received:
            return
        head = self._received.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
        method, target, _ = head[0].split(" ", 2)
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in head[1:])
        }
        request = Request(method, target, headers, self._address, self._server_hostname)
        self._internet.requests.append(request)
        response = self._internet.handlers[self._address](request)
        body = response.body.encode() if isinstance(response.body, str) else response.body
        lines = [f"HTTP/1.1 {response.status} {_REASONS.get(response.status, 'Status')}"]
        lines += [f"{name}: {value}" for name, value in response.headers.items()]
        lines += [f"Content-Length: {len(body)}", "Connection: close", "", ""]
        self._to_send = "\r\n".join(lines).encode("latin-1") + body

    async def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        chunk, self._to_send = self._to_send[:max_bytes], self._to_send[max_bytes:]
        return chunk

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> httpcore.AsyncNetworkStream:
        self._server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class FakeInternet:
    """Where ``httpcore`` opens TCP connections: public addresses are served in memory, the rest is the loopback."""

    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        # A public address whose connections are made for real, to a service of the test on the loopback.
        self.forwards: dict[str, tuple[str, int]] = {}
        # The addresses connections were opened to, the names a connection was asked for (a library that
        # resolves the name itself is handed one: the fix is that none is), and the dials to a private address.
        self.dials: list[tuple[str, int]] = []
        self.named_dials: list[tuple[str, int]] = []
        self.private_dials: list[tuple[str, int]] = []
        self.requests: list[Request] = []

    def serve(self, address: str, handler: Handler) -> None:
        self.handlers[address] = handler

    def install(self, monkeypatch: Any) -> None:
        internet = self
        real = httpcore.AnyIOBackend.connect_tcp

        async def connect_tcp(
            backend: Any,
            host: str,
            port: int,
            timeout: Optional[float] = None,
            local_address: Optional[str] = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            try:
                ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                # Handed a name, the library looks it up now, as the real one does, and dials what it says.
                internet.named_dials.append((host, port))
                try:
                    host = str(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0])
                except socket.gaierror as exc:
                    raise httpcore.ConnectError(str(exc)) from exc
            internet.dials.append((host, port))
            if host in internet.handlers:
                return _Exchange(internet, host)
            if host in internet.forwards:
                target, target_port = internet.forwards[host]
                return await real(
                    backend, target, target_port, timeout, local_address, socket_options
                )
            if not ipaddress.ip_address(host).is_global:
                # What the attack wants: a real connection to the loopback or the home network.
                internet.private_dials.append((host, port))
                return await real(backend, host, port, timeout, local_address, socket_options)
            raise httpcore.ConnectError(f"no route to {host} in this test")

        monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)


@dataclass
class World:
    """The three parts of the fake network, as the ``world`` fixture gives them."""

    dns: RebindingDNS
    internet: FakeInternet
    local: LocalService

    def rebinding_host(self, name: str, public: str, handler: Handler) -> str:
        """A name that answers *public* once and the loopback after, with *handler* behind *public*."""
        self.dns.rebinds(name, public)
        self.internet.serve(public, handler)
        return name
