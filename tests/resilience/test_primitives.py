"""Circuit breaker, bulkhead and retry: the building blocks of Phase 6."""

import asyncio

import httpx2
import pytest

from shared.resilience.bulkhead import Bulkhead, BulkheadFull
from shared.resilience.circuit_breaker import CircuitBreaker, State
from shared.resilience.retry import is_transient, retry


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def breaker(clock: Clock, **kw) -> CircuitBreaker:  # noqa: ANN003
    defaults = {"min_calls": 4, "failure_rate": 0.5, "window_s": 30, "reset_timeout_s": 10}
    return CircuitBreaker("b", clock=clock, **{**defaults, **kw})


def fail(b: CircuitBreaker, n: int) -> None:
    for _ in range(n):
        assert b.allow()
        b.record_failure()


# ------------------------------------------------------------------ circuit breaker


def test_a_healthy_backend_never_trips() -> None:
    b = breaker(Clock())
    for _ in range(100):
        assert b.allow()
        b.record_success()
    assert b.state is State.CLOSED and b.opened_count == 0


def test_it_does_not_judge_on_too_few_calls() -> None:
    b = breaker(Clock(), min_calls=10)
    fail(b, 9)  # 100% failures, but only 9 calls
    assert b.state is State.CLOSED
    fail(b, 1)
    assert b.state is State.OPEN


def test_it_opens_at_the_failure_rate_and_refuses_instantly() -> None:
    b = breaker(Clock())
    for ok in (True, False, True, False):  # 50% of 4 calls
        assert b.allow()
        (b.record_success if ok else b.record_failure)()
    assert b.state is State.OPEN and b.opened_count == 1
    assert not b.allow() and not b.allow()


def test_failures_below_the_rate_do_not_trip_it() -> None:
    b = breaker(Clock(), min_calls=10)
    for i in range(10):
        b.allow()
        (b.record_failure if i < 4 else b.record_success)()  # 40% failures
    assert b.state is State.CLOSED


def test_old_outcomes_age_out_of_the_window() -> None:
    clock = Clock()
    b = breaker(clock, min_calls=4)
    fail(b, 3)
    clock.now += 31  # those failures are now outside the 30s window
    b.allow()
    b.record_failure()  # 1 event in the window: below min_calls
    assert b.state is State.CLOSED


def test_after_the_reset_timeout_one_probe_is_allowed_and_success_closes_it() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, 4)
    assert b.state is State.OPEN and not b.allow()
    clock.now += 10
    assert b.allow() and b.state is State.HALF_OPEN
    assert not b.allow()  # only one trial at a time
    b.record_success()
    assert b.state is State.CLOSED and b.allow()


def test_a_failed_probe_reopens_the_circuit_for_another_full_timeout() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, 4)
    clock.now += 10
    assert b.allow()
    b.record_failure()
    assert b.state is State.OPEN and b.opened_count == 2
    clock.now += 9
    assert not b.allow()  # the clock restarted when the probe failed
    clock.now += 1
    assert b.allow()


def test_a_neutral_outcome_frees_the_probe_slot_without_judging_health() -> None:
    clock = Clock()
    b = breaker(clock)
    fail(b, 4)
    clock.now += 10
    assert b.allow() and not b.allow()
    b.record_neutral()  # e.g. the call was cancelled
    assert b.state is State.HALF_OPEN and b.allow()


def test_a_late_result_from_before_opening_is_ignored() -> None:
    b = breaker(Clock())
    fail(b, 4)
    b.record_success()  # a call that started earlier finishes now
    assert b.state is State.OPEN


# ------------------------------------------------------------------ bulkhead


@pytest.mark.anyio
async def test_bulkhead_limits_concurrency_and_sheds_the_excess() -> None:
    bulkhead = Bulkhead(max_concurrent=2, queue_timeout_s=0.05)
    gate = asyncio.Event()
    peak = 0

    async def work() -> str:
        nonlocal peak
        try:
            async with bulkhead.slot():
                peak = max(peak, bulkhead.in_flight)
                await gate.wait()
                return "ok"
        except BulkheadFull:
            return "shed"

    tasks = [asyncio.create_task(work()) for _ in range(5)]
    await asyncio.sleep(0.15)  # the 3 excess callers give up after the queue timeout
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sorted(results) == ["ok", "ok", "shed", "shed", "shed"]
    assert peak == 2 and bulkhead.rejected == 3 and bulkhead.in_flight == 0


