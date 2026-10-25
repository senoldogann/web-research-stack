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


def _result(name: str) -> dict:
    return {"title": name, "url": f"https://example.com/{name}", "snippet": "", "source": "x"}


@pytest.mark.asyncio
async def test_default_backend_is_the_windowless_camoufox(monkeypatch) -> None:
    searcher = GoogleSearcher()
    chrome_used = []

    async def no_results(query: str, num_results: int):
        return []

    async def camoufox_results(query: str, num_results: int):
        return [_result("camoufox")]

    async def chrome_results(query: str, num_results: int):
        chrome_used.append(True)
        return []

    monkeypatch.setattr(config, "google_allow_visible_browser", False)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", camoufox_results)
    monkeypatch.setattr(searcher, "_search_chrome", chrome_results)

    results = await searcher.search("anything")

    assert [result["url"] for result in results] == ["https://example.com/camoufox"]
    assert chrome_used == []


@pytest.mark.asyncio
async def test_headless_chrome_is_the_last_resort_when_a_window_is_forbidden(
    monkeypatch,
) -> None:
    searcher = GoogleSearcher()

    async def no_results(query: str, num_results: int):
        return []

    async def headless_results(query: str, num_results: int):
        return [_result("headless")]

    monkeypatch.setattr(config, "google_allow_visible_browser", False)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", no_results)
    monkeypatch.setattr(searcher, "_search_headless_chrome", headless_results)

    results = await searcher.search("anything")

    assert [result["url"] for result in results] == ["https://example.com/headless"]


@pytest.mark.asyncio
async def test_a_window_is_never_opened_without_permission(monkeypatch) -> None:
    """With windows forbidden, the visible Chrome path must not run at all."""
    searcher = GoogleSearcher()
    visible = []

    async def no_results(query: str, num_results: int):
        return []

    async def blocked(query: str, num_results: int):
        raise GoogleSearchBlocked("refused")

    async def chrome_results(query: str, num_results: int):
        visible.append(True)
        return [_result("visible")]

    monkeypatch.setattr(config, "google_allow_visible_browser", False)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", blocked)
    monkeypatch.setattr(searcher, "_search_headless_chrome", blocked)
    monkeypatch.setattr(searcher, "_search_chrome", chrome_results)

    with pytest.raises(GoogleSearchBlocked):
        await searcher.search("anything")

    assert visible == []


@pytest.mark.asyncio
async def test_visible_chrome_is_used_only_when_allowed(monkeypatch) -> None:
    searcher = GoogleSearcher()
    camoufox_used = []

    async def no_results(query: str, num_results: int):
        return []

    async def chrome_results(query: str, num_results: int):
        return [_result("chrome")]

    async def camoufox_results(query: str, num_results: int):
        camoufox_used.append(True)
        return []

    monkeypatch.setattr(config, "google_allow_visible_browser", True)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_chrome", chrome_results)
    monkeypatch.setattr(searcher, "_search_camoufox", camoufox_results)

    results = await searcher.search("anything")

    assert [result["url"] for result in results] == ["https://example.com/chrome"]
    assert camoufox_used == []


@pytest.mark.asyncio
async def test_a_blocked_camoufox_falls_through_to_headless_chrome(monkeypatch) -> None:
    searcher = GoogleSearcher()

    async def no_results(query: str, num_results: int):
        return []

    async def camoufox_blocked(query: str, num_results: int):
        raise GoogleSearchBlocked("camoufox was refused")

    async def headless_results(query: str, num_results: int):
        return [_result("headless")]

    monkeypatch.setattr(config, "google_allow_visible_browser", False)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", camoufox_blocked)
    monkeypatch.setattr(searcher, "_search_headless_chrome", headless_results)

    results = await searcher.search("anything")

    assert [result["url"] for result in results] == ["https://example.com/headless"]


@pytest.mark.asyncio
async def test_search_raises_when_every_backend_is_blocked(monkeypatch) -> None:
    searcher = GoogleSearcher()

    async def no_results(query: str, num_results: int):
        return []

    monkeypatch.setattr(config, "google_allow_visible_browser", False)
    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", no_results)
    monkeypatch.setattr(searcher, "_search_headless_chrome", no_results)

    with pytest.raises(GoogleSearchBlocked):
        await searcher.search("anything")


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


