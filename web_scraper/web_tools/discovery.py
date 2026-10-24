"""Multi-angle discovery, source typing, and diversity-aware ranking.

A search provider answers one query per call. General discovery needs more than
the top hits of a single phrasing: current material, historical background, and
different kinds of source. The service asks a small set of query angles and
merges them here with deterministic, explainable ranking instead of trusting a
provider's order.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import unquote, urlsplit

from web_scraper.web_tools.dates import (
    PATH_DAY_RE,
    DateResolution,
    assess,
    result_date,
    text_signals,
)
from web_scraper.web_tools.dates import infer_publication_date as infer_publication_date
from web_scraper.web_tools.search import SearchResult
from web_scraper.web_tools.sources import (
    BLOG_HOSTS,
    DEFAULT_RULES,
    FIRST_PARTY_SEGMENTS,
    FIRST_PARTY_SUBDOMAINS,
    GENERIC_NAMES,
    NEWS_HOSTS,
    PRESS_RELEASE_HOSTS,
    SOCIAL_HOSTS,
    SOCIAL_ON_NEWS_HOSTS,
    SourceRules,
    registrable_label,
    split_host,
)

DEPTHS = ("fast", "discover", "deep")
ANGLE_ORDER = {"base": 0, "current": 1, "background": 2, "primary": 3}
ANGLE_WEIGHTS = {"base": 1.0, "current": 0.9, "background": 0.75, "primary": 0.8}

_RRF_K = 10.0
_BM25_K1 = 1.2
_BM25_FIELDS = (
    ("title", 3.0, 0.5),
    ("snippet", 1.2, 0.75),
    ("url", 0.5, 0.2),
)
_NEAR_DUPLICATE_THRESHOLD = 0.55
_MMR_PENALTY = 0.35


def _normalise_token(token: str) -> str:
    folded = unicodedata.normalize("NFKD", token.casefold())
    without_marks = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    return without_marks.replace("ı", "i")


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "best",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "vs",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "bir",
        "bu",
        "da",
        "de",
        "daha",
        "en",
        "için",
        "ile",
        "mi",
        "mı",
        "mu",
        "mü",
        "nasıl",
        "ne",
        "nedir",
        "olan",
        "olarak",
        "şu",
        "çok",
    }
)
_TOKEN_RE = re.compile(r"[^\W_][\w.+-]+", re.UNICODE)
_YEAR_RE = re.compile(r"\b(?:19[89]\d|20[0-4]\d)\b")
_STOPWORD_KEYS = frozenset(_normalise_token(word) for word in _STOPWORDS)

_FRESHNESS_CUES = (
    ("day", ("breaking news", "today", "tonight", "son dakika", "bugün")),
    ("week", ("this week", "past week", "bu hafta", "geçen hafta")),
    ("month", ("this month", "past month", "bu ay", "geçen ay")),
    (
        "year",
        (
            "current",
            "güncel",
            "latest",
            "most recent",
            "recent",
            "recently",
            "son gelişme",
            "son haber",
            "this year",
            "bu yıl",
            "yeni sürüm",
        ),
    ),
)

_TECH_HINTS = frozenset(
    {
        "angular",
        "ansible",
        "anthropic",
        "api",
        "async",
        "asyncio",
        "authentication",
        "aws",
        "azure",
        "claude",
        "cli",
        "compiler",
        "config",
        "css",
        "database",
        "django",
        "docker",
        "embedding",
        "fastapi",
        "flask",
        "framework",
        "gcp",
        "git",
        "github",
        "golang",
        "graphql",
        "grpc",
        "huggingface",
        "html",
        "http",
        "install",
        "javascript",
        "json",
        "jwt",
        "kubernetes",
        "kotlin",
        "langchain",
        "library",
        "llm",
        "mcp",
        "mongodb",
        "mysql",
        "next.js",
        "nextjs",
        "node",
        "nodejs",
        "numpy",
        "oauth",
        "openai",
        "pandas",
        "playwright",
        "postgresql",
        "pydantic",
        "pytest",
        "python",
        "pytorch",
        "rag",
        "react",
        "redis",
        "rust",
        "sdk",
        "selenium",
        "server",
        "sklearn",
        "sql",
        "sqlalchemy",
        "sqlite",
        "swift",
        "tensorflow",
        "terraform",
        "typescript",
        "vue",
        "websocket",
        "yaml",
    }
)

_OFFICIAL_HOSTS = frozenset(
    {
        "ai.google.dev",
        "angular.io",
        "cloud.google.com",
        "developer.apple.com",
        "developer.mozilla.org",
        "docs.anthropic.com",
        "docs.aws.amazon.com",
        "docs.djangoproject.com",
        "docs.docker.com",
        "docs.github.com",
        "docs.npmjs.com",
        "docs.oracle.com",
        "docs.pydantic.dev",
        "docs.python.org",
        "docs.rs",
        "docs.sqlalchemy.org",
        "doc.rust-lang.org",
        "fastapi.tiangolo.com",
        "flask.palletsprojects.com",
        "go.dev",
        "huggingface.co",
        "kotlinlang.org",
        "kubernetes.io",
        "learn.microsoft.com",
        "mongodb.com",
        "nextjs.org",
        "nodejs.org",
        "opensearch.org",
        "pkg.go.dev",
        "platform.openai.com",
        "postgresql.org",
        "python-httpx.org",
        "pytorch.org",
        "python.org",
        "react.dev",
        "redis.io",
        "sqlite.org",
        "tensorflow.org",
        "typescriptlang.org",
        "vuejs.org",
    }
)
# Public bodies whose hosts carry no government suffix: every EU institution
# under europa.eu, the UK government, and Finnish authorities and public
# services (Oamk a public university of applied sciences, ouka.fi the City of
# Oulu). Yle, the public broadcaster, is a news outlet: see sources.py.
_PUBLIC_SECTOR_HOSTS = frozenset(
    {
        "dvv.fi",
        "eduskunta.fi",
        "europa.eu",
        "gov.uk",
        "kela.fi",
        "oamk.fi",
        "ouka.fi",
        "stat.fi",
        "traficom.fi",
        "vero.fi",
    }
)
_GOVERNMENT_SUFFIX = re.compile(r"(?:^|\.)(?:gov|mil|int)$|(?:^|\.)gov\.[a-z]{2}$")
_ACADEMIC_HOSTS = frozenset(
    {
        "aalto.fi",
        "abo.fi",
        "acm.org",
        "arxiv.org",
        "biorxiv.org",
        "helsinki.fi",
        "ieee.org",
        "jstor.org",
        "jyu.fi",
        "lappeenranta.fi",
        "lut.fi",
        "medrxiv.org",
        "nature.com",
        "ncbi.nlm.nih.gov",
        "oulu.fi",
        "pubmed.ncbi.nlm.nih.gov",
        "science.org",
        "springer.com",
        "ssrn.com",
        "tuni.fi",
        "utu.fi",
        "uwasa.fi",
    }
)
_ACADEMIC_SUFFIX = re.compile(r"\.edu$|\.(?:edu|ac)\.[a-z]{2}$")
_REFERENCE_HOSTS = frozenset(
    {
        "britannica.com",
        "ourworldindata.org",
        "pewresearch.org",
        "scholar.google.com",
        "statista.com",
        "wikipedia.org",
    }
)
_CODE_HOSTS = frozenset({"bitbucket.org", "codeberg.org", "github.com", "gitlab.com"})
_COMMUNITY_HOSTS = (
    frozenset(
        {
            "dev.to",
            "discourse.org",
            "hashnode.com",
            "lobste.rs",
            "medium.com",
            "news.ycombinator.com",
            "reddit.com",
            "stackexchange.com",
            "stackoverflow.com",
        }
    )
    | BLOG_HOSTS
)

_AUTHORITY = {
    "official": 0.24,
    "academic": 0.20,
    "news": 0.14,
    "reference": 0.10,
    "code": 0.06,
    "community": 0.04,
    "other": 0.0,
    "press_release": 0.0,
    "social": 0.0,
    # Somebody else's reporting a hop away: below the report itself, whatever the query.
    "aggregator": -0.12,
}
# With primary_first (the default for a news query) the organization's own site and the outlets
# that report it lead: a bonus on top of their authority, but not to a result too old for the
# time the query asks about.
_PRIMARY_FIRST = {"official": 0.12, "news": 0.10, "academic": 0.05}
# A query that names two organizations or more (a deal, a partnership, a comparison) is about
# what holds both: the bonuses of a site's kind count in proportion to the share of the query's
# distinctive words its title and snippet hold, and a result holding more than half of them gets
# up to this, all of it for every one.
_FULL_COVERAGE_BONUS = 0.12
_COVERAGE_MIN_TERMS = 3
# On a news query the kind of source decides more: an outlet's article goes before the page
# of an unknown site of the same freshness (an aggregator that copies its headline), and a
# post, a press release or a blog below it. An outlet gets 0.05 more, not a lot: a fresh page
# still goes before a stale outlet's when the query says how fresh (the date weighs 0.30).
_NEWS_QUERY_ADJUSTMENT = {
    "news": 0.05,
    "community": -0.06,
    "press_release": -0.24,
    "social": -0.30,
}
# The score of a result matching about a third of the query from a single
# angle's middle ranks; below it, a result is listed only when nothing
# relevant scores higher.
MIN_RESULT_SCORE = 0.25
# A result holding fewer than this share of the query's words is a weak match; when every
# result is one, the search says so (``low_relevance_results``). Half: a query of two words
# asks for both, a query of three for most of them.
MIN_RESULT_RELEVANCE = 0.5
# A token this short that mixes letters and digits ("7g3", "b12", "mp3") names a handle, a
# clone or a model as readily as what the query is about: it counts towards a match and
# never makes one.
_CODE_MAX_LENGTH = 3


@dataclass(frozen=True)
class SearchAngle:
    """One query the discovery strategy sends to the provider."""

    name: str
    query: str
    date_filter: Optional[str] = None


@dataclass
class DiscoveryCandidate:
    """A deduplicated result plus the angle ranks that surfaced it."""

    result: SearchResult
    angle_ranks: dict[str, int] = field(default_factory=dict)


def build_search_angles(
    query: str,
    depth: str,
    *,
    max_length: int = 500,
    year: Optional[int] = None,
    explicit_range: bool = False,
) -> list[SearchAngle]:
    """Return the query angles for a depth.

    ``fast`` keeps the caller's phrasing alone. ``discover`` adds the current
    year and a historical/background angle. ``deep`` adds an official or
    primary-source angle. A suffix that would overflow ``max_length`` truncates
    the base rather than dropping the angle. Against an explicit date range
    (``explicit_range``) the year-based angles, the current year and the
    historical one, are not added, and the range replaces the base angle's
    freshness cue: they would ask for what the caller ruled out.
    """
    normalized_depth = (depth or "discover").strip().lower()
    if normalized_depth not in DEPTHS:
        raise ValueError(f"Unknown search depth: {depth}")

    base = " ".join((query or "").split())
    if not base:
        raise ValueError("Query must not be empty")

    current_year = year or datetime.now().year
    intent_filter = None if explicit_range else detect_date_filter(base, year=current_year)
    angles = [SearchAngle("base", base[:max_length].rstrip(), intent_filter)]
    if normalized_depth == "fast":
        return angles

    if not explicit_range:
        if str(current_year) not in base:
            current = _fit_suffix(base, str(current_year), max_length)
            if current and current.casefold() != base.casefold():
                angles.append(SearchAngle("current", current, intent_filter or "year"))

        background = _fit_suffix(base, "history background", max_length)
        if background:
            angles.append(SearchAngle("background", background))

    if normalized_depth == "deep":
        suffix = "official documentation" if _is_technical_query(base) else "primary sources"
        primary = _fit_suffix(base, suffix, max_length)
        if primary:
            angles.append(SearchAngle("primary", primary))

    return angles


def detect_date_filter(query: str, *, year: Optional[int] = None) -> Optional[str]:
    """Map an explicit freshness cue to a provider date filter.

    General queries stay unfiltered: a hard date cut-off would discard useful
    historical material. Only queries that ask for freshness (or name the
    current year) get one.
    """
    normalized = " ".join((query or "").casefold().split())
    if not normalized:
        return None
    for name, cues in _FRESHNESS_CUES:
        if any(_contains_phrase(normalized, cue) for cue in cues):
            return name
    current_year = year or datetime.now().year
    if re.search(rf"(?<!\d){current_year}(?!\d)", normalized):
        return "year"
    return None


def query_tokens(text: str) -> set[str]:
    """Tokenise a query or result for deterministic lexical overlap.

    Turkish letters are folded to their ASCII equivalents so a query written
    with ``İ``/``ı`` matches a result written with ``i`` (and vice versa).
    """
    tokens = {
        _normalise_token(match.group(0).strip(".-+")) for match in _TOKEN_RE.finditer(text or "")
    }
    meaningful = {token for token in tokens if len(token) > 1 and token not in _STOPWORD_KEYS}
    return meaningful or {token for token in tokens if len(token) > 1}


# The alphanumeric words of a text: what lies between spaces, slashes, hyphens, dots and
# underscores.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _word_parts(text: str) -> list[str]:
    return [_normalise_token(part) for part in _WORD_RE.findall(text or "")]


def _text_words(text: str) -> set[str]:
    """The whole words of a title or snippet.

    A joined token ("next.js", "meow-7g3") and each word between its separators
    ("next", "js", "meow", "7g3"), never a piece of a longer word.
    """
    joined = {_normalise_token(match.group(0).strip(".-+")) for match in _TOKEN_RE.finditer(text)}
    return joined | {part for part in _word_parts(text) if len(part) > 1}


def _url_words(url: str) -> set[str]:
    """The words an address spells: its host and its path, not its ids.

    A query string, a numbered segment (``/560087``) and a handle (``/@7g3_na``) are ids
    the site made up; a word in one says nothing about the page, so only alphabetic
    words count.
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return set()
    spelled = unquote(f"{parts.hostname or ''}/{parts.path}")
    return {word for word in _word_parts(spelled) if len(word) > 1 and word.isalpha()}


