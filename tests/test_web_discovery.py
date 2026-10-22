"""Tests for multi-angle discovery and source-diversity ranking."""

from __future__ import annotations

import asyncio
from typing import Iterable

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.discovery import (
    DiscoveryCandidate,
    annotate_candidates,
    build_facets,
    build_search_angles,
    classify_recency,
    classify_source_type,
    infer_publication_year,
    rank_candidates,
    relevance_score,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings


class RoutingSearch:
    name = "fake"

    def __init__(
        self,
        results_by_query: dict[str, list[SearchResult]] | None = None,
        failures: Iterable[str] | None = None,
    ) -> None:
        self.results_by_query = results_by_query or {}
        self.failures = set(failures or ())
        self.calls: list[tuple[str, int]] = []
        self.date_filters: list[str | None] = []

    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: str | None = None,
    ) -> list[SearchResult]:
        self.calls.append((query, limit))
        self.date_filters.append(date_filter)
        if query in self.failures:
            raise WebToolError("search_failed", "refused", detail="bot_detected")
        return list(self.results_by_query.get(query, []))


def _service(search: RoutingSearch) -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(
            search_provider="fake",
            search_angle_delay_ms=0,
            search_jitter_min_ms=0,
            search_jitter_max_ms=0,
        ),
        fetcher=object(),  # type: ignore[arg-type]
        search_adapters={search.name: search},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


def _candidate(
    url: str,
    title: str,
    snippet: str = "",
    angles: dict[str, int] | None = None,
) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        result=SearchResult(title=title, url=url, snippet=snippet, source="fake"),
        angle_ranks=angles or {"base": 0},
    )


def test_fast_builds_one_angle() -> None:
    angles = build_search_angles("python asyncio", "fast", year=2026)

    assert [(angle.name, angle.query) for angle in angles] == [("base", "python asyncio")]


def test_discover_builds_base_current_and_background_angles() -> None:
    angles = build_search_angles("python asyncio", "discover", year=2026)

    assert [(angle.name, angle.query) for angle in angles] == [
        ("base", "python asyncio"),
        ("current", "python asyncio 2026"),
        ("background", "python asyncio history background"),
    ]


def test_discover_skips_current_angle_when_query_already_has_year() -> None:
    angles = build_search_angles("python 2026 changes", "discover", year=2026)

    assert [angle.name for angle in angles] == ["base", "background"]


def test_deep_adds_official_documentation_for_technical_query() -> None:
    angles = build_search_angles("python asyncio", "deep", year=2026)

    assert angles[-1].name == "primary"
    assert angles[-1].query == "python asyncio official documentation"


def test_deep_recognises_technical_topic_without_language_name() -> None:
    angles = build_search_angles("asyncio task cancellation", "deep", year=2026)

    assert angles[-1].query == "asyncio task cancellation official documentation"


def test_deep_adds_primary_sources_for_general_query() -> None:
    angles = build_search_angles("capital of france", "deep", year=2026)

    assert angles[-1].name == "primary"
    assert angles[-1].query == "capital of france primary sources"


def test_angle_suffixes_respect_max_length() -> None:
    angles = build_search_angles("x" * 500, "deep", max_length=500, year=2026)

    assert angles[0].query == "x" * 500
    assert len(angles) >= 3
    assert all(len(angle.query) <= 500 for angle in angles)


def test_unknown_depth_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_search_angles("python", "turbo")


