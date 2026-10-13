"""Bounded retries with exponential backoff and full jitter. Callers decide what is retryable."""

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx2

# Connection-level failures: the request very likely never reached (or never completed on) the
# backend. Timeouts are deliberately NOT here: the time budget is already spent.
_TRANSIENT = (ConnectionError, httpx2.TransportError)


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def is_transient(exc: BaseException) -> bool:
    """True if every underlying error is a connection-level failure (unwraps task groups)."""
    leaves = _leaves(exc)
    return bool(leaves) and all(
        isinstance(e, _TRANSIENT) and not isinstance(e, TimeoutError | httpx2.TimeoutException)
        for e in leaves
    )


async def retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_s: float = 0.05,
    max_delay_s: float = 1.0,
    is_retryable: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,  # noqa: S311 - jitter, not security
) -> T:
    """Call `fn` up to `attempts` times. Sleeps `rng() * min(max, base * 2**n)` between tries."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            await sleep(rng() * min(max_delay_s, base_delay_s * 2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover
