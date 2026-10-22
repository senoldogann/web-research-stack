"""Tests for the deterministic building blocks of the web research tools."""

from __future__ import annotations

import pytest
from defusedxml.common import EntitiesForbidden

from web_scraper.web_tools.content import (
    detect_interstitial,
    extract_page,
    extract_structured,
    html_to_markdown,
    needs_browser_render,
    parse_sitemap_xml,
)
from web_scraper.web_tools.urls import (
    UnsafeUrlError,
    canonicalize_url,
    content_hash,
    matches_url_filters,
    url_in_scope,
)


class TestCanonicalUrl:
    def test_lowercases_host_and_drops_fragment_and_default_port(self) -> None:
        url = "HTTPS://Example.COM:443/Docs/Guide/#intro"

        assert canonicalize_url(url) == "https://example.com/Docs/Guide"

    def test_removes_tracking_parameters_and_sorts_the_rest(self) -> None:
        url = "https://example.com/a?b=2&utm_source=x&a=1&fbclid=abc"

        assert canonicalize_url(url) == "https://example.com/a?a=1&b=2"

    def test_keeps_meaningful_query_parameters_distinct(self) -> None:
        first = canonicalize_url("https://example.com/search?q=alpha")
        second = canonicalize_url("https://example.com/search?q=beta")

        assert first != second

    def test_normalises_dot_segments_and_root_trailing_slash(self) -> None:
        assert canonicalize_url("https://example.com/a/./b/../c/") == "https://example.com/a/c"
        assert canonicalize_url("https://example.com/") == "https://example.com/"

    def test_rejects_credentials_and_unsupported_schemes(self) -> None:
        with pytest.raises(UnsafeUrlError):
            canonicalize_url("https://user:pass@example.com/")
        with pytest.raises(UnsafeUrlError):
            canonicalize_url("file:///etc/passwd")
        with pytest.raises(UnsafeUrlError):
            canonicalize_url("javascript:alert(1)")

    def test_rejects_localhost_literal_without_dns(self) -> None:
        with pytest.raises(UnsafeUrlError):
            canonicalize_url("http://localhost:8080/admin")
        with pytest.raises(UnsafeUrlError):
            canonicalize_url("http://127.0.0.1/admin")


class TestScopeAndFilters:
    def test_same_host_scope_rejects_other_hosts_and_lookalikes(self) -> None:
        base = "https://docs.example.com/start"

        assert url_in_scope("https://docs.example.com/next", base, include_subdomains=False)
        assert not url_in_scope("https://blog.example.com/post", base, include_subdomains=False)
        assert not url_in_scope(
            "https://docs.example.com.evil.test/x", base, include_subdomains=True
        )

    def test_subdomain_scope_accepts_real_subdomains_only(self) -> None:
        base = "https://example.com/"

        assert url_in_scope("https://docs.example.com/page", base, include_subdomains=True)
        assert not url_in_scope("https://example.com.evil.test/page", base, include_subdomains=True)
        assert not url_in_scope("https://notexample.com/page", base, include_subdomains=True)

    def test_include_and_exclude_globs(self) -> None:
        assert matches_url_filters(
            "https://example.com/docs/a",
            include_patterns=["*/docs/*"],
            exclude_patterns=["*/private/*"],
        )
        assert not matches_url_filters(
            "https://example.com/private/a",
            include_patterns=["*"],
            exclude_patterns=["*/private/*"],
        )
        assert not matches_url_filters(
            "https://example.com/blog/a",
            include_patterns=["*/docs/*"],
            exclude_patterns=[],
        )


class TestContentHash:
    def test_hash_ignores_insignificant_whitespace(self) -> None:
        assert content_hash("# Title\n\nBody") == content_hash("# Title\n\n\n  Body  ")

    def test_hash_changes_when_content_changes(self) -> None:
        assert content_hash("alpha") != content_hash("beta")


