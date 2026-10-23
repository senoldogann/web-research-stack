"""A cache hit reports the time it took, not the time of the call it replays."""

from __future__ import annotations

import asyncio

import pytest
from test_web_service import (
    ARTICLE_HTML,
    FakeFetcher,
    FakeSearch,
    _html_result,
    _service,
)

from web_scraper.web_tools.fetcher import FetchResult
from web_scraper.web_tools.search import SearchResult

SLOW_SECONDS = 0.2


class SlowFetcher(FakeFetcher):
    async def fetch(self, url: str) -> FetchResult:
        await asyncio.sleep(SLOW_SECONDS)
        return await super().fetch(url)


class SlowSearch(FakeSearch):
    async def search(
        self,
        query: str,
        limit: int,
        *,
        date_filter: str | None = None,
    ) -> list[SearchResult]:
        await asyncio.sleep(SLOW_SECONDS)
        return await super().search(query, limit, date_filter=date_filter)


@pytest.mark.asyncio
async def test_a_scrape_cache_hit_reports_its_own_time() -> None:
    url = "https://example.com/article"
    service = _service(SlowFetcher({url: _html_result(url, ARTICLE_HTML, network_ms=180.0)}))

    first = await service.scrape(url)
    second = await service.scrape(url)

    assert first["cache_hit"] is False
    assert first["timings"]["total_ms"] >= SLOW_SECONDS * 1000
    assert second["cache_hit"] is True
    assert second["timings"]["total_ms"] < 50
    assert second["timings"]["total_ms"] >= second["timings"]["cache_lookup_ms"]
    # No stage of the replayed call is passed off as this call's.
    assert "network_ms" not in second["timings"]
    assert second["cached_timings"] == first["timings"]
    assert second["cached_at"].endswith("Z")
    assert "cached_timings" not in first


@pytest.mark.asyncio
async def test_a_search_cache_hit_reports_its_own_time() -> None:
    search = SlowSearch(
        [SearchResult(title="Python asyncio", url="https://example.com/a", snippet="asyncio")]
    )
    service = _service(FakeFetcher({}), search=search)

    first = await service.search("python asyncio", depth="fast")
    second = await service.search("python asyncio", depth="fast")

    assert len(search.calls) == 1
    assert first["timings"]["total_ms"] >= SLOW_SECONDS * 1000
    assert second["cache_hit"] is True
    assert second["timings"]["total_ms"] < 50
    assert "search_ms" not in second["timings"]
    assert second["cached_timings"] == first["timings"]
    assert second["results"] == first["results"]
