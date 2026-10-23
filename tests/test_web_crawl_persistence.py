"""Crawl jobs outlive a restart: kept in SQLite in a data folder, interrupted when it cut them."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
from test_web_crawl import MapFetcher, _page, _service, _wait_for_status

from web_scraper.web_tools.crawl import CrawlStore
from web_scraper.web_tools.errors import WebToolError
from web_scraper.web_tools.fetcher import FetchResult
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

BASE = "https://example.com/"
ROBOTS = "User-agent: *\nAllow: /\n"


def _site(count: int = 4, *, same: bool = False) -> dict[str, FetchResult | Exception]:
    """A home that links to count pages."""
    links = [f"/p{n}" for n in range(count)]
    pages: dict[str, FetchResult | Exception] = {
        BASE + "robots.txt": FetchResult(
            BASE + "robots.txt", BASE + "robots.txt", 200, "text/plain", ROBOTS, {}, 1, 1, 1
        ),
        BASE: _page(BASE, "home", links),
    }
    for index, link in enumerate(links):
        body = "same body" if same else f"page number {index}"
        pages[BASE + link.lstrip("/")] = _page(BASE + link.lstrip("/"), body, [])
    return pages


def _make(
    folder: Path, fetcher: MapFetcher | None = None, **settings: Any
) -> tuple[WebToolsService, MapFetcher]:
    fetcher = fetcher or MapFetcher(_site())
    return _service(fetcher, data_dir=str(folder), **settings), fetcher


def _database(folder: Path) -> Path:
    return folder / "crawl-jobs.sqlite3"


def _count(folder: Path, table: str = "crawl_jobs") -> int:
    with sqlite3.connect(_database(folder)) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


async def _finish(service: WebToolsService, options: dict[str, Any] | None = None) -> str:
    started = await service.start_crawl(BASE, options or {"max_pages": 10, "max_depth": 1})
    await _wait_for_status(service, started["job_id"], {"completed", "failed"})
    return started["job_id"]


class TestRestart:
    @pytest.mark.asyncio
    async def test_a_finished_job_is_the_same_after_a_restart(self, tmp_path: Path) -> None:
        first, _ = _make(tmp_path)
        job_id = await _finish(first)
        status = await first.get_crawl(job_id)
        results = await first.crawl_results(job_id, limit=50)
        await first.close()

        second, _ = _make(tmp_path)

        assert await second.get_crawl(job_id) == status
        assert await second.crawl_results(job_id, limit=50) == results
        assert (await second.crawl_results(job_id, offset=2, limit=2))["results"] == results[
            "results"
        ][2:4]
        assert [item["job_id"] for item in await second.list_crawls()] == [job_id]
        assert status["status"] == "completed" and results["total"] == 5

    @pytest.mark.asyncio
    async def test_errors_and_duplicates_survive_too(self, tmp_path: Path) -> None:
        pages = _site(3, same=True)
        pages[BASE + "p2"] = WebToolError("http_error", "not found", url=BASE + "p2")
        first, _ = _make(tmp_path, MapFetcher(pages))
        job_id = await _finish(first)
        before = await first.crawl_results(job_id, limit=50)
        await first.close()

        second, _ = _make(tmp_path, MapFetcher(pages))
        after = await second.crawl_results(job_id, limit=50)

        assert before["errors"] and before["duplicates"]
        assert after["errors"] == before["errors"] and after["duplicates"] == before["duplicates"]
        assert (await second.get_crawl(job_id))["progress"]["duplicates"] == len(
            before["duplicates"]
        )

    @pytest.mark.asyncio
    async def test_a_job_that_was_running_is_interrupted_and_its_pages_stay_readable(
        self, tmp_path: Path
    ) -> None:
        first, _ = _make(tmp_path, MapFetcher(_site(8), delay=0.05), crawl_concurrency=1)
        started = await first.start_crawl(BASE, {"max_pages": 9, "max_depth": 1})
        for _ in range(100):  # the first pages are in; the crawl is far from done
            if (await first.get_crawl(started["job_id"]))["progress"]["fetched"] >= 3:
                break
            await asyncio.sleep(0.02)
        assert (await first.get_crawl(started["job_id"]))["status"] == "running"

        # The server dies here: a new one opens the same folder, the old never closed.
        second, _ = _make(tmp_path)
        status = await second.get_crawl(started["job_id"])
        results = await second.crawl_results(started["job_id"], limit=50)
        await first.close()

        assert status["status"] == "interrupted"
        assert status["finished_at"] is not None
        assert 1 <= results["total"] < 9 and results["total"] == status["progress"]["fetched"]
        assert results["status"] == "interrupted"
        assert all(item["markdown"] for item in results["results"])

    @pytest.mark.asyncio
    async def test_a_job_still_queued_is_interrupted_too(self, tmp_path: Path) -> None:
        first, _ = _make(tmp_path)
        started = await first.start_crawl(BASE, {"max_pages": 5})
        assert started["status"] == "queued"

        second, _ = _make(tmp_path)

        assert (await second.get_crawl(started["job_id"]))["status"] == "interrupted"
        await first.close()

    @pytest.mark.asyncio
    async def test_a_graceful_stop_leaves_the_job_interrupted_not_cancelled(
        self, tmp_path: Path
    ) -> None:
        first, _ = _make(tmp_path, MapFetcher(_site(8), delay=0.05), crawl_concurrency=1)
        started = await first.start_crawl(BASE, {"max_pages": 9, "max_depth": 1})
        await asyncio.sleep(0.2)

        await first.close()  # the daemon restarts the server: stdin closes
        second, _ = _make(tmp_path)

        status = await second.get_crawl(started["job_id"])
        assert status["status"] == "interrupted"
        assert (await second.crawl_results(started["job_id"], limit=50))["total"] >= 1

    @pytest.mark.asyncio
    async def test_a_cancelled_job_stays_cancelled(self, tmp_path: Path) -> None:
        first, _ = _make(tmp_path, MapFetcher(_site(8), delay=0.05), crawl_concurrency=1)
        started = await first.start_crawl(BASE, {"max_pages": 9, "max_depth": 1})
        await asyncio.sleep(0.1)
        await first.cancel_crawl(started["job_id"])
        await _wait_for_status(first, started["job_id"], {"cancelled"})
        await first.close()

        second, _ = _make(tmp_path)

        assert (await second.get_crawl(started["job_id"]))["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_an_interrupted_job_is_not_resumed_and_cannot_be_cancelled(
        self, tmp_path: Path
    ) -> None:
        first, _ = _make(tmp_path)
        started = await first.start_crawl(BASE, {"max_pages": 5})
        second, fetcher = _make(tmp_path)
        await first.close()

        again = await second.cancel_crawl(started["job_id"])
        await asyncio.sleep(0.05)

        assert again["status"] == "interrupted"
        assert fetcher.calls == []  # nothing runs it again
        assert (await second.get_crawl(started["job_id"]))["status"] == "interrupted"

    @pytest.mark.asyncio
    async def test_the_interruption_is_kept_for_the_next_restart(self, tmp_path: Path) -> None:
        first, _ = _make(tmp_path)
        started = await first.start_crawl(BASE, {"max_pages": 5})
        second, _ = _make(tmp_path)
        await second.get_crawl(started["job_id"])
        await first.close()
        await second.close()

        third, _ = _make(tmp_path)

        assert (await third.get_crawl(started["job_id"]))["status"] == "interrupted"

    @pytest.mark.asyncio
    async def test_a_new_job_after_a_restart_has_its_own_id_and_is_kept(
        self, tmp_path: Path
    ) -> None:
        first, _ = _make(tmp_path)
        old = await _finish(first)
        await first.close()
        second, _ = _make(tmp_path)

        new = await _finish(second)

        assert new != old
        assert {item["job_id"] for item in await second.list_crawls()} == {old, new}


class TestBounds:
    @pytest.mark.asyncio
    async def test_max_crawl_jobs_holds_on_disk_too(self, tmp_path: Path) -> None:
        service, _ = _make(tmp_path, max_crawl_jobs=3)
        ids = [await _finish(service) for _ in range(5)]
        await service.close()

        in_memory = {item["job_id"] for item in await service.list_crawls()}
        with sqlite3.connect(_database(tmp_path)) as connection:
            on_disk = {row[0] for row in connection.execute("SELECT job_id FROM crawl_jobs")}
            with_results = {
                row[0] for row in connection.execute("SELECT DISTINCT job_id FROM crawl_results")
            }

        assert 1 <= len(in_memory) <= 3 and ids[-1] in in_memory
        assert ids[0] not in in_memory  # pruned
        assert on_disk == in_memory  # and gone from disk, rows and results
        assert with_results == in_memory

    @pytest.mark.asyncio
    async def test_a_restart_keeps_to_the_bound_and_to_the_ttl(self, tmp_path: Path) -> None:
        service, _ = _make(tmp_path)
        old = await _finish(service)
        recent = await _finish(service)
        await service.close()
        with sqlite3.connect(_database(tmp_path)) as connection:  # an hour and more ago
            connection.execute(
                "UPDATE crawl_jobs SET updated_ts = updated_ts - 7200 WHERE job_id = ?", (old,)
            )

        reopened, _ = _make(tmp_path, crawl_job_ttl_seconds=3600)

        assert [item["job_id"] for item in await reopened.list_crawls()] == [recent]
        assert _count(tmp_path) == 1 and _count(tmp_path, "crawl_results") == 5
        with pytest.raises(WebToolError) as exc:
            await reopened.get_crawl(old)
        assert exc.value.code == "job_not_found"

    @pytest.mark.asyncio
    async def test_a_restart_over_a_full_folder_prunes_to_the_cap(self, tmp_path: Path) -> None:
        service, _ = _make(tmp_path, max_crawl_jobs=5)
        for _ in range(4):
            await _finish(service)
        await service.close()

        reopened, _ = _make(tmp_path, max_crawl_jobs=2)
        await reopened.list_crawls()

        assert _count(tmp_path) <= 2

    @pytest.mark.asyncio
    async def test_a_job_that_cannot_be_dropped_still_holds_a_place(self, tmp_path: Path) -> None:
        service, _ = _make(tmp_path, MapFetcher(_site(6), delay=0.2), max_crawl_jobs=1)
        await service.start_crawl(BASE, {"max_pages": 7})

        with pytest.raises(WebToolError) as exc:
            await service.start_crawl(BASE, {"max_pages": 7})
        await service.close()

        assert exc.value.code == "crawl_capacity"


class TestFolder:
    def test_the_default_is_the_xdg_data_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("WEB_TOOLS_DATA_DIR", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert WebToolsSettings.from_env().data_dir == str(tmp_path / "xdg" / "web-research")

        monkeypatch.delenv("XDG_DATA_HOME")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert WebToolsSettings.from_env().data_dir == str(
            tmp_path / ".local" / "share" / "web-research"
        )

    def test_the_folder_is_a_setting_and_an_empty_one_turns_it_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("WEB_TOOLS_DATA_DIR", str(tmp_path / "mine"))
        assert WebToolsSettings.from_env().data_dir == str(tmp_path / "mine")
        for off in ("", "none", "off"):
            monkeypatch.setenv("WEB_TOOLS_DATA_DIR", off)
            assert WebToolsSettings.from_env().data_dir == ""

    def test_a_settings_made_by_hand_keeps_nothing_on_disk(self) -> None:
        assert WebToolsSettings().data_dir == ""

    @pytest.mark.asyncio
    async def test_without_a_folder_nothing_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(MapFetcher(_site()))
        monkeypatch.chdir(tmp_path)

        await _finish(service)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_the_folder_is_made_and_private(self, tmp_path: Path) -> None:
        folder = tmp_path / "deep" / "web-research"
        service, _ = _make(folder)

        await _finish(service)

        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE(_database(folder).stat().st_mode) == 0o600

    @pytest.mark.asyncio
    async def test_a_folder_that_cannot_be_used_costs_the_persistence_not_the_crawl(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocker = tmp_path / "a-file"
        blocker.write_text("not a folder", encoding="utf-8")
        service, _ = _make(blocker / "web-research")

        job_id = await _finish(service)

        assert (await service.get_crawl(job_id))["status"] == "completed"
        assert any("crawl" in record.message.lower() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_damaged_database_costs_the_persistence_not_the_server(
        self, tmp_path: Path
    ) -> None:
        _database(tmp_path).write_bytes(b"this is not a sqlite database" * 100)
        service, _ = _make(tmp_path)

        job_id = await _finish(service)

        assert (await service.get_crawl(job_id))["status"] == "completed"

    def test_the_store_keeps_a_job_whole(self, tmp_path: Path) -> None:
        store = CrawlStore(_database(tmp_path))
        record = {
            "job_id": "abc",
            "url": BASE,
            "status": "completed",
            "options": {"max_pages": 3},
            "progress": {"fetched": 1},
            "errors": [{"url": BASE, "code": "x", "message": "y"}],
            "duplicates": [{"url": BASE, "duplicate_of": BASE}],
            "created_at": "2026-10-23T10:00:00+00:00",
            "started_at": None,
            "finished_at": None,
            "created_ts": 1.0,
            "updated_ts": 2.0,
        }

        store.save_job(record)
        store.append_results("abc", 0, [{"url": BASE, "markdown": "# é"}])
        loaded = CrawlStore(_database(tmp_path)).load()

        assert loaded == [{**record, "results": [{"url": BASE, "markdown": "# é"}]}]
        assert json.dumps(loaded)
