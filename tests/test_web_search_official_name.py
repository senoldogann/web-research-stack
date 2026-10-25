"""An organization's own site is official, and for a specification it leads.

For "Model Context Protocol specification" 1.3.1 ranked the project's own blog (``blog.modelcontextprotocol.io``),
a copy of its site (``modelcontextprotocol.info``) and the specification page (``modelcontextprotocol.io``) as
``other``, with a blog post first and the spec page fifth. Now a host whose registrable name is the subject's words
joined, and its subdomains, is ``official`` for that query (not the same name on a top-level domain that organizations
do not use), ``primary_first`` is on for a query that asks for a specification, documentation or an API reference,
and what leads is the document itself, its newest edition first.

The pool is what Yahoo answered on 2026-10-24 (``tests/fixtures/search_model_context_protocol_specification.json``,
the three angles of a discover search, before any ranking), and the clock is the day it was recorded.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools import discovery, sources
from web_scraper.web_tools.discovery import (
    DiscoveryCandidate,
    annotate_candidates,
    classify_source_type,
)
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings
from web_scraper.web_tools.urls import canonicalize_url

FIXTURE = Path(__file__).parent / "fixtures" / "search_model_context_protocol_specification.json"
QUERY = "Model Context Protocol specification"
SPEC = "https://modelcontextprotocol.io/specification/2026-07-28"
BLOG = "https://blog.modelcontextprotocol.io/posts/2026-07-28"
LOOKALIKE = "https://modelcontextprotocol.info/specification"
TODAY = date(2026, 10, 24)


class _Date(date):
    @classmethod
    def today(cls) -> date:  # type: ignore[override]
        return TODAY


class _DateTime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return datetime(2026, 10, 24, 12, 0, 0)


@pytest.fixture
def the_day_it_was_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "date", _Date)
    monkeypatch.setattr(discovery, "datetime", _DateTime)


def _recorded() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class RecordedProvider:
    """The provider as it answered: each angle of the search gets the answer recorded for it."""

    name = "recorded"
    headless = False

    def __init__(self) -> None:
        self.angles = _recorded()["angles"]
        self.queries: list[str] = []

    async def search(
        self, query: str, limit: int, *, date_filter: Optional[str] = None, options: Any = None
    ) -> list[SearchResult]:
        self.queries.append(query)
        if query.endswith("history background"):
            angle = "background"
        elif query == self.angles["base"]["query"]:
            angle = "base"
        else:
            angle = "current"  # the base and the year: whichever year the clock says
        return [
            SearchResult(item["title"], item["url"], item["snippet"], item.get("source", ""))
            for item in self.angles[angle]["results"]
        ]


def _service() -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(
            search_provider="recorded",
            search_angle_delay_ms=0,
            search_jitter_min_ms=0,
            search_jitter_max_ms=0,
            search_fallback_providers=[],
            search_resolve_dates="off",
        ),
        fetcher=None,  # type: ignore[arg-type]
        search_adapters={"recorded": RecordedProvider()},
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
    )


@pytest.mark.usefixtures("the_day_it_was_recorded")
class TestTheRecordedSearch:
    @pytest.mark.asyncio
    async def test_the_official_specification_page_leads(self) -> None:
        answer = await _service().search(QUERY, compact=True, limit=8)

        results = answer["results"]
        assert results[0]["url"] == SPEC and results[0]["source_type"] == "official"
        # Before: the spec page was `other` and fifth, under the project's blog and a copy of its site.
        by_url = {item["url"]: item for item in results}
        assert by_url[BLOG]["source_type"] == "official"
        assert by_url[BLOG]["score"] < by_url[SPEC]["score"]

    @pytest.mark.asyncio
    async def test_the_projects_hosts_are_official_and_a_copy_of_its_site_is_not(self) -> None:
        answer = await _service().search(QUERY, compact=True, limit=8)

        types = {item["url"]: item["source_type"] for item in answer["results"]}
        officials = [url for url, kind in types.items() if kind == "official"]
        assert officials and all("modelcontextprotocol.io" in url for url in officials)
        assert types[LOOKALIKE] == "other"
        assert types["https://en.wikipedia.org/wiki/Model_Context_Protocol"] == "reference"
        assert (
            types[
                "https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/index.mdx"
            ]
            == "code"
        )
        # The project's own pages go before the copy of its site; a page of it that says little may not.
        order = [item["url"] for item in answer["results"]]
        assert order.index(SPEC) < order.index(BLOG) < order.index(LOOKALIKE)

    @pytest.mark.asyncio
    async def test_the_newest_edition_of_the_specification_is_the_one_that_leads(self) -> None:
        answer = await _service().search(QUERY, compact=True, limit=8)

        urls = [item["url"] for item in answer["results"]]
        older = [url for url in urls if "/specification/2025-" in url]
        assert all(urls.index(SPEC) < urls.index(url) for url in older)

    @pytest.mark.asyncio
    async def test_the_bonus_and_the_demotion_are_the_arithmetic_of_what_the_page_is(self) -> None:
        service = _service()

        on = {
            item["url"]: item["score"]
            for item in (await service.search(QUERY, compact=True, limit=20))["results"]
        }
        off = {
            item["url"]: item["score"]
            for item in (await service.search(QUERY, compact=True, limit=20, primary_first=False))[
                "results"
            ]
        }

        # The page that is the specification, asked for as one: the primary bonus and a document bonus twice.
        assert on[SPEC] - off[SPEC] == pytest.approx(0.12 + 0.24, abs=1e-5)
        # A post about it keeps its authority and loses the primary bonus it would have had: no difference.
        assert on[BLOG] - off[BLOG] == pytest.approx(0.0, abs=1e-5)
        # The older edition of the specification is demoted a tenth under the newest, and has the bonuses.
        older = "https://modelcontextprotocol.io/specification/2025-03-26"
        assert on[older] - off[older] == pytest.approx(0.12 + 0.24 - 0.10, abs=1e-5)
        # What a site is does not depend on any of it.
        assert on[LOOKALIKE] == pytest.approx(off[LOOKALIKE], abs=1e-5)

    @pytest.mark.asyncio
    async def test_asked_not_to_the_bonus_is_gone_and_the_blog_leads_as_it_did(self) -> None:
        service = _service()

        on = await service.search(QUERY, compact=True, limit=8)
        off = await service.search(QUERY, compact=True, limit=8, primary_first=False)

        assert on["results"][0]["url"] == SPEC and off["results"][0]["url"] == BLOG
        assert {item["url"]: item["source_type"] for item in off["results"]}[LOOKALIKE] == "other"

    @pytest.mark.asyncio
    async def test_a_query_that_asks_for_no_document_leaves_the_bonus_off(self) -> None:
        service = _service()

        default = await service.search("Model Context Protocol", compact=True, limit=20)
        off = await service.search(
            "Model Context Protocol", compact=True, limit=20, primary_first=False
        )
        forced = await service.search(
            "Model Context Protocol", compact=True, limit=20, primary_first=True
        )

        scores = lambda answer: {item["url"]: item["score"] for item in answer["results"]}  # noqa: E731
        # Still official (the name is the subject), but nothing is added: no specification was asked for.
        assert scores(default) == scores(off)
        types = {item["url"]: item["source_type"] for item in default["results"]}
        assert types[SPEC] == "official" and types[BLOG] == "official"
        # Forced by the caller, the older table: official sites get 0.12.
        assert scores(forced)[SPEC] - scores(default)[SPEC] == pytest.approx(0.12, abs=1e-5)


def _candidates() -> list[DiscoveryCandidate]:
    """The recorded pool as the service merges it: one candidate per address, the rank it had in each angle."""
    pool: dict[str, DiscoveryCandidate] = {}
    for angle, answer in _recorded()["angles"].items():
        for rank, item in enumerate(answer["results"]):
            url = canonicalize_url(item["url"])
            if url in pool:
                pool[url].angle_ranks.setdefault(angle, rank)
            else:
                pool[url] = DiscoveryCandidate(
                    SearchResult(item["title"], url, item["snippet"], item.get("source", "")),
                    {angle: rank},
                )
    return list(pool.values())


FAMILY = [
    "https://modelcontextprotocol.io/specification/2026-07-28",  # the project's own
    "https://modelcontextprotocol.dev/specification/2026-07-28",  # whoever registers the name elsewhere
    "https://modelcontextprotocol.app/specification",
    "https://modelcontextprotocol.co/spec",
    "https://model-context-protocol.com/specification",
]


def _pool(urls: list[str], angles: tuple[str, ...] = ("base",)) -> list[DiscoveryCandidate]:
    """A pool in which each address has the rank of its place in *urls* in every one of *angles*."""
    return [
        DiscoveryCandidate(
            SearchResult(
                f"Model Context Protocol specification {n}", url, "The specification.", ""
            ),
            dict.fromkeys(angles, rank),
        )
        for rank, (n, url) in enumerate(enumerate(urls))
    ]


def _types(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {row["url"]: row["source_type"] for row in rows}


class TestOnlyOneOfAFamilyOfNamesIsOfficial:
    """The same name on several top-level domains an organization uses is one site and its lookalikes: 1.4.1 made
    every one of them ``official`` (``modelcontextprotocol.dev``, ``.app``, ``.co``, ``model-context-protocol.com``
    beside the project's ``.io``). In a pool, only the host the providers rank highest among those of the name is."""

    def test_the_one_ranked_first_is_official_and_the_others_are_not(self) -> None:
        rows = annotate_candidates(QUERY, _pool(FAMILY + [LOOKALIKE]))

        types = _types(rows)
        assert types[FAMILY[0]] == "official"
        assert [types[url] for url in FAMILY[1:]] == ["other"] * 4
        assert types[LOOKALIKE] == "other"  # not an organization's top-level domain: as before

    def test_its_subdomains_are_official_with_it_and_a_forum_still_is_not(self) -> None:
        pool = _pool(
            [
                FAMILY[0],
                FAMILY[1],
                "https://blog.modelcontextprotocol.io/posts/1",
                "https://docs.modelcontextprotocol.io/start",
                "https://discuss.modelcontextprotocol.io/t/1",
                "https://blog.modelcontextprotocol.dev/posts/1",
            ]
        )

        types = _types(annotate_candidates(QUERY, pool))

        assert (
            types[FAMILY[0]] == types["https://blog.modelcontextprotocol.io/posts/1"] == "official"
        )
        assert types["https://docs.modelcontextprotocol.io/start"] == "official"
        assert types["https://discuss.modelcontextprotocol.io/t/1"] == "other"
        assert types[FAMILY[1]] == types["https://blog.modelcontextprotocol.dev/posts/1"] == "other"

    def test_the_providers_ranking_is_the_evidence_whichever_host_it_favours(self) -> None:
        # Nothing else tells the project's host from a copy: the one the providers put first is the one.
        types = _types(annotate_candidates(QUERY, _pool(list(reversed(FAMILY)))))

        assert types[FAMILY[-1]] == "official"
        assert [types[url] for url in FAMILY[:-1]] == ["other"] * 4

    def test_agreement_of_several_angles_outweighs_one_first_place(self) -> None:
        real = DiscoveryCandidate(
            SearchResult(
                "Model Context Protocol specification", FAMILY[0], "The specification.", ""
            ),
            {"base": 2, "current": 2, "background": 2},
        )
        copy = DiscoveryCandidate(
            SearchResult(
                "Model Context Protocol specification", FAMILY[1], "The specification.", ""
            ),
            {"base": 0},
        )

        types = _types(annotate_candidates(QUERY, [copy, real]))

        assert types[FAMILY[0]] == "official" and types[FAMILY[1]] == "other"

    def test_the_best_ranked_page_of_a_host_stands_for_the_host(self) -> None:
        # The lookalike's first place is its one page; the project has a page at 0 too and others behind it.
        pool = _pool([FAMILY[0], FAMILY[1], "https://modelcontextprotocol.dev/b"])
        pool += _pool(["https://modelcontextprotocol.io/clients"] * 1)

        types = _types(annotate_candidates(QUERY, pool))

        assert types[FAMILY[0]] == types["https://modelcontextprotocol.io/clients"] == "official"
        assert types[FAMILY[1]] == types["https://modelcontextprotocol.dev/b"] == "other"

    def test_a_host_the_tables_name_is_the_one_even_when_a_lookalike_is_ranked_above_it(
        self,
    ) -> None:
        rules = sources.DEFAULT_RULES.extended(first_party=["modelcontextprotocol.io"])
        pool = _pool([FAMILY[1], FAMILY[2], FAMILY[0]])

        types = _types(annotate_candidates(QUERY, pool, rules=rules))

        assert types[FAMILY[0]] == "official"
        assert types[FAMILY[1]] == types[FAMILY[2]] == "other"

    def test_a_name_that_only_one_host_is_found_on_is_official_as_before(self) -> None:
        # No ranking can tell a lone host from the project's own: the older rule stands.
        types = _types(annotate_candidates(QUERY, _pool([FAMILY[1], LOOKALIKE])))

        assert types[FAMILY[1]] == "official" and types[LOOKALIKE] == "other"

    def test_a_pool_that_is_not_about_the_name_has_no_family(self) -> None:
        types = _types(annotate_candidates("something else entirely", _pool(FAMILY)))

        assert set(types.values()) == {"other"}

    def test_the_older_rule_for_a_name_of_one_word_is_not_a_family(self) -> None:
        pool = _pool(
            ["https://docs.frobnicator.com/start", "https://docs.frobnicator.dev/start"],
        )

        types = _types(annotate_candidates("frobnicator specification", pool))

        assert set(types.values()) == {
            "official"
        }  # _site_named_by: the organization's docs, any of them

    def test_a_search_says_it_the_same_way(self) -> None:
        class Provider:
            name = "family"
            headless = False

            async def search(self, query: str, limit: int, **options: Any) -> list[SearchResult]:
                return [
                    SearchResult(f"Model Context Protocol specification {n}", url, "Spec.", "")
                    for n, url in enumerate(FAMILY)
                ]

        service = WebToolsService(
            settings=WebToolsSettings(
                search_provider="family",
                search_angle_delay_ms=0,
                search_jitter_min_ms=0,
                search_jitter_max_ms=0,
                search_fallback_providers=[],
                search_resolve_dates="off",
            ),
            fetcher=None,  # type: ignore[arg-type]
            search_adapters={"family": Provider()},
            cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        )

        async def run() -> dict[str, Any]:
            return await service.search(QUERY, compact=True, limit=10)

        answer = asyncio.run(run())

        types = {item["url"]: item["source_type"] for item in answer["results"]}
        assert [url for url, kind in types.items() if kind == "official"] == [FAMILY[0]]
        assert len(types) == len(FAMILY)


class TestWhoIsOfficialByName:
    @pytest.mark.parametrize(
        ("url", "query", "expected"),
        [
            # The subject's words joined are the host's name: any page of it, and of its subdomains.
            (SPEC, QUERY, "official"),
            (BLOG, QUERY, "official"),
            ("https://modelcontextprotocol.io/", QUERY, "official"),
            ("https://www.modelcontextprotocol.io/clients", QUERY, "official"),
            ("https://docs.modelcontextprotocol.io/start", QUERY, "official"),
            (
                "https://modelcontextprotocol.io/some/page/that/is/an/article",
                "model context protocol",
                "official",
            ),
            ("https://model-context-protocol.org/spec", QUERY, "official"),
            (
                "https://reactnative.dev/docs/getting-started",
                "react native documentation",
                "official",
            ),
            ("https://golang.org/doc/", "go lang docs", "official"),
            # Words that are not the subject (a kind of page, a number, a stopword) are not in the name.
            (SPEC, "the Model Context Protocol 2025-06-18 specification latest", "official"),
            # Another top-level domain: where a copy of a site is put.
            (LOOKALIKE, QUERY, "other"),
            ("https://modelcontextprotocol.net/specification", QUERY, "other"),
            ("https://modelcontextprotocol.xyz/", QUERY, "other"),
            ("https://modelcontextprotocol.biz/", QUERY, "other"),
            (
                "https://reactnative.dev/some-article-about-things",
                "react native documentation",
                "official",
            ),
            (
                "https://reactnative.info/some-article-about-things",
                "react native documentation",
                "other",
            ),
            # Not the name.
            ("https://modelcontextprotocol-tutorial.io/", QUERY, "other"),
            ("https://notmodelcontextprotocol.io/", QUERY, "other"),
            ("https://modelcontext.io/some-article-about-things", QUERY, "other"),
            # Not the query's subject.
            (SPEC, "something else entirely", "other"),
            (SPEC, "", "other"),
            # A forum on the domain is not the organization speaking.
            ("https://discuss.modelcontextprotocol.io/t/1", QUERY, "other"),
            # A name of one word is the older rule's: only the organization's own pages (docs, blog...).
            (
                "https://frobnicator.com/some-article-about-nothing",
                "frobnicator specification",
                "other",
            ),
            ("https://docs.frobnicator.com/start", "frobnicator specification", "official"),
            # Short joined names are not names.
            ("https://ab.io/", "a b specification", "other"),
        ],
    )
    def test_a_host_named_as_the_subject_is_official_for_that_query(
        self, url: str, query: str, expected: str
    ) -> None:
        assert classify_source_type(url, query=query) == expected

    def test_the_tables_still_decide_first(self) -> None:
        # What the other tables say about a host is not undone by the query.
        assert (
            classify_source_type(
                "https://github.com/modelcontextprotocol/modelcontextprotocol", query=QUERY
            )
            == "code"
        )
        assert (
            classify_source_type(
                "https://en.wikipedia.org/wiki/Model_Context_Protocol", query=QUERY
            )
            == "reference"
        )
        assert (
            classify_source_type(
                "https://www.anthropic.com/news/model-context-protocol", query=QUERY
            )
            == "official"
        )
        assert (
            classify_source_type("https://modelcontextprotocol.medium.com/x", query=QUERY)
            == "community"
        )

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("modelcontextprotocol.io", True),
            ("blog.example.com", True),
            ("example.dev", True),
            ("example.ai", True),
            ("example.app", True),
            ("example.co", True),
            ("example.org", True),
            ("example.co.uk", True),
            ("example.com.au", True),
            ("example.info", False),
            ("example.net", False),
            ("example.xyz", False),
            ("example.gov.uk", False),
            ("example.ac.uk", False),
        ],
    )
    def test_the_top_level_domains_an_organization_is_on(self, host: str, expected: bool) -> None:
        assert sources.has_organization_suffix(host) is expected


