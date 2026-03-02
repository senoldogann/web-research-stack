"""Tests for outbound network safety checks."""

import pytest

from web_scraper.network_safety import UnsafeTargetError, validate_outbound_url


def test_validate_outbound_url_rejects_loopback_ip() -> None:
    with pytest.raises(UnsafeTargetError):
        validate_outbound_url("http://127.0.0.1/admin")


def test_validate_outbound_url_rejects_ipv6_loopback() -> None:
    """IPv6 loopback ::1 must be blocked."""
    with pytest.raises(UnsafeTargetError):
        validate_outbound_url("http://[::1]/admin")


def test_validate_outbound_url_rejects_link_local_aws_metadata() -> None:
    """AWS metadata endpoint 169.254.169.254 is link-local and must be blocked."""
    with pytest.raises(UnsafeTargetError):
        validate_outbound_url("http://169.254.169.254/latest/meta-data/")


def test_validate_outbound_url_rejects_unspecified_address() -> None:
    """0.0.0.0 is not globally routable and must be blocked."""
    with pytest.raises(UnsafeTargetError):
        validate_outbound_url("http://0.0.0.0/")


def test_validate_outbound_url_rejects_ipv4_mapped_ipv6_loopback() -> None:
    """IPv4-mapped IPv6 ::ffff:127.0.0.1 resolves to loopback and must be blocked."""
    with pytest.raises(UnsafeTargetError):
        validate_outbound_url("http://[::ffff:127.0.0.1]/")


def test_validate_outbound_url_rejects_private_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "web_scraper.network_safety.resolve_hostname",
        lambda hostname, port: ["10.0.0.8"],
    )

    with pytest.raises(UnsafeTargetError):
        validate_outbound_url("https://internal.example.test")


def test_validate_outbound_url_allows_public_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "web_scraper.network_safety.resolve_hostname",
        lambda hostname, port: ["93.184.216.34"],
    )

    assert validate_outbound_url("https://example.com/articles?id=1") == (
        "https://example.com/articles?id=1"
    )
