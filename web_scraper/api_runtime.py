"""Runtime helpers for the FastAPI application."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def stable_hash(value: str) -> str:
    """Create a stable hash without leaking the underlying input."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RateLimitExceeded(Exception):
    """Raised when a client exceeds the configured rate limit."""

    def __init__(self, retry_after: int, limit: int) -> None:
        super().__init__("Rate limit exceeded")
        self.retry_after = retry_after
        self.limit = limit


class CircuitBreakerOpen(Exception):
    """Raised when the upstream dependency is temporarily disabled."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Circuit breaker is open")
        self.retry_after = retry_after


class SlidingWindowRateLimiter:
    """Simple in-memory sliding window limiter."""

    def __init__(self, default_limit: int, window_seconds: int = 60) -> None:
        self.default_limit = max(default_limit, 1)
        self.window_seconds = window_seconds
        self._buckets: Dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: Optional[int] = None) -> Tuple[int, int]:
        """Record the request and return remaining quota and window seconds."""
        applied_limit = max(limit or self.default_limit, 1)
        now = time.monotonic()

        async with self._lock:
            bucket = self._buckets[key]
            while bucket and now - bucket[0] >= self.window_seconds:
                bucket.popleft()

            if len(bucket) >= applied_limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])))
                raise RateLimitExceeded(retry_after=retry_after, limit=applied_limit)

            bucket.append(now)
            remaining = max(applied_limit - len(bucket), 0)

        return remaining, self.window_seconds


class InMemoryTTLCache:
    """Small bounded TTL cache for repeated research requests."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self.ttl_seconds = max(ttl_seconds, 1)
        self.max_entries = max(max_entries, 1)
        self._entries: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> Optional[Any]:
        """Return a cached value if it exists and has not expired."""
        now = time.monotonic()
        async with self._lock:
            self._prune(now)
            entry = self._entries.pop(key, None)
            if entry is None:
                self.misses += 1
                return None

            expires_at, value = entry
            if expires_at <= now:
                self.misses += 1
                return None

            self._entries[key] = (expires_at, value)
            self.hits += 1
            return value

    async def set(self, key: str, value: Any) -> None:
        """Store a cache value with an expiry timestamp."""
        expires_at = time.monotonic() + self.ttl_seconds
        async with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (expires_at, value)
            self._prune(time.monotonic())
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    async def snapshot(self) -> Dict[str, int]:
        """Expose cache metrics (acquires lock to get a consistent read)."""
        async with self._lock:
            now = time.monotonic()
            self._prune(now)
            return {
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
            }

    def _prune(self, now: float) -> None:
        stale_keys = [key for key, (expires_at, _) in self._entries.items() if expires_at <= now]
        for key in stale_keys:
            self._entries.pop(key, None)


class CircuitBreaker:
    """Minimal circuit breaker around upstream model access."""

    def __init__(self, failure_threshold: int, recovery_seconds: int) -> None:
        self.failure_threshold = max(failure_threshold, 1)
        self.recovery_seconds = max(recovery_seconds, 1)
        self.failure_count = 0
        self.last_error: Optional[str] = None
        self.opened_at: Optional[float] = None
        self._lock = threading.Lock()

    def ensure_available(self) -> None:
        """Raise if the breaker is open and not yet ready to recover."""
        with self._lock:
            if self.opened_at is None:
                return

            elapsed = time.monotonic() - self.opened_at
            if elapsed < self.recovery_seconds:
                raise CircuitBreakerOpen(retry_after=max(1, int(self.recovery_seconds - elapsed)))

            self.opened_at = None
            self.failure_count = 0
            self.last_error = None

    def record_success(self) -> None:
        """Reset breaker state after a successful upstream call."""
        with self._lock:
            self.failure_count = 0
            self.last_error = None
            self.opened_at = None

    def record_failure(self, error: str) -> None:
        """Mark a failed upstream call."""
        with self._lock:
            self.failure_count += 1
            self.last_error = error
            if self.failure_count >= self.failure_threshold:
                self.opened_at = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        """Return the breaker state for health checks."""
        with self._lock:
            retry_after = None
            if self.opened_at is not None:
                retry_after = max(
                    0, int(self.recovery_seconds - (time.monotonic() - self.opened_at))
                )

            return {
                "open": self.opened_at is not None,
                "failure_count": self.failure_count,
                "failure_threshold": self.failure_threshold,
                "retry_after_seconds": retry_after,
                "last_error": self.last_error,
            }


