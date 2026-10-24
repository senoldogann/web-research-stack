"""A crawl that has ended says why, and what it left.

A crawl of modelcontextprotocol.io/docs with ``max_pages: 5`` answered ``completed``, ``percent: 100`` and
``queue_size: 32``: done, and thirty-two links waiting. Now a crawl that has ended says ``stopped_by`` (``max_pages``,
``max_depth``, ``done`` or ``cancelled``; ``interrupted`` and ``failed`` for the two other ways a job ends),
``progress.not_visited`` counts the links it left, ``web_crawl_results`` lists the first of them
(``not_visited_urls``), and ``queue_size`` is 0 once it has ended: nothing is waiting in a crawl that is over.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from rest_support import HEADERS, rest_client
from test_web_crawl import MapFetcher, _page, _service, _wait_for_status

from web_scraper.mcp_server import McpServer, _tools
from web_scraper.web_tools.crawl import CrawlManager, CrawlOptions
from web_scraper.web_tools.crawl_store import CrawlStore
from web_scraper.web_tools.settings import WebToolsSettings

BASE = "https://example.com/"


def _site(links: dict[str, list[str]]) -> dict[str, Any]:
    """Pages by path, each with its own words and the links given."""
    return {
        BASE + path.lstrip("/"): _page(
            BASE + path.lstrip("/"), f"the words of {path or 'the root'} and nothing else", targets
        )
        for path, targets in links.items()
    }


def _crawler(pages: dict[str, Any], **settings: Any) -> Any:
    base = {"respect_robots": False, "crawl_concurrency": 1, "max_crawl_pages": 100}
    base.update(settings)
    return _service(MapFetcher(pages), **base)


async def _crawl(service: Any, **options: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    started = await service.start_crawl(BASE, options)
    snapshot = await _wait_for_status(
        service, started["job_id"], {"completed", "failed", "cancelled", "interrupted"}
    )
    return snapshot, await service.crawl_results(started["job_id"], limit=100)


class TestWhyACrawlEnded:
    @pytest.mark.asyncio
    async def test_a_crawl_stopped_by_its_page_limit_says_so_and_what_it_left(self) -> None:
        pages = _site(
            {"": ["/a", "/b", "/c", "/d", "/e"], "a": [], "b": [], "c": [], "d": [], "e": []}
        )
        service = _crawler(pages)

        snapshot, results = await _crawl(service, max_pages=2, max_depth=2)

        assert snapshot["status"] == "completed" and snapshot["stopped_by"] == "max_pages"
        progress = snapshot["progress"]
        assert progress["pages_attempted"] == 2 and progress["fetched"] == 2
        # Nothing is waiting in a crawl that is over; four links were left unvisited.
        assert progress["queue_size"] == 0 and progress["not_visited"] == 4
        assert results["stopped_by"] == "max_pages" and results["not_visited"] == 4
        assert results["not_visited_urls"] == [BASE + path for path in ("b", "c", "d", "e")]
        # (percent keeps its meaning: the share of the page limit that was read)
        assert progress["percent"] == 100

    @pytest.mark.asyncio
    async def test_a_crawl_stopped_by_its_depth_limit_says_so(self) -> None:
        service = _crawler(_site({"": ["/a"], "a": ["/b", "/c"], "b": ["/d"], "c": [], "d": []}))

        snapshot, results = await _crawl(service, max_pages=20, max_depth=1)

        assert snapshot["stopped_by"] == "max_depth"
        assert snapshot["progress"]["fetched"] == 2 and snapshot["progress"]["queue_size"] == 0
        assert snapshot["progress"]["not_visited"] == 2
        assert results["not_visited_urls"] == [BASE + "b", BASE + "c"]

    @pytest.mark.asyncio
    async def test_a_crawl_that_visited_everything_there_was_is_done(self) -> None:
        service = _crawler(_site({"": ["/a", "/b"], "a": ["/"], "b": []}))

        snapshot, results = await _crawl(service, max_pages=20, max_depth=3)

        assert snapshot["stopped_by"] == "done" and snapshot["progress"]["fetched"] == 3
        assert snapshot["progress"]["not_visited"] == 0 and results["not_visited_urls"] == []

    @pytest.mark.asyncio
    async def test_the_page_limit_is_what_stopped_a_crawl_that_also_met_its_depth_limit(
        self,
    ) -> None:
        pages = _site({"": ["/a", "/b"], "a": ["/deep"], "b": [], "deep": []})
        service = _crawler(pages)

        snapshot, results = await _crawl(service, max_pages=2, max_depth=1)

        assert snapshot["stopped_by"] == "max_pages"
        # /b was waiting; /deep was past the depth limit: both are left.
        assert snapshot["progress"]["not_visited"] == 2
        assert set(results["not_visited_urls"]) == {BASE + "b", BASE + "deep"}

    @pytest.mark.asyncio
    async def test_the_links_listed_are_the_first_fifty_and_the_count_is_all_of_them(self) -> None:
        targets = [f"/p{n}" for n in range(80)]
        service = _crawler(_site({"": targets}), max_crawl_pages=5)

        snapshot, results = await _crawl(service, max_pages=1, max_depth=1)

        assert snapshot["stopped_by"] == "max_pages" and snapshot["progress"]["not_visited"] == 80
        assert len(results["not_visited_urls"]) == 50
        assert results["not_visited_urls"][0] == BASE + "p0"

    @pytest.mark.asyncio
    async def test_a_running_crawl_has_not_stopped_and_its_queue_is_live(self) -> None:
        pages = _site({"": ["/a", "/b", "/c"], "a": [], "b": [], "c": []})
        service = WebToolsServiceWithDelay.make(pages, delay=0.05)

        started = await service.start_crawl(BASE, {"max_pages": 10, "max_depth": 1})
        for _ in range(100):
            await asyncio.sleep(0.02)
            running = await service.get_crawl(started["job_id"])
            if running["progress"]["queue_size"]:
                break

        assert running["status"] == "running" and running["stopped_by"] is None
        assert running["progress"]["queue_size"] > 0
        await _wait_for_status(service, started["job_id"], {"completed"})
        ended = await service.get_crawl(started["job_id"])
        assert ended["stopped_by"] == "done" and ended["progress"]["queue_size"] == 0


class WebToolsServiceWithDelay:
    @staticmethod
    def make(pages: dict[str, Any], delay: float) -> Any:
        return _service(
            MapFetcher(pages, delay=delay),
            respect_robots=False,
            crawl_concurrency=1,
            max_crawl_pages=100,
        )


class TestTheOtherWaysAJobEnds:
    @pytest.mark.asyncio
    async def test_a_cancelled_crawl_says_cancelled_and_has_no_queue(self) -> None:
        pages = _site({"": ["/a", "/b", "/c", "/d"], "a": [], "b": [], "c": [], "d": []})
        service = WebToolsServiceWithDelay.make(pages, delay=0.05)

        started = await service.start_crawl(BASE, {"max_pages": 10, "max_depth": 1})
        await asyncio.sleep(0.12)
        await service.cancel_crawl(started["job_id"])
        snapshot = await _wait_for_status(service, started["job_id"], {"cancelled"})

        assert snapshot["status"] == "cancelled" and snapshot["stopped_by"] == "cancelled"
        assert snapshot["progress"]["queue_size"] == 0

    @pytest.mark.asyncio
    async def test_a_crawl_cancelled_before_it_began_left_its_start_page(self) -> None:
        service = _crawler(_site({"": []}))

        started = await service.start_crawl(BASE, {})
        cancelled = await service.cancel_crawl(started["job_id"])

        assert cancelled["status"] == "cancelled" and cancelled["stopped_by"] == "cancelled"
        assert (
            cancelled["progress"]["not_visited"] == 1 and cancelled["progress"]["queue_size"] == 0
        )

    @pytest.mark.asyncio
    async def test_a_crawl_that_the_server_stopped_is_interrupted(self) -> None:
        pages = _site({"": ["/a", "/b", "/c"], "a": [], "b": [], "c": []})
        service = WebToolsServiceWithDelay.make(pages, delay=0.2)

        started = await service.start_crawl(BASE, {"max_pages": 10, "max_depth": 1})
        await asyncio.sleep(0.1)
        await service.close()
        snapshot = await service.get_crawl(started["job_id"])

        assert snapshot["status"] == "interrupted" and snapshot["stopped_by"] == "interrupted"
        assert snapshot["progress"]["queue_size"] == 0

    @pytest.mark.asyncio
    async def test_a_crawl_that_failed_says_failed(self) -> None:
        class Boom:
            async def allowed(self, url: str) -> bool:
                raise RuntimeError("robots fell over")

        async def loader(url: str) -> dict[str, Any]:
            return {"url": url, "markdown": "x", "links": []}

        settings = WebToolsSettings(respect_robots=True)
        manager = CrawlManager(loader, settings, robots=Boom())
        job = await manager.create(
            BASE + "a",
            CrawlOptions(max_pages=3, max_depth=1, include_subdomains=False, respect_robots=True),
        )

        # The start page is behind robots.txt too: asking raises, and the job fails and says so.
        for _ in range(100):
            snapshot = await manager.get(job["job_id"])
            if snapshot["status"] == "failed":
                break
            await asyncio.sleep(0.01)

        assert snapshot["status"] == "failed" and snapshot["stopped_by"] == "failed"
        assert snapshot["progress"]["queue_size"] == 0


class TestWhatIsKept:
    @pytest.mark.asyncio
    async def test_a_restarted_server_remembers_why_a_crawl_ended(self, tmp_path: Path) -> None:
        pages = _site({"": ["/a", "/b", "/c"], "a": [], "b": [], "c": []})
        first = _crawler(pages, data_dir=str(tmp_path))
        started = await first.start_crawl(BASE, {"max_pages": 1, "max_depth": 1})
        await _wait_for_status(first, started["job_id"], {"completed"})
        await first.close()

        second = _crawler(pages, data_dir=str(tmp_path))  # the server, started again
        snapshot = await second.get_crawl(started["job_id"])
        listing = await second.crawl_results(started["job_id"])
        await second.close()

        assert snapshot["stopped_by"] == "max_pages" and snapshot["progress"]["not_visited"] == 3
        assert snapshot["progress"]["queue_size"] == 0
        assert listing["not_visited_urls"] == [BASE + path for path in ("a", "b", "c")]

    @pytest.mark.asyncio
    async def test_a_job_an_older_version_kept_has_no_reason_and_is_read_all_the_same(
        self, tmp_path: Path
    ) -> None:
        store = CrawlStore(tmp_path / "jobs.sqlite3")
        store.save_job(
            {
                "job_id": "old",
                "url": BASE,
                "status": "completed",
                "options": {"max_pages": 5, "max_depth": 1},
                "progress": {"discovered": 3, "fetched": 3, "queue_size": 7},
                "errors": [],
                "duplicates": [],
                "created_at": None,
                "started_at": None,
                "finished_at": None,
                "created_ts": time.time(),
                "updated_ts": time.time(),
            }
        )

        manager = CrawlManager(lambda url: None, WebToolsSettings(), store=store)  # type: ignore[arg-type, return-value]
        snapshot = await manager.get("old")

        assert snapshot["status"] == "completed" and snapshot["stopped_by"] is None
        assert snapshot["progress"]["not_visited"] == 0


class TestWhatAClientSees:
    @pytest.mark.asyncio
    async def test_the_mcp_tools_answer_with_it_and_say_what_it_means(self) -> None:
        pages = _site({"": ["/a", "/b", "/c"], "a": [], "b": [], "c": []})
        service = _crawler(pages)
        server = McpServer(service)

        try:
            started = await server._call_tool(
                {
                    "name": "web_crawl_start",
                    "arguments": {"url": BASE, "options": {"max_pages": 2, "max_depth": 1}},
                }
            )
            job_id = json.loads(started["content"][0]["text"])["job_id"]
            await _wait_for_status(service, job_id, {"completed"})
            status = json.loads(
                (
                    await server._call_tool(
                        {"name": "web_crawl_status", "arguments": {"job_id": job_id}}
                    )
                )["content"][0]["text"]
            )
            results = json.loads(
                (
                    await server._call_tool(
                        {"name": "web_crawl_results", "arguments": {"job_id": job_id}}
                    )
                )["content"][0]["text"]
            )
        finally:
            await service.close()

        assert status["stopped_by"] == "max_pages" and status["progress"]["not_visited"] == 2
        assert results["stopped_by"] == "max_pages" and len(results["not_visited_urls"]) == 2
        text = next(tool["description"] for tool in _tools() if tool["name"] == "web_crawl_status")
        assert all(
            word in text
            for word in ("stopped_by", "max_pages", "max_depth", "done", "cancelled", "not_visited")
        )

    @pytest.mark.asyncio
    async def test_the_rest_routes_keep_the_fields_their_models_would_have_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _crawler(_site({"": ["/a", "/b"], "a": [], "b": []}))
        client = rest_client(monkeypatch, service)

        started = client.post(
            "/api/v1/web/crawl",
            headers=HEADERS,
            json={"url": BASE, "options": {"max_pages": 1, "max_depth": 1}},
        ).json()
        for _ in range(200):
            status = client.get(f"/api/v1/web/crawl/{started['job_id']}", headers=HEADERS).json()
            if status["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        results = client.get(
            f"/api/v1/web/crawl/{started['job_id']}/results", headers=HEADERS
        ).json()

        assert status["stopped_by"] == "max_pages" and status["progress"]["not_visited"] == 2
        assert results["stopped_by"] == "max_pages" and results["not_visited"] == 2
        assert results["not_visited_urls"] == [BASE + "a", BASE + "b"]
