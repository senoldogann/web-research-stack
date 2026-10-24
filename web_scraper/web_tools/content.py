"""HTML parsing, Markdown conversion, challenge detection, and extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from defusedxml import ElementTree

from web_scraper.web_tools.dates import DISAGREEMENT_DAYS, DateResolution, resolve_page
from web_scraper.web_tools.fields import (
    DATE_FIELDS,
    canonical_field,
    element_text,
    find_field,
    normalize_date,
)
from web_scraper.web_tools.urls import UnsafeUrlError, canonicalize_url, url_in_scope

_STRIP_TAGS = (
    "script",
    "style",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "noscript",
    "svg",
    "canvas",
    "dialog",
    "video",
    "audio",
    "button",
    "input",
    "select",
    "textarea",
)
# Page chrome, as elements or as ARIA landmarks: menus, banners, footers, and
# sidebars repeat on every page of a site and are not the page's content.
_CHROME_TAGS = ("nav", "header", "footer", "aside")
_CHROME_ROLES = ("navigation", "banner", "contentinfo", "complementary", "search")
_BLOCK_TAGS = {
    "address",
    "article",
    "body",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "main",
    "section",
}
# Interstitial detection. A challenge page is small and says little; a real page
# often names "captcha" in its scripts (a login modal, a JSON config), so raw
# substrings are not evidence. Only widgets in the DOM and phrases in the
# visible text count, and only on pages too thin to be content.
_CHALLENGE_PAGE_MAX_BYTES = 100_000
_CHALLENGE_TEXT_MAX_CHARS = 3_000
_REFUSAL_BODY_MAX_BYTES = 20_000
_INVISIBLE_TAGS = ("script", "style", "template")
_CAPTCHA_WIDGETS = (
    "g-recaptcha",
    "recaptcha",
    "h-captcha",
    "hcaptcha",
    "cf-turnstile",
    "px-captcha",
)
_CAPTCHA_FRAMES = (
    "google.com/recaptcha",
    "recaptcha.net",
    "hcaptcha.com",
    "challenges.cloudflare.com",
    "captcha-delivery.com",
)
_CAPTCHA_PHRASES = ("unusual traffic", "verify you are human", "are you a robot")
_BLOCK_PHRASES = (
    "access denied",
    "just a moment",
    "checking your browser",
    "you have been blocked",
    "attention required",
    "request blocked",
)
_BLOCK_IDS = ("cf-error-details",)
_WALL_PHRASES = (
    "sign in to continue",
    "log in to continue",
    "sign in to view",
    "log in to view",
    "subscribe to continue",
    "create an account to continue",
)


@dataclass
class PageLink:
    url: str
    text: str
    kind: str
    # Inside navigation, a header, a footer, or a sidebar rather than the content.
    chrome: bool = False


@dataclass
class ParsedPage:
    title: str
    description: str
    canonical_url: str
    markdown: str
    links: list[PageLink] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # What every date signal of the page comes to (see dates.py); None for a document that is not
    # a web page (a PDF).
    dates: Optional[DateResolution] = None


@dataclass
class StructuredResult:
    data: dict[str, Any]
    provenance: dict[str, Any]
    errors: list[dict[str, Any]] = field(default_factory=list)
    # True when an ``all`` field had more values than its max_items.
    truncated: bool = False


# Sitemaps and feeds: read with the XML parser, as the HTML one misreads them and warns
# (XMLParsedAsHTMLWarning). lxml resolves internal entities only, never a file or a URL.
_XML_TYPES = frozenset(
    {"application/xml", "text/xml", "application/rss+xml", "application/atom+xml"}
)


def _looks_like_xml(markup: str) -> bool:
    """Whether a document opens with an XML declaration and is not XHTML (no ``<html``)."""
    head = (markup or "")[:500].lstrip("\ufeff \t\r\n").lower()
    return head.startswith("<?xml") and "<html" not in head


def _soup(html: str, *, xml: Optional[bool] = None) -> BeautifulSoup:
    """Parse a document: ``xml`` forces a parser, ``None`` lets the document say.

    A sitemap, a feed or any XML document parsed as HTML makes bs4 warn
    (XMLParsedAsHTMLWarning) on every call, and the warning floods the log of a server
    that reads many of them; a server may send one as text/html or with no type at all,
    so what the document opens with decides, not only the type it was sent as.
    """
    if xml is None:
        xml = _looks_like_xml(html)
    return BeautifulSoup(html or "", "xml" if xml else "lxml")


def interstitial_message(kind: str) -> str:
    """The message of a challenge, blocked or wall error ("an access_blocked", "a captcha")."""
    article = "an" if kind[:1].lower() in "aeiou" else "a"
    return f"Target returned {article} {kind} interstitial"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _resolve(base_url: str, href: str) -> Optional[str]:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    absolute = urljoin(base_url, href)
    if urlsplit(absolute).scheme.lower() not in {"http", "https"}:
        return None
    try:
        return canonicalize_url(absolute)
    except UnsafeUrlError:
        return None


def _inline(node: Any, base_url: str) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in {"strong", "b"}:
        inner = _inline_children(node, base_url).strip()
        return f"**{inner}**" if inner else ""
    if name in {"em", "i"}:
        inner = _inline_children(node, base_url).strip()
        return f"*{inner}*" if inner else ""
    if name == "code":
        return f"`{node.get_text()}`"
    if name == "a":
        inner = _inline_children(node, base_url).strip()
        target = _resolve(base_url, node.get("href", ""))
        return f"[{inner}]({target})" if target and inner else inner
    if name == "br":
        return "\n"
    if name == "img":
        src = node.get("src") or node.get("data-src") or ""
        alt = node.get("alt", "")
        target = _resolve(base_url, src) if src else None
        return f"![{alt}]({target})" if target else ""
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        inner = _inline_children(node, base_url).strip()
        return f"{'#' * int(name[1])} {inner}"
    return _inline_children(node, base_url)


def _inline_children(node: Tag, base_url: str) -> str:
    return "".join(_inline(child, base_url) for child in node.children)


def _render_list(node: Tag, base_url: str, ordered: bool) -> str:
    lines: list[str] = []
    index = 1
    for item in node.find_all("li", recursive=False):
        text = _render_children(item, base_url).strip()
        if not text:
            continue
        marker = f"{index}." if ordered else "-"
        lines.append(f"{marker} {text}")
        index += 1
    return "\n".join(lines)


def _render_table(node: Tag, base_url: str) -> str:
    rows: list[list[str]] = []
    for row in node.find_all("tr"):
        cells = [
            _clean_text(_inline_children(cell, base_url)) for cell in row.find_all(["th", "td"])
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n".join(lines)


def _render_pre(node: Tag) -> str:
    code = node.find("code")
    source = code if code else node
    classes = source.get("class", []) if isinstance(source, Tag) else []
    language = ""
    for value in classes:
        if value.startswith("language-"):
            language = value.removeprefix("language-")
            break
        if value.startswith("lang-"):
            language = value.removeprefix("lang-")
            break
    body = source.get_text().strip("\n")
    return f"```{language}\n{body}\n```"


def _render_children(node: Tag, base_url: str) -> str:
    return "\n\n".join(
        part for child in node.children if (part := _render_node(child, base_url)).strip()
    )


def _render_node(node: Any, base_url: str) -> str:
    if isinstance(node, NavigableString):
        text = _clean_text(str(node))
        return text
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name == "pre":
        return _render_pre(node)
    if name in {"ul", "ol"}:
        return _render_list(node, base_url, ordered=name == "ol")
    if name == "table":
        return _render_table(node, base_url)
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return _inline(node, base_url).strip()
    if name == "p":
        return _inline_children(node, base_url).strip()
    if name == "blockquote":
        text = _render_children(node, base_url).strip()
        return "\n".join(f"> {line}" for line in text.splitlines() if line.strip())
    if name == "hr":
        return "---"
    if name in _BLOCK_TAGS:
        return _render_children(node, base_url)
    return _inline(node, base_url).strip()


def _normalise_markdown(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_markdown(html: str, base_url: str, max_chars: int = 50_000) -> str:
    """Convert readable page HTML to compact Markdown, excluding page chrome."""
    soup = _soup(html)
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": list(_CHROME_ROLES)}):
        tag.decompose()

    root = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.find("body")
        or soup
    )
    markdown = _normalise_markdown(_render_children(root, base_url))
    return markdown[:max_chars]


def _extract_title(soup: BeautifulSoup) -> str:
    title = soup.find("title")
    if title and _clean_text(title.get_text()):
        return _clean_text(title.get_text())
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        return _clean_text(og_title["content"])
    heading = soup.find("h1")
    return _clean_text(heading.get_text()) if heading else ""


def _extract_description(soup: BeautifulSoup) -> str:
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            return _clean_text(meta["content"])
    return ""


def _extract_canonical(soup: BeautifulSoup, base_url: str) -> str:
    link = soup.find("link", rel=lambda value: value and "canonical" in value)
    if link and link.get("href"):
        target = _resolve(base_url, link["href"])
        if target:
            return target
    try:
        return canonicalize_url(base_url)
    except UnsafeUrlError:
        return base_url


def _iter_json_ld(soup: BeautifulSoup, limit: int = 20) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                items.extend(item for item in graph if isinstance(item, dict))
            else:
                items.append(candidate)
            if len(items) >= limit:
                return items[:limit]
    return items[:limit]


def _extract_metadata(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property")
        content = meta.get("content")
        if name and content:
            metadata[name] = _clean_text(content)
    metadata["json_ld"] = _iter_json_ld(soup)
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        metadata["lang"] = html_tag["lang"]
    metadata["canonical_url"] = _extract_canonical(soup, base_url)
    return metadata


def _extract_links(soup: BeautifulSoup, base_url: str, max_links: int) -> list[PageLink]:
    links: list[PageLink] = []
    seen: set[str] = set()
    chrome = soup.find_all(_CHROME_TAGS) + soup.find_all(attrs={"role": list(_CHROME_ROLES)})
    chrome_ids = {id(node) for node in chrome}
    for anchor in soup.find_all("a", href=True):
        target = _resolve(base_url, anchor["href"])
        if not target or target in seen:
            continue
        seen.add(target)
        links.append(
            PageLink(
                url=target,
                text=_clean_text(anchor.get_text())[:200],
                kind="internal"
                if url_in_scope(target, base_url, include_subdomains=False)
                else "external",
                chrome=any(id(parent) in chrome_ids for parent in anchor.parents),
            )
        )
        if len(links) >= max_links:
            break
    return links


def extract_page(
    html: str,
    base_url: str,
    max_links: int = 200,
    max_chars: int = 50_000,
) -> ParsedPage:
    """Extract a page's readable Markdown plus metadata, outgoing links and publication date."""
    soup = _soup(html)
    metadata = _extract_metadata(soup, base_url)
    return ParsedPage(
        title=_extract_title(soup),
        description=_extract_description(soup),
        canonical_url=_extract_canonical(soup, base_url),
        markdown=html_to_markdown(html, base_url, max_chars=max_chars),
        links=_extract_links(soup, base_url, max_links),
        metadata=metadata,
        dates=resolve_page(soup, metadata.get("json_ld") or [], base_url),
    )


