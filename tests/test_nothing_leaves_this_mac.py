"""No test reaches outside this Mac (``tests/conftest.py``).

The suite did: with ``camoufox`` a dependency, a test that read a page that could not be read made the manager try
the second browser, which downloaded Camoufox (a browser of about 100 MB, 2.5 GB on disk) from GitHub and
addons.mozilla.org, and a research-agent test asked a real search collector. A connection a test's Python code makes
to an address that is not the loopback is now refused where it is made, and a Camoufox started by accident has a proxy
that is not there.
"""

from __future__ import annotations

import socket

import pytest


def test_a_connection_outside_this_mac_is_refused() -> None:
    with pytest.raises(OSError, match="outside this Mac"):
        socket.create_connection(
            ("203.0.113.1", 80), timeout=1
        )  # a documentation address: it is nobody's


def test_a_connection_to_the_loopback_is_not() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        socket.create_connection(server.getsockname(), timeout=2).close()
    finally:
        server.close()


def test_a_camoufox_started_by_accident_has_a_proxy_that_is_not_there() -> None:
    camoufox = pytest.importorskip("camoufox")

    contained = camoufox.async_api.AsyncCamoufox(headless=True)
    chosen = camoufox.async_api.AsyncCamoufox(headless=True, proxy={"server": "http://127.0.0.1:1"})

    assert contained.launch_options["proxy"]["server"] == "http://127.0.0.1:9"
    assert (
        chosen.launch_options["proxy"]["server"] == "http://127.0.0.1:1"
    )  # a test that sets its own keeps it
