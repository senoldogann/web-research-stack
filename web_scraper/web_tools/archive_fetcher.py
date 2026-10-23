"""Reading web.archive.org the way a browser does.

A live check (2026-10-23) found the Wayback Machine's replay endpoint answering the httpx
client HTTP 429 whatever headers it sent, while curl got HTTP 200 for the same address: the
archive tells clients apart by more than headers, and an ``archive: true`` that could not read
a replay was no use. The CDX list and the replays of web.archive.org, and of no other host,
are now read through a curl_cffi session that impersonates one Chrome, as the DuckDuckGo
searcher does.

It is the same fetcher with another transport: the size cap, the timeouts, the retries, the
challenge and wall checks and the redirect validation are ``SafeFetcher``'s. What changes is
what a request may reach: the host is fixed here, in code, to ``web.archive.org`` over https
(a redirect out of it is refused like any other target), and the public-address check of the
fetcher still runs on it.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlsplit

import httpx
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_exceptions

from web_scraper.network_safety import UnsafeTargetError, validate_outbound_url
from web_scraper.stealth import HeaderFactory
from web_scraper.web_tools.fetcher import HostGate, SafeFetcher
from web_scraper.web_tools.settings import WebToolsSettings

ARCHIVE_HOST = "web.archive.org"

# What httpx puts on a request and an impersonated browser sets itself, all of one piece: ours
# would only disagree with the TLS fingerprint they go out under.
_BROWSER_SETS = frozenset(
    {"host", "user-agent", "accept", "accept-language", "accept-encoding", "connection"}
)
_TIMEOUTS = tuple(
    getattr(curl_exceptions, name)
    for name in ("Timeout", "ConnectTimeout", "ReadTimeout")
    if hasattr(curl_exceptions, name)
)


def archive_validator(
    url: str,
    allow_private_networks: bool = False,
    *,
    public_check: Callable[..., str] = validate_outbound_url,
) -> str:
    """Accept ``https://web.archive.org`` and nothing else, then check it is a public address.

    The scheme, the host (exactly, so ``web.archive.org.evil.example`` is not it), the port and
    the absence of credentials are fixed here; ``allow_private_networks`` is never honoured.
    """
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeTargetError("Blocked unsafe target: the port is not valid") from exc
    if (
        parts.scheme.lower() != "https"
        or (parts.hostname or "").lower().rstrip(".") != ARCHIVE_HOST
        or port not in (None, 443)
        or parts.username
        or parts.password
    ):
        raise UnsafeTargetError(f"Blocked unsafe target: only https://{ARCHIVE_HOST} is read here")
    return public_check(url, allow_private_networks=False)


class ImpersonatingTransport(httpx.AsyncBaseTransport):
    """An httpx transport that sends each request through a curl_cffi session.

    One session per event loop, impersonating one Chrome (the newest the installed curl_cffi
    knows), made on first use and closed with the transport. Redirects are not followed
    (``SafeFetcher`` validates each hop), the body is streamed (so the byte cap stops a
    download), and the body arrives decoded: a ``Content-Encoding`` or ``Content-Length``
    that described the wire is left out of the answer, so httpx does not decode twice.
    """

    def __init__(
        self,
        *,
        impersonate: Optional[str] = None,
        timeout: float = 20.0,
        session_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._impersonate = impersonate or HeaderFactory.stable_impersonate_target()
        self._timeout = timeout
        self._factory = session_factory or curl_requests.AsyncSession
        self._session: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        session = self._session_for_this_loop()
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _BROWSER_SETS
        }
        timeout = (request.extensions.get("timeout") or {}).get("read") or self._timeout
        extra: dict[str, Any] = {"headers": headers} if headers else {}
        try:
            response = await session.request(
                request.method,
                str(request.url),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                **extra,
            )
        except _TIMEOUTS as exc:
            raise httpx.ReadTimeout(str(exc), request=request) from exc
        except curl_exceptions.RequestException as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc

        pairs = list(response.headers.multi_items())
        if any(name.lower() == "content-encoding" for name, _ in pairs):
            pairs = [
                (name, value)
                for name, value in pairs
                if name.lower() not in {"content-encoding", "content-length"}
            ]
        return httpx.Response(
            status_code=response.status_code,
            headers=pairs,
            stream=_CurlStream(response, request),
            extensions={"http_version": b"HTTP/2"},
        )

    async def aclose(self) -> None:
        session, self._session, self._loop = self._session, None, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - a session of a finished loop cannot be closed
                pass

    def _session_for_this_loop(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._session is None or self._loop is not loop:
            self._session = self._factory(impersonate=self._impersonate)
            self._loop = loop
        return self._session


class _CurlStream(httpx.AsyncByteStream):
    """The body of a curl_cffi response, as httpx reads it; closing it ends the download."""

    def __init__(self, response: Any, request: httpx.Request) -> None:
        self._response = response
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._response.aiter_content():
                yield chunk
        except _TIMEOUTS as exc:
            raise httpx.ReadTimeout(str(exc), request=self._request) from exc
        except curl_exceptions.RequestException as exc:
            raise httpx.ReadError(str(exc), request=self._request) from exc

    async def aclose(self) -> None:
        await self._response.aclose()


def build_archive_fetcher(settings: WebToolsSettings) -> SafeFetcher:
    """The fetcher for web.archive.org: the settings' limits, an impersonated browser's transport."""
    return SafeFetcher(
        timeout_seconds=settings.fetch_timeout_seconds,
        max_response_bytes=settings.max_response_bytes,
        max_redirects=settings.max_redirects,
        retry_attempts=settings.fetch_retries,
        max_concurrency=2,
        transport=ImpersonatingTransport(timeout=settings.fetch_timeout_seconds),
        validator=archive_validator,
        host_gate=HostGate(
            max_per_host=settings.host_concurrency,
            min_interval_ms=settings.host_min_interval_ms,
        ),
    )
