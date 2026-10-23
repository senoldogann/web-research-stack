"""SQLite storage for crawl jobs, so a restart of the server does not lose them.

Standard library only. One file in the data folder holds every retained job: a row per job
(its state, options, progress counters, errors and duplicates, small and rewritten as the job
goes) and a row per result page (appended once, as the page arrives). The file is private to
the user, and a store that cannot be opened or written costs the persistence, never the crawl.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_JSON_FIELDS = ("options", "progress", "errors", "duplicates")
_COLUMNS = (
    "job_id",
    "url",
    "status",
    "options",
    "progress",
    "errors",
    "duplicates",
    "created_at",
    "started_at",
    "finished_at",
    "created_ts",
    "updated_ts",
)


class CrawlStore:
    """Crawl jobs on disk: ``save_job``, ``append_results``, ``delete_job`` and ``load``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.enabled = True
        self._lock = threading.Lock()
        try:
            self._initialize()
        except (OSError, sqlite3.Error) as exc:
            self._disable("it could not be opened", exc)

    def save_job(self, record: dict[str, Any]) -> None:
        """Write a job's row: its state and counters as they are now."""
        values = [
            json.dumps(record[name], ensure_ascii=False) if name in _JSON_FIELDS else record[name]
            for name in _COLUMNS
        ]
        assignments = ", ".join(f"{name} = excluded.{name}" for name in _COLUMNS[1:])
        self._run(
            lambda connection: connection.execute(
                f"INSERT INTO crawl_jobs ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)}) "
                f"ON CONFLICT(job_id) DO UPDATE SET {assignments}",
                values,
            ),
            "a job could not be saved",
        )

    def append_results(self, job_id: str, start: int, results: list[dict[str, Any]]) -> None:
        """Write the result pages of a job from position *start* on."""
        if not results:
            return
        rows = [
            (job_id, start + offset, json.dumps(item, ensure_ascii=False))
            for offset, item in enumerate(results)
        ]
        self._run(
            lambda connection: connection.executemany(
                "INSERT OR REPLACE INTO crawl_results (job_id, position, payload) VALUES (?, ?, ?)",
                rows,
            ),
            "results could not be saved",
        )

    def delete_job(self, job_id: str) -> None:
        """Forget a job and its results (what pruning does in memory, on disk)."""

        def delete(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM crawl_results WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM crawl_jobs WHERE job_id = ?", (job_id,))

        self._run(delete, "a job could not be deleted")

    def load(self) -> list[dict[str, Any]]:
        """Every kept job, the oldest first, each with its ``results`` in order."""
        if not self.enabled:
            return []
        jobs: list[dict[str, Any]] = []
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    f"SELECT {', '.join(_COLUMNS)} FROM crawl_jobs ORDER BY created_ts, job_id"
                ).fetchall()
                for row in rows:
                    record = dict(zip(_COLUMNS, row))
                    for name in _JSON_FIELDS:
                        record[name] = json.loads(record[name])
                    record["results"] = [
                        json.loads(payload)
                        for (payload,) in connection.execute(
                            "SELECT payload FROM crawl_results WHERE job_id = ? ORDER BY position",
                            (record["job_id"],),
                        )
                    ]
                    jobs.append(record)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._disable("the jobs could not be read", exc)
            return []
        return jobs

    # ------------------------------------------------------------------ internals

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _run(self, action: Any, what: str) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as connection:
                action(connection)
        except (OSError, sqlite3.Error) as exc:
            self._disable(what, exc)

    def _initialize(self) -> None:
        folder = self.path.parent
        made = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
        if made:
            os.chmod(
                folder, 0o700
            )  # the folder this store made; one that was there is left as it is
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawl_jobs (
                    job_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    options TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    errors TEXT NOT NULL,
                    duplicates TEXT NOT NULL,
                    created_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS crawl_results (
                    job_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (job_id, position)
                );
                """
            )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        os.chmod(self.path, 0o600)  # crawled pages are the user's own business

    def _disable(self, what: str, exc: BaseException) -> None:
        if self.enabled:
            logger.warning(
                "Crawl jobs are no longer kept on disk (%s: %s, %s); crawls go on in memory",
                what,
                type(exc).__name__,
                self.path,
            )
        self.enabled = False
