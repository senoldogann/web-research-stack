"""Tests for the safe HTTP fetcher used by the web tools."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from web_scraper.network_safety import UnsafeTargetError
from web_scraper.web_tools.fetcher import FetchError, HostGate, SafeFetcher

HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


def _allow(url: str, allow_private_networks: bool = False) -> str:
    return url


def _reject_loopback(url: str, allow_private_networks: bool = False) -> str:
    if "127.0.0.1" in url:
        raise UnsafeTargetError("blocked")
    return url


@pytest.mark.asyncio
async def test_fetches_html_and_records_network_timing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=HTML_HEADERS, text="<h1>Hello</h1>")

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/page")

    assert result.status_code == 200
    assert result.html == "<h1>Hello</h1>"
    assert result.final_url == "https://example.com/page"
    assert result.content_type.startswith("text/html")
    assert result.network_ms >= 0
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_retries_a_transient_status_once() -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, headers=HTML_HEADERS, text="busy")
        return httpx.Response(200, headers=HTML_HEADERS, text="<p>ok</p>")

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=1,
        backoff_seconds=0,
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/page")

    assert result.html == "<p>ok</p>"
    assert result.attempts == 2
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_does_not_retry_a_client_error() -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, headers=HTML_HEADERS, text="missing")

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=3,
        backoff_seconds=0,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("https://example.com/page")

    assert exc.value.code == "http_error"
    assert exc.value.status_code == 404
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_rejects_oversized_responses() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=HTML_HEADERS, text="x" * 100)

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
        max_response_bytes=10,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("https://example.com/big")

    assert exc.value.code == "too_large"


@pytest.mark.asyncio
async def test_validates_every_redirect_hop() -> None:
    seen: list[str] = []

    def validator(url: str, allow_private_networks: bool = False) -> str:
        seen.append(url)
        return _reject_loopback(url, allow_private_networks)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(200, headers=HTML_HEADERS, text="secret")

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=validator,
        retry_attempts=0,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("https://example.com/start")

    assert exc.value.code == "unsafe_target"
    assert seen == ["https://example.com/start", "http://127.0.0.1/admin"]


@pytest.mark.asyncio
async def test_follows_safe_redirect_and_reports_final_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, headers=HTML_HEADERS, text="<p>moved</p>")

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/old")

    assert result.final_url == "https://example.com/new"
    assert result.html == "<p>moved</p>"


@pytest.mark.asyncio
async def test_follows_slash_redirect_without_looping() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/docs":
            return httpx.Response(301, headers={"location": "/docs/"})
        if request.url.path == "/docs/":
            return httpx.Response(200, headers=HTML_HEADERS, text="<p>docs</p>")
        return httpx.Response(404, headers=HTML_HEADERS, text="missing")

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
        max_redirects=3,
    ) as fetcher:
        result = await fetcher.fetch("https://example.com/docs")

    assert result.final_url == "https://example.com/docs/"
    assert result.html == "<p>docs</p>"


@pytest.mark.asyncio
async def test_fails_fast_on_captcha_interstitial() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=HTML_HEADERS,
            text="<html><body><div class='g-recaptcha'></div></body></html>",
        )

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("https://example.com/captcha")

    assert exc.value.code == "blocked"
    assert exc.value.detail == "captcha"


@pytest.mark.asyncio
async def test_rejects_non_text_content() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"00"
        )

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("https://example.com/file.bin")

    assert exc.value.code == "unsupported_content_type"


@pytest.mark.asyncio
async def test_network_timeout_is_reported() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("https://example.com/slow")

    assert exc.value.code == "timeout"


@pytest.mark.asyncio
async def test_host_gate_serializes_same_host_requests() -> None:
    gate = HostGate(max_per_host=1, min_interval_ms=0)
    active = 0
    peak = 0

    async def work() -> None:
        nonlocal active, peak
        async with gate.acquire("example.com"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(work(), work())

    assert peak == 1


@pytest.mark.asyncio
async def test_host_gate_allows_parallel_requests_to_different_hosts() -> None:
    gate = HostGate(max_per_host=1, min_interval_ms=0)
    active = 0
    peak = 0

    async def work(host: str) -> None:
        nonlocal active, peak
        async with gate.acquire(host):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(work("a.example"), work("b.example"))

    assert peak == 2


@pytest.mark.asyncio
async def test_same_host_queue_does_not_starve_another_host() -> None:
    started_at = time.monotonic()
    first_start: dict[str, float] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        first_start.setdefault(request.url.host, time.monotonic() - started_at)
        await asyncio.sleep(0.02)
        return httpx.Response(200, headers=HTML_HEADERS, text="<p>ok</p>")

    gate = HostGate(max_per_host=2, min_interval_ms=120)
    async with SafeFetcher(
        max_concurrency=2,
        host_gate=gate,
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        await asyncio.gather(
            *(fetcher.fetch(f"https://a.example/{index}") for index in range(4)),
            fetcher.fetch("https://b.example/page"),
        )

    assert first_start["b.example"] < 0.12
