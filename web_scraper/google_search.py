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
from typing import Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from web_scraper.config import config
from web_scraper.stealth import HeaderFactory

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

    def __init__(self, headless: Optional[bool] = None) -> None:
        self.headless = config.google_playwright_headless if headless is None else headless

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(self, query: str, num_results: int = 10) -> List[Dict]:
        """Search Google and return top results.

        Tries HTTP first, then a rendered browser.  Raises
        ``GoogleSearchBlocked`` when both paths fail, so callers can tell
        "Google refused" from "Google found nothing".
        """
        results: List[Dict] = []

        try:
            results = await self._search_http(query, num_results)
        except Exception as exc:  # network error, curl_cffi missing, bot check
            logger.debug("Google HTTP search failed for %r: %s", query, exc)

        if not results and config.google_use_playwright:
            results = await self._search_browser(query, num_results)

        if not results:
            raise GoogleSearchBlocked(
                "Google returned no parseable results (consent page, bot check or JavaScript-only page)"
            )

        return results[:num_results]

    # ------------------------------------------------------------------
    # HTTP path
    # ------------------------------------------------------------------

    async def _search_http(self, query: str, num_results: int) -> List[Dict]:
        if cffi_requests is None:
            return []

        url = self._build_url(query, num_results)
        async with cffi_requests.AsyncSession(
            timeout=config.google_request_timeout_seconds, impersonate="chrome120"
        ) as client:
            client.cookies.update(self.CONSENT_COOKIE)
            response = await client.get(url, headers=HeaderFactory.get_headers(url))
            response.raise_for_status()

        return self._parse_results(response.text)

    # ------------------------------------------------------------------
    # Browser path
    # ------------------------------------------------------------------

    async def _search_browser(self, query: str, num_results: int) -> List[Dict]:
        async with _browser_lock():
            return await self._search_browser_once(query, num_results)

    async def _search_browser_once(self, query: str, num_results: int) -> List[Dict]:
        """Render Google in a normal Chrome started without automation flags.

        Playwright's own launch leaves ``--enable-automation`` and a
        remote-debugging pipe behind, which Google detects at once ("unusual
        traffic").  A Chrome started by hand with only ``--remote-debugging-port``
        and a throwaway profile gets the real results page, so we start the
        browser ourselves and attach over CDP.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise GoogleSearchBlocked(
                "playwright is not installed: pip install -e '.[playwright]'"
            ) from exc

        url = self._build_url(query, num_results)
        timeout_ms = int(config.google_playwright_timeout_seconds * 1000)

        port = self._free_port()
        profile_dir = tempfile.mkdtemp(prefix="google-search-profile-")
        process = self._start_chrome(port, profile_dir)
        try:
            await self._wait_for_cdp(port)

            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        await page.wait_for_selector("div#rso, div#search", timeout=timeout_ms // 2)
                    except Exception:
                        pass
                    # Results stream in after the container appears; a fixed
                    # sleep sometimes caught only the first two.
                    try:
                        await page.wait_for_function(
                            "() => document.querySelectorAll('div#rso a h3, div#search a h3').length >= 5",
                            timeout=5000,
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(300)

                    raw = await page.evaluate(
                        """() => {
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
                    )
                    html = await page.content()
                finally:
                    await page.close()
        finally:
            self._stop_chrome(process, profile_dir)

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

        if not results:
            lowered = html.lower()
            if "unusual traffic" in lowered or "captcha" in lowered or "enablejs" in lowered:
                raise GoogleSearchBlocked("Google served a bot check to the browser")
        return results

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

    def _start_chrome(self, port: int, profile_dir: str) -> Optional[subprocess.Popen]:
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
            "--window-size=1366,900",
        ]
        if self.headless:
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
        deadline = time.monotonic() + timeout_s
        url = f"http://127.0.0.1:{port}/json/version"
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    response = await client.get(url)
                if response.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.25)
        raise GoogleSearchBlocked("Chrome did not start in time")

    async def _resolve_links(self, links: List[str]) -> List[str]:
        """Follow Google's ``/goto?url=`` wrapper to the real address."""
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:

            async def resolve(link: str) -> str:
                if "google.com/goto" not in link and "/url?" not in link:
                    return link
                try:
                    response = await client.get(link, headers={"User-Agent": self.USER_AGENT})
                    location = response.headers.get("location", "")
                    return location if location.startswith("http") else link
                except Exception:
                    return link

            return list(await asyncio.gather(*(resolve(link) for link in links)))

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

    def _build_url(self, query: str, num_results: int) -> str:
        encoded = quote_plus(query)
        return f"{self.BASE_URL}?q={encoded}&num={num_results}&hl=en&gl=us"

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
