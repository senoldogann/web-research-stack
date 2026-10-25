"""Tests for DuckDuckGo search pagination helpers."""

import pytest

from web_scraper.duckduckgo_search import DuckDuckGoBlocked, DuckDuckGoSearcher
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.search import DuckDuckGoSearchAdapter


def test_parse_results_extracts_next_page_payload() -> None:
    searcher = DuckDuckGoSearcher()
    html = """
    <html>
        <body>
            <div class="result">
                <a class="result__a" href="https://example.com/article">Example</a>
                <a class="result__snippet">Example snippet</a>
            </div>
            <div class="nav-link">
                <form action="/html/" method="post">
                    <input type="hidden" name="q" value="openai agents" />
                    <input type="hidden" name="s" value="10" />
                    <input type="hidden" name="nextParams" value="" />
                    <input type="hidden" name="v" value="l" />
                    <input type="hidden" name="o" value="json" />
                    <input type="hidden" name="dc" value="11" />
                    <input type="hidden" name="api" value="d.js" />
                    <input type="hidden" name="vqd" value="token-123" />
                    <input type="submit" value="Next" />
                </form>
            </div>
        </body>
    </html>
    """

    results, next_payload = searcher._parse_results(html)

    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/article"
    assert next_payload == {
        "q": "openai agents",
        "s": "10",
        "nextParams": "",
        "v": "l",
        "o": "json",
        "dc": "11",
        "api": "d.js",
        "vqd": "token-123",
    }


def test_parse_results_preserves_spacing_around_inline_tags() -> None:
    searcher = DuckDuckGoSearcher()
    html = """
    <div class="result">
        <a class="result__a" href="https://example.com/free-threading">Docs</a>
        <a class="result__snippet">Python <b>3.14</b> introduces free-threaded mode in <b>3.14t</b>.</a>
    </div>
    """

    results, _ = searcher._parse_results(html)

    assert results[0]["snippet"] == "Python 3.14 introduces free-threaded mode in 3.14t."


def test_the_date_shown_beside_a_result_s_address_is_its_published_date() -> None:
    # The html endpoint's markup, as a page it served shows it (2026-09): a span after the address, for some results.
    searcher = DuckDuckGoSearcher()
    html = """
    <div class="result results_links results_links_deep web-result ">
      <div class="links_main links_deep result__body">
        <h2 class="result__title"><a class="result__a" href="https://tokio.rs/">Tokio</a></h2>
        <div class="result__extras"><div class="result__extras__url">
          <span class="result__icon"></span>
          <a class="result__url" href="https://tokio.rs/">tokio.rs</a>
          <span>&nbsp; &nbsp; 2026-10-02T00:00:00.0000000</span>
        </div></div>
        <a class="result__snippet">An asynchronous runtime.</a>
      </div>
    </div>
    <div class="result results_links results_links_deep web-result ">
      <div class="links_main links_deep result__body">
        <h2 class="result__title"><a class="result__a" href="https://docs.rs/tokio">Docs</a></h2>
        <div class="result__extras"><div class="result__extras__url">
          <a class="result__url" href="https://docs.rs/tokio">docs.rs/tokio</a>
        </div></div>
        <a class="result__snippet">Released 2026-01-05T00:00:00.0000000 in its notes.</a>
      </div>
    </div>
    """

    results, _ = searcher._parse_results(html)

    assert [item["published"] for item in results] == [
        "2026-10-02",
        "",
    ]  # never one of the snippet's words


@pytest.mark.asyncio
async def test_adapter_reports_bot_detection_as_search_failure() -> None:
    class _BlockedSearcher:
        async def search(
            self,
            query: str,
            num_results: int = 10,
            date_filter: str | None = None,
        ) -> list[dict]:
            raise DuckDuckGoBlocked("bot check")

    adapter = DuckDuckGoSearchAdapter(_BlockedSearcher())

    with pytest.raises(WebToolError) as exc:
        await adapter.search("query", 5)

    assert exc.value.code == "search_failed"
    assert exc.value.detail == "bot_detected"


def test_params_use_configured_locale_and_cap_provider_query_length() -> None:
    searcher = DuckDuckGoSearcher(region="tr-tr", language="tr-TR,tr;q=0.9")

    params = searcher._build_params("x" * 600, "year", vqd="token-123")

    assert params["q"] == "x" * 499
    assert params["kl"] == "tr-tr"
    assert params["df"] == "y"
    assert params["vqd"] == "token-123"
    assert searcher._build_headers()["accept-language"] == "tr-TR,tr;q=0.9"


@pytest.mark.asyncio
async def test_adapter_forwards_date_filter_to_searcher() -> None:
    class _RecordingSearcher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, str | None]] = []

        async def search(
            self,
            query: str,
            num_results: int = 10,
            date_filter: str | None = None,
        ) -> list[dict]:
            self.calls.append((query, num_results, date_filter))
            return []

    searcher = _RecordingSearcher()
    adapter = DuckDuckGoSearchAdapter(searcher)

    await adapter.search("query", 5, date_filter="month")

    assert searcher.calls == [("query", 5, "month")]
