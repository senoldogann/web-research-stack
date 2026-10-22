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


@pytest.mark.asyncio
async def test_adapter_reports_bot_detection_as_search_failure() -> None:
    class _BlockedSearcher:
        async def search(self, query: str, num_results: int = 10) -> list[dict]:
            raise DuckDuckGoBlocked("bot check")

    adapter = DuckDuckGoSearchAdapter(_BlockedSearcher())

    with pytest.raises(WebToolError) as exc:
        await adapter.search("query", 5)

    assert exc.value.code == "search_failed"
    assert exc.value.detail == "bot_detected"
