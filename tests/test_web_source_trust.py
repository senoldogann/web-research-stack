"""Source types that are right: first-party sites are official, aggregators rank lower, primary sources lead."""

from __future__ import annotations

from datetime import date

import pytest
from test_web_service import FakeFetcher, FakeSearch, _service

from web_scraper.api_models import WebSearchRequest
from web_scraper.web_tools.discovery import (
    DiscoveryCandidate,
    annotate_candidates,
    build_facets,
    classify_source_type,
    rank_candidates,
)
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.settings import WebToolsSettings
from web_scraper.web_tools.sources import DEFAULT_RULES, SourceRules

TODAY = date(2026, 10, 23)


@pytest.mark.parametrize(
    "url",
    [
        "https://openai.com/index/gpt-6/",
        "https://www.openai.com/blog/x",
        "https://help.openai.com/en/articles/1",
        "https://cloud.google.com/blog/products/ai-machine-learning/x",
        "https://blog.google/technology/ai/x/",
        "https://deepmind.google/discover/blog/x/",
        "https://research.google/blog/x/",
        "https://anthropic.com/news/claude",
        "https://www.anthropic.com/news/claude",
        "https://ai.meta.com/blog/x/",
        "https://about.fb.com/news/2026/10/x/",
        "https://blogs.microsoft.com/blog/2026/10/x/",
        "https://news.microsoft.com/source/x/",
        "https://azure.microsoft.com/en-us/blog/x/",
        "https://devblogs.microsoft.com/x/",
        "https://www.apple.com/newsroom/2026/10/x/",
        "https://machinelearning.apple.com/research/x",
        "https://aws.amazon.com/blogs/machine-learning/x/",
        "https://blogs.nvidia.com/blog/x/",
        "https://www.some-brand.google/anything",  # a brand's own top-level domain
    ],
)
def test_known_first_party_domains_are_official(url: str) -> None:
    assert classify_source_type(url) == "official"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.apple.com/iphone/", "other"),  # only the newsroom of apple.com is listed
        (
            "https://community.openai.com/t/question/1",
            "other",
        ),  # a forum on the domain is not OpenAI
        ("https://openai.com.evil.example/index/gpt-6/", "other"),
        ("https://notopenai.com/index", "other"),
        ("https://www.facebook.com/openai", "social"),
        ("https://x.com/openai/status/1", "social"),
        ("https://www.reuters.com/technology/openai-2026-10-21/", "news"),
        ("https://github.com/openai/whisper", "code"),
    ],
)
def test_what_is_not_first_party_stays_what_it_was(url: str, expected: str) -> None:
    assert classify_source_type(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://news.google.com/articles/abc",
        "https://www.msn.com/en-us/news/technology/x/ar-1",
        "https://flipboard.com/@someone/ai-news",
        "https://ground.news/article/openai-google",
        "https://www.newsbreak.com/news/1/x",
        "https://techmeme.com/261021/p1",
        "https://news.yahoo.com/x.html",
    ],
)
def test_known_aggregators_have_their_own_type(url: str) -> None:
    assert classify_source_type(url) == "aggregator"


class TestAnOrganizationTheQueryNames:
    def test_its_docs_blog_and_developer_pages_are_official_for_that_query(self) -> None:
        for url in (
            "https://developers.notion.com/reference/request-limits",
            "https://www.notion.so/help/guides",
            "https://notion.so/",
            "https://docs.notion.com/start",
        ):
            assert classify_source_type(url, query="notion api rate limits") == "official", url

    def test_without_the_query_naming_it_they_are_not(self) -> None:
        assert classify_source_type("https://developers.notion.com/reference/x") == "reference"
        assert classify_source_type("https://notion.so/help/guides") == "other"
        assert (
            classify_source_type("https://notion.so/help/guides", query="api rate limits")
            == "other"
        )

    def test_two_words_joined_are_a_name(self) -> None:
        assert (
            classify_source_type("https://docs.sendgrid.com/api", query="send grid api keys")
            == "official"
        )

    def test_an_article_that_merely_sits_on_a_site_with_the_name_is_not(self) -> None:
        query = "Türkiye yapay zeka eylem planı 2026"
        url = "https://turkiye.ai/turkiye-yapay-zeka-eylem-plani-aciklandi"

        assert classify_source_type(url, query=query) == "other"

    @pytest.mark.parametrize(
        ("url", "query"),
        [
            ("https://time.com/7000/ai", "time series database"),  # a word that is only a word
            ("https://www.nasa.gov/mission", "nasa mission"),  # gov: official by its suffix already
        ],
    )
    def test_a_word_that_is_only_a_word_names_nobody(self, url: str, query: str) -> None:
        assert classify_source_type(url, query=query) in {"news", "official"}

    def test_a_known_outlet_stays_an_outlet(self) -> None:
        assert classify_source_type("https://www.reuters.com/", query="reuters ai deals") == "news"

    def test_a_short_name_is_never_enough(self) -> None:
        assert classify_source_type("https://x.example/docs/a", query="x api docs") == "reference"


