"""libcurl resolves host names itself, so the sessions that use it are held to the addresses that were checked.

The first half runs the real libcurl (through curl_cffi) against a server on the loopback, for names no resolver knows
(``.test``): a request for such a name that arrives proves libcurl took the address from the pin and asked nobody. The
second half runs the older scrapers (``WebScraper``, ``WebScraperAsync``) with a recording client in place of the session.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from curl_cffi import requests
from curl_cffi.const import CurlOpt
from rebinding_support import PUBLIC_A, World

from web_scraper.async_scrapers import WebScraperAsync
from web_scraper.curl_pins import CurlPins, check_peer, pin_url, pin_url_async
from web_scraper.network_safety import UnsafeTargetError
from web_scraper.scrapers import WebScraper
from web_scraper.stealth import HeaderFactory

PIN_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class TestThePinList:
    def test_an_entry_is_host_port_and_the_checked_addresses_with_ipv6_in_brackets(self) -> None:
        pins = CurlPins()

        pins.pin("example.com", 443, ["93.184.216.34", "2606:4700:4700::1111"])

        assert pins.options[CurlOpt.RESOLVE] == [
            "example.com:443:93.184.216.34,[2606:4700:4700::1111]"
        ]

    def test_a_proxy_is_always_switched_off(self) -> None:
        # libcurl reads HTTPS_PROXY on its own; a proxy resolves names itself.
        assert CurlPins().options[CurlOpt.PROXY] == ""

    def test_the_next_check_of_a_host_replaces_its_entry_and_the_list_object_stays_the_same(
        self,
    ) -> None:
        pins = CurlPins()
        held = pins.options[CurlOpt.RESOLVE]

        pins.pin("example.com", 443, ["93.184.216.34"])
        pins.pin("other.example", 80, ["8.8.8.8"])
        pins.pin("example.com", 443, ["93.184.216.35"])

        assert pins.options[CurlOpt.RESOLVE] is held  # the session holds this very list
        assert held == ["other.example:80:8.8.8.8", "example.com:443:93.184.216.35"]

    def test_the_oldest_pins_go_first_when_there_are_too_many(self) -> None:
        pins = CurlPins(limit=2)

        for index in range(4):
            pins.pin(f"h{index}.example", 80, ["8.8.8.8"])

        assert pins.options[CurlOpt.RESOLVE] == ["h2.example:80:8.8.8.8", "h3.example:80:8.8.8.8"]


class TestTheLastLine:
    @pytest.mark.parametrize(
        "peer", ["127.0.0.1", "192.168.1.9", "::1", "::ffff:10.0.0.1", "64:ff9b::7f00:1"]
    )
    def test_an_answer_from_an_address_that_is_not_public_is_refused(self, peer: str) -> None:
        class Answer:
            primary_ip = peer

        with pytest.raises(UnsafeTargetError):
            check_peer(Answer())
        check_peer(Answer(), allow_private_networks=True)

    @pytest.mark.parametrize("peer", ["93.184.216.34", "2606:4700:4700::1111", "", None])
    def test_a_public_peer_and_an_unknown_one_pass(self, peer: Any) -> None:
        class Answer:
            primary_ip = peer

        check_peer(Answer())
        check_peer(object())


class TestPinnedUrls:
    @pytest.mark.asyncio
    async def test_the_url_curl_gets_is_the_normalized_one_so_that_the_pin_is_found(
        self, world: World
    ) -> None:
        # libcurl matches a pin by the host as written: "example.com." is not "example.com" to it.
        world.dns.rebinds("rebind.test", PUBLIC_A)
        pins = CurlPins()

        url = await pin_url_async(pins, "HTTP://Rebind.TEST.:8080/Path/?q=1#fragment")

        assert url == "http://rebind.test:8080/Path/?q=1"
        assert pins.options[CurlOpt.RESOLVE] == [f"rebind.test:8080:{PUBLIC_A}"]
        assert world.dns.lookups == {"rebind.test": 1}

    def test_the_synchronous_twin_does_the_same(self, world: World) -> None:
        world.dns.rebinds("rebind.test", PUBLIC_A)
        pins = CurlPins()

        assert pin_url(pins, "https://REBIND.test./x") == "https://rebind.test/x"
        assert pins.options[CurlOpt.RESOLVE] == [f"rebind.test:443:{PUBLIC_A}"]

    @pytest.mark.asyncio
    async def test_an_address_literal_needs_no_pin_and_a_private_one_is_refused(
        self, world: World
    ) -> None:
        pins = CurlPins()

        assert await pin_url_async(pins, "http://93.184.216.34/x") == "http://93.184.216.34/x"
        assert pins.options[CurlOpt.RESOLVE] == []
        for url in (
            "http://127.0.0.1/",
            "http://[::127.0.0.1]/",
            "http://0177.0.0.1/",
            "http://127.1/",
        ):
            with pytest.raises(UnsafeTargetError):
                await pin_url_async(pins, url)
            with pytest.raises(UnsafeTargetError):
                pin_url(pins, url)
        assert world.dns.lookups == {}

    @pytest.mark.asyncio
    async def test_a_name_that_answers_a_private_address_is_not_pinned(self, world: World) -> None:
        world.dns.answers("rebind.test", [PUBLIC_A, "192.168.1.9"])
        pins = CurlPins()

        with pytest.raises(UnsafeTargetError):
            await pin_url_async(pins, "http://rebind.test/")

        assert pins.options[CurlOpt.RESOLVE] == []


class TestLibcurlTakesTheAddressFromThePin:
    """The real libcurl, a name nobody can resolve, a server on the loopback."""

    @pytest.fixture(autouse=True)
    def no_proxy_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in PIN_NAMES:
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.asyncio
    async def test_a_pinned_name_is_connected_to_the_pinned_address(self, world: World) -> None:
        port = world.local.port
        pins = CurlPins()
        pins.pin("pinned.test", port, ["127.0.0.1"])

        async with requests.AsyncSession(curl_options=pins.options) as session:
            response = await session.get(f"http://pinned.test:{port}/a", timeout=10)

        assert response.status_code == 200 and response.primary_ip == "127.0.0.1"
        assert world.local.seen == [f"GET /a  Host: pinned.test:{port}"]

    @pytest.mark.asyncio
    async def test_a_pinned_ipv6_address_in_brackets_is_connected_to(self, world: World) -> None:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            probe.bind(("::1", 0))
        except OSError:
            pytest.skip("this machine has no IPv6 loopback")
        finally:
            probe.close()
        # The server of the fixture listens on IPv4 only, so the pin lists a dead IPv6 address first: libcurl
        # must go on to the next address of the entry.
        port = world.local.port
        pins = CurlPins()
        pins.pin("pinned.test", port, ["::1", "127.0.0.1"])

        async with requests.AsyncSession(curl_options=pins.options) as session:
            response = await session.get(f"http://pinned.test:{port}/b", timeout=10)

        assert response.status_code == 200
        assert world.local.seen == [f"GET /b  Host: pinned.test:{port}"]

    @pytest.mark.asyncio
    async def test_a_proxy_of_the_environment_is_not_used(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # trust_env=False does not stop libcurl reading these; the empty proxy of the pins does.
        for name in PIN_NAMES:
            monkeypatch.setenv(name, "http://127.0.0.1:9")
        port = world.local.port
        pins = CurlPins()
        pins.pin("pinned.test", port, ["127.0.0.1"])

        async with requests.AsyncSession(curl_options=pins.options, trust_env=False) as session:
            response = await session.get(f"http://pinned.test:{port}/c", timeout=10)

        assert response.status_code == 200
        assert world.local.seen == [f"GET /c  Host: pinned.test:{port}"]

    @pytest.mark.asyncio
    async def test_a_name_written_in_capitals_with_a_dot_reaches_the_pinned_address_once_normalized(
        self, world: World
    ) -> None:
        port = world.local.port
        world.dns.answers("pinned.test", ["127.0.0.1"])
        pins = CurlPins()

        # Private networks allowed only so that the loopback can stand for the public address of the test.
        url = await pin_url_async(
            pins, f"http://PINNED.test.:{port}/Path?q=1", allow_private_networks=True
        )
        async with requests.AsyncSession(curl_options=pins.options) as session:
            response = await session.get(url, timeout=10)

        assert response.status_code == 200
        assert world.local.seen == [f"GET /Path?q=1  Host: pinned.test:{port}"]

    @pytest.mark.asyncio
    async def test_the_async_scraper_reads_a_pinned_name_through_the_real_session(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Which browsers curl_cffi can impersonate depends on its release; this test is not about that.
        monkeypatch.setattr(HeaderFactory, "get_impersonate_target", classmethod(lambda cls: None))
        port = world.local.port
        world.dns.answers("pinned.test", ["127.0.0.1"])

        async with WebScraperAsync(allow_private_networks=True, timeout=10) as scraper:
            result = await scraper.scrape(f"http://pinned.test:{port}/admin")

        assert result.error is None and result.status_code == 200
        assert world.local.seen == [f"GET /admin  Host: pinned.test:{port}"]


class _Answer:
    def __init__(self, status: int = 200, location: str = "", url: str = "") -> None:
        self.status_code = status
        self.headers = {"location": location} if location else {}
        self.url = url
        self.primary_ip = ""


class _RecordingClient:
    """Stands for the curl session: it keeps the URL and the pins the scraper had made when it asked."""

    def __init__(self, scraper: Any, *answers: _Answer) -> None:
        self.scraper = scraper
        self.answers = list(answers)
        self.asked: list[tuple[str, list[str]]] = []

    def get(self, url: str) -> _Answer:
        self.asked.append((url, list(self.scraper._pins.options[CurlOpt.RESOLVE])))
        answer = self.answers.pop(0)
        answer.url = url
        return answer

    async def aget(self, url: str) -> _Answer:
        return self.get(url)


class TestTheOlderScrapers:
    def test_the_sync_scraper_pins_each_hop_and_refuses_a_hop_into_the_local_network(
        self, world: World
    ) -> None:
        world.dns.rebinds("rebind.test", PUBLIC_A)
        world.dns.rebinds("hop.test", "93.184.216.35")
        scraper = WebScraper()
        scraper._client = _RecordingClient(  # type: ignore[assignment]
            scraper,
            _Answer(302, "http://hop.test/landing"),
            _Answer(302, "http://127.0.0.1/private"),
        )

        with pytest.raises(UnsafeTargetError):
            scraper._request_with_safe_redirects("HTTP://Rebind.test./start")

        asked = scraper._client.asked  # type: ignore[attr-defined]
        assert [url for url, _ in asked] == ["http://rebind.test/start", "http://hop.test/landing"]
        assert asked[0][1] == [f"rebind.test:80:{PUBLIC_A}"]
        assert asked[1][1] == [f"rebind.test:80:{PUBLIC_A}", "hop.test:80:93.184.216.35"]
        assert world.dns.lookups == {"rebind.test": 1, "hop.test": 1}

    @pytest.mark.asyncio
    async def test_the_async_scraper_pins_each_hop_and_refuses_a_hop_into_the_local_network(
        self, world: World
    ) -> None:
        world.dns.answers("rebind.test", [PUBLIC_A])
        async with WebScraperAsync() as scraper:
            client = _RecordingClient(
                scraper, _Answer(301, "/next"), _Answer(302, "http://[::ffff:127.0.0.1]/")
            )

            async def get(url: str) -> _Answer:
                return client.get(url)

            scraper._client.get = get  # type: ignore[union-attr,method-assign]
            with pytest.raises(UnsafeTargetError):
                await scraper._request_with_safe_redirects("https://rebind.test/start")

        assert [url for url, _ in client.asked] == [
            "https://rebind.test/start",
            "https://rebind.test/next",
        ]
        assert client.asked[0][1] == [f"rebind.test:443:{PUBLIC_A}"]
        assert world.dns.lookups == {"rebind.test": 2}  # one for each request, each checked

    def test_a_scraper_that_is_told_it_may_reach_private_networks_still_pins(
        self, world: World
    ) -> None:
        world.dns.rebinds("lan.test", "192.168.1.5")
        scraper = WebScraper(allow_private_networks=True)
        scraper._client = _RecordingClient(scraper, _Answer(200))  # type: ignore[assignment]

        scraper._request_with_safe_redirects("http://lan.test/")

        assert scraper._client.asked == [  # type: ignore[attr-defined]
            ("http://lan.test/", ["lan.test:80:192.168.1.5"])
        ]

    def test_the_sessions_they_make_carry_the_pin_list_itself(self) -> None:
        # A session that copied the list would not see the pins made after it was built.
        scraper = WebScraper()
        with scraper:
            assert scraper._client is not None
            options = scraper._client.curl_options
            assert options[CurlOpt.RESOLVE] is scraper._pins.options[CurlOpt.RESOLVE]
            assert options[CurlOpt.PROXY] == ""
