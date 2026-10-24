"""Dates you can trust: every signal a page or a result gives, weighed together.

A page says when it was published in many places, and they do not always agree: the JSON-LD
``datePublished``, ``article:published_time`` and the other meta tags, a ``<time datetime>``, a date
printed near the headline, the address itself. Reading one of them and calling it *the* date is how a
tool ends with a null (the first place it looked had none) or with a September date on an October
story (the first place it looked was the old one).

This module reads all of them as :class:`DateSignal` s, each with the trust it deserves alone, and
:func:`resolve` weighs them together:

- signals that fall within a day of each other agree, and agreeing signals raise confidence
  (``1 - (1 - a)(1 - b)...``: two independent witnesses are more than one);
- the heaviest agreeing group is the page's ``date``; the signals outside it are ``date_conflicts``
  (each with its source and its value), and they lower the confidence of the date;
- a date that only the address or only the visible text gives stays under ``LOW_CONFIDENCE``: it is
  said, never trusted;
- ``dateModified``, ``article:modified_time`` and ``og:updated_time`` are *modified* signals: they
  answer ``date_modified`` and never stand in for the published date.

Nothing here reads the network or the clock but ``today``, which a caller may pin.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from web_scraper.web_tools.fields import element_text, is_article_item, normalize_date

# A ``date_confidence`` under this is low: the date is a guess (an address, a printed line), and
# ``strict_dates`` drops it.
LOW_CONFIDENCE = 0.5
# Two signals further apart than this many days disagree.
DISAGREEMENT_DAYS = 1
# How many disagreeing signals an answer lists.
MAX_CONFLICTS = 5
_CEILING = 0.99
_EARLIEST = date(1990, 1, 1)
_PLACEHOLDERS = frozenset({"1970-01-01", "1900-01-01", "0001-01-01", "1899-12-30", "2000-01-01"})

# --- the text of an address, a snippet or a title -------------------------------------------------

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_PATTERN = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
# A day in an address (/2026/10/20/ or -2026-10-20) and a month (/2026/10/).
PATH_DAY_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?!\d)"
)
_PATH_MONTH_RE = re.compile(r"/((?:19|20)\d{2})/(0[1-9]|1[0-2])(?=/|$)")
# A day in words: "Oct 9, 2026", "9 October 2026", "2026-10-09"; a month: "October 2026".
_TEXT_MONTH_DAY_RE = re.compile(
    rf"\b{_MONTH_PATTERN}\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE
)
_TEXT_DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})\s+{_MONTH_PATTERN}\.?,?\s+(\d{{4}})\b", re.IGNORECASE
)
_TEXT_ISO_DAY_RE = re.compile(r"\b((?:19|20)\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_TEXT_MONTH_RE = re.compile(rf"\b{_MONTH_PATTERN}\.?\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)
# 23.10.2026, as Finnish, Turkish and German pages write it: day first, dots between.
_TEXT_DOTTED_RE = re.compile(
    r"(?<![\d.])(0?[1-9]|[12]\d|3[01])\.(0?[1-9]|1[0-2])\.((?:19|20)\d{2})(?![\d.])"
)
# A provider's own note of age at the head of a snippet: "3 days ago ·", "Yesterday —".
_SNIPPET_AGO_RE = re.compile(r"^\s*(\d{1,3})\s+(minute|hour|day|week)s?\s+ago\b", re.IGNORECASE)
_SNIPPET_YESTERDAY_RE = re.compile(r"^\s*yesterday\b", re.IGNORECASE)


def _month_number(name: str) -> int:
    return next(
        index
        for index, month in enumerate(_MONTH_NAMES, start=1)
        if month.startswith(name[:3].lower())
    )


def _day(year: str, month: int, day: str, today: date) -> Optional[str]:
    try:
        found = date(int(year), month, int(day))
    except ValueError:
        return None
    return found.isoformat() if found <= today + timedelta(days=1) else None


def _month(year: str, month: int, today: date) -> Optional[str]:
    if (int(year), month) > (today.year, today.month):
        return None
    return f"{int(year):04d}-{month:02d}"


def _day_in_text(text: str, today: date) -> Optional[str]:
    for match in _TEXT_MONTH_DAY_RE.finditer(text):
        found = _day(match.group(3), _month_number(match.group(1)), match.group(2), today)
        if found:
            return found
    for match in _TEXT_DAY_MONTH_RE.finditer(text):
        found = _day(match.group(3), _month_number(match.group(2)), match.group(1), today)
        if found:
            return found
    for match in _TEXT_ISO_DAY_RE.finditer(text):
        found = _day(match.group(1), int(match.group(2)), match.group(3), today)
        if found:
            return found
    return None


def _month_in_text(text: str, today: date) -> Optional[str]:
    for match in _TEXT_MONTH_RE.finditer(text):
        found = _month(match.group(2), _month_number(match.group(1)), today)
        if found:
            return found
    return None


def infer_publication_date(
    title: str,
    snippet: str,
    url: str,
    *,
    today: Optional[date] = None,
) -> tuple[Optional[str], Optional[str]]:
    """A result's date, ``YYYY-MM-DD`` or ``YYYY-MM``, and where it was read: url, snippet or title.

    The address's own date comes first (``/2026/10/20/``), then the snippet's
    (a provider's "3 days ago", or a date in words), then the title's. A date
    after tomorrow is no publication date. A title's "October 2026" is weaker
    evidence than an address's day: ``date_source`` says which it was.
    """
    now = today or date.today()
    path = urlsplit(url or "").path
    for match in PATH_DAY_RE.finditer(path):
        found = _day(match.group(1), int(match.group(2)), match.group(3), now)
        if found:
            return found, "url"
    for match in _PATH_MONTH_RE.finditer(path):
        found = _month(match.group(1), int(match.group(2)), now)
        if found:
            return found, "url"
    ago = _SNIPPET_AGO_RE.match(snippet or "")
    if ago:
        amount, unit = int(ago.group(1)), ago.group(2).lower()
        days = {"minute": 0, "hour": 0, "day": amount, "week": 7 * amount}[unit]
        return (now - timedelta(days=days)).isoformat(), "snippet"
    if _SNIPPET_YESTERDAY_RE.match(snippet or ""):
        return (now - timedelta(days=1)).isoformat(), "snippet"
    for text, source in ((snippet, "snippet"), (title, "title")):
        found = _day_in_text(text or "", now)
        if found:
            return found, source
    for text, source in ((snippet, "snippet"), (title, "title")):
        found = _month_in_text(text or "", now)
        if found:
            return found, source
    return None, None


# --- signals ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DateSignal:
    """One place that gave a date: where, what, and how far it is trusted standing alone.

    *moment* is ISO 8601 as the signal gave it: a day, a day with a time and zone, or a month
    (``YYYY-MM``). *role* is ``published`` or ``modified``.
    """

    source: str
    moment: str
    weight: float
    role: str = "published"

    @property
    def precise(self) -> bool:
        """A day (or more), not a month."""
        return len(self.moment) > 7

    @property
    def first(self) -> date:
        if not self.precise:
            return date(int(self.moment[:4]), int(self.moment[5:7]), 1)
        return date.fromisoformat(self.moment[:10])

    @property
    def last(self) -> date:
        if not self.precise:
            year, month = int(self.moment[:4]), int(self.moment[5:7])
            return date(year, month, monthrange(year, month)[1])
        return date.fromisoformat(self.moment[:10])

    @property
    def label(self) -> str:
        """``YYYY-MM-DD``, or ``YYYY-MM`` for a month."""
        return self.moment[:10] if self.precise else self.moment[:7]


_MONTH_MOMENT_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")


def make_signal(
    source: str,
    text: Any,
    weight: float,
    *,
    role: str = "published",
    today: Optional[date] = None,
) -> Optional[DateSignal]:
    """A signal from a date as a page wrote it; None when it is no date, or no date it could be.

    A date after tomorrow is no publication date, nor is one before 1990 or a placeholder such as
    ``1970-01-01``.
    """
    if text is None or isinstance(text, bool):
        return None
    raw = re.sub(r"\s+", " ", str(text)).strip()
    if not raw:
        return None
    if _MONTH_MOMENT_RE.fullmatch(raw):
        moment = raw
    else:
        moment = normalize_date(raw) or ""
        if not moment or not re.match(r"\d{4}-\d{2}-\d{2}", moment):
            return None
    signal = DateSignal(source, moment, weight, role)
    try:
        first = signal.first
    except ValueError:
        return None
    now = today or date.today()
    if first < _EARLIEST or first > now + timedelta(days=1) or signal.label in _PLACEHOLDERS:
        return None
    return signal


def _gap(left: DateSignal, right: DateSignal) -> int:
    """Days between two signals' spans; 0 when they overlap."""
    return max(0, (max(left.first, right.first) - min(left.last, right.last)).days)


def agree(left: DateSignal, right: DateSignal) -> bool:
    return _gap(left, right) <= DISAGREEMENT_DAYS


# --- the weighing ----------------------------------------------------------------------------------


@dataclass
class DateResolution:
    """What the signals of a page (or of a result) come to.

    ``date`` is the published day (``YYYY-MM-DD``, a month when nothing is more exact), ``moment``
    the winning signal's own ISO 8601 (with a time when it gave one). ``date_confidence`` is 0 when
    there is no date.
    """

    date: Optional[str] = None
    date_source: Optional[str] = None
    date_confidence: float = 0.0
    date_conflicts: list[dict[str, str]] = field(default_factory=list)
    date_modified: Optional[str] = None
    date_modified_source: Optional[str] = None
    modified_moment: Optional[str] = None
    moment: Optional[str] = None
    signals: list[DateSignal] = field(default_factory=list)

    @property
    def low(self) -> bool:
        """No date, or one that is only a guess."""
        return self.date is None or self.date_confidence < LOW_CONFIDENCE

    def fields(self) -> dict[str, Any]:
        """The answer's date fields. ``date_modified`` is there only when a page gave one."""
        payload: dict[str, Any] = {
            "date": self.date,
            "date_source": self.date_source,
            "date_confidence": self.date_confidence,
            "date_conflicts": [dict(item) for item in self.date_conflicts],
        }
        if self.date_modified:
            payload["date_modified"] = self.date_modified
        return payload


def _combined(weights: Iterable[float]) -> float:
    """Agreeing witnesses: ``1 - (1 - a)(1 - b)...``, never certain."""
    remainder = 1.0
    for weight in weights:
        remainder *= 1.0 - weight
    return min(1.0 - remainder, _CEILING)


def resolve(
    signals: Iterable[DateSignal], *, prefer: Optional[str] = None, today: Optional[date] = None
) -> DateResolution:
    """Weigh signals into a date, its confidence, its source and the signals that disagree.

    With *prefer* (a ``YYYY-MM-DD`` or ``YYYY-MM`` already chosen by other means), the group that
    holds it is the winner whatever the weights say: the confidence and the conflicts are then
    those of a date a caller already has.
    """
    gathered = list(signals)
    published = [signal for signal in gathered if signal.role == "published"]
    modified = [signal for signal in gathered if signal.role == "modified"]
    resolution = DateResolution(signals=gathered)
    if modified:
        best_modified = max(modified, key=lambda signal: signal.weight)
        resolution.date_modified = best_modified.label
        resolution.date_modified_source = best_modified.source
        resolution.modified_moment = best_modified.moment
    if not published:
        return resolution

    groups: list[list[DateSignal]] = []
    for signal in sorted(published, key=lambda item: -item.weight):
        for group in groups:
            if agree(group[0], signal):
                group.append(signal)
                break
        else:
            groups.append([signal])

    def weight_of(group: list[DateSignal]) -> float:
        return _combined(signal.weight for signal in group)

    winner: Optional[list[DateSignal]] = None
    if prefer:
        wanted = make_signal("prefer", prefer, 1.0, today=today)
        if wanted is not None:
            winner = next((group for group in groups if agree(group[0], wanted)), None)
    if winner is None:
        # The heaviest group; of equal weights the earlier date: an update is stamped later than
        # the story it updates, not earlier.
        winner = max(
            groups, key=lambda group: (round(weight_of(group), 6), -group[0].first.toordinal())
        )
    precise = [signal for signal in winner if signal.precise]
    chosen = max(precise or winner, key=lambda signal: signal.weight)

    confidence = weight_of(winner)
    dissent = [group for group in groups if group is not winner]
    for group in dissent:
        confidence *= 1.0 - 0.5 * weight_of(group)
    resolution.date = chosen.label
    resolution.date_source = chosen.source
    resolution.moment = chosen.moment
    resolution.date_confidence = round(confidence, 2)
    resolution.date_conflicts = [
        {"source": signal.source, "value": signal.moment}
        for signal in sorted(
            (signal for group in dissent for signal in group), key=lambda item: -item.weight
        )[:MAX_CONFLICTS]
    ]
    return resolution


# --- what a result shows ---------------------------------------------------------------------------

_URL_DAY_WEIGHT = 0.35
_URL_MONTH_WEIGHT = 0.20
_SNIPPET_DAY_WEIGHT = 0.35
_SNIPPET_AGO_WEIGHT = 0.30
_TITLE_DAY_WEIGHT = 0.25
_MONTH_WEIGHT = 0.20
_PROVIDER_WEIGHT = 0.45


def text_signals(
    title: str,
    snippet: str,
    url: str,
    *,
    provider_date: str = "",
    today: Optional[date] = None,
) -> list[DateSignal]:
    """The weak dates a result shows without being read: its address, snippet and title.

    A date the provider gave as a field (Brave's ``page_age``) comes too. Every one of them is
    low alone; they matter when they agree, and when they do not.
    """
    now = today or date.today()
    signals: list[DateSignal] = []

    def add(source: str, moment: Optional[str], weight: float) -> None:
        if moment:
            made = make_signal(source, moment, weight, today=now)
            if made is not None:
                signals.append(made)

    path = urlsplit(url or "").path
    url_day = next(
        (
            found
            for match in PATH_DAY_RE.finditer(path)
            if (found := _day(match.group(1), int(match.group(2)), match.group(3), now))
        ),
        None,
    )
    if url_day:
        add("url", url_day, _URL_DAY_WEIGHT)
    else:
        url_month = next(
            (
                found
                for match in _PATH_MONTH_RE.finditer(path)
                if (found := _month(match.group(1), int(match.group(2)), now))
            ),
            None,
        )
        add("url", url_month, _URL_MONTH_WEIGHT)

    ago = _SNIPPET_AGO_RE.match(snippet or "")
    if ago:
        amount, unit = int(ago.group(1)), ago.group(2).lower()
        days = {"minute": 0, "hour": 0, "day": amount, "week": 7 * amount}[unit]
        add("snippet", (now - timedelta(days=days)).isoformat(), _SNIPPET_AGO_WEIGHT)
    elif _SNIPPET_YESTERDAY_RE.match(snippet or ""):
        add("snippet", (now - timedelta(days=1)).isoformat(), _SNIPPET_AGO_WEIGHT)
    else:
        snippet_day = _day_in_text(snippet or "", now)
        if snippet_day:
            add("snippet", snippet_day, _SNIPPET_DAY_WEIGHT)
        else:
            add("snippet", _month_in_text(snippet or "", now), _MONTH_WEIGHT)

    title_day = _day_in_text(title or "", now)
    if title_day:
        add("title", title_day, _TITLE_DAY_WEIGHT)
    else:
        add("title", _month_in_text(title or "", now), _MONTH_WEIGHT)

    if provider_date:
        add("provider", normalize_date(provider_date) or provider_date, _PROVIDER_WEIGHT)
    return signals


def result_date(
    title: str,
    snippet: str,
    url: str,
    *,
    provider_date: str = "",
    today: Optional[date] = None,
) -> tuple[Optional[str], Optional[str]]:
    """A result's date and where it was read: ``infer_publication_date``, and a date the provider gave.

    An address's own day stands before everything; a date the provider gave as a field of its
    own comes before the words of a snippet or a title.
    """
    published, source = infer_publication_date(title, snippet, url, today=today)
    if provider_date and not (source == "url" and published and len(published) == 10):
        made = make_signal("provider", provider_date, _PROVIDER_WEIGHT, today=today)
        if made is not None:
            return made.label, "provider"
    return published, source


def assess(
    published: Optional[str], signals: Sequence[DateSignal], *, today: Optional[date] = None
) -> DateResolution:
    """How far a date chosen elsewhere (a result's) is to be trusted, against the signals around it."""
    if not published:
        return DateResolution()
    resolution = resolve(signals, prefer=published, today=today)
    return resolution if resolution.date else DateResolution()


# --- what a page shows -----------------------------------------------------------------------------

# Meta tags (``property``, ``name`` or ``itemprop``, compared in lower case) and how far each is trusted.
_META_PUBLISHED: dict[str, float] = {
    "article:published_time": 0.75,
    "og:article:published_time": 0.75,
    "og:published_time": 0.75,
    "datepublished": 0.75,
    "parsely-pub-date": 0.65,
    "sailthru.date": 0.60,
    "publish-date": 0.60,
    "publishdate": 0.60,
    "pubdate": 0.60,
    "article.published": 0.60,
    "citation_publication_date": 0.60,
    "dc.date.issued": 0.60,
    "dcterms.issued": 0.60,
    "dc.date": 0.55,
    "dcterms.date": 0.55,
    "dcterms.created": 0.55,
    "citation_date": 0.55,
    "date": 0.50,
}
_META_MODIFIED: dict[str, float] = {
    "article:modified_time": 0.75,
    "og:article:modified_time": 0.75,
    "datemodified": 0.75,
    "og:updated_time": 0.65,
    "dcterms.modified": 0.60,
    "last-modified": 0.50,
}
_JSON_LD_PUBLISHED = (("datePublished", 0.80), ("dateCreated", 0.55), ("uploadDate", 0.55))
_JSON_LD_MODIFIED = (("dateModified", 0.80),)
_TIME_MARKED_WEIGHT = 0.70
_TIME_CONTENT_WEIGHT = 0.50
_TIME_LABELLED_WEIGHT = 0.55
_VISIBLE_WEIGHT = 0.35
_VISIBLE_LABELLED_WEIGHT = 0.40

_FURNITURE_TAGS = frozenset({"nav", "aside", "footer"})
_FURNITURE_ROLES = frozenset({"navigation", "complementary", "contentinfo", "banner"})
_FURNITURE_MARKS = frozenset(
    {
        "related",
        "recommended",
        "sidebar",
        "promo",
        "carousel",
        "trending",
        "popular",
        "newsletter",
        "comment",
        "comments",
        "footer",
        "teaser",
        "widget",
    }
)
_PUBLISHED_MARKS = ("publish", "posted", "entry-date", "post-date", "article-date", "pubdate")
_MODIFIED_MARKS = ("updated", "modified", "revised", "edited")
_MODIFIED_LABEL_RE = re.compile(r"(updated|modified|revised|edited)\b[^\d]{0,16}$", re.IGNORECASE)
_VISIBLE_LABEL_RE = re.compile(
    r"(published|posted|written|released|date)\b[^\d]{0,16}$", re.IGNORECASE
)
_VISIBLE_SHORT_CHARS = 140
_VISIBLE_STOP_CHARS = 260
_VISIBLE_ELEMENTS = 16


def _first_text(value: Any) -> Optional[str]:
    """A JSON-LD date value as text: a string, a number, or an object with ``@value``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        return _first_text(value.get("@value") or value.get("value"))
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return None


def _json_ld_signals(json_ld: Sequence[dict[str, Any]], today: date) -> list[DateSignal]:
    dated = [
        item
        for item in json_ld
        if isinstance(item, dict)
        and any(item.get(key) for key, _ in (*_JSON_LD_PUBLISHED, *_JSON_LD_MODIFIED))
    ]
    # An article's own item first, then the page's (a WebPage); two are enough, a third is a list.
    dated.sort(key=lambda item: not is_article_item(item))
    signals: list[DateSignal] = []
    for position, item in enumerate(dated[:2]):
        kind = item.get("@type")
        label = kind if isinstance(kind, str) else "item"
        for key, weight in _JSON_LD_PUBLISHED:
            made = make_signal(
                f"json_ld:{key}" if position == 0 else f"json_ld:{label}.{key}",
                _first_text(item.get(key)),
                weight if position == 0 else weight - 0.15,
                today=today,
            )
            if made is not None:
                signals.append(made)
        for key, weight in _JSON_LD_MODIFIED:
            made = make_signal(
                f"json_ld:{key}" if position == 0 else f"json_ld:{label}.{key}",
                _first_text(item.get(key)),
                weight if position == 0 else weight - 0.15,
                role="modified",
                today=today,
            )
            if made is not None:
                signals.append(made)
    return signals


def _meta_signals(soup: BeautifulSoup, today: date) -> list[DateSignal]:
    signals: list[DateSignal] = []
    seen: set[str] = set()
    for meta in soup.find_all("meta"):
        content = meta.get("content")
        if not content:
            continue
        for attribute in ("property", "name", "itemprop", "http-equiv"):
            key = str(meta.get(attribute) or "").strip().lower()
            if not key or key in seen:
                continue
            if key in _META_PUBLISHED:
                role, weight = "published", _META_PUBLISHED[key]
            elif key in _META_MODIFIED:
                role, weight = "modified", _META_MODIFIED[key]
            else:
                continue
            made = make_signal(f"meta:{key}", content, weight, role=role, today=today)
            if made is not None:
                seen.add(key)
                signals.append(made)
    return signals


def _parts(node: Tag) -> set[str]:
    names = [*(node.get("class") or []), str(node.get("id") or "")]
    return {
        part.lower()
        for name in names
        for part in re.split(r"[-_\s]+|(?<=[a-z])(?=[A-Z])", name)
        if part
    }


def _in_furniture(node: Tag) -> bool:
    """Whether the element sits in a menu, a sidebar, a footer or a list of other stories."""
    for parent in [node, *node.parents]:
        if not isinstance(parent, Tag):
            break
        if parent.name in _FURNITURE_TAGS or str(parent.get("role") or "") in _FURNITURE_ROLES:
            return True
        if _parts(parent) & _FURNITURE_MARKS:
            return True
    return False


def _marked(node: Tag, marks: tuple[str, ...]) -> bool:
    text = " ".join([*(node.get("class") or []), str(node.get("id") or "")]).lower()
    return any(mark in text for mark in marks)


def _microdata_signals(soup: BeautifulSoup, today: date) -> list[DateSignal]:
    """``itemprop=datePublished`` on a visible element (a ``<time>``, a ``<span content=...>``)."""
    signals: list[DateSignal] = []
    found: set[str] = set()
    for node in soup.find_all(attrs={"itemprop": re.compile(r"^date(Published|Modified)$", re.I)}):
        if node.name == "meta" or _in_furniture(node):
            continue
        prop = str(node.get("itemprop")).lower()
        role = "modified" if prop == "datemodified" else "published"
        if role in found:
            continue
        value = node.get("content") or node.get("datetime") or element_text(node)
        made = make_signal(
            f"{node.name}[itemprop={node.get('itemprop')}]",
            value,
            _TIME_MARKED_WEIGHT,
            role=role,
            today=today,
        )
        if made is not None:
            found.add(role)
            signals.append(made)
    return signals


def _time_signals(soup: BeautifulSoup, today: date) -> list[DateSignal]:
    """The first ``<time datetime>`` of the page's content that says it is a publication time or has none marked.

    A list of other stories, a sidebar and the comments carry ``<time>`` too; one element of
    content is taken, the first, as the byline's.
    """
    signals: list[DateSignal] = []
    took_published = took_modified = False
    for node in soup.find_all("time"):
        value = node.get("datetime")
        if not value or node.get("itemprop") or _in_furniture(node):
            continue
        modified = _marked(node, _MODIFIED_MARKS)
        if modified and took_modified or not modified and took_published:
            continue
        if modified:
            weight, role = _TIME_LABELLED_WEIGHT, "modified"
        elif node.has_attr("pubdate") or _marked(node, _PUBLISHED_MARKS):
            weight, role = _TIME_LABELLED_WEIGHT, "published"
        else:
            weight, role = _TIME_CONTENT_WEIGHT, "published"
        made = make_signal("time[datetime]", value, weight, role=role, today=today)
        if made is None:
            continue
        signals.append(made)
        if modified:
            took_modified = True
        else:
            took_published = True
        if took_modified and took_published:
            break
    return signals


def _main_heading(soup: BeautifulSoup) -> Optional[Tag]:
    for container in (soup.find("article"), soup.find("main"), soup):
        if container is not None:
            heading = container.find("h1")
            if isinstance(heading, Tag):
                return heading
    return None


def _near_headline(heading: Tag) -> list[str]:
    """The short lines around the headline: a byline, a kicker, a printed date.

    A paragraph of the story ends the search: a date inside it is the story's, not the page's.
    """
    lines: list[str] = []
    for sibling in heading.find_previous_siblings(limit=3):
        text = element_text(sibling) if isinstance(sibling, Tag) else ""
        if text and len(text) <= _VISIBLE_SHORT_CHARS:
            lines.append(text)
    for node in heading.find_all_next(limit=_VISIBLE_ELEMENTS):
        if not isinstance(node, Tag) or node.name in {"script", "style"}:
            continue
        text = element_text(node)
        if len(text) > _VISIBLE_STOP_CHARS:
            if node.name in {"p", "article", "main", "section"}:
                break
            continue
        if text and len(text) <= _VISIBLE_SHORT_CHARS and text not in lines:
            lines.append(text)
    return lines


def _printed_dates(text: str, today: date) -> list[tuple[str, int]]:
    """Dates in a line of text, as ``(YYYY-MM-DD, offset)``, in the order they are written."""
    found: list[tuple[str, int]] = []
    for pattern, order in (
        (_TEXT_MONTH_DAY_RE, "mdy"),
        (_TEXT_DAY_MONTH_RE, "dmy"),
        (_TEXT_ISO_DAY_RE, "ymd"),
        (_TEXT_DOTTED_RE, "dotted"),
    ):
        for match in pattern.finditer(text):
            if order == "mdy":
                day = _day(match.group(3), _month_number(match.group(1)), match.group(2), today)
            elif order == "dmy":
                day = _day(match.group(3), _month_number(match.group(2)), match.group(1), today)
            elif order == "ymd":
                day = _day(match.group(1), int(match.group(2)), match.group(3), today)
            else:
                day = _day(match.group(3), int(match.group(2)), match.group(1), today)
            if day:
                found.append((day, match.start()))
    return sorted(found, key=lambda item: item[1])


def _visible_signals(soup: BeautifulSoup, today: date) -> list[DateSignal]:
    heading = _main_heading(soup)
    if heading is None:
        return []
    signals: list[DateSignal] = []
    took_published = took_modified = False
    for line in _near_headline(heading):
        for day, offset in _printed_dates(line, today):
            before = line[max(0, offset - 24) : offset]
            if _MODIFIED_LABEL_RE.search(before):
                if took_modified:
                    continue
                made = make_signal("visible", day, _VISIBLE_WEIGHT, role="modified", today=today)
                took_modified = took_modified or made is not None
            else:
                if took_published:
                    continue
                labelled = bool(_VISIBLE_LABEL_RE.search(before))
                made = make_signal(
                    "visible",
                    day,
                    _VISIBLE_LABELLED_WEIGHT if labelled else _VISIBLE_WEIGHT,
                    today=today,
                )
                took_published = took_published or made is not None
            if made is not None:
                signals.append(made)
        if took_published and took_modified:
            break
    return signals


def page_signals(
    soup: BeautifulSoup,
    json_ld: Sequence[dict[str, Any]],
    url: str,
    *,
    today: Optional[date] = None,
) -> list[DateSignal]:
    """Every date signal a page has: its structured data, meta tags, ``<time>`` elements, the
    date printed near its headline, and its address."""
    now = today or date.today()
    signals = _json_ld_signals(json_ld, now)
    signals += _meta_signals(soup, now)
    signals += _microdata_signals(soup, now)
    signals += _time_signals(soup, now)
    signals += _visible_signals(soup, now)
    signals += [signal for signal in text_signals("", "", url, today=now) if signal.source == "url"]
    return signals


def resolve_page(
    soup: BeautifulSoup,
    json_ld: Sequence[dict[str, Any]],
    url: str,
    *,
    today: Optional[date] = None,
) -> DateResolution:
    """The date of a parsed page, from all its signals."""
    return resolve(page_signals(soup, json_ld, url, today=today), today=today)
