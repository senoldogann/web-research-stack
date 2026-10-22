"""Regression tests for challenge detection: DOM evidence, not words in scripts."""

from __future__ import annotations

import httpx
import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.browser import BrowserPage, BrowserRenderError
from web_scraper.web_tools.content import detect_interstitial
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, FetchResult, SafeFetcher
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

URL = "https://example.com/page"
ARTICLE = "<p>" + ("Oulu is a city on the coast of the Gulf of Bothnia. " * 8) + "</p>"


def _allow(url: str, allow_private_networks: bool = False) -> str:
    return url


def _large_page(*, extra_head: str = "", extra_body: str = "", size: int = 200_000) -> str:
    """A content page past the challenge size limit, like Wikipedia or GitHub."""
    page = (
        "<html><head><title>Oulu - Wikipedia</title>"
        f"{extra_head}</head><body><main><h1>Oulu</h1>{ARTICLE}{extra_body}"
    )
    filler = "<p>" + ("More about the history and culture of the region. " * 20) + "</p>"
    while len(page) < size:
        page += filler
    return page + "</main></body></html>"


def _small_page(body: str, title: str = "One moment") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


class TestCaptchaNeedsDomEvidence:
    def test_large_page_naming_captcha_only_in_scripts_is_content(self) -> None:
        html = _large_page(
            extra_head=(
                '<script>var config = {"captchaSiteKey": "x", "recaptcha": true,'
                ' "hcaptchaEnabled": false, "message": "verify you are human"};</script>'
                '<script src="https://www.google.com/recaptcha/api.js"></script>'
            )
        )

        assert len(html.encode()) >= 200_000
        assert detect_interstitial(html, URL, 200) is None

    def test_small_page_with_recaptcha_widget_is_captcha(self) -> None:
        html = _small_page('<form action="/verify"><div class="g-recaptcha"></div></form>')

        assert detect_interstitial(html, URL, 200) == "captcha"

    def test_small_page_with_captcha_word_only_in_script_is_content(self) -> None:
        html = _small_page(
            ARTICLE + '<script>window.captcha = "unusual traffic verify you are human";</script>',
            title="Article",
        )

        assert detect_interstitial(html, URL, 200) is None

    def test_challenge_phrase_in_visible_text_is_captcha(self) -> None:
        html = _small_page("<p>Please verify you are human to continue.</p>")

        assert detect_interstitial(html, URL, 200) == "captcha"

    def test_large_successful_page_is_never_a_captcha(self) -> None:
        html = _large_page(extra_body='<div class="modal"><div class="g-recaptcha"></div></div>')

        assert detect_interstitial(html, URL, 200) is None

    def test_large_page_with_a_large_captcha_form_and_little_text_is_captcha(self) -> None:
        # Reddit's "Prove your humanity" page: 200 OK, ~160 KB of inline assets
        # around one form that posts back to ?captcha=1.
        styles = "<style>" + (".shreddit-x{color:red}" * 8_000) + "</style>"
        html = (
            f"<html><head><title>Reddit</title>{styles}</head><body>"
            "<h1>Prove your humanity</h1><p>Complete the challenge below.</p>"
            '<form method="POST" action="/r/programming/?captcha=1">'
            '<div class="g-recaptcha" data-sitekey="x"></div></form></body></html>'
        )

        assert len(html.encode()) > 100_000
        assert detect_interstitial(html, URL, 200) == "captcha"

    def test_large_content_page_with_a_captcha_form_is_content(self) -> None:
        html = _large_page(extra_body='<form action="/comment?captcha=1"></form>')

        assert detect_interstitial(html, URL, 200) is None

    def test_large_page_with_widget_is_still_judged_on_an_error_status(self) -> None:
        html = _large_page(extra_body='<div class="g-recaptcha"></div>')

        assert detect_interstitial(html, URL, 429) == "captcha"

    def test_page_with_much_visible_text_is_content_despite_a_widget(self) -> None:
        html = _small_page(
            ARTICLE * 8 + '<form action="/contact"><div class="h-captcha"></div></form>',
            title="Contact",
        )

        assert detect_interstitial(html, URL, 200) is None

    @pytest.mark.parametrize(
        "widget",
        [
            '<div class="grecaptcha-badge"><iframe src="https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible"></iframe></div>',
            '<div class="g-recaptcha" data-size="invisible"></div>',
        ],
    )
    def test_invisible_recaptcha_is_not_a_challenge(self, widget: str) -> None:
        html = _small_page(ARTICLE + widget, title="Shop")

        assert detect_interstitial(html, URL, 200) is None

    @pytest.mark.parametrize(
        "widget",
        [
            '<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></iframe>',
            '<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x"></iframe>',
            '<form method="get" action="/errors/validateCaptcha"><input name="field"></form>',
            '<div id="px-captcha"></div>',
            '<div class="cf-turnstile" data-sitekey="x"></div>',
        ],
    )
    def test_captcha_frames_forms_and_vendor_widgets_are_captcha(self, widget: str) -> None:
        assert detect_interstitial(_small_page(widget), URL, 200) == "captcha"

    def test_widget_inside_a_template_is_not_shown(self) -> None:
        html = _small_page(ARTICLE + '<template><div class="g-recaptcha"></div></template>')

        assert detect_interstitial(html, URL, 200) is None


