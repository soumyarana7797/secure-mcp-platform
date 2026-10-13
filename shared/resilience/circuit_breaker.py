"""Circuit breaker: stop calling a failing dependency, then probe carefully for recovery.

CLOSED    normal; outcomes are recorded in a rolling time window.
OPEN      too many recent failures: calls are refused immediately until `reset_timeout_s` passes.
HALF_OPEN a limited number of trial calls; success closes the circuit, failure re-opens it.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    min_calls: int = 10  # do not judge on a handful of calls
    failure_rate: float = 0.5  # open at >= this fraction of failures
    window_s: float = 30.0
    reset_timeout_s: float = 15.0
    half_open_max_calls: int = 1
    clock: Callable[[], float] = time.monotonic
    _events: deque[tuple[float, bool]] = field(default_factory=deque)  # (time, succeeded)
    _state: State = State.CLOSED
    _opened_at: float = 0.0
    _probes: int = 0
    opened_count: int = 0  # how many times it has tripped (for metrics)

    @property
    def state(self) -> State:
        return self._state

    def allow(self) -> bool:
        """May a call proceed? In HALF_OPEN each True consumes a trial slot, which the caller
        MUST settle with record_success / record_failure / record_neutral."""
        now = self.clock()
        if self._state is State.OPEN:
            if now - self._opened_at < self.reset_timeout_s:
                return False
            self._state, self._probes = State.HALF_OPEN, 0
        if self._state is State.HALF_OPEN:
            if self._probes >= self.half_open_max_calls:
                return False
            self._probes += 1
        return True

    def record_success(self) -> None:
        if self._state is State.HALF_OPEN:
            self._close()
            return
        self._record(True)

    def record_failure(self) -> None:
        if self._state is State.HALF_OPEN:
            self._open()
            return
        self._record(False)

    def record_neutral(self) -> None:
        """A call that was allowed but says nothing about health (cancelled, rejected locally)."""
        if self._state is State.HALF_OPEN and self._probes > 0:
            self._probes -= 1

    def _record(self, ok: bool) -> None:
        if self._state is State.OPEN:  # late result from a call started before it opened
            return
        now = self.clock()
        self._events.append((now, ok))
        while self._events and now - self._events[0][0] > self.window_s:
            self._events.popleft()
        total = len(self._events)
        if total >= self.min_calls:
            failures = sum(1 for _, good in self._events if not good)
            if failures / total >= self.failure_rate:
                self._open()

    def _open(self) -> None:
        self._state, self._opened_at, self._probes = State.OPEN, self.clock(), 0
        self._events.clear()
        self.opened_count += 1

    def _close(self) -> None:
        self._state, self._probes = State.CLOSED, 0
        self._events.clear()