@pytest.mark.anyio
async def test_a_queued_caller_gets_in_when_a_slot_frees_up() -> None:
    bulkhead = Bulkhead(max_concurrent=1, queue_timeout_s=1.0)
    order: list[str] = []

    async def first() -> None:
        async with bulkhead.slot():
            order.append("first")
            await asyncio.sleep(0.05)

    async def second() -> None:
        async with bulkhead.slot():
            order.append("second")

    await asyncio.gather(first(), second())
    assert order == ["first", "second"] and bulkhead.rejected == 0


@pytest.mark.anyio
async def test_the_slot_is_released_even_when_the_work_fails() -> None:
    bulkhead = Bulkhead(max_concurrent=1)
    with pytest.raises(RuntimeError):
        async with bulkhead.slot():
            raise RuntimeError("boom")
    assert bulkhead.in_flight == 0
    async with bulkhead.slot():
        pass


def test_a_bulkhead_needs_at_least_one_slot() -> None:
    with pytest.raises(ValueError):
        Bulkhead(0)


# ------------------------------------------------------------------ retry


async def no_sleep(_: float) -> None:
    return None


@pytest.mark.anyio
async def test_a_transient_failure_is_retried_until_it_succeeds() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionResetError("reset")
        return "ok"

    assert await retry(flaky, attempts=3, sleep=no_sleep) == "ok" and calls == 3


@pytest.mark.anyio
async def test_it_gives_up_after_the_attempt_limit_and_raises_the_last_error() -> None:
    calls = 0

    async def down() -> str:
        nonlocal calls
        calls += 1
        raise ConnectionRefusedError(f"attempt {calls}")

    with pytest.raises(ConnectionRefusedError, match="attempt 3"):
        await retry(down, attempts=3, sleep=no_sleep)
    assert calls == 3


@pytest.mark.anyio
async def test_non_transient_errors_are_not_retried() -> None:
    calls = 0

    async def broken() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("a bug, not a blip")

    with pytest.raises(ValueError):
        await retry(broken, attempts=5, sleep=no_sleep)
    assert calls == 1


@pytest.mark.anyio
async def test_backoff_is_exponential_jittered_and_capped() -> None:
    sleeps: list[float] = []

    async def record(delay: float) -> None:
        sleeps.append(delay)

    async def down() -> str:
        raise ConnectionError

    with pytest.raises(ConnectionError):
        await retry(
            down, attempts=5, base_delay_s=0.1, max_delay_s=0.3, sleep=record, rng=lambda: 1.0
        )
    assert sleeps == pytest.approx([0.1, 0.2, 0.3, 0.3])  # rng=1 -> the full (capped) delay
    sleeps.clear()
    with pytest.raises(ConnectionError):
        await retry(down, attempts=3, base_delay_s=0.1, sleep=record, rng=lambda: 0.5)
    assert sleeps == pytest.approx([0.05, 0.1])  # jitter scales the delay down


def test_transient_classification_unwraps_task_groups_and_excludes_timeouts() -> None:
    assert is_transient(ConnectionRefusedError())
    assert is_transient(httpx2.ConnectError("x"))
    assert is_transient(
        ExceptionGroup("g", [ConnectionError(), ExceptionGroup("n", [httpx2.ConnectError("y")])])
    )
    assert not is_transient(TimeoutError())  # the budget is already spent: do not retry
    assert not is_transient(httpx2.ReadTimeout("slow"))
    assert not is_transient(ExceptionGroup("g", [ConnectionError(), ValueError()]))  # mixed: no
    assert not is_transient(ValueError())


@pytest.mark.anyio
async def test_attempts_must_be_positive() -> None:
    async def never() -> None:
        return None

    with pytest.raises(ValueError):
        await retry(never, attempts=0)
