"""Shared, always-headless browser worker for JavaScript-rendered pages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from web_scraper.network_safety import validate_outbound_url
from web_scraper.web_tools.content import shell_text

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
    "--disable-blink-features=AutomationControlled",
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
# The status a web socket the guard refuses is closed with (RFC 6455: 1008, a policy violation).
_REFUSED_SOCKET_CODE = 1008
# The whole render is bounded: its own timeout and this margin (a page whose script blocks the browser's main
# thread leaves ``page.evaluate`` and ``page.content`` unanswered for ever, and a render that waits on them
# keeps a context slot and, above it, a tool slot). A context that has not closed after
# ``_CLOSE_TIMEOUT_SECONDS`` is given up, with the browser that did not answer; so is a browser that has not
# shut down after ``_STOP_TIMEOUT_SECONDS``.
_HARD_LIMIT_MARGIN_SECONDS = 5.0
_CLOSE_TIMEOUT_SECONDS = 5.0
_STOP_TIMEOUT_SECONDS = 5.0
# How long Camoufox may take to start. It starts from what is installed (``camoufox_missing``), so this is a
# browser launching, seconds; it is not the time a download would take.
_STEALTH_START_TIMEOUT_SECONDS = 60.0
# What Camoufox lacks is fetched by ``python -m camoufox fetch`` (its browser from GitHub, the model its fingerprints
# are drawn from from GitHub, its ad blocker from addons.mozilla.org), once, in the background, the first time a page
# needs it and it is not there: about 100 MB to download and 2.3 GB on disk, which can take longer than any read, so
# no read waits for it. A fetch that failed is not tried again for ``_STEALTH_FETCH_RETRY_SECONDS``.
_STEALTH_FETCH_COMMAND: tuple[str, ...] = (sys.executable, "-m", "camoufox", "fetch")
# How the command is named to a person (the one they can run themselves).
_STEALTH_FETCH_HINT = "python -m camoufox fetch"
_STEALTH_FETCH_TIMEOUT_SECONDS = 1800.0
_STEALTH_FETCH_RETRY_SECONDS = 1800.0
_STEALTH_FETCH_TAIL_BYTES = 4096
# What Camoufox starts with: headless, a cursor that moves as a person's does, and no look-up of this machine's
# public address (``geoip`` off). The prefs switch off the browser's own calls to Mozilla and Google (an update
# of the application, of its media plug-ins and of its add-ons): they are made by the browser and not by a page,
# and a server that reads pages has no use for them. Measured with a proxy that refused everything and wrote
# down what it was asked: those five hosts, 20 seconds after each start, and none with these prefs. What stays is
# the ad blocker Camoufox ships with, which fetches its filter lists (docs/web-tools.md lists the hosts).
_STEALTH_OPTIONS: dict[str, Any] = {
    "headless": True,
    "humanize": True,
    "geoip": False,
    "firefox_user_prefs": {
        "app.update.enabled": False,
        "app.update.auto": False,
        "app.update.service.enabled": False,
        "media.gmp-manager.url": "",
        "media.gmp-provider.enabled": False,
        "extensions.update.enabled": False,
    },
}
# Waiting for a page to settle (see ``HeadlessBrowserManager._settle``): how long the network may take to
# go quiet, how often the page's text is looked at, and the time kept back from the render's own deadline
# to read the page in.
_NETWORK_IDLE_MAX_MS = 3_000
_POLL_SECONDS = 0.1
_DEADLINE_MARGIN_SECONDS = 0.5
# The text a reader of the page would see: the Markdown reader's own choices in the page itself (the
# first of main, article and role=main, else the body; no menus, banners, footers, sidebars, forms or
# scripts), counted without laying the page out again and again. Returns how many characters there are
# and the first ``limit`` of them, a line to each piece of text; it stops counting at 200,000 characters,
# which no shell is.
_VISIBLE_TEXT_JS = """(limit) => {
  const skip = 'script,style,noscript,template,nav,header,footer,aside,svg,canvas,form,iframe,dialog,' +
    'button,select,textarea,[role=navigation],[role=banner],[role=contentinfo],' +
    '[role=complementary],[role=search],[hidden],[aria-hidden=true]';
  const root = document.querySelector('main') || document.querySelector('article') ||
    document.querySelector('[role=main]') || document.body;
  if (!root) return {chars: 0, head: ''};
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let chars = 0, head = '', node;
  while ((node = walker.nextNode())) {
    const text = node.nodeValue.replace(/\\s+/g, ' ').trim();
    if (!text) continue;
    const parent = node.parentElement;
    if (parent && parent.closest(skip)) continue;
    chars += text.length + (chars ? 1 : 0);
    if (chars > 200000) break;
    if (head.length < limit) head += (head ? '\\n' : '') + text;
  }
  return {chars: chars, head: head.slice(0, limit)};
}"""


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
    # How the wait for the page to settle went: how long it was, how much visible text the page had at
    # the end (-1: it could not be read), and why it ended: ``stable`` (the text had stopped growing),
    # ``cap`` (the time allowed ran out first). Empty for a page that was not waited for.
    settled_ms: float = 0.0
    text_chars: int = -1
    settle: str = ""


@dataclass
class _Settled:
    waited_ms: float
    chars: int
    reason: str


@dataclass
class _VisibleText:
    chars: int
    head: str


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


def camoufox_installed() -> bool:
    """Whether the package camoufox can be imported (its browser may still be missing: ``camoufox_missing``)."""
    try:
        import camoufox  # noqa: F401
    except Exception:  # noqa: BLE001 - ImportError, or what a missing system library raises on the way
        return False
    return True


def _camoufox_needs() -> tuple[str, bool]:
    """Why Camoufox cannot start from what is on this machine, and whether ``python -m camoufox fetch`` would
    put that right; ``("", False)`` when it can start. Nothing is downloaded to find out."""
    try:
        import camoufox  # noqa: F401
    except ImportError:
        return "the package camoufox is not installed", False
    except Exception as exc:  # noqa: BLE001 - installed, but something it needs on this machine is not
        return f"the package camoufox cannot be imported here ({type(exc).__name__})", False
    fetch = "run `python -m camoufox fetch` once, which downloads what it needs"
    try:
        from camoufox.addons import DefaultAddons, get_addon_path
        from camoufox.fpgen_model import is_pinned
        from camoufox.pkgman import camoufox_path
    except ImportError:
        return (
            "this version of camoufox cannot be checked without starting it; install camoufox 0.5.8 or later",
            False,
        )
    try:
        camoufox_path(download_if_missing=False)
    except Exception:  # noqa: BLE001 - not installed, or not the build this camoufox needs
        return f"its browser is not installed ({fetch})", True
    try:
        for addon in DefaultAddons:
            if not os.path.exists(os.path.join(get_addon_path(addon.name), "manifest.json")):
                return f"its ad blocker ({addon.name}) is not installed ({fetch})", True
        if not is_pinned():
            return f"its fingerprint model is not installed ({fetch})", True
    except Exception:  # noqa: BLE001 - whatever stops the check is a reason not to start it
        return f"what it needs could not be checked ({fetch})", False
    return "", False


def camoufox_missing() -> str:
    """Why Camoufox cannot start from what is on this machine, in a sentence; empty when it can.

    Nothing is downloaded to find out. Left to itself Camoufox fetches what it lacks the moment it starts, in the
    middle of a page read, which waits for it with nothing to bound the wait, and in a thread that a read which gives
    up cannot stop: its browser (about 100 MB from GitHub, 2.3 GB once unpacked), the model its fingerprints are drawn
    from (from GitHub) and its ad blocker (the latest from addons.mozilla.org). This server starts it only from what
    is already on the machine, and fetches what is missing itself, apart from any read (``HeadlessBrowserManager``,
    ``stealth_fetch``), or you do with ``python -m camoufox fetch``.
    """
    return _camoufox_needs()[0]


class RequestGuard:
    """Holds the requests of a browser page to the public-address rule, one verdict per host kept for a while.

    A check at one moment, not a pin: the browser resolves the name itself when it connects (see
    ``HeadlessBrowserManager``).
    """

    def __init__(self) -> None:
        self._hosts: dict[str, _AllowedHost] = {}
        # The windows being closed (a task nobody holds can be collected before it has run).
        self._closing: set[asyncio.Future[Any]] = set()

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

    async def route_web_socket(self, socket: Any) -> None:
        """A Playwright web socket route handler: connect the page's web socket to its server if the address
        may be requested (``ws`` is ``http`` and ``wss`` is ``https`` to the rule), close it if not. A web
        socket is not a request a route sees; left unrouted it reaches any host."""
        try:
            parts = urlsplit(socket.url)
            scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower())
            allowed = scheme is not None and await self.allowed(
                parts._replace(scheme=scheme).geturl()
            )
        except Exception:  # noqa: BLE001 - deny on any failure
            allowed = False
        with contextlib.suppress(Exception):
            if allowed:
                socket.connect_to_server()
            else:
                await socket.close(code=_REFUSED_SOCKET_CODE, reason="blocked by the guard")

    async def hold(self, context: Any) -> None:
        """Hold every page of a Playwright browser context to the rule, before the first page is made: the
        requests they make (a window a page opens is a page of the same context, so its navigation and
        everything it loads are held too), and the web sockets they open.

        A Playwright too old to route web sockets (before 1.48) cannot hold them, and is not used: the
        guard does not run with a gap that is not the browser's own.
        """
        route_web_socket = getattr(context, "route_web_socket", None)
        if route_web_socket is None:
            raise BrowserUnavailableError(
                "This Playwright cannot hold a page's web sockets to the guard; "
                "install playwright 1.48 or later"
            )
        await context.route("**/*", self.route)
        await route_web_socket("**/*", self.route_web_socket)

    def close_popups(self, context: Any, page: Any) -> None:
        """Close every window that is not *page*: a page that opens one is not asking for a second page to
        be read, and what the window loads is nobody's to read or to wait for."""

        async def close(popup: Any) -> None:
            with contextlib.suppress(Exception):
                await popup.close()

        def opened(popup: Any) -> None:
            if popup is not page:
                task = asyncio.ensure_future(close(popup))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

        context.on("page", opened)


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

    When a site's bot check turns the first engine away (a captcha, an access block, a timeout)
    ``render`` reads the page once more in Camoufox, a Firefox made to look like a person's browser, if
    the package ``camoufox`` is installed and its browser is on the machine (``camoufox_missing``). If
    the browser is not there yet and ``stealth_fetch`` is true, ``python -m camoufox fetch`` is run once
    in the background, not in a read: the page that asked is returned as the first engine read it, and
    a later one is read in Camoufox. ``stealth_enabled=False`` never reads a page that way. The guard
    is put on the context of either engine (what Playwright cannot hold in Firefox is in
    docs/web-tools.md), and each read of either is bounded.
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
        settle_max_ms: int = 8_000,
        settle_stable_ms: int = 600,
        shell_chars: int = 200,
        stealth_enabled: bool = True,
        stealth_fetch: bool = False,
    ) -> None:
        self.preference = preference
        # Whether a page the primary browser could not read may be read again in Camoufox (when the package
        # camoufox is installed and has fetched what it needs). WEB_TOOLS_BROWSER_STEALTH.
        self.stealth_enabled = stealth_enabled
        # Whether what Camoufox lacks (its browser, ...) may be fetched, in the background, the first time a page
        # needs it. WEB_TOOLS_BROWSER_STEALTH_FETCH. A manager made by hand does not, unless it is told to.
        self.stealth_fetch = stealth_fetch
        self.timeout_seconds = max(timeout_seconds, 1.0)
        self.idle_seconds = max(idle_seconds, 5)
        # The least a page is waited for after it has loaded, and the most (within timeout_seconds); in
        # between it is waited for until its text has stopped growing for settle_stable_ms and is more than
        # a shell's (shell_chars is where a page stops being one).
        self.post_load_wait_ms = max(post_load_wait_ms, 0)
        self.settle_max_ms = max(settle_max_ms, 0)
        self.settle_stable_ms = max(settle_stable_ms, 0)
        self.shell_chars = max(shell_chars, 20)
        self.max_contexts = max(max_contexts, 1)
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height

        self._playwright: Optional[Any] = None
        self._camoufox_manager: Optional[Any] = None
        self._stealth_manager: Optional[Any] = None
        self._stealth_browser: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._engine: Optional[str] = None
        self._start_lock = asyncio.Lock()
        self._stealth_lock = asyncio.Lock()
        self._context_gate = asyncio.Semaphore(self.max_contexts)
        self._active = 0
        self._last_used = 0.0
        self._idle_task: Optional[asyncio.Task[Any]] = None
        self._guard = RequestGuard()
        # Why the stealth engine last could not start ("" when it did, or was not asked for).
        self._stealth_reason = ""
        # The fetch of what Camoufox lacks that is under way (or was), and when the last one failed.
        self._fetch_task: Optional[asyncio.Task[None]] = None
        self._fetch_failed_at: Optional[float] = None
        # The time the wait for a page measures and sleeps by; a test sets its own.
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep

    @property
    def engine(self) -> Optional[str]:
        return self._engine

    @property
    def running(self) -> bool:
        stealth_active = bool(
            self._stealth_browser is not None and self._stealth_browser.is_connected()
        )
        return self._primary_running or stealth_active

    @property
    def _primary_running(self) -> bool:
        return bool(self._browser is not None and self._browser.is_connected())

    @property
    def has_stealth(self) -> bool:
        """Whether a page may be read again in Camoufox: it is not turned off and the package is installed."""
        if not self.stealth_enabled:
            return False
        if self._engine == "camoufox" or self._stealth_browser is not None:
            return True
        return camoufox_installed()

    def _begin_fetch(self) -> bool:
        """Start fetching what Camoufox lacks, in the background, when that is allowed, nothing is under way and
        the last try (if any) is not recent. Whether a fetch is under way now."""
        if not self.stealth_fetch:
            return False
        task = self._fetch_task
        if task is not None and not task.done():
            return True
        failed = self._fetch_failed_at
        if failed is not None and self._clock() - failed < _STEALTH_FETCH_RETRY_SECONDS:
            return False
        self._fetch_task = asyncio.create_task(self._fetch_camoufox())
        return True

    async def _fetch_camoufox(self) -> None:
        """Fetch what Camoufox lacks, once, apart from any read, and say in the log how it went."""
        command = _STEALTH_FETCH_HINT
        logger.info(
            "Camoufox is not all on this machine yet: running `%s` once, in the background (about 100 MB to "
            "download and 2.3 GB on disk, from GitHub and addons.mozilla.org); a page that meets a wall "
            "meanwhile stays as the first browser read it",
            command,
        )
        try:
            fetched, detail = await self._run_fetch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a fetch that cannot run is not a server that cannot
            fetched, detail = False, type(exc).__name__
        if fetched:
            still_missing = await asyncio.to_thread(camoufox_missing)
            if not still_missing:
                self._fetch_failed_at = None
                logger.info("Camoufox fetched: a page that meets a wall from now on is read in it")
                return
            detail = f"it finished, but {still_missing}"
        self._fetch_failed_at = self._clock()
        logger.warning(
            "Fetching Camoufox failed (%s); it is tried again in %d minutes, or run `%s` yourself",
            detail or "it printed nothing",
            int(_STEALTH_FETCH_RETRY_SECONDS // 60),
            command,
        )

    async def _run_fetch(self) -> tuple[bool, str]:
        """Run the command that fetches what Camoufox lacks, bounded; whether it succeeded, and the last line
        it printed (or why it did not finish)."""
        process = await asyncio.create_subprocess_exec(
            *_STEALTH_FETCH_COMMAND,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        printed = b""

        async def read() -> None:
            nonlocal printed
            assert process.stdout is not None
            while chunk := await process.stdout.read(4096):
                printed = (printed + chunk)[-_STEALTH_FETCH_TAIL_BYTES:]
            await process.wait()

        try:
            await asyncio.wait_for(read(), timeout=_STEALTH_FETCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return False, "it did not finish in time"
        finally:
            # Still running means it timed out or the server is closing: it does not go on without us.
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
        text = printed.decode("utf-8", "replace").replace("\r", "\n")
        last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
        if process.returncode == 0:
            return True, last[:300]
        return False, last[:300] or f"exit status {process.returncode}"

    async def _launch_camoufox(self) -> tuple[Any, Any]:
        """Start Camoufox from what is installed, within ``_STEALTH_START_TIMEOUT_SECONDS``; its manager and
        its browser. Raises ``BrowserUnavailableError`` (with the reason) when it cannot be started here."""
        missing, fetchable = await asyncio.to_thread(_camoufox_needs)
        if missing:
            if fetchable and self._begin_fetch():
                raise BrowserUnavailableError(
                    f"Camoufox is not started: {missing}; this server is fetching it in the background, "
                    "and a page that meets a wall after that is read in it"
                )
            raise BrowserUnavailableError(f"Camoufox is not started: {missing}")
        from camoufox.async_api import AsyncCamoufox

        options = {
            name: dict(value) if isinstance(value, dict) else value
            for name, value in _STEALTH_OPTIONS.items()
        }
        manager = AsyncCamoufox(**options)
        try:
            browser = await asyncio.wait_for(
                manager.__aenter__(), timeout=_STEALTH_START_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    manager.__aexit__(None, None, None), timeout=_STOP_TIMEOUT_SECONDS
                )
            raise BrowserUnavailableError("Camoufox did not start in time") from exc
        return manager, browser

    async def _ensure_stealth_browser(self) -> Any:
        async with self._stealth_lock:
            if (
                self._engine == "camoufox"
                and self._browser is not None
                and self._browser.is_connected()
            ):
                return self._browser
            if self._stealth_browser is not None and self._stealth_browser.is_connected():
                return self._stealth_browser
            if not self.stealth_enabled:
                self._stealth_reason = "reading a page again in Camoufox is turned off"
                return None
            try:
                manager, browser = await self._launch_camoufox()
            except Exception as exc:  # noqa: BLE001
                reason = (
                    str(exc) if isinstance(exc, BrowserUnavailableError) else type(exc).__name__
                )
                if reason != self._stealth_reason:
                    logger.warning("Could not start stealth browser (Camoufox): %s", reason)
                self._stealth_reason = reason
                return None
            self._stealth_reason = ""
            self._stealth_manager = manager
            self._stealth_browser = browser
            self._last_used = time.monotonic()
            if self._idle_task is None:
                self._idle_task = asyncio.create_task(self._idle_loop())
            logger.info("Web tools stealth browser (Camoufox) started")
            return self._stealth_browser

    async def start(self) -> None:
        """Start the first available headless engine, reusing a live browser."""
        async with self._start_lock:
            # The primary engine, not any: a stealth browser that is up is not one to render the page with.
            if self._primary_running:
                return
            await self._shutdown_primary_locked()

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
                        # Our own sentence (what is missing, the command that fetches it), not a library's.
                        str(exc)
                        if isinstance(exc, BrowserUnavailableError)
                        else type(exc).__name__,
                    )
                    continue

                self._engine = candidate.engine
                self._last_used = time.monotonic()
                self._idle_task = asyncio.create_task(self._idle_loop())
                logger.info("Web tools using headless %s", candidate.engine)
                return

            await self._shutdown_primary_locked()
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
        manager, browser = await self._launch_camoufox()
        self._camoufox_manager = manager
        self._browser = browser

    async def _render_with_browser(
        self,
        browser: Any,
        engine_name: str,
        url: str,
        post_load_wait_ms: Optional[int] = None,
    ) -> BrowserPage:
        """Render *url* in a fresh context of *browser* (one engine's render), every request guarded.

        The page is read once it has settled (``_settle``), not after a fixed wait: a page whose
        script draws its content a second or three after the load is read with its content. The
        render is bounded (``timeout_seconds`` and a margin) whichever engine it is: a page that
        blocks the browser's main thread leaves every call to it unanswered, and the render ends with a
        ``timeout`` and frees its slot, its context closed (a renderer stuck in a loop dies with it).
        """
        least_ms = self.post_load_wait_ms if post_load_wait_ms is None else post_load_wait_ms
        async with self._context_gate:
            self._active += 1
            opened: list[Any] = []
            try:
                try:
                    return await asyncio.wait_for(
                        self._render(browser, engine_name, url, least_ms, opened),
                        timeout=self.timeout_seconds + _HARD_LIMIT_MARGIN_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    raise BrowserRenderError(
                        "timeout", "The page did not finish rendering in time"
                    ) from exc
            finally:
                try:
                    for context in opened:
                        await self._close_context(context, browser)
                finally:
                    self._active -= 1
                    self._last_used = time.monotonic()

    async def _render(
        self, browser: Any, engine_name: str, url: str, least_ms: int, opened: list[Any]
    ) -> BrowserPage:
        """The render itself: a context (put in *opened* the moment it exists, to be closed whatever happens),
        the page, its navigation, the wait for it to settle and its HTML."""
        deadline = self._clock() + self.timeout_seconds
        context = await self._create_context_for(browser, engine_name)
        opened.append(context)
        await self._guard.hold(context)
        page = await context.new_page()
        self._guard.close_popups(context, page)
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

        settled = await self._settle(page, least_ms, deadline)
        html = await self._content(page)
        final_url = page.url
        await self._check_landing(final_url)
        status = response.status if response else 0
        return BrowserPage(
            html=html,
            final_url=final_url,
            status_code=status,
            engine=engine_name,
            settled_ms=round(settled.waited_ms, 2),
            text_chars=settled.chars,
            settle=settled.reason,
        )

    async def _check_landing(self, final_url: str) -> None:
        """A page is returned only if the address it ended at is one the guard allows.

        The guard is asked about the first address of a request; a redirect the browser follows is not asked
        about again, so an allowed address that answers ``302 Location: http://127.0.0.1/...`` would be read from
        there. The request to the refused address is still made, but what it answered is not returned.
        """
        if urlsplit(final_url).scheme.lower() not in {"http", "https"}:
            return  # about:blank, an error page: not a page of any host
        if not await self._guard.allowed_now(final_url):
            raise BrowserRenderError(
                "unsafe_target", "The page ended at an address the guard refuses"
            )

    async def _close_context(self, context: Any, browser: Any) -> None:
        """Close a context of *browser*, for at most ``_CLOSE_TIMEOUT_SECONDS``. A browser that does not
        answer a close is not one to render with: it is shut down, and the next render starts another."""
        try:
            await asyncio.wait_for(context.close(), timeout=_CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Headless browser did not close a context in time; restarting it")
            await self._abandon(browser)
        except Exception:  # noqa: BLE001 - a context that is already gone has nothing to close
            return

    async def _abandon(self, browser: Any) -> None:
        """Shut down a browser that did not answer: the stealth engine alone when it is the one (the
        primary goes on rendering), else the primary and with it everything, as ``stop`` does."""
        if browser is not None and browser is self._stealth_browser:
            await self._stop_stealth()
        else:
            await self.stop()

    async def render(
        self,
        url: str,
        *,
        post_load_wait_ms: Optional[int] = None,
        stealth: bool = False,
    ) -> BrowserPage:
        """Render *url* in a fresh context with every network request guarded.

        The page is read once it has settled (``_settle``), not after a fixed wait: a page whose
        script draws its content a second or three after the load is read with its content.
        If stealth is True or the primary browser times out on a challenge or meets an interstitial,
        it automatically falls back to the stealth engine (Camoufox) when available.
        """
        if not await self._guard.allowed_now(url):
            raise BrowserRenderError("unsafe_target", "Blocked unsafe browser target")

        if stealth:
            return await self.render_stealth(url, post_load_wait_ms=post_load_wait_ms)

        await self.start()
        if self._browser is None:
            raise BrowserRenderError("browser_unavailable", "Headless browser is not running")

        primary_failed: Optional[BrowserRenderError] = None
        rendered_page: Optional[BrowserPage] = None
        try:
            rendered_page = await self._render_with_browser(
                self._browser,
                self._engine or "unknown",
                url,
                post_load_wait_ms=post_load_wait_ms,
            )
        except BrowserRenderError as exc:
            if (
                exc.code in {"timeout", "navigation_failed"}
                and self.has_stealth
                and self._engine != "camoufox"
            ):
                primary_failed = exc
            else:
                raise

        is_challenge = False
        if rendered_page is not None and self.has_stealth and self._engine != "camoufox":
            from web_scraper.web_tools.content import detect_interstitial

            challenge_type = detect_interstitial(
                rendered_page.html, rendered_page.final_url, rendered_page.status_code
            )
            if challenge_type in {"captcha", "access_blocked"}:
                is_challenge = True

        if (
            (primary_failed is not None or is_challenge)
            and self.has_stealth
            and self._engine != "camoufox"
        ):
            logger.info(
                "Primary engine %s encountered challenge or timeout on %s; escalating to stealth engine",
                self._engine,
                url,
            )
            try:
                return await self.render_stealth(url, post_load_wait_ms=post_load_wait_ms)
            except Exception as exc:  # noqa: BLE001 - the page the first engine read (or its failure) stands
                logger.info(
                    "Stealth engine could not read %s either: %s",
                    url,
                    getattr(exc, "code", type(exc).__name__),
                )
                if primary_failed is not None:
                    raise primary_failed from None
                if rendered_page is not None:
                    return rendered_page
                raise

        if primary_failed is not None:
            raise primary_failed
        assert rendered_page is not None
        return rendered_page

    async def render_stealth(
        self,
        url: str,
        *,
        post_load_wait_ms: Optional[int] = None,
    ) -> BrowserPage:
        """Render *url* directly using the stealth browser engine (Camoufox)."""
        if not await self._guard.allowed_now(url):
            raise BrowserRenderError("unsafe_target", "Blocked unsafe browser target")
        browser = await self._ensure_stealth_browser()
        if browser is None:
            raise BrowserUnavailableError(
                self._stealth_reason or "Stealth browser engine (Camoufox) is not available"
            )
        return await self._render_with_browser(
            browser,
            "camoufox",
            url,
            post_load_wait_ms=post_load_wait_ms,
        )

    async def _settle(self, page: Any, least_ms: int, deadline: float) -> _Settled:
        """Wait for a page that has reached DOMContentLoaded to finish drawing itself.

        After the load event, the network at rest (each bounded: a page that never goes quiet is
        waited for at most ``_NETWORK_IDLE_MAX_MS``); then the page's visible text, looked at every
        tenth of a second, until it has not changed for ``settle_stable_ms`` and is more than a
        shell's (a loading placeholder, an empty app root: ``shell_text``), and not before
        *least_ms* (``post_load_wait_ms``) have passed. A page that is still a shell is waited for
        up to ``settle_max_ms``: its script may be about to draw. Nothing is waited for past the
        render's own *deadline* (less the time to read the page in).
        """
        began = self._clock()
        cap_at = max(
            min(began + self.settle_max_ms / 1000, deadline - _DEADLINE_MARGIN_SECONDS), began
        )
        floor_at = min(began + max(least_ms, 0) / 1000, cap_at)
        await self._reach(page, "load", cap_at)
        await self._reach(
            page, "networkidle", min(cap_at, self._clock() + _NETWORK_IDLE_MAX_MS / 1000)
        )

        chars, since, reason = -1, self._clock(), "cap"
        while True:
            now = self._clock()
            seen = await self._visible_text(page)
            if seen.chars != chars:
                chars, since = seen.chars, now
            drawn = chars >= 0 and (
                chars >= self.shell_chars or shell_text(seen.head, self.shell_chars) is None
            )
            if now >= floor_at and drawn and now - since >= self.settle_stable_ms / 1000:
                reason = "stable"
                break
            if now >= cap_at:
                break
            await self._sleep(min(_POLL_SECONDS, max(cap_at - now, 0.0)))
        return _Settled(waited_ms=(self._clock() - began) * 1000, chars=chars, reason=reason)

    async def _reach(self, page: Any, state: str, until: float) -> None:
        """Wait for a load state until the time *until*; a page that does not reach it in time is not an error."""
        remaining_ms = int(max(until - self._clock(), 0.0) * 1000)
        if remaining_ms <= 0:
            return
        with contextlib.suppress(Exception):
            await page.wait_for_load_state(state, timeout=remaining_ms)

    async def _visible_text(self, page: Any) -> _VisibleText:
        """How much text the page shows and its first characters; ``-1`` characters when it cannot be read
        (the page is navigating)."""
        try:
            seen = await page.evaluate(_VISIBLE_TEXT_JS, self.shell_chars)
            return _VisibleText(chars=int(seen["chars"]), head=str(seen["head"]))
        except Exception:  # noqa: BLE001 - a page that is changing under the read is read again
            return _VisibleText(chars=-1, head="")

    async def _content(self, page: Any) -> str:
        """The page's HTML as it is now; a page that is navigating at that moment is asked again."""
        last: Optional[Exception] = None
        for _ in range(3):
            try:
                return str(await page.content())
            except Exception as exc:  # noqa: BLE001 - normalise Playwright errors
                last = exc
                await self._sleep(0.2)
        raise BrowserRenderError("navigation_failed", "Browser could not read the page") from last

    async def _create_context_for(self, browser: Any, engine_name: str) -> Any:
        options: dict[str, Any] = {
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "locale": "en-US",
            "java_script_enabled": True,
            "service_workers": "block",
        }
        if engine_name in {"chrome", "chromium"}:
            options["user_agent"] = _DEFAULT_USER_AGENT
        return await browser.new_context(**options)

    async def _create_context(self) -> Any:
        if self._browser is None:
            raise BrowserRenderError("browser_unavailable", "Headless browser is not running")
        return await self._create_context_for(self._browser, self._engine or "unknown")

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

    async def _stop_stealth(self) -> None:
        """Shut the stealth browser down (the primary engine goes on)."""
        async with self._stealth_lock:
            await self._shutdown_stealth()

    async def _shutdown_stealth(self) -> None:
        # Each step is bounded: a browser that stopped answering is the one being shut down.
        if self._stealth_browser is not None and self._stealth_browser is not self._browser:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._stealth_browser.close(), timeout=_STOP_TIMEOUT_SECONDS)
        if (
            self._stealth_manager is not None
            and self._stealth_manager is not self._camoufox_manager
        ):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._stealth_manager.__aexit__(None, None, None),
                    timeout=_STOP_TIMEOUT_SECONDS,
                )
        self._stealth_browser = None
        self._stealth_manager = None

    async def _shutdown_locked(self) -> None:
        async with self._stealth_lock:
            await self._shutdown_stealth()
        await self._shutdown_primary_locked()

    async def _shutdown_primary_locked(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._browser.close(), timeout=_STOP_TIMEOUT_SECONDS)
        if self._camoufox_manager is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._camoufox_manager.__aexit__(None, None, None),
                    timeout=_STOP_TIMEOUT_SECONDS,
                )
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._playwright.stop(), timeout=_STOP_TIMEOUT_SECONDS)
        self._browser = None
        self._camoufox_manager = None
        self._playwright = None
        self._engine = None

    def snapshot(self) -> dict[str, Any]:
        """Expose non-sensitive browser lifecycle state."""
        return {
            "running": self.running,
            "engine": self._engine,
            "has_stealth": self.has_stealth,
            "stealth_enabled": self.stealth_enabled,
            "stealth_reason": self._stealth_reason,
            "stealth_fetch": self.stealth_fetch,
            "stealth_fetching": bool(self._fetch_task is not None and not self._fetch_task.done()),
            "stealth_active": bool(
                self._stealth_browser is not None and self._stealth_browser.is_connected()
            ),
            "active_contexts": self._active,
            "idle_seconds": self.idle_seconds,
        }
