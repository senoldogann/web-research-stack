"""Dynamic header factory for browser impersonation."""

import random
from typing import Dict, List, Optional


class HeaderFactory:
    """Generates high-fidelity browser headers for stealth scraping."""

    # Maps curl_cffi impersonate target → UA version string.
    # Only include targets verified against the installed curl_cffi release.
    # Refresh after upgrading: python3 -c "from curl_cffi import requests;
    #   print([x for x in dir(requests.BrowserType) if 'chrome' in x.lower()])"
    _IMPERSONATE_MAP: Dict[str, str] = {
        "chrome142": "142.0.0.0",
        "chrome136": "136.0.0.0",
        "chrome133a": "133.0.0.0",
        "chrome131": "131.0.0.0",
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

    @classmethod
    def get_impersonate_target(cls) -> str:
        """Return a curl_cffi impersonate string for a random supported Chrome version."""
        return random.choice(list(cls._IMPERSONATE_MAP.keys()))  # noqa: S311

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

        Args:
            url: Target URL — used to populate the ``authority`` header.
            referer: Optional referer string. When supplied the ``sec-fetch-site``
                header is set to ``"cross-site"`` to match real browser behaviour
                when the user clicks a search-engine result.
        """
        version = random.choice(cls.CHROME_VERSIONS)  # noqa: S311
        os_name = random.choice(cls.OS_PLATFORMS)  # noqa: S311

        user_agent = cls._UA_TEMPLATES[os_name].replace("{version}", version)
        # sec-ch-ua-platform expects a quoted string
        platform_header = f'"{os_name}"'

        # When arriving from a search engine the browser sets sec-fetch-site to
        # "cross-site".  Without a referer it stays "none" (direct navigation).
        sec_fetch_site = "cross-site" if referer else "none"

        headers = {
            "upgrade-insecure-requests": "1",
            "user-agent": user_agent,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-ch-ua": f'"Not:A-Brand";v="99", "Google Chrome";v="{version.split(".")[0]}", "Chromium";v="{version.split(".")[0]}"',
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
