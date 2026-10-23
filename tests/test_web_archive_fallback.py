"""A page a site walls off says what to try next, and archive: true reads the Internet Archive's copy."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError
from test_web_service import ARTICLE_HTML, FakeFetcher, _html_result, _service

from web_scraper.api_models import WebScrapeRequest
from web_scraper.mcp_server import _dispatch_error, _scrape
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult

URL = "https://www.reuters.com/technology/openai-fires-three-researchers-2026-10-20/"
TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")
LOOKUP = "https://archive.org/wayback/available?" + urlencode({"url": URL, "timestamp": TODAY})
SNAPSHOT = f"https://web.archive.org/web/20261021093000id_/{URL}"
ARCHIVED_HTML = ARTICLE_HTML.replace(
    "</main>", '<p><a href="/world/story-one">The next story</a></p></main>'
)


def _captcha() -> FetchError:
    return FetchError(
        "blocked",
        "Target returned a captcha interstitial",
        url=URL,
        status_code=401,
        detail="captcha",
    )


def _json(url: str, payload: dict) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type="application/json",
        html=json.dumps(payload),
    )


def _available() -> FetchResult:
    return _json(
        LOOKUP,
        {
            "url": URL,
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "status": "200",
                    "timestamp": "20261021093000",
                    "url": f"http://web.archive.org/web/20261021093000/{URL}",
                }
            },
        },
    )


@pytest.mark.asyncio
async def test_a_walled_page_says_what_to_try_next() -> None:
    service = _service(FakeFetcher({URL: _captcha()}))

    with pytest.raises(WebToolError) as exc:
        await service.scrape(URL)

    assert exc.value.code == "blocked" and exc.value.detail == "captcha"
    assert "archive: true" in exc.value.hint
    envelope = json.loads(_dispatch_error(exc.value)["content"][0]["text"])["error"]
    assert envelope["hint"] == exc.value.hint
    assert {"code", "message", "url", "status_code", "detail"} <= set(envelope)


@pytest.mark.asyncio
async def test_archive_true_reads_the_latest_archived_copy_and_says_so() -> None:
    fetcher = FakeFetcher(
        {URL: _captcha(), LOOKUP: _available(), SNAPSHOT: _html_result(SNAPSHOT, ARCHIVED_HTML)}
    )
    service = _service(fetcher)

    result = await service.scrape(URL, archive=True)

    assert fetcher.calls == [URL, LOOKUP, SNAPSHOT]
    assert result["url"] == URL
    # The page's own links lead to its site, not into the archive.
    assert [link["url"] for link in result["links"]] == ["https://www.reuters.com/world/story-one"]
    assert result["fetched_with"] == "archive"
    assert result["archived_at"] == "2026-10-21T09:30:00+00:00"
    assert result["archive_url"] == SNAPSHOT
    assert "archived_copy" in result["warnings"]
    assert "Useful article" in result["markdown"]
    assert result["requested_url"] == URL.rstrip("/")


@pytest.mark.asyncio
async def test_archive_true_leaves_a_page_that_answers_alone() -> None:
    fetcher = FakeFetcher({URL: _html_result(URL, ARTICLE_HTML)})
    service = _service(fetcher)

    result = await service.scrape(URL, archive=True)

    assert fetcher.calls == [URL]
    assert result["fetched_with"] == "http"
    assert "archived_at" not in result


@pytest.mark.asyncio
async def test_with_no_archived_copy_the_walls_error_stands() -> None:
    nothing = _json(LOOKUP, {"url": URL, "archived_snapshots": {}})
    service = _service(FakeFetcher({URL: _captcha(), LOOKUP: nothing}))

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
    fetcher = FakeFetcher(
        {URL: _captcha(), LOOKUP: _available(), SNAPSHOT: _html_result(SNAPSHOT, ARTICLE_HTML)}
    )
    result = await _scrape(_service(fetcher), {"url": URL, "archive": True})

    assert result["fetched_with"] == "archive"
    assert WebScrapeRequest(url=URL).archive is False
    with pytest.raises(ValidationError):
        WebScrapeRequest(url=URL, archive="sometimes")


@pytest.mark.asyncio
async def test_an_archive_that_does_not_answer_is_not_called_empty() -> None:
    refused = FetchError("http_error", "Target returned HTTP 429", url=LOOKUP, status_code=429)
    service = _service(FakeFetcher({URL: _captcha(), LOOKUP: refused}))

    with pytest.raises(WebToolError) as exc:
        await service.scrape(URL, archive=True)

    assert exc.value.code == "blocked"
    assert "did not answer" in exc.value.hint and "429" in exc.value.hint
    assert "no archived copy" not in exc.value.hint