def _holds(term: str, words: set[str]) -> bool:
    """Whether a query term is one of the whole words, or all the words it joins ("next.js")."""
    if term in words:
        return True
    parts = [part for part in _word_parts(term) if len(part) > 1]
    return len(parts) > 1 and all(part in words for part in parts)


def matched_terms(terms: Iterable[str], title: str, snippet: str, url: str) -> set[str]:
    """The query terms a result holds as whole words in its title, snippet or address."""
    words = _text_words(f"{title} {snippet}") | _url_words(url)
    return {term for term in terms if _holds(term, words)}


def _is_number(token: str) -> bool:
    return not any(character.isalpha() for character in token)


def _is_code(token: str) -> bool:
    return (
        len(token) <= _CODE_MAX_LENGTH
        and any(character.isdigit() for character in token)
        and any(character.isalpha() for character in token)
    )


def distinctive_terms(query: str) -> set[str]:
    """The query's terms that can make a result relevant by themselves.

    Bare numbers (a year, ``9237``, ``3.12``) and short codes (``7g3``, ``b12``) do not
    when the query has words too: a page about something else names them as well. A query
    of nothing else keeps them.
    """
    wanted = query_tokens(query)
    words = {token for token in wanted if not _is_number(token)}
    return {token for token in words if not _is_code(token)} or words or wanted


