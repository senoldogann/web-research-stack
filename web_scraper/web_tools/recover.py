"""Reading a walled page another way: what the page's own wall, URL and site give.

A page that answers a captcha, a 401, 403 or 429 or a bot wall can often still be read:

- **archive**: the Internet Archive's copy (``WebToolsService._archived_copy``);
- **amp** or **print**: the site's own other form of the same page, named in the wall's head
  (``<link rel="amphtml">``) or guessed from the address (``/amp``, ``?outputType=amp``, ``?print=1``);
- **feed**: the site's RSS or Atom feed, when it carries the article, in full or as a summary;
- **other_source**: another outlet's report of the same story, found by searching the page's
  title, which the wall shows or the address spells.

This module holds the parts that need no network: telling a wall from another failure, the title a
walled page has, the candidate addresses, the feed reader, and the words that say whose a recovered
text is. ``WebToolsService`` does the fetching, within a time budget.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from web_scraper.web_tools.discovery import query_tokens
from web_scraper.web_tools.errors import WebToolError

# A page the site would not serve to this client: a captcha, a bot check, a login or pay wall (code
# "blocked"), or a status that says so.
WALL_STATUSES = frozenset({401, 403, 429})
# A recovered text shorter than this is a stub, a login box or a summary line: not the page.
MIN_RECOVERED_CHARS = 400
# A feed's text shorter than this is a summary, not the article.
FEED_FULL_CHARS = 1_200
MAX_AMP_CANDIDATES = 4
MAX_FEED_CANDIDATES = 6
# Words of a title that match before another outlet's page counts as the same story.
MIN_TITLE_OVERLAP = 3
MIN_TITLE_MATCH = 0.6

_GENERIC_WALL_TITLES = (
    "access denied",
    "just a moment",
    "attention required",
    "are you a robot",
    "are you a human",
    "pardon our interruption",
    "security check",
    "verify you",
    "captcha",
    "blocked",
    "forbidden",
    "please enable",
    "sign in",
    "log in",
    "login",
    "subscribe",
    "error 4",
    "403",
    "401",
    "429",
    "cloudflare",
    "datadome",
)


def is_wall(error: WebToolError) -> bool:
    """Whether a failed read was a refusal: a captcha, a bot or login wall, 401, 403 or 429."""
    return error.code == "blocked" or (
        error.code == "http_error" and error.status_code in WALL_STATUSES
    )


def host_of(url: str) -> str:
    """The host of an address, lower case and without a leading ``www.``."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def same_site(candidate: str, original: str) -> bool:
    """Whether two addresses are of one site: the same host, or one a subdomain of the other."""
    left, right = host_of(candidate), host_of(original)
    if not left or not right:
        return False
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def page_key(url: str) -> str:
    """An address without scheme, ``www.``, query, fragment and trailing slash: the page it names."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return f"{host_of(url)}{parts.path.rstrip('/')}".lower()


# --- the title of a walled page ----------------------------------------------------------------------

_TRAILING_ID = re.compile(r"^(?:\d{4}[-_]?\d{2}[-_]?\d{2}|\d{6,}|[0-9a-f]{12,})$", re.IGNORECASE)
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:·]\s+")
_SLUG_EXTENSION = re.compile(r"\.(?:html?|php|aspx?|shtml|amp|cms)$", re.IGNORECASE)


def title_from_url(url: str) -> str:
    """The words a story's address spells: its slug without dates and ids; empty when it spells none.

    ``/openai-sell-its-top-models-google-cloud-2026-10-21`` is "openai sell its top models google
    cloud". An address that is an id or a few words (``/article/123``, ``/news``) is no title.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    for segment in reversed([part for part in path.split("/") if part]):
        stem = _SLUG_EXTENSION.sub("", segment)
        words = [word for word in re.split(r"[-_+\s]+", stem) if word]
        words = [word for word in words if not word.isdigit() and not _TRAILING_ID.match(word)]
        words = [word for word in words if any(c.isalpha() for c in word)]
        if len(words) >= MIN_TITLE_OVERLAP:
            return " ".join(words[:16])
    return ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _without_site_name(title: str) -> str:
    """ "Headline | Outlet" as "Headline": of the parts the title splits into, the longest."""
    parts = [part for part in _TITLE_SPLIT.split(title) if part.strip()]
    return max(parts, key=len).strip() if parts else title


