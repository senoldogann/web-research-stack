"""Tests for centralized runtime configuration."""

from web_scraper.config import Config, config
from web_scraper.research_agent import ResearchAgent


def test_config_clamps_research_runtime_knobs() -> None:
    settings = Config(
        scraper_max_redirects=99,
        api_max_request_bytes=64,
        research_normal_auto_min_sources=0,
        research_normal_auto_max_sources=99,
        research_deep_min_sources=60,
        research_deep_max_sources=80,
        research_default_normal_source_target=100,
        research_default_deep_source_target=1,
        research_search_pool_extra_normal=-4,
        research_search_pool_extra_deep=99,
        research_query_rewrite_max_variants=99,
        research_query_rewrite_timeout_seconds=-3.0,
        research_google_fallback_min_results=99,
        research_rerank_domain_diversity_boost=-1.0,
        research_rerank_same_domain_penalty=-1.0,
        research_rerank_exact_query_boost=-1.0,
        duckduckgo_request_delay_seconds=-1.0,
    )

    assert settings.scraper_max_redirects == 20
    assert settings.api_max_request_bytes == 128
    assert settings.research_normal_auto_min_sources == 1
    assert settings.research_normal_auto_max_sources == 50
    assert settings.research_deep_min_sources == 50
    assert settings.research_deep_max_sources == 50
    assert settings.research_default_normal_source_target == 50
    assert settings.research_default_deep_source_target == 50
    assert settings.research_search_pool_extra_normal == 0
    assert settings.research_search_pool_extra_deep == 50
    assert settings.research_query_rewrite_max_variants == 8
    assert settings.research_query_rewrite_timeout_seconds == 1.0
    assert settings.research_google_fallback_min_results == 50
    assert settings.research_rerank_domain_diversity_boost == 0.0
    assert settings.research_rerank_same_domain_penalty == 0.0
    assert settings.research_rerank_exact_query_boost == 0.0
    assert settings.duckduckgo_request_delay_seconds == 0.0


def test_research_agent_reads_runtime_defaults_from_config(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "default_research_model", "runtime-model", raising=False)
    monkeypatch.setattr(config, "ollama_host", "http://runtime-host:11434", raising=False)
    monkeypatch.setattr(config, "research_normal_auto_min_sources", 2, raising=False)
    monkeypatch.setattr(config, "research_normal_auto_max_sources", 7, raising=False)

    agent = ResearchAgent()
    target = ResearchAgent._resolve_target_source_count(
        requested_max_sources=99,
        ai_suggested_sources=None,
        deep_mode=False,
    )

    assert agent.model == "runtime-model"
    assert agent.host == "http://runtime-host:11434"
    assert target == 7


def test_config_preserves_trusted_hosts_from_env_inputs() -> None:
    settings = Config(
        api_trusted_hosts=["localhost", "backend", "example.com"],
    )

    assert settings.api_trusted_hosts == ["localhost", "backend", "example.com"]
