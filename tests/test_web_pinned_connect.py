"""Every connection goes to the address that was checked in the same step (DNS rebinding, 1.3.1).

``SafeFetcher`` used to check a host by resolving its name and then let httpx resolve it again to connect, so a name
that answered a public address to the first lookup and ``127.0.0.1`` to the next was checked as public and connected
to as the loopback. These tests run the real fetcher, the real transport and the real resolver code against a name
that does exactly that (``tests/rebinding_support.py``): the owner-only service on the loopback must see nothing,
and no connection may be opened to an address that is not public.
"""

from __future__ import annotations

import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpcore
import httpx
import pytest
from rebinding_support import (
    LOOPBACK,
    PUBLIC_A,
    PUBLIC_B,
    Response,
    World,
    html,
)

from web_scraper import network_safety
from web_scraper.network_safety import HostnameResolutionError, UnsafeTargetError
from web_scraper.pinned_transport import PinnedBackend, PinnedTransport
from web_scraper.web_tools.fetcher import FetchError, SafeFetcher

PAGE = "<html><head><title>The public page</title></head><body><p>Served from the public address.</p></body></html>"


def _fetcher(**limits: Any) -> SafeFetcher:
    return SafeFetcher(timeout_seconds=5, retry_attempts=0, **limits)


class TestTheBackend:
    @pytest.mark.asyncio
    async def test_it_connects_to_the_address_it_checked_and_looks_the_name_up_once(
        self, world: World
    ) -> None:
        world.rebinding_host("rebind.test", PUBLIC_A, lambda request: html(PAGE))

        await PinnedBackend().connect_tcp("rebind.test", 80, timeout=5)

        assert world.internet.dials == [(PUBLIC_A, 80)]
        assert world.dns.lookups == {"rebind.test": 1}
        assert world.internet.private_dials == []

    @pytest.mark.asyncio
    async def test_a_name_with_one_address_that_is_not_public_is_refused_whole(
        self, world: World
    ) -> None:
        world.dns.answers("mixed.test", [PUBLIC_A, "10.0.0.8"])
        world.internet.serve(PUBLIC_A, lambda request: html(PAGE))

        with pytest.raises(UnsafeTargetError):
            await PinnedBackend().connect_tcp("mixed.test", 80, timeout=5)

        assert world.internet.dials == []

    @pytest.mark.parametrize(
        "answer",
        [
            "127.0.0.1",
            "10.1.2.3",
            "192.168.1.1",
            "172.16.0.9",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "::",
            "fe80::1",
            "fc00::1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::127.0.0.1",
            "::192.168.1.1",
            "64:ff9b::7f00:1",
            "64:ff9b::c0a8:101",
            "64:ff9b::a9fe:a9fe",
            "64:ff9b:1::1",
            "64:ff9b:1:ffff:ffff:ffff:ffff:ffff",
            "2002:7f00:1::",
            "2002:c0a8:101::1",
            "2002:a9fe:a9fe::1",
            "::ffff:0:7f00:1",
            "2001:db8::1",
        ],
    )
    @pytest.mark.asyncio
    async def test_no_answer_that_is_not_public_is_dialled(self, world: World, answer: str) -> None:
        world.dns.answers("answers.test", [answer])

        with pytest.raises(UnsafeTargetError):
            await PinnedBackend().connect_tcp("answers.test", 80, timeout=5)

        assert world.internet.dials == []

    @pytest.mark.asyncio
    async def test_ipv4_goes_first_and_a_dead_address_falls_back_to_the_next(
        self, world: World
    ) -> None:
        world.dns.answers("dual.test", ["2606:4700:4700::1111", PUBLIC_A, PUBLIC_B])
        world.internet.serve(PUBLIC_B, lambda request: html(PAGE))  # PUBLIC_A has nothing behind it

        await PinnedBackend().connect_tcp("dual.test", 80, timeout=5)

        assert world.internet.dials == [(PUBLIC_A, 80), (PUBLIC_B, 80)]

    @pytest.mark.asyncio
    async def test_private_networks_allowed_lifts_the_rule_not_the_pin(self, world: World) -> None:
        world.dns.rebinds("lan.test", "192.168.1.5", then=LOOPBACK)
        world.internet.serve("192.168.1.5", lambda request: html(PAGE))

        await PinnedBackend(allow_private_networks=True).connect_tcp("lan.test", 80, timeout=5)

        assert world.internet.dials == [("192.168.1.5", 80)]
        assert world.dns.lookups == {"lan.test": 1}

    @pytest.mark.asyncio
    async def test_a_name_that_does_not_resolve_is_a_dns_error(self, world: World) -> None:
        with pytest.raises(HostnameResolutionError) as exc:
            await PinnedBackend().connect_tcp("nowhere.test", 80, timeout=5)

        assert exc.value.hostname == "nowhere.test"
        assert world.internet.dials == []

    @pytest.mark.asyncio
    async def test_the_lookup_is_held_to_the_connect_timeout(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def slow(hostname: str, port: int) -> list[str]:
            time.sleep(0.5)
            return [PUBLIC_A]

        monkeypatch.setattr(network_safety, "resolve_hostname", slow)

        with pytest.raises(httpcore.ConnectTimeout):
            await PinnedBackend().connect_tcp("slow.test", 80, timeout=0.05)

    @pytest.mark.asyncio
    async def test_unix_sockets_are_not_reachable(self) -> None:
        with pytest.raises(UnsafeTargetError):
            await PinnedBackend().connect_unix_socket("/var/run/docker.sock")


class TestTheFetcherPinsItsConnections:
    @pytest.mark.asyncio
    async def test_a_scrape_of_a_name_that_rebinds_reads_the_checked_address(
        self, world: World
    ) -> None:
        port = world.local.port
        world.rebinding_host("rebind.test", PUBLIC_A, lambda request: html(PAGE))

        async with _fetcher() as fetcher:
            result = await fetcher.fetch(f"http://rebind.test:{port}/admin/restart?x=1")

        assert "Served from the public address." in result.html
        assert world.internet.dials == [(PUBLIC_A, port)]
        assert world.dns.lookups == {"rebind.test": 1}
        (request,) = world.internet.requests
        assert request.target == "/admin/restart?x=1"
        assert request.headers["host"] == f"rebind.test:{port}"
        assert world.local.seen == [] and world.internet.private_dials == []
        assert world.internet.named_dials == []

    @pytest.mark.asyncio
    async def test_the_next_connection_to_the_name_is_checked_again_and_refused(
        self, world: World
    ) -> None:
        port = world.local.port
        world.rebinding_host("rebind.test", PUBLIC_A, lambda request: html(PAGE))

        async with _fetcher() as fetcher:
            await fetcher.fetch(f"http://rebind.test:{port}/one")
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(f"http://rebind.test:{port}/two")

        assert exc.value.code == "unsafe_target"
        assert world.dns.lookups == {"rebind.test": 2}
        assert world.internet.dials == [(PUBLIC_A, port)]  # the second connection was never made
        assert world.local.seen == [] and world.internet.private_dials == []
        assert world.internet.named_dials == []

    @pytest.mark.asyncio
    async def test_a_redirect_hop_is_checked_and_pinned_again(self, world: World) -> None:
        port = world.local.port
        world.rebinding_host(
            "first.test",
            PUBLIC_A,
            lambda request: Response(302, {"Location": f"http://hop.test:{port}/landing"}),
        )
        world.rebinding_host("hop.test", PUBLIC_B, lambda request: html(PAGE))

        async with _fetcher() as fetcher:
            result = await fetcher.fetch(f"http://first.test:{port}/start")

        assert "Served from the public address." in result.html
        assert result.final_url == f"http://hop.test:{port}/landing"
        assert world.internet.dials == [(PUBLIC_A, port), (PUBLIC_B, port)]
        assert world.dns.lookups == {"first.test": 1, "hop.test": 1}
        assert world.local.seen == [] and world.internet.private_dials == []
        assert world.internet.named_dials == []

    @pytest.mark.parametrize(
        ("target", "code"),
        [
            ("http://127.0.0.1:{port}/private", "unsafe_target"),
            ("http://[::127.0.0.1]:{port}/private", "unsafe_target"),
            ("http://[64:ff9b::7f00:1]:{port}/private", "unsafe_target"),
            ("http://[::ffff:127.0.0.1]:{port}/private", "unsafe_target"),
            ("http://localhost:{port}/private", "unsafe_target"),
            ("http://2130706433:{port}/private", "unsafe_target"),
            ("http://127.1:{port}/private", "unsafe_target"),
            ("http://0x7f.0.0.1:{port}/private", "unsafe_target"),
            ("http://hop-private.test:{port}/private", "unsafe_target"),
            # httpx itself will not read a Location that spells an address with a leading zero.
            ("http://0177.0.0.1:{port}/private", "network_error"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_redirect_into_the_local_network_is_refused_in_every_spelling(
        self, world: World, target: str, code: str
    ) -> None:
        port = world.local.port
        world.dns.answers("hop-private.test", [LOOPBACK])
        world.rebinding_host(
            "first.test",
            PUBLIC_A,
            lambda request: Response(302, {"Location": target.format(port=port)}),
        )

        async with _fetcher() as fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(f"http://first.test:{port}/start")

        assert exc.value.code == code
        assert world.internet.dials == [(PUBLIC_A, port)]
        assert world.local.seen == [] and world.internet.private_dials == []
        assert world.internet.named_dials == []

    @pytest.mark.asyncio
    async def test_a_head_read_for_a_date_reads_the_checked_address(self, world: World) -> None:
        port = world.local.port
        world.rebinding_host("rebind.test", PUBLIC_A, lambda request: html(PAGE))

        async with _fetcher() as fetcher:
            result = await fetcher.fetch_head(f"http://rebind.test:{port}/story", max_bytes=65536)

        assert "Served from the public address." in result.html
        assert world.internet.dials == [(PUBLIC_A, port)]
        assert world.dns.lookups == {"rebind.test": 1}
        assert world.local.seen == [] and world.internet.private_dials == []
        assert world.internet.named_dials == []

    @pytest.mark.asyncio
    async def test_a_head_read_of_a_name_that_is_already_the_loopback_is_refused(
        self, world: World
    ) -> None:
        port = world.local.port
        world.dns.answers("rebind.test", [LOOPBACK])

        async with _fetcher() as fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch_head(f"http://rebind.test:{port}/story")

        assert exc.value.code == "unsafe_target"
        assert world.local.seen == [] and world.internet.dials == []

    @pytest.mark.asyncio
    async def test_a_name_that_does_not_resolve_is_a_dns_error_and_not_retried(
        self, world: World
    ) -> None:
        async with SafeFetcher(timeout_seconds=5, retry_attempts=3, backoff_seconds=0) as fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch("http://nowhere.test/")

        assert (exc.value.code, exc.value.detail) == ("dns_error", "nowhere.test")
        assert exc.value.retryable is False
        assert world.internet.dials == []  # no connection was tried for a name nobody resolves

    @pytest.mark.parametrize(
        "url",
        [
            "http://[::127.0.0.1]/",
            "http://[::192.168.1.1]/",
            "http://[64:ff9b::7f00:1]/",
            "http://[64:ff9b::c0a8:101]/",
            "http://[64:ff9b:1::7f00:1]/",
            "http://[2002:7f00:1::]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:0:7f00:1]/",
            "http://127.1/",
            "http://0x7f.0.0.1/",
            "http://0177.0.0.1/",
            "http://2130706433/",
            "http://localhost./",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_host_written_as_a_private_address_is_refused_before_any_lookup(
        self, world: World, url: str
    ) -> None:
        async with _fetcher() as fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(url)
            with pytest.raises(FetchError):
                await fetcher.fetch_head(url)

        assert exc.value.code == "unsafe_target"
        assert world.dns.lookups == {} and world.internet.dials == []

    @pytest.mark.asyncio
    async def test_a_proxy_in_the_environment_is_not_used(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A proxy resolves the name itself, which is the lookup this removes.
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(variable, "http://127.0.0.1:9")
        world.rebinding_host("rebind.test", PUBLIC_A, lambda request: html(PAGE))

        async with _fetcher() as fetcher:
            result = await fetcher.fetch("http://rebind.test/page")

        assert "Served from the public address." in result.html
        assert world.internet.dials == [(PUBLIC_A, 80)]

    @pytest.mark.asyncio
    async def test_private_networks_allowed_pins_the_lookup_it_made(self, world: World) -> None:
        world.dns.rebinds("lan.test", "192.168.1.5", then=LOOPBACK)
        world.internet.serve("192.168.1.5", lambda request: html(PAGE))

        async with _fetcher(allow_private_networks=True) as fetcher:
            result = await fetcher.fetch("http://lan.test/page")

        assert "Served from the public address." in result.html
        assert world.internet.dials == [("192.168.1.5", 80)]
        assert world.dns.lookups == {"lan.test": 1}

    @pytest.mark.asyncio
    async def test_the_default_transport_is_the_pinned_one_and_a_given_one_is_the_callers(
        self,
    ) -> None:
        async with SafeFetcher() as fetcher:
            assert isinstance(fetcher._client_or_create()._transport, PinnedTransport)
            assert fetcher.validator is network_safety.validate_outbound_url_syntax

        mock = httpx.MockTransport(lambda request: httpx.Response(200))
        async with SafeFetcher(transport=mock) as fetcher:
            assert fetcher._client_or_create()._transport is mock
            assert fetcher.validator is network_safety.validate_outbound_url

    @pytest.mark.asyncio
    async def test_the_pool_opens_its_connections_through_the_pinned_backend(self) -> None:
        transport = PinnedTransport()
        try:
            assert isinstance(transport._pool._network_backend, PinnedBackend)
        finally:
            await transport.aclose()


class _TlsService(ThreadingHTTPServer):
    """An HTTPS server on the loopback for a name the test makes a certificate for."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self.names: list[str] = []
        self.hosts: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hosts.append(str(self.headers.get("Host")))
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return None

        super().__init__((LOOPBACK, 0), Handler)
        context.sni_callback = self._remember_name
        self.socket = context.wrap_socket(self.socket, server_side=True)
        threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def _remember_name(self, sock: Any, name: Any, context: Any) -> None:
        self.names.append(str(name))

    @property
    def port(self) -> int:
        return int(self.server_address[1])


class TestTlsThroughThePinnedAddress:
    """The connection goes to the checked address; the certificate is still the name's."""

    def _server(self, trustme: Any, ca: Any, *names: str) -> tuple[_TlsService, ssl.SSLContext]:
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert(*names).configure_cert(server_context)
        client_context = ssl.create_default_context()
        ca.configure_trust(client_context)
        return _TlsService(server_context), client_context

    @pytest.mark.asyncio
    async def test_the_name_is_the_sni_the_host_header_and_the_certificate_of_the_pinned_address(
        self, world: World
    ) -> None:
        trustme = pytest.importorskip("trustme")
        ca = trustme.CA()
        service, client_context = self._server(trustme, ca, "pinned.test")
        try:
            world.dns.answers("pinned.test", [PUBLIC_A])
            world.internet.forwards[PUBLIC_A] = (LOOPBACK, service.port)
            transport = PinnedTransport(ssl_context=client_context)

            async with SafeFetcher(
                transport=transport,
                validator=network_safety.validate_outbound_url_syntax,
                timeout_seconds=5,
                retry_attempts=0,
            ) as fetcher:
                result = await fetcher.fetch(f"https://pinned.test:{service.port}/page")

            assert "Served from the public address." in result.html
            assert world.internet.dials == [(PUBLIC_A, service.port)]  # what the dialler was handed
            assert service.names == ["pinned.test"]  # SNI
            assert service.hosts == [f"pinned.test:{service.port}"]
        finally:
            service.shutdown()
            service.server_close()

    @pytest.mark.asyncio
    async def test_a_certificate_for_another_name_is_refused_though_the_address_is_checked(
        self, world: World
    ) -> None:
        trustme = pytest.importorskip("trustme")
        ca = trustme.CA()
        service, client_context = self._server(trustme, ca, "other.test")
        try:
            world.dns.answers("pinned.test", [PUBLIC_A])
            world.internet.forwards[PUBLIC_A] = (LOOPBACK, service.port)

            async with SafeFetcher(
                transport=PinnedTransport(ssl_context=client_context),
                validator=network_safety.validate_outbound_url_syntax,
                timeout_seconds=5,
                retry_attempts=0,
            ) as fetcher:
                with pytest.raises(FetchError) as exc:
                    await fetcher.fetch(f"https://pinned.test:{service.port}/page")

            assert exc.value.code == "network_error"
            assert service.hosts == []  # no request was sent over the connection
        finally:
            service.shutdown()
            service.server_close()
