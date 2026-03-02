"""Tests for research planning and synthesis behavior."""

import asyncio

import pytest

from web_scraper.config import config
from web_scraper.research_agent import ResearchAgent, ResearchResult


def test_deep_mode_clamps_requested_sources_to_minimum() -> None:
    target = ResearchAgent._resolve_target_source_count(
        requested_max_sources=5,
        ai_suggested_sources=None,
        deep_mode=True,
    )

    assert target == 15


def test_deep_mode_clamps_requested_sources_to_maximum() -> None:
    target = ResearchAgent._resolve_target_source_count(
        requested_max_sources=70,
        ai_suggested_sources=None,
        deep_mode=True,
    )

    assert target == 50


def test_normal_mode_allows_single_source_when_ai_decides() -> None:
    # Normal mode: AI suggestion of 1 is respected (above minimum of 1)
    target = ResearchAgent._resolve_target_source_count(
        requested_max_sources=None,
        ai_suggested_sources=1,
        deep_mode=False,
    )
    assert target == 1


def test_normal_mode_caps_ai_decision_to_fifteen_sources() -> None:
    target = ResearchAgent._resolve_target_source_count(
        requested_max_sources=None,
        ai_suggested_sources=40,
        deep_mode=False,
    )

    assert target == 15


def test_normal_mode_caps_requested_sources_to_fifteen() -> None:
    target = ResearchAgent._resolve_target_source_count(
        requested_max_sources=40,
        ai_suggested_sources=None,
        deep_mode=False,
    )

    assert target == 15


def test_synthesis_prompt_contains_high_reliability_structure() -> None:
    prompt = ResearchAgent._build_synthesis_prompt(
        query="yapay zeka ajanlari",
        results=[
            ResearchResult(
                source="docs",
                url="https://example.com",
                title="Example",
                content="Detayli kaynak icerigi",
                relevance_score=0.9,
            )
        ],
        deep_mode=False,
    )

    assert "executive_summary" in prompt
    assert "confidence_level" in prompt
    assert "data_table" in prompt
    assert "conflicts_uncertainty" in prompt
    assert "Executive Summary" not in prompt  # no plain heading — only JSON key


def test_deep_mode_prompt_demands_long_output_and_large_source_range() -> None:
    prompt = ResearchAgent._build_source_selection_prompt(
        query="deep research",
        ddg_results=[
            {"title": "A", "url": "https://example.com", "snippet": "s", "source": "example"}
        ],
        max_to_check=25,
        deep_mode=True,
    )

    assert "15-50" in prompt
    assert "very detailed" in prompt


def test_query_rewrite_prompt_forbids_inventing_missing_facts() -> None:
    prompt = ResearchAgent._build_query_rewrite_prompt(
        "bana openai ajanlarini bul", deep_mode=False
    )

    assert "Do not invent" in prompt
    assert '"normalized_query"' in prompt
    assert '"search_queries"' in prompt


def test_normalize_search_queries_dedupes_and_keeps_original(monkeypatch) -> None:
    monkeypatch.setattr(config, "research_query_rewrite_max_variants", 3, raising=False)

    queries = ResearchAgent._normalize_search_queries(
        original_query="OpenAI ajanlari",
        normalized_query="OpenAI ajanlari son gelismeler",
        search_queries=[
            "OpenAI ajanlari son gelismeler",
            "OpenAI agents latest updates",
            "  OpenAI ajanlari  ",
            "extra query that should be trimmed by limit",
        ],
    )

    assert queries == [
        "OpenAI ajanlari son gelismeler",
        "OpenAI ajanlari",
        "OpenAI agents latest updates",
    ]


def test_merge_and_rank_search_results_dedupes_urls_and_rewards_domain_diversity() -> None:
    ranked = ResearchAgent._merge_and_rank_search_results(
        query="ai agents framework",
        result_sets=[
            [
                {
                    "title": "AI Agents Overview",
                    "url": "https://docs.example.com/agents",
                    "snippet": "Framework overview and orchestration details",
                    "source": "docs",
                    "search_provider": "duckduckgo",
                    "search_query": "ai agents framework",
                },
                {
                    "title": "AI Agents Tutorial",
                    "url": "https://blog.example.com/agents",
                    "snippet": "Tutorial on building agents",
                    "source": "blog",
                    "search_provider": "duckduckgo",
                    "search_query": "ai agents framework",
                },
            ],
            [
                {
                    "title": "AI Agents Overview Duplicate",
                    "url": "https://docs.example.com/agents#section",
                    "snippet": "Duplicate result from another provider",
                    "source": "docs",
                    "search_provider": "google",
                    "search_query": "ai agents framework",
                },
                {
                    "title": "Open Source Agent Benchmarks",
                    "url": "https://bench.example.net/ai-agents",
                    "snippet": "Benchmarks and evaluation results",
                    "source": "bench",
                    "search_provider": "google",
                    "search_query": "ai agents framework",
                },
            ],
        ],
        limit=3,
    )

    assert len(ranked) == 3
    assert ranked[0]["url"] == "https://docs.example.com/agents"
    assert len({item["url"] for item in ranked}) == 3
    assert len({item["source"] for item in ranked[:2]}) >= 2


