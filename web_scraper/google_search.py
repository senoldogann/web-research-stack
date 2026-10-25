"""Google Search integration for finding the best sources dynamically.

Google stopped serving results to plain HTTP clients: an ordinary request now
comes back as a consent page or a JavaScript shell with no results in it.  The
searcher therefore tries the cheap HTTP path first (some networks, regions and
residential IPs still get server-rendered results) and falls back to a real
browser through Playwright, which renders the page and reads the results out of
the DOM.  When neither path yields a result the caller gets
:class:`GoogleSearchBlocked` instead of a silent empty list, so the research
pipeline can fall back to another provider instead of pretending Google
answered.
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import date
from typing import Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup

from web_scraper.config import config
from web_scraper.stealth import HeaderFactory
from web_scraper.stealth_camoufox import (
    camoufox_lock,
    close_windows_of,
    get_camoufox_page,
    hold_context,
    landing_allowed,
    release_page,
    warm_up_camoufox,
)
from web_scraper.stealth_camoufox import (
    close_camoufox as close_shared_camoufox,
)

logger = logging.getLogger(__name__)

try:  # curl_cffi impersonates a real Chrome; plain httpx is blocked far sooner
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - optional at import time
    cffi_requests = None  # type: ignore[assignment]


class GoogleSearchBlocked(Exception):
    """Google answered with a consent page, a CAPTCHA or a JavaScript shell."""


# One Chrome at a time: the research pipeline asks Google for several query
# variants at once, and each search would otherwise open its own window.
_BROWSER_LOCK: Optional[asyncio.Semaphore] = None


def _browser_lock() -> asyncio.Semaphore:
    global _BROWSER_LOCK
    if _BROWSER_LOCK is None:
        _BROWSER_LOCK = asyncio.Semaphore(1)
    return _BROWSER_LOCK


# Read the result list out of the rendered page.  Anchors carry a /goto?url=
# wrapper; the caller resolves them.  Shared by Camoufox and Chrome.
_EXTRACT_JS = """() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll('div#rso a h3, div#search a h3').forEach(h => {
    const a = h.closest('a');
    if (!a || !a.href || seen.has(a.href)) return;
    const block = h.closest('div.MjjYud, div.g, div[data-hveid]');
    const snip = block
      ? block.querySelector('div.VwiC3b, div.IsZvec, span.aCOpRe, div[data-sncf]')
      : null;
    seen.add(a.href);
    out.push({
      title: (h.innerText || '').trim(),
      href: a.href,
      snippet: snip ? (snip.innerText || '').trim().slice(0, 300) : ''
    });
  });
  return out;
}"""

# The shared Camoufox is in web_scraper/stealth_camoufox.py: starting it costs ~6 s, a search in it under one.
# It holds ~2 GB across its processes, so it shuts down after a few idle minutes rather than sitting in a
# desktop agent's memory all day. A search never waits for its read longer than the timeout and this margin.
_CAMOUFOX_READ_MARGIN_S = 30.0

# The shared Chrome: a normal browser, its window minimised over CDP.
_CHROME_STATE: Dict[str, object] = {
    "proc": None,
    "profile": None,
    "manager": None,
    "browser": None,
    "page": None,
    "last": 0.0,
}
_CHROME_IDLE_S = 180.0

# Headless Chrome is refused by Google on this machine; the attempt is retried
# at most once an hour rather than on every search.
_HEADLESS_STATE: Dict[str, float] = {"blocked_until": 0.0}
_HEADLESS_COOLDOWN_S = 3600.0
_BACKEND_LOGGED = False


class GoogleSearcher:
    """Search Google and extract top results."""

    BASE_URL = "https://www.google.com/search"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # Google's consent wall is skipped with this cookie; without it every
    # request lands on consent.google.com and never reaches the results.
    CONSENT_COOKIE = {"CONSENT": "YES+cb"}
    CONSENT_LABELS = (
        "Accept all",
        "Reject all",
        "Tümünü kabul et",
        "Tümünü reddet",
        "Alle akzeptieren",
        "Hyväksy kaikki",
    )

    def __init__(
        self,
        headless: Optional[bool] = None,
        allow_visible_browser: Optional[bool] = None,
    ) -> None:
        self.headless = config.google_playwright_headless if headless is None else headless
        self.allow_visible_browser = allow_visible_browser

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        num_results: int = 10,
        *,
        gl: Optional[str] = None,
        hl: Optional[str] = None,
        date_filter: Optional[str] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> List[Dict]:
        """Search Google and return top results.

        ``gl`` is a country (``fi``), ``hl`` a language (``fi``) and ``date_from`` and
        ``date_to`` a custom date range (Google's ``tbs=cdr``); each unset one is Google's
        default for this searcher (``us``, ``en``, any time).

        Two browser paths, neither of which leaves a window on screen for
        longer than the start-up flash: the installed Chrome with its window
        minimised over CDP, then Camoufox (a patched headless Firefox).  A
        refused Chrome falls through to Camoufox instead of ending the search.

        Raises ``GoogleSearchBlocked`` when every path fails, so callers can
        tell "Google refused" from "Google found nothing".
        """
        results: List[Dict] = []
        # Only what the caller set: with nothing set, every path is called as it always was.
        extra = {
            name: value
            for name, value in (
                ("gl", gl),
                ("hl", hl),
                ("date_filter", date_filter),
                ("date_from", date_from),
                ("date_to", date_to),
            )
            if value is not None
        }

        try:
            results = await self._search_http(query, num_results, **extra)
        except Exception as exc:  # network error, curl_cffi missing, bot check
            logger.debug("Google HTTP search failed for %r: %s", query, exc)

        global _BACKEND_LOGGED

        # With a window forbidden, only windowless backends are tried: a
        # refused search must end in an error, never in a visible Chrome.
        allow_visible = (
            config.google_allow_visible_browser
            if self.allow_visible_browser is None
            else self.allow_visible_browser
        )
        if allow_visible:
            paths = ((self._search_chrome, "chrome", "visible"),)
            paths += ((self._search_camoufox, "camoufox", "none"),)
        else:
            paths = ((self._search_camoufox, "camoufox", "none"),)
            paths += ((self._search_headless_chrome, "chrome-headless", "none"),)

        blocked: List[str] = []
        for path, backend, window in paths:
            if results or not config.google_use_playwright:
                break
            try:
                results = await path(query, num_results, **extra)
            except GoogleSearchBlocked as exc:
                # A refused backend must not end the search: the next may pass.
                blocked.append(f"{backend}: {exc}")
                continue
            if results and not _BACKEND_LOGGED:
                logger.info("google search backend=%s window=%s", backend, window)
                _BACKEND_LOGGED = True

        if not results:
            detail = f" ({'; '.join(blocked)})" if blocked else ""
            raise GoogleSearchBlocked(
                "Google returned no parseable results (consent page, bot check or JavaScript-only page)"
                + detail
            )

        return results[:num_results]

    async def warm_up(self) -> None:
        """Start the browser and open Google once, before the first query.

        A cold Camoufox spends ~4 s on Google's first page (cookies, DNS,
        first paint); the same page costs ~0.8 s once warmed.  The API calls
        this at start-up so the cost is not paid inside a request.
        """
        if not config.google_use_playwright:
            return
        allow_visible = (
            config.google_allow_visible_browser
            if self.allow_visible_browser is None
            else self.allow_visible_browser
        )
        if allow_visible:
            page = await self._chrome_page()
            if page is None:
                return
            async with _browser_lock():
                try:
                    await page.goto(
                        "https://www.google.com/ncr", wait_until="commit", timeout=20000
                    )
                    try:
                        await page.wait_for_selector("form, div#rso, #search", timeout=15000)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.debug("google warm-up failed: %s", exc)
        else:
            await warm_up_camoufox()

    # ------------------------------------------------------------------
    # HTTP path
    # ------------------------------------------------------------------

    async def _search_http(self, query: str, num_results: int, **extra) -> List[Dict]:
        if cffi_requests is None:
            return []

        url = self._build_url(query, num_results, **extra)
        async with cffi_requests.AsyncSession(
            timeout=min(config.google_request_timeout_seconds, 3.0), impersonate="chrome120"
        ) as client:
            client.cookies.update(self.CONSENT_COOKIE)
            response = await client.get(url, headers=HeaderFactory.get_headers(url))
            response.raise_for_status()

        return self._parse_results(response.text)

    # ------------------------------------------------------------------
    # Camoufox path: headless, no window
    # ------------------------------------------------------------------

    async def _search_camoufox(self, query: str, num_results: int, **extra) -> List[Dict]:
        """Search through a headless Camoufox, a Firefox built to look ordinary.

        Headless Chromium is caught by Google within a request; Camoufox
        spoofs the whole fingerprint (WebGL, fonts, screen, navigator), so the
        same query comes back with real results and no window on screen.

        The page is one of a context that is held to the address guard, it is read only if it ended at an
        address the guard allows, and the whole read is bounded: a page that stops answering would keep the
        one lock every search takes, so it ends the read and takes its browser with it. A page that served
        the whole search is kept for the next one (a new page costs Camoufox about 3 s); any other is closed.
        """
        page = await get_camoufox_page()
        if page is None:
            return []

        url = self._build_url(query, num_results, **extra)
        timeout_ms = int(config.google_playwright_timeout_seconds * 1000)
        limit = config.google_playwright_timeout_seconds + _CAMOUFOX_READ_MARGIN_S

        served = False
        try:
            async with camoufox_lock():
                raw, html = await asyncio.wait_for(
                    self._read_camoufox_page(page, url, timeout_ms), timeout=limit
                )
            served = True
        except asyncio.TimeoutError as exc:
            await close_shared_camoufox()
            raise GoogleSearchBlocked("Camoufox did not finish reading Google in time") from exc
        finally:
            await release_page(page, reuse=served)

        results = await self._results_from_raw(raw)
        if not results:
            lowered = html.lower()
            if "unusual traffic" in lowered or "captcha" in lowered:
                raise GoogleSearchBlocked("Google served a bot check to Camoufox")
        return results

    async def _read_camoufox_page(self, page, url: str, timeout_ms: int) -> tuple:
        """Open *url* in *page* and read the result list out of it (and, if there is none, its HTML)."""
        await page.goto(url, wait_until="commit", timeout=timeout_ms)
        try:
            await page.wait_for_selector("div#rso, div#search", timeout=min(6000, timeout_ms // 2))
        except Exception:
            await self._dismiss_consent(page)
            try:
                await page.wait_for_selector("div#rso, div#search", timeout=4000)
            except Exception:
                pass
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('div#rso a h3, div#search a h3').length >= 3",
                timeout=3000,
            )
        except Exception:
            pass

        # A redirect the browser followed is not one the guard was asked about: where the page ended is.
        if not await landing_allowed(page):
            raise GoogleSearchBlocked("Google's page ended at an address that is not allowed")
        raw = await page.evaluate(_EXTRACT_JS)
        html = await page.content() if not raw else ""
        return raw, html

    async def close_camoufox(self) -> None:
        """Shut the shared Camoufox down (idle timeout, or at process end)."""
        await close_shared_camoufox()

    async def _results_from_raw(self, raw) -> List[Dict]:
        results: List[Dict] = []
        links = [item for item in raw if item.get("title") and item.get("href")]
        resolved = await self._resolve_links([item["href"] for item in links])

        for item, real_url in zip(links, resolved):
            url_value = self._clean_url(real_url)
            if not url_value:
                continue
            results.append(
                {
                    "title": item["title"],
                    "url": url_value,
                    "snippet": item.get("snippet", ""),
                    "source": self._get_source_name(url_value),
                }
            )
        return results

    # ------------------------------------------------------------------
    # Chrome path: one browser, window minimised before it is seen
    # ------------------------------------------------------------------

    async def _search_chrome(self, query: str, num_results: int, **extra) -> List[Dict]:
        """Search through a Chrome whose window is minimised at once.

        Headless Chrome is caught within a request; a headed one gets the real
        results page.  Minimising the window over CDP keeps it off the screen
        while the page still renders, so the only trace is a short flash when
        Chrome starts.  One Chrome and one tab serve every search: ~2 s to
        start, ~0.4 s a query after that.
        """
        page = await self._chrome_page()
        if page is None:
            return []

        url = self._build_url(query, num_results, **extra)
        timeout_ms = int(config.google_playwright_timeout_seconds * 1000)

        async with _browser_lock():
            await page.goto(url, wait_until="commit", timeout=timeout_ms)
            try:
                await page.wait_for_selector("div#rso, div#search", timeout=timeout_ms // 2)
            except Exception:
                pass
            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('div#rso a h3, div#search a h3').length >= 5",
                    timeout=3000,
                )
            except Exception:
                pass
            if not await landing_allowed(page):
                raise GoogleSearchBlocked("Google's page ended at an address that is not allowed")
            raw = await page.evaluate(_EXTRACT_JS)
            html = await page.content() if not raw else ""

        results = await self._results_from_raw(raw)
        if not results:
            lowered = html.lower()
            if "unusual traffic" in lowered or "captcha" in lowered or "enablejs" in lowered:
                raise GoogleSearchBlocked("Google served a bot check to Chrome")
        return results

    async def _search_headless_chrome(self, query: str, num_results: int, **extra) -> List[Dict]:
        """One-shot headless Chrome: invisible, and usually refused by Google.

        Tried because the refusal is not certain on every network; a refusal is
        remembered for an hour, so it costs one attempt rather than one per
        search.  This is the only Chrome path used when a window is not
        allowed.
        """
        now = time.monotonic()
        if now < _HEADLESS_STATE["blocked_until"]:
            return []

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []

        url = self._build_url(query, num_results, **extra)
        timeout_ms = int(config.google_playwright_timeout_seconds * 1000)
        port = self._free_port()
        profile = tempfile.mkdtemp(prefix="google-headless-profile-")
        process = self._start_chrome(port, profile, headless=True)
        if process is None:
            shutil.rmtree(profile, ignore_errors=True)
            return []

        raw: List[Dict] = []
        html = ""
        try:
            await self._wait_for_cdp(port)
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                # Held to the address guard before the search's page exists, as the Camoufox path is
                # (it was not: a script of the page reached any address); a context the guard cannot
                # hold is not used.
                try:
                    await hold_context(context)
                except Exception as exc:  # noqa: BLE001 - an unheld context is not searched in
                    logger.warning("headless Chrome not held to the address guard: %s", exc)
                    return []
                page = await context.new_page()
                close_windows_of(page)
                try:
                    await page.goto(url, wait_until="commit", timeout=timeout_ms)
                    try:
                        await page.wait_for_function(
                            "() => document.querySelectorAll('div#rso a h3, div#search a h3').length >= 5",
                            timeout=4000,
                        )
                    except Exception:
                        pass
                    # A redirect the browser followed is not one the guard was asked about.
                    if not await landing_allowed(page):
                        raise GoogleSearchBlocked(
                            "Google's page ended at an address that is not allowed"
                        )
                    raw = await page.evaluate(_EXTRACT_JS)
                    if not raw:
                        html = await page.content()
                finally:
                    await page.close()
        finally:
            self._stop_chrome(process, profile)

        results = await self._results_from_raw(raw)
        if not results:
            lowered = html.lower()
            if "unusual traffic" in lowered or "captcha" in lowered or "enablejs" in lowered:
                _HEADLESS_STATE["blocked_until"] = time.monotonic() + _HEADLESS_COOLDOWN_S
                logger.info("headless Chrome was refused; trying it again in an hour")
            return []
        return results

    async def _chrome_page(self):
        """The shared Chrome tab, started once and minimised at once."""
        state = _CHROME_STATE
        now = time.monotonic()
        process = state.get("proc")
        page = state.get("page")

        if page is not None and now - float(state["last"]) > _CHROME_IDLE_S:
            await self.close_chrome()
            page = None

        if (
            page is not None
            and not page.is_closed()
            and process is not None
            and process.poll() is None
        ):
            state["last"] = now
            return page

        await self.close_chrome()

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.debug("playwright is not installed; skipping the Chrome path")
            return None

        port = self._free_port()
        profile = tempfile.mkdtemp(prefix="google-chrome-profile-")
        process = self._start_chrome(port, profile)
        if process is None:
            shutil.rmtree(profile, ignore_errors=True)
            return None

        manager = async_playwright()
        pw = await manager.__aenter__()
        browser = None
        try:
            await self._wait_for_cdp(port)
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            # Held to the address guard before the tab every search uses exists; if it cannot be,
            # this Chrome is not used (the except below stops it).
            await hold_context(context)
            page = await context.new_page()
            close_windows_of(page)
            if not config.google_allow_visible_browser:
                await self._minimize(page)
        except Exception as exc:
            logger.warning("Chrome could not be started: %s", exc)
            await manager.__aexit__(None, None, None)
            self._stop_chrome(process, profile)
            return None

        state.update(
            {
                "proc": process,
                "profile": profile,
                "manager": manager,
                "browser": browser,
                "page": page,
                "last": now,
            }
        )
        return page

    @staticmethod
    async def _minimize(page) -> None:
        """Minimise the window over CDP: no Accessibility permission needed."""
        session = await page.context.new_cdp_session(page)
        window = await session.send("Browser.getWindowForTarget")
        await session.send(
            "Browser.setWindowBounds",
            {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}},
        )
        await session.detach()

    async def close_chrome(self) -> None:
        """Shut the shared Chrome down (idle timeout, or at process end)."""
        state = _CHROME_STATE
        manager = state.get("manager")
        process = state.get("proc")
        profile = state.get("profile")
        state.update(
            {
                "proc": None,
                "profile": None,
                "manager": None,
                "browser": None,
                "page": None,
                "last": 0.0,
            }
        )
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        if isinstance(profile, str):
            shutil.rmtree(profile, ignore_errors=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _chrome_binary(self) -> Optional[str]:
        channel = (config.google_browser_channel or "").strip()
        candidates: List[Optional[str]] = []
        if channel in ("", "chrome"):
            candidates.extend(
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    shutil.which("google-chrome"),
                    shutil.which("google-chrome-stable"),
                    shutil.which("chromium"),
                    shutil.which("chromium-browser"),
                ]
            )
        if channel and channel not in ("chrome",):
            candidates.append(shutil.which(channel))
        found = next((path for path in candidates if path and os.path.exists(path)), None)
        return found or self._playwright_chromium()

    @staticmethod
    def _playwright_chromium() -> Optional[str]:
        """The Chromium Playwright downloaded, for hosts without Chrome (Docker)."""
        roots = [
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
            os.path.expanduser("~/Library/Caches/ms-playwright"),
            os.path.expanduser("~/.cache/ms-playwright"),
            "/app/.playwright",
        ]
        patterns = [
            "chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
            "chromium-*/chrome-win/chrome.exe",
        ]
        for root in [root for root in roots if root and os.path.isdir(root)]:
            for pattern in patterns:
                matches = sorted(glob.glob(os.path.join(root, pattern)))
                if matches:
                    return matches[-1]
        return None

    def _start_chrome(
        self, port: int, profile_dir: str, headless: Optional[bool] = None
    ) -> Optional[subprocess.Popen]:
        binary = self._chrome_binary()
        if binary is None:
            return None
        args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            # Chrome keeps the key of a profile's cookies and passwords in the login keychain ("Chrome Safe
            # Storage"). A throwaway profile has no business there, and where no keychain is found macOS
            # asks the owner on screen ("Keychain Not Found") with a button that resets their keychains.
            # Playwright passes the same two to every Chrome it starts (the second is Linux's).
            "--use-mock-keychain",
            "--password-store=basic",
            "--window-size=400,300"
            if not config.google_allow_visible_browser
            else "--window-size=1366,900",
        ]
        if self.headless if headless is None else headless:
            args.append("--headless=new")
        if os.environ.get("GOOGLE_CHROME_NO_SANDBOX", "").lower() in ("1", "true", "yes"):
            # Chromium's sandbox needs privileges a container usually lacks.
            args.append("--no-sandbox")
        return subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    def _stop_chrome(process: Optional[subprocess.Popen], profile_dir: str) -> None:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        shutil.rmtree(profile_dir, ignore_errors=True)

    @staticmethod
    async def _wait_for_cdp(port: int, timeout_s: float = 15.0) -> None:
        """Wait until Chrome listens for DevTools connections."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return
            except OSError:
                pass
            await asyncio.sleep(0.1)
        raise GoogleSearchBlocked("Chrome did not start in time")

    async def _resolve_links(self, links: List[str]) -> List[str]:
        """Follow Google's ``/goto?url=`` wrapper to the real address.

        curl_cffi is used when it is installed: it keeps the console quiet
        (httpx logs every request at INFO) and carries the same Chrome
        fingerprint as the rest of the searcher.
        """
        if cffi_requests is None:
            return list(links)

        async with cffi_requests.AsyncSession(
            timeout=10.0, impersonate="chrome120", allow_redirects=False
        ) as client:

            async def resolve(link: str) -> str:
                if not self._is_google_wrapper(link):
                    return link
                try:
                    response = await client.get(link, headers={"User-Agent": self.USER_AGENT})
                    location = response.headers.get("location", "")
                    return location if location.startswith("http") else link
                except Exception:
                    return link

            return list(await asyncio.gather(*(resolve(link) for link in links)))

    @staticmethod
    def _is_google_wrapper(link: str) -> bool:
        """Whether *link* is Google's own redirect wrapper (``/goto?url=``, ``/url?q=``), the only kind of
        link that is requested here. A result's address that merely has ``/url?`` in it is a page on some
        other host, and a request to it would go wherever a stranger's page points."""
        try:
            parts = urlparse(link)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        on_google = parts.scheme in ("http", "https") and (
            host == "google.com" or host.endswith(".google.com")
        )
        return on_google and parts.path in ("/goto", "/url")

    @staticmethod
    async def _dismiss_consent(page) -> None:
        for label in GoogleSearcher.CONSENT_LABELS:
            try:
                button = page.get_by_role("button", name=label, exact=False)
                if await button.count():
                    await button.first.click(timeout=3000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _build_url(
        self,
        query: str,
        num_results: int,
        *,
        gl: Optional[str] = None,
        hl: Optional[str] = None,
        date_filter: Optional[str] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> str:
        encoded = quote_plus(query)
        url = f"{self.BASE_URL}?q={encoded}&num={num_results}&hl={hl or 'en'}&gl={gl or 'us'}"
        if date_from or date_to:
            # A custom range: cdr:1, then the bounds as M/D/YYYY (a missing one is open).
            bounds = ["cdr:1"]
            if date_from:
                bounds.append(f"cd_min:{date_from.month}/{date_from.day}/{date_from.year}")
            if date_to:
                bounds.append(f"cd_max:{date_to.month}/{date_to.day}/{date_to.year}")
            url += "&tbs=" + quote_plus(",".join(bounds))
        elif date_filter:
            qdr_map = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
            if date_filter in qdr_map:
                url += f"&tbs={qdr_map[date_filter]}"
        return url

    def _parse_results(self, html: str) -> List[Dict]:
        """Parse Google results from rendered or server-rendered HTML."""
        soup = BeautifulSoup(html, "lxml")
        results: List[Dict] = []
        seen: set = set()

        def add(result: Optional[Dict]) -> None:
            if not result:
                return
            if result["url"] in seen:
                return
            seen.add(result["url"])
            results.append(result)

        # 1. Container-based extraction: works for the classic markup
        #    (div.g) and for the markup Google served through 2023-2025.
        for container in soup.select("div.MjjYud, div.g"):
            add(self._extract_result(container))

        # 2. Heading-based extraction: every result title sits in an h3 inside
        #    the anchor that points at the page.  This survives class renames.
        if not results:
            scope = soup.select_one("div#rso") or soup.select_one("div#search") or soup
            for heading in scope.find_all("h3"):
                anchor = heading.find_parent("a", href=True)
                if anchor is None:
                    continue
                add(self._result_from_anchor(heading, anchor))

        return results

    def _result_from_anchor(self, heading, anchor) -> Optional[Dict]:
        title = heading.get_text(strip=True)
        url = self._clean_url(anchor.get("href", ""))
        if not title or not url:
            return None

        snippet = ""
        block = (
            heading.find_parent("div", class_=re.compile(r"^(MjjYud|g|yuRUbf|kvH3mc)$"))
            or heading.parent
        )
        if block is not None:
            node = block.select_one("div.VwiC3b, div.IsZvec, div[data-sncf], span.aCOpRe")
            if node is not None:
                snippet = node.get_text(" ", strip=True)

        return {
            "title": title,
            "url": url,
            "snippet": snippet[:300],
            "source": self._get_source_name(url),
        }

    def _extract_result(self, container) -> Optional[Dict]:
        """Extract data from a single result container."""
        try:
            title_elem = container.find("h3")
            if not title_elem:
                return None

            link_elem = title_elem.find_parent("a", href=True) or container.find("a", href=True)
            if not link_elem:
                return None

            url = self._clean_url(link_elem.get("href", ""))
            if not url:
                return None

            snippet = ""
            snippet_elem = container.select_one(
                "div.VwiC3b, div.IsZvec, div[data-sncf], span.aCOpRe, div.Lyiue"
            )
            if snippet_elem is not None:
                snippet = snippet_elem.get_text(" ", strip=True)

            return {
                "title": title_elem.get_text(strip=True),
                "url": url,
                "snippet": snippet[:300],
                "source": self._get_source_name(url),
            }
        except Exception:
            return None

    @staticmethod
    def _clean_url(href: str) -> str:
        """Turn a Google redirect or protocol-relative href into a real URL."""
        if not href:
            return ""
        if href.startswith("/url?"):
            query = parse_qs(urlparse(href).query)
            target = (query.get("q") or [""])[0]
            href = unquote(target)
        elif href.startswith("/"):
            return ""

        if not href.startswith("http"):
            return ""

        parsed = urlparse(href)
        if "google." in parsed.netloc:
            return ""
        return href

    def _get_source_name(self, url: str) -> str:
        """Extract source name from URL."""
        try:
            domain = urlparse(url).netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]

            source_map = {
                "reddit.com": "reddit",
                "github.com": "github",
                "stackoverflow.com": "stackoverflow",
                "medium.com": "medium",
                "dev.to": "devto",
                "news.ycombinator.com": "hackernews",
                "arxiv.org": "arxiv",
                "wikipedia.org": "wikipedia",
                "youtube.com": "youtube",
                "twitter.com": "twitter",
                "x.com": "twitter",
            }
            for key, value in source_map.items():
                if key in domain:
                    return value
            return domain.split(".")[0]
        except Exception:
            return "unknown"


async def get_best_sources(query: str, max_sources: int = 5) -> List[Dict]:
    """Get the best sources for a query from Google search."""
    searcher = GoogleSearcher()
    results = await searcher.search(query, num_results=max_sources + 3)

    valid_results = [
        r for r in results if r.get("url") and not r["url"].startswith("https://www.google.com")
    ]

    return valid_results[:max_sources]
