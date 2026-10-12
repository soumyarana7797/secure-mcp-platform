"""Token-bucket rate limiting behind an interface (swap in Redis for multi-replica deployments)."""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Limit:
    requests_per_minute: int
    burst: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0

    @property
    def retry_after_header(self) -> str:
        return str(max(1, math.ceil(self.retry_after_s)))


class RateLimiter(Protocol):
    async def check(self, key: str, limit: Limit) -> Decision: ...


class InMemoryTokenBucket:
    """Per-process buckets. With N replicas the effective limit is up to N times the setting."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 100_000
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_update)

    def __len__(self) -> int:
        return len(self._buckets)

    async def check(self, key: str, limit: Limit) -> Decision:
        now = self._clock()
        rate = limit.requests_per_minute / 60.0  # tokens per second
        tokens, updated = self._buckets.get(key, (float(limit.burst), now))
        # A changed policy takes effect immediately: never hold more than the current burst.
        tokens = min(float(limit.burst), tokens + (now - updated) * rate)
        if tokens >= 1.0:
            self._store(key, tokens - 1.0, now, limit)
            return Decision(True)
        self._store(key, tokens, now, limit)
        return Decision(False, (1.0 - tokens) / rate)

    def _store(self, key: str, tokens: float, now: float, limit: Limit) -> None:
        self._buckets[key] = (tokens, now)
        if len(self._buckets) > self._max_keys:
            self._evict_idle(now, limit)

    def _evict_idle(self, now: float, limit: Limit) -> None:
        """Drop buckets that have fully refilled: they are indistinguishable from new ones."""
        rate = limit.requests_per_minute / 60.0
        full_after = limit.burst / rate
        for key in [k for k, (_, t) in self._buckets.items() if now - t >= full_after]:
            del self._buckets[key]
