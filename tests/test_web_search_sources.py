"""Reliable sources first: wider news outlets, social and press_release types, blogs as community."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from web_scraper.web_tools.discovery import (
    DiscoveryCandidate,
    _wants_news,
    annotate_candidates,
    build_facets,
    classify_source_type,
    rank_candidates,
)
from web_scraper.web_tools.search import SearchResult

TODAY = date(2026, 10, 23)
BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "url",
    [
        # wire services
        "https://www.reuters.com/world/europe/story-2026-10-22/",
        "https://apnews.com/article/ai-regulation-1234",
        "https://www.upi.com/Top_News/World-News/2026/10/22/story/1234/",
        "https://www.stt.fi/uutiset/story",
        "https://www.aa.com.tr/en/world/story/3000000",
        "https://www.kyodonews.net/news/2026/10/abc.html",
        # national and public broadcasters
        "https://www.bbc.com/news/articles/c1234",
        "https://www.bbc.com/turkce/articles/c1234",
        "https://www.bbc.co.uk/news/uk-1234",
        "https://yle.fi/a/74-20150000",
        "https://svenska.yle.fi/a/7-10000",
        "https://www3.nhk.or.jp/nhkworld/en/news/20261022_01/",
        "https://www.dw.com/en/ai-act/a-70000000",
        "https://www.france24.com/en/europe/20261022-story",
        "https://www.trthaber.com/haber/dunya/story-1234.html",
        "https://www.trtworld.com/article/1234",
        "https://www.cbc.ca/news/world/story-1.7000000",
        "https://www.abc.net.au/news/2026-10-22/story/1234",
        "https://www.svt.se/nyheter/inrikes/story",
        "https://www.nrk.no/norge/story-1.17000000",
        "https://www.dr.dk/nyheder/udland/story",
        # papers of record and major outlets: US, UK, EU, Nordics, Turkey, others
        "https://www.nytimes.com/2026/10/22/technology/ai.html",
        "https://www.washingtonpost.com/technology/2026/10/22/ai/",
        "https://www.wsj.com/tech/ai/story-1234",
        "https://www.latimes.com/business/story/2026-10-22/ai",
        "https://www.politico.com/news/2026/10/22/ai-00000",
        "https://www.theguardian.com/technology/2026/oct/22/ai",
        "https://www.telegraph.co.uk/business/2026/10/22/ai/",
        "https://www.thetimes.co.uk/article/ai-regulation-abc",
        "https://www.independent.co.uk/tech/ai-1234.html",
        "https://www.lemonde.fr/en/economy/article/2026/10/22/ai_1.html",
        "https://www.spiegel.de/international/europe/ai-a-1234.html",
        "https://www.elpais.com/tecnologia/2026-10-22/ai.html",
        "https://www.corriere.it/tecnologia/26_ottobre_22/ai.shtml",
        "https://www.euronews.com/next/2026/10/22/ai",
        "https://www.hs.fi/talous/art-2000011111111.html",
        "https://www.kauppalehti.fi/uutiset/ai/abc",
        "https://www.kaleva.fi/uutiset/ai/abc",
        "https://www.dn.se/ekonomi/ai/",
        "https://www.hurriyet.com.tr/ekonomi/ai-43000000",
        "https://www.sabah.com.tr/teknoloji/2026/10/22/ai",
        "https://www.cumhuriyet.com.tr/haber/ai-2000000",
        "https://www.haberturk.com/ai-3000000",
        "https://www.asahi.com/ajw/articles/12345678",
        "https://www.scmp.com/tech/article/3000000/ai",
        "https://www.straitstimes.com/world/ai-regulation",
        "https://www.thehindu.com/sci-tech/ai/article70000000.ece",
        "https://www.haaretz.com/israel-news/2026-10-22/ty-article/0000",
        "https://www.democracynow.org/2026/10/22/headlines",
        # the business and tech press
        "https://www.ft.com/content/abc-123",
        "https://www.cnbc.com/2026/10/22/ai.html",
        "https://www.forbes.com/sites/someone/2026/10/22/ai/",
        "https://techcrunch.com/2026/10/22/ai-regulation/",
        "https://www.theverge.com/2026/10/22/ai-regulation",
        "https://arstechnica.com/ai/2026/10/ai-regulation/",
        "https://www.wired.com/story/ai-regulation/",
    ],
)
def test_established_outlets_are_news(url: str) -> None:
    assert classify_source_type(url) == "news"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/watch/?v=1234567890",
        "https://m.facebook.com/story.php?id=1",
        "https://fb.watch/abc123/",
        "https://www.instagram.com/p/Cabc123/",
        "https://www.tiktok.com/@someone/video/123",
        "https://x.com/someone/status/123",
        "https://twitter.com/someone/status/123",
        "https://www.threads.net/@someone/post/abc",
    ],
)
def test_posts_and_videos_are_social(url: str) -> None:
    assert classify_source_type(url) == "social"


def test_youtube_is_social_only_for_a_news_query() -> None:
    video = "https://www.youtube.com/watch?v=abc123"

    assert classify_source_type(video, news=True) == "social"
    assert classify_source_type("https://youtu.be/abc123", news=True) == "social"
    assert classify_source_type(video) == "other"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.prnewswire.com/news-releases/acme-launches-301000000.html",
        "https://www.globenewswire.com/news-release/2026/10/22/1/0/en/acme.html",
        "https://www.businesswire.com/news/home/20261022000001/en/Acme",
        "https://www.accessnewswire.com/newsroom/en/acme",
        "https://www.einpresswire.com/article/1/acme",
        "https://www.prweb.com/releases/acme-1.htm",
        "https://www.mynewsdesk.com/fi/acme/pressreleases/1",
        "https://www.sttinfo.fi/tiedote/acme",
    ],
)
def test_press_release_wires_are_press_releases(url: str) -> None:
    assert classify_source_type(url) == "press_release"


@pytest.mark.parametrize(
    "url",
    [
        "https://someone.blogspot.com/2026/10/ai.html",
        "https://someone.wordpress.com/2026/10/22/ai/",
        "https://someone.substack.com/p/ai-regulation",
        "https://substack.com/@someone/note/c-1",
    ],
)
def test_blog_hosts_are_community(url: str) -> None:
    assert classify_source_type(url) == "community"


@pytest.mark.parametrize(
    "url",
    [
        "https://newsnow-ai-daily.com/ai-regulation",
        "https://example.com/blog/post",
        "https://notbbc.com/news",
        "https://bbc.com.evil.example/news",
    ],
)
def test_a_site_is_not_an_outlet_by_its_name(url: str) -> None:
    assert classify_source_type(url) == "other"


def _candidate(title: str, url: str, snippet: str, rank: int) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        result=SearchResult(title=title, url=url, snippet=snippet, source=""),
        angle_ranks={"base": rank},
    )


QUERY = "EU AI regulation delay news this week"
HEADLINE = "EU lawmakers agree AI regulation delay as tech lobby pushes back"
SNIPPET = "Brussels confirms the AI regulation delay on Tuesday this week."
# The live evaluation's case, all of one freshness: the provider put first a fresh aggregator
# copy of an outlet's headline, a Facebook video and unknown blogs; the outlets came after.
EVALUATION = [
    _candidate(
        HEADLINE,
        "https://dailyaibrief.com/2026/10/22/eu-lawmakers-agree-ai-regulation-delay",
        SNIPPET,
        0,
    ),
    _candidate(
        "EU AI regulation delay news this week | Facebook",
        "https://www.facebook.com/watch/?v=1234567890",
        "Yesterday — EU AI regulation delay news this week.",
        1,
    ),
    _candidate(
        "EU AI regulation delay: my take",
        "https://mythoughtsonai.net/2026/10/22/eu-ai-regulation-delay",
        "Yesterday — my take on the EU AI regulation delay this week.",
        2,
    ),
    _candidate(
        "Acme welcomes the EU AI regulation delay - PR Newswire",
        "https://www.prnewswire.com/news-releases/acme-welcomes-eu-ai-regulation-delay-3010.html",
        "Yesterday — Acme welcomes the EU AI regulation delay this week.",
        3,
    ),
    _candidate(
        HEADLINE,
        "https://www.wsj.com/tech/ai/eu-lawmakers-agree-ai-regulation-delay-2026-10-22",
        SNIPPET,
        4,
    ),
    _candidate(
        "Exclusive: EU set for AI regulation delay after industry pressure",
        "https://www.reuters.com/technology/eu-ai-regulation-delay-2026-10-22/",
        "The EU AI regulation delay was agreed on Tuesday this week.",
        5,
    ),
]


def test_the_outlets_rank_first_for_a_news_query() -> None:
    ranked = rank_candidates(QUERY, EVALUATION, 6, today=TODAY)

    assert {item["domain"] for item in ranked[:2]} == {"wsj.com", "reuters.com"}
    by_domain = {item["domain"]: item for item in ranked}
    assert by_domain["wsj.com"]["source_type"] == "news"
    assert by_domain["facebook.com"]["source_type"] == "social"
    assert by_domain["prnewswire.com"]["source_type"] == "press_release"
    assert by_domain["dailyaibrief.com"]["source_type"] == "other"
    # All of the same freshness, so it is the kind of source that decides.
    assert {item["freshness"] for item in ranked} == {"this_week"}


def _same_but_for_the_host(query: str) -> list[DiscoveryCandidate]:
    words = "Asyncio event loops explained, with examples of coroutines and tasks."
    return [
        _candidate(
            "Python asyncio tutorial", f"https://www.{host}/python-asyncio-tutorial", words, 0
        )
        for host in ("facebook.com", "prnewswire.com", "mycompany.com")
    ]


def test_the_penalties_are_for_news_queries_only() -> None:
    scores = {
        item["source_type"]: item["score"]
        for item in annotate_candidates(
            "python asyncio tutorial", _same_but_for_the_host(""), today=TODAY
        )
    }

    assert scores["social"] == scores["press_release"] == scores["other"]


def test_a_news_query_penalises_them() -> None:
    scores = {
        item["source_type"]: item["score"]
        for item in annotate_candidates(
            "python asyncio tutorial news", _same_but_for_the_host(""), today=TODAY
        )
    }

    assert scores["social"] < scores["press_release"] < scores["other"]
    assert scores["other"] - scores["social"] == pytest.approx(0.30)
    assert scores["other"] - scores["press_release"] == pytest.approx(0.24)


def test_a_fresh_page_still_beats_a_stale_outlet_when_the_query_says_how_fresh() -> None:
    stale = _candidate(
        QUERY, "https://www.cnn.com/2026/02/28/business/ai-week", f"{QUERY} said in February.", 0
    )
    fresh = _candidate(
        QUERY, "https://aiagentstore.ai/ai-agent-news/this-week", f"Yesterday — {QUERY}.", 1
    )

    ranked = rank_candidates(QUERY, [stale, fresh], 2, today=TODAY)

    assert ranked[0]["url"] == fresh.result.url


def test_facets_count_the_new_types() -> None:
    ranked = rank_candidates(QUERY, EVALUATION, 6, today=TODAY)

    facets = build_facets(ranked, total_candidates=6, limit=6)

    assert facets["source_types"]["social"] == 1
    assert facets["source_types"]["press_release"] == 1
    assert facets["source_types"]["news"] == 2


@pytest.mark.parametrize(
    ("query", "news"),
    [
        ("Oulu uutiset", True),
        ("bugün haberler", True),
        ("Stockholm nyheter", True),
        ("aktuelle Nachrichten", True),
        ("python asyncio", False),
        ("Eiffel Tower history", False),
    ],
)
def test_news_words_of_other_languages_make_a_news_query(query: str, news: bool) -> None:
    assert _wants_news(query) is news


def test_real_queries_keep_their_results_and_their_order() -> None:
    # The ten recorded queries of benchmarks/web_search_relevance.json, ranked with BM25 squashed
    # into (0, 1) (tests/fixtures/ranking_of_recorded_queries.json): none of their results is an
    # outlet, a post or a press release, so the source types of round 3 do not touch them. Up to
    # the merge of the integration branch the order pinned here was the one with BM25 scaled to
    # the pool's best match; the squash moved eight of the ten, an official page above blog posts
    # that spell the whole query (react.dev's useEffect went from fifth to first).
    recorded = json.loads((BENCHMARKS / "web_search_relevance.json").read_text(encoding="utf-8"))
    golden = json.loads((FIXTURES / "ranking_of_recorded_queries.json").read_text(encoding="utf-8"))
    assert len(recorded["results"]) == len(golden) == 10
    for row in recorded["results"]:
        candidates = [
            _candidate(item["title"], item["url"], "", rank) for rank, item in enumerate(row["top"])
        ]

        ranked = rank_candidates(row["query"], candidates, 8, today=date(2026, 10, 22))

        assert [item["url"] for item in ranked] == golden[row["query"]], row["query"]
