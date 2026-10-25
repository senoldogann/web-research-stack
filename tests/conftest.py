"""Test configuration for local package imports."""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# The API lifespan warms a real Google browser; the tests must not.
os.environ.setdefault("GOOGLE_WARMUP_ON_STARTUP", "false")
# The crawl jobs the server keeps live in a data folder; a test keeps none there.
os.environ.setdefault("WEB_TOOLS_DATA_DIR", "")

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebinding_support import FakeInternet, LocalService, RebindingDNS, World  # noqa: E402


def _on_this_mac(host: object) -> bool:
    return str(host) in {"localhost", "::1", "0.0.0.0", ""} or str(host).startswith("127.")


@pytest.fixture(autouse=True)
def _nothing_leaves_this_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches outside this Mac. A connection that a test's Python code makes to an address that is not the
    loopback is refused where it is made, and says so. (A browser is another process and libcurl does not use
    Python's sockets: the one browser a test can start by accident, Camoufox, is contained below.)"""
    real_connect = socket.socket.connect

    def connect(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) and address else None
        if (
            self.family in (socket.AF_INET, socket.AF_INET6)
            and host is not None
            and not _on_this_mac(host)
        ):
            raise OSError(f"a test tried to reach {host}, outside this Mac")
        real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture(autouse=True)
def _camoufox_reaches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real Camoufox that a test starts by accident (a page that cannot be read makes the manager try the second
    browser, when one is on the machine) calls out by itself: its ad blocker fetches filter lists, and Camoufox
    downloads what it lacks. It is made to use a proxy that is not there; a test that wants another sets its own."""
    try:
        from camoufox import async_api
    except Exception:  # noqa: BLE001 - not installed, or not importable here: nothing to contain
        return
    original = async_api.AsyncCamoufox

    class Contained(original):  # type: ignore[valid-type, misc]
        def __init__(self, **options: object) -> None:
            options.setdefault(
                "proxy", {"server": "http://127.0.0.1:9", "bypass": "127.0.0.1,localhost"}
            )
            super().__init__(**options)

    monkeypatch.setattr(async_api, "AsyncCamoufox", Contained)


@pytest.fixture(autouse=True)
def _nothing_is_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test downloads Camoufox. The manager fetches what Camoufox lacks by running a command; if a test lets it,
    the command it runs is this one, which does nothing and fails."""
    from web_scraper.web_tools import browser as browser_module

    monkeypatch.setattr(
        browser_module, "_STEALTH_FETCH_COMMAND", (sys.executable, "-c", "raise SystemExit(3)")
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """DNS that rebinds, an Internet that is in memory, and an owner-only service on the loopback."""
    local = LocalService()
    dns, internet = RebindingDNS(), FakeInternet()
    dns.install(monkeypatch)
    internet.install(monkeypatch)
    try:
        yield World(dns, internet, local)
    finally:
        local.close()