class TestTheTablesAreExtendable:
    def test_a_host_or_a_host_and_a_path_is_added(self) -> None:
        rules = DEFAULT_RULES.extended(
            first_party=["https://www.Example.org/newsroom/", "acme.example"],
            aggregators=["clone.example"],
        )

        assert (
            classify_source_type("https://example.org/newsroom/2026/x", rules=rules) == "official"
        )
        assert classify_source_type("https://example.org/shop", rules=rules) == "other"
        assert classify_source_type("https://blog.acme.example/post", rules=rules) == "official"
        assert classify_source_type("https://clone.example/copy", rules=rules) == "aggregator"
        # The built-in tables are not touched.
        assert classify_source_type("https://example.org/newsroom/2026/x") == "other"
        assert isinstance(rules, SourceRules) and DEFAULT_RULES is not rules

    def test_the_environment_adds_to_the_tables_of_a_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WEB_TOOLS_OFFICIAL_HOSTS", "acme.example, example.org/newsroom")
        monkeypatch.setenv("WEB_TOOLS_AGGREGATOR_HOSTS", "clone.example")

        settings = WebToolsSettings.from_env()
        service = _service(FakeFetcher({}), settings=settings)

        assert settings.official_hosts == ["acme.example", "example.org/newsroom"]
        rules = service._source_rules
        assert classify_source_type("https://acme.example/", rules=rules) == "official"
        assert classify_source_type("https://clone.example/x", rules=rules) == "aggregator"
        assert WebToolsSettings().official_hosts == []


def _candidate(title: str, url: str, snippet: str, **ranks: int) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        result=SearchResult(title, url, snippet), angle_ranks=ranks or {"base": 0}
    )


class TestAggregatorsRankLower:
    def _pair(self, aggregator_host: str) -> list[DiscoveryCandidate]:
        words = "OpenAI sells models through Google Cloud to enterprise customers."
        return [
            _candidate(
                "OpenAI sells models through Google Cloud",
                f"https://{aggregator_host}/openai-sells-models",
                words,
                base=0,
            ),
            _candidate(
                "OpenAI sells models through Google Cloud",
                "https://unknown-site.example/openai-sells-models",
                words,
                base=0,
            ),
        ]

    def test_the_penalty_is_a_fixed_part_of_the_score_for_any_query(self) -> None:
        for query in (
            "openai sells models enterprise customers",
            "openai sells models news this week",
        ):
            scores = {
                item["domain"]: item["score"]
                for item in annotate_candidates(query, self._pair("flipboard.com"), today=TODAY)
            }

            # The host's own words weigh a little in BM25 (its address is a field): hence the tolerance.
            gap = scores["unknown-site.example"] - scores["flipboard.com"]
            assert gap == pytest.approx(0.12, abs=0.002)

    def test_an_aggregator_ranked_first_by_the_provider_goes_below_the_report(self) -> None:
        report = _candidate(
            "OpenAI sells models through Google Cloud",
            "https://www.reuters.com/technology/openai-sells-models-2026-10-21/",
            "OpenAI sells models through Google Cloud, the companies said.",
            base=3,
        )
        copy = _candidate(
            "OpenAI sells models through Google Cloud",
            "https://news.google.com/articles/openai-sells-models",
            "OpenAI sells models through Google Cloud, the companies said.",
            base=0,
        )

        ranked = rank_candidates(
            "openai sells models through google cloud", [copy, report], 2, today=TODAY
        )

        assert [item["source_type"] for item in ranked] == ["news", "aggregator"]
        facets = build_facets(ranked, total_candidates=2, limit=2)
        assert facets["source_types"] == {"aggregator": 1, "news": 1}