def test_turkish_letters_fold_for_lexical_relevance() -> None:
    assert (
        relevance_score(
            "İstanbul için hava durumu",
            "Istanbul weather",
            "Hava durumu",
            "https://example.com/istanbul",
        )
        == 1.0
    )
    assert (
        relevance_score(
            "yapay zeka çalışmaları",
            "Yapay zeka calismalari",
            "",
            "https://example.com/ai",
        )
        == 1.0
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://docs.python.org/3/library/asyncio.html", "official"),
        ("https://arxiv.org/abs/2401.00001", "academic"),
        ("https://www.reuters.com/technology/ai", "news"),
        ("https://en.wikipedia.org/wiki/Asyncio", "reference"),
        ("https://stackoverflow.com/questions/1/asyncio", "community"),
        ("https://github.com/python/cpython", "code"),
        ("https://www.nasa.gov/mission", "official"),
        ("https://mit.edu/research", "academic"),
        ("https://docs.opensearch.org/latest/vector-search/", "official"),
        ("https://sqlite.org/fts5.html", "official"),
        ("https://python-httpx.org/async/", "official"),
        # A path that says "docs" is a documentation mirror, not the project's own site: mdn.org.cn and
        # runebook.dev copy another project's docs and must not wear the authority of its canonical host.
        ("https://example.com/docs/getting-started", "reference"),
        ("https://mdn.org.cn/en-US/docs/Web/API/AbortController", "reference"),
        ("https://runebook.dev/docs/postgresql/textsearch", "reference"),
        ("https://example.com/blog/post", "other"),
    ],
)
def test_source_type_classification(url: str, expected: str) -> None:
    assert classify_source_type(url) == expected


def test_publication_year_and_recency_are_inferred_conservatively() -> None:
    assert (
        infer_publication_year(
            "Release notes",
            "Python shipped this in 2024",
            "",
            year_now=2026,
        )
        == 2024
    )
    assert (
        infer_publication_year(
            "No date",
            "nothing",
            "https://example.com/2025/03/post",
            year_now=2026,
        )
        == 2025
    )
    assert (
        infer_publication_year(
            "No date",
            "nothing",
            "https://example.com/post",
            year_now=2026,
        )
        is None
    )
    assert classify_recency(2026, year_now=2026) == "current"
    assert classify_recency(2024, year_now=2026) == "recent"
    assert classify_recency(2020, year_now=2026) == "historical"
    assert classify_recency(None, year_now=2026) == "unknown"


def test_ranking_prefers_current_material_over_old_when_otherwise_equal() -> None:
    current = _candidate(
        "https://blog.example.com/current",
        "Python asyncio guide",
        "Updated in 2026",
    )
    old = _candidate(
        "https://blog.example.com/old",
        "Python asyncio guide",
        "Written in 2018",
    )

    results = rank_candidates("python asyncio", [old, current], 2, year_now=2026)

    assert [item["url"] for item in results] == [current.result.url, old.result.url]
    assert results[0]["recency"] == "current"
    assert results[1]["recency"] == "historical"


def test_ranking_keeps_authoritative_historical_sources() -> None:
    old_docs = _candidate(
        "https://docs.python.org/3/library/asyncio.html",
        "Python asyncio guide",
        "Archived in 2016",
    )
    new_blog = _candidate(
        "https://blog.example.com/asyncio",
        "Python asyncio guide",
        "2026 tutorial",
    )

    results = rank_candidates("python asyncio", [new_blog, old_docs], 2, year_now=2026)

    assert results[0]["url"] == old_docs.result.url
    assert results[0]["source_type"] == "official"
    assert results[0]["recency"] == "historical"


def test_ranking_caps_repeated_domains_when_alternatives_exist() -> None:
    candidates = [
        _candidate(
            f"https://example.com/{index}",
            f"Python asyncio guide {index}",
            "2026",
        )
        for index in range(5)
    ]
    candidates.extend(
        [
            _candidate(
                "https://docs.python.org/3/library/asyncio.html",
                "Python asyncio guide",
                "2026",
            ),
            _candidate(
                "https://en.wikipedia.org/wiki/Asyncio",
                "Python asyncio guide",
                "2026",
            ),
            _candidate(
                "https://arxiv.org/abs/2401.00001",
                "Python asyncio guide",
                "2026",
            ),
        ]
    )

    results = rank_candidates("python asyncio", candidates, 5, year_now=2026)

    assert len(results) == 5
    assert sum(item["domain"] == "example.com" for item in results) <= 2


