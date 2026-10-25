"""A short-lived cache of the documents the web tools fetched.

Scraping a page and then extracting from it, or reading it again with another cap, used to
fetch it every time: the scrape cache holds the scrape's *answer*, keyed by what it was asked
for, and extract used no cache at all. This holds the fetched document itself (the HTML as
the tools last read it, rendered by the browser when it had to be), keyed by the page's
canonical URL, so each call parses it as it needs: its own character cap, its own selectors.

It is bounded in the number of documents and in bytes, and each lives for the TTL. What it
never holds is decided by the caller: errors, challenge pages and archived copies are not
documents of the page.

A document kept has a ``content_id``, a short name for exactly this text of this page: a scrape that
cut a long page says it, and a later ``web_scrape`` with the id and an offset reads on from the same
document without fetching the page again. The id goes with the document: when it expires, is
pushed out, or the page is stored again with other content, the id is gone (and a fresh scrape
has a new one).
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class CachedDocument:
    """What a fetch of a page left: its final address, its text and how it was got."""

    final_url: str
    status_code: int
    content_type: str
    html: str
    fetched_with: str
    warnings: tuple[str, ...]
    # The fetch's own stages (network_ms, http_ms, browser_ms, ...): what it cost once.
    timings: dict[str, float]
    # Whether the page was asked if its site gave it for any address (``soft_404`` in ``warnings`` when it did).
    # A page read without the question (a map's start page) is asked it when a read that wants the answer gets
    # it from here, and kept with the answer.
    judged: bool = True


class PageCache:
    """Documents by canonical URL, oldest unused out first, bounded in count and in bytes."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        max_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self.max_entries = max(int(max_entries), 1)
        self.max_bytes = max(int(max_bytes), 1)
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, int, CachedDocument]] = OrderedDict()
        self._ids: dict[str, str] = {}  # key -> content id
        self._keys: dict[str, str] = {}  # content id -> key
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[CachedDocument]:
        """The document of *key* if it is there and has not outlived the TTL."""
        self._expire()
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry[2]

    def put(self, key: str, document: CachedDocument) -> bool:
        """Keep a document; False when it is not kept (no TTL, or it alone is over the byte bound)."""
        if self.ttl_seconds <= 0:
            return False
        size = len(document.html.encode("utf-8", errors="ignore"))
        if size > self.max_bytes:
            return False
        self._drop(key)
        self._entries[key] = (self._clock() + self.ttl_seconds, size, document)
        self._bytes += size
        content_id = _content_id(key, document.html)
        self._ids[key] = content_id
        self._keys[content_id] = key
        while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
            self._drop(next(iter(self._entries)))
        return True

    def revise(self, key: str, document: CachedDocument) -> None:
        """Put another reading of the page of *key* (the same HTML, with more known of it) where the document was,
        with the time it has left; nothing is kept for a page that is no longer here."""
        entry = self._entries.get(key)
        if entry is not None:
            self._entries[key] = (entry[0], entry[1], document)

    def id_for(self, key: str) -> Optional[str]:
        """The ``content_id`` of the document of *key*, if it is kept (and has not outlived the TTL)."""
        self._expire()
        return self._ids.get(key) if key in self._entries else None

    def get_by_id(self, content_id: str) -> Optional[CachedDocument]:
        """The document a ``content_id`` names, if it is still kept; None once it is gone."""
        self._expire()
        key = self._keys.get(content_id)
        entry = self._entries.get(key) if key is not None else None
        if entry is None:
            return None
        self._entries.move_to_end(key)  # type: ignore[arg-type]
        return entry[2]

    def snapshot(self) -> dict[str, float]:
        """The cache's own numbers, for the stats endpoint."""
        self._expire()
        return {
            "entries": len(self._entries),
            "bytes": self._bytes,
            "hits": self.hits,
            "misses": self.misses,
            "ttl_seconds": self.ttl_seconds,
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
        }

    def _drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry[1]
        content_id = self._ids.pop(key, None)
        if content_id is not None:
            self._keys.pop(content_id, None)

    def _expire(self) -> None:
        now = self._clock()
        for key in [key for key, (expires_at, _, _) in self._entries.items() if expires_at <= now]:
            self._drop(key)


def _content_id(key: str, html: str) -> str:
    """A short name for this document of this page: it changes when the page's text does."""
    digest = hashlib.sha256(f"{key}\0{html}".encode("utf-8", errors="ignore")).hexdigest()
    return f"c-{digest[:12]}"
