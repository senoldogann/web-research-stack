"""Tests for outbound network safety checks."""

import ipaddress
import socket

import pytest

from web_scraper import network_safety
from web_scraper.network_safety import (
    HostnameResolutionError,
    UnsafeTargetError,
    embedded_ipv4,
    ends_in_a_number,
    is_public_ip_address,
    resolve_checked_addresses,
    resolve_checked_addresses_sync,
    validate_outbound_url,
    validate_outbound_url_syntax,
)
from web_scraper.web_tools.urls import UnsafeUrlError, canonicalize_url, normalize_request_url


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


# Addresses that no request may be made to, however they are written. The IPv6 forms that carry an IPv4 address
# were accepted before 1.3.1 (``::127.0.0.1`` and ``64:ff9b::7f00:1`` were "global" to the standard library).
NOT_PUBLIC = [
    "127.0.0.1",
    "10.1.2.3",
    "172.16.0.9",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",
    "0.0.0.0",
    "224.0.0.1",
    "255.255.255.255",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd00::1",
    "ff02::1",
    "2001:db8::1",
    "2001::1",
    "3fff::1",
    "100::1",
    # IPv4-compatible
    "::127.0.0.1",
    "::7f00:1",
    "::192.168.1.1",
    "::c0a8:101",
    "::10.0.0.1",
    "::169.254.169.254",
    # IPv4-mapped
    "::ffff:127.0.0.1",
    "::ffff:7f00:1",
    "::ffff:192.168.1.1",
    "::ffff:169.254.169.254",
    # IPv4-translated (SIIT)
    "::ffff:0:7f00:1",
    "::ffff:0:c0a8:101",
    # NAT64, well-known prefix and local-use prefix
    "64:ff9b::7f00:1",
    "64:ff9b::c0a8:101",
    "64:ff9b::a9fe:a9fe",
    "64:ff9b::a00:1",
    "64:ff9b:1::7f00:1",
    "64:ff9b:1:ffff:ffff:ffff:ffff:ffff",
    "64:ff9b:1::",
    # 6to4
    "2002:7f00:1::",
    "2002:c0a8:101::1",
    "2002:a9fe:a9fe::1",
    "2002:a00:1::",
]
PUBLIC = [
    "93.184.216.34",
    "8.8.8.8",
    "2606:4700:4700::1111",
    "2a00:1450:4001:81b::200e",
    # a public IPv4 address in the forms: a DNS64 gateway on an IPv6-only network answers these
    "::ffff:93.184.216.34",
    "::93.184.216.34",
    "64:ff9b::5db8:d822",
    "2002:5db8:d822::1",
    "::ffff:0:5db8:d822",
]


def _literal(address: str) -> str:
    return f"[{address}]" if ":" in address else address