def test_ranking_drops_results_without_any_query_overlap() -> None:
    relevant = _candidate(
        "https://docs.python.org/3/library/asyncio.html",
        "Python asyncio guide",
    )
    unrelated = _candidate(
        "https://food.example.com/pasta",
        "Cooking pasta",
        "Tomato and basil",
    )

    results = rank_candidates("python asyncio", [unrelated, relevant], 5, year_now=2026)

    assert [item["url"] for item in results] == [relevant.result.url]


def test_ranking_returns_nothing_when_no_result_matches_lexically() -> None:
    unrelated = _candidate(
        "https://food.example.com/pasta",
        "Cooking pasta",
        "Tomato and basil",
    )

    results = rank_candidates("python asyncio", [unrelated], 5, year_now=2026)

    assert results == []


def test_facets_summarise_source_type_recency_domains_and_angles() -> None:
    candidates = [
        _candidate(
            "https://docs.python.org/3/library/asyncio.html",
            "Python asyncio guide",
            "2026",
            {"base": 0, "current": 1},
        ),
        _candidate(
            "https://arxiv.org/abs/2401.00001",
            "Python asyncio paper",
            "2016",
            {"background": 0},
        ),
    ]
    results = rank_candidates("python asyncio", candidates, 5, year_now=2026)

    facets = build_facets(results, total_candidates=2, limit=5)

    assert facets["source_types"] == {"academic": 1, "official": 1}
    assert facets["recency"] == {"current": 1, "historical": 1}
    assert facets["angles"] == {"background": 1, "base": 1, "current": 1}
    assert facets["total_candidates"] == 2
    assert facets["domain_cap"] == 2


@pytest.mark.asyncio
async def test_discover_fans_out_merges_and_tags_results() -> None:
    angles = build_search_angles("asyncio guide", "discover")
    base_query, current_query, background_query = [angle.query for angle in angles]
    search = RoutingSearch(
        {
            base_query: [
                SearchResult(
                    "Asyncio guide",
                    "https://docs.python.org/3/library/asyncio.html",
                    "Official guide",
                    "docs.python.org",
                ),
                SearchResult(
                    "Asyncio tutorial",
                    "https://blog.example.com/asyncio",
                    "A tutorial",
                    "example",
                ),
            ],
            current_query: [
                SearchResult(
                    "Asyncio 2026 update",
                    "https://blog.example.com/asyncio",
                    "A longer tutorial snippet with 2026 updates",
                    "example",
                ),
                SearchResult(
                    "Asyncio news",
                    "https://news.example.org/asyncio-2026",
                    "2026 release",
                    "example",
                ),
            ],
            background_query: [
                SearchResult(
                    "Asyncio history",
                    "https://en.wikipedia.org/wiki/Asyncio",
                    "Background and history",
                    "wikipedia",
                ),
                SearchResult(
                    "Asyncio paper",
                    "https://arxiv.org/abs/2401.00001",
                    "Academic paper",
                    "arxiv",
                ),
            ],
        }
    )

    result = await _service(search).search("asyncio guide", limit=5)

    assert [query for query, _ in search.calls] == [
        base_query,
        current_query,
        background_query,
    ]
    assert result["depth"] == "discover"
    assert len(result["queries"]) == 3
    assert result["facets"]["total_candidates"] == 5
    assert result["facets"]["angles"] == {
        "background": 2,
        "base": 2,
        "current": 2,
    }
    by_url = {item["url"]: item for item in result["results"]}
    blog = by_url["https://blog.example.com/asyncio"]
    assert blog["angles"] == ["base", "current"]
    assert blog["snippet"] == "A longer tutorial snippet with 2026 updates"
    assert by_url["https://docs.python.org/3/library/asyncio.html"]["source_type"] == "official"
    assert by_url["https://arxiv.org/abs/2401.00001"]["source_type"] == "academic"


