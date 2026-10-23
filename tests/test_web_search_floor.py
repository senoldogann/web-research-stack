"""A floor under relevance: a result must match the query's distinctive terms as whole words."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_web_search_honesty import _service

from web_scraper.web_tools.discovery import (
    DiscoveryCandidate,
    has_relevant_result,
    rank_candidates,
    relevance_score,
)
from web_scraper.web_tools.search import SearchResult

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
QUERY = "zlorgfimble quaxotronic 7g3"

# What DuckDuckGo answered to QUERY on 2026-10-23 (one live probe, eight results). The three
# results that scored 1/3 hold nothing of the query but the 3-character code 7g3: a channel
# named so, a clone of an antibody, a page of links. The other five hold none of it.
LIVE = [
    SearchResult(
        "7g3 - YouTube",
        "https://www.youtube.com/@7g3_na",
        "Share your videos with friends, family, and the world",
    ),
    SearchResult("Meow-7g3 - YouTube", "https://www.youtube.com/@Meow-zg8r2", "Meow meow"),
    SearchResult(
        "APC Mouse anti-Human CD123 - BD Biosciences",
        "https://www.bdbiosciences.com/en-us/products/reagents/flow-cytometry-reagents/"
        "research-reagents/single-color-antibodies-ruo/apc-mouse-anti-human-cd123.560087"
        "?tab=product_details",
        "The 7G3 monoclonal antibody specifically recognizes human CD123, the 70 kDa IL-3 "
        "Receptor α (IL-3Rα) chain. CD123 associates with CD131, the 120-140 kDa Common β chain",
    ),
    SearchResult(
        "GitHub - Batalhoti/QuattroBox: Fully 3D-printed MMU system with ...",
        "https://github.com/Batalhoti/QuattroBox",
        "QuattroBox v2 is the complete evolution of my MMU for 3D printers.",
    ),
    SearchResult(
        "Cnc G73 Pattern Repeation Cycle for Lathe ..example 1",
        "https://www.cncknowledge.in/2018/07/cnc-g73-pattern-repetition-cycle-for-fanuc-lathe.html",
        "04532 N10 G90 G50 S1200 ; N20 G99 F0.25 ; N30 M03 G96 S200 ; N80 G73 U4 W2 R3 ;",
    ),
    SearchResult(
        "Play Quake III Online - q3.pieter.com (Non-commercial and unofficial)",
        "https://q3.pieter.com/",
        "Play Quake III Arena in your browser at q3.pieter.com.",
    ),
    SearchResult(
        "7g3 Official: Instagram, X | Linktree",
        "https://linktr.ee/7g3",
        "View 7g3's Linktree to discover and stream music from top platforms like Spotify.",
    ),
    SearchResult(
        "CNC Fanuc G73 Pattern Repeating Cycle CNC Program Example",
        "https://www.helmancnc.com/cnc-fanuc-g73-pattern-repeating-cycle-cnc-program-example/",
        "Here is a cnc programming example, I already have posted multiple cnc examples.",
    ),
]


def _items(results: list[SearchResult]) -> list[dict[str, str]]:
    return [{"title": r.title, "snippet": r.snippet, "url": r.url} for r in results]


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", ["fast", "discover", "deep"])
async def test_the_nonsense_query_gets_nothing_and_says_so(depth: str) -> None:
    response = await _service(LIVE).search(QUERY, depth=depth)

    assert response["results"] == []
    assert response["warnings"] == ["no_relevant_results"]
    assert response["facets"]["total_candidates"] == len(LIVE)


def test_a_code_the_query_shares_alone_is_not_relevance() -> None:
    # The query's words (zlorgfimble, quaxotronic) are in no result; 7g3 is a handle, a clone
    # name, a link page: it counts towards a match, it never makes one.
    assert not has_relevant_result(QUERY, _items(LIVE))
    assert has_relevant_result("7g3", _items(LIVE))
    assert has_relevant_result("7g3 antibody", _items(LIVE))


def test_short_codes_help_a_result_that_has_a_word_too() -> None:
    foods = [{"title": "Vitamin B12 foods", "snippet": "", "url": "https://x.example/foods"}]
    deficiency = [{"title": "B12 deficiency", "snippet": "", "url": "https://x.example/b12"}]

    assert not has_relevant_result("b12 deficiency", foods)
    assert has_relevant_result("b12 deficiency", deficiency)
    assert relevance_score("b12 deficiency", "B12 deficiency", "", "") == 1.0


@pytest.mark.asyncio
async def test_a_query_that_is_a_code_alone_still_finds_what_holds_it() -> None:
    response = await _service(LIVE).search("7g3", depth="fast")

    assert [item["url"] for item in response["results"]] == [
        "https://www.youtube.com/@7g3_na",
        "https://www.youtube.com/@Meow-zg8r2",
        LIVE[2].url,
        "https://linktr.ee/7g3",
    ]
    assert response["warnings"] == []


@pytest.mark.asyncio
async def test_a_code_and_a_word_keep_only_the_result_with_the_word() -> None:
    response = await _service(LIVE).search("7g3 antibody", depth="fast")

    assert [item["url"] for item in response["results"]] == [LIVE[2].url]
    assert response["results"][0]["relevance"] == 1.0


@pytest.mark.parametrize(
    ("query", "title", "snippet", "expected"),
    [
        ("go concurrency", "Google Cloud", "gopher gophers", 0.0),
        ("7g3", "a7g3b9 and 7g3x and x7g3", "", 0.0),
        ("asyncio", "asynciox guide", "", 0.0),
        ("meow 7g3", "Meow-7g3 - YouTube", "", 1.0),
        ("sqlite bm25", "GitHub - midclique/sqlite-bm25-search", "", 1.0),
        ("async await", "Async/Await patterns", "", 1.0),
        ("next.js routing", "Next JS routing", "", 1.0),
    ],
)
def test_a_term_matches_as_a_whole_word_never_inside_a_longer_one(
    query: str, title: str, snippet: str, expected: float
) -> None:
    assert relevance_score(query, title, snippet, "https://x.example/") == expected


@pytest.mark.parametrize(
    ("query", "url", "expected"),
    [
        # The id of a handle, a query string and a numbered path are no words of the page.
        ("quaxotronic 7g3", "https://linktr.ee/7g3", 0.0),
        ("quaxotronic watch", "https://youtube.com/watch?v=quaxotronic", 0.5),
        ("quaxotronic clone", "https://x.example/clone/560087?tab=quaxotronic", 0.5),
        # The words of the address count: a slug, a host.
        ("kela housing allowance", "https://www.kela.fi/housing-allowance", 1.0),
        ("türkiye yapay", "https://tr.wikipedia.org/wiki/T%C3%BCrkiye_Yapay_Zek%C3%A2", 1.0),
    ],
)
def test_only_the_words_of_an_address_count_not_its_ids(
    query: str, url: str, expected: float
) -> None:
    assert relevance_score(query, "Weather today", "Sunny and cold", url) == expected


def _candidate(url: str, title: str, snippet: str = "") -> DiscoveryCandidate:
    return DiscoveryCandidate(
        result=SearchResult(title=title, url=url, snippet=snippet, source="fake"),
        angle_ranks={"base": 0},
    )


def test_every_result_under_half_of_the_words_is_low_relevance() -> None:
    query = "oulu rental apartments"
    results = [
        SearchResult("Oulu weather", "https://weather.example/oulu", ""),
        SearchResult("Oulu airport", "https://airport.example/oulu", ""),
    ]

    answer = rank_candidates(query, [_candidate(r.url, r.title) for r in results], 5, year_now=2026)

    assert [item["relevance"] for item in answer] == [pytest.approx(1 / 3)] * 2


@pytest.mark.asyncio
async def test_results_matching_a_third_of_three_words_warn() -> None:
    results = [
        SearchResult("Oulu weather", "https://weather.example/oulu", "Sunny"),
        SearchResult("Oulu airport", "https://airport.example/oulu", "Flights"),
    ]

    response = await _service(results).search("oulu rental apartments", depth="fast")

    assert len(response["results"]) == 2
    assert response["warnings"] == ["low_relevance_results"]


@pytest.mark.asyncio
async def test_two_of_three_words_and_one_of_two_words_do_not_warn() -> None:
    two_of_three = [SearchResult("Oulu rental guide", "https://rent.example/oulu", "")]
    one_of_two = [SearchResult("Oulu weather", "https://weather.example/oulu", "")]

    first = await _service(two_of_three).search("oulu rental apartments", depth="fast")
    second = await _service(one_of_two).search("oulu apartments", depth="fast")

    assert first["warnings"] == []
    assert second["warnings"] == []
    assert second["results"][0]["relevance"] == 0.5


def _benchmark_rows() -> list[tuple[str, list[SearchResult]]]:
    recorded = json.loads((BENCHMARKS / "web_search_relevance.json").read_text(encoding="utf-8"))
    queries = {
        row["query"]
        for row in json.loads((BENCHMARKS / "search_relevance_queries.json").read_text("utf-8"))
    }
    return [
        (row["query"], [SearchResult(item["title"], item["url"], "") for item in row["top"]])
        for row in recorded["results"]
        if row["query"] in queries
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("query", "results"), _benchmark_rows(), ids=lambda value: str(value)[:40])
async def test_real_queries_keep_their_results_and_their_relevance(
    query: str, results: list[SearchResult]
) -> None:
    for depth in ("fast", "discover"):
        response = await _service(results).search(query, limit=8, depth=depth)

        assert {item["url"] for item in response["results"]} == {r.url for r in results}
        assert response["warnings"] == []
        assert max(item["relevance"] for item in response["results"]) >= 0.5
