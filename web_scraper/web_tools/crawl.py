"""Background crawl jobs with bounded BFS, robots, dedupe, cancellation and a restart that keeps them."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import urlsplit

from web_scraper.web_tools.crawl_store import CrawlStore
from web_scraper.web_tools.errors import WebToolError, unusable_address
from web_scraper.web_tools.settings import WebToolsSettings
from web_scraper.web_tools.urls import (
    UnsafeUrlError,
    canonicalize_url,
    content_hash,
    matches_url_filters,
    upgrade_to_https,
    url_in_scope,
)

PageLoader = Callable[[str], Awaitable[dict[str, Any]]]
# The URLs a site's sitemaps list (in scope of the crawl's options), and the errors met reading them.
SeedLoader = Callable[["str", "CrawlOptions"], Awaitable[tuple[list[str], list[dict[str, Any]]]]]
# A job is over in one of these: ``interrupted`` is what a restart (or a stop) of the server
# made of a job that was queued or running; it is not resumed.
_TERMINAL = {"completed", "cancelled", "failed", "interrupted"}
_COUNTERS = (
    "discovered",
    "seeded",
    "pages_attempted",
    "fetched",
    "failed",
    "skipped",
    "duplicates",
    "current_depth",
    "queue_size",
)


@dataclass
class CrawlOptions:
    max_pages: int
    max_depth: int
    include_subdomains: bool = False
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    respect_robots: bool = True
    # Seed the frontier from the site's sitemaps: pages no link reaches.
    use_sitemap: bool = False

    @classmethod
    def from_dict(
        cls,
        data: Optional[Mapping[str, Any] | Any],
        settings: WebToolsSettings,
    ) -> CrawlOptions:
        if data is None:
            values: Mapping[str, Any] = {}
        elif isinstance(data, Mapping):
            values = data
        elif hasattr(data, "model_dump"):
            values = data.model_dump()
        else:
            raise WebToolError("invalid_request", "Crawl options must be an object")

        max_pages = _clamp(
            _int(values.get("max_pages"), settings.default_crawl_pages),
            1,
            settings.max_crawl_pages,
        )
        max_depth = _clamp(
            _int(values.get("max_depth"), settings.default_crawl_depth),
            0,
            settings.max_crawl_depth,
        )
        respect_robots_value = values.get("respect_robots")
        return cls(
            max_pages=max_pages,
            max_depth=max_depth,
            include_subdomains=bool(values.get("include_subdomains", False)),
            include_patterns=_string_list(values.get("include_patterns")),
            exclude_patterns=_string_list(values.get("exclude_patterns")),
            respect_robots=(
                settings.respect_robots
                if respect_robots_value is None
                else bool(respect_robots_value)
            ),
            use_sitemap=bool(values.get("use_sitemap") or False),
        )

    @classmethod
    def from_stored(cls, data: Mapping[str, Any], settings: WebToolsSettings) -> CrawlOptions:
        """Options as ``to_dict`` wrote them, kept as they were whatever the settings are now."""
        return cls(
            max_pages=max(_int(data.get("max_pages"), settings.default_crawl_pages), 1),
            max_depth=max(_int(data.get("max_depth"), settings.default_crawl_depth), 0),
            include_subdomains=bool(data.get("include_subdomains", False)),
            include_patterns=_string_list(data.get("include_patterns")),
            exclude_patterns=_string_list(data.get("exclude_patterns")),
            respect_robots=bool(data.get("respect_robots", True)),
            use_sitemap=bool(data.get("use_sitemap", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "include_subdomains": self.include_subdomains,
            "include_patterns": list(self.include_patterns),
            "exclude_patterns": list(self.exclude_patterns),
            "respect_robots": self.respect_robots,
            "use_sitemap": self.use_sitemap,
        }


@dataclass
class CrawlJob:
    job_id: str
    url: str
    options: CrawlOptions
    status: str = "queued"
    created_at: str = field(default_factory=lambda: _iso(time.time()))
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)
    discovered: int = 1
    seeded: int = 0
    pages_attempted: int = 0
    fetched: int = 0
    failed: int = 0
    skipped: int = 0
    duplicates: int = 0
    current_depth: int = 0
    queue_size: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    duplicate_entries: list[dict[str, str]] = field(default_factory=list)
    # https URLs this job made from http links, and hosts that refused https.
    upgraded: set[str] = field(default_factory=set)
    http_hosts: set[str] = field(default_factory=set)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    run_task: Optional[asyncio.Task[Any]] = None
    # How many results the store holds, and whether the server is stopping (a job it ends
    # is interrupted, not cancelled).
    persisted: int = 0
    stopping: bool = False

    def record(self) -> dict[str, Any]:
        """The job as the store keeps it, results apart."""
        return {
            "job_id": self.job_id,
            "url": self.url,
            "status": self.status,
            "options": self.options.to_dict(),
            "progress": {name: getattr(self, name) for name in _COUNTERS},
            "errors": list(self.errors),
            "duplicates": list(self.duplicate_entries),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any], settings: WebToolsSettings) -> CrawlJob:
        counters = {
            name: _int((record.get("progress") or {}).get(name), 0)
            for name in _COUNTERS
            if name != "discovered"
        }
        counters["discovered"] = _int((record.get("progress") or {}).get("discovered"), 1)
        results = list(record.get("results") or [])
        return cls(
            job_id=str(record["job_id"]),
            url=str(record["url"]),
            options=CrawlOptions.from_stored(record.get("options") or {}, settings),
            status=str(record["status"]),
            created_at=record.get("created_at") or _iso(float(record["created_ts"])),
            started_at=record.get("started_at"),
            finished_at=record.get("finished_at"),
            created_ts=float(record["created_ts"]),
            updated_ts=float(record["updated_ts"]),
            results=results,
            errors=list(record.get("errors") or []),
            duplicate_entries=list(record.get("duplicates") or []),
            persisted=len(results),
            **counters,
        )

    def snapshot(self) -> dict[str, Any]:
        terminal = self.status in _TERMINAL
        percent = (
            100
            if terminal and self.status == "completed"
            else min(
                99,
                int(self.pages_attempted / max(self.options.max_pages, 1) * 100),
            )
        )
        return {
            "job_id": self.job_id,
            "url": self.url,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "options": self.options.to_dict(),
            "progress": {
                "discovered": self.discovered,
                "seeded": self.seeded,
                "pages_attempted": self.pages_attempted,
                "fetched": self.fetched,
                "failed": self.failed,
                "skipped": self.skipped,
                "duplicates": self.duplicates,
                "queue_size": self.queue_size,
                "current_depth": self.current_depth,
                "percent": percent,
                "total_results": len(self.results),
            },
            "errors": list(self.errors),
            "duplicates": list(self.duplicate_entries),
        }


class CrawlManager:
    """Owns crawl jobs and their asyncio workers for one service instance."""

    def __init__(
        self,
        loader: PageLoader,
        settings: WebToolsSettings,
        robots: Any = None,
        seeder: Optional[SeedLoader] = None,
        store: Optional[CrawlStore] = None,
    ) -> None:
        self._loader = loader
        self._settings = settings
        self._robots = robots
        self._seeder = seeder
        self._store = store
        self._jobs: dict[str, CrawlJob] = {}
        if store is not None:
            self._restore()

    async def create(
        self,
        url: str,
        options: CrawlOptions,
        *,
        upgraded: bool = False,
    ) -> dict[str, Any]:
        """Queue a crawl of ``url``; ``upgraded`` marks a seed rewritten from http."""
        self._prune()
        if len(self._jobs) >= self._settings.max_crawl_jobs:
            raise WebToolError(
                "crawl_capacity",
                "Too many crawl jobs are retained; wait for one to expire",
            )
        job = CrawlJob(job_id=uuid.uuid4().hex, url=url, options=options)
        if upgraded:
            job.upgraded.add(url)
        self._jobs[job.job_id] = job
        self._persist(job)
        job.run_task = asyncio.create_task(self._run(job))
        return job.snapshot()

    async def get(self, job_id: str) -> dict[str, Any]:
        self._prune()
        return self._job(job_id).snapshot()

    async def results(
        self,
        job_id: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        self._prune()
        job = self._job(job_id)
        offset = max(offset, 0)
        limit = max(min(limit, self._settings.max_crawl_pages), 1)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "total": len(job.results),
            "offset": offset,
            "limit": limit,
            "results": job.results[offset : offset + limit],
            "errors": list(job.errors),
            "duplicates": list(job.duplicate_entries),
        }

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self._prune()
        job = self._job(job_id)
        if job.status in _TERMINAL:
            return job.snapshot()

        job.cancel_event.set()
        for task in list(job.tasks):
            task.cancel()
        if job.run_task is not None and not job.run_task.done():
            job.run_task.cancel()
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = _iso(time.time())
            job.updated_ts = time.time()
        elif job.status != "cancelled":
            job.status = "cancelling"
        self._persist(job)
        return job.snapshot()

    async def list_jobs(self) -> list[dict[str, Any]]:
        self._prune()
        return [job.snapshot() for job in self._jobs.values()]

    async def close(self) -> None:
        """Stop the jobs still going: each is interrupted, kept as far as it got, not resumed."""
        tasks: list[asyncio.Task[Any]] = []
        for job in self._jobs.values():
            if job.status not in _TERMINAL:
                job.stopping = True
            job.cancel_event.set()
            for task in list(job.tasks):
                task.cancel()
                tasks.append(task)
            if job.run_task is not None and not job.run_task.done():
                job.run_task.cancel()
                tasks.append(job.run_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            if job.stopping and job.status not in _TERMINAL:
                self._interrupt(job)  # a task cancelled before it began never ended its job

    def _interrupt(self, job: CrawlJob) -> None:
        job.status = "interrupted"
        job.finished_at = _iso(time.time())
        job.updated_ts = time.time()
        self._persist(job)

    def _restore(self) -> None:
        """Read the jobs the store kept; those that were going when the server ended are over.

        A job that was queued or running is ``interrupted`` (its pages stay readable; it is
        not resumed), one that was being cancelled is ``cancelled``. Pruning then holds the
        bounds on what was left on disk.
        """
        assert self._store is not None
        for record in self._store.load():
            try:
                job = CrawlJob.from_record(record, self._settings)
            except (KeyError, TypeError, ValueError):
                continue  # a row this version cannot read is left alone, and pruned in time
            self._jobs[job.job_id] = job
            if job.status in {"queued", "running"}:
                self._interrupt(job)
            elif job.status == "cancelling":
                job.status = "cancelled"
                job.updated_ts = time.time()
                self._persist(job)
        self._prune()

    def _persist(self, job: CrawlJob) -> None:
        """Write the job's state, and the results the store does not hold yet."""
        if self._store is None:
            return
        self._store.save_job(job.record())
        fresh = job.results[job.persisted :]
        if fresh:
            self._store.append_results(job.job_id, job.persisted, fresh)
            job.persisted = len(job.results)

    async def _run(self, job: CrawlJob) -> None:
        if job.cancel_event.is_set():
            job.status = "interrupted" if job.stopping else "cancelled"
            self._persist(job)
            return

        job.status = "running"
        job.started_at = _iso(time.time())
        job.updated_ts = time.time()
        self._persist(job)
        queue: deque[tuple[str, int]] = deque([(job.url, 0)])
        # One entry per host and path: http and https are the same page.
        seen_urls = {_identity(job.url)}
        seen_hashes: dict[str, str] = {}

        try:
            queue.extend(await self._seeds(job, seen_urls))
            job.queue_size = len(queue)
            while queue and job.pages_attempted < job.options.max_pages:
                if job.cancel_event.is_set():
                    break
                batch: list[tuple[str, int]] = []
                while queue and len(batch) < self._settings.crawl_concurrency:
                    if job.pages_attempted + len(batch) >= job.options.max_pages:
                        break
                    candidate, depth = queue.popleft()
                    if depth > job.options.max_depth:
                        continue
                    if depth > 0 and not self._in_scope(candidate, job.url, job.options):
                        job.skipped += 1
                        continue
                    if (
                        job.options.respect_robots
                        and self._robots is not None
                        and not await self._robots.allowed(candidate)
                    ):
                        job.skipped += 1
                        continue
                    if job.cancel_event.is_set():
                        break
                    batch.append((candidate, depth))

                if not batch or job.cancel_event.is_set():
                    break

                job.pages_attempted += len(batch)
                job.current_depth = max(depth for _, depth in batch)
                tasks = [
                    asyncio.create_task(self._process(job, candidate, depth, seen_hashes))
                    for candidate, depth in batch
                ]
                job.tasks.update(tasks)
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                job.tasks.difference_update(tasks)

                for outcome in outcomes:
                    if isinstance(outcome, asyncio.CancelledError):
                        continue
                    if isinstance(outcome, BaseException):
                        job.failed += 1
                        self._record_error(job, job.url, "internal_error", type(outcome).__name__)
                        continue
                    if not outcome:
                        continue
                    for link, depth in outcome:
                        if depth > job.options.max_depth:
                            continue
                        key = _identity(link)
                        if key in seen_urls:
                            continue
                        if not self._in_scope(link, job.url, job.options):
                            continue
                        seen_urls.add(key)
                        queue.append((self._target(job, link), depth))
                        job.discovered += 1

                job.queue_size = len(queue)
                job.updated_ts = time.time()
                self._persist(job)

            if job.cancel_event.is_set():
                job.status = "interrupted" if job.stopping else "cancelled"
            else:
                job.status = "completed"
        except asyncio.CancelledError:
            job.status = "interrupted" if job.stopping else "cancelled"
        except Exception as exc:  # noqa: BLE001 - job must not crash the process
            job.status = "failed"
            self._record_error(job, job.url, "internal_error", type(exc).__name__)
        finally:
            job.queue_size = len(queue)
            job.finished_at = _iso(time.time())
            job.updated_ts = time.time()
            self._persist(job)

    async def _seeds(self, job: CrawlJob, seen_urls: set[str]) -> list[tuple[str, int]]:
        """The sitemaps' pages as the first of the frontier, when the job asked for them.

        They are the start page's equals (depth 0, so their links are followed as ever),
        in the sitemap's order, kept in scope, through the filters and behind robots.txt,
        and they count against ``max_pages`` like any page: no more are queued than the
        crawl could read. A sitemap that cannot be read is an error of the job, not its end.
        """
        if not job.options.use_sitemap or self._seeder is None:
            return []
        try:
            urls, errors = await self._seeder(job.url, job.options)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the links still lead the crawl
            self._record_error(job, job.url, "sitemap_failed", type(exc).__name__)
            return []
        for error in errors:
            if str(error.get("url") or "").endswith("/robots.txt"):
                continue  # a site without robots.txt is a site without Sitemap lines
            self._record_error(
                job,
                str(error.get("url") or job.url),
                str(error.get("code") or "sitemap_error"),
                str(error.get("message") or ""),
            )
        seeds: list[tuple[str, int]] = []
        room = max(job.options.max_pages - 1, 0)
        for url in urls:
            if len(seeds) >= room:
                break
            key = _identity(url)
            if key in seen_urls or not self._in_scope(url, job.url, job.options):
                continue
            if (
                job.options.respect_robots
                and self._robots is not None
                and not await self._robots.allowed(url)
            ):
                job.skipped += 1
                continue
            seen_urls.add(key)
            seeds.append((self._target(job, url), 0))
        job.seeded = len(seeds)
        job.discovered += len(seeds)
        return seeds

    async def _process(
        self,
        job: CrawlJob,
        url: str,
        depth: int,
        seen_hashes: dict[str, str],
    ) -> Optional[list[tuple[str, int]]]:
        if job.cancel_event.is_set():
            return None
        try:
            payload = await self._load(job, url)
        except asyncio.CancelledError:
            raise
        except WebToolError as exc:
            job.failed += 1
            self._record_error(job, url, exc.code, str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - one page must not fail the crawl
            job.failed += 1
            self._record_error(job, url, "internal_error", type(exc).__name__)
            return None

        if job.cancel_event.is_set():
            return None

        digest = str(payload.get("content_hash") or content_hash(str(payload.get("markdown", ""))))
        if digest in seen_hashes:
            job.duplicates += 1
            if len(job.duplicate_entries) < self._settings.max_crawl_errors:
                job.duplicate_entries.append({"url": url, "duplicate_of": seen_hashes[digest]})
            return None

        seen_hashes[digest] = url
        job.fetched += 1
        job.results.append(
            {
                "url": payload.get("url", url),
                "title": payload.get("title", ""),
                "description": payload.get("description", ""),
                "markdown": payload.get("markdown", ""),
                "status_code": payload.get("status_code"),
                "content_hash": digest,
                "fetched_with": payload.get("fetched_with", "http"),
                "depth": depth,
            }
        )
        job.updated_ts = time.time()
        if depth >= job.options.max_depth:
            return []
        return [(link, depth + 1) for link in payload.get("links", [])]

    async def _load(self, job: CrawlJob, url: str) -> dict[str, Any]:
        try:
            return await self._loader(url)
        except WebToolError as exc:
            if exc.code != "network_error" or url not in job.upgraded:
                raise
        # The host does not answer https: crawl it over http, as its links said.
        job.http_hosts.add((urlsplit(url).hostname or "").lower())
        return await self._loader("http://" + url.split("://", 1)[1])

    def _target(self, job: CrawlJob, url: str) -> str:
        """The URL to fetch for a link: https unless its host refused https."""
        target = upgrade_to_https(url)
        if target == url or (urlsplit(url).hostname or "").lower() in job.http_hosts:
            return url
        job.upgraded.add(target)
        return target

    def _in_scope(self, url: str, base: str, options: CrawlOptions) -> bool:
        if not url_in_scope(url, base, options.include_subdomains):
            return False
        return matches_url_filters(url, options.include_patterns, options.exclude_patterns)

    def _record_error(self, job: CrawlJob, url: str, code: str, message: str) -> None:
        if len(job.errors) >= self._settings.max_crawl_errors:
            return
        job.errors.append({"url": url, "code": code, "message": message})

    def _job(self, job_id: str) -> CrawlJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise WebToolError("job_not_found", "Crawl job not found", detail=job_id)
        return job

    def _prune(self) -> None:
        now = time.time()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in _TERMINAL
            and now - job.updated_ts > self._settings.crawl_job_ttl_seconds
        ]
        for job_id in expired:
            self._forget(job_id)

        if len(self._jobs) < self._settings.max_crawl_jobs:
            return
        terminal = sorted(
            (job for job in self._jobs.values() if job.status in _TERMINAL),
            key=lambda job: job.updated_ts,
        )
        while terminal and len(self._jobs) >= self._settings.max_crawl_jobs:
            job = terminal.pop(0)
            self._forget(job.job_id)

    def _forget(self, job_id: str) -> None:
        """Drop a job from memory and from the store: the bounds hold on disk too."""
        self._jobs.pop(job_id, None)
        if self._store is not None:
            self._store.delete_job(job_id)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


def canonical_target(url: str) -> str:
    """Canonicalise a crawl target on https and classify failures as user errors."""
    try:
        return upgrade_to_https(canonicalize_url(url))
    except UnsafeUrlError as exc:
        raise unusable_address(url, exc) from exc


def _identity(url: str) -> str:
    # Canonical URLs differ only by scheme when they name the same page.
    return url.split("://", 1)[-1]
