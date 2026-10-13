"""The proxy under failure: retries, circuit breaker, bulkhead, cancellation and isolation."""

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from apps.gateway.router.resilience import GuardRegistry, ResilienceConfig
from shared.audit.logger import AuditEvent
from shared.registry.models import BackendSpec
from tests.integration.test_gateway import InProcessConnector, Rig, make_rig

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


class Clock:
    now = 5000.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)

    @property
    def outcomes(self) -> list[str]:
        return [e.outcome for e in self.events if e.event_type == "tool_call"]


class FaultyConnector(InProcessConnector):
    """Counts connection attempts and can refuse the next N (or all) of them."""

    def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        super().__init__(*a, **kw)
        self.attempts = 0
        self.refuse_next = 0

    def connect(self, headers=None):  # noqa: ANN001, ANN201
        self.attempts += 1
        if self.refuse_next > 0:
            self.refuse_next -= 1
            raise ConnectionRefusedError("injected: connection refused")
        return super().connect(headers)


def make_rt() -> tuple[MCPServer, list[str]]:
    """A backend with read-only and mutating tools, a slow one, and one that rejects input."""
    calls: list[str] = []
    rt = MCPServer(name="rt")

    @rt.tool(annotations=READ_ONLY)
    def ping() -> str:
        calls.append("ping")
        return "pong"

    @rt.tool(annotations=READ_ONLY)
    async def slow_read() -> str:
        calls.append("slow_read")
        await asyncio.sleep(0.4)
        return "done"

    @rt.tool(annotations=ToolAnnotations(read_only_hint=False))
    def write() -> str:  # a mutating tool: must never be retried
        calls.append("write")
        return "written"

    @rt.tool(annotations=READ_ONLY)
    def reject() -> str:
        calls.append("reject")
        raise ToolError("record not found")  # an application-level error, not an outage

    return rt, calls


async def make_world(**cfg) -> tuple[Rig, FaultyConnector, Clock, Recorder, list[str]]:  # noqa: ANN003
    clock, audit = Clock(), Recorder()
    config = ResilienceConfig(
        **{
            "retry_attempts": 2,
            "retry_base_delay_s": 0.001,
            "breaker_min_calls": 4,
            "breaker_failure_rate": 0.5,
            "breaker_reset_s": 10,
            "queue_timeout_s": 0.05,
            **cfg,
        }
    )
    rig = await make_rig()
    rt, calls = make_rt()
    spec = BackendSpec(
        name="rt", url="x", audience="mcp-rt", timeout_s=2.0, tool_timeouts={"slow_read": 0.2}
    )
    connector = FaultyConnector(spec, rt)
    from tests.integration.test_gateway import pin_all

    rig.specs.append(spec)
    rig.connectors["rt"] = connector
    pinned = await pin_all(
        rt, {n: "football:read" for n in ("ping", "slow_read", "write", "reject")}
    )
    pinned["slow_read"] = pinned["slow_read"].model_copy(update={"timeout_s": 0.2})
    rig.pinned["rt"] = pinned
    rig.guards, rig.audit = GuardRegistry(config, clock=clock), audit
    await rig.start()
    connector.attempts = 0
    calls.clear()
    return rig, connector, clock, audit, calls


async def call(rig: Rig, tool: str, args: dict | None = None):  # noqa: ANN201
    async with rig.client("analyst") as client:
        return await client.call_tool(tool, args or {})


def text(result) -> str:  # noqa: ANN001
    return result.content[0].text


# ------------------------------------------------------------------ retries


@pytest.mark.anyio
async def test_a_read_only_tool_survives_one_connection_blip() -> None:
    rig, rt, _, _, calls = await make_world()
    rt.refuse_next = 1
    result = await call(rig, "rt.ping")
    assert not result.is_error and rt.attempts == 2 and calls == ["ping"]


@pytest.mark.anyio
async def test_a_mutating_tool_is_never_retried() -> None:
    rig, rt, _, _, calls = await make_world()
    rt.refuse_next = 1
    result = await call(rig, "rt.write")
    assert result.is_error and text(result) == "Backend 'rt' is unavailable"
    assert rt.attempts == 1 and calls == []  # one attempt only


@pytest.mark.anyio
async def test_retries_are_bounded() -> None:
    rig, rt, _, _, _ = await make_world(retry_attempts=3)
    rt.refuse_next = 99
    result = await call(rig, "rt.ping")
    assert result.is_error and rt.attempts == 3


@pytest.mark.anyio
async def test_a_timeout_is_not_retried_because_the_budget_is_spent() -> None:
    rig, rt, _, _, calls = await make_world()
    result = await call(rig, "rt.slow_read")
    assert result.is_error and "timed out after 0.2s" in text(result)
    assert calls == ["slow_read"] and rt.attempts == 1


