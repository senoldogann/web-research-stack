"""Dates you can trust: every signal a page gives, weighed together, with the conflicts said."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from test_web_service import FakeFetcher, _html_result, _service

from web_scraper.web_tools.content import extract_page, extract_structured, resolve_html_dates
from web_scraper.web_tools.dates import (
    LOW_CONFIDENCE,
    DateSignal,
    make_signal,
    resolve,
    text_signals,
)
from web_scraper.web_tools.fields import normalize_date

TODAY = date(2026, 10, 23)
URL = "https://cloud.google.com/blog/products/compute/introducing-trillium-6th-gen-tpus"


def _page(head: str = "", body: str = "<h1>Introducing Trillium</h1><p>The story.</p>") -> str:
    return f"<html><head><title>Introducing Trillium</title>{head}</head><body><main><article>{body}</article></main></body></html>"


def _ld(**fields: str) -> str:
    import json

    item = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Trillium"}
    item.update(fields)
    return f'<script type="application/ld+json">{json.dumps(item)}</script>'


def _resolve(html: str, url: str = "https://example.org/story"):
    return resolve_html_dates(html, url, today=TODAY)


class TestEachSignal:
    def test_json_ld_date_published_is_the_date(self) -> None:
        resolved = _resolve(_page(_ld(datePublished="2026-10-21T08:30:00+02:00")))

        assert resolved.date == "2026-10-21"
        assert resolved.date_source == "json_ld:datePublished"
        assert resolved.moment == "2026-10-21T08:30:00+02:00"
        assert resolved.date_confidence == pytest.approx(0.8)
        assert resolved.date_conflicts == []

    def test_json_ld_date_modified_is_the_modified_date_and_never_the_published_one(self) -> None:
        resolved = _resolve(_page(_ld(dateModified="2026-10-22")))

        assert resolved.date is None and resolved.date_confidence == 0.0
        assert resolved.date_modified == "2026-10-22"
        assert resolved.fields()["date_modified"] == "2026-10-22"

    @pytest.mark.parametrize(
        ("meta", "source"),
        [
            (
                '<meta property="article:published_time" content="2026-10-20T10:00:00Z">',
                "meta:article:published_time",
            ),
            ('<meta property="og:published_time" content="2026-10-20">', "meta:og:published_time"),
            ('<meta itemprop="datePublished" content="2026-10-20">', "meta:datepublished"),
            ('<meta name="date" content="2026-10-20">', "meta:date"),
            ('<meta name="pubdate" content="2026-10-20">', "meta:pubdate"),
            ('<meta name="publish-date" content="2026-10-20">', "meta:publish-date"),
            ('<meta name="DC.date" content="2026-10-20">', "meta:dc.date"),
            (
                '<meta name="parsely-pub-date" content="2026-10-20T10:00:00.000Z">',
                "meta:parsely-pub-date",
            ),
        ],
    )
    def test_each_meta_tag_gives_the_published_date(self, meta: str, source: str) -> None:
        resolved = _resolve(_page(meta))

        assert resolved.date == "2026-10-20"
        assert resolved.date_source == source

    def test_article_modified_time_and_og_updated_time_are_modified_signals(self) -> None:
        head = (
            '<meta property="article:published_time" content="2026-09-22T09:00:00Z">'
            '<meta property="article:modified_time" content="2026-10-21T09:00:00Z">'
            '<meta property="og:updated_time" content="2026-10-21T09:00:00Z">'
        )

        resolved = _resolve(_page(head))

        assert resolved.date == "2026-09-22"
        assert resolved.date_modified == "2026-10-21"
        # A page updated a month after it was published disagrees with nothing.
        assert resolved.date_conflicts == []

    def test_og_updated_time_alone_dates_nothing(self) -> None:
        resolved = _resolve(_page('<meta property="og:updated_time" content="2026-10-21">'))

        assert resolved.date is None
        assert resolved.date_modified == "2026-10-21"

    def test_a_time_element_of_the_article_gives_the_date(self) -> None:
        body = '<h1>Trillium</h1><p class="byline">By A. Writer <time datetime="2026-10-19T07:00:00Z">Oct 19</time></p><p>The story.</p>'

        resolved = _resolve(_page(body=body))

        assert resolved.date == "2026-10-19"
        assert resolved.date_source == "time[datetime]"
        assert resolved.date_confidence == pytest.approx(0.5)

    def test_time_elements_of_a_sidebar_a_footer_and_related_stories_are_not_the_pages(
        self,
    ) -> None:
        html = (
            "<html><head><title>T</title></head><body>"
            '<aside><time datetime="2026-10-22">Oct 22</time></aside>'
            '<div class="related-stories"><time datetime="2026-10-21">Oct 21</time></div>'
            "<main><article><h1>Trillium</h1><p>The story.</p></article></main>"
            '<footer><time datetime="2026-10-20">Oct 20</time></footer></body></html>'
        )

        assert _resolve(html).date is None

    def test_a_date_printed_near_the_headline_is_a_low_confidence_date(self) -> None:
        body = (
            "<h1>Trillium</h1><p class='meta'>Published October 21, 2026</p><p>"
            + "Words. " * 60
            + "</p>"
        )

        resolved = _resolve(_page(body=body))

        assert resolved.date == "2026-10-21"
        assert resolved.date_source == "visible"
        assert resolved.date_confidence < LOW_CONFIDENCE

    def test_a_date_in_the_story_itself_is_not_the_pages(self) -> None:
        body = (
            "<h1>Trillium</h1><p>"
            + "Words about the chip. " * 20
            + "On March 3, 2026 the team met.</p>"
        )

        assert _resolve(_page(body=body)).date is None

    def test_an_updated_line_near_the_headline_is_the_modified_date(self) -> None:
        body = (
            "<h1>Trillium</h1><p class='meta'>Updated October 22, 2026</p><p>"
            + "Words. " * 60
            + "</p>"
        )

        resolved = _resolve(_page(body=body))

        assert resolved.date is None
        assert resolved.date_modified == "2026-10-22"

    def test_the_address_gives_a_low_confidence_date(self) -> None:
        resolved = _resolve(_page(), "https://example.org/2026/10/20/trillium")

        assert resolved.date == "2026-10-20"
        assert resolved.date_source == "url"
        assert resolved.date_confidence < LOW_CONFIDENCE

    def test_dotted_dates_are_read_as_day_month_year(self) -> None:
        body = (
            "<h1>Uutinen</h1><p class='meta'>Julkaistu 23.10.2026</p><p>" + "Sanoja. " * 60 + "</p>"
        )

        assert _resolve(_page(body=body)).date == "2026-10-23"

    @pytest.mark.parametrize("value", ["2026-11-30", "1970-01-01", "0001-01-01", "not a date", ""])
    def test_a_date_in_the_future_a_placeholder_or_none_is_no_date(self, value: str) -> None:
        head = f'<meta property="article:published_time" content="{value}">'

        assert _resolve(_page(head)).date is None


class TestAgreementAndConflicts:
    def test_agreeing_signals_raise_the_confidence(self) -> None:
        alone = _resolve(_page(_ld(datePublished="2026-10-21")))
        together = _resolve(
            _page(
                _ld(datePublished="2026-10-21")
                + '<meta property="article:published_time" content="2026-10-21T06:00:00Z">'
                + '<meta name="date" content="2026-10-21">'
            )
        )

        assert alone.date_confidence == pytest.approx(0.8)
        assert together.date == "2026-10-21"
        assert together.date_confidence > 0.95
        assert together.date_confidence < 1.0
        assert together.date_conflicts == []

    def test_signals_a_day_apart_agree_so_a_time_zone_is_no_conflict(self) -> None:
        head = (
            '<meta property="article:published_time" content="2026-10-21T23:30:00-08:00">'
            '<meta name="date" content="2026-10-22">'
        )

        resolved = _resolve(_page(head))

        assert resolved.date_conflicts == []

    def test_signals_more_than_a_day_apart_are_conflicts_with_their_source_and_value(self) -> None:
        # The "Claude Opus 5.5" page: 22 September in one place, 21 October in another.
        html = _page(
            _ld(datePublished="2026-09-22")
            + '<meta property="article:published_time" content="2026-10-21T09:00:00Z">'
        )

        resolved = _resolve(html)

        assert resolved.date == "2026-09-22"
        assert resolved.date_source == "json_ld:datePublished"
        assert resolved.date_conflicts == [
            {"source": "meta:article:published_time", "value": "2026-10-21T09:00:00+00:00"}
        ]
        # Two witnesses against each other: the date is said, and it is not trusted.
        assert resolved.date_confidence < LOW_CONFIDENCE + 0.05
        assert resolved.fields()["date_conflicts"] == resolved.date_conflicts

    def test_the_heavier_side_wins_and_the_lighter_one_is_the_conflict(self) -> None:
        html = _page(
            _ld(datePublished="2026-10-21")
            + '<meta property="article:published_time" content="2026-09-22T09:00:00Z">'
            + '<meta name="date" content="2026-09-22">'
        )

        resolved = _resolve(html)

        assert resolved.date == "2026-09-22"
        assert [item["source"] for item in resolved.date_conflicts] == ["json_ld:datePublished"]

    def test_of_equal_weight_the_earlier_date_wins(self) -> None:
        signals = [
            DateSignal("a", "2026-10-21", 0.6),
            DateSignal("b", "2026-09-22", 0.6),
        ]

        assert resolve(signals).date == "2026-09-22"

    def test_a_weak_signal_against_a_strong_one_lowers_the_confidence_a_little(self) -> None:
        agreed = _resolve(
            _page(_ld(datePublished="2026-10-21")), "https://example.org/2026/10/21/x"
        )
        against = _resolve(
            _page(_ld(datePublished="2026-10-21")), "https://example.org/2026/10/01/x"
        )

        assert agreed.date_confidence > 0.8
        assert 0.6 < against.date_confidence < 0.8
        assert against.date == "2026-10-21"
        assert against.date_conflicts == [{"source": "url", "value": "2026-10-01"}]

    def test_a_month_agrees_with_a_day_inside_it(self) -> None:
        signals = [
            DateSignal("json_ld:datePublished", "2026-10-21", 0.8),
            DateSignal("title", "2026-10", 0.2),
        ]

        resolved = resolve(signals)

        assert resolved.date == "2026-10-21" and resolved.date_conflicts == []


FIXTURES = Path(__file__).parent / "fixtures"


def _trillium_page() -> str:
    """The head of a real Google Cloud blog post, as recorded (tests/fixtures), with the 120 KB
    of inline scripts it has before its metadata put back: every date signal is past byte 120,000."""
    recorded = (FIXTURES / "cloud_google_trillium_head.html").read_text(encoding="utf-8")
    head, _, rest = recorded.partition("<title>")
    filler = "<script>" + "var cloud = '" + "x" * 120_000 + "';</script>"
    return f"{head}{filler}<title>{rest}</head><body><main><article><p>Trillium.</p></article></main></body></html>"


class TestARecordedGoogleCloudHead:
    """The page of the case: published_time in a meta tag, datePublished in JSON-LD, 140 KB in."""

    URL = "https://cloud.google.com/blog/products/compute/introducing-trillium-6th-gen-tpus"

    def test_the_recorded_signals_agree_and_date_the_page(self) -> None:
        resolved = resolve_html_dates(_trillium_page(), self.URL, today=TODAY)

        assert resolved.date == "2024-05-15"
        assert resolved.date_source == "json_ld:datePublished"
        assert {"json_ld:datePublished", "meta:published_time"} <= {
            signal.source for signal in resolved.signals
        }
        assert resolved.date_conflicts == []
        # JSON-LD (0.80) and the meta tag (0.65), both read: 1 - 0.2 * 0.35.
        assert resolved.date_confidence == pytest.approx(0.93, abs=0.005)

    def test_the_first_64_kb_hold_none_of_it_and_the_head_read_to_its_end_does(self) -> None:
        page = _trillium_page()

        assert resolve_html_dates(page[:65_536], self.URL, today=TODAY).date is None
        assert (
            resolve_html_dates(page[: page.index("</head>") + 7], self.URL, today=TODAY).date
            == "2024-05-15"
        )

    def test_the_zone_without_a_colon_is_read_on_every_python(self) -> None:
        assert normalize_date("2024-05-15T04:05:01+0300") == "2024-05-15T04:05:01+03:00"
        assert normalize_date("2026-10-21T08:30:00.12Z") == "2026-10-21T08:30:00.120000+00:00"
        assert normalize_date("2026-10-21T08:30+03") == "2026-10-21T08:30:00+03:00"
        assert normalize_date("2026-10-21 08:30:00") == "2026-10-21T08:30:00"

    async def test_a_search_dates_the_result_the_snippet_did_not(self) -> None:
        from test_web_search_resolve_dates import HeadFetcher, _search_service

        page = _trillium_page()
        fetcher = HeadFetcher({self.URL: page})
        service = _search_service(fetcher, (self.URL,))

        without = await service.search(
            "Trillium TPU news this week", depth="fast", resolve_dates=False
        )
        answer = await service.search("Trillium TPU announcement this week", depth="fast")

        assert without["results"][0]["date"] is None
        item = answer["results"][0]
        assert item["date"] == "2024-05-15" and item["date_source"] == "json_ld:datePublished"
        assert item["date_confidence"] == pytest.approx(0.93, abs=0.005)


class TestTheCloudGoogleCase:
    """A cloud.google.com page with article:published_time came back with date: null."""

    HEAD = (
        '<meta property="og:title" content="Introducing Trillium">'
        '<meta property="article:published_time" content="2024-05-15T16:00:00+00:00">'
        '<meta property="article:modified_time" content="2024-06-01T12:00:00+00:00">'
    )

    def test_the_head_alone_dates_the_page(self) -> None:
        # Only the first bytes of the page: a head and the start of a body, cut in a script.
        head = f"<html><head><title>Trillium</title>{self.HEAD}</head><body><script>var x = ["

        resolved = resolve_html_dates(head, URL, today=TODAY)

        assert resolved.date == "2024-05-15"
        assert resolved.date_source == "meta:article:published_time"
        assert resolved.date_modified == "2024-06-01"

    async def test_a_scrape_says_the_date_where_it_said_nothing(self) -> None:
        html = _page(self.HEAD + _ld(datePublished="2024-05-15"))
        service = _service(FakeFetcher({URL: _html_result(URL, html)}))

        page = await service.scrape(URL)

        assert page["date"] == "2024-05-15"
        assert page["date_source"] == "json_ld:datePublished"
        assert page["date_confidence"] >= 0.95
        assert page["date_conflicts"] == []
        assert page["date_modified"] == "2024-06-01"

    async def test_a_page_with_no_signal_says_so_and_does_not_invent_one(self) -> None:
        url = "https://example.org/page"
        service = _service(FakeFetcher({url: _html_result(url, _page())}))

        page = await service.scrape(url)

        assert page["date"] is None and page["date_source"] is None
        assert page["date_confidence"] == 0.0 and page["date_conflicts"] == []
        assert "date_modified" not in page


class TestExtract:
    async def test_extract_says_the_date_of_the_page(self) -> None:
        html = _page(
            _ld(datePublished="2026-10-21")
            + '<meta property="article:published_time" content="2026-10-21T06:00:00Z">'
        )
        service = _service(FakeFetcher({URL: _html_result(URL, html)}))

        answer = await service.extract(URL, {"headline": {}, "published_at": {}})

        assert answer["date"] == "2026-10-21"
        assert answer["date_confidence"] > 0.9
        assert answer["data"]["published_at"].startswith("2026-10-21")
        entry = answer["provenance"]["published_at"]
        assert entry["method"] == "json_ld"
        assert entry["date_confidence"] == answer["date_confidence"]
        assert entry["date_conflicts"] == []

    def test_a_published_date_found_by_name_is_held_to_the_signals(self) -> None:
        # JSON-LD, which is read first, says October; two other signals say September.
        html = _page(
            _ld(datePublished="2026-10-21")
            + '<meta property="article:published_time" content="2026-09-22T09:00:00Z">'
            + '<meta name="date" content="2026-09-22">'
        )
        resolution = resolve_html_dates(html, URL, today=TODAY)

        result = extract_structured(html, URL, {"published_at": {}}, dates=resolution)

        assert resolution.date == "2026-09-22"
        assert result.data["published_at"] == "2026-09-22T09:00:00+00:00"
        entry = result.provenance["published_at"]
        assert entry["method"] == "meta" and entry["selector"] == "meta:article:published_time"
        assert entry["date_conflicts"] == [
            {"source": "json_ld:datePublished", "value": "2026-10-21"}
        ]

    def test_without_the_signals_a_field_is_read_as_it_always_was(self) -> None:
        html = _page(_ld(datePublished="2026-10-21T05:00:00Z"))

        result = extract_structured(html, URL, {"published_at": {}})

        assert result.data["published_at"] == "2026-10-21T05:00:00+00:00"
        assert "date_confidence" not in result.provenance["published_at"]


class TestTheWordsOfAResult:
    def test_a_result_shows_weak_dates_which_disagree_or_do_not(self) -> None:
        signals = text_signals(
            "AI news",
            "Oct 9, 2026 · Something happened.",
            "https://a.example/2026/10/20/story",
            today=TODAY,
        )

        assert {(signal.source, signal.label) for signal in signals} == {
            ("url", "2026-10-20"),
            ("snippet", "2026-10-09"),
        }
        resolved = resolve(signals)
        assert resolved.date_confidence < LOW_CONFIDENCE
        assert len(resolved.date_conflicts) == 1

    def test_make_signal_refuses_what_is_no_date(self) -> None:
        assert make_signal("x", "yesterday", 0.5, today=TODAY) is None
        assert make_signal("x", True, 0.5, today=TODAY) is None
        assert make_signal("x", "2026-10-21", 0.5, today=TODAY) is not None
        assert make_signal("x", "2026-10", 0.5, today=TODAY) is not None


def test_extract_page_carries_the_resolution_of_the_page() -> None:
    page = extract_page(_page(_ld(datePublished="2026-10-21")), "https://example.org/story")

    assert page.dates is not None and page.dates.date == "2026-10-21"
