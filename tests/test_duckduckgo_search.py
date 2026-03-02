"""Tests for DuckDuckGo search pagination helpers."""

from web_scraper.duckduckgo_search import DuckDuckGoSearcher


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