def resolve_html_dates(html: str, base_url: str, *, today: Optional[date] = None) -> DateResolution:
    """The publication date of a page given as HTML, possibly only its first bytes.

    A page read from its head alone (the first 64 KB) has its meta tags and structured data, which
    is where most of the dates are; a script cut in half is skipped.
    """
    soup = _soup(html)
    return resolve_page(soup, _iter_json_ld(soup), base_url, today=today)


def needs_browser_render(html: str, markdown: str, min_chars: int = 200) -> bool:
    """Whether static HTML is likely a client-rendered shell rather than content."""
    text_length = len(_clean_text(markdown))
    if text_length >= min_chars:
        return False
    lowered = (html or "").lower()
    if "<script" not in lowered:
        return False
    shell_markers = ('id="root"', "id='root'", 'id="app"', "id='app'")
    if any(marker in lowered for marker in shell_markers):
        return True
    if "document.write(" in lowered or "document.writeln(" in lowered:
        return True
    return text_length < max(20, min_chars // 4)


def _body_bytes(text: str) -> int:
    # UTF-8 never has fewer bytes than characters, so a long string skips encoding.
    if len(text) > _CHALLENGE_PAGE_MAX_BYTES:
        return len(text)
    return len(text.encode("utf-8", errors="ignore"))


def _is_invisible_captcha(node: Tag) -> bool:
    # reCAPTCHA v3 and invisible widgets score a visitor in the background of a
    # normal page; they never stand between the visitor and the content.
    if str(node.get("data-size") or "").lower() == "invisible":
        return True
    if "grecaptcha-badge" in " ".join(node.get("class") or []).lower():
        return True
    return "size=invisible" in str(node.get("src") or "").lower()


def _has_captcha_form(soup: BeautifulSoup) -> bool:
    # A form that posts to a captcha endpoint exists to pass a challenge.
    return any(
        "captcha" in f"{form.get('action') or ''} {form.get('id') or ''}".lower()
        for form in soup.find_all("form")
    )


def _has_captcha_widget(soup: BeautifulSoup) -> bool:
    for node in soup.find_all(["div", "form", "iframe"]):
        if _is_invisible_captcha(node):
            continue
        marks = f"{' '.join(node.get('class') or [])} {node.get('id') or ''}".lower()
        if any(widget in marks for widget in _CAPTCHA_WIDGETS):
            return True
        if node.name == "iframe":
            source = str(node.get("src") or "").lower()
            if any(frame in source for frame in _CAPTCHA_FRAMES):
                return True
    return _has_captcha_form(soup)


def _visible_text(soup: BeautifulSoup) -> str:
    return _clean_text(soup.get_text(" ")).lower()


def detect_interstitial(
    html: str,
    final_url: str,
    status_code: int = 200,
    *,
    content_type: str = "",
) -> Optional[str]:
    """Classify bot challenges, blocks, and login/pay walls from fetched HTML.

    A captcha needs evidence a visitor would meet: a widget in the DOM or a
    challenge phrase in the visible text, never a word inside a script. A
    successful response over ``_CHALLENGE_PAGE_MAX_BYTES``, or one with more
    visible text than ``_CHALLENGE_TEXT_MAX_CHARS``, is content, not a
    challenge, with one exception: a page that says little and carries a form
    posting to a captcha endpoint (Reddit inlines ~160 KB of assets around
    one). A short 401/403 page is an edge refusal (``access_blocked``).
    """
    raw = html or ""
    size = _body_bytes(raw)
    succeeded = 200 <= status_code < 300
    soup = _soup(raw, xml=True if content_type in _XML_TYPES else None)
    for tag in soup(_INVISIBLE_TAGS):
        tag.decompose()

    if succeeded and size > _CHALLENGE_PAGE_MAX_BYTES:
        if _has_captcha_form(soup) and len(_visible_text(soup)) <= _CHALLENGE_TEXT_MAX_CHARS:
            return "captcha"
    else:
        text = _visible_text(soup)
        if not succeeded or len(text) <= _CHALLENGE_TEXT_MAX_CHARS:
            if _has_captcha_widget(soup) or any(phrase in text for phrase in _CAPTCHA_PHRASES):
                return "captcha"
            if soup.find(id=list(_BLOCK_IDS)) or any(phrase in text for phrase in _BLOCK_PHRASES):
                return "access_blocked"
            if any(phrase in text for phrase in _WALL_PHRASES):
                return "access_wall"
        if (
            status_code in (401, 403)
            and size <= _REFUSAL_BODY_MAX_BYTES
            and not raw.lstrip().startswith(("{", "["))
        ):
            return "access_blocked"

    title = _clean_text(soup.title.get_text()).lower() if soup.title else ""
    path = urlsplit(final_url).path.lower()
    login_path = bool(re.search(r"/(login|signin|sign-in|log-in|auth)(/|$)", path))
    login_title = any(value in title for value in ("sign in", "log in", "login"))
    if login_path and login_title:
        return "access_wall"
    if login_title and soup.find("input", attrs={"type": "password"}):
        return "access_wall"
    return None


def parse_sitemap_xml(data: bytes) -> tuple[str, list[dict[str, str]]]:
    """Parse a sitemap URL set or index, rejecting entity-expansion payloads."""
    root = ElementTree.fromstring(data)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"urlset", "sitemapindex"}:
        return kind, []

    entries: list[dict[str, str]] = []
    child_name = "url" if kind == "urlset" else "sitemap"
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != child_name:
            continue
        entry: dict[str, str] = {}
        for node in child:
            name = node.tag.rsplit("}", 1)[-1]
            text = (node.text or "").strip()
            if name == "loc" and text:
                entry["url"] = text
            elif name == "lastmod" and text:
                entry["lastmod"] = text
        if entry.get("url"):
            entries.append(entry)
    return kind, entries


