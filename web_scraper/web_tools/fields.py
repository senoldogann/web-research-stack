"""Fields named in plain words, found without a selector (web_extract).

A field with no selector, or a well-known one whose selectors find nothing, is
looked up by its name: "headline", "author", "published_at" and their kin are
read from the page's JSON-LD first, then its meta tags, then the HTML
conventions sites share (``<time datetime>``, ``rel=author``, ``itemprop``, the
article's first paragraph). Dates come back in ISO 8601. A name this module
does not know is read from JSON-LD and meta tags under that name alone, and is
otherwise reported missing: nothing is guessed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional

from bs4 import BeautifulSoup, Tag


@dataclass
class Found:
    """What a field's name found: its value, how, where, and the text as the page wrote it."""

    values: list[str]
    method: str
    selector: str
    evidence: str


# The names a caller may give each well-known field.
_ALIASES: dict[str, tuple[str, ...]] = {
    "headline": ("headline",),
    "title": ("title",),
    "name": ("name",),
    "price": ("price",),
    "author": ("author", "authors", "byline", "writer", "creator", "by"),
    "published": (
        "published",
        "published_at",
        "date_published",
        "publish_date",
        "publication_date",
        "pub_date",
        "pubdate",
        "date",
        "created_at",
        "date_created",
        "posted_at",
        "release_date",
    ),
    "modified": (
        "modified",
        "modified_at",
        "updated",
        "updated_at",
        "date_modified",
        "last_modified",
        "last_updated",
    ),
    "description": ("description", "summary", "abstract", "excerpt", "dek", "standfirst"),
    "image": ("image", "thumbnail", "og_image", "cover_image", "picture"),
    "site_name": ("site_name", "publisher", "site", "source", "outlet", "publication"),
    "section": ("section", "category", "article_section"),
    "keywords": ("keywords", "tags", "topics"),
    "language": ("language", "lang", "locale", "in_language"),
    "url": ("url", "canonical", "canonical_url", "permalink"),
    "lead": ("lead", "lede", "first_paragraph", "intro", "introduction", "opening"),
    "body": ("body", "article_body", "article_text", "full_text"),
    "word_count": ("word_count",),
}
_CANONICAL = {alias: name for name, aliases in _ALIASES.items() for alias in aliases}
DATE_FIELDS = frozenset({"published", "modified"})
# Fields whose JSON-LD or meta value is about the article, read from an article's
# item before the page's others (a WebSite or an Organization item has a name too, and a
# description and an image: a page a browser has drawn often carries the site's own items
# ahead of the post's).
_ARTICLE_FIELDS = frozenset(
    {
        "headline",
        "author",
        "published",
        "modified",
        "section",
        "keywords",
        "body",
        "word_count",
        "description",
        "image",
    }
)
_ARTICLE_TYPES = ("article", "posting", "report", "review", "recipe", "webpage")

