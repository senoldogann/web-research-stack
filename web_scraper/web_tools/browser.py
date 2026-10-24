"""Shared, always-headless browser worker for JavaScript-rendered pages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from web_scraper.network_safety import validate_outbound_url

logger = logging.getLogger(__name__)

_CHROMIUM_ARGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-extensions",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-first-run",
)
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# How long the verdict on a host is kept for the requests of a page. An allow is short: a name can answer
# another address a moment later; a refusal is kept long, since it only errs toward not loading.
_ALLOWED_KEPT_SECONDS = 30.0
_REFUSED_KEPT_SECONDS = 300.0
_CHECK_TIMEOUT_SECONDS = 5.0
_HOSTS_KEPT_MAX = 1024


@dataclass(frozen=True)
class BrowserLaunchCandidate:
    engine: str
    headless: bool
    channel: Optional[str] = None
    args: tuple[str, ...] = ()


@dataclass
class BrowserPage:
    html: str
    final_url: str
    status_code: int
    engine: str


@dataclass
class _AllowedHost:
    allowed: bool
    expires_at: float


class BrowserUnavailableError(RuntimeError):
    """No headless browser engine could be started."""


class BrowserRenderError(RuntimeError):
    """A browser navigation failed; ``code`` is stable for API callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def browser_launch_candidates(preference: str = "auto") -> list[BrowserLaunchCandidate]:
    """Return the ordered headless engines to try for a preference."""
    normalized = (preference or "auto").strip().lower()
    chromium_args = _CHROMIUM_ARGS
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        chromium_args += ("--no-sandbox",)

    chrome = BrowserLaunchCandidate(
        engine="chrome",
        headless=True,
        channel="chrome",
        args=chromium_args,
    )
    chromium = BrowserLaunchCandidate(
        engine="chromium",
        headless=True,
        channel=None,
        args=chromium_args,
    )
    camoufox = BrowserLaunchCandidate(engine="camoufox", headless=True)

    if normalized == "camoufox":
        return [camoufox]
    if normalized == "chrome":
        return [chrome, camoufox]
    if normalized == "chromium":
        return [chromium, camoufox]
    return [chrome, chromium, camoufox]


def browser_request_allowed(url: str) -> bool:
    """Whether a browser subresource may leave the machine.

    Non-http(s) schemes (``file:``, ``data:``, ``blob:``) are refused because they
    can read local files or bypass the network guard entirely.
    """
    try:
        if urlsplit(url).scheme.lower() not in {"http", "https"}:
            return False
        validate_outbound_url(url)
        return True
    except Exception:  # noqa: BLE001 - any validation failure means deny
        return False


