from shared.audit.throttle import AuditThrottle


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_first_event_is_recorded_then_the_flood_is_counted_and_reported_later() -> None:
    clock = Clock()
    throttle = AuditThrottle(interval_s=10, clock=clock)
    assert throttle.admit("k") == (True, 0)
    assert [throttle.admit("k") for _ in range(4)] == [(False, 0)] * 4
    clock.now += 10
    assert throttle.admit("k") == (True, 4)  # the next record says how many were suppressed
    assert throttle.admit("k") == (False, 0)


def test_different_keys_are_throttled_independently() -> None:
    throttle = AuditThrottle(interval_s=10, clock=Clock())
    assert throttle.admit("a")[0] and throttle.admit("b")[0]
    assert not throttle.admit("a")[0]


def test_memory_stays_bounded_by_forgetting_keys_outside_the_window() -> None:
    clock = Clock()
    throttle = AuditThrottle(interval_s=1, clock=clock, max_keys=100)
    for i in range(1000):
        throttle.admit(f"user-{i}")
        clock.now += 2
    assert len(throttle._state) <= 101