def wall_title(body: str) -> str:
    """The title a refusing page still shows (a paywall shows the headline), or "" when it shows none.

    A captcha's own title ("Just a moment...", "Access Denied", "Reuters") says nothing of the
    story and is not it.
    """
    if not (body or "").strip():
        return ""
    soup = BeautifulSoup(body, "lxml")
    candidates: list[str] = []
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        meta = soup.find("meta", attrs=attrs)
        if meta is not None and meta.get("content"):
            candidates.append(str(meta["content"]))
    heading = soup.find("h1")
    if heading is not None:
        candidates.append(heading.get_text(" "))
    if soup.title is not None:
        candidates.append(soup.title.get_text(" "))
    for raw in candidates:
        title = _without_site_name(_clean(raw))
        lowered = title.lower()
        if len(title.split()) >= MIN_TITLE_OVERLAP and not any(
            mark in lowered for mark in _GENERIC_WALL_TITLES
        ):
            return title[:200]
    return ""


def title_match(wanted: str, found: str) -> tuple[int, float]:
    """How much of the shorter title the other holds: ``(words they share, that share)``."""
    left, right = query_tokens(wanted), query_tokens(found)
    if not left or not right:
        return 0, 0.0
    shared = len(left & right)
    return shared, shared / min(len(left), len(right))


def same_story(wanted: str, found: str) -> Optional[float]:
    """The share of title words two titles hold in common when it is enough to be one story, else None."""
    shared, share = title_match(wanted, found)
    return share if shared >= MIN_TITLE_OVERLAP and share >= MIN_TITLE_MATCH else None


# --- the page's other forms ----------------------------------------------------------------------------


def _head_links(body: str, base: str, rel: str, types: Iterable[str] = ()) -> list[str]:
    if not (body or "").strip():
        return []
    soup = BeautifulSoup(body, "lxml")
    wanted = set(types)
    found: list[str] = []
    for link in soup.find_all("link", href=True):
        rels = {str(value).lower() for value in (link.get("rel") or [])}
        if rel not in rels:
            continue
        if wanted and str(link.get("type") or "").lower() not in wanted:
            continue
        found.append(urljoin(base, str(link["href"])))
    return found


def _with_query(url: str, query: str) -> str:
    parts = urlsplit(url)
    joined = f"{parts.query}&{query}" if parts.query else query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, joined, ""))


def amp_candidates(url: str, body: str = "") -> list[tuple[str, str]]:
    """Addresses of the page's AMP and print forms, ``(kind, url)``: the wall's own ``amphtml`` link
    first, then the usual spellings. Only addresses of the same site, at most four."""
    candidates: list[tuple[str, str]] = [
        ("amp", link) for link in _head_links(body, url, "amphtml")
    ]
    parts = urlsplit(url)
    bare = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    if parts.path.strip("/") and not parts.path.rstrip("/").endswith("/amp"):
        candidates.append(("amp", f"{bare}/amp"))
    candidates.append(("amp", _with_query(url, "outputType=amp")))
    candidates.append(("print", _with_query(url, "print=1")))
    candidates.append(("amp", _with_query(url, "amp=1")))
    unique: list[tuple[str, str]] = []
    for kind, candidate in candidates:
        if same_site(candidate, url) and candidate != url and (kind, candidate) not in unique:
            unique.append((kind, candidate))
    return unique[:MAX_AMP_CANDIDATES]


_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")


