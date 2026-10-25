"""Shared Camoufox stealth browser instance for search providers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CAMOUFOX_STATE: Dict[str, object] = {
    "browser": None,
    "manager": None,
    "context": None,
    "last": 0.0,
}
_CAMOUFOX_LOCK: Optional[asyncio.Semaphore] = None
_CAMOUFOX_IDLE_SECONDS = 180.0

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


async def get_camoufox_browser():
    state = _CAMOUFOX_STATE
    now = time.monotonic()
    if state["browser"] is not None and now - float(state["last"]) > _CAMOUFOX_IDLE_SECONDS:
        await close_camoufox()

    if state["browser"] is None:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            logger.debug("camoufox is not installed")
            return None
        try:
            manager = AsyncCamoufox(headless=True, humanize=True)
            state["browser"] = await manager.__aenter__()
            state["manager"] = manager
        except Exception as exc:
            logger.warning("Camoufox failed to start: %s", exc)
            state["browser"] = None
            return None

    state["last"] = now
    return state["browser"]


async def get_camoufox_context():
    browser = await get_camoufox_browser()
    if browser is None:
        return None
    state = _CAMOUFOX_STATE
    context = state.get("context")
    if context is None:
        try:
            context = await browser.new_context()
            await context.add_cookies(GOOGLE_CONSENT_COOKIES)
        except Exception as exc:
            logger.debug("Failed setting consent cookies on Camoufox context: %s", exc)
        state["context"] = context
    return context


async def get_camoufox_page():
    context = await get_camoufox_context()
    if context is None:
        return None
    return await context.new_page()


async def warm_up_camoufox() -> None:
    """Prime the stealth browser so DNS, TLS and caches are warm."""
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
            try:
                await page.close()
            except Exception:
                pass


async def close_camoufox() -> None:
    """Close the shared Camoufox browser cleanly."""
    state = _CAMOUFOX_STATE
    manager = state.get("manager")
    context = state.get("context")
    state["browser"] = None
    state["context"] = None
    state["manager"] = None
    state["last"] = 0.0
    if context is not None:
        try:
            await context.close()
        except Exception:
            pass
    if manager is not None:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass
