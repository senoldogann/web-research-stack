"""A name that rebinds reaches no service of the owner's by any way the server reads a page of its own accord.

Version 1.3.0 added two ways: the dates read from the heads of search results, and the recovery of a walled page (its
AMP or print form, its feed, another outlet's copy, the Internet Archive's). Each read the address a name pointed to
at the moment of the read, not the one the check had judged. A plain news search sent a blind GET to a local server,
and a walled scrape answered with a local service's page, a token in it, as "another outlet's copy".

Each test gives a name that answers a public address to its first lookup and ``127.0.0.1`` to every later one, runs
the real service, fetcher and resolver code against it (``tests/rebinding_support.py``: nothing leaves the machine),
and asks what reached the owner's service on the loopback: nothing may, and no connection may be opened to an address
that is not public.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from curl_cffi.const import CurlOpt
from rebinding_support import (
    OWNER_TOKEN,
    PUBLIC_A,
    PUBLIC_B,
    PUBLIC_C,
    PUBLIC_D,
    Response,
    World,
    html,
)
from test_web_scrape_recover import (
    RECORDED_WALL,
    SENTENCE,
    SLUG,
    TITLE,
    _article,
    _result,
    _rss,
    _rss_item,
)
from test_web_service import FakeSearch

from web_scraper.web_tools.archive_fetcher import (
    ImpersonatingTransport,
    archive_validator,
)
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchError, SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

DATED_HEAD = (
    "<html><head><title>Reactor restart date announced</title>"
    '<meta property="article:published_time" content="2026-10-20T10:00:00Z"></head>'
    "<body><p>The operator said the reactor will restart.</p></body></html>"
)


def _service(search: Optional[FakeSearch] = None, **settings: Any) -> WebToolsService:
    base: dict[str, Any] = {
        "search_provider": "fake",
        "search_angle_delay_ms": 0,
        "search_jitter_min_ms": 0,
        "search_jitter_max_ms": 0,
        "search_fallback_providers": [],
        "search_resolve_dates": "off",
        "browser_enabled": False,
    }
    base.update(settings)
    return WebToolsService(
        settings=WebToolsSettings(**base),
        fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
        browser=None,
        search_adapters={search.name: search} if search is not None else {},
    )


def _nothing_reached_the_owner(world: World) -> None:
    assert world.local.seen == []
    assert world.internet.private_dials == []
    assert world.internet.named_dials == []  # the dialler was handed addresses that were checked
    assert OWNER_TOKEN not in str(world.internet.requests)


class TestDatesReadFromTheHeadsOfResults:
    @pytest.mark.asyncio
    async def test_a_news_search_reads_the_checked_address_and_sends_nothing_to_a_local_server(
        self, world: World
    ) -> None:
        port = world.local.port
        world.rebinding_host("news.test", PUBLIC_A, lambda request: html(DATED_HEAD))
        search = FakeSearch(
            [
                _result(
                    f"http://news.test:{port}/admin/restart?x=1", "Reactor restart date announced"
                )
            ]
        )
        service = _service(search, search_resolve_dates="on")

        try:
            answer = await service.search("latest reactor restart news", depth="fast", compact=True)
        finally:
            await service.close()

        _nothing_reached_the_owner(world)
        assert world.dns.lookups == {"news.test": 1}
        assert [request.target for request in world.internet.requests] == ["/admin/restart?x=1"]
        assert world.internet.dials == [(PUBLIC_A, port)]
        # The head that was read is the public address's: it gave the result its date.
        assert answer["results"][0]["date"] == "2026-10-20"

    @pytest.mark.asyncio
    async def test_the_same_search_of_a_name_that_is_the_loopback_already_reads_nothing(
        self, world: World
    ) -> None:
        port = world.local.port
        world.dns.answers("news.test", ["127.0.0.1"])
        search = FakeSearch([_result(f"http://news.test:{port}/admin", "Reactor restart date")])
        service = _service(search, search_resolve_dates="on")

        try:
            answer = await service.search("latest reactor restart news", depth="fast", compact=True)
        finally:
            await service.close()

        _nothing_reached_the_owner(world)
        assert answer["results"][0].get("date") in (None, "")
        assert world.internet.dials == []


class TestWalledPageRecovery:
    """A walled page at a public address; the way it is read another way points at a name that rebinds."""

    @staticmethod
    def _wall(world: World, *, amp: str = "", feed: str = "") -> str:
        links = ""
        if amp:
            links += f'<link rel="amphtml" href="{amp}">'
        if feed:
            links += f'<link rel="alternate" type="application/rss+xml" href="{feed}">'
        body = RECORDED_WALL.replace("</head>", links + "</head>")
        world.dns.answers("walled.test", [PUBLIC_A])
        world.internet.serve(PUBLIC_A, lambda request: html(body, status=401))
        return f"http://walled.test:{world.local.port}/business/{SLUG}/"

    @pytest.mark.asyncio
    async def test_the_amp_form_the_wall_names_is_read_at_the_checked_address(
        self, world: World
    ) -> None:
        port = world.local.port
        world.rebinding_host("amp.walled.test", PUBLIC_B, lambda request: html(_article()))
        url = self._wall(world, amp=f"http://amp.walled.test:{port}/amp-story")
        service = _service()

        try:
            page = await service.scrape(url)
        finally:
            await service.close()

        _nothing_reached_the_owner(world)
        assert page["recovered_from"]["kind"] == "amp"
        assert "OpenAI will sell its top models" in page["markdown"]
        assert world.dns.lookups["amp.walled.test"] == 1
        assert (PUBLIC_B, port) in world.internet.dials

    @pytest.mark.asyncio
    async def test_the_feed_the_wall_names_is_read_at_the_checked_address(
        self, world: World
    ) -> None:
        port = world.local.port
        url = self._wall(world, feed=f"http://feeds.walled.test:{port}/rss")
        feed = _rss(_rss_item(url.rstrip("/")))
        world.rebinding_host(
            "feeds.walled.test",
            PUBLIC_C,
            lambda request: Response(200, {"Content-Type": "application/rss+xml"}, feed),
        )
        service = _service()

        try:
            page = await service.scrape(url)
        finally:
            await service.close()

        _nothing_reached_the_owner(world)
        assert page["recovered_from"]["kind"] == "feed"
        assert world.dns.lookups["feeds.walled.test"] == 1
        assert (PUBLIC_C, port) in world.internet.dials

    @pytest.mark.asyncio
    async def test_another_outlets_copy_is_read_at_the_checked_address_and_no_local_page_is_passed_off(
        self, world: World
    ) -> None:
        port = world.local.port
        url = self._wall(world)
        world.rebinding_host("other.test", PUBLIC_D, lambda request: html(_article()))
        search = FakeSearch([_result(f"http://other.test:{port}/admin", TITLE, SENTENCE)])
        service = _service(search)

        try:
            page = await service.scrape(url)
        finally:
            await service.close()

        _nothing_reached_the_owner(world)
        assert page["recovered_from"]["kind"] == "other_source"
        assert "OWNER-ONLY SERVICE" not in page["markdown"]
        assert "OpenAI will sell its top models" in page["markdown"]
        assert world.dns.lookups["other.test"] == 1
        assert (PUBLIC_D, port) in world.internet.dials

    @pytest.mark.asyncio
    async def test_a_walled_host_that_rebinds_after_its_wall_has_its_other_forms_refused(
        self, world: World
    ) -> None:
        """The wall comes from the public address; the AMP, print and feed forms of the same host, each a
        connection of its own, meet the loopback and are not read."""
        port = world.local.port
        world.rebinding_host(
            "rebind-wall.test", PUBLIC_A, lambda request: html(RECORDED_WALL, status=401)
        )
        url = f"http://rebind-wall.test:{port}/business/{SLUG}/"
        service = _service()

        try:
            with pytest.raises(WebToolError) as exc:
                await service.scrape(url)
        finally:
            await service.close()

        _nothing_reached_the_owner(world)
        assert exc.value.code == "blocked"
        assert world.internet.dials == [
            (PUBLIC_A, port)
        ]  # the wall itself, and no other connection
        assert (
            world.dns.lookups["rebind-wall.test"] > 1
        )  # the other forms were looked up, and refused

    @pytest.mark.asyncio
    async def test_a_name_that_is_the_loopback_already_ends_in_the_walls_error_not_a_local_page(
        self, world: World
    ) -> None:
        """The search finds a page on a name that is the loopback: nothing is read, and the wall's error stands."""
        port = world.local.port
        url = self._wall(world)
        world.dns.answers("other.test", ["127.0.0.1"])
        search = FakeSearch([_result(f"http://other.test:{port}/admin", TITLE, SENTENCE)])
        service = _service(search)

        try:
            with pytest.raises(WebToolError) as exc:
                await service.scrape(url)
        finally:
            await service.close()

        _nothing_reached_the_owner(world)
        assert "OWNER-ONLY SERVICE" not in str(exc.value.to_dict())