class TestPrimarySourcesLead:
    QUERY = "openai gpt-6 release news this week"

    def _pool(self) -> list[DiscoveryCandidate]:
        words = "OpenAI released GPT-6 this week, the company said."
        return [
            _candidate(
                "OpenAI released GPT-6 this week",
                "https://blogger.example/2026/10/22/openai-released-gpt-6",
                words,
                base=0,
            ),
            _candidate(
                "OpenAI releases GPT-6", "https://openai.com/index/gpt-6-release/", words, base=1
            ),
            _candidate(
                "OpenAI released GPT-6 this week",
                "https://www.reuters.com/technology/openai-released-gpt-6-2026-10-22/",
                words,
                base=2,
            ),
        ]

    def test_in_a_news_query_the_organization_and_the_outlets_lead_by_default(self) -> None:
        ranked = rank_candidates(self.QUERY, self._pool(), 3, today=TODAY)

        assert [item["source_type"] for item in ranked[:2]] == ["official", "news"] or {
            item["source_type"] for item in ranked[:2]
        } == {"official", "news"}
        assert ranked[-1]["domain"] == "blogger.example"

    def test_the_bonus_is_what_primary_first_adds(self) -> None:
        with_it = {
            item["source_type"]: item["score"]
            for item in annotate_candidates(
                self.QUERY, self._pool(), today=TODAY, primary_first=True
            )
        }
        without = {
            item["source_type"]: item["score"]
            for item in annotate_candidates(
                self.QUERY, self._pool(), today=TODAY, primary_first=False
            )
        }

        assert with_it["official"] - without["official"] == pytest.approx(0.12)
        assert with_it["news"] - without["news"] == pytest.approx(0.10)
        assert with_it["other"] == without["other"]

    def test_it_is_on_for_a_news_query_and_off_for_one_that_is_not(self) -> None:
        news = {
            i["source_type"]: i["score"]
            for i in annotate_candidates(self.QUERY, self._pool(), today=TODAY)
        }
        news_off = {
            i["source_type"]: i["score"]
            for i in annotate_candidates(self.QUERY, self._pool(), today=TODAY, primary_first=False)
        }
        plain_query = "openai gpt-6 release"
        plain = {
            i["source_type"]: i["score"]
            for i in annotate_candidates(plain_query, self._pool(), today=TODAY)
        }
        plain_on = {
            i["source_type"]: i["score"]
            for i in annotate_candidates(plain_query, self._pool(), today=TODAY, primary_first=True)
        }

        assert news["official"] > news_off["official"]
        assert plain_on["official"] - plain["official"] == pytest.approx(0.12)

    def test_a_result_too_old_for_the_time_the_query_asks_about_gets_no_bonus(self) -> None:
        stale = _candidate(
            "OpenAI released GPT-4",
            "https://www.reuters.com/technology/openai-released-gpt-4-2026-02-01/",
            "OpenAI released GPT-4 this week, the company said.",
            base=0,
        )

        on = annotate_candidates(self.QUERY, [stale], today=TODAY, primary_first=True)[0]
        off = annotate_candidates(self.QUERY, [stale], today=TODAY, primary_first=False)[0]

        assert on["score"] == off["score"] and on["freshness"] == "this_year"


