"""Tests for redis_cache module (unit tests using mocks — no live Redis required)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web_scraper.redis_cache import RedisCache, RedisRateLimiter, RedisUnavailable


# ---------------------------------------------------------------------------
# RedisCache
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_redis_client():
    """Async mock that mimics redis.asyncio.Redis."""
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    client.get = AsyncMock(return_value=None)
    client.setex = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    client.info = AsyncMock(return_value={"keyspace_hits": 5, "keyspace_misses": 2})
    client.dbsize = AsyncMock(return_value=10)
    return client


@pytest.mark.asyncio
async def test_redis_cache_connect_success(mock_redis_client):
    cache = RedisCache(ttl_seconds=60)
    with patch("web_scraper.redis_cache.aioredis") as mock_aioredis:
        mock_aioredis.from_url.return_value = mock_redis_client
        await cache.connect("redis://localhost:6379/0")
    assert cache._client is not None


@pytest.mark.asyncio
async def test_redis_cache_connect_failure():
    cache = RedisCache(ttl_seconds=60)
    with patch("web_scraper.redis_cache.aioredis") as mock_aioredis:
        client = AsyncMock()
        client.ping = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        mock_aioredis.from_url.return_value = client
        with pytest.raises(RedisUnavailable, match="Redis ping failed"):
            await cache.connect("redis://localhost:6379/0")


@pytest.mark.asyncio
async def test_redis_cache_get_miss(mock_redis_client):
    cache = RedisCache(ttl_seconds=60)
    cache._client = mock_redis_client
    mock_redis_client.get.return_value = None

    result = await cache.get("missing_key")

    assert result is None
    assert cache._misses == 1
    assert cache._hits == 0


@pytest.mark.asyncio
async def test_redis_cache_get_hit(mock_redis_client):
    cache = RedisCache(ttl_seconds=60)
    cache._client = mock_redis_client
    payload = {"answer": "42"}
    mock_redis_client.get.return_value = json.dumps(payload)

    result = await cache.get("my_key")

    assert result == payload
    assert cache._hits == 1
    assert cache._misses == 0


@pytest.mark.asyncio
async def test_redis_cache_set(mock_redis_client):
    cache = RedisCache(ttl_seconds=120)
    cache._client = mock_redis_client

    await cache.set("my_key", {"data": "value"})

    mock_redis_client.setex.assert_awaited_once()
    call_args = mock_redis_client.setex.call_args
    # First arg is the namespaced key, second is ttl, third is json string
    assert call_args.args[1] == 120
    assert json.loads(call_args.args[2]) == {"data": "value"}


@pytest.mark.asyncio
async def test_redis_cache_get_raises_unavailable_when_not_connected():
    cache = RedisCache(ttl_seconds=60)
    # _client is None (not connected)
    with pytest.raises(RedisUnavailable, match="not connected"):
        await cache.get("key")


@pytest.mark.asyncio
async def test_redis_cache_set_raises_unavailable_when_not_connected():
    cache = RedisCache(ttl_seconds=60)
    with pytest.raises(RedisUnavailable, match="not connected"):
        await cache.set("key", {"data": "x"})


@pytest.mark.asyncio
async def test_redis_cache_snapshot_connected(mock_redis_client):
    cache = RedisCache(ttl_seconds=60)
    cache._client = mock_redis_client
    cache._hits = 3
    cache._misses = 1

    snap = await cache.snapshot()

    assert snap["status"] == "connected"
    assert snap["hits"] == 3
    assert snap["misses"] == 1
    assert snap["ttl_seconds"] == 60


@pytest.mark.asyncio
async def test_redis_cache_close(mock_redis_client):
    cache = RedisCache(ttl_seconds=60)
    cache._client = mock_redis_client
    await cache.close()
    mock_redis_client.aclose.assert_awaited_once()
    assert cache._client is None


# ---------------------------------------------------------------------------
# RedisRateLimiter
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_rl_client():
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    client.script_load = AsyncMock(return_value="sha1abc")
    client.evalsha = AsyncMock(return_value=[4, 60])  # remaining=4, window=60
    client.aclose = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_redis_rate_limiter_connect_success(mock_rl_client):
    limiter = RedisRateLimiter(default_limit=5)
    with patch("web_scraper.redis_cache.aioredis") as mock_aioredis:
        mock_aioredis.from_url.return_value = mock_rl_client
        await limiter.connect("redis://localhost:6379/0")
    assert limiter._client is not None
    assert limiter._lua_sha == "sha1abc"


@pytest.mark.asyncio
async def test_redis_rate_limiter_check_allowed(mock_rl_client):
    limiter = RedisRateLimiter(default_limit=5, window_seconds=60)
    limiter._client = mock_rl_client
    limiter._lua_sha = "sha1abc"
    mock_rl_client.evalsha.return_value = [4, 60]  # 4 remaining, window 60s

    remaining, window = await limiter.check("user:1")

    assert remaining == 4
    assert window == 60


@pytest.mark.asyncio
async def test_redis_rate_limiter_check_exceeded(mock_rl_client):
    from web_scraper.api_runtime import RateLimitExceeded

    limiter = RedisRateLimiter(default_limit=5, window_seconds=60)
    limiter._client = mock_rl_client
    limiter._lua_sha = "sha1abc"
    # remaining < 0 means quota exhausted (retry_after = 30s)
    mock_rl_client.evalsha.return_value = [-1, 30]

    with pytest.raises(RateLimitExceeded) as exc_info:
        await limiter.check("user:1")

    assert exc_info.value.retry_after == 30


@pytest.mark.asyncio
async def test_redis_rate_limiter_raises_unavailable_when_not_connected():
    limiter = RedisRateLimiter(default_limit=5)
    with pytest.raises(RedisUnavailable, match="not connected"):
        await limiter.check("key")


@pytest.mark.asyncio
async def test_redis_rate_limiter_close(mock_rl_client):
    limiter = RedisRateLimiter(default_limit=5)
    limiter._client = mock_rl_client
    await limiter.close()
    mock_rl_client.aclose.assert_awaited_once()
    assert limiter._client is None


# ---------------------------------------------------------------------------
# api.py fallback behaviour (no Redis → in-memory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_uses_in_memory_when_redis_url_not_set():
    """When REDIS_URL is absent the app must start with in-memory cache/rate-limiter."""
    import os

    from web_scraper.api import create_app
    from web_scraper.api_runtime import InMemoryTTLCache, SlidingWindowRateLimiter

    env_backup = os.environ.pop("REDIS_URL", None)
    try:
        app = create_app()
        assert isinstance(app.state.cache, InMemoryTTLCache)
        assert isinstance(app.state.rate_limiter, SlidingWindowRateLimiter)
    finally:
        if env_backup is not None:
            os.environ["REDIS_URL"] = env_backup