_JSON_LD: dict[str, tuple[str, ...]] = {
    "headline": ("headline",),
    "title": ("headline",),
    "name": ("name",),
    "price": ("price", "offers.price"),
    "author": ("author",),
    "published": ("datePublished", "dateCreated", "uploadDate"),
    "modified": ("dateModified",),
    "description": ("description",),
    "image": ("image",),
    "site_name": ("publisher", "sourceOrganization"),
    "section": ("articleSection",),
    "keywords": ("keywords",),
    "language": ("inLanguage",),
    "url": ("url", "mainEntityOfPage"),
    "body": ("articleBody",),
    "word_count": ("wordCount",),
}
_META: dict[str, tuple[tuple[str, str], ...]] = {
    "headline": (("property", "og:title"), ("name", "twitter:title")),
    "title": (("property", "og:title"), ("name", "twitter:title")),
    "price": (("property", "product:price:amount"), ("property", "og:price:amount")),
    "author": (
        ("name", "author"),
        ("name", "parsely-author"),
        ("name", "sailthru.author"),
        ("name", "byl"),
        ("name", "dc.creator"),
        ("property", "article:author"),
    ),
    "published": (
        ("property", "article:published_time"),
        ("name", "article:published_time"),
        ("property", "og:published_time"),
        ("itemprop", "datePublished"),
        ("name", "parsely-pub-date"),
        ("name", "pubdate"),
        ("name", "publish-date"),
        ("name", "sailthru.date"),
        ("name", "dc.date"),
        ("name", "date"),
    ),
    "modified": (
        ("property", "article:modified_time"),
        ("name", "article:modified_time"),
        ("property", "og:updated_time"),
        ("itemprop", "dateModified"),
        ("name", "last-modified"),
    ),
    "description": (
        ("name", "description"),
        ("property", "og:description"),
        ("name", "twitter:description"),
    ),
    "image": (("property", "og:image"), ("name", "twitter:image")),
    "site_name": (("property", "og:site_name"), ("name", "application-name")),
    "section": (("property", "article:section"), ("name", "parsely-section")),
    "keywords": (
        ("name", "keywords"),
        ("name", "news_keywords"),
        ("property", "article:tag"),
        ("name", "parsely-tags"),
    ),
    "language": (("property", "og:locale"),),
    "url": (("property", "og:url"),),
}
# Several values in one string: "news, world".
_SPLIT_FIELDS = frozenset({"keywords"})

# A paragraph long enough to be an article's lead, not a label ("In this article").
_LEAD_MIN_WORDS = 12
_NOT_CONTENT = ("nav", "aside", "header", "footer", "figure", "figcaption", "form", "table")
_NOT_CONTENT_MARKS = frozenset(
    {
        "toc",
        "caption",
        "credit",
        "byline",
        "breadcrumb",
        "breadcrumbs",
        "share",
        "newsletter",
        "promo",
        "carousel",
        "gallery",
        "slide",
        "slideshow",
        "photo",
        "related",
        "recommended",
        "sponsored",
        "advertisement",
    }
)


def element_text(node: Tag) -> str:
    """An element's text as a reader sees it: a space between its parts, none before a closing mark.

    "Oct 23, 2026" and "8:00 AM" in two spans read "Oct 23, 2026 8:00 AM", and "<a>Link</a>, more
    (<b>bold</b>)." reads "Link, more (bold).".
    """
    text = re.sub(r"\s+", " ", node.get_text(" ")).strip()
    text = re.sub(r"\s+([,.;:!?%)\]}])", r"\1", text)
    return re.sub(r"([(\[{])\s+", r"\1", text)


def canonical_field(name: str) -> Optional[str]:
    """The well-known field a caller's name stands for ("datePublished" -> "published")."""
    return _CANONICAL.get(_snake(name))


def find_field(
    soup: BeautifulSoup,
    json_ld: list[dict[str, Any]],
    name: str,
    base_url: str,
    *,
    allow_unknown: bool = True,
) -> Optional[Found]:
    """Find a field by its name; None when the page does not say it."""
    known = canonical_field(name)
    if known is None:
        return _find_unknown(soup, json_ld, name) if allow_unknown else None
    for finder in (_from_json_ld, _from_meta, _from_html):
        found = finder(soup, json_ld, known, base_url)
        if found is not None and found.values:
            if known in _SPLIT_FIELDS:
                found.values = _split(found.values)
            if known == "author":
                found.values = [_without_by(value) for value in found.values]
            return _one_of_each(found)
    return None


def _one_of_each(found: Found) -> Found:
    """The values of a field with those that read alike kept once.

    Two signals that give the same value are one value: a page that writes its ``og:title`` twice
    (a framework that renders its head from the layout and from the page) has one title, not
    "Title, Title", and an author named by the JSON-LD and by a meta tag is named once. Alike is
    the same words in the same order whatever the spacing and the case; the first spelling stays.
    """
    kept: dict[str, str] = {}
    for value in found.values:
        kept.setdefault(" ".join(unicodedata.normalize("NFKC", value).split()).casefold(), value)
    if len(kept) < len(found.values):
        found.values = list(kept.values())
        if found.method in {"json_ld", "meta"}:
            found.evidence = ", ".join(found.values)
    return found