class RequestGuard:
    """Holds the requests of a browser page to the public-address rule, one verdict per host kept for a while.

    A check at one moment, not a pin: the browser resolves the name itself when it connects (see
    ``HeadlessBrowserManager``).
    """

    def __init__(self) -> None:
        self._hosts: dict[str, _AllowedHost] = {}

    async def allowed_now(self, url: str) -> bool:
        """Whether *url* may be requested, judged now and off the event loop; any failure is a no."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(browser_request_allowed, url),
                timeout=_CHECK_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - deny on any failure
            return False

    async def allowed(self, url: str) -> bool:
        """Whether a request of a page may be made: the verdict on its host, kept 30 seconds for a yes, 300 for a no."""
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False

        now = time.monotonic()
        cached = self._hosts.get(host)
        if cached is not None and cached.expires_at > now:
            return cached.allowed
        allowed = await self.allowed_now(url)

        kept = _ALLOWED_KEPT_SECONDS if allowed else _REFUSED_KEPT_SECONDS
        if len(self._hosts) >= _HOSTS_KEPT_MAX:
            self._hosts = {h: v for h, v in self._hosts.items() if v.expires_at > now}
        self._hosts[host] = _AllowedHost(allowed=allowed, expires_at=now + kept)
        return allowed

    async def route(self, route: Any) -> None:
        """A Playwright route handler: let a request through if it may be made, abort it if not."""
        if await self.allowed(route.request.url):
            with contextlib.suppress(Exception):
                await route.continue_()
        else:
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")


class HeadlessBrowserManager:
    """One long-lived headless browser, one isolated context per request.

    Chrome is always launched with ``headless=True`` and Playwright's own
    temporary profile, never the user's Chrome profile. If Chrome and bundled
    Chromium cannot start, headless Camoufox is tried. There is deliberately no
    code path that launches a visible window.

    A page is rendered only if its host resolves to public addresses only when the
    render is asked for, and every request the page makes is held to the same rule
    (``RequestGuard``). That is a check at one moment, not a pin: the browser
    resolves the name itself when it connects, which this server cannot steer to the
    address it checked, so a name that answers a public address to the check and
    another to the browser is the one thing it does not stop. ``SafeFetcher`` pins
    its connections and has no such gap.
    """

    def __init__(
        self,
        *,
        preference: str = "auto",
        timeout_seconds: float = 30.0,
        idle_seconds: int = 120,
        post_load_wait_ms: int = 500,
        max_contexts: int = 4,
        viewport_width: int = 1440,
        viewport_height: int = 900,
    ) -> None:
        self.preference = preference
        self.timeout_seconds = max(timeout_seconds, 1.0)
        self.idle_seconds = max(idle_seconds, 5)
        self.post_load_wait_ms = max(post_load_wait_ms, 0)
        self.max_contexts = max(max_contexts, 1)
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height

        self._playwright: Optional[Any] = None
        self._camoufox_manager: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._engine: Optional[str] = None
        self._start_lock = asyncio.Lock()
        self._context_gate = asyncio.Semaphore(self.max_contexts)
        self._active = 0
        self._last_used = 0.0
        self._idle_task: Optional[asyncio.Task[Any]] = None
        self._guard = RequestGuard()

    @property
    def engine(self) -> Optional[str]:
        return self._engine

    @property
    def running(self) -> bool:
        return bool(self._browser is not None and self._browser.is_connected())

    async def start(self) -> None:
        """Start the first available headless engine, reusing a live browser."""
        async with self._start_lock:
            if self.running:
                return
            await self._shutdown_locked()

            for candidate in browser_launch_candidates(self.preference):
                try:
                    if candidate.engine == "camoufox":
                        await self._start_camoufox(candidate)
                    else:
                        await self._start_chromium(candidate)
                except Exception as exc:  # noqa: BLE001 - try the next engine
                    logger.warning(
                        "Headless browser engine %s could not start: %s",
                        candidate.engine,
                        type(exc).__name__,
                    )
                    continue

                self._engine = candidate.engine
                self._last_used = time.monotonic()
                self._idle_task = asyncio.create_task(self._idle_loop())
                logger.info("Web tools using headless %s", candidate.engine)
                return

            await self._shutdown_locked()
            raise BrowserUnavailableError(
                "No headless browser available; install Playwright Chromium/Chrome or Camoufox"
            )

    async def _start_chromium(self, candidate: BrowserLaunchCandidate) -> None:
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

        launch_options: dict[str, Any] = {
            "headless": True,
            "args": list(candidate.args),
        }
        if candidate.channel:
            launch_options["channel"] = candidate.channel
        self._browser = await self._playwright.chromium.launch(**launch_options)

    async def _start_camoufox(self, candidate: BrowserLaunchCandidate) -> None:
        from camoufox.async_api import AsyncCamoufox

        manager = AsyncCamoufox(headless=True, humanize=True, geoip=False)
        browser = await manager.__aenter__()
        self._camoufox_manager = manager
        self._browser = browser

    async def render(self, url: str, *, post_load_wait_ms: Optional[int] = None) -> BrowserPage:
        """Render *url* in a fresh context with every network request guarded."""
        if not await self._guard.allowed_now(url):
            raise BrowserRenderError("unsafe_target", "Blocked unsafe browser target")
        await self.start()
        if self._browser is None:
            raise BrowserRenderError("browser_unavailable", "Headless browser is not running")

        wait_ms = self.post_load_wait_ms if post_load_wait_ms is None else post_load_wait_ms
        async with self._context_gate:
            self._active += 1
            context = None
            try:
                context = await self._create_context()
                page = await context.new_page()
                await page.route("**/*", self._guard.route)
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self.timeout_seconds * 1000),
                    )
                except Exception as exc:  # noqa: BLE001 - normalise Playwright errors
                    name = type(exc).__name__.lower()
                    code = "timeout" if "timeout" in name else "navigation_failed"
                    raise BrowserRenderError(code, "Browser navigation failed") from exc

                if wait_ms:
                    with contextlib.suppress(Exception):
                        await page.wait_for_timeout(wait_ms)

                html = await page.content()
                final_url = page.url
                status = response.status if response else 0
                return BrowserPage(
                    html=html,
                    final_url=final_url,
                    status_code=status,
                    engine=self._engine or "unknown",
                )
            finally:
                if context is not None:
                    with contextlib.suppress(Exception):
                        await context.close()
                self._active -= 1
                self._last_used = time.monotonic()

    async def _create_context(self) -> Any:
        if self._browser is None:
            raise BrowserRenderError("browser_unavailable", "Headless browser is not running")

        options: dict[str, Any] = {
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "locale": "en-US",
            "java_script_enabled": True,
            "service_workers": "block",
        }
        if self._engine in {"chrome", "chromium"}:
            options["user_agent"] = _DEFAULT_USER_AGENT
        return await self._browser.new_context(**options)

    async def _idle_loop(self) -> None:
        interval = max(5.0, self.idle_seconds / 2)
        while True:
            await asyncio.sleep(interval)
            if self._active or not self.running:
                continue
            if time.monotonic() - self._last_used >= self.idle_seconds:
                await self.stop()
                return

    async def stop(self) -> None:
        """Shut the browser down and clear the cached engine."""
        async with self._start_lock:
            task = self._idle_task
            self._idle_task = None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._camoufox_manager is not None:
            with contextlib.suppress(Exception):
                await self._camoufox_manager.__aexit__(None, None, None)
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._browser = None
        self._camoufox_manager = None
        self._playwright = None
        self._engine = None

    def snapshot(self) -> dict[str, Any]:
        """Expose non-sensitive browser lifecycle state."""
        return {
            "running": self.running,
            "engine": self._engine,
            "active_contexts": self._active,
            "idle_seconds": self.idle_seconds,
        }