_DATE_TYPES = frozenset({"date", "datetime"})
_NUMBER_TYPES = frozenset({"number", "float", "integer"})


def _coerce(value: Any, value_type: str) -> Any:
    if value is None:
        return None
    if value_type in _DATE_TYPES:
        text = _clean_text(str(value))
        return normalize_date(text) or text
    if value_type in _NUMBER_TYPES:
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            cleaned = re.sub(r"[^0-9.\-]", "", str(value).replace(",", ""))
            try:
                number = float(cleaned)
            except ValueError:
                return None
        return int(number) if value_type == "integer" else number
    if value_type == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes"}
    return _clean_text(str(value))


def _moment_of(element: Tag) -> str:
    """The machine-readable moment an element carries: its own datetime or content, or its time's."""
    for node in (element, element.find("time", attrs={"datetime": True})):
        if isinstance(node, Tag):
            moment = node.get("datetime") or (node.get("content") if node.get("itemprop") else None)
            if moment:
                return str(moment)
    return ""


def _record_page(entry: dict[str, Any], total: int, returned: int, offset: int) -> None:
    """The paging of an ``all`` field in its provenance.

    ``total`` matches, ``returned`` of them from ``offset`` on; ``truncated`` and the
    ``next_offset`` to ask for only when matches are left after this page.
    """
    entry.update(
        total=total,
        returned=returned,
        offset=offset,
        truncated=offset + returned < total,
    )
    if offset + returned < total:
        entry["next_offset"] = offset + returned