def _iso_moment(text: str) -> str:
    """A moment as ``datetime.fromisoformat`` of every supported Python reads it.

    Before Python 3.11 it reads neither the zone "Z", an offset without its colon (``+0300``,
    which cloud.google.com writes) or only hours (``+03``), nor a fraction of other than three or
    six digits (``.12Z``); a space for the ``T`` it reads on every version.
    """
    if not re.search(r"\d{2}:\d{2}", text):
        return text
    moment = re.sub(r"[Zz]$", "+00:00", text)
    moment = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", moment)
    moment = re.sub(r"(\d{2}:\d{2}(?::\d{2})?[.]?\d*)([+-]\d{2})$", r"\1\2:00", moment)
    return re.sub(
        r"([.,])(\d{1,9})(?=[+-]|$)",
        lambda match: "." + match.group(2)[:6].ljust(6, "0"),
        moment,
    )


def normalize_date(text: str) -> Optional[str]:
    """A date or a moment as written on a page, in ISO 8601; None when it cannot be read."""
    cleaned = _date_text(text)
    if not cleaned:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            return date.fromisoformat(cleaned).isoformat()
        return datetime.fromisoformat(_iso_moment(cleaned)).isoformat()
    except ValueError:
        pass
    if re.match(r"^(?:[A-Za-z]{3},\s*)?\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d", cleaned):
        try:
            return parsedate_to_datetime(cleaned).isoformat()
        except (TypeError, ValueError, IndexError):
            pass
    # The other forms carry no zone this module can read; a zone's letters are dropped.
    cleaned = re.sub(r"\s+(?!AM$|PM$)(?:[A-Z]{2,4}|UTC[+-]\d{1,2})$", "", cleaned)
    for pattern in _MOMENT_FORMATS:
        try:
            return datetime.strptime(cleaned, pattern).isoformat()
        except ValueError:
            continue
    for pattern in _DAY_FORMATS:
        try:
            return datetime.strptime(cleaned, pattern).date().isoformat()
        except ValueError:
            continue
    return None


_MOMENT_FORMATS = (
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%B %d, %Y, %I:%M %p",
    "%b %d, %Y %H:%M",
    "%B %d, %Y %H:%M",
    "%d %b %Y %H:%M",
    "%d %B %Y %H:%M",
    "%d %b %Y, %H:%M",
    "%d %B %Y, %H:%M",
    "%Y/%m/%d %H:%M",
    "%d.%m.%Y %H:%M",
)
_DAY_FORMATS = (
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
)
_DATE_LABEL = re.compile(
    r"^(?:last\s+updated|updated|published|posted|modified|date)\s*(?:on|at)?\s*:?\s*",
    re.IGNORECASE,
)


def _date_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = _DATE_LABEL.sub("", cleaned)
    # "Oct. 23" and "Sept. 23" read as "Oct 23" and "Sep 23".
    cleaned = re.sub(r"\b([A-Za-z]{3})[a-z]?\.(?=\s)", r"\1", cleaned)
    return cleaned.rstrip(".").strip()


def _snake(name: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name.strip())
    return re.sub(r"[\s\-.]+", "_", spaced).lower()


def _camel(name: str) -> str:
    head, *rest = _snake(name).split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _text(value: Any, index: Optional[dict[str, dict[str, Any]]] = None) -> list[str]:
    """The texts a JSON-LD value holds: a string, a person's or a thing's name or address, or a list of them.

    A node that only points at another of the page's graph (``{"@id": "#author"}``) is read as that node.
    """
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (str, int, float)):
        text = re.sub(r"\s+", " ", str(value)).strip()
        return [text] if text else []
    if isinstance(value, dict):
        target = (index or {}).get(str(value.get("@id") or ""))
        if target is not None and target is not value and not value.get("name"):
            value = target
        for key in ("name", "url", "contentUrl", "@id", "text", "@value"):
            if value.get(key):
                return _text(value[key])
        return []
    if isinstance(value, list):
        return [text for item in value for text in _text(item, index)]
    return []


