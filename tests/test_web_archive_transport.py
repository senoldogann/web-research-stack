"""Wayback replays are read the way a browser reads them: web.archive.org answers httpx HTTP 429.

A live check (2026-10-23) found the archive's replay endpoint refusing the httpx client with
HTTP 429 whatever its headers, while curl got 200 for the same address; so ``archive: true``
read no copy. The CDX list and the replays of web.archive.org, and no other host, now go
through a curl_cffi session that impersonates one browser, inside the fetcher's own limits.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Optional

import pytest
from curl_cffi.requests import exceptions as curl_exceptions
from test_web_archive_fallback import ARCHIVED_HTML, CDX, URL, WALL_HTML, _cdx, _snapshot
from test_web_service import FakeFetcher, _html_result, _service

from web_scraper.web_tools.archive_fetcher import (
    ARCHIVE_HOST,
    ImpersonatingTransport,
    archive_validator,
    build_archive_fetcher,
)
from web_scraper.web_tools.fetcher import FetchError, SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

REPLAY = "https://web.archive.org/web/20250925030642id_/https://www.reuters.com/technology/ai/"


class FakeHeaders:
    def __init__(self, items: dict[str, str]) -> None:
        self._items = list(items.items())

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)


class FakeCurlResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes
        | list[bytes] = b"<html><body><main><p>The archived page.</p></main></body></html>",
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.status_code = status
        self.headers = FakeHeaders(headers or {"content-type": "text/html; charset=utf-8"})
        self.chunks = body if isinstance(body, list) else [body]
        self.read = 0
        self.closed = False

    async def aiter_content(self, chunk_size: Optional[int] = None):
        for chunk in self.chunks:
            self.read += 1
            yield chunk
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        self.closed = True


class FakeSession:
    """What curl_cffi's AsyncSession is to the transport: ``request`` and ``close``."""

    def __init__(self, *answers: FakeCurlResponse | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeCurlResponse:
        self.calls.append((method, url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self) -> None:
        self.closed = True


def _public(url: str, allow_private_networks: bool = False) -> str:
    return url  # the check of the URL is not run by a test


async def _resolved(host: str, port: int, **kwargs: Any) -> list[str]:
    return ["93.184.216.34"]  # the lookup is not run by a test either: none leaves the machine


def _fetcher(
    *answers: FakeCurlResponse | Exception, **limits: Any
) -> tuple[SafeFetcher, FakeSession, list[dict[str, Any]]]:
    session = FakeSession(*answers)
    made: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeSession:
        made.append(kwargs)
        return session

    transport = ImpersonatingTransport(
        timeout=limits.pop("timeout", 20.0), session_factory=factory, resolver=_resolved
    )
    fetcher = SafeFetcher(
        transport=transport,
        validator=functools.partial(archive_validator, public_check=_public),
        retry_attempts=limits.pop("retry_attempts", 0),
        backoff_seconds=0,
        **limits,
    )
    return fetcher, session, made


class TestReading:
    @pytest.mark.asyncio
    async def test_a_replay_is_read_through_one_impersonated_browser(self) -> None:
        fetcher, session, made = _fetcher(FakeCurlResponse(), timeout_seconds=12.0)

        async with fetcher:
            result = await fetcher.fetch(REPLAY)

        assert "The archived page." in result.html and result.status_code == 200
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("GET", REPLAY)
        assert kwargs["stream"] is True and kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == 12.0  # the fetcher's own, given on to curl
        # The impersonated browser's own headers go out: ours would only disagree with them.
        sent = {name.lower() for name in (kwargs.get("headers") or {})}
        assert not {"user-agent", "accept", "accept-language", "accept-encoding"} & sent
        assert len(made) == 1 and made[0]["impersonate"].startswith("chrome")

    @pytest.mark.asyncio
    async def test_one_session_serves_every_request_of_a_loop_and_is_closed_at_the_end(
        self,
    ) -> None:
        fetcher, session, made = _fetcher(FakeCurlResponse(), FakeCurlResponse())

        async with fetcher:
            await fetcher.fetch(REPLAY)
            await fetcher.fetch(REPLAY.replace("2025", "2024"))

        assert len(made) == 1 and len(session.calls) == 2
        assert session.closed is True

    @pytest.mark.asyncio
    async def test_a_compressed_answer_is_not_decoded_twice(self) -> None:
        # curl has decoded the body already; a header that still says gzip must not make httpx try.
        answer = FakeCurlResponse(
            headers={
                "content-type": "text/html",
                "content-encoding": "gzip",
                "content-length": "9",
            },
            body=b"<p>plain, already decoded</p>",
        )
        fetcher, _, _ = _fetcher(answer)

        async with fetcher:
            result = await fetcher.fetch(REPLAY)

        assert result.html == "<p>plain, already decoded</p>"

    @pytest.mark.asyncio
    async def test_a_pdf_replay_is_a_document(self) -> None:
        answer = FakeCurlResponse(
            body=b"%PDF-1.4 body", headers={"content-type": "application/pdf"}
        )
        fetcher, _, _ = _fetcher(answer)

        async with fetcher:
            result = await fetcher.fetch(REPLAY)

        assert result.content_type == "application/pdf" and result.content.startswith(b"%PDF-")


class TestLimits:
    @pytest.mark.parametrize(
        "url",
        [
            "http://web.archive.org/web/2025id_/https://a.example/",
            "https://archive.org/web/2025id_/https://a.example/",
            "https://web.archive.org.evil.example/web/2025id_/https://a.example/",
            "https://evil.example/web.archive.org/web/2025id_/",
            "https://web.archive.org:8443/web/2025id_/https://a.example/",
            "https://user:secret@web.archive.org/web/2025id_/https://a.example/",
            "https://127.0.0.1/web/2025id_/",
            "ftp://web.archive.org/x",
        ],
    )
    @pytest.mark.asyncio
    async def test_only_web_archive_org_over_https_is_read(self, url: str) -> None:
        fetcher, session, made = _fetcher(FakeCurlResponse())

        async with fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(url)

        assert exc.value.code == "unsafe_target"
        assert session.calls == [] and made == []

    def test_the_host_is_fixed_in_code(self) -> None:
        assert ARCHIVE_HOST == "web.archive.org"
        assert archive_validator(REPLAY, public_check=_public) == REPLAY

    @pytest.mark.asyncio
    async def test_the_public_address_check_still_runs_on_the_fixed_host(self) -> None:
        def refuse(url: str, allow_private_networks: bool = False) -> str:
            raise ValueError("resolves to a private address")

        validator = functools.partial(archive_validator, public_check=refuse)
        session = FakeSession(FakeCurlResponse())
        fetcher = SafeFetcher(
            transport=ImpersonatingTransport(
                session_factory=lambda **kw: session, resolver=_resolved
            ),
            validator=validator,
            retry_attempts=0,
        )

        async with fetcher:
            with pytest.raises(FetchError):
                await fetcher.fetch(REPLAY)

        assert session.calls == []

    @pytest.mark.asyncio
    async def test_a_redirect_within_the_archive_is_followed_one_out_of_it_is_not(self) -> None:
        nearer = "/web/20250925030700id_/https://www.reuters.com/technology/ai/"
        fetcher, session, _ = _fetcher(
            FakeCurlResponse(302, b"", {"location": nearer}), FakeCurlResponse()
        )
        async with fetcher:
            result = await fetcher.fetch(REPLAY)
        assert "The archived page." in result.html
        assert session.calls[1][1] == "https://web.archive.org" + nearer

        away, session, _ = _fetcher(
            FakeCurlResponse(302, b"", {"location": "https://evil.example/steal"}),
            FakeCurlResponse(),
        )
        async with away:
            with pytest.raises(FetchError) as exc:
                await away.fetch(REPLAY)
        assert exc.value.code == "unsafe_target" and len(session.calls) == 1

    @pytest.mark.asyncio
    async def test_redirects_are_counted(self) -> None:
        hop = {"location": "/web/20250925030642id_/https://www.reuters.com/technology/ai/"}
        fetcher, _, _ = _fetcher(
            *[FakeCurlResponse(302, b"", hop) for _ in range(4)], max_redirects=2
        )

        async with fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(REPLAY)

        assert exc.value.code == "too_many_redirects"

    @pytest.mark.asyncio
    async def test_the_size_cap_stops_the_download_and_closes_it(self) -> None:
        answer = FakeCurlResponse(body=[b"x" * 400, b"x" * 400, b"x" * 400, b"x" * 400])
        fetcher, _, _ = _fetcher(answer, max_response_bytes=1_000)

        async with fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(REPLAY)

        assert exc.value.code == "too_large"
        assert answer.read <= 3 and answer.closed is True

    @pytest.mark.asyncio
    async def test_a_declared_length_over_the_cap_is_refused_unread(self) -> None:
        answer = FakeCurlResponse(
            headers={"content-type": "text/html", "content-length": "5000"}, body=[b"x" * 10]
        )
        fetcher, _, _ = _fetcher(answer, max_response_bytes=1_000)

        async with fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(REPLAY)

        assert exc.value.code == "too_large" and answer.read == 0

    @pytest.mark.asyncio
    async def test_the_timeouts_of_curl_are_timeouts_and_its_failures_network_errors(self) -> None:
        slow, _, _ = _fetcher(curl_exceptions.ReadTimeout("took too long"))
        async with slow:
            with pytest.raises(FetchError) as timed_out:
                await slow.fetch(REPLAY)
        assert timed_out.value.code == "timeout" and timed_out.value.retryable is True

        down, _, _ = _fetcher(curl_exceptions.ConnectionError("refused"))
        async with down:
            with pytest.raises(FetchError) as failed:
                await down.fetch(REPLAY)
        assert failed.value.code == "network_error"

    @pytest.mark.asyncio
    async def test_a_429_is_an_http_error_with_its_status_and_is_tried_once_more(self) -> None:
        refused = FakeCurlResponse(429, b"<html>Too many requests</html>")
        fetcher, session, _ = _fetcher(refused, FakeCurlResponse(429, b"again"), retry_attempts=1)

        async with fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(REPLAY)

        assert (exc.value.code, exc.value.status_code) == ("http_error", 429)
        assert len(session.calls) == 2

    @pytest.mark.asyncio
    async def test_an_archived_captcha_is_still_flagged_by_the_interstitial_check(self) -> None:
        fetcher, _, _ = _fetcher(FakeCurlResponse(body=WALL_HTML.encode()))

        async with fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(REPLAY)

        assert exc.value.code == "blocked" and exc.value.detail == "captcha"

    def test_the_production_fetcher_carries_the_fixed_host_and_the_settings_limits(self) -> None:
        settings = WebToolsSettings(
            fetch_timeout_seconds=7.0, max_response_bytes=123_456, max_redirects=3, fetch_retries=2
        )

        fetcher = build_archive_fetcher(settings)

        assert isinstance(fetcher.transport, ImpersonatingTransport)
        assert fetcher.validator is archive_validator
        assert (fetcher.timeout_seconds, fetcher.max_response_bytes) == (7.0, 123_456)
        assert (fetcher.max_redirects, fetcher.retry_attempts) == (3, 2)


class TestInTheService:
    @pytest.mark.asyncio
    async def test_the_lookup_and_the_replays_go_to_the_archive_fetcher_the_page_does_not(
        self,
    ) -> None:
        stamp = "20250925030642"
        walled = FetchError("blocked", "captcha", url=URL, status_code=401, detail="captcha")
        page = FakeFetcher({URL: walled})
        archive = FakeFetcher(
            {CDX: _cdx(stamp), _snapshot(stamp): _html_result(_snapshot(stamp), ARCHIVED_HTML)}
        )
        service = _service(page)
        service._archive = archive  # type: ignore[assignment]

        result = await service.scrape(URL, archive=True)

        assert result["fetched_with"] == "archive" and result["archived_at"].startswith(
            "2025-09-25"
        )
        assert page.calls == [URL]
        assert archive.calls == [CDX, _snapshot(stamp)]

    @pytest.mark.asyncio
    async def test_a_service_made_by_hand_reads_the_archive_through_its_one_fetcher(self) -> None:
        stamp = "20250925030642"
        walled = FetchError("blocked", "captcha", url=URL, status_code=401, detail="captcha")
        only = FakeFetcher(
            {
                URL: walled,
                CDX: _cdx(stamp),
                _snapshot(stamp): _html_result(_snapshot(stamp), ARCHIVED_HTML),
            }
        )

        result = await _service(only).scrape(URL, archive=True)

        assert result["fetched_with"] == "archive" and only.calls == [URL, CDX, _snapshot(stamp)]

    @pytest.mark.asyncio
    async def test_the_server_reads_the_archive_with_its_own_fetcher_and_closes_it(self) -> None:
        service = WebToolsService.from_env()
        try:
            assert isinstance(service._archive, SafeFetcher)
            assert isinstance(service._archive.transport, ImpersonatingTransport)
            assert service._archive is not service.fetcher
        finally:
            await service.close()