def feed_candidates(url: str, body: str = "") -> list[str]:
    """Addresses of feeds that may carry the page: those its head names, then the usual places on
    the site and in the page's section. Only addresses of the same site, at most six."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates = _head_links(body, url, "alternate", _FEED_TYPES)
    segments = [part for part in parts.path.split("/") if part]
    if len(segments) > 1:
        section = f"{origin}/{segments[0]}"
        candidates += [f"{section}/feed", f"{section}/rss.xml"]
    candidates += [origin + path for path in _FEED_PATHS]
    unique: list[str] = []
    for candidate in candidates:
        if same_site(candidate, url) and candidate not in unique:
            unique.append(candidate)
    return unique[:MAX_FEED_CANDIDATES]


@dataclass
class FeedItem:
    """One entry of an RSS or Atom feed: its address, title, full text if it has one, summary, date."""

    title: str
    link: str
    html: str = ""
    summary: str = ""
    published: str = ""


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _inner_xml(node: Any) -> str:
    """The markup inside an element: its text and its children, children as XML."""
    parts = [node.text or ""]
    for child in node:
        parts.append(ElementTree.tostring(child, encoding="unicode"))
    return "".join(parts)


def parse_feed(text: str) -> list[FeedItem]:
    """The entries of an RSS 2.0 or Atom feed; none for anything else (a page, a broken feed)."""
    if not (text or "").strip():
        return []
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except (ElementTree.ParseError, ValueError):
        return []
    items: list[FeedItem] = []
    for node in root.iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue
        fields: dict[str, str] = {}
        link = ""
        for child in node:
            name = _local(child.tag)
            if name == "link":
                href = child.get("href")
                relation = (child.get("rel") or "alternate").lower()
                if href and relation == "alternate" and not link:
                    link = href
                elif child.text and child.text.strip() and not link:
                    link = child.text.strip()
            elif name in {"title", "description", "summary", "encoded", "content", "guid", "id"}:
                fields.setdefault(name, _inner_xml(child).strip())
            elif name in {"pubdate", "published", "updated", "date"}:
                fields.setdefault("published", (child.text or "").strip())
        link = link or (fields.get("guid", "") if fields.get("guid", "").startswith("http") else "")
        if not link:
            continue
        items.append(
            FeedItem(
                title=_clean(BeautifulSoup(fields.get("title", ""), "lxml").get_text(" ")),
                link=link,
                html=fields.get("encoded") or fields.get("content") or "",
                summary=fields.get("description") or fields.get("summary") or "",
                published=fields.get("published", ""),
            )
        )
    return items


def find_feed_item(items: Iterable[FeedItem], url: str, title: str = "") -> Optional[FeedItem]:
    """The entry of a feed that is the page: the one at its address, else the one of its title."""
    entries = list(items)
    wanted = page_key(url)
    for item in entries:
        if page_key(item.link) == wanted:
            return item
    if title:
        scored = [(same_story(title, item.title), item) for item in entries]
        matches = [(share, item) for share, item in scored if share is not None and share >= 0.8]
        if matches:
            return max(matches, key=lambda pair: pair[0] or 0.0)[1]
    return None


def feed_document_html(item: FeedItem) -> tuple[str, bool]:
    """A feed entry as a small HTML page, and whether it holds the article in full.

    Full when the entry carries the article's text (``content:encoded`` or Atom content) of a
    length that is an article's; the summary of an entry is a summary.
    """
    body = item.html or item.summary
    text = _clean(BeautifulSoup(body, "lxml").get_text(" "))
    complete = bool(item.html) and len(text) >= FEED_FULL_CHARS
    title = html.escape(item.title)
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><article><h1>{title}</h1>{body}</article></body></html>",
        complete,
    )


# --- whose text it is ------------------------------------------------------------------------------------


def recovery_header(
    kind: str,
    *,
    requested_url: str,
    source_url: str,
    taken: Optional[str] = None,
    title: str = "",
    complete: bool = True,
) -> str:
    """The line at the head of a recovered text that says whose it is: a blockquote of Markdown.

    A page written to a file is its Markdown alone, so the words that say it is not the live page
    (or not the requested one) travel with it.
    """
    if kind == "archive":
        when = f" taken {taken}" if taken else ""
        return (
            f"> Recovered from the Internet Archive's copy of {requested_url}{when}"
            f" ({source_url}). Not the live page; it may be out of date."
        )
    if kind == "amp":
        return f"> Recovered from the AMP version of {requested_url}: {source_url}."
    if kind == "print":
        return f"> Recovered from the print version of {requested_url}: {source_url}."
    if kind == "feed":
        extent = (
            "the article in full"
            if complete
            else "only a summary of the article: the rest of the page is missing"
        )
        return f"> Recovered from the site's feed ({source_url}), which carries {extent}."
    named = f'"{title}" ' if title else ""
    return (
        f"> Not the requested page. {requested_url} would not be served to this client; this is "
        f"{named}from {host_of(source_url)} ({source_url}), found by searching the page's title. "
        "Treat it as that outlet's own report."
    )
