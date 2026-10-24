"""Safe, bounded HTTP fetching with redirect validation and retries."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from web_scraper.network_safety import (
    HostnameResolutionError,
    UnsafeTargetError,
    validate_outbound_url,
)
from web_scraper.web_tools.content import detect_interstitial, interstitial_message
from web_scraper.web_tools.pdf import PDF_CONTENT_TYPE, looks_like_pdf
from web_scraper.web_tools.urls import UnsafeUrlError, normalize_request_url

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
# How much of a refusing page a FetchError keeps: its head, where the title and the page's other
# forms (AMP, feeds) are named.
_REFUSAL_BODY_KEPT = 30_000
_TEXTUAL_TYPES = {
    "application/atom+xml",
    "application/json",
    "application/ld+json",
    "application/rss+xml",
    "application/xhtml+xml",
    "application/xml",
}
_SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-language",
    "content-type",
    "date",
    "etag",
    "last-modified",
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    """A classified fetch failure that is safe to return to callers."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        url: str = "",
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
        retryable: bool = False,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.url = url
        self.status_code = status_code
        self.detail = detail
        self.retryable = retryable
        # The first bytes of the page that refused (a wall's own head), kept for the code that
        # tries to read the page another way; it is not part of ``to_dict``.
        self.body = body

    def to_dict(self) -> dict[str, Any]:
        """Serialise the error without leaking request headers or bodies."""
        return {
            "code": self.code,
            "message": str(self),
            "url": self.url,
            "status_code": self.status_code,
            "detail": self.detail,
            "retryable": self.retryable,
        }


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str
    html: str
    headers: dict[str, str] = field(default_factory=dict)
    network_ms: float = 0.0
    total_ms: float = 0.0
    attempts: int = 1
    # Raw body of a binary document (a PDF); ``html`` is empty then.
    content: bytes = b""


@dataclass
class _HostState:
    semaphore: asyncio.Semaphore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_started: float = 0.0


class HostGate:
    """Bounded per-host concurrency plus a minimum delay between request starts."""

    def __init__(self, max_per_host: int = 2, min_interval_ms: int = 0) -> None:
        self.max_per_host = max(max_per_host, 1)
        self.min_interval_seconds = max(min_interval_ms, 0) / 1000.0
        self._hosts: dict[str, _HostState] = {}

    def _state(self, host: str) -> _HostState:
        state = self._hosts.get(host)
        if state is None:
            state = _HostState(semaphore=asyncio.Semaphore(self.max_per_host))
            self._hosts[host] = state
        return state

    @asynccontextmanager
    async def acquire(self, host: str) -> AsyncIterator[None]:
        """Hold one of the host's concurrency slots."""
        state = self._state(host)
        async with state.semaphore:
            yield

    async def wait_for_turn(self, host: str) -> None:
        """Block until the host may start another request."""
        state = self._state(host)
        async with state.lock:
            wait = self.min_interval_seconds - (time.monotonic() - state.last_started)
            if wait > 0:
                await asyncio.sleep(wait)
            state.last_started = time.monotonic()