class TestHtmlToMarkdown:
    HTML = """
    <html><head><title>Example title</title>
      <meta name="description" content="A description">
      <link rel="canonical" href="/canonical">
    </head><body>
      <nav>Menu <a href="/nav">Nav</a></nav>
      <main>
        <h1>Heading</h1>
        <p>Hello <strong>world</strong> and <a href="/docs">docs</a>.</p>
        <ul><li>One</li><li>Two</li></ul>
        <pre><code class="language-python">print("hi")</code></pre>
        <table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>
        <script>alert('not content')</script>
      </main>
    </body></html>
    """

    def test_converts_core_markdown_without_chrome_or_scripts(self) -> None:
        markdown = html_to_markdown(self.HTML, "https://example.com/article")

        assert "# Heading" in markdown
        assert "**world**" in markdown
        assert "[docs](https://example.com/docs)" in markdown
        assert "- One" in markdown
        assert '```python\nprint("hi")\n```' in markdown
        assert "| Name | Value |" in markdown
        assert "not content" not in markdown
        assert "Menu" not in markdown

    def test_extracts_title_description_canonical_and_links(self) -> None:
        page = extract_page(self.HTML, "https://example.com/article")

        assert page.title == "Example title"
        assert page.description == "A description"
        assert page.canonical_url == "https://example.com/canonical"
        assert page.markdown.startswith("# Heading")
        assert {link.url for link in page.links} >= {
            "https://example.com/docs",
            "https://example.com/nav",
        }
        assert page.metadata["description"] == "A description"

    def test_extracts_json_ld(self) -> None:
        html = """
        <html><head><script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[
          {"@type":"Article","headline":"LD title","datePublished":"2026-01-02"},
          {"@type":"Organization","name":"Example Org"}
        ]}
        </script></head><body><p>Body text</p></body></html>
        """

        page = extract_page(html, "https://example.com/a")

        assert page.metadata["json_ld"][0]["headline"] == "LD title"
        assert any(item.get("name") == "Example Org" for item in page.metadata["json_ld"])

    def test_limits_markdown_length(self) -> None:
        markdown = html_to_markdown(
            "<p>" + ("x" * 500) + "</p>", "https://example.com/", max_chars=80
        )

        assert len(markdown) <= 80


class TestRenderAndChallengeDetection:
    def test_script_shell_with_little_text_needs_a_browser(self) -> None:
        html = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'

        assert needs_browser_render(html, "Loading", min_chars=200) is True

    def test_document_write_shell_with_boilerplate_text_needs_a_browser(self) -> None:
        boilerplate = "Home Login Next " * 12
        html = (
            f"<html><body><nav>{boilerplate}</nav>"
            '<script>document.write("<div class=quote>Rendered later</div>");</script>'
            "</body></html>"
        )

        assert needs_browser_render(html, boilerplate, min_chars=200) is True

    def test_rich_static_page_does_not_need_a_browser(self) -> None:
        html = "<html><body><article>" + ("<p>Useful text.</p>" * 40) + "</article></body></html>"
        markdown = "Useful text. " * 40

        assert needs_browser_render(html, markdown, min_chars=200) is False

    def test_short_static_page_without_scripts_does_not_need_a_browser(self) -> None:
        assert (
            needs_browser_render("<html><body><p>Short note</p></body></html>", "Short note")
            is False
        )

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            (
                "<html><body>Our systems have detected unusual traffic from your network.</body></html>",
                "captcha",
            ),
            ("<html><body><div class='g-recaptcha'></div></body></html>", "captcha"),
            ("<html><body><h1>Access Denied</h1></body></html>", "access_blocked"),
            ("<html><body>Just a moment... checking your browser</body></html>", "access_blocked"),
        ],
    )
    def test_detects_bot_challenges(self, html: str, expected: str) -> None:
        assert detect_interstitial(html, "https://example.com/a") == expected

    def test_detects_login_wall(self) -> None:
        html = "<html><head><title>Sign in</title></head><body><p>Sign in to continue</p></body></html>"

        assert detect_interstitial(html, "https://example.com/login?next=/a") == "access_wall"

    def test_normal_article_is_not_a_challenge(self) -> None:
        html = "<html><head><title>Docs</title></head><body><p>Sign in to your account later.</p></body></html>"

        assert detect_interstitial(html, "https://example.com/docs") is None


