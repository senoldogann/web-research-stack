"""Tests for headless-only browser selection and request guarding."""

from __future__ import annotations

from web_scraper.web_tools.browser import (
    browser_launch_candidates,
    browser_request_allowed,
)


def test_auto_candidates_are_always_headless_with_isolated_profile_flags() -> None:
    candidates = browser_launch_candidates("auto")

    assert [candidate.engine for candidate in candidates] == ["chrome", "chromium", "camoufox"]
    for candidate in candidates:
        assert candidate.headless is True
        if candidate.engine in {"chrome", "chromium"}:
            assert candidate.args
            assert "--user-data-dir" not in " ".join(candidate.args)


def test_explicit_camoufox_preference_does_not_include_chrome() -> None:
    candidates = browser_launch_candidates("camoufox")

    assert [candidate.engine for candidate in candidates] == ["camoufox"]


def test_private_and_unsupported_targets_are_blocked() -> None:
    assert browser_request_allowed("https://93.184.216.34/") is True
    assert browser_request_allowed("http://127.0.0.1/admin") is False
    assert browser_request_allowed("http://localhost/admin") is False
    assert browser_request_allowed("file:///etc/passwd") is False
    assert browser_request_allowed("data:text/html,hello") is False