def _same_day(found: str, settled: Optional[str]) -> bool:
    """Whether a date a field found is within a day of the one the signals settled on (a month: in it)."""
    if not settled:
        return True
    try:
        day = date.fromisoformat(found[:10])
        if len(settled) == 7:
            return (day.year, day.month) == (int(settled[:4]), int(settled[5:7]))
        return abs((day - date.fromisoformat(settled[:10])).days) <= DISAGREEMENT_DAYS
    except ValueError:
        return True


def _signal_method(source: str) -> str:
    return (
        "json_ld"
        if source.startswith("json_ld")
        else "meta"
        if source.startswith("meta")
        else "html"
    )


def _settle_date(
    kind: str,
    value: Any,
    entry: dict[str, Any],
    dates: DateResolution,
    base_url: str,
) -> tuple[Any, dict[str, Any]]:
    """A published or modified date found by its name, held to the page's date signals.

    The value stands when it is within a day of what the signals settled on. When they weigh
    against it (JSON-LD says September, two other signals say October), their winner is the
    value, with its own source: the answer's ``date`` and ``published_at`` are never two different
    days. A published date's provenance gains ``date_confidence`` and ``date_conflicts``.
    """
    if kind == "published":
        settled, moment, source = dates.date, dates.moment, dates.date_source
    else:
        settled, moment, source = (
            dates.date_modified,
            dates.modified_moment,
            dates.date_modified_source,
        )
    if not settled or not isinstance(value, str):
        return value, entry
    if not _same_day(value, settled) and moment and source:
        value = moment
        entry = {
            "method": _signal_method(source),
            "selector": source,
            "evidence": moment,
            "source_url": base_url,
        }
    if kind == "published":
        entry["date_confidence"] = dates.date_confidence
        entry["date_conflicts"] = [dict(item) for item in dates.date_conflicts]
    return value, entry