def relevance_score(query: str, title: str, snippet: str, url: str) -> float:
    """Share of the query's meaningful terms the result holds as whole words."""
    wanted = query_tokens(query)
    if not wanted:
        return 0.0
    return len(matched_terms(wanted, title, snippet, url)) / len(wanted)


def infer_publication_year(
    title: str,
    snippet: str,
    url: str,
    *,
    year_now: Optional[int] = None,
) -> Optional[int]:
    """Infer a publication year from what a provider exposes.

    DuckDuckGo's HTML results carry no structured date, so the newest plausible
    year in the title, snippet, or URL stands in. ``None`` means unknown, not
    old; callers must not treat it as a freshness signal.
    """
    upper = (year_now or datetime.now().year) + 1
    years = [
        int(match.group(0))
        for text in (title, snippet, url)
        for match in _YEAR_RE.finditer(text or "")
        if 1980 <= int(match.group(0)) <= upper
    ]
    return max(years) if years else None


def classify_freshness(
    published: Optional[str],
    year: Optional[int],
    *,
    today: Optional[date] = None,
) -> str:
    """How old a result is, by its date when it shows one: this_week, this_month, this_year, older, undated.

    Unlike ``recency``, which goes by the newest year a result names (this year
    or last is "current"), this reads the day or the month first.
    """
    now = today or date.today()
    if published and len(published) == 10:
        day = date.fromisoformat(published)
        age = (now - day).days
        if age <= 7:
            return "this_week"
        if age <= 31:
            return "this_month"
        return "this_year" if day.year == now.year else "older"
    if published:
        found_year, found_month = (int(part) for part in published.split("-"))
        if (found_year, found_month) == (now.year, now.month):
            return "this_month"
        return "this_year" if found_year == now.year else "older"
    if year is not None:
        return "this_year" if year >= now.year else "older"
    return "undated"