class _CurlAnswer:
    """What curl_cffi's response is to the transport: a status, headers, a body, and where it came from."""

    status_code = 200

    class headers:  # noqa: N801 - the shape curl_cffi gives
        @staticmethod
        def multi_items() -> list[tuple[str, str]]:
            return [("content-type", "text/html")]

    def __init__(self, primary_ip: str = "") -> None:
        self.primary_ip = primary_ip

    async def aiter_content(self, chunk_size: Any = None) -> Any:
        yield b"<html><body><main><p>The archived page.</p></main></body></html>"

    async def aclose(self) -> None:
        return None


class _FakeCurlSession:
    """The part of curl_cffi's AsyncSession the archive transport uses; it keeps what libcurl would be told."""

    def __init__(self, primary_ip: str = "", **options: Any) -> None:
        self.options = options
        self.primary_ip = primary_ip
        self.requests: list[tuple[str, list[str]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> _CurlAnswer:
        self.requests.append((url, list(self.options["curl_options"][CurlOpt.RESOLVE])))
        return _CurlAnswer(self.primary_ip)

    async def close(self) -> None:
        return None


REPLAY = "https://web.archive.org/web/2025id_/https://a.example/"


class TestTheInternetArchive:
    """The archive's own host is fixed in code; its session is held to the address that was checked all the same."""

    @staticmethod
    def _fetcher(sessions: list[_FakeCurlSession], primary_ip: str = "") -> SafeFetcher:
        def factory(**options: Any) -> _FakeCurlSession:
            sessions.append(_FakeCurlSession(primary_ip, **options))
            return sessions[-1]

        return SafeFetcher(
            transport=ImpersonatingTransport(timeout=5, session_factory=factory),
            validator=archive_validator,
            retry_attempts=0,
        )

    @pytest.mark.asyncio
    async def test_the_session_is_told_the_checked_address_and_the_name_is_looked_up_once(
        self, world: World
    ) -> None:
        world.dns.rebinds("web.archive.org", PUBLIC_A)
        sessions: list[_FakeCurlSession] = []

        async with self._fetcher(sessions) as fetcher:
            result = await fetcher.fetch(REPLAY)

        assert "The archived page." in result.html
        (session,) = sessions
        assert session.requests == [(REPLAY, [f"web.archive.org:443:{PUBLIC_A}"])]
        assert world.dns.lookups == {"web.archive.org": 1}
        # A proxy of the environment would resolve the name itself.
        assert session.options["curl_options"][CurlOpt.PROXY] == ""

    @pytest.mark.asyncio
    async def test_the_next_request_is_checked_again_and_a_name_that_rebinds_is_refused(
        self, world: World
    ) -> None:
        world.dns.rebinds("web.archive.org", PUBLIC_A)
        sessions: list[_FakeCurlSession] = []

        async with self._fetcher(sessions) as fetcher:
            await fetcher.fetch(REPLAY)
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(REPLAY.replace("2025", "2024"))

        assert exc.value.code == "unsafe_target"
        (session,) = sessions
        assert len(session.requests) == 1  # nothing was asked of libcurl for the second
        assert world.dns.lookups == {"web.archive.org": 2}

    @pytest.mark.asyncio
    async def test_an_answer_that_came_from_a_private_address_is_not_returned(
        self, world: World
    ) -> None:
        # The pin is what keeps the request away; this is the line behind it.
        world.dns.answers("web.archive.org", [PUBLIC_A])
        sessions: list[_FakeCurlSession] = []

        async with self._fetcher(sessions, primary_ip="192.168.1.9") as fetcher:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch(REPLAY)

        assert exc.value.code == "unsafe_target"
