"""Rate-limit policies and rate_limited audit events against real PostgreSQL."""

from contextlib import asynccontextmanager

import psycopg
import pytest

from shared.audit.logger import AuditEvent, AuditLogger
from shared.rate_limit.limiter import Limit
from shared.rate_limit.policies import FALLBACK, DbPolicyStore
from tests.integration.test_gateway_db import tenant_id


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clean_overrides(seeded: dict[str, str]):  # noqa: ANN201
    def wipe() -> None:
        with psycopg.connect(seeded["owner"], autocommit=True) as conn:
            conn.execute("DELETE FROM rate_limit_policies WHERE tenant_id IS NOT NULL")

    wipe()
    yield
    wipe()


@pytest.mark.anyio
async def test_platform_defaults_come_from_the_migration(app_db, seeded, clean_overrides) -> None:  # noqa: ANN001
    store = DbPolicyStore(app_db)
    acme = tenant_id(seeded, "acme")
    assert await store.limit_for("tenant", acme) == Limit(600, 100)
    assert await store.limit_for("user", acme) == Limit(120, 30)


@pytest.mark.anyio
async def test_a_tenant_override_wins_for_that_tenant_only_and_is_picked_up_after_the_ttl(
    app_db,
    seeded,
    clean_overrides,  # noqa: ANN001
) -> None:
    clock = Clock()
    store = DbPolicyStore(app_db, ttl_s=30, clock=clock)
    acme, globex = tenant_id(seeded, "acme"), tenant_id(seeded, "globex")
    assert await store.limit_for("user", acme) == Limit(120, 30)

    with psycopg.connect(seeded["owner"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO rate_limit_policies (tenant_id, scope, requests_per_minute, burst) "
            "VALUES (%s, 'user', 10, 5)",
            (acme,),
        )
    assert await store.limit_for("user", acme) == Limit(120, 30)  # cached until the TTL passes
    clock.now += 31
    assert await store.limit_for("user", acme) == Limit(10, 5)
    assert await store.limit_for("user", globex) == Limit(120, 30)
    assert await store.limit_for("tenant", acme) == Limit(600, 100)  # other scope unaffected


@pytest.mark.anyio
async def test_the_database_enforces_sane_policies(seeded, clean_overrides) -> None:  # noqa: ANN001
    acme = tenant_id(seeded, "acme")
    with psycopg.connect(seeded["owner"], autocommit=True) as conn:
        for rpm, burst, scope in ((0, 5, "user"), (10, 0, "user"), (10, 5, "everyone")):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO rate_limit_policies VALUES (gen_random_uuid(), %s, %s, %s, %s)",
                    (acme, scope, rpm, burst),
                )
        conn.execute(
            "INSERT INTO rate_limit_policies VALUES (gen_random_uuid(), %s, 'user', 10, 5)", (acme,)
        )
        with pytest.raises(psycopg.errors.UniqueViolation):  # one policy per (tenant, scope)
            conn.execute(
                "INSERT INTO rate_limit_policies VALUES (gen_random_uuid(), %s, 'user', 20, 5)",
                (acme,),
            )


class _BrokenDb:
    @asynccontextmanager
    async def tx(self):  # noqa: ANN202
        raise ConnectionError("database down")
        yield


@pytest.mark.anyio
async def test_policy_outage_uses_safe_fallbacks_and_keeps_stale_values(app_db, seeded) -> None:  # noqa: ANN001
    acme = tenant_id(seeded, "acme")
    never_loaded = DbPolicyStore(_BrokenDb())  # type: ignore[arg-type]
    assert await never_loaded.limit_for("user", acme) == FALLBACK["user"]

    clock = Clock()
    store = DbPolicyStore(app_db, ttl_s=30, clock=clock)
    assert await store.limit_for("user", acme) == Limit(120, 30)
    store._db = _BrokenDb()  # type: ignore[assignment]  # the database now goes away
    clock.now += 31
    assert await store.limit_for("user", acme) == Limit(120, 30)  # stale, not reset


@pytest.mark.anyio
async def test_rate_limited_events_can_be_audited(app_db, seeded) -> None:  # noqa: ANN001
    acme = tenant_id(seeded, "acme")
    audit = AuditLogger(app_db, flush_interval_s=0.01)
    await audit.start()
    audit.emit(
        AuditEvent(
            event_type="rate_limited",
            decision="deny",
            outcome="user_limit",
            request_id="rl-1",
            tenant_id=acme,
        )
    )
    await audit.stop()
    with psycopg.connect(seeded["owner"]) as conn:
        row = conn.execute(
            "SELECT event_type, decision, outcome FROM audit_events WHERE request_id = 'rl-1'"
        ).fetchone()
    assert row == ("rate_limited", "deny", "user_limit")