# ------------------------------------------------------------------ circuit breaker


async def hammer(rig: Rig, rt: FaultyConnector, n: int) -> None:
    rt.refuse_next = 10_000
    for _ in range(n):
        await call(rig, "rt.write")  # not retried: one failure per call


@pytest.mark.anyio
async def test_repeated_infrastructure_failures_open_the_circuit_and_calls_fail_fast() -> None:
    rig, rt, _, audit, _ = await make_world()
    await hammer(rig, rt, 4)
    assert rig.guards.circuit_states() == {"rt": "open"}
    before = rt.attempts
    result = await call(rig, "rt.ping")
    assert result.is_error and "temporarily unavailable" in text(result)
    assert rt.attempts == before  # the dead backend was not even contacted
    assert audit.outcomes[-1] == "circuit_open"


@pytest.mark.anyio
async def test_repeated_timeouts_also_open_the_circuit() -> None:
    rig, rt, _, _, calls = await make_world()
    for _ in range(4):
        assert "timed out" in text(await call(rig, "rt.slow_read"))
    assert rig.guards.circuit_states() == {"rt": "open"}
    before = len(calls)
    assert "temporarily unavailable" in text(await call(rig, "rt.slow_read"))
    assert len(calls) == before  # no more calls wasted on a backend that keeps timing out


@pytest.mark.anyio
async def test_the_circuit_recovers_through_a_half_open_probe() -> None:
    rig, rt, clock, _, _ = await make_world()
    await hammer(rig, rt, 4)
    rt.refuse_next = 0  # the backend heals
    clock.now += 10
    probe = await call(rig, "rt.ping")
    assert not probe.is_error
    assert rig.guards.circuit_states() == {"rt": "closed"}
    assert not (await call(rig, "rt.ping")).is_error


@pytest.mark.anyio
async def test_a_failed_probe_keeps_the_circuit_open() -> None:
    rig, rt, clock, _, _ = await make_world()
    await hammer(rig, rt, 4)
    clock.now += 10
    await call(rig, "rt.write")  # the probe hits a still-dead backend
    assert rig.guards.circuit_states() == {"rt": "open"}
    clock.now += 5
    before = rt.attempts
    assert "temporarily unavailable" in text(await call(rig, "rt.ping"))
    assert rt.attempts == before


@pytest.mark.anyio
async def test_application_errors_never_open_the_circuit() -> None:
    """Otherwise one tenant could trip the shared breaker for everybody ('breaker poisoning')."""
    rig, rt, _, _, calls = await make_world()
    for _ in range(30):
        result = await call(rig, "rt.reject")
        assert result.is_error and "record not found" in text(result)
    assert rig.guards.circuit_states() == {"rt": "closed"} and len(calls) == 30
    assert not (await call(rig, "rt.ping")).is_error


@pytest.mark.anyio
async def test_a_sick_backend_does_not_affect_the_others() -> None:
    rig, rt, _, _, _ = await make_world()
    await hammer(rig, rt, 4)
    assert rig.guards.circuit_states()["rt"] == "open"
    football = await call(rig, "football.get_live_scores")
    lab = await call(rig, "lab.echo", {"text": "hi"})
    assert not football.is_error and not lab.is_error


@pytest.mark.anyio
async def test_a_cancelled_probe_does_not_wedge_the_breaker() -> None:
    rig, rt, clock, _, _ = await make_world()
    await hammer(rig, rt, 4)
    rt.refuse_next = 0
    clock.now += 10  # half-open: exactly one trial call is allowed
    task = asyncio.create_task(call(rig, "rt.slow_read"))
    await asyncio.sleep(0.05)  # the probe is in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.guards.guard("rt").breaker.allow()  # the trial slot was released


# ------------------------------------------------------------------ bulkhead


@pytest.mark.anyio
async def test_excess_concurrent_calls_are_shed_without_blaming_the_backend() -> None:
    rig, rt, _, audit, calls = await make_world(max_concurrency=2)
    rig.pinned["rt"]["slow_read"] = rig.pinned["rt"]["slow_read"].model_copy(
        update={"timeout_s": 1.0}
    )
    await rig.catalog.refresh()
    results = await asyncio.gather(*(call(rig, "rt.slow_read") for _ in range(5)))
    busy = [r for r in results if r.is_error and "busy" in text(r)]
    ok = [r for r in results if not r.is_error]
    assert len(ok) == 2 and len(busy) == 3 and calls.count("slow_read") == 2
    assert rig.guards.circuit_states() == {"rt": "closed"}  # overload is not an outage
    assert sorted(audit.outcomes).count("overloaded") == 3
