"""Tests for research planning and synthesis behavior."""

import asyncio
from datetime import datetime

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


def test_normal_mode_allows_single_source_when_ai_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "research_normal_auto_min_sources", 1, raising=False)
    monkeypatch.setattr(config, "research_normal_auto_max_sources", 15, raising=False)

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


@pytest.mark.asyncio
async def test_prepare_search_queries_generates_variants_in_standard_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ResearchAgent(model="demo-model", host="http://ollama.local")

    async def fake_call_llm(prompt, timeout, max_tokens=None):
        return (
            '{"query_ready": true,'
            '"normalized_query":"dünyada neler oluyor",'
            '"search_queries":['
            '"dünyada neler oluyor",'
            '"world news now",'
            '"international current events"'
            "],"
            '"rewrite_reason":"standard mode variants",'
            '"temporal_scope":{"type":"current","resolved_period":null,"reference":"bugün"}}'
        )

    monkeypatch.setattr(agent, "_call_llm", fake_call_llm)

    result = await agent._prepare_search_queries("Dünyada neler oluyor bugün?", deep_mode=False)
    current_year = str(datetime.now().year)

    assert len(result["search_queries"]) >= 3
    assert all(current_year in query for query in result["search_queries"])


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


def test_collect_search_results_skips_google_when_ddg_meets_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ResearchAgent(model="demo-model", host="http://ollama.local")
    monkeypatch.setattr(config, "research_enable_google_fallback", True, raising=False)
    monkeypatch.setattr(config, "research_google_fallback_min_results", 2, raising=False)

    async def fake_ddg(search_queries, search_pool_size, temporal_scope=None):
        return [
            {
                "title": "DDG result 1",
                "url": "https://one.example.com/ddg-1",
                "snippet": "Result one",
                "source": "ddg1",
                "search_provider": "duckduckgo",
                "search_query": search_queries[0],
            },
            {
                "title": "DDG result 2",
                "url": "https://two.example.com/ddg-2",
                "snippet": "Result two",
                "source": "ddg2",
                "search_provider": "duckduckgo",
                "search_query": search_queries[0],
            },
        ]

    async def fake_google(search_queries, search_pool_size):
        return [
            {
                "title": "Google result 1",
                "url": "https://three.example.com/google-1",
                "snippet": "Result three",
                "source": "google1",
                "search_provider": "google",
                "search_query": search_queries[0],
            }
        ]

    monkeypatch.setattr(agent, "_collect_duckduckgo_results", fake_ddg)
    monkeypatch.setattr(agent, "_collect_google_results", fake_google)

    collected = asyncio.run(
        agent._collect_search_results(
            query="ai agents framework",
            search_queries=["ai agents framework"],
            search_pool_size=3,
            target_count=3,
        )
    )

    assert collected["fallback_used"] is False
    assert set(collected["providers_used"]) == {"duckduckgo"}


def test_profile_aware_ranking_favors_academic_sources() -> None:
    ranked = ResearchAgent._merge_and_rank_search_results(
        query="retrieval augmented generation evaluation",
        result_sets=[
            [
                {
                    "title": "General blog post",
                    "url": "https://blog.example.com/rag-eval",
                    "snippet": "overview and tutorial",
                    "source": "blog",
                    "search_provider": "duckduckgo",
                    "search_query": "retrieval augmented generation evaluation",
                },
                {
                    "title": "arXiv paper on retrieval evaluation",
                    "url": "https://arxiv.org/abs/2501.12345",
                    "snippet": "benchmark and methodology",
                    "source": "arxiv",
                    "search_provider": "duckduckgo",
                    "search_query": "retrieval augmented generation evaluation",
                },
            ]
        ],
        limit=2,
        research_profile="academic",
    )

    assert ranked[0]["url"].startswith("https://arxiv.org/")


def test_collect_search_results_uses_profile_collector_in_deep_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ResearchAgent(model="demo-model", host="http://ollama.local")
    monkeypatch.setattr(config, "research_enable_google_fallback", False, raising=False)

    async def fake_ddg(search_queries, search_pool_size, temporal_scope=None):
        return []

    async def fake_arxiv(search_queries, search_pool_size, timeout_seconds):
        return [
            {
                "title": "A paper",
                "url": "https://arxiv.org/abs/2501.00001",
                "snippet": "paper summary",
                "source": "arxiv",
                "search_provider": "arxiv",
                "search_query": search_queries[0],
            }
        ]

    monkeypatch.setattr(agent, "_collect_duckduckgo_results", fake_ddg)
    monkeypatch.setattr("web_scraper.research.agent.collect_arxiv_results", fake_arxiv)

    collected = asyncio.run(
        agent._collect_search_results(
            query="rag evaluation",
            search_queries=["rag evaluation"],
            search_pool_size=5,
            target_count=3,
            research_profile="academic",
            deep_mode=True,
        )
    )

    assert collected["profile_provider_used"] is True
    assert "arxiv" in collected["providers_used"]
    assert len(collected["results"]) >= 1


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


# ---------------------------------------------------------------------------
# Citation verifier tests
# ---------------------------------------------------------------------------


def test_citation_verifier_supported_when_overlap_sufficient() -> None:
    from web_scraper.research.citation_verifier import verify_citations

    synthesis = "The Eiffel Tower is located in Paris [1]."
    sources = ["The Eiffel Tower is a wrought-iron lattice tower located in Paris, France."]
    results = verify_citations(synthesis, sources)
    assert len(results) == 1
    assert results[0]["citation_num"] == 1
    assert results[0]["supported"] is True


def test_citation_verifier_flags_out_of_range_citation() -> None:
    from web_scraper.research.citation_verifier import verify_citations

    synthesis = "Some fact [5]."
    sources = ["Only one source here."]
    results = verify_citations(synthesis, sources)
    assert any(r["reason"] == "out_of_range" for r in results)


def test_citation_audit_summary_returns_perfect_score_with_no_citations() -> None:
    from web_scraper.research.citation_verifier import citation_audit_summary

    audit = citation_audit_summary("No citations in this text at all.", [])
    assert audit["total_citations"] == 0
    assert audit["faithfulness_score"] == 1.0


def test_citation_audit_summary_has_faithfulness_between_0_and_1() -> None:
    from web_scraper.research.citation_verifier import citation_audit_summary

    synthesis = "Rust uses ownership model [1]. Python is interpreted [2]."
    sources = [
        "Rust programming language uses ownership and borrowing.",
        "Python is a scripting language.",
    ]
    audit = citation_audit_summary(synthesis, sources)
    assert 0.0 <= audit["faithfulness_score"] <= 1.0
    assert audit["total_citations"] >= 2


# ---------------------------------------------------------------------------
# Retry utils tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_retry_succeeds_on_first_attempt() -> None:
    from web_scraper.research.retry_utils import async_retry

    counter = {"calls": 0}

    async def succeed():
        counter["calls"] += 1
        return "ok"

    result = await async_retry(succeed, max_attempts=3, base_delay=0.01, label="test")
    assert result == "ok"
    assert counter["calls"] == 1


@pytest.mark.asyncio
async def test_async_retry_retries_on_failure_then_succeeds() -> None:
    from web_scraper.research.retry_utils import async_retry

    attempts = {"n": 0}

    async def fail_twice():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient")
        return "recovered"

    result = await async_retry(
        fail_twice,
        max_attempts=3,
        base_delay=0.01,
        retryable_exceptions=(ConnectionError,),
        label="test",
    )
    assert result == "recovered"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_async_retry_raises_after_max_attempts() -> None:
    from web_scraper.research.retry_utils import async_retry

    async def always_fail():
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        await async_retry(
            always_fail,
            max_attempts=2,
            base_delay=0.01,
            retryable_exceptions=(ValueError,),
            label="test",
        )


# ---------------------------------------------------------------------------
# Profile collectors — new adapters
# ---------------------------------------------------------------------------


def test_parse_rss_items_rss2_format() -> None:
    from web_scraper.research.profile_collectors import _parse_rss_items

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>Test Article</title>
          <link>https://example.com/article</link>
          <description>Test description</description>
          <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>"""
    items = _parse_rss_items(xml)
    assert len(items) == 1
    assert items[0]["title"] == "Test Article"
    assert items[0]["url"] == "https://example.com/article"


def test_parse_rss_items_returns_empty_on_invalid_xml() -> None:
    from web_scraper.research.profile_collectors import _parse_rss_items

    items = _parse_rss_items("not xml at all <><")
    assert items == []


# ---------------------------------------------------------------------------
# MetricsRegistry histogram tests
# ---------------------------------------------------------------------------


def test_metrics_registry_histogram_renders_buckets() -> None:
    from web_scraper.api_runtime import MetricsRegistry

    registry = MetricsRegistry()
    registry.observe_histogram("test_latency_seconds", 0.1)
    registry.observe_histogram("test_latency_seconds", 0.5)
    registry.observe_histogram("test_latency_seconds", 5.0)

    output = registry.render_prometheus()
    assert "# TYPE test_latency_seconds histogram" in output
    assert "test_latency_seconds_bucket" in output
    assert "test_latency_seconds_sum" in output
    assert "test_latency_seconds_count" in output
    # 3 observations total
    assert "_count{} 3" in output or "_count 3" in output


def test_metrics_registry_histogram_cumulative_buckets_are_monotonic() -> None:
    from web_scraper.api_runtime import MetricsRegistry

    registry = MetricsRegistry()
    for v in [0.01, 0.1, 1.0, 10.0]:
        registry.observe_histogram("req_dur", v)

    output = registry.render_prometheus()
    bucket_lines = [ln for ln in output.splitlines() if "req_dur_bucket" in ln]
    counts = [float(ln.split()[-1]) for ln in bucket_lines]
    # Bucket counts must be non-decreasing (cumulative histogram)
    assert all(counts[i] <= counts[i + 1] for i in range(len(counts) - 1))