# What a result's address says it is: one story or page (an article), a list of
# them (a section, tag, archive or search page), or a site's front page.
_LISTING_SEGMENTS = frozenset(
    {
        "archive",
        "archives",
        "author",
        "authors",
        "categories",
        "category",
        "hub",
        "search",
        "section",
        "sections",
        "tag",
        "tags",
        "topic",
        "topics",
    }
)
_LISTING_NAMES = frozenset(
    {
        "all",
        "archive",
        "archives",
        "articles",
        "blog",
        "home",
        "index",
        "latest",
        "live",
        "news",
        "stories",
        "updates",
    }
)
_ARTICLE_SEGMENTS = frozenset(
    {"article", "articles", "p", "post", "posts", "stories", "story", "wiki"}
)
_LANGUAGE_ROOT_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_ARTICLE_SLUG_WORDS = 4
# Words that make a slug longer without making it a story's: "ai-news-and-updates".
_SLUG_FILLERS = frozenset(
    {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
)
# How many days a freshness cue reaches back.
_WINDOW_DAYS = {"day": 2, "week": 8, "month": 32}


def classify_page_kind(url: str) -> str:
    """article, listing, front, or page when the address does not tell."""
    path = urlsplit(url or "").path
    names = [segment.lower() for segment in path.split("/") if segment]
    if not names or (
        len(names) == 1 and (_LANGUAGE_ROOT_RE.match(names[0]) or names[0].startswith("index."))
    ):
        return "front"
    last = names[-1].rsplit(".", 1)[0]
    if names[0] in _LISTING_SEGMENTS or all(name.isdigit() for name in names):
        return "listing"
    if last in _LISTING_NAMES:
        return "listing"
    if names[0] in _ARTICLE_SEGMENTS and len(names) >= 2:
        return "article"
    words = [
        word for word in re.split(r"[-_+]+", last) if len(word) > 1 and word not in _SLUG_FILLERS
    ]
    if len(words) >= _ARTICLE_SLUG_WORDS or PATH_DAY_RE.search(path):
        return "article"
    if re.search(r"\d{6,}|[0-9a-f]{12,}", last):
        return "article"
    return "page"


# "News" as the owner's languages and the big ones say it.
_NEWS_WORDS = frozenset(
    {
        "actualites",
        "haber",
        "haberler",
        "headlines",
        "nachrichten",
        "news",
        "noticias",
        "notizie",
        "nyheter",
        "uutiset",
        "uutisia",
        "uutinen",
    }
)


def wants_news(query: str) -> bool:
    """Whether a query asks for news, so articles go before the pages that list them."""
    return detect_date_filter(query) is not None or bool(_NEWS_WORDS & query_tokens(query))


_wants_news = wants_news


def _within_window(published: Optional[str], window: Optional[str], today: date) -> Optional[bool]:
    """Whether a result's date falls in the time a query's freshness cue asks for; None when it cannot tell."""
    if not published or window is None:
        return None
    if window == "year":
        return int(published[:4]) >= today.year
    if len(published) == 10:
        return (today - date.fromisoformat(published)).days <= _WINDOW_DAYS[window]
    found_year, found_month = (int(part) for part in published.split("-"))
    months_back = (today.year - found_year) * 12 + today.month - found_month
    return False if months_back >= 2 or (months_back == 1 and window != "month") else None


def within_date_range(
    published: Optional[str], date_from: Optional[date], date_to: Optional[date]
) -> bool:
    """Whether a result's date is inside a range; a result with no date is kept.

    *published* is ``YYYY-MM-DD`` or ``YYYY-MM``; a month is inside when any of its days is.
    A range may be open on one side.
    """
    if not published or (date_from is None and date_to is None):
        return True
    if len(published) == 10:
        first = last = date.fromisoformat(published)
    else:
        year, month = (int(part) for part in published.split("-"))
        first = date(year, month, 1)
        last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    if date_from is not None and last < date_from:
        return False
    return date_to is None or first <= date_to


def classify_recency(year: Optional[int], *, year_now: Optional[int] = None) -> str:
    """Map an inferred year to current, recent, historical, or unknown."""
    if year is None:
        return "unknown"
    current = year_now or datetime.now().year
    if year >= current - 1:
        return "current"
    if year >= current - 4:
        return "recent"
    return "historical"


def classify_source_type(
    url: str,
    *,
    news: bool = False,
    query: str = "",
    rules: Optional[SourceRules] = None,
) -> str:
    """Classify a result URL: official, academic, news, press_release, social, reference,
    code, community, aggregator or other.

    ``news`` says the query asks for news: a video site such as YouTube is ``social``
    then, and a video of the news is not the story. An organization's own site is ``official``:
    a known first-party domain (``openai.com``, ``cloud.google.com``, ``blog.google``,
    ``apple.com/newsroom``, a table that WEB_TOOLS_OFFICIAL_HOSTS extends; *rules* carries it),
    and, for a query that names the organization, its docs, blog, newsroom or developer pages
    (``docs.stripe.com`` for "stripe webhooks"). A site that collects other outlets' stories is
    ``aggregator``.
    """
    rules = rules or DEFAULT_RULES
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").lower()
    except ValueError:
        return "other"
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "other"

    if (
        _host_matches(host, _OFFICIAL_HOSTS)
        or _host_matches(host, _PUBLIC_SECTOR_HOSTS)
        or _GOVERNMENT_SUFFIX.search(host)
        or rules.is_first_party(host, path)
    ):
        return "official"
    if _host_matches(host, _ACADEMIC_HOSTS) or _ACADEMIC_SUFFIX.search(host):
        return "academic"
    if _host_matches(host, NEWS_HOSTS):
        return "news"
    if _host_matches(host, PRESS_RELEASE_HOSTS):
        return "press_release"
    if _host_matches(host, SOCIAL_HOSTS) or (news and _host_matches(host, SOCIAL_ON_NEWS_HOSTS)):
        return "social"
    if _host_matches(host, _REFERENCE_HOSTS):
        return "reference"
    if _host_matches(host, _CODE_HOSTS) or host.endswith(".github.io"):
        return "code"
    if _host_matches(host, _COMMUNITY_HOSTS):
        return "community"
    if rules.is_aggregator(host, path):
        return "aggregator"
    if query and _site_named_by(host, path, _query_words(query)):
        return "official"
    # A docs-looking path alone does not make a host official: mirrors (mdn.org.cn, runebook.dev) copy another
    # project's documentation. Give them the weight of a reference, below the canonical project's own domain.
    if any(
        segment in path
        for segment in ("/docs/", "/documentation/", "/api/", "/reference/", "/manual/")
    ):
        return "reference"
    return "other"


def _query_words(query: str) -> list[str]:
    return [_normalise_token(word) for word in re.findall(r"[^\W_]+", query or "", re.UNICODE)]


def _names(words: list[str]) -> set[str]:
    """What a query may be calling a site: each of its words, and two next to each other joined."""
    return set(words) | {left + right for left, right in zip(words, words[1:])}


def _site_named_by(host: str, path: str, words: list[str]) -> bool:
    """Whether a query names the organization behind a site and the page is the organization's own.

    The name is the site's registrable label (``stripe`` of ``docs.stripe.com``) when the query
    says it, as a word or two joined (``hugging face``), at least four letters and not a word that
    is only a word (``time``, ``cloud``). The page is the organization speaking: its front page,
    its docs, developers, blog or newsroom (a subdomain or a first path segment), never an
    article that merely sits on the site (``turkiye.ai/turkiye-yapay-zeka-eylem-plani``).
    """
    label = registrable_label(host)
    if len(label) < 4 or label in GENERIC_NAMES or label not in _names(words):
        return False
    subdomain, _ = split_host(host)
    first = subdomain.split(".")[0] if subdomain else ""
    segments = [part for part in path.split("/") if part]
    if first in ("", "www"):
        return not segments or segments[0] in FIRST_PARTY_SEGMENTS
    return first in FIRST_PARTY_SUBDOMAINS


def _field_tokens(text: str) -> list[str]:
    # Split hostnames, paths, and version strings so a query term can match
    # inside ``docs.python.org`` rather than only as the whole hostname.
    return [_normalise_token(token) for token in re.findall(r"[^\W_]+", text or "", re.UNICODE)]


def _bm25_query_terms(query: str) -> list[str]:
    terms = [
        token
        for token in _field_tokens(query)
        if (len(token) > 1 or token.isdigit()) and token not in _STOPWORD_KEYS
    ]
    return list(dict.fromkeys(terms))


def _document_stats(candidate: DiscoveryCandidate) -> _DocumentStats:
    result = candidate.result
    texts = {"title": result.title, "snippet": result.snippet, "url": result.url}
    counts: dict[str, Counter[str]] = {}
    lengths: dict[str, int] = {}
    for field_name, text in texts.items():
        tokens = _field_tokens(text)
        counts[field_name] = Counter(tokens)
        lengths[field_name] = len(tokens)
    return _DocumentStats(counts=counts, lengths=lengths)


def _bm25_raw_scores(
    terms: list[str],
    documents: list[_DocumentStats],
) -> list[float]:
    if not terms or not documents:
        return [0.0] * len(documents)

    total = len(documents)
    document_frequency = {
        term: sum(
            1
            for document in documents
            if any(document.counts[field][term] for field, _, _ in _BM25_FIELDS)
        )
        for term in terms
    }
    average_lengths = {
        field: max(
            1.0,
            sum(document.lengths[field] for document in documents) / total,
        )
        for field, _, _ in _BM25_FIELDS
    }

    scores: list[float] = []
    for document in documents:
        score = 0.0
        for term in terms:
            frequency = document_frequency[term]
            if frequency == 0:
                continue
            idf = math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
            weighted_tf = 0.0
            for field_name, weight, length_norm in _BM25_FIELDS:
                term_frequency = document.counts[field_name][term]
                if not term_frequency:
                    continue
                norm = (
                    1.0
                    - length_norm
                    + length_norm * (document.lengths[field_name] / average_lengths[field_name])
                )
                weighted_tf += weight * term_frequency / norm
            if weighted_tf:
                score += idf * weighted_tf * (_BM25_K1 + 1.0) / (weighted_tf + _BM25_K1)
        scores.append(score)
    return scores


@dataclass
class _DocumentStats:
    counts: dict[str, Counter[str]]
    lengths: dict[str, int]


def annotate_candidates(
    query: str,
    candidates: Iterable[DiscoveryCandidate],
    *,
    year_now: Optional[int] = None,
    today: Optional[date] = None,
    dates: Optional[Mapping[str, DateResolution]] = None,
    rules: Optional[SourceRules] = None,
    primary_first: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """Score a candidate pool, including corpus-relative BM25F.

    Field weighting and document frequency only make sense against the other
    results from the same search, so the whole pool is scored together. *dates* holds, by
    address, the dates found by reading a page (see ``dates.py``); a result with one is dated by
    it and not by its address, snippet or title. *rules* are the tables of first-party sites and
    aggregators (``sources.py``). With *primary_first* (unset: when the query asks for news) the
    organization's own site and the news outlets that report lead.
    """
    items = list(candidates)
    terms = _bm25_query_terms(query)
    raw_scores = _bm25_raw_scores(terms, [_document_stats(item) for item in items])
    # Squash raw BM25 into (0, 1) instead of scaling it to the pool's best match: a pool of
    # only weak matches must not look as strong as a pool with a genuine answer, and one page
    # that spells the whole query does not take all of BM25's weight from the others.
    normalized = [score / (score + 1.0) for score in raw_scores]
    now = today or date.today()
    news = wants_news(query)
    window = detect_date_filter(query, year=now.year)
    facts = _QueryFacts.of(query, news, primary_first, rules or DEFAULT_RULES)
    return [
        _annotate_candidate(
            query,
            item,
            normalized[index],
            year_now or now.year,
            now,
            news,
            window,
            (dates or {}).get(item.result.url),
            facts,
        )
        for index, item in enumerate(items)
    ]


@dataclass(frozen=True)
class _QueryFacts:
    """What the query itself says that scoring needs, found once for the whole pool."""

    rules: SourceRules
    primary_first: bool
    # The query names two organizations or more and has words enough for a page to hold some of them.
    by_coverage: bool
    terms: frozenset[str]

    @classmethod
    def of(
        cls, query: str, news: bool, primary_first: Optional[bool], rules: SourceRules
    ) -> _QueryFacts:
        terms = frozenset(distinctive_terms(query))
        named = _names(_query_words(query)) & rules.organizations()
        return cls(
            rules=rules,
            primary_first=news if primary_first is None else primary_first,
            by_coverage=len(named) >= 2 and len(terms) >= _COVERAGE_MIN_TERMS,
            terms=terms,
        )


def annotate_candidate(
    query: str,
    candidate: DiscoveryCandidate,
    *,
    year_now: Optional[int] = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Score one candidate and expose the signals behind the score."""
    return annotate_candidates(query, [candidate], year_now=year_now, today=today)[0]


def _annotate_candidate(
    query: str,
    candidate: DiscoveryCandidate,
    bm25: float,
    year_now: Optional[int],
    today: date,
    news: bool,
    window: Optional[str],
    resolution: Optional[DateResolution] = None,
    facts: Optional[_QueryFacts] = None,
) -> dict[str, Any]:
    facts = facts or _QueryFacts.of(query, news, None, DEFAULT_RULES)
    result = candidate.result
    url = result.url
    source_type = classify_source_type(url, news=news, query=query, rules=facts.rules)
    year = infer_publication_year(
        result.title,
        result.snippet,
        url,
        year_now=year_now,
    )
    recency = classify_recency(year, year_now=year_now)
    published, date_source = result_date(
        result.title, result.snippet, url, provider_date=result.published, today=today
    )
    assessed = assess(
        published,
        text_signals(
            result.title, result.snippet, url, provider_date=result.published, today=today
        ),
        today=today,
    )
    if resolution is not None and resolution.date:
        published, date_source, assessed = resolution.date, resolution.date_source, resolution
    freshness = classify_freshness(published, year, today=today)
    kind = classify_page_kind(url)
    relevance = relevance_score(query, result.title, result.snippet, url)
    angles = sorted(
        candidate.angle_ranks,
        key=lambda name: (ANGLE_ORDER.get(name, 99), candidate.angle_ranks[name]),
    )

    rrf = sum(
        ANGLE_WEIGHTS.get(name, 0.5) / (_RRF_K + rank)
        for name, rank in candidate.angle_ranks.items()
    )
    # What a site's kind is worth counts in proportion to how much of the query a result's title
    # and snippet hold, for a query about what holds several organizations: the page of one of
    # them is not the story of both.
    coverage = 1.0
    if facts.by_coverage:
        coverage = len(matched_terms(facts.terms, result.title, result.snippet, "")) / len(
            facts.terms
        )
    authority = _AUTHORITY[source_type]
    score = (
        0.45 * bm25
        + 0.10 * relevance
        + 1.8 * rrf
        + (authority * coverage if authority > 0 else authority)
    )
    if facts.by_coverage and coverage > 0.5 and authority >= 0:
        score += _FULL_COVERAGE_BONUS * (coverage - 0.5) / 0.5
    # A query that says how fresh, and a result whose date can answer it: the
    # date decides, not the year it names (a February article is "current" in
    # October by its year, and no news of this week).
    fresh = _within_window(published, window, today)
    if fresh is not None:
        score += 0.10 if fresh else -0.20
    else:
        score += {"current": 0.10, "recent": 0.05}.get(recency, 0.0)
    if facts.primary_first and fresh is not False:
        score += _PRIMARY_FIRST.get(source_type, 0.0) * coverage
    if recency == "historical":
        score += 0.08 if source_type in {"official", "academic", "reference"} else 0.04
    if _has_exact_phrase(query, result.title, result.snippet):
        score += 0.10
    if "current" in candidate.angle_ranks and recency == "current":
        score += 0.05
    if (
        "background" in candidate.angle_ranks
        and recency in {"historical", "unknown"}
        and source_type in {"official", "academic", "reference"}
    ):
        score += 0.05
    if "primary" in candidate.angle_ranks and source_type == "official":
        score += 0.08 * coverage
    # A news query wants the stories, not the front pages and lists that link them.
    if news:
        score += {"article": 0.08, "listing": -0.08, "front": -0.08}.get(kind, 0.0)
        score += _NEWS_QUERY_ADJUSTMENT.get(source_type, 0.0)

    return {
        "title": result.title,
        "url": url,
        "snippet": result.snippet,
        "source": result.source,
        "domain": _domain(url),
        "source_type": source_type,
        "year": year,
        "recency": recency,
        "date": published,
        "date_source": date_source,
        "date_confidence": assessed.date_confidence,
        "date_conflicts": [dict(item) for item in assessed.date_conflicts],
        "age_days": (today - date.fromisoformat(published)).days
        if published and len(published) == 10
        else None,
        "freshness": freshness,
        "kind": kind,
        "angles": angles,
        "relevance": round(relevance, 6),
        "bm25": round(bm25, 6),
        "score": round(score, 6),
    }


def relevant_results(query: str, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The annotated results holding a distinctive term of the query as a whole word."""
    terms = distinctive_terms(query)
    return [
        item for item in items if matched_terms(terms, item["title"], item["snippet"], item["url"])
    ]


def has_relevant_result(query: str, items: Iterable[dict[str, Any]]) -> bool:
    """Whether any annotated result holds a distinctive term of the query as a whole word.

    A bare number or a short code does not count when the query has words too: a year
    or a code such as ``9237`` or ``7g3`` also appears on pages about something else.
    """
    return bool(relevant_results(query, items))


def rank_candidates(
    query: str,
    candidates: Iterable[DiscoveryCandidate],
    limit: int,
    *,
    year_now: Optional[int] = None,
    today: Optional[date] = None,
    dates: Optional[Mapping[str, DateResolution]] = None,
    rules: Optional[SourceRules] = None,
    primary_first: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """Rank candidates by relevance, agreement, authority, and recency, then diversify.

    Results that hold no distinctive term of the query as a whole word are
    dropped, and when nothing relevant is left the list is empty: an empty
    answer is honest, a page of unrelated links is not. Relevant results scoring under
    ``MIN_RESULT_SCORE`` are dropped beside better ones and kept only when no
    relevant result reaches it.
    """
    annotated = annotate_candidates(
        query,
        candidates,
        year_now=year_now,
        today=today,
        dates=dates,
        rules=rules,
        primary_first=primary_first,
    )
    relevant = relevant_results(query, annotated)
    if not relevant:
        return []
    eligible = [item for item in relevant if item["score"] >= MIN_RESULT_SCORE] or relevant

    eligible.sort(
        key=lambda item: (
            -item["score"],
            ANGLE_ORDER.get(item["angles"][0] if item["angles"] else "", 99),
            item["url"],
        )
    )
    return _select_diverse(eligible, limit)


def domain_cap(limit: int) -> int:
    """Maximum results from one domain before diversity relaxation."""
    return max(1, math.ceil(max(limit, 1) / 3))


def build_facets(
    results: list[dict[str, Any]],
    *,
    total_candidates: int,
    limit: int,
) -> dict[str, Any]:
    """Summarise source-type, recency, domain, and angle coverage."""
    angle_counts = Counter(angle for item in results for angle in item.get("angles", []))
    return {
        "source_types": _ordered_counts(item["source_type"] for item in results),
        "recency": _ordered_counts(item["recency"] for item in results),
        "freshness": _ordered_counts(item.get("freshness", "undated") for item in results),
        "kinds": _ordered_counts(item.get("kind", "page") for item in results),
        "domains": _ordered_counts(item["domain"] for item in results),
        "angles": _ordered_counts(angle_counts.elements()),
        "total_candidates": total_candidates,
        "domain_cap": domain_cap(limit),
    }


def _select_diverse(
    ranked: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    remaining = list(ranked)
    selected: list[dict[str, Any]] = []
    selected_tokens: list[set[str]] = []
    per_domain: Counter[str] = Counter()
    per_type: Counter[str] = Counter()
    cap = domain_cap(limit)

    while remaining and len(selected) < limit:
        best_index = _best_index(remaining, per_domain, per_type, cap, selected_tokens)
        if best_index is None:
            best_index = _best_index(remaining, per_domain, per_type, None, selected_tokens)
        if best_index is None:
            break
        chosen = remaining.pop(best_index)
        selected.append(chosen)
        selected_tokens.append(_content_tokens(chosen))
        per_domain[chosen["domain"]] += 1
        per_type[chosen["source_type"]] += 1
    return selected


def _best_index(
    remaining: list[dict[str, Any]],
    per_domain: Counter[str],
    per_type: Counter[str],
    cap: Optional[int],
    selected_tokens: list[set[str]],
) -> Optional[int]:
    best_index: Optional[int] = None
    best_effective: Optional[float] = None
    for index, item in enumerate(remaining):
        if cap is not None and per_domain[item["domain"]] >= cap:
            continue
        type_count = per_type[item["source_type"]]
        effective = item["score"] + (0.04 if type_count == 0 else -0.02 * type_count)
        if selected_tokens:
            similarity = max(_jaccard(_content_tokens(item), chosen) for chosen in selected_tokens)
            excess = max(0.0, similarity - _NEAR_DUPLICATE_THRESHOLD)
            effective -= _MMR_PENALTY * excess / (1.0 - _NEAR_DUPLICATE_THRESHOLD)
        if best_effective is None or effective > best_effective:
            best_index = index
            best_effective = effective
    return best_index


def _content_tokens(item: dict[str, Any]) -> set[str]:
    return query_tokens(f"{item.get('title', '')} {item.get('snippet', '')}")


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _contains_phrase(text: str, phrase: str) -> bool:
    return (
        re.search(
            rf"(?<!\w){re.escape(phrase)}(?!\w)",
            text,
            flags=re.UNICODE,
        )
        is not None
    )


def _fit_suffix(base: str, suffix: str, max_length: int) -> str:
    room = max_length - len(suffix) - 1
    if room < 1:
        return ""
    return f"{base[:room].rstrip()} {suffix}"


def _is_technical_query(query: str) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", query.casefold()) for word in _TECH_HINTS)


def _host_matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _has_exact_phrase(query: str, *texts: str) -> bool:
    needle = " ".join((query or "").casefold().split())
    if not needle:
        return False
    return any(needle in " ".join((text or "").casefold().split()) for text in texts)


def _ordered_counts(values: Iterable[str]) -> dict[str, int]:
    counts = Counter(value for value in values if value)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
