"""Outbound network safety checks for scraper fetches."""

from __future__ import annotations

import ipaddress
import socket
from typing import Union
from urllib.parse import urlsplit


class UnsafeTargetError(ValueError):
    """Raised when a scraper target is not safe to fetch."""


def _normalize_hostname(hostname: str) -> str:
    """Normalize hostnames to a stable ASCII representation."""
    ascii_host = hostname.encode("idna").decode("ascii")
    return ascii_host.rstrip(".").lower()


def _normalize_ip_literal(
    ip_text: str,
) -> Union[ipaddress.IPv4Address, ipaddress.IPv6Address]:
    """Parse IPv4, IPv6, and IPv4-mapped IPv6 literals."""
    parsed = ipaddress.ip_address(ip_text)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        return parsed.ipv4_mapped
    return parsed


def is_public_ip_address(ip_text: str) -> bool:
    """Return True only for globally routable IP addresses."""
    return _normalize_ip_literal(ip_text).is_global


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


def validate_outbound_url(url: str, allow_private_networks: bool = False) -> str:
    """Reject internal or malformed targets before any outbound fetch."""
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

    hostname = _normalize_hostname(parsed.hostname)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeTargetError("Blocked unsafe target: localhost is not allowed")

    if allow_private_networks:
        return url

    try:
        parsed_ip = _normalize_ip_literal(hostname)
    except ValueError:
        parsed_ip = None

    if parsed_ip is not None:
        if not parsed_ip.is_global:
            raise UnsafeTargetError(
                "Blocked unsafe target: non-public IP addresses are not allowed"
            )
        return url

    port = parsed.port or (443 if scheme == "https" else 80)

    try:
        resolved_addresses = resolve_hostname(hostname, port)
    except socket.gaierror as exc:
        raise UnsafeTargetError("Blocked unsafe target: hostname resolution failed") from exc

    if not resolved_addresses:
        raise UnsafeTargetError("Blocked unsafe target: hostname resolution returned no addresses")

    for ip_text in resolved_addresses:
        if not is_public_ip_address(ip_text):
            raise UnsafeTargetError("Blocked unsafe target: resolved IP is not publicly routable")

    return url
