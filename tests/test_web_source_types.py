"""Source-type classification for EU, UK, and Finnish official and academic hosts."""

from __future__ import annotations

import pytest

from web_scraper.web_tools.discovery import (
    DiscoveryCandidate,
    classify_source_type,
    rank_candidates,
)
from web_scraper.web_tools.search import SearchResult


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.kela.fi/web/en/housing-allowance", "official"),
        ("https://www.traficom.fi/en/transport/road", "official"),
        ("https://stat.fi/en/statistics/vaerak", "official"),
        ("https://www.vero.fi/en/individuals/tax-cards-and-tax-returns/", "official"),
        ("https://dvv.fi/en/individuals", "official"),
        ("https://www.eduskunta.fi/EN/Pages/default.aspx", "official"),
        ("https://www.oamk.fi/en/", "official"),
        ("https://www.oulu.fi/en/university", "academic"),
        ("https://yle.fi/a/74-20150000", "news"),
        ("https://eur-lex.europa.eu/eli/reg/2024/1689/oj", "official"),
        ("https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai", "official"),
        ("https://commission.europa.eu/news/ai-act-enters-force-2024-08-01_en", "official"),
        ("https://www.europarl.europa.eu/topics/en/article/20230601STO93804/eu-ai-act", "official"),
    ],
)
def test_finnish_and_eu_public_hosts(url: str, expected: str) -> None:
    assert classify_source_type(url) == expected


@pytest.mark.parametrize(
    "host",
    [
        "www.helsinki.fi",
        "www.aalto.fi",
        "www.utu.fi",
        "www.jyu.fi",
        "www.tuni.fi",
        "www.uwasa.fi",
        "www.abo.fi",
        "www.lappeenranta.fi",
        "www.lut.fi",
        "oulu.fi",
    ],
)
def test_finnish_universities_are_academic(host: str) -> None:
    assert classify_source_type(f"https://{host}/en/research") == "academic"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.gov.uk/guidance/ai-regulation", "official"),
        ("https://assets.publishing.service.gov.uk/media/x.pdf", "official"),
        ("https://www.turkiye.gov.tr/e-devlet", "official"),
        ("https://www.nasa.gov/mission", "official"),
        ("https://www.army.mil/", "official"),
        ("https://www.who.int/news", "official"),
        ("https://www.ox.ac.uk/research", "academic"),
        ("https://www.metu.edu.tr/research", "academic"),
        ("https://www.u-tokyo.ac.jp/en/", "academic"),
        ("https://www.mygov.uk/blog", "other"),
        ("https://www.regulation-ai.eu/en/blog/ai-act-timeline", "other"),
        ("https://artificialintelligenceact.eu/implementation-timeline/", "other"),
    ],
)
def test_government_and_academic_suffixes(url: str, expected: str) -> None:
    assert classify_source_type(url) == expected


def test_official_eu_timeline_outranks_a_commercial_compliance_blog() -> None:
    blog = DiscoveryCandidate(
        result=SearchResult(
            "EU AI Act implementation timeline",
            "https://www.regulation-ai.eu/en/blog/ai-act-timeline",
            "Key dates of the EU AI Act",
        ),
        angle_ranks={"base": 0},
    )
    official = DiscoveryCandidate(
        result=SearchResult(
            "AI Act implementation timeline",
            "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
            "The EU AI Act applies in stages",
        ),
        angle_ranks={"base": 2},
    )

    results = rank_candidates("eu ai act timeline", [blog, official], 5, year_now=2026)

    assert [item["source_type"] for item in results] == ["official", "other"]
    assert results[0]["url"] == official.result.url