class ConcurrencyGate:
    """Global semaphore for expensive endpoints."""

    def __init__(self, max_concurrent: int) -> None:
        self.max_concurrent = max(max_concurrent, 1)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._active = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self):
        """Acquire the semaphore and expose active request count."""
        await self._semaphore.acquire()
        async with self._lock:
            self._active += 1

        try:
            yield self._active
        finally:
            async with self._lock:
                self._active -= 1
            self._semaphore.release()

    async def snapshot(self) -> Dict[str, int]:
        """Return active request data for health and metrics."""
        async with self._lock:
            active = self._active
        return {"active": active, "max_concurrent": self.max_concurrent}


@dataclass(frozen=True)
class MetricPoint:
    """Immutable metric key."""

    name: str
    labels: Tuple[Tuple[str, str], ...]


class MetricsRegistry:
    """Tiny in-memory metrics store with Prometheus rendering."""

    def __init__(self) -> None:
        self._counters: Dict[MetricPoint, float] = defaultdict(float)
        self._gauges: Dict[MetricPoint, float] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        """Increment a counter metric."""
        point = MetricPoint(name=name, labels=self._normalize_labels(labels))
        with self._lock:
            self._counters[point] += amount

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        """Set a gauge metric."""
        point = MetricPoint(name=name, labels=self._normalize_labels(labels))
        with self._lock:
            self._gauges[point] = value

    def render_prometheus(self) -> str:
        """Render counters and gauges in Prometheus text format."""
        lines: List[str] = []
        with self._lock:
            counters = list(self._counters.items())
            gauges = list(self._gauges.items())

        counter_names = sorted({point.name for point, _ in counters})
        gauge_names = sorted({point.name for point, _ in gauges})

        for name in counter_names:
            lines.append(f"# TYPE {name} counter")
            for point, value in counters:
                if point.name != name:
                    continue
                lines.append(f"{name}{self._format_labels(point.labels)} {value}")

        for name in gauge_names:
            lines.append(f"# TYPE {name} gauge")
            for point, value in gauges:
                if point.name != name:
                    continue
                lines.append(f"{name}{self._format_labels(point.labels)} {value}")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _normalize_labels(labels: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted((str(key), str(value)) for key, value in labels.items()))

    @staticmethod
    def _format_labels(labels: Tuple[Tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        rendered = ",".join(f'{key}="{value}"' for key, value in labels)
        return "{" + rendered + "}"


class ResearchHistoryStore:
    """SQLite-backed history store for auditability."""

    def __init__(self, db_path: Optional[str]) -> None:
        self.db_path = db_path
        self.enabled = bool(db_path)
        self._lock = threading.Lock()

        if self.enabled:
            try:
                self._initialize()
            except (OSError, sqlite3.Error):
                logger.exception(
                    "Disabling research history persistence because SQLite database initialization failed",
                    extra={"db_path": self.db_path},
                )
                self.enabled = False

    def record(
        self,
        *,
        route: str,
        query_hash: str,
        query_length: int,
        model: Optional[str],
        success: bool,
        cached: bool,
        response_ms: float,
        trace_id: str,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a research call summary without storing the raw prompt."""
        if not self.enabled or self.db_path is None:
            return

        payload = json.dumps(metadata or {}, ensure_ascii=True)
        with self._lock, sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO research_history (
                    route,
                    query_hash,
                    query_length,
                    model,
                    success,
                    cached,
                    response_ms,
                    trace_id,
                    error,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route,
                    query_hash,
                    query_length,
                    model,
                    int(success),
                    int(cached),
                    response_ms,
                    trace_id,
                    error,
                    payload,
                ),
            )

    def snapshot(self) -> dict[str, Any]:
        """Expose persistence health and row counts."""
        if not self.enabled or self.db_path is None:
            return {"enabled": False, "records": 0, "path": None}

        path = Path(self.db_path)
        with self._lock, sqlite3.connect(self.db_path) as connection:
            records = connection.execute("SELECT COUNT(*) FROM research_history").fetchone()[0]

        return {"enabled": True, "records": records, "path": str(path.resolve())}

    def _initialize(self) -> None:
        if self.db_path is None:
            return

        path = Path(self.db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock, sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS research_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    route TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    query_length INTEGER NOT NULL,
                    model TEXT,
                    success INTEGER NOT NULL,
                    cached INTEGER NOT NULL,
                    response_ms REAL NOT NULL,
                    trace_id TEXT NOT NULL,
                    error TEXT,
                    metadata TEXT
                )
                """
            )
