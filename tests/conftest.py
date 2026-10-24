"""Test configuration for local package imports."""

from __future__ import annotations

import os
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
