"""Fields named in plain words are found without a selector, and dates come back in ISO 8601."""

from __future__ import annotations

import pytest

from web_scraper.web_tools.content import extract_structured
from web_scraper.web_tools.fields import normalize_date

URL = "https://news.example.com/2026/10/23/big-news"
JSON_LD = """<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Big news happens",
 "datePublished": "2026-10-23T05:00:00Z", "dateModified": "2026-10-23T07:30:00Z",
 "author": [{"@type": "Person", "name": "Jane Roe"}, {"@type": "Person", "name": "John Doe"}],
 "publisher": {"@type": "Organization", "name": "The Daily"},
 "articleSection": "World", "keywords": ["news", "world"], "inLanguage": "en-GB"}
</script>"""
META = """<meta property="og:site_name" content="The Daily">
<meta property="article:published_time" content="2026-10-23T08:00:00+03:00">
<meta name="author" content="Jane Roe">
<meta property="og:title" content="Big news happens - The Daily">"""
BODY = """<body><header><nav><a href="/">Home</a></nav></header><main><article>
<h1>Big news happens</h1>
<div class="toc"><p>In this article</p></div>
<p class="dateline"><span>Oct 23, 2026</span><span>8:00 AM</span></p>
<p>Short.</p>
<p>The first real paragraph of the story says what happened, where it happened and why it matters.</p>
<p>The second paragraph adds the details.</p>
</article></main><footer><p>Copyright The Daily, every right reserved, all of them, always.</p></footer></body>"""


def _page(head: str, body: str = BODY) -> str:
    return f'<html lang="en"><head><title>Big news - The Daily</title>{head}</head>{body}</html>'


def test_plain_names_read_the_pages_structured_data() -> None:
    schema = {
        "headline": {},
        "author": {},
        "published_at": {},
        "dateModified": {},
        "publisher": {},
        "section": {},
        "tags": {"all": True},
        "language": {},
    }

    result = extract_structured(_page(JSON_LD + META), URL, schema)

    assert result.data == {
        "headline": "Big news happens",
        "author": "Jane Roe, John Doe",
        "published_at": "2026-10-23T05:00:00+00:00",
        "dateModified": "2026-10-23T07:30:00+00:00",
        "publisher": "The Daily",
        "section": "World",
        "tags": ["news", "world"],
        "language": "en-GB",
    }
    assert result.provenance["published_at"]["method"] == "json_ld"
    assert result.provenance["published_at"]["selector"] == "datePublished"
    assert result.provenance["published_at"]["evidence"] == "2026-10-23T05:00:00Z"
    assert result.errors == []


def test_meta_tags_answer_when_there_is_no_json_ld() -> None:
    result = extract_structured(
        _page(META), URL, {"published": {}, "byline": {}, "site_name": {}, "title": {}}
    )

    assert result.data["published"] == "2026-10-23T08:00:00+03:00"
    assert result.provenance["published"]["method"] == "meta"
    assert result.provenance["published"]["selector"] == "meta[property=article:published_time]"
    assert result.data["byline"] == "Jane Roe"
    assert result.data["site_name"] == "The Daily"
    assert result.data["title"] == "Big news happens - The Daily"


def test_the_html_conventions_answer_last() -> None:
    body = """<body><main><article><h1>Plain page</h1>
    <p>By <a rel="author" href="/people/jane">Jane Roe</a>,
    <time datetime="2026-10-23T08:00:00Z">yesterday</time></p>
    <p>The opening paragraph of this plain page is long enough to be the lead of the story.</p>
    </article></main></body>"""

    result = extract_structured(
        _page("", body), URL, {"headline": {}, "author": {}, "published_at": {}, "lead": {}}
    )

    assert result.data == {
        "headline": "Plain page",
        "author": "Jane Roe",
        "published_at": "2026-10-23T08:00:00+00:00",
        "lead": "The opening paragraph of this plain page is long enough to be the lead of the story.",
    }
    assert {entry["method"] for entry in result.provenance.values()} == {"html"}
    assert result.provenance["published_at"]["selector"] == "time[datetime]"


def test_the_lead_skips_labels_and_short_lines() -> None:
    result = extract_structured(_page(""), URL, {"first_paragraph": {}})

    assert result.data["first_paragraph"] == (
        "The first real paragraph of the story says what happened, where it happened and why it matters."
    )


def test_a_selected_date_reads_with_spaces_between_its_parts_and_in_iso() -> None:
    result = extract_structured(
        _page(""), URL, {"date": {"selector": ".dateline", "type": "date"}, "raw": ".dateline"}
    )

    assert result.data["raw"] == "Oct 23, 2026 8:00 AM"
    assert result.data["date"] == "2026-10-23T08:00:00"
    assert result.provenance["date"]["evidence"] == "Oct 23, 2026 8:00 AM"


def test_a_selector_that_finds_nothing_falls_back_for_a_known_field() -> None:
    result = extract_structured(_page(JSON_LD), URL, {"author": {"selector": ".no-such-byline"}})

    assert result.data["author"] == "Jane Roe, John Doe"
    assert result.provenance["author"]["method"] == "json_ld"


def test_an_unknown_name_is_reported_missing_not_invented() -> None:
    result = extract_structured(
        _page(JSON_LD + META), URL, {"favourite_colour": {"required": True}}
    )

    assert result.data["favourite_colour"] is None
    assert result.provenance["favourite_colour"]["method"] == "not_found"
    assert result.errors[0]["code"] == "required_field_missing"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-23T05:00:00Z", "2026-10-23T05:00:00+00:00"),
        ("2026-10-23", "2026-10-23"),
        ("Oct 23, 2026 8:00 AM", "2026-10-23T08:00:00"),
        ("October 23, 2026", "2026-10-23"),
        ("23 October 2026", "2026-10-23"),
        ("Updated: 23 Oct 2026, 14:05", "2026-10-23T14:05:00"),
        ("Fri, 23 Oct 2026 08:00:00 GMT", "2026-10-23T08:00:00+00:00"),
        ("2026/10/23", "2026-10-23"),
        ("yesterday", None),
        ("", None),
    ],
)
def test_dates_are_read_in_the_forms_pages_write_them(text: str, expected: str | None) -> None:
    assert normalize_date(text) == expected
