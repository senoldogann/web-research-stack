"""web_scrape: links on request. ``links`` turns them off, ``max_links`` bounds them (0 to 200)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_web_service import FakeFetcher, _html_result, _service

from web_scraper.api_models import WebScrapeRequest
from web_scraper.mcp_server import _scrape, _tools
from web_scraper.web_tools.settings import WebToolsSettings

URL = "https://example.com/list"
COUNT = 120
PAGE = (
    "<html><head><title>List</title></head><body><main><h1>A list</h1><p>"
    + ("Some words of the page. " * 80)
    + "</p>"
    + "".join(f'<a href="/p/{index}">Link {index}</a>' for index in range(COUNT))
    + "</main></body></html>"
)
PAGE_WITH_MENU = PAGE.replace(
    "<main>", '<nav><a href="/menu-a">Menu A</a><a href="/menu-b">Menu B</a></nav><main>'
)


def _fetcher(html: str = PAGE) -> FakeFetcher:
    return FakeFetcher({URL: _html_result(URL, html)})


class TestRequest:
    def test_the_defaults_are_links_on_and_the_servers_cap(self) -> None:
        request = WebScrapeRequest(url=URL)

        assert request.links is True
        assert request.max_links is None

    def test_the_schema_states_both(self) -> None:
        properties = WebScrapeRequest.model_json_schema()["properties"]

        assert properties["links"]["default"] is True
        limits = [
            option for option in properties["max_links"]["anyOf"] if option.get("type") == "integer"
        ][0]
        assert (limits["minimum"], limits["maximum"]) == (0, 200)
        assert "50" in properties["max_links"]["description"]
        assert "Markdown only" in properties["max_chars"]["description"]

    @pytest.mark.parametrize("value", [-1, 201])
    def test_a_cap_outside_zero_to_two_hundred_is_refused(self, value: int) -> None:
        with pytest.raises(ValidationError):
            WebScrapeRequest(url=URL, max_links=value)

    @pytest.mark.parametrize("value", [0, 1, 200])
    def test_the_edges_are_accepted(self, value: int) -> None:
        assert WebScrapeRequest(url=URL, max_links=value).max_links == value

    def test_the_tool_description_says_which_limit_bounds_what(self) -> None:
        scrape = next(tool for tool in _tools() if tool["name"] == "web_scrape")

        assert "max_links" in scrape["description"]
        assert "links: false" in scrape["description"]


class TestAnswer:
    @pytest.mark.asyncio
    async def test_by_default_fifty_links_and_the_rest_said_to_exist(self) -> None:
        result = await _service(_fetcher()).scrape(URL)

        assert len(result["links"]) == 50
        assert result["links_truncated"] is True

    @pytest.mark.asyncio
    async def test_max_links_sets_how_many(self) -> None:
        service = _service(_fetcher())

        ten = await service.scrape(URL, max_links=10)
        all_of_them = await service.scrape(URL, max_links=200)

        assert len(ten["links"]) == 10 and ten["links_truncated"] is True
        assert len(all_of_them["links"]) == COUNT
        assert all_of_them["links_truncated"] is False

    @pytest.mark.asyncio
    async def test_links_false_returns_none_and_is_not_truncated(self) -> None:
        result = await _service(_fetcher()).scrape(URL, links=False)

        assert result["links"] == []
        assert result["links_truncated"] is False
        assert "Some words of the page." in result["markdown"]

    @pytest.mark.asyncio
    async def test_zero_with_links_on_says_more_exist(self) -> None:
        result = await _service(_fetcher()).scrape(URL, max_links=0)

        assert result["links"] == []
        assert result["links_truncated"] is True

    @pytest.mark.asyncio
    async def test_a_page_with_no_links_is_not_truncated(self) -> None:
        html = "<html><body><main><p>" + "Words. " * 100 + "</p></main></body></html>"

        result = await _service(_fetcher(html)).scrape(URL, max_links=5)

        assert result["links"] == [] and result["links_truncated"] is False

    @pytest.mark.asyncio
    async def test_menu_links_still_count_as_links_left_out(self) -> None:
        result = await _service(_fetcher(PAGE_WITH_MENU)).scrape(URL, max_links=200)

        assert len(result["links"]) == COUNT  # the menu's two are page chrome
        assert result["links_truncated"] is True

    @pytest.mark.asyncio
    async def test_max_chars_bounds_the_markdown_only(self) -> None:
        result = await _service(_fetcher()).scrape(URL, max_chars=500)

        assert len(result["markdown"]) <= 500
        assert len(result["links"]) == 50

    @pytest.mark.asyncio
    async def test_the_servers_setting_is_the_default_cap_and_a_request_is_held_to_its_ceiling(
        self,
    ) -> None:
        settings = WebToolsSettings(
            search_provider="fake",
            scrape_max_links=20,
            max_links=100,  # type: ignore[arg-type]
        )
        service = _service(_fetcher(), settings=settings)

        default = await service.scrape(URL)
        asked_for_more = await service.scrape(URL, max_links=200)

        assert len(default["links"]) == 20
        assert len(asked_for_more["links"]) == 100

    @pytest.mark.asyncio
    async def test_each_choice_has_its_own_cache_entry(self) -> None:
        service = _service(_fetcher())

        no_links = await service.scrape(URL, links=False)
        with_links = await service.scrape(URL)
        again = await service.scrape(URL, links=False)

        assert no_links["links"] == [] and len(with_links["links"]) == 50
        assert again["cache_hit"] is True and again["links"] == []

    @pytest.mark.asyncio
    async def test_the_mcp_tool_takes_both(self) -> None:
        service = _service(_fetcher())

        none = await _scrape(service, {"url": URL, "links": False})
        some = await _scrape(service, {"url": URL, "max_links": 7})

        assert none["links"] == []
        assert len(some["links"]) == 7
        with pytest.raises(ValidationError):
            await _scrape(service, {"url": URL, "max_links": 201})
