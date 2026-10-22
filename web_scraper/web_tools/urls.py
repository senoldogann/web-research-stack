"""URL canonicalisation, scope checks, and deterministic content fingerprints."""

from __future__ import annotations

import hashlib
import ipaddress
import posixpath
import re
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
}
_TRACKING_PREFIXES = ("utm_",)
_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_URL_LENGTH = 4096


class UnsafeUrlError(ValueError):
    """Raised when a URL is malformed or can never be a safe public target."""


def _normalise_host(hostname: str) -> str:
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("URL hostname is not valid IDNA") from exc
    return ascii_host.rstrip(".").lower()


def _reject_non_public_literal(hostname: str) -> None:
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError("Blocked unsafe target: localhost is not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global:
        raise UnsafeUrlError("Blocked unsafe target: non-public IP addresses are not allowed")


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or any(
        lowered.startswith(prefix) for prefix in _TRACKING_PREFIXES
    )


def _validated_url_parts(url: str) -> tuple[Any, str, str]:
    """Validate an http(s) URL and return its parsed form, scheme, and netloc."""
    if not isinstance(url, str):
        raise UnsafeUrlError("URL must be a string")
    candidate = url.strip()
    if not candidate or len(candidate) > _MAX_URL_LENGTH:
        raise UnsafeUrlError("URL is empty or too long")

    parsed = urlsplit(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError("Blocked unsafe target: only http and https URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeUrlError(
            "Blocked unsafe target: URLs with embedded credentials are not allowed"
        )
    if not parsed.hostname:
        raise UnsafeUrlError("Blocked unsafe target: hostname is required")

    hostname = _normalise_host(parsed.hostname)
    _reject_non_public_literal(hostname)

    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL port is not valid") from exc

    netloc = hostname
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    return parsed, scheme, netloc


def canonicalize_url(url: str) -> str:
    """Return a stable http(s) URL for caching, dedupe, and scope decisions.

    DNS is deliberately not resolved here: that is an I/O check and belongs to
    the fetcher. This function rejects schemes, credentials, localhost names,
    and non-public IP literals that no outbound request may use.
    """
    parsed, scheme, netloc = _validated_url_parts(url)

    path = parsed.path or "/"
    normalised_path = posixpath.normpath(path)
    if not normalised_path.startswith("/"):
        normalised_path = "/" + normalised_path
    if normalised_path != "/" and normalised_path.endswith("/"):
        normalised_path = normalised_path.rstrip("/")
    if not normalised_path:
        normalised_path = "/"

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)

    return urlunsplit((scheme, netloc, normalised_path, query, ""))


def normalize_request_url(url: str) -> str:
    """Normalise a URL for an outbound request without changing its path.

    Trailing slashes and dot segments are preserved: servers redirect between
    ``/docs`` and ``/docs/``, so rewriting them here would create redirect
    loops and break relative link resolution. Identity comparisons still use
    :func:`canonicalize_url`.
    """
    parsed, scheme, netloc = _validated_url_parts(url)
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def is_http_url(url: str) -> bool:
    """Whether *url* is an http(s) string, without resolving or canonicalising it."""
    try:
        return urlsplit(url).scheme.lower() in _ALLOWED_SCHEMES
    except ValueError:
        return False


def url_in_scope(url: str, base_url: str, include_subdomains: bool) -> bool:
    """Whether *url* is on the crawl's host, optionally including its subdomains."""
    try:
        target = urlsplit(canonicalize_url(url))
        base = urlsplit(canonicalize_url(base_url))
    except (UnsafeUrlError, ValueError):
        return False

    target_host = (target.hostname or "").lower()
    base_host = (base.hostname or "").lower()
    if not target_host or not base_host:
        return False
    if target_host == base_host:
        return True
    return include_subdomains and target_host.endswith("." + base_host)


def matches_url_filters(
    url: str,
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> bool:
    """Apply simple include/exclude globs; any exclude wins over includes."""
    if any(fnmatchcase(url, pattern) for pattern in exclude_patterns):
        return False
    if not include_patterns:
        return True
    return any(fnmatchcase(url, pattern) for pattern in include_patterns)


def content_hash(markdown: str) -> str:
    """Fingerprint extracted text while ignoring insignificant whitespace."""
    normalised = " ".join(markdown.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def matches_search_filter(url: str, search: str) -> bool:
    """Case-insensitive substring or ``re:`` regular-expression URL filter."""
    if not search:
        return True
    if search.startswith("re:"):
        try:
            return re.search(search[3:], url, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    return search.lower() in url.lower()
