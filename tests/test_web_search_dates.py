"""A result says how old it is by a date when it shows one, and what kind of page it is."""

from __future__ import annotations

from datetime import date

import pytest

from web_scraper.web_tools.discovery import (
    DiscoveryCandidate,
    _wants_news,
    annotate_candidate,
    build_facets,
    classify_freshness,
    classify_page_kind,
    infer_publication_date,
    rank_candidates,
)
from web_scraper.web_tools.search import SearchResult

TODAY = date(2026, 10, 23)


@pytest.mark.parametrize(
    ("title", "snippet", "url", "expected"),
    [
        (
            "OpenAI fires three researchers",
            "",
            "https://www.reuters.com/technology/openai-fires-three-researchers-2026-10-20/",
            ("2026-10-20", "url"),
        ),
        (
            "Top weekly AI news",
            "",
            "https://aitalks.blog/2026/10/02/top-weekly-ai-news-october-2-2026",
            ("2026-10-02", "url"),
        ),
        ("Archives", "", "https://aidailypost.com/archives/2026/10", ("2026-10", "url")),
        (
            "A story",
            "Oct 9, 2026 · Something happened in Oulu.",
            "https://a.example/story",
            ("2026-10-09", "snippet"),
        ),
        (
            "A story",
            "3 days ago · The tower opened again.",
            "https://a.example/story",
            ("2026-10-20", "snippet"),
        ),
        (
            "A story",
            "Yesterday — The tower opened again.",
            "https://a.example/story",
            ("2026-10-22", "snippet"),
        ),
        (
            "AI news October 2026: what it means",
            "",
            "https://a.example/ai-news",
            ("2026-10", "title"),
        ),
        ("Next year", "", "https://a.example/2027/01/01/plans-for-next-year", (None, None)),
        ("No date", "Nothing here says when.", "https://a.example/page", (None, None)),
    ],
)
def test_a_results_date_is_read_from_its_url_snippet_or_title(
    title: str, snippet: str, url: str, expected: tuple[str | None, str | None]
) -> None:
    assert infer_publication_date(title, snippet, url, today=TODAY) == expected


@pytest.mark.parametrize(
    ("published", "year", "expected"),
    [
        ("2026-10-20", 2026, "this_week"),
        ("2026-10-02", 2026, "this_month"),
        ("2026-10", 2026, "this_month"),
        ("2026-03-01", 2026, "this_year"),
        ("2025-12-01", 2025, "older"),
        (None, 2026, "this_year"),
        (None, 2025, "older"),
        (None, None, "undated"),
    ],
)
def test_freshness_says_how_old_by_the_date_first(
    published: str | None, year: int | None, expected: str
) -> None:
    assert classify_freshness(published, year, today=TODAY) == expected


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://www.cnbc.com/", "front"),
        ("https://aiweekly.co/en/", "front"),
        ("https://aidailypost.com/archives/2026/10", "listing"),
        ("https://indianexpress.com/section/technology/artificial-intelligence", "listing"),
        ("https://aienews.org/news.html", "listing"),
        (
            "https://www.reuters.com/technology/openai-fires-three-researchers-2026-10-20/",
            "article",
        ),
        (
            "https://apnews.com/article/ftc-ai-investigation-anthropic-openai-89ac416717adbfb1d72f2d85e6ce83d1",
            "article",
        ),
        ("https://en.wikipedia.org/wiki/Eiffel_Tower", "article"),
        ("https://aitalks.blog/2026/10/02/top-weekly-ai-news-october-2-2026", "article"),
        ("https://www.reuters.com/technology/artificial-intelligence", "page"),
        ("https://docs.python.org/3/library/asyncio.html", "page"),
    ],
)
def test_a_results_kind_is_read_from_its_address(url: str, kind: str) -> None:
    assert classify_page_kind(url) == kind


def _candidate(title: str, url: str, snippet: str, rank: int) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        result=SearchResult(title=title, url=url, snippet=snippet), angle_ranks={"base": rank}
    )


def test_each_result_carries_its_date_freshness_and_kind() -> None:
    item = annotate_candidate(
        "openai researchers",
        _candidate(
            "OpenAI fires three researchers",
            "https://www.reuters.com/technology/openai-fires-three-researchers-2026-10-20/",
            "OpenAI fired three of its researchers.",
            0,
        ),
        today=TODAY,
    )

    assert item["date"] == "2026-10-20"
    assert item["date_source"] == "url"
    assert item["age_days"] == 3
    assert item["freshness"] == "this_week"
    assert item["kind"] == "article"
    # The year-based label stays as it was: this year or last is "current".
    assert item["year"] == 2026 and item["recency"] == "current"


def test_a_news_query_puts_articles_before_fronts_and_listings() -> None:
    # The same words on each, so only the kind of page tells them apart.
    title, snippet = "AI news this week", "AI news this week from the labs."
    candidates = [
        _candidate(title, "https://aiweekly.co/", snippet, 0),
        _candidate(title, "https://aidailypost.com/archives/2026/10", snippet, 1),
        _candidate(
            title, "https://londondaily.com/this-week-in-ai-meta-superintelligence-push", snippet, 2
        ),
    ]

    ranked = rank_candidates("AI news this week", candidates, 3, today=TODAY)

    assert [item["kind"] for item in ranked] == ["article", "listing", "front"]
    facets = build_facets(ranked, total_candidates=3, limit=3)
    assert facets["kinds"] == {"article": 1, "listing": 1, "front": 1}
    assert facets["freshness"] == {"undated": 2, "this_month": 1}


@pytest.mark.parametrize(
    ("query", "news"),
    [
        ("AI news this week", True),
        ("latest OpenAI announcements", True),
        ("Oulu weather today", True),
        ("python asyncio", False),
        ("Eiffel Tower history", False),
    ],
)
def test_only_a_query_that_asks_for_news_moves_articles_first(query: str, news: bool) -> None:
    assert _wants_news(query) is news


def test_a_fresh_result_goes_before_a_stale_article_when_the_query_asks_for_this_week() -> None:
    title, snippet = "AI news this week", "AI news this week from the labs."
    candidates = [
        _candidate(
            title,
            "https://www.cnn.com/2026/02/28/business/ai-week-anthropic-nvidia-block",
            snippet,
            0,
        ),
        _candidate(
            title, "https://aiagentstore.ai/ai-agent-news/this-week", "Yesterday — " + snippet, 1
        ),
    ]

    ranked = rank_candidates("AI news this week", candidates, 2, today=TODAY)

    assert [item["url"] for item in ranked] == [
        "https://aiagentstore.ai/ai-agent-news/this-week",
        "https://www.cnn.com/2026/02/28/business/ai-week-anthropic-nvidia-block",
    ]
    assert ranked[0]["freshness"] == "this_week" and ranked[1]["freshness"] == "this_year"


def test_filler_words_do_not_make_a_hub_an_article() -> None:
    assert classify_page_kind("https://headsupai.io/ai-news-and-updates") == "page"
    assert classify_page_kind("https://example.com/the-state-of-the-art") == "page"
