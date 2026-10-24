"""Outbound network safety checks for scraper fetches.

A fetch is safe when it connects only to a public address, and only to an address that was checked in the
same step as the connection. Looking a name up to check it and then letting the HTTP client look it up again
to connect is not that: a name whose DNS answer changes between the two lookups (DNS rebinding) is checked as
a public address and connected to as the loopback or a LAN address. So this module has two halves:

* what a URL's host may be (:func:`validate_outbound_url_syntax`, :func:`is_public_ip_address`): a literal
  address is judged as it is written, including the IPv6 forms that carry an IPv4 address (``::127.0.0.1``,
  ``::ffff:127.0.0.1``, ``64:ff9b::7f00:1``, ``2002:7f00:1::``), and a spelling of an address that clients read
  in different ways (``127.1``, ``0177.0.0.1``, ``2130706433``) is no host at all;
* which addresses a connection may go to (:func:`resolve_checked_addresses`): a name is resolved once, every
  address it has is checked, and the connection is made to those addresses and to no others
  (``web_scraper/pinned_transport.py``).

:func:`validate_outbound_url` is the check made at one moment, for the callers that cannot pin a connection
(a browser resolves the name itself).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Optional, Sequence, Union
from urllib.parse import urlsplit

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# IPv6 prefixes whose last 32 bits are the IPv4 address a connection to them reaches: the IPv4-compatible form
# (deprecated, ``::127.0.0.1``), IPv4-mapped, IPv4-translated (SIIT) and NAT64's well-known prefix (RFC 6052),
# which a gateway on an IPv6-only network translates to that IPv4 address.
_IPV4_IN_LAST_32_BITS = tuple(
    ipaddress.IPv6Network(prefix)
    for prefix in ("::/96", "::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96")
)
# 6to4 puts the IPv4 address in bits 16 to 47.
_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")
# A public IPv6 address is global unicast, minus the ranges that are not routed to the Internet. The standard
# library's own table of them grows with every patch release, so the judgement does not rest on it alone.
_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")
_NOT_PUBLIC_IN_GLOBAL_UNICAST = tuple(
    ipaddress.IPv6Network(prefix)
    for prefix in (
        "2001::/23",  # IETF protocol assignments: Teredo (2001::/32), benchmarking, ORCHID
        "2001:db8::/32",  # documentation
        "3fff::/20",  # documentation (RFC 9637)
    )
)
# The last label of a host that a client may read as a number: digits, or 0x and hex digits (WHATWG URL's
# "ends in a number"). No domain name ends so, and clients disagree on what such a host is.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")


class UnsafeTargetError(ValueError):
    """Raised when a scraper target is not safe to fetch."""


class HostnameResolutionError(UnsafeTargetError):
    """Raised when a target's hostname does not resolve.

    The target is unreachable rather than unsafe, so callers that classify errors
    report it apart; it stays an ``UnsafeTargetError`` so that every caller still
    refuses it.
    """

    def __init__(self, hostname: str) -> None:
        super().__init__(f"Could not resolve host {hostname}")
        self.hostname = hostname


def _normalize_hostname(hostname: str) -> str:
    """Normalize hostnames to a stable ASCII representation."""
    ascii_host = hostname.encode("idna").decode("ascii")
    return ascii_host.rstrip(".").lower()


def parse_ip_address(text: str) -> IPAddress:
    """An address as a URL or a resolver writes it: brackets and an IPv6 zone (``%en0``) are dropped.

    Raises ``ValueError`` for text that is no address.
    """
    cleaned = text.strip().strip("[]")
    if ":" in cleaned:  # a zone belongs to an IPv6 address; "127.0.0.1%2e" is no IPv4 address
        cleaned = cleaned.partition("%")[0]
    return ipaddress.ip_address(cleaned)


def embedded_ipv4(address: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    """The IPv4 address an IPv6 address carries, or ``None``.

    A connection to ``::ffff:127.0.0.1``, ``::127.0.0.1``, ``64:ff9b::7f00:1`` or ``2002:7f00:1::`` reaches
    127.0.0.1 where the network translates the form, so it is judged as that address.
    """
    if any(address in prefix for prefix in _IPV4_IN_LAST_32_BITS):
        return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)
    if address in _SIX_TO_FOUR:
        return ipaddress.IPv4Address((int(address) >> 80) & 0xFFFF_FFFF)
    return None


def _is_public(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        carried = embedded_ipv4(address)
        if carried is not None:
            return _is_public(carried)
        if address not in _GLOBAL_UNICAST:
            return False  # loopback, unspecified, link-local, unique local, multicast, reserved
        if any(address in prefix for prefix in _NOT_PUBLIC_IN_GLOBAL_UNICAST):
            return False
    # IPv4 multicast counts as global to the standard library; it is no host to read a page from.
    return address.is_global and not address.is_multicast


def is_public_ip_address(ip_text: str) -> bool:
    """Return True only for globally routable IP addresses.

    An IPv4 address written as IPv6 (IPv4-mapped, IPv4-compatible, NAT64, 6to4) is judged by its IPv4 part.
    Raises ``ValueError`` for text that is no address.
    """
    return _is_public(parse_ip_address(ip_text))


def is_public_address(ip_text: str) -> bool:
    """Whether text is a public address, as :func:`is_public_ip_address` judges; text that is no address is not."""
    try:
        return is_public_ip_address(ip_text)
    except ValueError:
        return False


def _literal_address(hostname: str) -> Optional[IPAddress]:
    try:
        return parse_ip_address(hostname)
    except ValueError:
        return None


def ends_in_a_number(hostname: str) -> bool:
    """Whether a host name ends in a numeric label (``127.1``, ``0x7f.1``, ``2130706433``, ``1.2.3``).

    Clients read such a host as an IPv4 address in the old forms (``inet_aton``: ``127.1`` is 127.0.0.1,
    ``0177.0.0.1`` is 127.0.0.1 where ``getaddrinfo`` may say 177.0.0.1), and a check made with one reading
    does not hold for a connection made with another. A real IPv4 address is written as four decimal
    numbers, and that one is not refused here.
    """
    last = hostname.rstrip(".").rpartition(".")[2]
    return bool(_NUMERIC_LABEL.fullmatch(last))


def is_localhost_name(hostname: str) -> bool:
    return hostname == "localhost" or hostname.endswith(".localhost")


def host_problem(hostname: str, allow_private_networks: bool = False) -> Optional[str]:
    """Why a host (ASCII, lower case, no trailing dot) can never be a target, or ``None``.

    The decision is made on the host as it is written, with no lookup: ``localhost``, an address literal that is
    not public, and a numeric spelling of an address that is not written as four decimal numbers. With
    *allow_private_networks* only ``localhost`` is refused.
    """
    if is_localhost_name(hostname):
        return "Blocked unsafe target: localhost is not allowed"
    if allow_private_networks:
        return None
    literal = _literal_address(hostname)
    if literal is not None:
        if _is_public(literal):
            return None
        return "Blocked unsafe target: non-public IP addresses are not allowed"
    if ends_in_a_number(hostname):
        return "Blocked unsafe target: IP addresses must be written as four decimal numbers"
    return None


def resolve_hostname(hostname: str, port: int) -> list[str]:
    """Resolve a hostname into unique IP addresses."""
    addresses = []
    seen = set()
    for info in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM):
        ip_text = info[4][0]
        if ip_text in seen:
            continue
        seen.add(ip_text)
        addresses.append(ip_text)
    return addresses


def _checked(hostname: str, addresses: Sequence[str], allow_private_networks: bool) -> list[str]:
    """The answer of a lookup, every address checked, IPv4 first (a host without an IPv6 route stalls on one).

    One address that is not public refuses the host: a name that answers a public address and a private one is
    not one to choose among.
    """
    unique = list(dict.fromkeys(str(address) for address in addresses))
    if not unique:
        raise HostnameResolutionError(hostname)
    if not allow_private_networks and not all(is_public_address(address) for address in unique):
        raise UnsafeTargetError("Blocked unsafe target: resolved IP is not publicly routable")
    return sorted(unique, key=lambda address: ":" in address)


def resolve_checked_addresses_sync(
    hostname: str, port: int, allow_private_networks: bool = False
) -> list[str]:
    """Resolve *hostname* once and return its addresses, each of them checked.

    Whoever connects must connect to these addresses and to no others; a second lookup could answer
    differently. Raises ``HostnameResolutionError`` for a name that does not resolve and ``UnsafeTargetError``
    for one with an address that is not public (unless *allow_private_networks*).
    """
    try:
        addresses = resolve_hostname(hostname, port)
    except socket.gaierror as exc:
        raise HostnameResolutionError(hostname) from exc
    except UnicodeError as exc:
        raise UnsafeTargetError("Blocked unsafe target: the host name is not valid") from exc
    return _checked(hostname, addresses, allow_private_networks)


async def resolve_checked_addresses(
    hostname: str,
    port: int,
    *,
    allow_private_networks: bool = False,
    timeout: Optional[float] = None,
) -> list[str]:
    """:func:`resolve_checked_addresses_sync` without blocking the event loop (``asyncio.TimeoutError`` after
    *timeout* seconds)."""
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(resolve_hostname, hostname, port), timeout=timeout
        )
    except socket.gaierror as exc:
        raise HostnameResolutionError(hostname) from exc
    except UnicodeError as exc:
        raise UnsafeTargetError("Blocked unsafe target: the host name is not valid") from exc
    return _checked(hostname, addresses, allow_private_networks)


def _checked_parts(url: str, allow_private_networks: bool) -> tuple[str, Optional[IPAddress], int]:
    """The normalized host of *url*, its address if it is a literal, and its port, if *url* may be a target."""
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise UnsafeTargetError("Blocked unsafe target: only http and https URLs are allowed")

    if parsed.username or parsed.password:
        raise UnsafeTargetError(
            "Blocked unsafe target: URLs with embedded credentials are not allowed"
        )

    if not parsed.hostname:
        raise UnsafeTargetError("Blocked unsafe target: hostname is required")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeTargetError("Blocked unsafe target: the port is not valid") from exc

    hostname = _normalize_hostname(parsed.hostname)
    problem = host_problem(hostname, allow_private_networks)
    if problem is not None:
        raise UnsafeTargetError(problem)
    return hostname, _literal_address(hostname), port


def validate_outbound_url_syntax(url: str, allow_private_networks: bool = False) -> str:
    """Reject targets that can be refused from the URL alone: scheme, credentials, ``localhost``, addresses.

    No name is looked up. This is the check for a fetcher whose connections resolve, check and connect in one
    step (``web_scraper/pinned_transport.py``), which is where a host name is judged by what it resolves to.
    """
    _checked_parts(url, allow_private_networks)
    return url


def validate_outbound_url(url: str, allow_private_networks: bool = False) -> str:
    """Reject internal or malformed targets before any outbound fetch.

    A host name is looked up and every address it has must be public. This is a check at one moment: for a
    caller that connects by itself (a browser) it is all there is to make, and a connection that it does not
    pin can meet another answer. A fetcher that pins its connections uses :func:`validate_outbound_url_syntax`.
    """
    hostname, literal, port = _checked_parts(url, allow_private_networks)
    if allow_private_networks or literal is not None:
        return url

    resolve_checked_addresses_sync(hostname, port)
    return url
