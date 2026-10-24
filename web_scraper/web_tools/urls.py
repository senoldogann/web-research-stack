"""URL canonicalisation, scope checks, and deterministic content fingerprints."""

from __future__ import annotations

import hashlib
import posixpath
import re
from fnmatch import fnmatchcase
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web_scraper.network_safety import host_problem

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


class NotAWebAddressError(UnsafeUrlError):
    """Raised for text that is no web address at all: no scheme, or no host that is a name.

    It is the caller's slip, and the message says what an address looks like. An address that is
    one and must not be used (``localhost``, a private address, ``ftp://``, credentials in it) is an
    :class:`UnsafeUrlError` with the wording of the safety refusals, not this.
    """


# What a web address looks like, for the message that says a text is not one.
EXAMPLE_ADDRESS = "https://example.com/page"
_EXPLICIT_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
# Schemes that are written without slashes: a text that begins with one names a scheme, not a host.
_OPAQUE_SCHEMES = ("about:", "blob:", "data:", "file:", "javascript:", "mailto:", "sms:", "tel:")
_HOST_NAME = re.compile(r"^(?:[\w-]+\.)+[\w-]+$")
_HOST_SYNTAX = re.compile(r"^(?:[a-z0-9_-]+\.)*[a-z0-9_-]+$")


def has_scheme(text: str) -> bool:
    """Whether a text begins with a scheme of its own (``https://``, ``ftp://``, ``mailto:``)."""
    stripped = (text or "").strip()
    return bool(_EXPLICIT_SCHEME.match(stripped)) or stripped.lower().startswith(_OPAQUE_SCHEMES)


def bare_host_address(text: str) -> Optional[str]:
    """``https://`` and the text, when the text is a host written without its scheme.

    ``example.com``, ``example.com:8080/docs``, ``localhost:3000`` and ``[::1]/x`` are; so is
    ``127.1``, which is then refused as the address it is. Text with a space in it, a single word
    and a text with a scheme of its own are not: ``None``.
    """
    candidate = (text or "").strip()
    if not candidate or has_scheme(candidate) or re.search(r"[\s<>\"'`]", candidate):
        return None
    host = re.split(r"[/?#]", candidate, maxsplit=1)[0].rpartition("@")[2]
    if host.startswith("["):
        return (
            "https://" + candidate
            if re.fullmatch(r"\[[0-9A-Fa-f:.%]+\](?::\d{0,5})?", host)
            else None
        )
    name = re.sub(r":\d{0,5}$", "", host).rstrip(".")
    if name.lower() == "localhost" or _HOST_NAME.fullmatch(name):
        return "https://" + candidate
    return None


def not_a_web_address(text: str, why: str = "") -> NotAWebAddressError:
    """The error for a text that is no web address: what it was, and an address to give instead.

    The text is quoted as it was given, on one line and not longer than 80 characters; a text that is a
    host without its scheme is answered with the address it was probably meant to be.
    """
    shown = " ".join((text or "").split())
    shown = shown if len(shown) <= 80 else shown[:77] + "..."
    guess = bare_host_address(text)
    advice = (
        f"Did you mean {guess}?" if guess else f"Give a full address, such as {EXAMPLE_ADDRESS}"
    )
    return NotAWebAddressError(f'"{shown}" is not a web address{why}. {advice}')


def _normalise_host(hostname: str) -> str:
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("URL hostname is not valid IDNA") from exc
    return ascii_host.rstrip(".").lower()


def _reject_unsafe_host(hostname: str) -> None:
    """Refuse a host that no request may use, whatever it would resolve to: ``localhost``, an address that is not
    public (IPv6 forms that carry a private IPv4 address included) and a numeric spelling of one."""
    problem = host_problem(hostname)
    if problem is not None:
        raise UnsafeUrlError(problem)


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

    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise not_a_web_address(candidate) from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        if not has_scheme(candidate):
            # "not a url at all", "senoldogan.dev": no scheme was given, so nothing was refused.
            raise not_a_web_address(candidate)
        raise UnsafeUrlError("Blocked unsafe target: only http and https URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeUrlError(
            "Blocked unsafe target: URLs with embedded credentials are not allowed"
        )
    if not parsed.hostname:
        raise not_a_web_address(candidate, ": it names no host")

    hostname = _normalise_host(parsed.hostname)
    if ":" not in hostname and not _HOST_SYNTAX.fullmatch(hostname):
        raise not_a_web_address(candidate, ": its host is not a name")
    _reject_unsafe_host(hostname)

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


def upgrade_to_https(url: str) -> str:
    """Rewrite an ``http`` URL on the default port to ``https``; leave others alone.

    An explicit port is kept as it is: a service on ``:8080`` speaks plain HTTP.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "http":
        return url
    try:
        if parsed.port is not None:
            return url
    except ValueError:
        return url
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))


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


def url_on_domains(url: str, domains: list[str]) -> bool:
    """Whether *url* is on one of *domains* or on a subdomain of one."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


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