class TestPrimaryFirstForDocumentQueries:
    @pytest.mark.parametrize(
        "query",
        [
            "Model Context Protocol specification",
            "react documentation",
            "stripe API reference",
            "kubernetes docs",
            "python 3.12 specs",
            "react dokümantasyon",
            "HTTP spesifikasyonu",
            "Oulu dokumentaatio",
            "OAuth Spezifikation",
        ],
    )
    def test_a_query_that_asks_for_the_thing_itself(self, query: str) -> None:
        assert discovery.asks_for_primary_source(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "react hooks tutorial",
            "python asyncio",
            "latest openai news",
            "api rate limits",
            "reference letter",
        ],
    )
    def test_and_one_that_does_not(self, query: str) -> None:
        assert discovery.asks_for_primary_source(query) is False

    @staticmethod
    def _pool() -> list[DiscoveryCandidate]:
        words = "Everything about the Frobnicator protocol, said once. "
        urls = {
            "official": "https://openai.com/index/frobnicator-protocol",
            "news": "https://www.reuters.com/technology/frobnicator",
            "academic": "https://arxiv.org/abs/2601.00001",
            "other": "https://unknown-site.example/frobnicator",
        }
        return [
            DiscoveryCandidate(SearchResult("Frobnicator protocol", url, words), {"base": 0})
            for url in urls.values()
        ]

    def _scores(self, query: str, **options: Any) -> dict[str, float]:
        rows = annotate_candidates(query, self._pool(), today=TODAY, **options)
        return {row["source_type"]: row["score"] for row in rows}

    def test_it_is_on_for_a_document_query_and_only_the_organizations_own_pages_lead(self) -> None:
        query = "frobnicator protocol specification"
        on = self._scores(query)
        off = self._scores(query, primary_first=False)

        assert on["official"] - off["official"] == pytest.approx(0.12)
        assert on["academic"] - off["academic"] == pytest.approx(0.05)
        # An outlet that writes about a specification is not the specification: no bonus.
        assert on["news"] == off["news"] and on["other"] == off["other"]

    def test_asked_for_by_the_caller_it_is_the_news_table_whatever_the_query(self) -> None:
        query = "frobnicator protocol specification"
        on = self._scores(query, primary_first=True)
        off = self._scores(query, primary_first=False)

        assert on["official"] - off["official"] == pytest.approx(0.12)
        assert on["news"] - off["news"] == pytest.approx(0.10)

    def test_a_news_query_keeps_the_news_table_and_a_plain_query_has_none(self) -> None:
        news = self._scores("frobnicator protocol news this week")
        news_off = self._scores("frobnicator protocol news this week", primary_first=False)
        plain = self._scores("frobnicator protocol")
        plain_off = self._scores("frobnicator protocol", primary_first=False)

        assert news["official"] > news_off["official"] and news["news"] > news_off["news"]
        assert plain == plain_off

    def test_a_query_that_is_news_and_asks_for_docs_is_a_news_query(self) -> None:
        both = self._scores("frobnicator documentation news this week")
        off = self._scores("frobnicator documentation news this week", primary_first=False)

        assert both["news"] > off["news"]

    def test_the_document_goes_before_a_post_about_it_and_the_kind_asked_for_counts_twice(
        self,
    ) -> None:
        def candidate(title: str, url: str, **ranks: int) -> DiscoveryCandidate:
            return DiscoveryCandidate(
                SearchResult(title, url, "The Frobnicator specification, as published."),
                ranks or {"base": 0},
            )

        spec = "https://openai.com/specification/frobnicator"
        docs = "https://openai.com/docs/frobnicator-notes"
        post = "https://openai.com/blog/frobnicator-specification"
        pool = [
            candidate("Frobnicator specification", spec),
            candidate("Frobnicator documentation", docs),
            candidate("Frobnicator specification news", post, base=0, current=0, background=0),
        ]

        def scores(**options: Any) -> dict[str, float]:
            rows = annotate_candidates("frobnicator specification", pool, today=TODAY, **options)
            return {row["url"]: row["score"] for row in rows}

        on, off = scores(), scores(primary_first=False)

        # The primary bonus 0.12 for each page; and a /specification/ page, which is what was asked for, gets a
        # document bonus twice (0.24), a /docs/ page once (0.12); a post about it loses the primary bonus.
        assert on[spec] - off[spec] == pytest.approx(0.12 + 0.24)
        assert on[docs] - off[docs] == pytest.approx(0.12 + 0.12)
        assert on[post] - off[post] == pytest.approx(0.0)

    def test_the_kinds_that_lead_are_not_held_back_for_variety(self) -> None:
        def row(url: str, score: float, kind: str) -> dict[str, Any]:
            domain = url.split("/")[2]
            return {
                "url": url,
                "domain": domain,
                "score": score,
                "source_type": kind,
                "title": f"title of {domain}",
                "snippet": f"words of {domain}",
            }

        ranked = [
            row("https://a.example/1", 1.00, "official"),
            row("https://b.example/1", 0.95, "official"),
            row("https://c.example/1", 0.90, "official"),
            row("https://d.example/1", 0.85, "other"),
        ]

        spared = discovery._select_diverse(list(ranked), 4, spared=frozenset({"official"}))
        held_back = discovery._select_diverse(list(ranked), 4)

        # Each kind after its first loses a little to the others, so that one kind cannot fill the page...
        assert [item["source_type"] for item in held_back] == [
            "official",
            "official",
            "other",
            "official",
        ]
        # ...unless the query is for a document and the kind is the one that leads.
        assert [item["source_type"] for item in spared] == [
            "official",
            "official",
            "official",
            "other",
        ]

    def test_an_older_edition_goes_after_the_newest_and_a_lone_edition_after_nobody(self) -> None:
        def row(url: str) -> dict[str, Any]:
            return {"url": url, "score": 1.0}

        rows = [
            row(
                "https://x.org/docs/guide/page/1"
            ),  # a bare number is a page number, not an edition
            row("https://x.org/docs/guide/page/2"),
            row("https://x.org/handbook/2024/intro"),  # a year is
            row("https://x.org/handbook/2025/intro"),
            row("https://x.org/specification/2025-03-26"),
            row("https://x.org/specification/2026-07-28"),
            row("https://x.org/specification/2026-07-28/architecture"),  # no other edition of it
            row("https://x.org/docs/3.11/library/asyncio"),
            row("https://x.org/docs/3.12/library/asyncio"),
            row("https://x.org/docs/v2/guide"),
            row("https://x.org/docs/v10/guide"),  # 10 is more than 2
            row("https://x.org/docs/latest/library/asyncio"),  # an alias, no edition
            row("https://y.org/specification/2025-03-26"),  # another host, another document
        ]

        discovery._demote_superseded(rows)

        scores = {item["url"]: item["score"] for item in rows}
        assert scores["https://x.org/docs/guide/page/1"] == 1.0
        assert scores["https://x.org/docs/guide/page/2"] == 1.0
        assert scores["https://x.org/handbook/2024/intro"] == pytest.approx(0.90)
        assert scores["https://x.org/handbook/2025/intro"] == 1.0
        assert scores["https://x.org/specification/2025-03-26"] == pytest.approx(0.90)
        assert scores["https://x.org/specification/2026-07-28"] == 1.0
        assert scores["https://x.org/specification/2026-07-28/architecture"] == 1.0
        assert scores["https://x.org/docs/3.11/library/asyncio"] == pytest.approx(0.90)
        assert scores["https://x.org/docs/3.12/library/asyncio"] == 1.0
        assert scores["https://x.org/docs/v2/guide"] == pytest.approx(0.90)
        assert scores["https://x.org/docs/v10/guide"] == 1.0
        assert scores["https://x.org/docs/latest/library/asyncio"] == 1.0
        assert scores["https://y.org/specification/2025-03-26"] == 1.0

    def test_only_the_organizations_own_pages_of_a_query_without_a_number_are_demoted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[list[str]] = []
        monkeypatch.setattr(
            discovery, "_demote_superseded", lambda rows: asked.append([row["url"] for row in rows])
        )

        annotate_candidates(QUERY, _candidates(), today=TODAY)
        annotate_candidates(QUERY + " 2025-03-26", _candidates(), today=TODAY)  # names an edition
        annotate_candidates(
            "Model Context Protocol", _candidates(), today=TODAY
        )  # asks for no document
        annotate_candidates(QUERY, _candidates(), today=TODAY, primary_first=False)  # asked not to

        # Once, and only the official pages of the pool: a copy of the site is not an edition of it.
        assert len(asked) == 1
        assert SPEC in asked[0] and LOOKALIKE not in asked[0]

    def test_the_ten_recorded_queries_are_not_document_queries_and_keep_their_order(self) -> None:
        recorded = json.loads(
            (
                Path(__file__).resolve().parents[1] / "benchmarks" / "web_search_relevance.json"
            ).read_text(encoding="utf-8")
        )
        docs_like = [
            row["query"]
            for row in recorded["results"]
            if discovery.asks_for_primary_source(row["query"])
        ]

        # The golden order of tests/test_web_search_sources.py holds because none asks for a document.
        assert docs_like == []
