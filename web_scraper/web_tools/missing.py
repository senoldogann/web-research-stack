"""Pages that are not there and say they are: a site that answers every address with a page (soft 404s).

A site that answers any address with its home page, or with its app's shell, gives a 200 for a page
that does not exist, and a reader of the answer takes the fallback for the page. The way to tell is to
ask the same host for an address that cannot exist and compare what comes back with the page in
question. That is what this module is the arithmetic of; the service asks (``WebToolsService``).

The comparison, and why it is this one:

* **The title and the text, read as a reader sees them.** Both documents come through the same loader,
  so a page drawn by scripts is compared as the browser rendered it, not as the HTTP shell was: two
  shells that are the same in HTML are the same after rendering when the site draws one fallback, and
  two pages that differ only after rendering are told apart.
* **The same title is required.** A site that knows a page (a post, a product) writes its own
  ``<title>`` into the HTML; a fallback carries the fallback's. A real page whose script never filled
  it has the text of a shell but its own title, and is not taken for a missing one.
* **The same text, or nearly.** The same hash of the text (whitespace and case aside), or nine in ten
  of the words alike: a fallback that names the address it was asked for ("Nothing at /x") is still the
  fallback.
* **Only a page that may be one is compared** (``suspect``): not the root, which is what a fallback
  shows, and one that does not say it is itself (no ``<link rel=canonical>``, or one that names
  another address) or whose title is the root's. A page that names itself is a page the site
  vouches for, and costs no request.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from web_scraper.web_tools.urls import UnsafeUrlError, canonicalize_url

# Nine words in ten alike, of all the words that either text has, is the same text.
SAME_WORDS = 0.9
# How much of a page's text is compared: the start of it says what the page is.
COMPARED_CHARS = 6_000
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class Fingerprint:
    """A page as the comparison sees it: its title, its text's hash, and the words of its text."""

    title: str
    text_hash: str
    words: frozenset[str]


def _key(text: str) -> str:
    """Text as the comparison reads it: one spacing, no case, compatibility forms folded (NFKC)."""
    return " ".join(unicodedata.normalize("NFKC", text or "").split()).casefold()


def title_key(title: str) -> str:
    """A title as the comparison reads it."""
    return _key(title)


def fingerprint(title: str, text: str) -> Fingerprint:
    """The fingerprint of a page's title and the start of its readable text."""
    key = _key(text[:COMPARED_CHARS])
    return Fingerprint(
        title=_key(title),
        text_hash=hashlib.sha256(key.encode("utf-8")).hexdigest(),
        words=frozenset(_WORD.findall(key)),
    )


def same_page(left: Fingerprint, right: Fingerprint) -> bool:
    """Whether two documents of one host are one page that the site gave twice.

    The same title, and the same text or nine in ten of its words (see the module's note).
    """
    if left.title != right.title:
        return False
    if left.text_hash == right.text_hash:
        return True
    union = left.words | right.words
    return bool(union) and len(left.words & right.words) / len(union) >= SAME_WORDS


def _names_itself(final_url: str, declared_canonical: str) -> bool:
    """Whether a page's own ``<link rel=canonical>`` names the page's own address."""
    if not declared_canonical:
        return False
    try:
        return canonicalize_url(declared_canonical) == canonicalize_url(final_url)
    except UnsafeUrlError:
        return False


def suspect(final_url: str, declared_canonical: str, title: str, root_title: str = "") -> bool:
    """Whether a page may be what its host answers an address that is not there with.

    Not the root of the site (the root is what a fallback shows); and either the page does not say
    that it is itself (no canonical link, or one to another address) or its title is *root_title*,
    the title of the site's root as this session read it (``title_key`` of it; empty when it did not).
    """
    if (urlsplit(final_url).path or "/") == "/":
        return False
    if not _names_itself(final_url, declared_canonical):
        return True
    return bool(root_title) and title_key(title) == root_title


def probe_url(origin: str) -> str:
    """An address of *origin* that cannot exist: a path no site has made, and a different one each time."""
    return f"{origin.rstrip('/')}/web-research-probe-{secrets.token_hex(8)}"
