"""Redis-backed cache and rate limiter — drop-in replacements for the in-memory variants.

If the Redis connection is unavailable the helpers raise ``RedisUnavailable``
so callers can gracefully fall back to the in-memory implementations.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class RedisUnavailable(Exception):
    """Raised when Redis cannot be reached."""


try:
    import redis.asyncio as aioredis  # type: ignore[import-untyped]

    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REDIS_AVAILABLE = False


class RedisCache:
    """Async TTL cache backed by Redis.

    Implements the same ``get`` / ``set`` / ``snapshot`` interface as
    ``InMemoryTTLCache`` so it can be swapped in transparently.
    """

    def __init__(self, ttl_seconds: int, namespace: str = "research") -> None:
        if not _REDIS_AVAILABLE:
            raise RedisUnavailable("redis package is not installed")
        self.ttl_seconds = max(ttl_seconds, 1)
        self._ns = namespace
        self._client: Optional[aioredis.Redis] = None  # type: ignore[name-defined]
        self._hits = 0
        self._misses = 0

    def _key(self, key: str) -> str:
        return f"{self._ns}:{key}"

    async def connect(self, url: str) -> None:
        """Open the Redis connection pool."""
        self._client = aioredis.from_url(  # type: ignore[attr-defined]
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        # Verify connectivity
        try:
            await self._client.ping()  # type: ignore[misc]
        except Exception as exc:
            self._client = None
            raise RedisUnavailable(f"Redis ping failed: {exc}") from exc

    async def close(self) -> None:
        """Close the connection pool gracefully."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, key: str) -> Optional[Any]:
        """Return cached value or ``None``."""
        if self._client is None:
            raise RedisUnavailable("Redis client not connected")
        try:
            raw = await self._client.get(self._key(key))
        except Exception as exc:
            raise RedisUnavailable(f"Redis GET failed: {exc}") from exc

        if raw is None:
            self._misses += 1
            return None
        try:
            self._hits += 1
            return json.loads(raw)
        except json.JSONDecodeError:
            self._misses += 1
            return None

    async def set(self, key: str, value: Any) -> None:
        """Store *value* under *key* with the configured TTL."""
        if self._client is None:
            raise RedisUnavailable("Redis client not connected")
        try:
            await self._client.setex(
                self._key(key),
                self.ttl_seconds,
                json.dumps(value, ensure_ascii=True),
            )
        except Exception as exc:
            raise RedisUnavailable(f"Redis SET failed: {exc}") from exc

    async def snapshot(self) -> Dict[str, Any]:
        """Return cache runtime statistics."""
        if self._client is None:
            return {"status": "disconnected"}
        try:
            info = await self._client.info("stats")
            dbsize = await self._client.dbsize()
            return {
                "status": "connected",
                "hits": self._hits,
                "misses": self._misses,
                "ttl_seconds": self.ttl_seconds,
                "db_keys": dbsize,
                "redis_keyspace_hits": info.get("keyspace_hits", 0),
                "redis_keyspace_misses": info.get("keyspace_misses", 0),
            }
        except Exception:
            return {"status": "error", "hits": self._hits, "misses": self._misses}


class RedisRateLimiter:
    """Sliding-window rate limiter backed by Redis (atomic Lua script).

    Implements the same ``check`` interface as ``SlidingWindowRateLimiter``.
    Falls back gracefully — callers should catch ``RedisUnavailable`` and
    allow the request through (fail-open) or use the in-memory limiter.
    """

    # Lua script: atomically remove stale entries, check quota, record hit
    _SCRIPT = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local cutoff = now - window * 1000

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = tonumber(redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')[2])
    return {0, math.ceil((oldest + window * 1000 - now) / 1000)}
end

redis.call('ZADD', key, now, now .. '-' .. math.random(1e9))
redis.call('PEXPIRE', key, window * 1000)
return {limit - count - 1, window}
"""

    def __init__(self, default_limit: int, window_seconds: int = 60, namespace: str = "rl") -> None:
        if not _REDIS_AVAILABLE:
            raise RedisUnavailable("redis package is not installed")
        self.default_limit = max(default_limit, 1)
        self.window_seconds = window_seconds
        self._ns = namespace
        self._client: Optional[aioredis.Redis] = None  # type: ignore[name-defined]
        self._lua_sha: Optional[str] = None

    def _key(self, key: str) -> str:
        return f"{self._ns}:{key}"

    async def connect(self, url: str) -> None:
        """Open the Redis connection pool and register the Lua script."""
        self._client = aioredis.from_url(  # type: ignore[attr-defined]
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            await self._client.ping()  # type: ignore[misc]
            self._lua_sha = await self._client.script_load(self._SCRIPT)  # type: ignore[misc]
        except Exception as exc:
            self._client = None
            raise RedisUnavailable(f"Redis ping/script_load failed: {exc}") from exc

    async def close(self) -> None:
        """Close the connection pool gracefully."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def check(self, key: str, limit: Optional[int] = None) -> Tuple[int, int]:
        """Record the request and return (remaining, window_seconds).

        Raises ``RateLimitExceeded`` when quota is exhausted.
        Raises ``RedisUnavailable`` when Redis is unreachable.
        """
        from web_scraper.api_runtime import RateLimitExceeded

        if self._client is None or self._lua_sha is None:
            raise RedisUnavailable("Redis client not connected")

        applied_limit = max(limit or self.default_limit, 1)
        now_ms = int(time.time() * 1000)

        try:
            result = await self._client.evalsha(  # type: ignore[misc]
                self._lua_sha,
                1,
                self._key(key),
                str(now_ms),
                str(self.window_seconds),
                str(applied_limit),
            )
        except Exception as exc:
            raise RedisUnavailable(f"Redis EVALSHA failed: {exc}") from exc

        remaining, retry_after_or_window = int(result[0]), int(result[1])
        if remaining < 0:
            raise RateLimitExceeded(retry_after=max(1, retry_after_or_window), limit=applied_limit)

        return remaining, self.window_seconds