class TestTheDealOutranksThePageAboutOneOfThem:
    """The "OpenAI Google Cloud GPT-6 Astra" case: a model page ranked over the deal."""

    QUERY = "OpenAI Google Cloud GPT-6 Astra"

    def _pool(self) -> list[DiscoveryCandidate]:
        return [
            # About OpenAI and the model, found by two angles, first for the provider.
            _candidate(
                "Introducing GPT-6 Astra | OpenAI",
                "https://openai.com/index/gpt-6-astra/",
                "GPT-6 Astra is OpenAI's newest model, with a longer context and better tools.",
                base=0,
                primary=0,
            ),
            # About Google Cloud and the model.
            _candidate(
                "Gemini and Astra on Google Cloud | Google Cloud",
                "https://cloud.google.com/blog/products/ai-machine-learning/astra-on-google-cloud",
                "Run Astra workloads on Google Cloud with Vertex AI.",
                base=1,
                primary=1,
            ),
            # Both organizations and the event.
            _candidate(
                "OpenAI to sell GPT-6 Astra through Google Cloud in latest enterprise push",
                "https://www.reuters.com/business/retail-consumer/openai-sell-gpt-6-astra-google-cloud-2026-10-21/",
                "OpenAI will sell its GPT-6 Astra model to enterprise customers through Google Cloud.",
                base=3,
            ),
            # An aggregator's copy of the same words.
            _candidate(
                "OpenAI to sell GPT-6 Astra through Google Cloud - Ground News",
                "https://ground.news/article/openai-to-sell-gpt-6-astra-through-google-cloud",
                "OpenAI will sell GPT-6 Astra through Google Cloud.",
                base=2,
            ),
        ]

    @pytest.mark.parametrize("query", [QUERY, QUERY + " news this week"])
    def test_the_result_with_both_organizations_and_the_event_comes_first(self, query: str) -> None:
        ranked = rank_candidates(query, self._pool(), 4, today=TODAY)

        assert ranked[0]["domain"] == "reuters.com"
        assert [item["domain"] for item in ranked].index("ground.news") == 3

    def test_both_pages_about_one_organization_are_official_and_still_come_after_the_deal(
        self,
    ) -> None:
        ranked = {
            item["domain"]: item
            for item in annotate_candidates(self.QUERY, self._pool(), today=TODAY)
        }

        assert ranked["openai.com"]["source_type"] == "official"
        assert ranked["cloud.google.com"]["source_type"] == "official"
        assert ranked["reuters.com"]["score"] > ranked["openai.com"]["score"]
        assert ranked["reuters.com"]["score"] > ranked["cloud.google.com"]["score"]
        assert ranked["ground.news"]["source_type"] == "aggregator"

    def test_the_pages_about_one_organization_were_ahead_before_this_round(self) -> None:
        # What the same pool does when the query names no second organization: the official
        # pages keep their weight, as they do for the documentation of one project.
        ranked = rank_candidates("OpenAI GPT-6 Astra", self._pool(), 4, today=TODAY)

        assert ranked[0]["domain"] in {"openai.com", "reuters.com"}

    def test_only_a_query_that_names_two_organizations_counts_what_a_result_holds_of_it(
        self,
    ) -> None:
        from web_scraper.web_tools.discovery import _QueryFacts

        def counts(query: str) -> bool:
            return _QueryFacts.of(query, False, None, DEFAULT_RULES).by_coverage

        assert counts("OpenAI Google Cloud GPT-6 Astra")
        assert counts("anthropic openai compute deal")
        # One organization, one project's documentation, or a name that is only a word: the
        # official pages keep the weight they always had.
        assert not counts("OpenAI GPT-6 Astra release")
        assert not counts("python httpx async client timeout")
        assert not counts("react useEffect cleanup patterns")
        assert not counts("Google Cloud")

    async def test_the_service_ranks_the_same_way(self) -> None:
        results = [
            SearchResult(item.result.title, item.result.url, item.result.snippet)
            for item in self._pool()
        ]
        service = _service(
            FakeFetcher({}),
            search=FakeSearch(results),
            settings=WebToolsSettings(
                search_provider="fake",
                search_angle_delay_ms=0,
                search_jitter_min_ms=0,
                search_jitter_max_ms=0,
                search_fallback_providers=[],
                search_resolve_dates="off",
            ),
        )

        answer = await service.search(self.QUERY, depth="fast", limit=4)
        answer = await service.search(self.QUERY, limit=4)

        assert answer["results"][0]["domain"] == "reuters.com"
        assert answer["results"][-1]["source_type"] == "aggregator"
        assert answer["facets"]["source_types"]["aggregator"] == 1

    def test_the_request_takes_primary_first(self) -> None:
        assert (
            WebSearchRequest.model_validate({"query": "x", "primary_first": False}).primary_first
            is False
        )
        assert WebSearchRequest.model_validate({"query": "x"}).primary_first is None