class TestBlocksAndWalls:
    @pytest.mark.parametrize("status", [401, 403])
    def test_short_refusal_is_access_blocked(self, status: int) -> None:
        html = _small_page("<p>Forbidden</p>", title="Error")

        assert detect_interstitial(html, URL, status) == "access_blocked"

    def test_empty_403_is_access_blocked(self) -> None:
        assert detect_interstitial("", URL, 403) == "access_blocked"

    def test_json_403_is_an_api_error_not_a_block(self) -> None:
        assert detect_interstitial('{"error": "forbidden"}', URL, 403) is None

    def test_long_403_page_without_markers_stays_an_http_error(self) -> None:
        html = _small_page(ARTICLE * 60, title="Forbidden")

        assert len(html.encode()) > 20_000
        assert detect_interstitial(html, URL, 403) is None

    def test_long_article_about_access_denied_errors_is_content(self) -> None:
        html = _small_page(
            "<h1>Fixing access denied for user root</h1>" + ARTICLE * 8,
            title="MySQL access denied",
        )

        assert detect_interstitial(html, URL, 200) is None

    def test_block_phrase_inside_script_on_large_page_is_content(self) -> None:
        html = _large_page(extra_head='<script>var i18n = {"e": "Access denied"};</script>')

        assert detect_interstitial(html, URL, 200) is None

    def test_cloudflare_error_details_block_is_detected(self) -> None:
        html = _small_page('<div id="cf-error-details"><h1>Error 1020</h1></div>')

        assert detect_interstitial(html, URL, 403) == "access_blocked"

    def test_login_title_with_password_field_is_a_wall(self) -> None:
        html = _small_page(
            '<form><input type="text" name="u"><input type="password" name="p"></form>',
            title="Sign in",
        )

        assert detect_interstitial(html, "https://example.com/account", 200) == "access_wall"

    def test_login_title_with_password_only_in_scripts_is_not_a_wall(self) -> None:
        html = _small_page(
            ARTICLE
            + '<script>var labels = {"password": "Password", "reset": "password"};</script>',
            title="How to log in to your router",
        )

        assert detect_interstitial(html, "https://example.com/guides/router", 200) is None


@pytest.mark.asyncio
async def test_fetcher_returns_large_page_that_mentions_captcha_in_scripts() -> None:
    html = _large_page(extra_head='<script>var s = {"captcha": "g-recaptcha"};</script>')

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        result = await fetcher.fetch(URL)

    assert result.status_code == 200
    assert "Oulu" in result.html


@pytest.mark.asyncio
async def test_fetcher_classifies_short_403_as_access_blocked() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"content-type": "text/html"},
            text="<html><body><p>Please enable JS and disable any ad blocker</p></body></html>",
        )

    async with SafeFetcher(
        transport=httpx.MockTransport(handler),
        validator=_allow,
        retry_attempts=0,
    ) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch(URL)

    assert exc.value.code == "blocked"
    assert exc.value.detail == "access_blocked"


class _BlockedFetcher:
    async def fetch(self, url: str) -> FetchResult:
        raise FetchError(
            "blocked",
            "Target returned a access_blocked interstitial",
            url=url,
            status_code=403,
            detail="access_blocked",
        )


class _Browser:
    def __init__(self, outcome: BrowserPage | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    async def render(self, url: str, *, post_load_wait_ms: int | None = None) -> BrowserPage:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _blocked_service(browser: _Browser) -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(),
        fetcher=_BlockedFetcher(),
        browser=browser,  # type: ignore[arg-type]
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


@pytest.mark.asyncio
async def test_failed_browser_fallback_reports_the_http_refusal() -> None:
    browser = _Browser(BrowserRenderError("navigation_failed", "Browser navigation failed"))

    with pytest.raises(WebToolError) as exc:
        await _blocked_service(browser).scrape(URL)

    assert browser.calls == 1
    assert (exc.value.code, exc.value.detail, exc.value.status_code) == (
        "blocked",
        "access_blocked",
        403,
    )


@pytest.mark.asyncio
async def test_challenge_seen_by_the_browser_keeps_its_status() -> None:
    page = _small_page('<iframe src="https://geo.captcha-delivery.com/captcha/?x=1"></iframe>')
    browser = _Browser(BrowserPage(page, URL, 401, "chrome"))

    with pytest.raises(WebToolError) as exc:
        await _blocked_service(browser).scrape(URL)

    assert (exc.value.code, exc.value.detail, exc.value.status_code) == ("blocked", "captcha", 401)
