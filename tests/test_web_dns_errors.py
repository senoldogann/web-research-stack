"""A host that does not resolve is a DNS failure, not an unsafe target."""

from __future__ import annotations

import socket

import httpx
import pytest

from web_scraper.mcp_server import _dispatch_error
from web_scraper.network_safety import (
    HostnameResolutionError,
    UnsafeTargetError,
    validate_outbound_url,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

MISSING_HOST = "this-domain-definitely-does-not-exist-xyz123.com"


@pytest.fixture
def unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(hostname: str, port: int) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    monkeypatch.setattr("web_scraper.network_safety.resolve_hostname", fail)


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request may leave for {request.url}")


def test_validation_reports_an_unresolvable_host_by_name(unresolvable: None) -> None:
    with pytest.raises(HostnameResolutionError) as exc:
        validate_outbound_url(f"https://{MISSING_HOST}/")

    assert exc.value.hostname == MISSING_HOST
    # The older scrapers catch UnsafeTargetError and still refuse the target.
    assert isinstance(exc.value, UnsafeTargetError)


def test_localhost_and_private_addresses_stay_unsafe_targets() -> None:
    for url in ("http://localhost:8000/", "http://127.0.0.1/admin", "http://10.0.0.8/"):
        with pytest.raises(UnsafeTargetError) as exc:
            validate_outbound_url(url)
        assert not isinstance(exc.value, HostnameResolutionError), url


@pytest.mark.asyncio
async def test_fetcher_classifies_a_dns_failure_as_dns_error(unresolvable: None) -> None:
    async with SafeFetcher(transport=httpx.MockTransport(_no_network), retry_attempts=0) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch(f"https://{MISSING_HOST}/")

    assert exc.value.code == "dns_error"
    assert exc.value.detail == MISSING_HOST
    assert MISSING_HOST in str(exc.value)


@pytest.mark.asyncio
async def test_fetcher_keeps_localhost_an_unsafe_target() -> None:
    async with SafeFetcher(transport=httpx.MockTransport(_no_network), retry_attempts=0) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("http://localhost:8000/")

    assert exc.value.code == "unsafe_target"


@pytest.mark.asyncio
async def test_scrape_of_an_unresolvable_host_returns_the_dns_error_envelope(
    unresolvable: None,
) -> None:
    async with SafeFetcher(transport=httpx.MockTransport(_no_network), retry_attempts=0) as fetcher:
        service = WebToolsService(
            settings=WebToolsSettings(browser_enabled=False),
            fetcher=fetcher,
            browser=None,
            search_adapters={},
        )
        with pytest.raises(WebToolError) as exc:
            await service.scrape(f"https://{MISSING_HOST}/")

    assert exc.value.code == "dns_error"
    assert exc.value.http_status == 502
    assert exc.value.to_dict() == {
        "code": "dns_error",
        "message": f"Could not resolve host {MISSING_HOST}",
        "url": f"https://{MISSING_HOST}/",
        "status_code": None,
        "detail": MISSING_HOST,
    }
    envelope = _dispatch_error(exc.value)
    assert envelope["isError"] is True
    assert '"code":"dns_error"' in envelope["content"][0]["text"]


def test_unsafe_target_keeps_its_status() -> None:
    assert WebToolError("unsafe_target", "blocked").http_status == 400
    assert WebToolError("dns_error", "unresolved").http_status == 502
