"""Latency tracking with P50/P95 reporting for the web tools."""

from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict

from web_scraper.api_runtime import MetricsRegistry


class LatencyTracker:
    """Bounded per-stage millisecond samples with percentile snapshots."""

    def __init__(self, max_samples: int = 2_048) -> None:
        self.max_samples = max(max_samples, 1)
        self._samples: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=self.max_samples))
        self._lock = threading.Lock()

    def record(self, stage: str, milliseconds: float) -> None:
        with self._lock:
            self._samples[stage].append(max(float(milliseconds), 0.0))

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            samples = {stage: list(values) for stage, values in self._samples.items()}

        result: Dict[str, Dict[str, float]] = {}
        for stage, values in sorted(samples.items()):
            if not values:
                continue
            ordered = sorted(values)
            count = len(ordered)
            result[stage] = {
                "count": count,
                "p50_ms": round(_nearest_rank(ordered, 0.50), 2),
                "p95_ms": round(_nearest_rank(ordered, 0.95), 2),
                "avg_ms": round(sum(ordered) / count, 2),
                "max_ms": round(ordered[-1], 2),
            }
        return result


class WebToolsMetrics:
    """Record stage timings into the shared latency tracker and Prometheus registry."""

    def __init__(
        self,
        tracker: LatencyTracker,
        registry: MetricsRegistry | None = None,
    ) -> None:
        self.tracker = tracker
        self.registry = registry

    def observe(self, stage: str, milliseconds: float, **labels: Any) -> None:
        self.tracker.record(stage, milliseconds)
        if self.registry is not None:
            self.registry.observe_histogram(
                f"web_tools_{stage}_seconds",
                max(milliseconds, 0.0) / 1000.0,
                **labels,
            )

    def count(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        if self.registry is not None:
            self.registry.increment(f"web_tools_{name}", amount, **labels)


def _nearest_rank(ordered: list[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]
