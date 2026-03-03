"""Pure text / JSON / language utilities for the research pipeline.

All functions are stateless module-level callables — no class required.
"""

import json
import re
from typing import Optional

from web_scraper.research.constants import DATE_PATTERNS

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_TURKISH_CHARS: frozenset[str] = frozenset("çğıöşüÇĞİÖŞÜ")
_TURKISH_WORDS: frozenset[str] = frozenset(
    {
        "ve",
        "bir",
        "bu",
        "ile",
        "için",
        "da",
        "de",
        "ne",
        "nedir",
        "nasıl",
        "neden",
        "kaç",
        "hangi",
        "bana",
        "benim",
        "olan",
        "var",
        "yok",
        "ama",
        "veya",
        "gibi",
        "daha",
        "çok",
        "hakkında",
        "haber",
        "haberleri",
        "neler",
        "oldu",
        "yapay",
        "zeka",
        "araştır",
        "anlat",
        "açıkla",
        "karşılaştır",
        "listele",
        "özetle",
        "tablo",
        "son",
        "güncel",
        "bugün",
        "yarın",
        "dün",
        "geçen",
        "gelecek",
        "mı",
        "mi",
        "mu",
        "mü",
        "ki",
        "dan",
        "den",
        "dir",
    }
)


def detect_query_language(query: str) -> str:
    """Return ``'tr'`` for Turkish queries, ``'en'`` otherwise."""
    if any(c in _TURKISH_CHARS for c in query):
        return "tr"
    query_words = set(query.lower().split())
    match_count = len(query_words & _TURKISH_WORDS)
    if match_count >= 2 or (match_count >= 1 and len(query_words) <= 5):
        return "tr"
    return "en"


# ---------------------------------------------------------------------------
# JSON utilities
# ---------------------------------------------------------------------------


def repair_truncated_json(raw_text: str) -> Optional[dict]:
    """Attempt to repair truncated / malformed JSON from LLM output.

    Handles the most common failure mode: token-limit truncation where the
    JSON is cut mid-string, leaving unmatched quotes, brackets, or braces.
    Returns the parsed dict on success, ``None`` on failure.
    """
    try:
        start = raw_text.index("{")
    except ValueError:
        return None

    fragment = raw_text[start:]

    try:
        return json.loads(fragment)
    except json.JSONDecodeError:
        pass

    repaired = fragment.rstrip()

    while repaired.endswith(","):
        repaired = repaired[:-1].rstrip()

    # Close unclosed string literal
    in_string = False
    i = 0
    while i < len(repaired):
        ch = repaired[i]
        if ch == "\\" and in_string:
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        i += 1
    if in_string:
        repaired += '"'

    # Close unclosed brackets / braces in correct nesting order
    stack: list[str] = []
    in_string = False
    i = 0
    while i < len(repaired):
        ch = repaired[i]
        if ch == "\\" and in_string:
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()
        i += 1

    repaired += "".join(reversed(stack))

    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Aggressive fallback: trim back to last comma and re-close
    trim_point = fragment.rstrip().rstrip(",")
    last_comma = trim_point.rfind(",")
    if last_comma > 0:
        aggressive = trim_point[:last_comma]
        stack2: list[str] = []
        in_str2 = False
        j = 0
        while j < len(aggressive):
            ch = aggressive[j]
            if ch == "\\" and in_str2:
                j += 2
                continue
            if ch == '"':
                in_str2 = not in_str2
            elif not in_str2:
                if ch in "{[":
                    stack2.append("}" if ch == "{" else "]")
                elif ch in "}]" and stack2 and stack2[-1] == ch:
                    stack2.pop()
            j += 1
        if in_str2:
            aggressive += '"'
        aggressive += "".join(reversed(stack2))
        try:
            return json.loads(aggressive)
        except json.JSONDecodeError:
            pass

    return None


def extract_json_payload(response_text: str) -> dict:
    """Extract the first JSON object from an LLM response string."""
    try:
        start = response_text.index("{")
        end = response_text.rindex("}") + 1
        return json.loads(response_text[start:end])
    except (ValueError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Text / content utilities
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Direct URL detection
# ---------------------------------------------------------------------------

_URL_RE: re.Pattern = re.compile(r"https?://[^\s,，、;；\"'<>()\[\]{}]+", re.IGNORECASE)

# Keywords (Turkish + English) that signal the user wants subpages crawled too
_SUBPAGE_KEYWORDS: frozenset[str] = frozenset(
    {
        # Turkish
        "alt sayfa",
        "alt sayfalar",
        "alt sayfaları",
        "alt sayfalarını",
        "alt sayfalarini",
        "alt sayfalarında",
        "alt sayfalarinda",
        "alt sayfaları da",
        "alt sayfalarını da",
        "alt sayfalarini da",
        "tüm sayfaları",
        "tüm sayfalarını",
        "bağlantıları",
        "linkleri",
        "sitedeki sayfalar",
        "sitemap",
        "tara",
        "hepsini tara",
        "tüm",
        # English
        "subpages",
        "sub-pages",
        "all pages",
        "crawl",
        "sublinks",
        "internal links",
        "all links",
        "every page",
        "entire site",
        "whole site",
        "follow links",
    }
)


def extract_direct_urls(query: str) -> list[str]:
    """Extract all well-formed http(s) URLs from *query*.

    Returns a list of unique URLs (preserving first-seen order).
    Trailing punctuation (``.``, ``,``, ``!`` etc.) is stripped.
    """
    raw = _URL_RE.findall(query)
    seen: set[str] = set()
    result: list[str] = []
    for url in raw:
        url = url.rstrip(".,;:!?)")
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def has_subpage_crawl_intent(query: str) -> bool:
    """Return ``True`` when the query signals that subpages should also be crawled."""
    lower = query.lower()
    return any(kw in lower for kw in _SUBPAGE_KEYWORDS)


# ---------------------------------------------------------------------------
# Text / content utilities
# ---------------------------------------------------------------------------


def clean_query_text(query: str) -> str:
    """Normalize whitespace so query comparisons stay stable."""
    return " ".join((query or "").split()).strip()


def filter_low_quality_results(results: list, min_chars: int = 100) -> list:
    """Drop results that are errored or have too little content.

    Filters out: error responses, empty content, content shorter than
    *min_chars* characters (bot-protected pages, redirects, spam stubs).
    """
    return [r for r in results if not r.error and r.content and len(r.content.strip()) >= min_chars]


def extract_publication_date(content: str) -> Optional[str]:
    """Scan the first 4 000 characters of scraped content for a publication date.

    Returns an ISO-8601 date string (YYYY-MM-DD or YYYY) if found, else None.
    """
    excerpt = content[:4000]
    for pattern in DATE_PATTERNS:
        match = pattern.search(excerpt)
        if match:
            return match.group(1)
    return None


def extract_date_from_snippet(snippet: str) -> Optional[str]:
    """Extract publication date from a DuckDuckGo search result snippet.

    DDG often includes dates in the snippet like "Jan 15, 2024" or "2024-01-15".
    """
    if not snippet:
        return None

    match = re.search(r"\b(202[0-9]-[01]\d-[0123]\d)\b", snippet)
    if match:
        return match.group(1)

    month_pattern = (
        r"\b(January|February|March|April|May|June|July|August|September"
        r"|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+\d{1,2},?\s+(202[0-9])\b"
    )
    match = re.search(month_pattern, snippet, re.IGNORECASE)
    if match:
        return match.group(2)

    match = re.search(r"\b(202[0-9]|201[9-9])\b", snippet)
    if match:
        return match.group(1)

    return None