def extract_structured(
    html: str,
    base_url: str,
    schema: dict[str, Any],
    *,
    max_items: int = 100,
    dates: Optional[DateResolution] = None,
) -> StructuredResult:
    """Extract schema fields with selector or structured-data provenance.

    An ``all`` field returns at most its own ``max_items`` values (*max_items*
    when it names none); its provenance counts every match in ``total``. With *dates* (what
    the page's date signals come to), a published or modified date found by its name is held to
    them: see ``_settle_date``.
    """
    soup = _soup(html)
    json_ld = _iter_json_ld(soup)
    data: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    truncated = False

    for field_name, raw_spec in schema.items():
        spec = {"selector": raw_spec} if isinstance(raw_spec, str) else dict(raw_spec or {})
        value_type = str(spec.get("type", "string"))
        required = bool(spec.get("required", False))
        found = False

        selectors = [spec["selector"]] if spec.get("selector") else []
        selectors.extend(spec.get("fallback_selectors") or [])
        for selector in selectors:
            try:
                elements = soup.select(selector)
            except Exception:  # noqa: BLE001 - invalid user-supplied CSS selector
                errors.append(
                    {
                        "field": field_name,
                        "code": "invalid_selector",
                        "message": f"Selector {selector!r} is not valid CSS",
                    }
                )
                elements = []
            if not elements:
                continue

            wants_all = bool(spec.get("all", False))
            limit = int(spec.get("max_items") or max_items) if wants_all else 1
            skip = int(spec.get("offset") or 0) if wants_all else 0
            selected = elements if wants_all else elements[:1]
            values: list[Any] = []
            evidence: list[str] = []
            total = 0
            for element in selected:
                attribute = spec.get("attribute")
                if attribute:
                    raw_value = element.get(attribute, "")
                elif value_type in _DATE_TYPES and _moment_of(element):
                    raw_value = _moment_of(element)
                else:
                    # A space between the element's parts: "Oct 23, 2026" and "8:00 AM" in
                    # two spans read "Oct 23, 2026 8:00 AM", not "Oct 23, 20268:00 AM".
                    raw_value = element_text(element)
                if attribute == "href":
                    raw_value = _resolve(base_url, raw_value) or raw_value
                coerced = _coerce(raw_value, value_type)
                if coerced in (None, ""):
                    continue
                total += 1
                if total <= skip:
                    continue  # before the page asked for
                if len(values) < limit:
                    values.append(coerced)
                    evidence.append(element_text(element) or _clean_text(str(raw_value)))

            # An offset past the last match is an empty page of a field that exists.
            if values or (wants_all and total):
                data[field_name] = values if wants_all else values[0]
                provenance[field_name] = {
                    "method": "selector",
                    "selector": selector,
                    "evidence": " | ".join(evidence)[:500],
                    "source_url": base_url,
                }
                if wants_all:
                    _record_page(provenance[field_name], total, len(values), skip)
                    truncated = truncated or skip + len(values) < total
                found = True
                break

        if found:
            continue

        # By its name: a field with no selector, or a well-known one whose selectors
        # found nothing, from the page's JSON-LD, meta tags, then HTML conventions.
        named = find_field(soup, json_ld, field_name, base_url, allow_unknown=not selectors)
        if named is not None:
            wants_all = bool(spec.get("all", False))
            kind = value_type
            if canonical_field(field_name) in DATE_FIELDS and value_type not in _NUMBER_TYPES:
                kind = "date"
            if wants_all:
                limit = int(spec.get("max_items") or max_items)
                skip = int(spec.get("offset") or 0)
                every = [_coerce(value, kind) for value in named.values]
                every = [value for value in every if value not in (None, "")]
                value: Any = every[skip : skip + limit]
            elif kind == "string":
                value = _coerce(", ".join(named.values), kind)
            else:
                value = _coerce(named.values[0], kind)
            if value not in (None, "") and (value != [] or (wants_all and every)):
                entry: dict[str, Any] = {
                    "method": named.method,
                    "selector": named.selector,
                    "evidence": named.evidence[:500],
                    "source_url": base_url,
                }
                known = canonical_field(field_name)
                if dates is not None and known in DATE_FIELDS and not wants_all:
                    value, entry = _settle_date(known, value, entry, dates, base_url)
                data[field_name] = value
                provenance[field_name] = entry
                if wants_all:
                    _record_page(provenance[field_name], len(every), len(value), skip)
                    truncated = truncated or skip + len(value) < len(every)
                continue

        data[field_name] = None
        provenance[field_name] = {
            "method": "not_found",
            "selector": spec.get("selector"),
            "evidence": "",
            "source_url": base_url,
        }
        if required:
            errors.append(
                {
                    "field": field_name,
                    "code": "required_field_missing",
                    "message": f"Required field {field_name!r} was not found on the page",
                }
            )

    return StructuredResult(
        data=data,
        provenance=provenance,
        errors=errors,
        truncated=truncated,
    )