@pytest.mark.asyncio
async def test_discover_survives_a_failed_enrichment_angle() -> None:
    angles = build_search_angles("asyncio", "discover")
    base_query, current_query, background_query = [angle.query for angle in angles]
    search = RoutingSearch(
        {
            base_query: [
                SearchResult(
                    "Asyncio guide",
                    "https://docs.python.org/3/library/asyncio.html",
                    "Official guide",
                    "docs.python.org",
                )
            ],
            current_query: [
                SearchResult(
                    "Asyncio 2026",
                    "https://news.example.org/asyncio",
                    "2026 release",
                    "example",
                )
            ],
        },
        failures={background_query},
    )

    result = await _service(search).search("asyncio", limit=5)

    assert result["results"]
    assert result["warnings"] == ["background_query_failed"]
    failed = [item for item in result["queries"] if item["angle"] == "background"]
    assert failed[0]["error"]["detail"] == "bot_detected"


@pytest.mark.asyncio
async def test_discover_keeps_enrichment_results_when_base_query_fails() -> None:
    angles = build_search_angles("asyncio", "discover")
    base_query, current_query, _ = [angle.query for angle in angles]
    search = RoutingSearch(
        {
            current_query: [
                SearchResult(
                    "Asyncio 2026",
                    "https://news.example.org/asyncio",
                    "2026 release",
                    "example",
                )
            ]
        },
        failures={base_query},
    )

    result = await _service(search).search("asyncio", limit=5)

    assert [item["url"] for item in result["results"]] == ["https://news.example.org/asyncio"]
    assert result["warnings"] == ["base_query_failed"]


@pytest.mark.asyncio
async def test_discover_raises_when_every_angle_fails() -> None:
    angles = build_search_angles("asyncio", "discover")
    search = RoutingSearch(failures={angle.query for angle in angles})

    with pytest.raises(WebToolError) as exc:
        await _service(search).search("asyncio", limit=5)

    assert exc.value.code == "search_failed"
    assert exc.value.detail == "bot_detected"


@pytest.mark.asyncio
async def test_unknown_service_depth_is_rejected_without_provider_calls() -> None:
    search = RoutingSearch()

    with pytest.raises(WebToolError) as exc:
        await _service(search).search("asyncio", depth="turbo")

    assert exc.value.code == "invalid_request"
    assert search.calls == []


@pytest.mark.asyncio
async def test_fast_still_uses_one_query_and_keeps_provider_order() -> None:
    search = RoutingSearch(
        {
            "asyncio": [
                SearchResult("First", "https://example.com/first", "Asyncio", "example"),
                SearchResult("Second", "https://example.com/second", "Asyncio", "example"),
            ]
        }
    )

    result = await _service(search).search("asyncio", limit=5, depth="fast")

    assert search.calls == [("asyncio", 5)]
    assert [item["title"] for item in result["results"]] == ["First", "Second"]
    assert result["queries"][0]["angle"] == "base"
    assert result["results"][0]["score"] > 0


@pytest.mark.asyncio
async def test_deep_runs_four_angles() -> None:
    search = RoutingSearch(
        {
            "python asyncio": [
                SearchResult(
                    "Asyncio guide",
                    "https://docs.python.org/3/library/asyncio.html",
                    "Official",
                    "docs.python.org",
                )
            ]
        }
    )

    result = await _service(search).search("python asyncio", limit=5, depth="deep")

    assert len(search.calls) == 4
    assert search.calls[-1][0] == "python asyncio official documentation"
    assert result["depth"] == "deep"


def test_bm25f_weights_title_matches_above_snippet_matches() -> None:
    title_match = _candidate(
        "https://alpha.example.com/title",
        "Asyncio cancellation",
    )
    snippet_match = _candidate(
        "https://beta.example.com/snippet",
        "Guide",
        "Asyncio cancellation",
    )

    results = annotate_candidates(
        "asyncio cancellation",
        [snippet_match, title_match],
        year_now=2026,
    )

    by_url = {item["url"]: item for item in results}
    assert by_url[title_match.result.url]["bm25"] > by_url[snippet_match.result.url]["bm25"]
    assert (
        by_url[title_match.result.url]["relevance"] == by_url[snippet_match.result.url]["relevance"]
    )


