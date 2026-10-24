"""Pacing and health for search providers.

DuckDuckGo refused searches sent in parallel (``bot_detected``) and answered the same searches sent
one at a time, so each provider gets a small number of concurrent calls (one by default) with a
random pause between them.

Each provider also keeps its record, in this process: its calls and what they came to (an answer,
a wall such as a captcha or a bot check, an HTTP error, a timeout, any other exception), the
latency of the calls, and the last error. The record steers the search:

- a **wall** starts a cooldown at once, and so does the Nth failure in a row of any other kind
  (``search_provider_failure_limit``, 3 by default); the cooldown is ``search_provider_cooldown_seconds``
  (10 minutes by default), doubling each time a provider that comes back is refused again, up to
  ``search_provider_max_cooldown_seconds``; a provider that cools down is skipped for it, and
  asked again only as a last resort when every other provider failed;
- when the cooldown is over the provider is on probation: its record of failures is cleared and
  the next call is the probe, a success clears the rest, another failure starts a longer cooldown;
- providers that are not cooling are ordered by how their recent calls went (``standing``): one
  that answered at least 4 of every 5 of its recent calls goes before one that answered half, and
  that before one that mostly failed; the configured order breaks ties.

Nothing here reads the network: it is arithmetic over what the calls left.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from web_scraper.web_tools.errors import WebToolError

# How a failed call is classified.
WALL = "wall"  # a captcha, a bot check, a refusal: the provider says no to this client
TIMEOUT = "timeout"
HTTP_ERROR = "http_error"
ERROR = "error"  # any other exception, the provider's parser included

_WALL_DETAILS = frozenset(
    {
        "bot_detected",
        "captcha",
        "access_blocked",
        "access_wall",
        "challenge",
        "consent_wall",
        "blocked",
    }
)
_WALL_STATUSES = frozenset({403, 429, 999})
_TIMEOUT_DETAILS = frozenset({"search_timeout", "angle_budget"})

# The share of recent calls that answered, above which a provider is good and above which it is
# shaky; under the second it is poor.
GOOD_RATE = 0.8
SHAKY_RATE = 0.5
# Standing, best first: the number a provider's record earns, and COOLING for one that is cooling.
GOOD, SHAKY, POOR, COOLING = 0, 1, 2, 3
_RECENT_CALLS = 20
# A record of fewer calls than this says nothing: one failure is no reason to prefer another provider.
MIN_CALLS = 4


def failure_kind(error: WebToolError) -> str:
    """What kind of failure an error of a provider's call is: a wall, a timeout, an HTTP error, other."""
    detail = (error.detail or "").lower()
    if error.code == "timeout" or detail in _TIMEOUT_DETAILS:
        return TIMEOUT
    if (
        error.code == "blocked"
        or detail in _WALL_DETAILS
        or "captcha" in detail
        or "bot check" in detail
        or "bot_" in detail
    ):
        return WALL
    if detail == "rate_limited" or detail.startswith("http_") or detail == "invalid_key":
        return HTTP_ERROR
    if error.status_code is not None and error.status_code in _WALL_STATUSES:
        return WALL
    if error.status_code is not None and error.status_code >= 400:
        return HTTP_ERROR
    return ERROR


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
    """One provider's record in this process, and the cooldown it has earned.

    ``consecutive_failures`` and ``cooldown_until`` are what the cooldown reads; the rest is the
    record ``web_health`` reports. *clock* is injectable for a test (monotonic seconds).
    """

    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    trips: int = 0
    calls: int = 0
    ok: int = 0
    failures: int = 0
    walls: int = 0
    timeouts: int = 0
    http_errors: int = 0
    errors: int = 0
    latency_ms_total: float = 0.0
    latency_calls: int = 0
    last_error: Optional[dict[str, Any]] = None
    last_error_at: Optional[float] = None
    last_ok_at: Optional[float] = None
    recent: deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=_RECENT_CALLS))
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def cooling_down(self) -> bool:
        return self.cooldown_until > self.clock()

    def retry_after(self) -> float:
        return max(0.0, self.cooldown_until - self.clock())

    def record_failure(
        self,
        base_seconds: float,
        max_seconds: float,
        *,
        kind: str = ERROR,
        limit: int = 1,
        error: Optional[WebToolError] = None,
        latency_ms: Optional[float] = None,
    ) -> float:
        """Count a failed call and return the cooldown it starts (0 when it starts none).

        A wall starts one at once; any other kind starts one when it is the *limit*-th failure in
        a row (1, the default, is every failure). The cooldown is *base_seconds*, doubling for
        each cooldown started since the provider last answered, up to *max_seconds*; a base of 0
        turns cooldowns off.
        """
        now = self.clock()
        self.calls += 1
        self.failures += 1
        self.consecutive_failures += 1
        if kind == WALL:
            self.walls += 1
        elif kind == TIMEOUT:
            self.timeouts += 1
        elif kind == HTTP_ERROR:
            self.http_errors += 1
        else:
            self.errors += 1
        if latency_ms is not None:
            self.latency_ms_total += latency_ms
            self.latency_calls += 1
        self.recent.append((now, False))
        self.last_error_at = now
        self.last_error = {
            "kind": kind,
            "code": error.code if error is not None else None,
            "detail": error.detail if error is not None else None,
            "status_code": error.status_code if error is not None else None,
        }
        if base_seconds <= 0:
            self.cooldown_until = 0.0
            return 0.0
        if kind != WALL and self.consecutive_failures < max(limit, 1):
            return 0.0
        self.trips += 1
        cooldown = min(max_seconds, base_seconds * 2 ** (self.trips - 1))
        self.cooldown_until = now + cooldown
        # The cooldown is the penalty for what went before: the probe after it starts clean.
        self.recent.clear()
        return cooldown

    def record_success(self, latency_ms: Optional[float] = None) -> None:
        self.calls += 1
        self.ok += 1
        self.consecutive_failures = 0
        self.trips = 0
        self.cooldown_until = 0.0
        self.last_ok_at = self.clock()
        if latency_ms is not None:
            self.latency_ms_total += latency_ms
            self.latency_calls += 1
        self.recent.append((self.last_ok_at, True))

    def success_rate(self, window_seconds: float) -> Optional[float]:
        """The share of the recent calls (the last 20, within the window) that answered.

        None when they are fewer than ``MIN_CALLS``: a record that short is no evidence.
        """
        floor = self.clock() - window_seconds
        recent = [ok for when, ok in self.recent if when >= floor]
        return sum(recent) / len(recent) if len(recent) >= MIN_CALLS else None

    def standing(self, window_seconds: float = 1800.0) -> int:
        """GOOD, SHAKY or POOR by the recent calls, COOLING while the cooldown lasts."""
        if self.cooling_down():
            return COOLING
        rate = self.success_rate(window_seconds)
        if rate is None or rate >= GOOD_RATE:
            return GOOD
        return SHAKY if rate >= SHAKY_RATE else POOR

    @property
    def mean_latency_ms(self) -> Optional[float]:
        return self.latency_ms_total / self.latency_calls if self.latency_calls else None

    def report(self) -> dict[str, Any]:
        """The record as ``web_health`` says it: no secrets, no network, numbers and one error."""
        now = self.clock()
        entry: dict[str, Any] = {
            "state": "cooling" if self.cooling_down() else "ok",
            "calls": self.calls,
            "ok": self.ok,
            "failures": self.failures,
            "block_rate": round(self.walls / self.calls, 3) if self.calls else 0.0,
            "mean_latency_ms": (
                round(self.mean_latency_ms, 1) if self.mean_latency_ms is not None else None
            ),
        }
        if self.cooling_down():
            entry["cooldown_remaining_s"] = round(self.retry_after(), 1)
        elif self.standing() in (SHAKY, POOR):
            entry["state"] = "degraded"
        if self.last_error is not None and self.last_error_at is not None:
            entry["last_error"] = {
                **self.last_error,
                "seconds_ago": round(max(0.0, now - self.last_error_at), 1),
            }
        else:
            entry["last_error"] = None
        if self.last_ok_at is not None:
            entry["last_ok_seconds_ago"] = round(max(0.0, now - self.last_ok_at), 1)
        return entry