@pytest.mark.parametrize(
    "link",
    [
        "https://www.google.com/goto?url=https%3A%2F%2Fexample.com%2Fa",
        "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fa&sa=U",
        "http://google.com/url?q=https%3A%2F%2Fexample.com%2Fa",
        "https://news.google.com/url?q=https%3A%2F%2Fexample.com%2Fa",
    ],
)
def test_googles_own_redirect_wrapper_is_followed(link: str) -> None:
    assert GoogleSearcher._is_google_wrapper(link) is True


@pytest.mark.parametrize(
    "link",
    [
        # A result's own address that happens to hold "/url?" or "google.com/goto" is a page on another host:
        # requesting it would go wherever a stranger's page points, a local server included.
        "http://127.0.0.1:8080/url?q=1",
        "http://169.254.169.254/latest/url?x=1",
        "https://example.com/url?q=https%3A%2F%2Fexample.com%2Fa",
        "https://example.com/google.com/goto?url=x",
        "https://evil-google.com/url?q=1",
        "https://google.com.evil.example/url?q=1",
        "https://www.google.com/search?q=url%3F",
        "https://www.google.com/",
        "ftp://www.google.com/url?q=1",
        "/url?q=https%3A%2F%2Fexample.com%2Fa",
        "",
    ],
)
def test_no_other_link_is_requested_to_resolve_it(link: str) -> None:
    assert GoogleSearcher._is_google_wrapper(link) is False


@pytest.mark.asyncio
async def test_only_googles_wrapper_links_are_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    class Response:
        headers = {"location": "https://example.com/real"}

    class Session:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, link: str, **kwargs: object) -> Response:
            requested.append(link)
            return Response()

    monkeypatch.setattr("web_scraper.google_search.cffi_requests.AsyncSession", Session)
    wrapper = "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Freal"
    stranger = "http://127.0.0.1:8080/url?q=1"

    resolved = await GoogleSearcher()._resolve_links(
        [wrapper, stranger, "https://example.com/plain"]
    )

    assert requested == [wrapper]
    assert resolved == ["https://example.com/real", stranger, "https://example.com/plain"]


@pytest.mark.parametrize("headless", [True, False])
def test_a_chrome_the_search_starts_never_reaches_the_owner_s_keychain(
    monkeypatch: pytest.MonkeyPatch, headless: bool
) -> None:
    # A throwaway profile's cookie key went to the login keychain ("Chrome Safe Storage"); where none was found,
    # macOS asked the owner on screen ("Keychain Not Found"), next to a button that resets their keychains.
    import web_scraper.google_search as google_module

    started: list[list[str]] = []

    def popen(args: list[str], **kwargs: object) -> object:
        started.append(args)
        return object()

    monkeypatch.setattr(GoogleSearcher, "_chrome_binary", lambda self: "/Applications/Chrome")
    monkeypatch.setattr(google_module.subprocess, "Popen", popen)

    GoogleSearcher()._start_chrome(9222, "/tmp/profile", headless=headless)

    assert "--use-mock-keychain" in started[0] and "--password-store=basic" in started[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("seen", ["nothing", "a bot check"])
async def test_an_empty_answer_is_a_wall_only_when_a_backend_saw_one(
    monkeypatch, seen: str
) -> None:
    # Every empty answer said "bot check", and the adapter cooled Google down for it (see
    # tests/test_web_search_chain.py, TestGoogleIsAWallOnlyWhenItSaysSo).
    from web_scraper.web_tools.errors import WebToolError
    from web_scraper.web_tools.providers import ERROR, WALL, failure_kind
    from web_scraper.web_tools.search import GoogleSearchAdapter

    searcher = GoogleSearcher(headless=True, allow_visible_browser=False)

    async def no_results(query: str, num_results: int):
        return []

    async def camoufox(query: str, num_results: int):
        if seen == "a bot check":
            raise GoogleSearchBlocked("Google served a bot check to Camoufox", wall=True)
        return []

    monkeypatch.setattr(searcher, "_search_http", no_results)
    monkeypatch.setattr(searcher, "_search_camoufox", camoufox)
    monkeypatch.setattr(searcher, "_search_headless_chrome", no_results)

    with pytest.raises(GoogleSearchBlocked) as refused:
        await searcher.search("anything")
    with pytest.raises(WebToolError) as failed:
        await GoogleSearchAdapter(searcher).search("anything", 5)

    walled = seen == "a bot check"
    assert refused.value.wall is walled
    assert ("bot check" in str(refused.value)) is walled
    assert failed.value.detail == ("bot_detected" if walled else "no_results")
    assert failure_kind(failed.value) == (WALL if walled else ERROR)