class TestAddressLiterals:
    @pytest.mark.parametrize("address", NOT_PUBLIC)
    def test_an_address_that_is_not_public_is_refused_however_it_is_written(
        self, address: str
    ) -> None:
        url = f"http://{_literal(address)}/admin"

        assert is_public_ip_address(address) is False
        with pytest.raises(UnsafeTargetError):
            validate_outbound_url(url)
        with pytest.raises(UnsafeTargetError):
            validate_outbound_url_syntax(url)
        with pytest.raises(UnsafeUrlError):
            canonicalize_url(url)
        with pytest.raises(UnsafeUrlError):
            normalize_request_url(url)

    @pytest.mark.parametrize("address", PUBLIC)
    def test_a_public_address_is_accepted_in_every_form_it_has(self, address: str) -> None:
        url = f"http://{_literal(address)}/page"

        assert is_public_ip_address(address) is True
        assert validate_outbound_url(url) == url
        assert validate_outbound_url_syntax(url) == url
        canonicalize_url(url)
        normalize_request_url(url)

    def test_the_standard_librarys_own_judgement_is_not_what_the_answer_rests_on(self) -> None:
        # It said "global" for these on the Python versions the server supports, whatever their patch level.
        for address in ("::127.0.0.1", "64:ff9b::7f00:1", "::ffff:0:7f00:1"):
            assert is_public_ip_address(address) is False

    @pytest.mark.parametrize(
        ("address", "carried"),
        [
            ("::ffff:127.0.0.1", "127.0.0.1"),
            ("::127.0.0.1", "127.0.0.1"),
            ("::ffff:0:c0a8:101", "192.168.1.1"),
            ("64:ff9b::a9fe:a9fe", "169.254.169.254"),
            ("2002:c0a8:101::1", "192.168.1.1"),
            ("2002:5db8:d822::", "93.184.216.34"),
        ],
    )
    def test_the_ipv4_address_an_ipv6_form_carries(self, address: str, carried: str) -> None:
        assert embedded_ipv4(ipaddress.IPv6Address(address)) == ipaddress.IPv4Address(carried)

    @pytest.mark.parametrize("address", ["2606:4700:4700::1111", "64:ff9b:1::1", "fe80::1"])
    def test_an_address_that_carries_none_is_judged_as_it_is(self, address: str) -> None:
        assert embedded_ipv4(ipaddress.IPv6Address(address)) is None

    def test_a_zone_and_brackets_are_not_part_of_the_address(self) -> None:
        assert is_public_ip_address("[2606:4700:4700::1111]") is True
        assert is_public_ip_address("fe80::1%en0") is False
        with pytest.raises(ValueError):
            is_public_ip_address("127.0.0.1%2e")

    @pytest.mark.parametrize(
        "host",
        [
            "127.1",
            "0x7f.0.0.1",
            "0x7f000001",
            "0177.0.0.1",
            "017700000001",
            "2130706433",
            "1.2.3",
            "10.1",
            "256.1.1.1",
            "foo.123",
        ],
    )
    def test_a_host_spelled_as_a_number_in_an_old_form_is_no_host(self, host: str) -> None:
        # Clients read these as IPv4 addresses and disagree on which: 0177.0.0.1 is 127.0.0.1 to curl and
        # may be 177.0.0.1 to getaddrinfo, so the address that was checked is not the one that is reached.
        assert ends_in_a_number(host)
        with pytest.raises(UnsafeTargetError):
            validate_outbound_url_syntax(f"http://{host}/")
        with pytest.raises(UnsafeUrlError):
            canonicalize_url(f"http://{host}/")

    @pytest.mark.parametrize(
        "host",
        ["example.com", "1password.com", "a1.b2.example.org", "xn--bcher-kva.de", "93.184.216.34"],
    )
    def test_names_and_dotted_addresses_are_not_mistaken_for_those(self, host: str) -> None:
        if host == "93.184.216.34":
            assert ends_in_a_number(host)  # numeric, but it is a plain address and parses as one
        else:
            assert not ends_in_a_number(host)
        validate_outbound_url_syntax(f"https://{host}/")

    def test_private_networks_allowed_lifts_the_address_rule_but_not_localhost(self) -> None:
        assert validate_outbound_url("http://10.0.0.8/", allow_private_networks=True)
        assert validate_outbound_url_syntax("http://[::127.0.0.1]/", allow_private_networks=True)
        with pytest.raises(UnsafeTargetError):
            validate_outbound_url("http://localhost/", allow_private_networks=True)


