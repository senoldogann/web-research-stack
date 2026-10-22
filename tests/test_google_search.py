"""Tests for the Google search parser and its fallback chain.

Google stopped serving results to plain HTTP clients, so the searcher has three
jobs worth holding still: read the classic markup, read the modern markup, and
say "blocked" instead of returning an empty list that looks like "no results".
"""

import pytest

from web_scraper.config import config
from web_scraper.google_search import GoogleSearchBlocked, GoogleSearcher, get_best_sources

LEGACY_HTML = """
<html><body><div id="search">
  <div class="g">
    <a href="/url?q=https%3A%2F%2Fexample.com%2Flegacy&amp;sa=U">
      <h3>Legacy result</h3>
    </a>
    <div class="VwiC3b">A legacy snippet.</div>
  </div>
</div></body></html>
"""

MODERN_HTML = """
<html><body><div id="rso">
  <div class="MjjYud">
    <a href="https://example.com/modern"><h3>Modern result</h3></a>
    <div class="IsZvec">A modern snippet.</div>
  </div>
</div></body></html>
"""

BARE_HTML = """
<html><body><div id="rso">
  <div>
    <a href="https://example.com/bare"><h3>Bare result</h3></a>
    <div class="VwiC3b">A bare snippet.</div>
  </div>
</div></body></html>
"""

BLOCK_HTML = """
<html><body><h1>Our systems have detected unusual traffic</h1>
<form id="captcha-form"></form></body></html>
"""


def test_parses_legacy_container_markup() -> None:
    results = GoogleSearcher()._parse_results(LEGACY_HTML)

    assert len(results) == 1
    assert results[0]["title"] == "Legacy result"
    assert results[0]["url"] == "https://example.com/legacy"
    assert results[0]["snippet"] == "A legacy snippet."


def test_parses_modern_container_markup() -> None:
    results = GoogleSearcher()._parse_results(MODERN_HTML)

    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/modern"
    assert results[0]["snippet"] == "A modern snippet."


def test_parses_markup_without_known_classes() -> None:
    """A class rename must not empty the answer: the h3 inside the anchor is enough."""
    results = GoogleSearcher()._parse_results(BARE_HTML)

    assert len(results) == 1
    assert results[0]["title"] == "Bare result"
    assert results[0]["url"] == "https://example.com/bare"


def test_clean_url_decodes_google_redirect() -> None:
    url = GoogleSearcher._clean_url("/url?q=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&sa=U")

    assert url == "https://example.com/a?b=1"


def test_clean_url_drops_google_internal_links() -> None:
    assert GoogleSearcher._clean_url("/search?q=test") == ""
    assert GoogleSearcher._clean_url("https://www.google.com/preferences") == ""


def test_clean_url_keeps_plain_links() -> None:
    assert GoogleSearcher._clean_url("https://example.com/page") == "https://example.com/page"


@pytest.mark.asyncio
async def test_search_raises_when_every_path_is_blocked(monkeypatch) -> None:
    searcher = GoogleSearcher()

    async def no_results(query: str, num_results: int):
        return []

    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(config, "google_use_playwright", False)

    with pytest.raises(GoogleSearchBlocked):
        await searcher.search("anything")


@pytest.mark.asyncio
async def test_search_falls_back_to_the_browser_when_allowed(monkeypatch) -> None:
    searcher = GoogleSearcher()

    async def no_results(query: str, num_results: int):
        return []

    async def browser_results(query: str, num_results: int):
        return [
            {
                "title": "From the browser",
                "url": "https://example.com/browser",
                "snippet": "rendered",
                "source": "example",
            }
        ]

    monkeypatch.setattr(config, "google_allow_visible_browser", True)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", no_results)
    monkeypatch.setattr(searcher, "_search_browser", browser_results)

    results = await searcher.search("anything")

    assert [result["url"] for result in results] == ["https://example.com/browser"]


@pytest.mark.asyncio
async def test_no_visible_browser_unless_it_is_allowed(monkeypatch) -> None:
    """A visible Chrome window must never open on its own."""
    searcher = GoogleSearcher()
    opened = []

    async def no_results(query: str, num_results: int):
        return []

    async def browser(*args):
        opened.append(True)
        return []

    monkeypatch.setattr(config, "google_allow_visible_browser", False)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", no_results)
    monkeypatch.setattr(searcher, "_search_browser", browser)

    with pytest.raises(GoogleSearchBlocked):
        await searcher.search("anything")

    assert opened == []


@pytest.mark.asyncio
async def test_headless_camoufox_comes_before_the_visible_browser(monkeypatch) -> None:
    searcher = GoogleSearcher()
    opened = []

    async def no_results(query: str, num_results: int):
        return []

    async def camoufox_results(query: str, num_results: int):
        return [{"title": "headless", "url": "https://example.com/h", "snippet": "", "source": "x"}]

    async def browser(*args):
        opened.append(True)
        return []

    monkeypatch.setattr(config, "google_allow_visible_browser", True)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", camoufox_results)
    monkeypatch.setattr(searcher, "_search_browser", browser)

    results = await searcher.search("anything")

    assert [result["url"] for result in results] == ["https://example.com/h"]
    assert opened == []


@pytest.mark.asyncio
async def test_get_best_sources_filters_google_links(monkeypatch) -> None:
    async def fake_search(self, query: str, num_results: int = 10):
        return [
            {"title": "keep", "url": "https://example.com/keep", "snippet": "", "source": "x"},
            {
                "title": "drop",
                "url": "https://www.google.com/preferences",
                "snippet": "",
                "source": "x",
            },
        ]

    monkeypatch.setattr(GoogleSearcher, "search", fake_search)

    results = await get_best_sources("anything", max_sources=5)

    assert [result["url"] for result in results] == ["https://example.com/keep"]
