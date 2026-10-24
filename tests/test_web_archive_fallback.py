"""A page a site walls off says what to try next, and archive: true reads a copy of it that is not a wall."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import pytest
from pydantic import ValidationError
from test_web_service import ARTICLE_HTML, FakeFetcher, _html_result, _service

from web_scraper.api_models import WebScrapeRequest
from web_scraper.mcp_server import _dispatch_error, _scrape
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult, SafeFetcher
from web_scraper.web_tools.settings import WebToolsSettings

URL = "https://www.reuters.com/technology/openai-fires-three-researchers-2026-10-20/"
CDX = (
    "https://web.archive.org/cdx/search/cdx?url="
    + quote(URL, safe=":/,")
    + (
        "&output=json&filter=statuscode:200&collapse=digest&limit=-8"
        "&fl=timestamp,original,statuscode,digest"
    )
)
HEADER = ["timestamp", "original", "statuscode", "digest"]
ARCHIVED_HTML = ARTICLE_HTML.replace(
    "</main>", '<p><a href="/world/story-one">The next story</a></p></main>'
)
WALL_HTML = (
    "<html><head><title>Reuters</title></head><body><p>Please verify you are human</p>"
    '<div class="g-recaptcha" data-sitekey="x"></div></body></html>'
)


def _snapshot(stamp: str, original: str = URL) -> str:
    return f"https://web.archive.org/web/{stamp}id_/{original}"


def _captcha(url: str = URL, status: int = 401) -> FetchError:
    return FetchError(
        "blocked",
        "Target returned a captcha interstitial",
        url=url,
        status_code=status,
        detail="captcha",
    )


def _json(url: str, payload: object) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type="application/json",
        html=json.dumps(payload),
    )


def _cdx(*stamps: str, original: str = URL) -> FetchResult:
    """What the CDX API answers: a header row, then the copies, the oldest first."""
    rows = [[stamp, original, "200", f"DIGEST{stamp}"] for stamp in stamps]
    return _json(CDX, [HEADER, *rows])


class ArchiveFetcher(FakeFetcher):
    """Answers the CDX lookup and the pages; a stall makes every snapshot read slow."""

    def __init__(self, pages: dict[str, FetchResult | Exception], stall: float = 0.0) -> None:
        super().__init__(pages)
        self.stall = stall

    async def fetch(self, url: str) -> FetchResult:
        if self.stall and "/web/" in url:
            self.calls.append(url)
            await asyncio.sleep(self.stall)
            return await self._answer(url)
        return await super().fetch(url)

    async def _answer(self, url: str) -> FetchResult:
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


def _iso(stamp: str) -> str:
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_a_walled_page_says_what_to_try_next() -> None:
    # recover: false is the call that does nothing about the wall; by default the server tries
    # every way round it first (tests/test_web_scrape_recover.py).
    service = _service(FakeFetcher({URL: _captcha()}))

    with pytest.raises(WebToolError) as exc:
        await service.scrape(URL, recover=False)

    assert exc.value.code == "blocked" and exc.value.detail == "captcha"
    assert "archive: true" in exc.value.hint
    envelope = json.loads(_dispatch_error(exc.value)["content"][0]["text"])["error"]
    assert envelope["hint"] == exc.value.hint
    assert {"code", "message", "url", "status_code", "detail"} <= set(envelope)


@pytest.mark.asyncio
async def test_archive_true_reads_the_latest_archived_copy_and_says_so() -> None:
    stamp = "20261021093000"
    fetcher = FakeFetcher(
        {
            URL: _captcha(),
            CDX: _cdx("20261001000000", stamp),
            _snapshot(stamp): _html_result(_snapshot(stamp), ARCHIVED_HTML),
        }
    )
    service = _service(fetcher)

    result = await service.scrape(URL, archive=True)

    assert fetcher.calls == [URL, CDX, _snapshot(stamp)]
    assert result["url"] == URL
    # The page's own links lead to its site, not into the archive.
    assert [link["url"] for link in result["links"]] == ["https://www.reuters.com/world/story-one"]
    assert result["fetched_with"] == "archive"
    assert result["archived_at"] == "2026-10-21T09:30:00+00:00"
    assert result["archive_url"] == _snapshot(stamp)
    assert result["archive_skipped"] == []
    assert "archived_copy" in result["warnings"]
    assert "Useful article" in result["markdown"]
    assert result["requested_url"] == URL.rstrip("/")


@pytest.mark.asyncio
async def test_the_lookup_asks_for_the_last_copies_that_answered_200_one_per_digest() -> None:
    fetcher = FakeFetcher({URL: _captcha(), CDX: _cdx()})
    service = _service(fetcher)

    with pytest.raises(WebToolError):
        await service.scrape(URL, archive=True)

    lookup = fetcher.calls[1]
    assert lookup.startswith("https://web.archive.org/cdx/search/cdx?url=https://www.reuters.com/")
    for part in (
        "output=json",
        "filter=statuscode:200",
        "collapse=digest",
        "limit=-8",
        "fl=timestamp,original,statuscode,digest",
    ):
        assert part in lookup


@pytest.mark.asyncio
async def test_a_copy_that_is_a_captcha_is_skipped_for_an_older_good_one() -> None:
    newest, newer, good = "20261021093000", "20261015080000", "20260918100000"
    fetcher = FakeFetcher(
        {
            URL: _captcha(),
            CDX: _cdx(good, newer, newest),
            _snapshot(newest): _captcha(_snapshot(newest), status=200),
            _snapshot(newer): FetchError(
                "blocked",
                "Target returned an access_wall interstitial",
                url=_snapshot(newer),
                status_code=200,
                detail="access_wall",
            ),
            _snapshot(good): _html_result(_snapshot(good), ARCHIVED_HTML),
        }
    )
    service = _service(fetcher)

    result = await service.scrape(URL, archive=True)

    assert fetcher.calls == [URL, CDX, _snapshot(newest), _snapshot(newer), _snapshot(good)]
    assert result["fetched_with"] == "archive"
    assert result["archived_at"] == _iso(good)
    assert result["archive_url"] == _snapshot(good)
    assert result["archive_skipped"] == [
        {"timestamp": _iso(newest), "reason": "captcha"},
        {"timestamp": _iso(newer), "reason": "access_wall"},
    ]


@pytest.mark.asyncio
async def test_the_snapshot_is_read_at_the_address_the_copy_was_archived_under() -> None:
    stamp = "20261021093000"
    archived_under = "http://reuters.com/technology/openai-fires-three-researchers-2026-10-20"
    fetcher = FakeFetcher(
        {
            URL: _captcha(),
            CDX: _cdx(stamp, original=archived_under),
            _snapshot(stamp, archived_under): _html_result(
                _snapshot(stamp, archived_under), ARCHIVED_HTML
            ),
        }
    )

    result = await _service(fetcher).scrape(URL, archive=True)

    assert result["archive_url"] == _snapshot(stamp, archived_under)
    assert result["url"] == URL


@pytest.mark.asyncio
async def test_it_stops_after_four_copies() -> None:
    stamps = [f"2026100{day}120000" for day in range(1, 7)]
    pages: dict[str, FetchResult | Exception] = {URL: _captcha(), CDX: _cdx(*stamps)}
    for stamp in stamps:
        pages[_snapshot(stamp)] = _captcha(_snapshot(stamp), status=200)
    fetcher = FakeFetcher(pages)

    with pytest.raises(WebToolError) as exc:
        await _service(fetcher).scrape(URL, archive=True)

    assert fetcher.calls == [URL, CDX, *(_snapshot(stamp) for stamp in reversed(stamps[-4:]))]
    assert exc.value.code == "blocked" and exc.value.detail == "captcha"
    assert "walls" in exc.value.hint and "captcha" in exc.value.hint
    assert "2026-10-06" in exc.value.hint and "web_search" in exc.value.hint


@pytest.mark.asyncio
async def test_a_copy_with_nothing_to_read_is_skipped() -> None:
    empty, good = "20261021093000", "20261001000000"
    blank = "<html><head><title>x</title></head><body></body></html>"
    fetcher = FakeFetcher(
        {
            URL: _captcha(),
            CDX: _cdx(good, empty),
            _snapshot(empty): _html_result(_snapshot(empty), blank),
            _snapshot(good): _html_result(_snapshot(good), ARCHIVED_HTML),
        }
    )

    result = await _service(fetcher).scrape(URL, archive=True)

    assert result["archived_at"] == _iso(good)
    assert result["archive_skipped"] == [{"timestamp": _iso(empty), "reason": "empty_content"}]


@pytest.mark.asyncio
async def test_the_time_budget_ends_the_walk() -> None:
    first, second = "20261021093000", "20261001000000"
    fetcher = ArchiveFetcher(
        {
            URL: _captcha(),
            CDX: _cdx(second, first),
            _snapshot(first): _html_result(_snapshot(first), ARCHIVED_HTML),
            _snapshot(second): _html_result(_snapshot(second), ARCHIVED_HTML),
        },
        stall=0.5,
    )
    settings = WebToolsSettings(archive_budget_seconds=0.2, search_provider="fake")

    with pytest.raises(WebToolError) as exc:
        await _service(fetcher, settings=settings).scrape(URL, archive=True)

    assert exc.value.code == "blocked"
    assert "time" in exc.value.hint and "WEB_TOOLS_ARCHIVE_BUDGET_SECONDS" in exc.value.hint
    assert fetcher.calls == [URL, CDX, _snapshot(first)]


def test_the_budget_is_a_setting_of_twenty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_TOOLS_ARCHIVE_BUDGET_SECONDS", raising=False)
    assert WebToolsSettings().archive_budget_seconds == 20.0
    assert WebToolsSettings.from_env().archive_budget_seconds == 20.0

    monkeypatch.setenv("WEB_TOOLS_ARCHIVE_BUDGET_SECONDS", "7.5")

    assert WebToolsSettings.from_env().archive_budget_seconds == 7.5


@pytest.mark.asyncio
async def test_archive_true_leaves_a_page_that_answers_alone() -> None:
    fetcher = FakeFetcher({URL: _html_result(URL, ARTICLE_HTML)})
    service = _service(fetcher)

    result = await service.scrape(URL, archive=True)

    assert fetcher.calls == [URL]
    assert result["fetched_with"] == "http"
    assert "archived_at" not in result and "archive_skipped" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["empty", "header_only", "none_found"])
async def test_with_no_archived_copy_the_walls_error_stands(answer: str) -> None:
    body = {"empty": [], "header_only": [HEADER], "none_found": []}[answer]
    nothing = _json(CDX, body)
    if answer == "empty":
        nothing = FetchResult(CDX, CDX, 200, "application/json", "")
    service = _service(FakeFetcher({URL: _captcha(), CDX: nothing}))

    with pytest.raises(WebToolError) as exc:
        await service.scrape(URL, archive=True)

    assert exc.value.code == "blocked" and exc.value.detail == "captcha"
    assert "no archived copy" in exc.value.hint


@pytest.mark.asyncio
async def test_an_unresolvable_host_gets_no_archive_and_no_hint() -> None:
    missing = "https://this-domain-definitely-does-not-exist-xyz123.com/"
    error = FetchError("dns_error", "Could not resolve host", url=missing, detail="x")
    fetcher = FakeFetcher({missing: error})
    service = _service(fetcher)

    with pytest.raises(WebToolError) as exc:
        await service.scrape(missing, archive=True)

    assert exc.value.code == "dns_error"
    assert exc.value.hint is None
    assert fetcher.calls == [missing]
    assert "hint" not in exc.value.to_dict()


@pytest.mark.asyncio
async def test_the_mcp_tool_takes_archive() -> None:
    stamp = "20261021093000"
    fetcher = FakeFetcher(
        {
            URL: _captcha(),
            CDX: _cdx(stamp),
            _snapshot(stamp): _html_result(_snapshot(stamp), ARTICLE_HTML),
        }
    )
    result = await _scrape(_service(fetcher), {"url": URL, "archive": True})

    assert result["fetched_with"] == "archive"
    assert WebScrapeRequest(url=URL).archive is False
    with pytest.raises(ValidationError):
        WebScrapeRequest(url=URL, archive="sometimes")


@pytest.mark.asyncio
async def test_a_429_from_the_archive_is_said_as_such_with_a_hint_to_retry() -> None:
    refused = FetchError("http_error", "Target returned HTTP 429", url=CDX, status_code=429)
    service = _service(FakeFetcher({URL: _captcha(), CDX: refused}))

    with pytest.raises(WebToolError) as exc:
        await service.scrape(URL, archive=True)

    assert exc.value.code == "blocked"
    assert "429" in exc.value.hint and "later" in exc.value.hint
    assert "no archived copy" not in exc.value.hint


@pytest.mark.asyncio
async def test_a_429_on_a_snapshot_ends_the_walk_with_the_same_hint() -> None:
    newest, older = "20261021093000", "20261001000000"
    fetcher = FakeFetcher(
        {
            URL: _captcha(),
            CDX: _cdx(older, newest),
            _snapshot(newest): FetchError(
                "http_error", "Target returned HTTP 429", url=_snapshot(newest), status_code=429
            ),
            _snapshot(older): _html_result(_snapshot(older), ARCHIVED_HTML),
        }
    )

    with pytest.raises(WebToolError) as exc:
        await _service(fetcher).scrape(URL, archive=True)

    assert "429" in exc.value.hint and "later" in exc.value.hint
    assert fetcher.calls == [URL, CDX, _snapshot(newest)]


@pytest.mark.asyncio
async def test_an_archive_that_does_not_answer_is_not_called_empty() -> None:
    down = FetchError("network_error", "Network error: ReadError", url=CDX)
    service = _service(FakeFetcher({URL: _captcha(), CDX: down}))

    with pytest.raises(WebToolError) as exc:
        await service.scrape(URL, archive=True)

    assert "did not answer" in exc.value.hint and "network_error" in exc.value.hint
    assert "no archived copy" not in exc.value.hint


@pytest.mark.asyncio
async def test_the_real_interstitial_check_skips_an_archived_captcha_page() -> None:
    newest, good = "20261021093000", "20260918100000"
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        html = {"content-type": "text/html"}
        if url == URL:
            return httpx.Response(401, headers=html, text=WALL_HTML)
        if url.startswith("https://web.archive.org/cdx/"):
            body = _cdx(good, newest).html
            return httpx.Response(200, headers={"content-type": "application/json"}, text=body)
        if url == _snapshot(newest):
            return httpx.Response(200, headers=html, text=WALL_HTML)
        if url == _snapshot(good):
            return httpx.Response(200, headers=html, text=ARCHIVED_HTML)
        return httpx.Response(404, headers=html, text="missing")

    def allow(url: str, allow_private_networks: bool = False) -> str:
        return url

    async with SafeFetcher(
        transport=httpx.MockTransport(handler), validator=allow, retry_attempts=0
    ) as fetcher:
        result = await _service(fetcher).scrape(URL, archive=True)  # type: ignore[arg-type]

    assert result["fetched_with"] == "archive"
    assert result["archived_at"] == _iso(good)
    assert result["archive_skipped"] == [{"timestamp": _iso(newest), "reason": "captcha"}]
    assert "Useful article" in result["markdown"]
