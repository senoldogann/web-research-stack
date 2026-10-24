"""HTTP connections that go only to an address that was checked in the same step.

``SafeFetcher`` used to check a host (``validate_outbound_url`` looked its name up and judged every address) and
then let httpx look the name up again to connect, and so did each redirect hop. A name whose DNS answer changes
(DNS rebinding: a public address to the first lookup, ``127.0.0.1`` or a 192.168.x.x to the next) was checked as
public and connected to as the loopback or the home network.

Here the name is resolved once, inside the connection, every address of the answer is checked, and the TCP
connection is made to one of those addresses. The host name stays what the request says it is, so TLS verifies
the certificate for it (SNI included) and the ``Host`` header is the URL's. A redirect is a new request and goes
through the same step again. ``httpcore`` comes with ``httpx``.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any, Optional

import httpcore
import httpx

from web_scraper.network_safety import UnsafeTargetError, resolve_checked_addresses


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP connections to addresses that were resolved and checked a moment before.

    *allow_private_networks* lifts the public-address rule, not the pinning: the name is still resolved once and
    the connection still goes to what that lookup answered.
    """

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        inner: Optional[httpcore.AsyncNetworkBackend] = None,
    ) -> None:
        self._inner: httpcore.AsyncNetworkBackend = inner or httpcore.AnyIOBackend()
        self._allow_private_networks = allow_private_networks

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await resolve_checked_addresses(
                host,
                port,
                allow_private_networks=self._allow_private_networks,
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise httpcore.ConnectTimeout("The host name did not resolve in time") from exc
        failure: Optional[Exception] = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        if failure is None:  # unreachable: the lookup answered at least one address
            raise UnsafeTargetError("Blocked unsafe target: the host name resolved to no address")
        raise failure

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeTargetError("Blocked unsafe target: unix sockets are not reachable")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """The standard httpx transport, its connection pool opened through :class:`PinnedBackend`.

    HTTP/1.1, no proxies from the environment (a proxy would resolve the name itself), and the library's own
    limits. *ssl_context* is for a caller that trusts another set of authorities.
    """

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        limits: Optional[httpx.Limits] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        backend: Optional[httpcore.AsyncNetworkBackend] = None,
    ) -> None:
        limits = limits or httpx.Limits()
        context = ssl_context or httpx.create_ssl_context(trust_env=False)
        super().__init__(verify=context, http2=False, limits=limits, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=backend or PinnedBackend(allow_private_networks=allow_private_networks),
        )