def _lookup(item: dict[str, Any], path: str) -> Any:
    head, _, rest = path.partition(".")
    value = item.get(head)
    if not rest:
        return value
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and entry.get(rest) not in (None, "", []):
                return entry[rest]
        return None
    return value.get(rest) if isinstance(value, dict) else None


def is_article_item(item: dict[str, Any]) -> bool:
    """Whether a JSON-LD item is an article, a posting, a report or a page: what a page is about."""
    kinds = item.get("@type")
    names = kinds if isinstance(kinds, list) else [kinds]
    return any(
        isinstance(kind, str) and any(mark in kind.lower() for mark in _ARTICLE_TYPES)
        for kind in names
    )


def _ordered(json_ld: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    if field not in _ARTICLE_FIELDS:
        return json_ld
    return [item for item in json_ld if is_article_item(item)] + [
        item for item in json_ld if not is_article_item(item)
    ]


def _from_json_ld(
    soup: BeautifulSoup, json_ld: list[dict[str, Any]], field: str, base_url: str
) -> Optional[Found]:
    index = {str(item["@id"]): item for item in json_ld if item.get("@id")}
    for item in _ordered(json_ld, field):
        for path in _JSON_LD.get(field, ()):
            values = _text(_lookup(item, path), index)
            if values:
                return Found(values, "json_ld", path, ", ".join(values))
    return None


def _meta_tags(soup: BeautifulSoup, attribute: str, value: str) -> list[str]:
    tags = soup.find_all("meta", attrs={attribute: value})
    return [re.sub(r"\s+", " ", tag["content"]).strip() for tag in tags if tag.get("content")]


def _from_meta(
    soup: BeautifulSoup, json_ld: list[dict[str, Any]], field: str, base_url: str
) -> Optional[Found]:
    for attribute, value in _META.get(field, ()):
        values = [text for text in _meta_tags(soup, attribute, value) if text]
        if values:
            return Found(values, "meta", f"meta[{attribute}={value}]", ", ".join(values))
    return None


def _from_html(
    soup: BeautifulSoup, json_ld: list[dict[str, Any]], field: str, base_url: str
) -> Optional[Found]:
    finder = _HTML_FINDERS.get(field)
    return finder(soup) if finder is not None else None


def _found_text(node: Optional[Tag], selector: str, limit: int = 0) -> Optional[Found]:
    if node is None:
        return None
    text = element_text(node)
    if not text or (limit and len(text) > limit):
        return None
    return Found([text], "html", selector, text)


def _found_attribute(node: Optional[Tag], attribute: str, selector: str) -> Optional[Found]:
    if node is None or not node.get(attribute):
        return None
    text = str(node[attribute]).strip()
    return Found([text], "html", selector, text) if text else None


def _content_root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    return soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"}) or soup


def _html_headline(soup: BeautifulSoup) -> Optional[Found]:
    root = _content_root(soup)
    return _found_text(root.find("h1") or soup.find("h1"), "h1", 300) or _found_text(
        soup.find("title"), "title", 300
    )


def _html_title(soup: BeautifulSoup) -> Optional[Found]:
    return _found_text(soup.find("title"), "title", 300) or _found_text(soup.find("h1"), "h1", 300)


def _html_author(soup: BeautifulSoup) -> Optional[Found]:
    for selector in (
        "[rel~=author]",
        "[itemprop=author] [itemprop=name]",
        "[itemprop=author]",
        ".byline",
        ".author",
        "[class*=byline]",
    ):
        found = _found_text(soup.select_one(selector), selector, 120)
        if found is not None:
            return found
    return None


def _html_moment(soup: BeautifulSoup, field: str) -> Optional[Found]:
    prop = "datePublished" if field == "published" else "dateModified"
    for selector, attribute in (
        (f"time[itemprop={prop}][datetime]", "datetime"),
        (f"[itemprop={prop}][content]", "content"),
        (f"[itemprop={prop}][datetime]", "datetime"),
    ):
        found = _found_attribute(soup.select_one(selector), attribute, selector)
        if found is not None:
            return found
    if field == "published":
        for selector in ("time[pubdate][datetime]", "time[datetime]"):
            node = _content_root(soup).select_one(selector) or soup.select_one(selector)
            found = _found_attribute(node, "datetime", selector)
            if found is not None:
                return found
    return None


def _html_language(soup: BeautifulSoup) -> Optional[Found]:
    return _found_attribute(soup.find("html"), "lang", "html[lang]")


def _html_url(soup: BeautifulSoup) -> Optional[Found]:
    return _found_attribute(soup.select_one("link[rel=canonical]"), "href", "link[rel=canonical]")


def _is_content(node: Tag, root: Tag | BeautifulSoup) -> bool:
    """Whether nothing between `node` and the content's root is page furniture.

    Only the elements under the root count, and a mark only as a whole part of a class or
    an id ("article-toc", not "vector-toc-available" on Wikipedia's <html>).
    """
    for parent in [node, *node.parents]:
        if parent is root or not isinstance(parent, Tag):
            break
        if parent.name in _NOT_CONTENT:
            return False
        names = [*(parent.get("class") or []), str(parent.get("id") or "")]
        # "CarouselSlide-infoDescription" is made of carousel, slide, info and description.
        parts = {
            part.lower()
            for name in names
            for part in re.split(r"[-_\s]+|(?<=[a-z])(?=[A-Z])", name)
            if part
        }
        if parts & _NOT_CONTENT_MARKS:
            return False
    return True


def _html_lead(soup: BeautifulSoup) -> Optional[Found]:
    root = _content_root(soup)
    for paragraph in root.find_all("p"):
        if not _is_content(paragraph, root):
            continue
        text = element_text(paragraph)
        if len(text.split()) >= _LEAD_MIN_WORDS:
            return Found([text], "html", "p", text)
    return None


def _html_body(soup: BeautifulSoup) -> Optional[Found]:
    root = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"})
    found = _found_text(root, root.name if root is not None else "body")
    return found


_HTML_FINDERS = {
    "headline": _html_headline,
    "title": _html_title,
    "author": _html_author,
    "published": lambda soup: _html_moment(soup, "published"),
    "modified": lambda soup: _html_moment(soup, "modified"),
    "language": _html_language,
    "url": _html_url,
    "lead": _html_lead,
    "body": _html_body,
}


def _find_unknown(soup: BeautifulSoup, json_ld: list[dict[str, Any]], name: str) -> Optional[Found]:
    """A name this module does not know, read under that name alone (its camelCase too)."""
    keys = list(dict.fromkeys((name, _camel(name))))
    for item in json_ld:
        for key in keys:
            values = _text(item.get(key))
            if values:
                return _one_of_each(Found(values, "json_ld", key, ", ".join(values)))
    for key in keys:
        for attribute in ("name", "property", "itemprop"):
            values = _meta_tags(soup, attribute, key)
            if values:
                return _one_of_each(
                    Found(values, "meta", f"meta[{attribute}={key}]", ", ".join(values))
                )
    return None


def _split(values: Iterable[str]) -> list[str]:
    parts = [part.strip() for value in values for part in value.split(",")]
    return list(dict.fromkeys(part for part in parts if part))


def _without_by(value: str) -> str:
    return re.sub(r"^by\s+", "", value, flags=re.IGNORECASE).strip()
