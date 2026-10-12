"""Collapses floods of identical audit events into one record plus a count.

An attacker controls how many rejected requests they send, so recording each one would let them
fill the audit table. The first event per key is recorded immediately; later ones inside the
window are counted and reported on the next recorded event.
"""

import time
from collections.abc import Callable


class AuditThrottle:
    def __init__(
        self,
        interval_s: float = 10.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self._interval = interval_s
        self._clock = clock
        self._max_keys = max_keys
        self._state: dict[str, tuple[float, int]] = {}  # key -> (last_emitted, suppressed_since)

    def admit(self, key: str) -> tuple[bool, int]:
        """Return (record_it, how_many_were_suppressed_since_the_last_record)."""
        now = self._clock()
        last, suppressed = self._state.get(key, (None, 0))
        if last is not None and now - last < self._interval:
            self._state[key] = (last, suppressed + 1)
            return False, 0
        if len(self._state) >= self._max_keys:  # bounded memory: forget keys outside the window
            self._state = {k: v for k, v in self._state.items() if now - v[0] < self._interval}
        self._state[key] = (now, 0)
        return True, suppressed
