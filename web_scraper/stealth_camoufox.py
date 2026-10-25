"""Shared Camoufox stealth browser instance for search providers.

One headless Camoufox for the process, with one browser context that every search reuses. It is held to the same
rules as the Camoufox that reads a page after a bot check (``web_scraper.web_tools.browser``):

* it is started from what is on the machine, never downloaded in the middle of a search (what it lacks is fetched in
  the background, once, by ``python -m camoufox fetch``; a search that finds it missing is answered by the next
  provider), within a time limit, and with the options that keep its own calls to Mozilla and Google off;
* the context is held to the address guard (``RequestGuard``) before its first page exists, so that no request of a
  page, a frame, a window or a web socket goes to an address the guard refuses, and a window a page opens is closed;
* a page is read only if it ended at an address the guard allows (``landing_allowed``);
* what a search does with a page is bounded by the caller (``GoogleSearcher``), and a browser that does not answer a
  close is shut down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CAMOUFOX_STATE: Dict[str, object] = {
    "browser": None,
    "manager": None,
    "context": None,
    "last": 0.0,
}
_CAMOUFOX_LOCK: Optional[asyncio.Semaphore] = None
_START_LOCK: Optional[asyncio.Lock] = None
_GUARD: Optional[Any] = None
_CAMOUFOX_IDLE_SECONDS = 180.0
# How long a close (of a page, a context, the browser) may take before it is given up, and how long the warm-up of
# the browser may take altogether.
_CLOSE_TIMEOUT_SECONDS = 5.0
_WARM_UP_TIMEOUT_SECONDS = 30.0
# Why the browser could not be had last time, so that the log says it once and not at every search.
_LAST_REASON = ""
# Windows being closed (a task nobody holds can be collected before it has run).
_CLOSING: set[asyncio.Future[Any]] = set()

GOOGLE_CONSENT_COOKIES = [
    {"name": "CONSENT", "value": "YES+cb", "domain": ".google.com", "path": "/"},
    {
        "name": "SOCS",
        "value": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
        "domain": ".google.com",
        "path": "/",
    },
]


def camoufox_lock() -> asyncio.Semaphore:
    global _CAMOUFOX_LOCK
    if _CAMOUFOX_LOCK is None:
        _CAMOUFOX_LOCK = asyncio.Semaphore(1)
    return _CAMOUFOX_LOCK


def _start_lock() -> asyncio.Lock:
    global _START_LOCK
    if _START_LOCK is None:
        _START_LOCK = asyncio.Lock()
    return _START_LOCK


def _guard() -> Any:
    """The address guard of the search's pages: one, so that the verdict on a host is kept between searches."""
    global _GUARD
    if _GUARD is None:
        from web_scraper.web_tools.browser import RequestGuard

        _GUARD = RequestGuard()
    return _GUARD


def _fetch_allowed() -> bool:
    """Whether what Camoufox lacks may be fetched by this server (``WEB_TOOLS_BROWSER_STEALTH_FETCH``)."""
    from web_scraper.web_tools.settings import WebToolsSettings

    return WebToolsSettings.from_env().browser_stealth_fetch


def _unavailable(reason: str) -> None:
    global _LAST_REASON
    if reason != _LAST_REASON:
        _LAST_REASON = reason
        logger.warning("Camoufox is not available for the search: %s", reason)


