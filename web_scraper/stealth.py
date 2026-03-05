"""Dynamic header factory for browser impersonation."""

import random
import re
from typing import Dict, List, Optional, Tuple


class HeaderFactory:
    """Generates high-fidelity browser headers for stealth scraping.

    CRITICAL: The TLS fingerprint (curl_cffi ``impersonate`` target) and the
    HTTP ``User-Agent`` header MUST advertise the **same** Chrome major version.
    A mismatch (e.g. TLS says Chrome 131 but UA says Chrome 142) is a trivial
    bot-detection signal used by Cloudflare, Akamai, and PerimeterX.

    Use :meth:`get_identity` to obtain a coherent (impersonate, headers) pair
    rather than calling ``get_impersonate_target`` and ``get_headers``
    independently.
    """

    # Maps curl_cffi impersonate target → UA version string.
    # Only include targets verified against the installed curl_cffi release.
    # Refresh after upgrading: python3 -c "from curl_cffi import requests;
    #   print([x for x in dir(requests.BrowserType) if 'chrome' in x.lower()])"
    _IMPERSONATE_MAP: Dict[str, str] = {
        "chrome142": "142.0.0.0",
        "chrome136": "136.0.0.0",
        "chrome133a": "133.0.0.0",
        "chrome131": "131.0.0.0",
        "chrome124": "124.0.0.0",
        "chrome123": "123.0.0.0",
        "chrome120": "120.0.0.0",
        "chrome119": "119.0.0.0",
        "chrome116": "116.0.0.0",
        "chrome110": "110.0.0.0",
        "chrome107": "107.0.0.0",
        "chrome104": "104.0.0.0",
        "chrome101": "101.0.0.0",
        "chrome100": "100.0.0.0",
        "chrome99": "99.0.0.0",
    }
    # Kept for backward-compat (header generation still needs the dotted string).
    CHROME_VERSIONS = list(_IMPERSONATE_MAP.values())
    OS_PLATFORMS = ["macOS", "Windows", "Linux"]

    # Realistic referers used when navigating to an external site from search
    _SEARCH_REFERERS: List[str] = [
        "https://www.google.com/",
        "https://www.google.com/search?q=",
        "https://duckduckgo.com/",
        "https://www.bing.com/search?q=",
    ]

    # UA templates keyed by OS platform (must match sec-ch-ua-platform values)
    _UA_TEMPLATES = {
        "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36",
        "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36",
        "Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36",
    }

    # Lazily populated runtime capabilities cache.
    _SUPPORTED_TARGETS_CACHE: Optional[Tuple[str, ...]] = None

    @classmethod
    def _extract_chrome_major(cls, target: str) -> Optional[int]:
        match = re.search(r"chrome(\d+)", target)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @classmethod
    def _resolve_supported_targets(cls) -> Tuple[str, ...]:
        """Return impersonate targets supported by installed curl_cffi runtime."""
        if cls._SUPPORTED_TARGETS_CACHE is not None:
            return cls._SUPPORTED_TARGETS_CACHE

        supported: Tuple[str, ...]
        try:
            from curl_cffi import requests

            runtime_targets = {
                name
                for name in dir(requests.BrowserType)
                if isinstance(name, str) and name.startswith("chrome")
            }

            mapped = tuple(t for t in cls._IMPERSONATE_MAP if t in runtime_targets)
            if mapped:
                supported = mapped
            elif runtime_targets:
                # Fallback: choose the highest available Chrome target from runtime.
                highest = sorted(
                    runtime_targets,
                    key=lambda item: cls._extract_chrome_major(item) or -1,
                )[-1]
                major = cls._extract_chrome_major(highest)
                if major is None:
                    supported = ("chrome120",)
                else:
                    dotted = f"{major}.0.0.0"
                    cls._IMPERSONATE_MAP.setdefault(highest, dotted)
                    cls.CHROME_VERSIONS = list(dict.fromkeys(cls._IMPERSONATE_MAP.values()))
                    supported = (highest,)
            else:
                supported = ("chrome120",)
        except Exception:
            # Keep scraper operational even when runtime probing fails.
            supported = ("chrome120",)

        cls._SUPPORTED_TARGETS_CACHE = supported
        return supported

    @classmethod
    def _pick_identity(cls) -> Tuple[str, str, str]:
        """Return a coherent (impersonate_target, version, os_name) triple.

        All three values are chosen from a single random draw so TLS fingerprint,
        User-Agent version, and sec-ch-ua all agree on the same Chrome build.
        """
        supported_targets = cls._resolve_supported_targets()
        target = random.choice(list(supported_targets))  # noqa: S311

        major = cls._extract_chrome_major(target)
        version = cls._IMPERSONATE_MAP.get(target) or (
            f"{major}.0.0.0" if major is not None else "120.0.0.0"
        )
        os_name = random.choice(cls.OS_PLATFORMS)  # noqa: S311
        return target, version, os_name

    @classmethod
    def get_identity(
        cls,
        url: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> Tuple[str, Dict[str, str]]:
        """Return a coherent ``(impersonate_target, headers)`` pair.

        This is the **recommended** entry point.  It guarantees that the TLS
        fingerprint and HTTP headers advertise the same Chrome version.
        """
        target, version, os_name = cls._pick_identity()
        headers = cls._build_headers(version, os_name, url=url, referer=referer)
        return target, headers

    @classmethod
    def get_impersonate_target(cls) -> str:
        """Return a curl_cffi impersonate string for a random supported Chrome version.

        .. deprecated:: Use :meth:`get_identity` instead to avoid version mismatch.
        """
        return random.choice(list(cls._resolve_supported_targets()))  # noqa: S311

    @classmethod
    def get_referer(cls, query: Optional[str] = None) -> str:
        """Return a realistic search-engine referer string.

        When a *query* is provided the referer is built as a Google/DDG search
        URL so the target server sees us arriving from a search results page,
        which is the most common real-user pattern.
        """
        from urllib.parse import quote_plus

        base = random.choice(cls._SEARCH_REFERERS)  # noqa: S311
        if query and base.endswith("q="):
            return base + quote_plus(query)
        return base

    @classmethod
    def get_headers(
        cls, url: Optional[str] = None, referer: Optional[str] = None
    ) -> Dict[str, str]:
        """Generate a complete set of browser headers.

        .. deprecated:: Use :meth:`get_identity` instead to avoid version mismatch.

        When called standalone (without :meth:`get_identity`), the Chrome version
        in the headers is chosen randomly and may NOT match the TLS fingerprint.
        """
        version = random.choice(cls.CHROME_VERSIONS)  # noqa: S311
        os_name = random.choice(cls.OS_PLATFORMS)  # noqa: S311
        return cls._build_headers(version, os_name, url=url, referer=referer)

    @classmethod
    def _build_headers(
        cls,
        version: str,
        os_name: str,
        url: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> Dict[str, str]:
        """Build the full header dict for a given Chrome *version* and *os_name*."""
        user_agent = cls._UA_TEMPLATES[os_name].replace("{version}", version)
        platform_header = f'"{os_name}"'
        major = version.split(".")[0]

        sec_fetch_site = "cross-site" if referer else "none"

        headers = {
            "upgrade-insecure-requests": "1",
            "user-agent": user_agent,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-ch-ua": f'"Not:A-Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": platform_header,
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": sec_fetch_site,
            "sec-fetch-user": "?1",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-US,en;q=0.9",
            "priority": "u=0, i",
        }

        if referer:
            headers["referer"] = referer

        if url:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            headers["authority"] = parsed.netloc

        return headers
