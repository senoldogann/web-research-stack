"""A walled page read another way: archive, AMP or print, feed, another outlet; never passed off as the page."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import pytest
from test_web_service import FakeFetcher, FakeSearch, _html_result, _service

from web_scraper.api_models import WebScrapeRequest
from web_scraper.mcp_server import _dispatch_error, _scrape
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult
from web_scraper.web_tools.recover import (
    amp_candidates,
    feed_candidates,
    find_feed_item,
    is_wall,
    parse_feed,
    same_story,
    title_from_url,
    wall_title,
)
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.settings import WebToolsSettings

SLUG = "openai-sell-its-top-models-google-cloud-latest-enterprise-push-2026-10-21"
URL = f"https://www.reuters.com/business/retail-consumer/{SLUG}/"
TITLE = "OpenAI to sell its top models through Google Cloud in latest enterprise push"
SENTENCE = (
    "OpenAI will sell its top models to enterprise customers through Google Cloud, the companies "
    "said on Tuesday, in the latest push to reach large businesses with its models. "
)


def _article(
    title: str = TITLE, *, sentences: int = 8, published: Optional[str] = "2026-10-21T14:00:00Z"
) -> str:
    meta = f'<meta property="article:published_time" content="{published}">' if published else ""
    paragraphs = "".join(f"<p>{SENTENCE}</p>" for _ in range(sentences))
    return (
        f"<html><head><title>{title}</title>{meta}</head><body><main><article>"
        f"<h1>{title}</h1>{paragraphs}</article></main></body></html>"
    )


def _wall(status: int = 401, detail: str = "access_blocked", body: str = "") -> FetchError:
    return FetchError(
        "blocked",
        f"Target returned an {detail} interstitial",
        url=URL,
        status_code=status,
        detail=detail,
        body=body,
    )


CDX = (
    "https://web.archive.org/cdx/search/cdx?url="
    + quote(URL, safe=":/,")
    + "&output=json&filter=statuscode:200&collapse=digest&limit=-8"
    "&fl=timestamp,original,statuscode,digest"
)
AMP = URL.rstrip("/") + "/amp"
PRINT = URL + "?print=1"
FEED = "https://www.reuters.com/feed"


def _service_for(
    pages: dict[str, FetchResult | Exception],
    *,
    search: Optional[FakeSearch] = None,
    **settings: object,
):
    fetcher = FakeFetcher(pages)
    base = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": [],
        "search_resolve_dates": "off",
    }
    base.update(settings)
    return _service(fetcher, search=search, settings=WebToolsSettings(**base)), fetcher  # type: ignore[arg-type]


def _rss(*items: str) -> str:
    return (
        '<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Feed</title>{''.join(items)}</channel></rss>"
    )


def _rss_item(link: str, *, title: str = TITLE, full: bool = True) -> str:
    body = "".join(f"<p>{SENTENCE}</p>" for _ in range(8))
    content = f"<content:encoded><![CDATA[{body}]]></content:encoded>" if full else ""
    return (
        f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid>"
        f"<description>{SENTENCE}</description>{content}</item>"
    )


def _result(url: str, title: str = TITLE, snippet: str = SENTENCE) -> SearchResult:
    return SearchResult(title, url, snippet)


CNBC = "https://www.cnbc.com/2026/10/21/openai-google-cloud-models-enterprise.html"
THEVERGE = "https://www.theverge.com/2026/10/21/openai-sells-models-through-google-cloud"
BLOG = "https://someblog.example/2026/10/my-take-on-the-openai-google-cloud-push"


RECORDED_WALL = (Path(__file__).parent / "fixtures" / "reuters_datadome_wall.html").read_text(
    encoding="utf-8"
)


class TestTheRecordedReutersWall:
    """What Reuters answered a plain client on 2026-10-24: HTTP 401, a 774-byte DataDome page."""

    def test_it_shows_no_title_of_the_story_and_names_no_other_form(self) -> None:
        assert wall_title(RECORDED_WALL) == ""  # its title is "reuters.com"
        candidates = amp_candidates(URL, RECORDED_WALL)
        assert [kind for kind, _ in candidates] == ["amp", "amp", "print", "amp"]
        assert all(address.startswith("https://www.reuters.com/") for _, address in candidates)

    @pytest.mark.asyncio
    async def test_the_story_is_searched_by_what_its_address_spells(self) -> None:
        search = FakeSearch([_result(CNBC)])
        service, fetcher = _service_for(
            {
                URL: _wall(body=RECORDED_WALL),
                CNBC: _html_result(CNBC, _article()),
            },
            search=search,
        )

        page = await service.scrape(URL)

        assert page["recovered_from"]["kind"] == "other_source" and page["url"] == CNBC
        assert search.calls[0][0].startswith("openai sell its top models google cloud latest")
        assert "-site:reuters.com" in search.calls[0][0]


class TestTheWallIsRecognised:
    @pytest.mark.parametrize("status", [401, 403, 429])
    def test_a_refusing_status_is_a_wall(self, status: int) -> None:
        error = WebToolError("http_error", "refused", status_code=status)

        assert is_wall(error)

    @pytest.mark.parametrize("detail", ["captcha", "access_blocked", "access_wall"])
    def test_a_captcha_a_bot_check_and_a_login_wall_are_walls(self, detail: str) -> None:
        assert is_wall(WebToolError("blocked", "wall", detail=detail))

    @pytest.mark.parametrize(
        "error",
        [
            WebToolError("http_error", "gone", status_code=404),
            WebToolError("http_error", "broken", status_code=500),
            WebToolError("timeout", "slow"),
            WebToolError("network_error", "down"),
        ],
    )
    def test_a_missing_page_a_server_error_and_a_timeout_are_not(self, error: WebToolError) -> None:
        assert not is_wall(error)

    @pytest.mark.asyncio
    async def test_a_page_that_is_gone_is_not_hunted_for(self) -> None:
        service, fetcher = _service_for({})

        with pytest.raises(WebToolError) as exc:
            await service.scrape(URL)

        assert exc.value.status_code == 404 and fetcher.calls == [URL]
        assert "archive: true" in exc.value.hint


class TestTheSameStoryOnTheSameSite:
    @pytest.mark.asyncio
    async def test_the_amp_form_the_wall_names_is_the_one_read(self) -> None:
        named = "https://www.reuters.com/amp/the-wall-names-this-one"
        body = f'<html><head><link rel="amphtml" href="{named}"></head><body>x</body></html>'
        service, fetcher = _service_for(
            {URL: _wall(body=body), named: _html_result(named, _article())}
        )

        page = await service.scrape(URL)

        assert page["recovered_from"] == {"kind": "amp", "url": named}
        # The answer is the page's: its address, the AMP form named in recovered_from.
        assert page["url"] == URL and page["requested_url"] == URL.rstrip("/")
        assert page["fetched_with"] == "amp"
        assert page["markdown"].startswith(
            f"> Recovered from the AMP version of {page['requested_url']}"
        )
        assert "OpenAI will sell its top models" in page["markdown"]
        assert "recovered_amp" in page["warnings"]
        assert named in fetcher.calls
        assert "AMP" in page["next_action"] and page["alternates"] is not None
        assert page["date"] == "2026-10-21"

    @pytest.mark.asyncio
    async def test_the_usual_amp_spelling_is_tried_when_the_wall_names_none(self) -> None:
        service, _ = _service_for({URL: _wall(), AMP: _html_result(AMP, _article())})

        page = await service.scrape(URL)

        assert page["recovered_from"] == {"kind": "amp", "url": AMP}

    @pytest.mark.asyncio
    async def test_a_print_form_is_read_when_there_is_no_amp(self) -> None:
        service, _ = _service_for({URL: _wall(), PRINT: _html_result(PRINT, _article())})

        page = await service.scrape(URL)

        assert page["recovered_from"] == {"kind": "print", "url": PRINT}
        assert page["markdown"].startswith("> Recovered from the print version of ")

    @pytest.mark.asyncio
    async def test_the_freshest_form_that_holds_nearly_as_much_as_the_longest_is_given(
        self,
    ) -> None:
        archive_stamp = "20261021093000"
        snapshot = f"https://web.archive.org/web/{archive_stamp}id_/{URL}"
        rows = json.dumps(
            [
                ["timestamp", "original", "statuscode", "digest"],
                [archive_stamp, URL, "200", "D"],
            ]
        )
        service, _ = _service_for(
            {
                URL: _wall(),
                CDX: FetchResult(CDX, CDX, 200, "application/json", rows),
                snapshot: _html_result(snapshot, _article(sentences=10)),
                AMP: _html_result(AMP, _article(sentences=9)),
            }
        )

        page = await service.scrape(URL)

        # The archive's copy is a little longer; the AMP form is the live site's.
        assert page["recovered_from"]["kind"] == "amp"
        assert [alternate["why"] for alternate in page["alternates"]] == [
            "the Internet Archive's copy of the same page, taken 2026-10-21T09:30:00+00:00"
        ]

    @pytest.mark.asyncio
    async def test_the_archive_copy_says_it_is_the_archives(self) -> None:
        stamp = "20261021093000"
        snapshot = f"https://web.archive.org/web/{stamp}id_/{URL}"
        rows = json.dumps(
            [
                ["timestamp", "original", "statuscode", "digest"],
                [stamp, URL, "200", "D"],
            ]
        )
        service, _ = _service_for(
            {
                URL: _wall(),
                CDX: FetchResult(CDX, CDX, 200, "application/json", rows),
                snapshot: _html_result(snapshot, _article()),
            }
        )

        page = await service.scrape(URL)

        assert page["recovered_from"] == {
            "kind": "archive",
            "url": snapshot,
            "at": "2026-10-21T09:30:00+00:00",
        }
        assert (
            page["archived_at"] == "2026-10-21T09:30:00+00:00" and page["archive_url"] == snapshot
        )
        assert page["markdown"].startswith("> Recovered from the Internet Archive's copy of ")
        assert "Not the live page" in page["markdown"].splitlines()[0]
        assert "archived_copy" in page["warnings"]
        assert "taken 2026-10-21T09:30:00+00:00" in page["next_action"]

    @pytest.mark.asyncio
    async def test_a_candidate_on_another_site_is_never_followed(self) -> None:
        body = (
            '<html><head><link rel="amphtml" href="https://evil.example/amp/x">'
            '<link rel="alternate" type="application/rss+xml" href="https://evil.example/feed">'
            "</head></html>"
        )
        service, fetcher = _service_for({URL: _wall(body=body)})

        with pytest.raises(WebToolError):
            await service.scrape(URL)

        assert not any("evil.example" in call for call in fetcher.calls)


class TestTheFeed:
    @pytest.mark.asyncio
    async def test_a_feed_that_carries_the_article_in_full_gives_it(self) -> None:
        feed = FetchResult(FEED, FEED, 200, "application/rss+xml", _rss(_rss_item(URL.rstrip("/"))))
        service, _ = _service_for({URL: _wall(), FEED: feed})

        page = await service.scrape(URL)

        assert page["recovered_from"] == {"kind": "feed", "url": FEED, "partial": False}
        assert (
            page["fetched_with"] == "feed" and "OpenAI will sell its top models" in page["markdown"]
        )
        assert "which carries the article in full" in page["markdown"].splitlines()[0]
        assert "feed_summary_only" not in page["warnings"]

    @pytest.mark.asyncio
    async def test_a_feed_with_only_a_summary_is_given_when_nothing_else_reads(self) -> None:
        feed = FetchResult(
            FEED, FEED, 200, "application/rss+xml", _rss(_rss_item(URL.rstrip("/"), full=False))
        )
        service, _ = _service_for({URL: _wall(), FEED: feed})

        page = await service.scrape(URL)

        assert page["recovered_from"] == {"kind": "feed", "url": FEED, "partial": True}
        assert "feed_summary_only" in page["warnings"]
        assert "only a summary" in page["markdown"].splitlines()[0]
        assert "summary" in page["next_action"]

    @pytest.mark.asyncio
    async def test_a_summary_goes_on_to_another_outlet_that_has_the_story_in_full(self) -> None:
        feed = FetchResult(
            FEED, FEED, 200, "application/rss+xml", _rss(_rss_item(URL.rstrip("/"), full=False))
        )
        service, _ = _service_for(
            {URL: _wall(), FEED: feed, CNBC: _html_result(CNBC, _article())},
            search=FakeSearch([_result(CNBC)]),
        )

        page = await service.scrape(URL)

        assert page["recovered_from"]["kind"] == "other_source" and page["url"] == CNBC


class TestAnotherOutlet:
    def _search(self) -> FakeSearch:
        return FakeSearch(
            [
                _result(BLOG, "My take on the OpenAI and Google Cloud push"),
                _result(
                    "https://www.reuters.com/world/other-story-2026-10-21/",
                    "OpenAI to sell its top models through Google Cloud",
                ),
                _result(CNBC),
                _result("https://example.org/unrelated", "Weather in Oulu this week", "Cold."),
                _result(THEVERGE, "OpenAI sells its top models through Google Cloud"),
            ]
        )

    @pytest.mark.asyncio
    async def test_the_story_is_read_from_the_outlet_the_title_finds(self) -> None:
        search = self._search()
        service, fetcher = _service_for(
            {
                URL: _wall(),
                CNBC: _html_result(CNBC, _article()),
                THEVERGE: _html_result(THEVERGE, _article()),
            },
            search=search,
        )

        page = await service.scrape(URL)

        # The answer is the other outlet's, and says so in every place it says anything.
        assert page["url"] == CNBC and page["requested_url"] == URL.rstrip("/")
        assert page["recovered_from"] == {
            "kind": "other_source",
            "url": CNBC,
            "domain": "cnbc.com",
            "title": TITLE,
        }
        first = page["markdown"].splitlines()[0]
        assert first.startswith("> Not the requested page. ") and "cnbc.com" in first
        assert page["canonical_url"] == CNBC
        assert "recovered_other_source" in page["warnings"]
        assert page["next_action"].startswith("The text is cnbc.com's report, not reuters.com's")
        # Cross-checked against another address, not against the one that was just read.
        assert THEVERGE in page["next_action"] and CNBC not in page["next_action"]
        # The search was for the address's words, away from the walled site.
        query = search.calls[0][0]
        assert query.startswith("openai sell its top models google cloud latest enterprise push")
        assert "-site:reuters.com" in query

    @pytest.mark.asyncio
    async def test_alternates_say_domain_title_date_type_and_why_and_leave_out_what_is_not_the_story(
        self,
    ) -> None:
        service, _ = _service_for(
            {
                URL: _wall(),
                CNBC: _html_result(CNBC, _article()),
                THEVERGE: _html_result(THEVERGE, _article()),
            },
            search=self._search(),
        )

        page = await service.scrape(URL)

        by_url = {alternate["url"]: alternate for alternate in page["alternates"]}
        # The outlets first, the blog last; the other site's weather and the walled site's own
        # pages are no alternates of the story.
        assert list(by_url) == [CNBC, THEVERGE, BLOG]
        assert by_url[BLOG]["source_type"] == "other"
        cnbc = by_url[CNBC]
        assert set(cnbc) == {"url", "domain", "title", "date", "source_type", "why"}
        assert cnbc["domain"] == "cnbc.com" and cnbc["source_type"] == "news"
        assert cnbc["date"] == "2026-10-21"
        assert "title words" in cnbc["why"] and cnbc["why"].endswith("(read)")
        assert "(read)" not in by_url[THEVERGE]["why"]

    @pytest.mark.asyncio
    async def test_the_title_the_wall_shows_is_the_one_searched(self) -> None:
        body = (
            f'<html><head><meta property="og:title" content="{TITLE} | Reuters"></head>'
            "<body>Subscribe to continue reading</body></html>"
        )
        search = FakeSearch([_result(CNBC)])
        service, _ = _service_for(
            {URL: _wall(detail="access_wall", body=body), CNBC: _html_result(CNBC, _article())},
            search=search,
        )

        await service.scrape(URL)

        assert search.calls[0][0].startswith("OpenAI to sell its top models through Google Cloud")

    @pytest.mark.asyncio
    async def test_a_captchas_own_title_is_not_searched(self) -> None:
        body = "<html><head><title>Just a moment...</title></head><body>Checking</body></html>"
        search = FakeSearch([_result(CNBC)])
        service, _ = _service_for(
            {URL: _wall(body=body), CNBC: _html_result(CNBC, _article())}, search=search
        )

        await service.scrape(URL)

        assert "just a moment" not in search.calls[0][0].lower()
        assert search.calls[0][0].startswith("openai sell its top models")

    @pytest.mark.asyncio
    async def test_candidates_that_cannot_be_read_either_are_alternates_in_the_error(self) -> None:
        service, _ = _service_for({URL: _wall()}, search=self._search())

        with pytest.raises(WebToolError) as exc:
            await service.scrape(URL)

        error = exc.value
        assert error.code == "blocked" and error.detail == "access_blocked"
        assert [alternate["url"] for alternate in error.extra["alternates"]] == [
            CNBC,
            THEVERGE,
            BLOG,
        ]
        assert CNBC in error.extra["next_action"] and "reuters.com" in error.extra["next_action"]
        tried = {item["method"]: item["result"] for item in error.extra["recovery_tried"]}
        assert tried["other sources"] == "the candidates could not be read either"
        assert tried["archive"] and tried["amp/print"] and tried["feed"]
        assert "walled" in error.hint
        envelope = json.loads(_dispatch_error(error)["content"][0]["text"])["error"]
        assert envelope["alternates"] == error.extra["alternates"]
        assert (
            envelope["next_action"] == error.extra["next_action"] and envelope["hint"] == error.hint
        )
        assert error.to_dict()["code"] == "blocked"

    @pytest.mark.asyncio
    async def test_with_no_other_site_the_error_says_to_search_again(self) -> None:
        service, _ = _service_for(
            {URL: _wall()},
            search=FakeSearch([_result("https://example.org/x", "Cold in Oulu", "Cold.")]),
        )

        with pytest.raises(WebToolError) as exc:
            await service.scrape(URL)

        assert exc.value.extra["alternates"] == []
        assert "web_search" in exc.value.extra["next_action"]

    @pytest.mark.asyncio
    async def test_the_title_of_an_address_that_spells_none_is_not_searched(self) -> None:
        url = "https://www.reuters.com/article/12345678"
        search = FakeSearch([_result(CNBC)])
        service, _ = _service_for({url: _wall()}, search=search)

        with pytest.raises(WebToolError) as exc:
            await service.scrape(url)

        assert search.calls == []
        tried = {item["method"]: item["result"] for item in exc.value.extra["recovery_tried"]}
        assert tried["other sources"] == "the address and the wall give no title to search"


class TestWhatTheCallSays:
    @pytest.mark.asyncio
    async def test_recover_false_does_nothing_about_the_wall(self) -> None:
        service, fetcher = _service_for({URL: _wall(), AMP: _html_result(AMP, _article())})

        with pytest.raises(WebToolError) as exc:
            await service.scrape(URL, recover=False)

        assert fetcher.calls == [URL] and "recover is false" in exc.value.hint

    @pytest.mark.asyncio
    async def test_archive_true_alone_asks_only_the_archive_as_it_always_did(self) -> None:
        service, fetcher = _service_for({URL: _wall(), AMP: _html_result(AMP, _article())})

        with pytest.raises(WebToolError) as exc:
            await service.scrape(URL, archive=True)

        assert fetcher.calls == [URL, CDX] and AMP not in fetcher.calls
        assert "archive" in exc.value.hint.lower()

    @pytest.mark.asyncio
    async def test_archive_with_recover_tries_everything_and_prefers_the_archive(self) -> None:
        stamp = "20261021093000"
        snapshot = f"https://web.archive.org/web/{stamp}id_/{URL}"
        rows = json.dumps(
            [
                ["timestamp", "original", "statuscode", "digest"],
                [stamp, URL, "200", "D"],
            ]
        )
        service, _ = _service_for(
            {
                URL: _wall(),
                CDX: FetchResult(CDX, CDX, 200, "application/json", rows),
                snapshot: _html_result(snapshot, _article(sentences=4)),
                AMP: _html_result(AMP, _article(sentences=9)),
            }
        )

        page = await service.scrape(URL, archive=True, recover=True)

        assert page["recovered_from"]["kind"] == "archive"
        assert [alternate["url"] for alternate in page["alternates"]] == [AMP]

    @pytest.mark.asyncio
    async def test_the_setting_off_is_absolute_whatever_a_call_says(self) -> None:
        service, fetcher = _service_for(
            {URL: _wall(), AMP: _html_result(AMP, _article())}, scrape_recover="off"
        )

        with pytest.raises(WebToolError) as exc:
            await service.scrape(URL, recover=True)

        assert fetcher.calls == [URL] and "WEB_TOOLS_SCRAPE_RECOVER=off" in exc.value.hint

    @pytest.mark.asyncio
    async def test_a_search_that_reads_its_results_does_not_hunt_for_a_walled_one(self) -> None:
        # A search names a page by its canonical address: no trailing slash.
        walled = URL.rstrip("/")
        search = FakeSearch([_result(walled, TITLE)])
        service, fetcher = _service_for(
            {walled: _wall(), AMP: _html_result(AMP, _article())}, search=search
        )

        answer = await service.search("openai google cloud models", depth="fast", read=1)

        assert answer["results"][0]["content_error"]["code"] == "blocked"
        assert fetcher.calls == [walled]

    @pytest.mark.asyncio
    async def test_a_batch_passes_the_choice_to_each_page(self) -> None:
        other = "https://example.org/fine"
        service, fetcher = _service_for(
            {
                URL: _wall(),
                AMP: _html_result(AMP, _article()),
                other: _html_result(other, _article()),
            }
        )

        result = await service.scrape_many([URL, other], recover=False)

        assert "error" in result["pages"][0] and "markdown" in result["pages"][1]
        assert AMP not in fetcher.calls

    @pytest.mark.asyncio
    async def test_the_recovered_answer_is_cached(self) -> None:
        service, fetcher = _service_for({URL: _wall(), AMP: _html_result(AMP, _article())})

        first = await service.scrape(URL)
        calls = len(fetcher.calls)
        second = await service.scrape(URL)

        assert second["cache_hit"] is True and second["recovered_from"] == first["recovered_from"]
        assert len(fetcher.calls) == calls

    def test_the_request_and_the_mcp_tool_take_recover(self) -> None:
        assert (
            WebScrapeRequest.model_validate({"url": "example.org", "recover": False}).recover
            is False
        )
        assert WebScrapeRequest.model_validate({"url": "example.org"}).recover is None

    @pytest.mark.asyncio
    async def test_the_mcp_tool_passes_it_on_only_when_it_is_set(self) -> None:
        seen: list[dict] = []

        class Spy:
            async def scrape(self, url: str, **kwargs: object) -> dict:
                seen.append(kwargs)
                return {}

        await _scrape(Spy(), {"url": "example.org/a"})  # type: ignore[arg-type]
        await _scrape(Spy(), {"url": "example.org/a", "recover": False})  # type: ignore[arg-type]

        assert "recover" not in seen[0] and seen[1]["recover"] is False


class TestTheBudget:
    @pytest.mark.asyncio
    async def test_a_recovery_that_runs_out_of_time_says_so_and_fails_with_the_wall(self) -> None:
        class Slow(FakeFetcher):
            async def fetch(self, url: str) -> FetchResult:
                if url == URL:
                    return await super().fetch(url)
                await asyncio.sleep(5)
                return await super().fetch(url)

        fetcher = Slow({URL: _wall()})
        service, _ = _service_for({}, recover_budget_seconds=1.0)
        service.fetcher = fetcher  # type: ignore[assignment]
        started = time.perf_counter()

        with pytest.raises(WebToolError) as exc:
            await service.scrape(URL)

        assert time.perf_counter() - started < 3.0
        tried = {item["method"]: item["result"] for item in exc.value.extra["recovery_tried"]}
        assert tried["archive"] == tried["amp/print"] == tried["feed"] == "out of time"

    def test_the_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        defaults = WebToolsSettings()
        assert (defaults.scrape_recover, defaults.recover_budget_seconds) == ("on", 30.0)
        monkeypatch.setenv("WEB_TOOLS_SCRAPE_RECOVER", "off")
        monkeypatch.setenv("WEB_TOOLS_RECOVER_BUDGET_SECONDS", "12")
        monkeypatch.setenv("WEB_TOOLS_RECOVER_MAX_ALTERNATES", "3")

        settings = WebToolsSettings.from_env()

        assert settings.scrape_recover == "off" and settings.recover_budget_seconds == 12.0
        assert settings.recover_max_alternates == 3


class TestTheHelpers:
    @pytest.mark.parametrize(
        ("url", "title"),
        [
            (URL, "openai sell its top models google cloud latest enterprise push"),
            ("https://example.org/news/ai-act-enters-into-force.html", "ai act enters into force"),
            ("https://example.org/2026/10/21/a-b", ""),
            ("https://example.org/article/12345678", ""),
            ("https://example.org/", ""),
            (
                "https://example.org/p/0a1b2c3d4e5f6789-big-news-about-the-labs",
                "big news about the labs",
            ),
        ],
    )
    def test_the_title_an_address_spells(self, url: str, title: str) -> None:
        assert title_from_url(url) == title

    def test_the_title_a_wall_still_shows(self) -> None:
        assert wall_title(f"<title>{TITLE} - Reuters</title>") == TITLE
        assert wall_title("<title>Just a moment...</title>") == ""
        assert wall_title("<title>Reuters</title><h1>Access Denied</h1>") == ""
        assert wall_title("") == ""

    def test_one_story_in_two_headlines(self) -> None:
        assert (
            same_story("openai sell its top models google cloud latest enterprise push", TITLE)
            == 1.0
        )
        # Another outlet's headline for it, and a blog's take on it, hold most of the same words ...
        assert same_story(
            TITLE, "OpenAI will sell models via Google Cloud in a new enterprise deal"
        )
        assert same_story(TITLE, "My take on the OpenAI and Google Cloud push")
        # ... a page about something else does not.
        assert same_story(TITLE, "Weather in Oulu this week") is None
        assert same_story(TITLE, "Google Cloud outage hits Oulu") is None

    def test_amp_and_print_candidates_are_of_the_same_site_and_at_most_four(self) -> None:
        body = (
            '<link rel="amphtml" href="/amp/x"><link rel="amphtml" href="https://evil.example/a">'
        )

        candidates = amp_candidates(URL, body)

        assert candidates[0] == ("amp", "https://www.reuters.com/amp/x")
        assert len(candidates) <= 4 and all("evil" not in url for _, url in candidates)
        assert ("print", PRINT) in candidates

    def test_feed_candidates_are_the_heads_then_the_usual_places(self) -> None:
        body = '<link rel="alternate" type="application/rss+xml" href="/arc/feed.xml">'

        candidates = feed_candidates(URL, body)

        assert candidates[0] == "https://www.reuters.com/arc/feed.xml"
        assert "https://www.reuters.com/feed" in candidates and len(candidates) <= 6

    def test_a_feed_is_read_as_rss_or_atom_and_nothing_else(self) -> None:
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Story</title>'
            '<link rel="alternate" href="https://example.org/story"/><updated>2026-10-21T10:00:00Z</updated>'
            '<content type="html">&lt;p&gt;Body&lt;/p&gt;</content></entry></feed>'
        )

        rss = parse_feed(_rss(_rss_item("https://example.org/a", title="A story here")))
        entries = parse_feed(atom)

        assert [item.link for item in rss] == [
            "https://example.org/a"
        ] and "OpenAI will sell" in rss[0].html
        assert entries[0].link == "https://example.org/story" and entries[0].html == "<p>Body</p>"
        assert parse_feed("<html><body>not a feed</body></html>") == []
        assert parse_feed("") == []
        bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><rss><channel><item><title>&b;</title><link>https://e.org/x</link></item></channel></rss>'
        assert parse_feed(bomb) == []

    def test_the_entry_of_a_page_is_found_by_its_address_then_its_title(self) -> None:
        items = parse_feed(
            _rss(
                _rss_item("https://example.org/other", title="Something else entirely about it"),
                _rss_item("https://www.reuters.com/world/the-page/", title=TITLE),
            )
        )

        assert find_feed_item(items, "https://reuters.com/world/the-page").link.endswith(
            "the-page/"
        )  # type: ignore[union-attr]
        assert find_feed_item(items, "https://example.org/moved", TITLE) is items[1]
        assert (
            find_feed_item(items, "https://example.org/moved", "A different title altogether")
            is None
        )
