"""Rate limiting: bucket maths, tenant/user fairness, policy overrides, and HTTP behaviour."""

from uuid import uuid4

import httpx
import pytest

from apps.gateway.config import Settings
from apps.gateway.main import create_app
from shared.audit.logger import AuditEvent
from shared.auth.principal import Principal
from shared.rate_limit.limiter import InMemoryTokenBucket, Limit
from shared.rate_limit.policies import StaticPolicyStore
from tests.integration.test_gateway import POLICY, make_principal, make_rig

LIMIT = Limit(requests_per_minute=60, burst=5)  # 1 token per second, burst of 5


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


async def drain(bucket: InMemoryTokenBucket, key: str, limit: Limit, n: int) -> list[bool]:
    return [(await bucket.check(key, limit)).allowed for _ in range(n)]


@pytest.mark.anyio
async def test_burst_then_throttle_then_refill() -> None:
    clock = Clock()
    bucket = InMemoryTokenBucket(clock=clock)
    assert await drain(bucket, "k", LIMIT, 7) == [True] * 5 + [False] * 2
    clock.now += 1  # one token refills per second
    assert await drain(bucket, "k", LIMIT, 2) == [True, False]
    clock.now += 600  # idle for a long time: refills only up to the burst, never beyond
    assert await drain(bucket, "k", LIMIT, 7) == [True] * 5 + [False] * 2


@pytest.mark.anyio
async def test_retry_after_tells_the_caller_when_to_come_back() -> None:
    clock = Clock()
    bucket = InMemoryTokenBucket(clock=clock)
    await drain(bucket, "k", LIMIT, 5)
    denied = await bucket.check("k", LIMIT)
    assert not denied.allowed and denied.retry_after_s == pytest.approx(1.0)
    assert denied.retry_after_header == "1"
    clock.now += 0.4
    assert (await bucket.check("k", LIMIT)).retry_after_s == pytest.approx(0.6)
    clock.now += 0.6
    assert (await bucket.check("k", LIMIT)).allowed


@pytest.mark.anyio
async def test_keys_are_independent() -> None:
    bucket = InMemoryTokenBucket(clock=Clock())
    await drain(bucket, "a", LIMIT, 10)
    assert (await bucket.check("b", LIMIT)).allowed


@pytest.mark.anyio
async def test_a_lowered_limit_applies_immediately() -> None:
    bucket = InMemoryTokenBucket(clock=Clock())
    await bucket.check("k", Limit(60, 100))  # bucket was created with a large burst
    assert await drain(bucket, "k", Limit(60, 2), 4) == [True, True, False, False]


@pytest.mark.anyio
async def test_memory_is_bounded_by_evicting_idle_buckets() -> None:
    clock = Clock()
    bucket = InMemoryTokenBucket(clock=clock, max_keys=50)
    for i in range(200):
        await bucket.check(f"user-{i}", LIMIT)
        clock.now += 10  # every earlier bucket is fully refilled by now
    assert len(bucket) <= 52


# ---------------------------------------------------------------- through the HTTP stack

RPC = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"Accept": "application/json, text/event-stream"}
BASE = "http://127.0.0.1:9000"


class Recorder:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class WhoIs:
    """Resolver that picks the principal from a header, so tests can switch users."""

    def __init__(self, users: dict[str, Principal]) -> None:
        self.users = users

    async def resolve(self, headers):  # noqa: ANN001, ANN201
        return self.users.get(headers.get("x-test-user", ""))


def person(tenant: Principal | None = None, name: str = "u") -> Principal:
    base = tenant or make_principal("viewer")
    return Principal(name, uuid4(), base.tenant_id, base.tenant_slug, frozenset({"viewer"}))


async def start(users: dict[str, Principal], policies: StaticPolicyStore, clock: Clock):
    rig = await make_rig()
    audit = Recorder()
    app = create_app(
        Settings(),
        registry=rig.registry(),
        connector_factory=lambda s: rig.connectors[s.name],
        resolver=WhoIs(users),
        policy=POLICY,
        audit=audit,
        limiter=InMemoryTokenBucket(clock=clock),
        limit_policies=policies,
    )
    return app, audit


async def hit(client: httpx.AsyncClient, user: str, path: str = "/mcp") -> httpx.Response:
    if path == "/mcp":
        return await client.post(path, json=RPC, headers={**ACCEPT, "X-Test-User": user})
    return await client.get(path, headers={"X-Test-User": user})


