"""Small, dependency-free relevance checks for web search results."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


def _host(value: str) -> str:
    """The host of a URL or a bare host, without a leading www."""
    text = (value or "").strip().lower()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").strip(".")
    return host[4:] if host.startswith("www.") else host


def domain_matches(url: str, expected_domains: Iterable[str]) -> bool:
    """Whether a URL belongs to one of the expected domains or their subdomains."""
    host = _host(url)
    if not host:
        return False
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in (_host(item) for item in expected_domains)
        if domain
    )


def ndcg_at_k(grades: Sequence[float], k: int) -> float:
    """Normalised discounted cumulative gain for the first k ranked grades."""
    if k <= 0 or not grades:
        return 0.0
    ranked = list(grades[:k])
    ideal = sorted(grades, reverse=True)[:k]
    dcg = sum(grade / math.log2(index + 2) for index, grade in enumerate(ranked))
    idcg = sum(grade / math.log2(index + 2) for index, grade in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def score_results(
    row: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    *,
    k: int = 5,
    current_year: int | None = None,
) -> dict[str, Any]:
    """Score one query's ranked results against its expected domains."""
    top = list(results[:k])
    grades = [
        1.0 if domain_matches(str(result.get("url", "")), row.get("expected_domains", ())) else 0.0
        for result in top
    ]
    relevant_ranks = [index + 1 for index, grade in enumerate(grades) if grade > 0]
    official = any(result.get("source_type") == "official" for result in top[:3])
    freshness = None
    if row.get("freshness"):
        freshness = any(
            result.get("recency") == "current"
            or (current_year is not None and result.get("year") == current_year)
            for result in top
        )
    # By the day: a query about what happens now (``fresh_days``) wants a result of the last days among the first
    # three; "current" is a year, and a story of three weeks ago is current in October.
    fresh_days = row.get("fresh_days")
    fresh_at_3 = None
    if isinstance(fresh_days, int) and fresh_days > 0:
        fresh_at_3 = any(
            isinstance(result.get("age_days"), int) and 0 <= result["age_days"] <= fresh_days
            for result in top[:3]
        )
    domains = [_host(str(result.get("url", ""))) for result in top]
    return {
        "ndcg_at_5": round(ndcg_at_k(grades, k), 4),
        "mrr": round(1.0 / relevant_ranks[0], 4) if relevant_ranks else 0.0,
        "relevant_at_5": len(relevant_ranks),
        "official_hit_at_3": official,
        "freshness_hit_at_5": freshness,
        "fresh_at_3": fresh_at_3,
        "dated_at_5": round(sum(1 for result in top if result.get("date")) / len(top), 4)
        if top
        else 0.0,
        "unique_domains_at_5": len({domain for domain in domains if domain}),
    }


def summarize(answered: Sequence[Mapping[str, Any]], failures: int = 0) -> dict[str, Any]:
    """Average the per-query checks, counting unanswered queries as failures."""
    count = len(answered)

    def mean(key: str) -> float:
        values = [float(item[key]) for item in answered if item.get(key) is not None]
        return round(sum(values) / len(values), 4) if values else 0.0

    return {
        "queries": count + failures,
        "answered": count,
        "failed": failures,
        "mean_ndcg_at_5": mean("ndcg_at_5"),
        "mean_mrr": mean("mrr"),
        "official_hit_at_3": mean("official_hit_at_3"),
        "freshness_hit_at_5": mean("freshness_hit_at_5"),
        "fresh_at_3": mean("fresh_at_3"),
        "mean_dated_at_5": mean("dated_at_5"),
        "mean_unique_domains_at_5": mean("unique_domains_at_5"),
    }
