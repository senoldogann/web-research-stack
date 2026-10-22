"""Pacing and health for search providers.

DuckDuckGo refused searches sent in parallel (``bot_detected``) and answered
the same searches sent one at a time, so each provider gets a small number of
concurrent calls (one by default) with a random pause between them. A failed
call earns the provider a short cooldown, doubling while failures repeat;
other providers are not affected.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


class ProviderGate:
    """Concurrency slots for one provider plus a random gap between its calls."""

    def __init__(self, concurrency: int = 1, min_gap_ms: int = 0, max_gap_ms: int = 0) -> None:
        self._semaphore = asyncio.Semaphore(max(concurrency, 1))
        self._min_gap = max(min_gap_ms, 0) / 1000
        self._max_gap = max(max_gap_ms, min_gap_ms, 0) / 1000
        self._next_start = 0.0

    @asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        """Hold one of the provider's slots."""
        async with self._semaphore:
            yield

    async def pace(self) -> None:
        """Wait out the gap the previous call left; call while holding a turn."""
        wait = self._next_start - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    def finish(self) -> None:
        """Start the gap before the next call."""
        self._next_start = time.monotonic() + random.uniform(self._min_gap, self._max_gap)


@dataclass
class ProviderHealth:
    """Consecutive failures of one provider and the cooldown they earned."""

    consecutive_failures: int = 0
    cooldown_until: float = 0.0

    def cooling_down(self) -> bool:
        return self.cooldown_until > time.monotonic()

    def retry_after(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())

    def record_failure(self, base_seconds: float, max_seconds: float) -> float:
        """Count a failure and return the cooldown it starts."""
        self.consecutive_failures += 1
        if base_seconds <= 0:
            self.cooldown_until = 0.0
            return 0.0
        cooldown = min(max_seconds, base_seconds * 2 ** (self.consecutive_failures - 1))
        self.cooldown_until = time.monotonic() + cooldown
        return cooldown

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