class TestWhatTheUrlItselfShows:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://user:secret@example.com/",
            "http://user@example.com/",
            "http:///path",
            "http://example.com:99999/",
            "http://example.com:notaport/",
            "http://localhost:8000/",
            "http://app.localhost/",
            "http://LOCALHOST./",
        ],
    )
    def test_a_url_that_can_never_be_a_target_is_refused_without_a_lookup(
        self, url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def lookup(hostname: str, port: int) -> list[str]:
            raise AssertionError("a name was looked up")

        monkeypatch.setattr(network_safety, "resolve_hostname", lookup)

        with pytest.raises(UnsafeTargetError):
            validate_outbound_url_syntax(url)
        with pytest.raises(UnsafeTargetError):
            validate_outbound_url(url)


class TestTheLookup:
    """A name is resolved once and every address of the answer is checked."""

    def test_the_syntax_check_looks_nothing_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def lookup(hostname: str, port: int) -> list[str]:
            raise AssertionError("a name was looked up")

        monkeypatch.setattr(network_safety, "resolve_hostname", lookup)

        assert validate_outbound_url_syntax("https://internal.example.test/") == (
            "https://internal.example.test/"
        )

    def test_the_check_at_one_moment_looks_a_name_up_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lookups: list[tuple[str, int]] = []

        def lookup(hostname: str, port: int) -> list[str]:
            lookups.append((hostname, port))
            return ["93.184.216.34"]

        monkeypatch.setattr(network_safety, "resolve_hostname", lookup)

        validate_outbound_url("https://example.com/")
        validate_outbound_url("http://example.com:8080/")

        assert lookups == [("example.com", 443), ("example.com", 8080)]

    def test_the_addresses_are_unique_and_ipv4_comes_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            network_safety,
            "resolve_hostname",
            lambda hostname, port: [
                "2606:4700:4700::1111",
                "93.184.216.34",
                "93.184.216.34",
                "8.8.8.8",
            ],
        )

        assert resolve_checked_addresses_sync("example.com", 443) == [
            "93.184.216.34",
            "8.8.8.8",
            "2606:4700:4700::1111",
        ]

    @pytest.mark.parametrize(
        "private", ["10.0.0.8", "::ffff:127.0.0.1", "64:ff9b::c0a8:101", "fe80::1%en0", "nonsense"]
    )
    def test_one_address_that_is_not_public_refuses_the_name(
        self, monkeypatch: pytest.MonkeyPatch, private: str
    ) -> None:
        monkeypatch.setattr(
            network_safety, "resolve_hostname", lambda hostname, port: ["93.184.216.34", private]
        )

        with pytest.raises(UnsafeTargetError):
            resolve_checked_addresses_sync("example.com", 443)
        with pytest.raises(UnsafeTargetError):
            validate_outbound_url("https://example.com/")

    def test_private_networks_allowed_returns_what_the_lookup_answered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(network_safety, "resolve_hostname", lambda hostname, port: ["10.0.0.8"])

        assert resolve_checked_addresses_sync("lan.test", 80, allow_private_networks=True) == [
            "10.0.0.8"
        ]

    def test_a_name_that_does_not_resolve_and_a_name_that_is_no_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing(hostname: str, port: int) -> list[str]:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")

        monkeypatch.setattr(network_safety, "resolve_hostname", missing)
        with pytest.raises(HostnameResolutionError) as exc:
            resolve_checked_addresses_sync("nowhere.test", 80)
        assert exc.value.hostname == "nowhere.test"

        def invalid(hostname: str, port: int) -> list[str]:
            raise UnicodeError("label empty or too long")

        monkeypatch.setattr(network_safety, "resolve_hostname", invalid)
        with pytest.raises(UnsafeTargetError) as bad:
            resolve_checked_addresses_sync("a..b", 80)
        assert not isinstance(bad.value, HostnameResolutionError)

        monkeypatch.setattr(network_safety, "resolve_hostname", lambda hostname, port: [])
        with pytest.raises(HostnameResolutionError):
            resolve_checked_addresses_sync("empty.test", 80)

    @pytest.mark.asyncio
    async def test_the_asynchronous_lookup_answers_and_refuses_the_same(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            network_safety,
            "resolve_hostname",
            lambda hostname, port: ["2606:4700:4700::1111", "8.8.8.8"],
        )
        assert await resolve_checked_addresses("example.com", 443) == [
            "8.8.8.8",
            "2606:4700:4700::1111",
        ]

        monkeypatch.setattr(
            network_safety, "resolve_hostname", lambda hostname, port: ["8.8.8.8", "::127.0.0.1"]
        )
        with pytest.raises(UnsafeTargetError):
            await resolve_checked_addresses("example.com", 443)