@pytest.mark.anyio
async def test_user_over_limit_gets_429_with_retry_after_and_an_audit_record() -> None:
    clock = Clock()
    alice = person(name="alice")
    policies = StaticPolicyStore({"tenant": Limit(6000, 1000), "user": Limit(60, 3)})
    app, audit = await start({"alice": alice}, policies, clock)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            codes = [(await hit(c, "alice")).status_code for _ in range(5)]
            blocked = await hit(c, "alice")
    assert codes == [200, 200, 200, 429, 429]
    assert blocked.headers["retry-after"] == "1" and blocked.json() == {"error": "rate_limited"}
    limited = [e for e in audit.events if e.event_type == "rate_limited"]
    assert len(limited) == 3 and limited[0].outcome == "user_limit"
    assert limited[0].user_id == alice.user_id and limited[0].decision == "deny"


@pytest.mark.anyio
async def test_one_noisy_user_does_not_block_a_colleague() -> None:
    clock = Clock()
    base = make_principal("viewer")
    noisy, quiet = person(base, "noisy"), person(base, "quiet")  # same tenant
    policies = StaticPolicyStore({"tenant": Limit(6000, 1000), "user": Limit(60, 2)})
    app, _ = await start({"noisy": noisy, "quiet": quiet}, policies, clock)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            for _ in range(5):
                await hit(c, "noisy")
            assert (await hit(c, "noisy")).status_code == 429
            assert (await hit(c, "quiet")).status_code == 200


@pytest.mark.anyio
async def test_a_noisy_tenant_is_capped_without_affecting_other_tenants() -> None:
    clock = Clock()
    acme = [person(make_principal("viewer"), f"a{i}") for i in range(3)]
    acme = [Principal(p.subject, p.user_id, acme[0].tenant_id, "acme", p.roles) for p in acme]
    globex = person(make_principal("viewer"), "g")
    users = {f"a{i}": p for i, p in enumerate(acme)} | {"g": globex}
    policies = StaticPolicyStore({"tenant": Limit(60, 4), "user": Limit(6000, 1000)})
    app, audit = await start(users, policies, clock)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            # three different acme users share acme's tenant budget of 4
            codes = [(await hit(c, f"a{i % 3}")).status_code for i in range(6)]
            other_tenant = await hit(c, "g")
    assert codes == [200, 200, 200, 200, 429, 429]
    assert other_tenant.status_code == 200
    assert {e.outcome for e in audit.events if e.event_type == "rate_limited"} == {"tenant_limit"}


@pytest.mark.anyio
async def test_a_tenant_specific_policy_overrides_the_platform_default() -> None:
    clock = Clock()
    vip, normal = person(make_principal("viewer"), "vip"), person(make_principal("viewer"), "norm")
    policies = StaticPolicyStore(
        {"tenant": Limit(6000, 1000), "user": Limit(60, 2)},
        {("user", vip.tenant_id): Limit(6000, 50)},
    )
    app, _ = await start({"vip": vip, "norm": normal}, policies, clock)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            vip_codes = {(await hit(c, "vip")).status_code for _ in range(10)}
            norm_codes = [(await hit(c, "norm")).status_code for _ in range(4)]
    assert vip_codes == {200}
    assert norm_codes == [200, 200, 429, 429]


@pytest.mark.anyio
async def test_requests_recover_after_waiting() -> None:
    clock = Clock()
    alice = person(name="alice")
    app, _ = await start(
        {"alice": alice},
        StaticPolicyStore({"tenant": Limit(6000, 1000), "user": Limit(60, 1)}),
        clock,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            assert (await hit(c, "alice")).status_code == 200
            assert (await hit(c, "alice")).status_code == 429
            clock.now += 1.0
            assert (await hit(c, "alice")).status_code == 200


@pytest.mark.anyio
async def test_health_discovery_and_unauthenticated_requests_do_not_spend_anyones_budget() -> None:
    clock = Clock()
    alice = person(name="alice")
    app, audit = await start(
        {"alice": alice},
        StaticPolicyStore({"tenant": Limit(6000, 1000), "user": Limit(60, 1)}),
        clock,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            for _ in range(20):
                assert (await c.get("/healthz")).status_code == 200
                assert (await c.get("/.well-known/oauth-protected-resource")).status_code == 200
                assert (await hit(c, "nobody")).status_code == 401  # unauthenticated
            assert (await hit(c, "alice")).status_code == 200  # budget untouched
    assert not [e for e in audit.events if e.event_type == "rate_limited"]
