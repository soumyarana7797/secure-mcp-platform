"""Per-backend protection: a circuit breaker and a bulkhead for each registered backend."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from shared.resilience.bulkhead import Bulkhead
from shared.resilience.circuit_breaker import CircuitBreaker


@dataclass(frozen=True)
class ResilienceConfig:
    max_concurrency: int = 20  # simultaneous calls per backend
    queue_timeout_s: float = 0.25  # how long an extra caller waits before being shed
    retry_attempts: int = 2  # total tries (1 = no retry); only for read-only tools
    retry_base_delay_s: float = 0.05
    breaker_min_calls: int = 10
    breaker_failure_rate: float = 0.5
    breaker_window_s: float = 30.0
    breaker_reset_s: float = 15.0


@dataclass
class BackendGuard:
    breaker: CircuitBreaker
    bulkhead: Bulkhead


@dataclass
class GuardRegistry:
    config: ResilienceConfig = field(default_factory=ResilienceConfig)
    clock: Callable[[], float] = time.monotonic
    _guards: dict[str, BackendGuard] = field(default_factory=dict)

    def guard(self, backend: str) -> BackendGuard:
        if backend not in self._guards:
            c = self.config
            self._guards[backend] = BackendGuard(
                CircuitBreaker(
                    backend,
                    min_calls=c.breaker_min_calls,
                    failure_rate=c.breaker_failure_rate,
                    window_s=c.breaker_window_s,
                    reset_timeout_s=c.breaker_reset_s,
                    clock=self.clock,
                ),
                Bulkhead(c.max_concurrency, c.queue_timeout_s),
            )
        return self._guards[backend]

    def circuit_states(self) -> dict[str, str]:
        return {name: g.breaker.state.value for name, g in self._guards.items()}