def test_collect_search_results_falls_back_to_google_when_duckduckgo_is_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ResearchAgent(model="demo-model", host="http://ollama.local")
    monkeypatch.setattr(config, "research_enable_google_fallback", True, raising=False)
    monkeypatch.setattr(config, "research_google_fallback_min_results", 4, raising=False)

    async def fake_ddg(search_queries, search_pool_size, temporal_scope=None):
        return [
            {
                "title": "Sparse DDG result",
                "url": "https://one.example.com/ddg",
                "snippet": "Only one result",
                "source": "ddg",
                "search_provider": "duckduckgo",
                "search_query": search_queries[0],
            }
        ]

    async def fake_google(search_queries, search_pool_size):
        return [
            {
                "title": "Google result 1",
                "url": "https://two.example.com/result-1",
                "snippet": "Extra coverage from Google",
                "source": "google1",
                "search_provider": "google",
                "search_query": search_queries[0],
            },
            {
                "title": "Google result 2",
                "url": "https://three.example.com/result-2",
                "snippet": "More sources",
                "source": "google2",
                "search_provider": "google",
                "search_query": search_queries[0],
            },
            {
                "title": "Google result 3",
                "url": "https://four.example.com/result-3",
                "snippet": "More sources",
                "source": "google3",
                "search_provider": "google",
                "search_query": search_queries[0],
            },
        ]

    monkeypatch.setattr(agent, "_collect_duckduckgo_results", fake_ddg)
    monkeypatch.setattr(agent, "_collect_google_results", fake_google)

    collected = asyncio.run(
        agent._collect_search_results(
            query="ai agents framework",
            search_queries=["ai agents framework"],
            search_pool_size=4,
            target_count=4,
        )
    )

    assert collected["fallback_used"] is True
    assert set(collected["providers_used"]) == {"duckduckgo", "google"}
    assert len(collected["results"]) == 4


# ---------------------------------------------------------------------------
# FAZ 6 — Source tier classification
# ---------------------------------------------------------------------------


def test_classify_source_tier_gov_is_tier_one() -> None:
    assert ResearchAgent._classify_source_tier("https://nasa.gov/article") == 1


def test_classify_source_tier_mil_is_tier_one() -> None:
    assert ResearchAgent._classify_source_tier("https://defense.mil/news") == 1


def test_classify_source_tier_edu_tld_is_tier_two() -> None:
    assert ResearchAgent._classify_source_tier("https://someuniversity.edu/paper") == 2


def test_classify_source_tier_academic_domain_is_tier_two() -> None:
    assert ResearchAgent._classify_source_tier("https://arxiv.org/abs/1234") == 2


def test_classify_source_tier_major_media_is_tier_three() -> None:
    assert ResearchAgent._classify_source_tier("https://reuters.com/article") == 3


def test_classify_source_tier_wiki_is_tier_four() -> None:
    assert ResearchAgent._classify_source_tier("https://wikipedia.org/wiki/AI") == 4


def test_classify_source_tier_unknown_is_tier_five() -> None:
    assert ResearchAgent._classify_source_tier("https://myblog.io/post") == 5


# ---------------------------------------------------------------------------
# FAZ 6 — Low-quality result filtering
# ---------------------------------------------------------------------------


def test_filter_low_quality_results_drops_short_and_errored() -> None:
    results = [
        ResearchResult(source="a", url="u1", title="t", content="x" * 200),
        ResearchResult(source="b", url="u2", title="t", content="short"),
        ResearchResult(
            source="c", url="u3", title="t", content="ok content here" * 20, error="404"
        ),
        ResearchResult(source="d", url="u4", title="t", content=""),
    ]
    filtered = ResearchAgent._filter_low_quality_results(results, min_chars=100)
    assert len(filtered) == 1
    assert filtered[0].source == "a"


# ---------------------------------------------------------------------------
# FAZ 6 — Publication date extraction
# ---------------------------------------------------------------------------


def test_extract_publication_date_finds_iso_date() -> None:
    content = "Published on 2024-07-15 in our weekly newsletter."
    date = ResearchAgent._extract_publication_date(content)
    assert date == "2024-07-15"


def test_extract_publication_date_finds_json_metadata_date() -> None:
    content = '{"datePublished": "2024-03-22", "author": "Jane"}'
    date = ResearchAgent._extract_publication_date(content)
    assert date == "2024-03-22"


def test_extract_publication_date_returns_none_when_absent() -> None:
    date = ResearchAgent._extract_publication_date("No dates here at all.")
    assert date is None