async def _closed(awaitable: Any) -> None:
    """Wait for a close, but not for ever: a browser that has stopped answering is the one being shut down."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(awaitable, timeout=_CLOSE_TIMEOUT_SECONDS)


async def get_camoufox_browser():
    global _LAST_REASON
    state = _CAMOUFOX_STATE
    now = time.monotonic()
    browser = state["browser"]
    if browser is not None:
        gone = False
        with contextlib.suppress(Exception):
            gone = not browser.is_connected()
        if gone or now - float(state["last"]) > _CAMOUFOX_IDLE_SECONDS:
            await close_camoufox()

    if state["browser"] is None:
        async with _start_lock():  # one start at a time; a search that waited finds it started
            if state["browser"] is None:
                from web_scraper.web_tools import browser as browser_module

                try:
                    manager, started = await browser_module.start_camoufox(fetch=_fetch_allowed())
                except Exception as exc:  # noqa: BLE001 - no browser means the next provider
                    _unavailable(str(exc) or type(exc).__name__)
                    return None
                _LAST_REASON = ""
                state["manager"] = manager
                state["browser"] = started

    state["last"] = now
    return state["browser"]


async def get_camoufox_context():
    browser = await get_camoufox_browser()
    if browser is None:
        return None
    state = _CAMOUFOX_STATE
    context = state.get("context")
    if context is None:
        async with _start_lock():
            context = state.get("context")
            if context is None:
                try:
                    context = await browser.new_context()
                    # Before its first page exists: the requests of a page, a frame, a window and a web socket.
                    await _guard().hold(context)
                except Exception as exc:  # noqa: BLE001 - a context the guard does not hold is not used
                    _unavailable(f"its context could not be held to the address guard ({exc})")
                    if context is not None:
                        await _closed(context.close())
                    return None
                try:
                    await context.add_cookies(GOOGLE_CONSENT_COOKIES)
                except Exception as exc:  # noqa: BLE001 - the dialog is then answered by a click
                    logger.debug("Failed setting consent cookies on Camoufox context: %s", exc)
                state["context"] = context
    return context


def _close_windows_of(page: Any) -> None:
    """Close every window *page* opens: a search is not asking for a second page, and what the window loads is
    nobody's to read or to wait for. (On the page and not on the context: the context is shared, and a handler
    that closes every page but one would close the next search's page.)"""

    async def close(popup: Any) -> None:
        with contextlib.suppress(Exception):
            await popup.close()

    def opened(popup: Any) -> None:
        task = asyncio.ensure_future(close(popup))
        _CLOSING.add(task)
        task.add_done_callback(_CLOSING.discard)

    page.on("popup", opened)


async def get_camoufox_page():
    context = await get_camoufox_context()
    if context is None:
        return None
    page = await context.new_page()
    _close_windows_of(page)
    return page


async def landing_allowed(page: Any) -> bool:
    """Whether the page ended at an address the guard allows. The guard is asked about the first address of a
    request; a redirect the browser follows is not asked about again, so the address a page ended at is asked
    about once more before it is read. A page that is not on a host (``about:blank``, an error page) is not judged
    by one."""
    from urllib.parse import urlsplit

    url = str(getattr(page, "url", "") or "")
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        return True
    return bool(await _guard().allowed_now(url))


async def close_page(page: Any) -> None:
    """Close a search's page, bounded."""
    await _closed(page.close())


async def warm_up_camoufox() -> None:
    """Prime the stealth browser so DNS, TLS and caches are warm."""
    try:
        await asyncio.wait_for(_warm_up(), timeout=_WARM_UP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.debug("Camoufox warm-up took too long; the browser is shut down")
        await close_camoufox()


async def _warm_up() -> None:
    async with camoufox_lock():
        page = await get_camoufox_page()
        if page is None:
            return
        try:
            await page.goto("https://www.google.com/ncr", wait_until="commit", timeout=15000)
            try:
                await page.wait_for_selector("form, div#rso, #search", timeout=6000)
            except Exception:
                pass
        except Exception as exc:
            logger.debug("Camoufox warm-up error: %s", exc)
        finally:
            await close_page(page)


async def close_camoufox() -> None:
    """Close the shared Camoufox browser cleanly (and, if it does not answer, anyway)."""
    state = _CAMOUFOX_STATE
    manager = state.get("manager")
    context = state.get("context")
    state["browser"] = None
    state["context"] = None
    state["manager"] = None
    state["last"] = 0.0
    if context is not None:
        await _closed(context.close())
    if manager is not None:
        await _closed(manager.__aexit__(None, None, None))
