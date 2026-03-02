"""Async retry with exponential back-off and uniform jitter.

Usage
-----
::

    from web_scraper.research.retry_utils import async_retry

    result = await async_retry(
        lambda: my_async_call(arg1, arg2),
        label="DuckDuckGo search",
        max_attempts=3,
        base_delay=1.5,
    )
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def async_retry(
    coro_fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    multiplier: float = 2.0,
    jitter: float = 0.4,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    label: str = "call",
) -> T:
    """Retry *coro_fn* with exponential back-off + uniform ±jitter.

    Args:
        coro_fn:              Zero-argument async callable returning ``T``.
        max_attempts:         Total number of attempts (1 = no retry).
        base_delay:           Initial sleep after first failure (seconds).
        multiplier:           Back-off multiplier applied after each failure.
        jitter:               Fraction of computed delay added/subtracted randomly.
        retryable_exceptions: Exception types that should trigger a retry.
        label:                Human-readable name used in log messages.

    Returns:
        The first successful return value of *coro_fn*.

    Raises:
        The last exception if every attempt fails.
    """
    delay = base_delay
    last_exc: Exception = RuntimeError("No attempts were made")

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt >= max_attempts:
                logger.warning(
                    "%s failed after %d/%d attempt(s): %s",
                    label,
                    attempt,
                    max_attempts,
                    exc,
                )
                break
            sleep_time = delay * (1.0 + random.uniform(-jitter, jitter))
            logger.debug(
                "%s attempt %d/%d failed (%s); retrying in %.2fs",
                label,
                attempt,
                max_attempts,
                exc,
                sleep_time,
            )
            await asyncio.sleep(max(0.0, sleep_time))
            delay *= multiplier

    raise last_exc
