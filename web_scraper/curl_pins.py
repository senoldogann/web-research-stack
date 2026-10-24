"""libcurl's own name resolution, held to the addresses that were checked.

A curl_cffi session resolves a host name itself, so a name that ``validate_outbound_url`` looked up and judged
public can answer something else to libcurl's lookup (DNS rebinding). :class:`CurlPins` is the session's
``CURLOPT_RESOLVE`` list: a request to a host that is pinned connects to the addresses that were checked for it,
and libcurl asks no resolver. Three things are the pin's own conditions and are made here:

* the URL curl is given is the normalized one (lower case, ASCII, no trailing dot), because libcurl matches a pin by
  the host as it writes it: ``example.com.`` is not ``example.com`` to it and would be looked up;
* a host that is a number in an old spelling (``0177.0.0.1``, ``2130706433``) is refused beforehand
  (``network_safety``), because libcurl reads it as an address and never consults a pin for it;
* a proxy of the environment (``HTTPS_PROXY``) is switched off for the session, because a proxy resolves the name
  itself; ``trust_env=False`` does not do that for libcurl, which reads those variables on its own.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from curl_cffi.const import CurlOpt

from web_scraper.network_safety import (
    UnsafeTargetError,
    is_public_address,
    parse_ip_address,
    resolve_checked_addresses,
    resolve_checked_addresses_sync,
    validate_outbound_url_syntax,
)
from web_scraper.web_tools.urls import UnsafeUrlError, normalize_request_url

# How many hosts a session keeps pinned. A pin is replaced by the check of the next request to the same host and
# port, and is never taken away before then, so a request always finds the pin it made (or a later, checked one).
_PIN_LIMIT = 512


class CurlPins:
    """The ``host:port:address,address`` entries of one curl session, for hosts whose addresses were checked."""

    def __init__(self, limit: int = _PIN_LIMIT) -> None:
        self._limit = max(limit, 1)
        self._entries: OrderedDict[tuple[str, int], str] = OrderedDict()
        # The one list object the session holds: it is rebuilt in place, never replaced.
        self._resolve: list[str] = []
        self._lock = threading.Lock()

    @property
    def options(self) -> dict[Any, Any]:
        """The session's ``curl_options``: these pins, and no proxy."""
        return {CurlOpt.RESOLVE: self._resolve, CurlOpt.PROXY: ""}

    def pin(self, host: str, port: int, addresses: Sequence[str]) -> None:
        """Make curl connect *host*:*port* to *addresses*, which have been checked, and to nothing else."""
        entry = f"{host}:{port}:{','.join(_for_curl(address) for address in addresses)}"
        with self._lock:
            self._entries.pop((host, port), None)
            self._entries[(host, port)] = entry
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)
            self._resolve[:] = list(self._entries.values())


def _for_curl(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def _target(url: str, allow_private_networks: bool) -> tuple[str, str, int, bool]:
    """The URL curl is to request, its host and port, and whether the host is a name that has to be resolved."""
    try:
        validate_outbound_url_syntax(url, allow_private_networks)
        normalized = normalize_request_url(url)
    except UnsafeUrlError as exc:
        raise UnsafeTargetError(str(exc)) from exc
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        parse_ip_address(host)
    except ValueError:
        return normalized, host, port, True
    return normalized, host, port, False


def pin_url(pins: CurlPins, url: str, *, allow_private_networks: bool = False) -> str:
    """Check *url*, resolve its host once, pin the addresses that were checked, and return the URL to request."""
    normalized, host, port, is_name = _target(url, allow_private_networks)
    if is_name:
        pins.pin(host, port, resolve_checked_addresses_sync(host, port, allow_private_networks))
    return normalized


async def pin_url_async(
    pins: CurlPins,
    url: str,
    *,
    allow_private_networks: bool = False,
    timeout: Optional[float] = None,
) -> str:
    """:func:`pin_url` without blocking the event loop."""
    normalized, host, port, is_name = _target(url, allow_private_networks)
    if is_name:
        addresses = await resolve_checked_addresses(
            host, port, allow_private_networks=allow_private_networks, timeout=timeout
        )
        pins.pin(host, port, addresses)
    return normalized


def check_peer(response: Any, allow_private_networks: bool = False) -> None:
    """Refuse an answer that came from an address that is not public: the last line, after the request was made.

    libcurl says which address it connected to (``primary_ip``). The pin is what keeps a request away from a
    private address; this keeps its answer from being read if the pin ever missed.
    """
    peer = str(getattr(response, "primary_ip", "") or "")
    if peer and not allow_private_networks and not is_public_address(peer):
        raise UnsafeTargetError(
            "Blocked unsafe target: the connection reached a non-public address"
        )
