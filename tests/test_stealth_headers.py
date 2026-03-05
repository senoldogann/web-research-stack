"""Tests for coherent browser identity generation."""

import re

from curl_cffi import requests

from web_scraper.playwright_scrapers import PlaywrightScraper
from web_scraper.stealth import HeaderFactory


def _extract_chrome_major_from_target(target: str) -> str:
    match = re.search(r"chrome(\d+)", target)
    assert match is not None
    return match.group(1)


def _extract_chrome_major_from_ua(ua: str) -> str:
    match = re.search(r"Chrome/(\d+)\.", ua)
    assert match is not None
    return match.group(1)


def _extract_chrome_major_from_sec_ch_ua(sec_ch_ua: str) -> str:
    match = re.search(r'Google Chrome";v="(\d+)"', sec_ch_ua)
    assert match is not None
    return match.group(1)


def test_get_identity_returns_coherent_tls_and_headers() -> None:
    # Run multiple draws to ensure random identities remain internally coherent.
    for _ in range(30):
        target, headers = HeaderFactory.get_identity()
        target_major = _extract_chrome_major_from_target(target)
        ua_major = _extract_chrome_major_from_ua(headers["user-agent"])
        sec_major = _extract_chrome_major_from_sec_ch_ua(headers["sec-ch-ua"])

        assert target_major == ua_major == sec_major


def test_get_identity_uses_runtime_supported_targets() -> None:
    runtime_targets = {
        name for name in dir(requests.BrowserType) if isinstance(name, str) and name.startswith("chrome")
    }
    assert runtime_targets

    for _ in range(30):
        target, _ = HeaderFactory.get_identity()
        assert target in runtime_targets


def test_cloudflare_challenge_signature_detection_helper() -> None:
    challenge_html = "<html><title>Just a moment...</title><body>Cloudflare challenge</body></html>"
    normal_html = "<html><title>Example</title><body>Welcome</body></html>"

    assert PlaywrightScraper._is_cloudflare_challenge_html(challenge_html) is True
    assert PlaywrightScraper._is_cloudflare_challenge_html(normal_html) is False