def test_bm25f_saturates_repeated_terms() -> None:
    single = _candidate(
        "https://alpha.example.com/single",
        "Asyncio guide",
    )
    repeated = _candidate(
        "https://beta.example.com/repeated",
        "Asyncio asyncio asyncio asyncio asyncio guide",
    )

    results = annotate_candidates("asyncio guide", [single, repeated], year_now=2026)

    by_url = {item["url"]: item for item in results}
    assert by_url[single.result.url]["bm25"] > 0
    assert by_url[repeated.result.url]["bm25"] < 2 * by_url[single.result.url]["bm25"]


def test_weighted_rrf_prefers_current_angle_over_background() -> None:
    background = _candidate(
        "https://a.example.com/background",
        "Asyncio event loop",
        "Asyncio guide",
        {"background": 0},
    )
    current = _candidate(
        "https://z.example.com/current",
        "Asyncio event loop",
        "Asyncio guide",
        {"current": 0},
    )

    results = rank_candidates("asyncio", [background, current], 2, year_now=2026)

    assert [item["url"] for item in results] == [current.result.url, background.result.url]


def test_mmr_prefers_a_distinct_result_over_a_near_duplicate() -> None:
    snippet = "Complete asyncio guide with examples"
    first = _candidate(
        "https://alpha.example.com/guide",
        "Asyncio guide",
        snippet,
        {"base": 0},
    )
    duplicate = _candidate(
        "https://mirror.example.net/guide",
        "Asyncio guide",
        snippet,
        {"base": 1},
    )
    distinct = _candidate(
        "https://beta.example.org/guide",
        "Asyncio guide",
        "Threads tasks cancellation event loop debugging testing",
        {"base": 2},
    )

    results = rank_candidates(
        "asyncio guide",
        [first, duplicate, distinct],
        2,
        year_now=2026,
    )

    assert [item["url"] for item in results] == [first.result.url, distinct.result.url]


def test_freshness_intent_adds_provider_date_filters() -> None:
    discover = build_search_angles("latest python release", "discover", year=2026)
    turkish = build_search_angles("güncel yapay zeka haberleri", "fast", year=2026)
    general = build_search_angles("python asyncio", "discover", year=2026)

    assert [(angle.name, angle.date_filter) for angle in discover] == [
        ("base", "year"),
        ("current", "year"),
        ("background", None),
    ]
    assert turkish[0].date_filter == "year"
    assert general[0].date_filter is None
    assert general[1].date_filter == "year"
    assert general[2].date_filter is None


@pytest.mark.asyncio
async def test_freshness_query_forwards_date_filter_to_provider() -> None:
    search = RoutingSearch(
        {
            "latest asyncio release": [
                SearchResult(
                    "Asyncio release notes",
                    "https://docs.python.org/3/library/asyncio.html",
                    "Latest changes",
                    "docs.python.org",
                )
            ]
        }
    )

    result = await _service(search).search("latest asyncio release", limit=5, depth="fast")

    assert search.date_filters == ["year"]
    assert result["queries"][0]["date_filter"] == "year"


@pytest.mark.asyncio
async def test_search_timeout_is_reported_as_a_provider_failure() -> None:
    class HangingSearch:
        name = "fake"

        async def search(
            self,
            query: str,
            limit: int,
            *,
            date_filter: str | None = None,
        ) -> list[SearchResult]:
            await asyncio.sleep(0.05)
            return []

    service = _service(HangingSearch())  # type: ignore[arg-type]
    service.settings.search_timeout_seconds = 0.01

    with pytest.raises(WebToolError) as exc:
        await service.search("asyncio", limit=5, depth="fast")

    assert exc.value.code == "timeout"
    assert exc.value.detail == "search_timeout"
