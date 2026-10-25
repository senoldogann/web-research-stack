"""Tests for the labelled web-search relevance evaluation."""

from __future__ import annotations

import json
from pathlib import Path

from web_scraper.web_tools.relevance import (
    domain_matches,
    ndcg_at_k,
    score_results,
    summarize,
)

QUERIES = Path(__file__).resolve().parents[1] / "benchmarks" / "search_relevance_queries.json"


def _result(
    url: str, *, source_type: str = "other", year: int | None = None, recency: str = "unknown"
):
    return {
        "url": url,
        "domain": url.split("/")[2],
        "source_type": source_type,
        "year": year,
        "recency": recency,
    }


def test_domain_matches_accepts_the_domain_and_its_subdomains() -> None:
    assert domain_matches("https://python.org/", {"python.org"})
    assert domain_matches("https://docs.python.org/3/", {"python.org"})
    assert not domain_matches("https://notpython.org/", {"python.org"})


def test_ndcg_at_k_rewards_a_relevant_result_at_the_top() -> None:
    assert ndcg_at_k([1, 0, 0], 5) == 1.0
    assert ndcg_at_k([0, 1, 0], 5) < 1.0
    assert ndcg_at_k([0, 0, 0], 5) == 0.0


def test_score_results_reports_relevance_official_and_freshness() -> None:
    row = {
        "query": "latest python release",
        "expected_domains": ["python.org"],
        "freshness": True,
    }
    results = [
        _result("https://realpython.com/python-release/"),
        _result(
            "https://www.python.org/downloads/",
            source_type="official",
            year=2026,
            recency="current",
        ),
        _result("https://docs.python.org/3/whatsnew/", source_type="official"),
    ]

    score = score_results(row, results, k=5, current_year=2026)

    assert score["ndcg_at_5"] == 0.6934
    assert score["mrr"] == 0.5
    assert score["relevant_at_5"] == 2
    assert score["official_hit_at_3"] is True
    assert score["freshness_hit_at_5"] is True
    assert score["unique_domains_at_5"] == 3


def test_a_query_about_now_is_scored_by_the_day_and_not_by_the_year() -> None:
    # A story of three weeks ago is "current" in October; a query with fresh_days wants one of the last days
    # among the first three.
    row = {"query": "syysmyrsky", "expected_domains": ["yle.fi"], "fresh_days": 7}
    old = {"url": "https://yle.fi/a/1", "date": "2026-10-06", "age_days": 19, "recency": "current"}
    new = {"url": "https://yle.fi/a/2", "date": "2026-10-24", "age_days": 1, "recency": "current"}
    undated = {"url": "https://yle.fi/a/3", "date": None, "age_days": None}

    late = score_results(row, [old, undated, old, new], k=5, current_year=2026)
    early = score_results(row, [new, old, undated], k=5, current_year=2026)
    unasked = score_results({"query": "x", "expected_domains": ["yle.fi"]}, [new], k=5)

    assert late["fresh_at_3"] is False and early["fresh_at_3"] is True
    assert unasked["fresh_at_3"] is None
    assert late["dated_at_5"] == 0.75 and early["dated_at_5"] == 0.6667


def test_summarize_counts_only_answered_queries() -> None:
    answered = [
        {"ndcg_at_5": 1.0, "mrr": 1.0, "official_hit_at_3": True},
        {"ndcg_at_5": 0.5, "mrr": 0.5, "official_hit_at_3": False},
    ]
    summary = summarize(answered, failures=1)

    assert summary["queries"] == 3
    assert summary["answered"] == 2
    assert summary["failed"] == 1
    assert summary["mean_ndcg_at_5"] == 0.75
    assert summary["official_hit_at_3"] == 0.5


def test_query_fixture_is_valid_and_unique() -> None:
    rows = json.loads(QUERIES.read_text(encoding="utf-8"))

    assert len(rows) >= 8
    assert len({row["query"] for row in rows}) == len(rows)
    for row in rows:
        assert row["query"].strip()
        assert row["expected_domains"]
        for domain in row["expected_domains"]:
            assert domain == domain.lower()
            assert "." in domain


def test_the_provider_query_set_is_valid() -> None:
    # benchmarks/search_eval_queries.json: providers side by side (scripts/evaluate_web_search.py --provider).
    from scripts.evaluate_web_search import _ROW_OPTIONS

    rows = json.loads((QUERIES.parent / "search_eval_queries.json").read_text(encoding="utf-8"))

    assert len(rows) >= 20
    assert len({row["query"] for row in rows}) == len(rows)
    for row in rows:
        assert row["query"].strip() and row["kind"] in {
            "docs",
            "news",
            "official",
            "tr",
            "fi",
            "local",
        }
        assert row["expected_domains"]
        assert all(domain == domain.lower() and "." in domain for domain in row["expected_domains"])
        assert set(row.get("options", {})) <= {*_ROW_OPTIONS, "depth"}
        assert row.get("fresh_days") is None or (
            isinstance(row["fresh_days"], int) and row["fresh_days"] > 0
        )
