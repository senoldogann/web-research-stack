"""Markup that is not text is not in the Markdown: comments, CDATA sections, processing instructions.

senoldogan.dev's blog pages come back from the server as ``<div id="root"></div>`` and, before ``</body>``,
``<!-- Loading animation while page loads -->``. The Markdown reader printed that comment as the page's text,
so ``web_scrape`` answered "Loading animation while page loads" (34 characters) as if it were the page: nobody
who opens the page sees it. A comment is a ``NavigableString`` of the parser, and the reader took every one of
those for text. (The same went for a Google Tag Manager comment inside ``<main>``, or an IE conditional
comment, in the Markdown of any page that has one.)
"""

from __future__ import annotations

import pytest

from web_scraper.api_runtime import InMemoryTTLCache
from web_scraper.web_tools.content import extract_page, markdown_of, needs_browser_render
from web_scraper.web_tools.fetcher import FetchResult
from web_scraper.web_tools.metrics import LatencyTracker
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

BASE = "https://blog.example/post"
# The shell the owner's blog serves, as it comes over HTTP.
OWNERS_SHELL = (
    "<!DOCTYPE html><html><head><title>Websites in Oulu</title></head><body>"
    '<div id="root"></div><script src="/assets/app.js"></script>'
    "<!-- Loading animation while page loads --></body></html>"
)


class TestACommentIsNotPageText:
    def test_the_owners_shell_has_no_text_at_all(self) -> None:
        assert markdown_of(OWNERS_SHELL, BASE) == ""
        assert extract_page(OWNERS_SHELL, BASE).markdown == ""

    def test_and_so_it_is_read_again_in_the_browser_as_any_empty_page_with_scripts(self) -> None:
        assert needs_browser_render(OWNERS_SHELL, markdown_of(OWNERS_SHELL, BASE)) is True

    @pytest.mark.parametrize(
        "html",
        [
            "<html><body><main><!-- Google Tag Manager --><p>Real text.</p></main></body></html>",
            "<html><body><main><p>Real text.</p><!-- an end comment --></main></body></html>",
            "<html><body><main><p>Real <!-- inline -->text.</p></main></body></html>",
            "<html><body><main><p>Real text.</p>"
            "<!--[if lt IE 9]><p>Upgrade your browser</p><![endif]--></main></body></html>",
            "<html><body><main><p>Real text.</p><!-- <p>commented out</p> --></main></body></html>",
            "<html><body><main><p>Real text.</p><![CDATA[ not text ]]></main></body></html>",
            "<html><body><main><p>Real text.</p><?php echo 1 ?></main></body></html>",
            "<html><body><main><ul><li>Real text.<!-- item note --></li></ul></main></body></html>",
            "<html><body><main><table><tr><td>Real text.<!-- cell --></td></tr></table></main></body></html>",
            "<html><body><main><blockquote>Real text.<!-- quote --></blockquote></main></body></html>",
        ],
    )
    def test_a_comment_or_a_cdata_section_inside_the_page_is_not_in_its_markdown(
        self, html: str
    ) -> None:
        markdown = markdown_of(html, BASE)

        assert "Real" in markdown and "text." in markdown
        for markup in (
            "comment",
            "Tag Manager",
            "Upgrade",
            "commented out",
            "CDATA",
            "php",
            "note",
        ):
            assert markup not in markdown
        assert "inline" not in markdown and "cell" not in markdown and "quote" not in markdown

    def test_the_words_around_an_inline_comment_still_read_as_one_sentence(self) -> None:
        html = "<html><body><main><p>Real <!-- inline -->text.</p></main></body></html>"

        assert markdown_of(html, BASE) == "Real text."

    def test_visible_text_and_code_are_untouched(self) -> None:
        html = (
            "<html><body><main><h1>Title</h1><p>Some <b>bold</b> and <a href='/x'>a link</a>.</p>"
            "<pre><code class='language-py'>x = 1  # not an HTML comment\n</code></pre>"
            "<p>1 &lt; 2 &amp; <code>&lt;!-- shown as code --&gt;</code></p></main></body></html>"
        )

        markdown = markdown_of(html, BASE)

        assert "# Title" in markdown and "**bold**" in markdown
        assert "[a link](https://blog.example/x)" in markdown
        assert "x = 1  # not an HTML comment" in markdown
        assert "`<!-- shown as code -->`" in markdown  # text that spells a comment is text

    def test_a_page_with_only_comments_for_text_is_empty(self) -> None:
        html = "<html><body><main><!-- a --><!-- b --></main></body></html>"

        assert markdown_of(html, BASE) == ""


class _Fetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(url, url, 200, "text/html", self.pages[url], {}, 1.0, 2.0, 1)


def _service(pages: dict[str, str]) -> WebToolsService:
    return WebToolsService(
        settings=WebToolsSettings(respect_robots=False, soft_404_check=False),
        fetcher=_Fetcher(pages),
        browser=None,
        cache=InMemoryTTLCache(ttl_seconds=60, max_entries=10),
        latency=LatencyTracker(),
    )


class TestWhatTheServerAnswersForTheShell:
    @pytest.mark.asyncio
    async def test_without_a_browser_it_says_the_page_was_not_read_and_not_that_it_says_loading(
        self,
    ) -> None:
        answer = await _service({BASE: OWNERS_SHELL}).scrape(BASE)

        # (before: markdown "Loading animation while page loads", warnings [] and a confident short page)
        assert answer["markdown"] == ""
        assert answer["warnings"] == ["empty_content", "needs_javascript"]
        assert "no browser read it" in answer["hint"]

    @pytest.mark.asyncio
    async def test_a_batch_and_a_search_read_say_the_same(self) -> None:
        batch = await _service({BASE: OWNERS_SHELL, BASE + "2": OWNERS_SHELL}).scrape_many(
            [BASE, BASE + "2"]
        )

        assert [page["warnings"] for page in batch["pages"]] == [
            ["empty_content", "needs_javascript"]
        ] * 2