class TestSitemapParsing:
    def test_parses_urlset(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/a</loc><lastmod>2026-01-01</lastmod></url>
          <url><loc>https://example.com/b</loc></url>
        </urlset>"""

        kind, entries = parse_sitemap_xml(xml)

        assert kind == "urlset"
        assert [entry["url"] for entry in entries] == [
            "https://example.com/a",
            "https://example.com/b",
        ]
        assert entries[0]["lastmod"] == "2026-01-01"

    def test_parses_sitemap_index(self) -> None:
        xml = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
        </sitemapindex>"""

        kind, entries = parse_sitemap_xml(xml)

        assert kind == "sitemapindex"
        assert entries[0]["url"] == "https://example.com/sitemap-1.xml"

    def test_rejects_entity_expansion(self) -> None:
        xml = b"""<?xml version="1.0"?>
        <!DOCTYPE lolz [<!ENTITY lol "lol">]>
        <urlset><url><loc>&lol;</loc></url></urlset>"""

        with pytest.raises(EntitiesForbidden):
            parse_sitemap_xml(xml)


class TestStructuredExtraction:
    HTML = """
    <html><head>
      <meta property="og:title" content="OG title">
      <script type="application/ld+json">
      {"@type":"Product","name":"Widget","offers":{"price":"19.99"}}
      </script>
    </head><body>
      <h1>Visible title</h1>
      <div class="price">$1,299.00</div>
      <span class="author">Ada Lovelace</span>
      <a class="doc" href="/one">One</a>
      <a class="doc" href="/two">Two</a>
    </body></html>
    """

    def test_extracts_selector_fields_with_provenance(self) -> None:
        schema = {
            "title": {"selector": "h1", "required": True},
            "author": {"selector": ".author"},
            "price": {"selector": ".price", "type": "number"},
            "documents": {"selector": "a.doc", "attribute": "href", "all": True},
        }

        result = extract_structured(self.HTML, "https://example.com/product", schema)

        assert result.data["title"] == "Visible title"
        assert result.data["author"] == "Ada Lovelace"
        assert result.data["price"] == 1299.0
        assert result.data["documents"] == ["https://example.com/one", "https://example.com/two"]
        assert result.provenance["author"]["method"] == "selector"
        assert result.provenance["author"]["selector"] == ".author"
        assert "Ada Lovelace" in result.provenance["author"]["evidence"]
        assert result.provenance["author"]["source_url"] == "https://example.com/product"

    def test_infers_fields_from_json_ld_and_meta(self) -> None:
        schema = {
            "name": {"type": "string"},
            "price": {"type": "number"},
            "title": {"type": "string"},
        }

        result = extract_structured(self.HTML, "https://example.com/product", schema)

        assert result.data["name"] == "Widget"
        assert result.data["price"] == 19.99
        assert result.data["title"] == "OG title"
        assert result.provenance["name"]["method"] == "json_ld"
        assert result.provenance["title"]["method"] == "meta"

    def test_missing_required_field_is_reported_not_invented(self) -> None:
        schema = {"sku": {"selector": ".sku", "required": True}}

        result = extract_structured(self.HTML, "https://example.com/product", schema)

        assert result.data["sku"] is None
        assert result.provenance["sku"]["method"] == "not_found"
        assert result.errors[0]["field"] == "sku"
        assert result.errors[0]["code"] == "required_field_missing"

    def test_accepts_plain_selector_strings(self) -> None:
        result = extract_structured(self.HTML, "https://example.com/", {"heading": "h1"})

        assert result.data["heading"] == "Visible title"