class SafeFetcher:
    """Fetch public HTTP targets with size, redirect, and redirect-hop guards."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 5_000_000,
        max_redirects: int = 5,
        retry_attempts: int = 1,
        backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 5.0,
        max_concurrency: int = 8,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        validator: Callable[..., str] = validate_outbound_url,
        allow_private_networks: bool = False,
        host_gate: Optional[HostGate] = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.timeout_seconds = max(timeout_seconds, 0.1)
        self.max_response_bytes = max(max_response_bytes, 1)
        self.max_redirects = max(max_redirects, 0)
        self.retry_attempts = max(retry_attempts, 0)
        self.backoff_seconds = max(backoff_seconds, 0.0)
        self.max_backoff_seconds = max(max_backoff_seconds, 0.0)
        self.transport = transport
        self.validator = validator
        self.allow_private_networks = allow_private_networks
        self.host_gate = host_gate or HostGate(max_per_host=max_concurrency)
        self._global_gate = asyncio.Semaphore(max(max_concurrency, 1))
        self.headers = {
            "user-agent": user_agent,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
            "accept-language": "en-US,en;q=0.9",
        }
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> SafeFetcher:
        self._client = self._build_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
            transport=self.transport,
            headers=self.headers,
            trust_env=False,
        )

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _validate(self, url: str) -> str:
        try:
            self.validator(url, allow_private_networks=self.allow_private_networks)
            return normalize_request_url(url)
        except HostnameResolutionError as exc:
            raise FetchError("dns_error", str(exc), url=url, detail=exc.hostname) from exc
        except (UnsafeTargetError, UnsafeUrlError, ValueError) as exc:
            raise FetchError("unsafe_target", str(exc), url=url) from exc

    async def fetch(self, url: str) -> FetchResult:
        """Fetch *url* after validating every hop, retrying only transient errors."""
        started = time.monotonic()
        current = self._validate(url)

        for attempt in range(self.retry_attempts + 1):
            try:
                return await self._fetch_once(
                    original_url=url,
                    start_url=current,
                    attempts=attempt + 1,
                    started=started,
                )
            except FetchError as exc:
                if not exc.retryable or attempt >= self.retry_attempts:
                    raise
                delay = min(self.backoff_seconds * (2**attempt), self.max_backoff_seconds)
                if delay > 0:
                    await asyncio.sleep(delay)

        raise FetchError("network_error", "Fetch exhausted retries", url=url)

    async def fetch_head(
        self, url: str, *, max_bytes: int = 65_536, head_cap: Optional[int] = None
    ) -> FetchResult:
        """Fetch only the first *max_bytes* of a page, once, and close the connection.

        Enough for a page's ``<head>``, where its meta tags and structured data are, and for the
        byline near its headline; the rest of a long page is never downloaded. With *head_cap*
        (larger than *max_bytes*) a head that has not ended by *max_bytes* is read on to its end
        (``</head>``) or to *head_cap*, whichever comes first: some sites' heads are 170 KB of
        inline scripts with the structured data in the middle. Every hop is validated as in
        :meth:`fetch`, and a refusal or an error status raises as it does there; what differs is
        that a long body is not an error (``too_large``): it is cut, and a page cut is content,
        never judged a challenge. There is no retry: the caller has a budget.
        """
        started = time.monotonic()
        return await self._fetch_once(
            original_url=url,
            start_url=self._validate(url),
            attempts=1,
            started=started,
            head_bytes=max(int(max_bytes), 1),
            head_cap=max(int(head_cap), int(max_bytes)) if head_cap else None,
        )

    async def _fetch_once(
        self,
        *,
        original_url: str,
        start_url: str,
        attempts: int,
        started: float,
        head_bytes: Optional[int] = None,
        head_cap: Optional[int] = None,
    ) -> FetchResult:
        current = start_url
        redirects = 0
        client = self._client_or_create()

        while True:
            host = (urlsplit(current).hostname or "").lower()
            request_started = time.monotonic()
            try:
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(self.host_gate.acquire(host))
                    await stack.enter_async_context(self._global_gate)
                    await self.host_gate.wait_for_turn(host)
                    async with client.stream("GET", current) as response:
                        content_type = (
                            response.headers.get("content-type", "").split(";")[0].strip().lower()
                        )
                        status = response.status_code

                        if status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchError(
                                    "http_error",
                                    "Redirect response did not include a Location header",
                                    url=current,
                                    status_code=status,
                                )
                            redirects += 1
                            if redirects > self.max_redirects:
                                raise FetchError(
                                    "too_many_redirects",
                                    f"Exceeded {self.max_redirects} redirects",
                                    url=original_url,
                                    status_code=status,
                                )
                            current = self._validate(urljoin(current, location))
                            continue

                        cut = False
                        if head_bytes is None:
                            body = await self._read_body(response, content_type)
                        else:
                            body, cut = await self._read_head(response, head_bytes, head_cap)
                        network_ms = (time.monotonic() - request_started) * 1000

                        if status >= 400:
                            html = self._decode(body, response)
                            interstitial = (
                                detect_interstitial(
                                    html, str(response.url), status, content_type=content_type
                                )
                                if self._is_textual(content_type)
                                else None
                            )
                            if interstitial:
                                raise FetchError(
                                    "blocked",
                                    interstitial_message(interstitial),
                                    url=current,
                                    status_code=status,
                                    detail=interstitial,
                                    body=html[:_REFUSAL_BODY_KEPT],
                                )
                            raise FetchError(
                                "http_error",
                                f"Target returned HTTP {status}",
                                url=current,
                                status_code=status,
                                retryable=status in _RETRYABLE_STATUS,
                                body=html[:_REFUSAL_BODY_KEPT]
                                if self._is_textual(content_type)
                                else "",
                            )

                        safe_headers = {
                            key: value
                            for key, value in response.headers.items()
                            if key.lower() in _SAFE_RESPONSE_HEADERS
                        }
                        if looks_like_pdf(content_type, body):
                            return FetchResult(
                                url=original_url,
                                final_url=normalize_request_url(str(response.url)),
                                status_code=status,
                                content_type=PDF_CONTENT_TYPE,
                                html="",
                                headers=safe_headers,
                                network_ms=network_ms,
                                total_ms=(time.monotonic() - started) * 1000,
                                attempts=attempts,
                                content=body,
                            )

                        if not self._is_textual(content_type):
                            raise FetchError(
                                "unsupported_content_type",
                                f"Unsupported content type: {content_type or 'unknown'}",
                                url=current,
                                status_code=status,
                                detail=content_type,
                            )

                        html = self._decode(body, response)
                        interstitial = (
                            None
                            if cut
                            else detect_interstitial(
                                html, str(response.url), status, content_type=content_type
                            )
                        )
                        if interstitial:
                            raise FetchError(
                                "blocked",
                                interstitial_message(interstitial),
                                url=current,
                                status_code=status,
                                detail=interstitial,
                                body=html[:_REFUSAL_BODY_KEPT],
                            )

                        return FetchResult(
                            url=original_url,
                            final_url=normalize_request_url(str(response.url)),
                            status_code=status,
                            content_type=content_type,
                            html=html,
                            headers=safe_headers,
                            network_ms=network_ms,
                            total_ms=(time.monotonic() - started) * 1000,
                            attempts=attempts,
                        )
            except FetchError:
                raise
            except httpx.TimeoutException as exc:
                raise FetchError(
                    "timeout",
                    "Target request timed out",
                    url=current,
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise FetchError(
                    "network_error",
                    f"Network error: {type(exc).__name__}",
                    url=current,
                    retryable=True,
                ) from exc
            except ValueError as exc:
                raise FetchError("unsafe_target", str(exc), url=current) from exc

    async def _read_body(self, response: httpx.Response, content_type: str) -> bytes:
        declared_length = response.headers.get("content-length")
        if declared_length and declared_length.isdigit():
            if int(declared_length) > self.max_response_bytes:
                raise FetchError(
                    "too_large",
                    "Target response is larger than the configured limit",
                    url=str(response.url),
                    status_code=response.status_code,
                )

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.max_response_bytes:
                raise FetchError(
                    "too_large",
                    "Target response exceeded the configured byte limit",
                    url=str(response.url),
                    status_code=response.status_code,
                    detail=content_type,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _read_head(
        response: httpx.Response, head_bytes: int, head_cap: Optional[int] = None
    ) -> tuple[bytes, bool]:
        """The first *head_bytes* of a body, and whether the body went on past them.

        With *head_cap*, a body whose ``<head>`` has not ended by *head_bytes* is read on until
        it has, or to *head_cap*.
        """
        chunks: list[bytes] = []
        total = 0
        ceiling = head_bytes
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total <= head_bytes:
                continue
            if head_cap is None:
                break
            ended = b"".join(chunks).lower().find(b"</head")
            if ended >= 0:
                ceiling = max(head_bytes, ended + len(b"</head>"))
                break
            ceiling = head_cap
            if total >= head_cap:
                break
        body = b"".join(chunks)
        return body[:ceiling], len(body) > ceiling

    @staticmethod
    def _is_textual(content_type: str) -> bool:
        return content_type.startswith("text/") or content_type in _TEXTUAL_TYPES

    @staticmethod
    def _decode(body: bytes, response: httpx.Response) -> str:
        encoding = response.charset_encoding or "utf-8"
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")
