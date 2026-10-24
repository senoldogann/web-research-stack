"""Dating search results by reading the head of their pages (``resolve_dates``).

A search result carries the date its address, snippet or title happen to show, and most show none:
the page's own ``datePublished`` or ``article:published_time`` is a download away. This reads the
first bytes of a few results' pages, within a time budget, and weighs what is there with what the
result already showed (``dates.py``). It never reads more than ``max_bytes`` of a page, never reads a
section front or a list of stories (their dates are other stories'), and a page that cannot be read
costs only the time it took: the result keeps the date it had.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from web_scraper.web_tools.content import resolve_html_dates
from web_scraper.web_tools.dates import LOW_CONFIDENCE, DateResolution, resolve, text_signals

# A page kind whose dates say nothing of the page: a front page or a section lists stories.
_UNDATED_KINDS = frozenset({"front", "listing"})
# How many heads of one host are read together (the fetcher's own gate allows two a host).
_PER_HOST = 2
_SKIPPED_SUFFIXES = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mp3")


@dataclass
class DatesReport:
    """What reading the heads came to, for the answer's ``dates`` block."""

    asked: int = 0
    read: int = 0
    cached: int = 0
    resolved: int = 0
    failed: int = 0
    cut: int = 0
    unavailable: bool = False
    elapsed_ms: float = 0.0
    budget_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "asked": self.asked,
            "read": self.read,
            "from_page_cache": self.cached,
            "resolved": self.resolved,
            "failed": self.failed,
            "cut": self.cut,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "budget_ms": round(self.budget_ms, 2),
        }


def needs_reading(item: Mapping[str, Any]) -> bool:
    """Whether a result's date is unknown or low and its page is one a date is worth reading from."""
    if item.get("kind") in _UNDATED_KINDS:
        return False
    path = urlsplit(str(item.get("url") or "")).path.lower()
    if path.endswith(_SKIPPED_SUFFIXES):
        return False
    return not item.get("date") or float(item.get("date_confidence") or 0.0) < LOW_CONFIDENCE


async def _head(fetcher: Any, url: str, max_bytes: int, head_cap: Optional[int] = None) -> str:
    """The first *max_bytes* of a page as text (the head read on to its end up to *head_cap*): the
    fetcher's ``fetch_head``, else its ``fetch`` cut."""
    reader = getattr(fetcher, "fetch_head", None)
    if reader is not None:
        fetched = await reader(url, max_bytes=max_bytes, head_cap=head_cap)
        return str(fetched.html or "")
    fetch = getattr(fetcher, "fetch", None)
    if fetch is None:
        raise AttributeError("the fetcher cannot read a page")
    fetched = await fetch(url)
    return str(fetched.html or "")[:max_bytes]


async def read_result_dates(
    items: Sequence[Mapping[str, Any]],
    *,
    fetcher: Any,
    cached_html: Callable[[str], Optional[str]],
    top: int,
    budget_seconds: float,
    max_bytes: int,
    concurrency: int,
    snippets: Mapping[str, tuple[str, str, str]],
    head_cap: Optional[int] = None,
    today: Optional[date] = None,
) -> tuple[dict[str, DateResolution], DatesReport]:
    """Resolve the dates of the first *top* results whose date is unknown or low.

    *snippets* holds, by address, the ``(title, snippet, provider date)`` a result showed, which
    are weighed with the page's own signals. Returns a resolution by address for each page that
    gave a date, and a report. Pages are read together (*concurrency* at a time, two per host) and
    stopped when *budget_seconds* is spent; a stopped read is counted as ``cut``.
    """
    report = DatesReport(budget_ms=budget_seconds * 1000)
    started = time.perf_counter()
    wanted = [item for item in list(items)[:top] if needs_reading(item)]
    report.asked = len(wanted)
    resolutions: dict[str, DateResolution] = {}
    if not wanted:
        return resolutions, report

    overall = asyncio.Semaphore(max(concurrency, 1))
    per_host: dict[str, asyncio.Semaphore] = {}

    async def one(item: Mapping[str, Any]) -> None:
        url = str(item["url"])
        host = (urlsplit(url).hostname or "").lower()
        gate = per_host.setdefault(host, asyncio.Semaphore(_PER_HOST))
        async with gate, overall:
            html = cached_html(url)
            if html is not None:
                report.cached += 1
            else:
                try:
                    html = await _head(fetcher, url, max_bytes, head_cap)
                except AttributeError:
                    report.unavailable = True
                    return
                except Exception:  # noqa: BLE001 - a page that cannot be read keeps its date
                    report.failed += 1
                    return
                report.read += 1
            title, snippet, provider = snippets.get(url, ("", "", ""))
            page = resolve_html_dates(html, url, today=today)
            around = text_signals(title, snippet, url, provider_date=provider, today=today)
            # The address is a signal of the page and of the result both: counted once.
            combined = resolve(dict.fromkeys([*page.signals, *around]), today=today)
            if combined.date and any(signal.source != "url" for signal in page.signals):
                resolutions[url] = combined
                report.resolved += 1

    tasks = [asyncio.create_task(one(item)) for item in wanted]
    _, pending = await asyncio.wait(tasks, timeout=budget_seconds or None)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    report.cut = len(pending)
    report.elapsed_ms = (time.perf_counter() - started) * 1000
    return resolutions, report
