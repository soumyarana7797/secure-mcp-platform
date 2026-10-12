"""Audit trail against real PostgreSQL: append-only, tenant-scoped, safe, and resilient."""

import json
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException

from apps.gateway.config import Settings
from apps.gateway.main import create_app
from shared.audit.logger import AuditEvent, AuditLogger
from shared.auth.jwt_validator import JwksCache, TokenValidator
from shared.auth.resolver import BearerTokenResolver, DbPrincipalLookup
from shared.registry.models import BackendSpec
from tests.integration.test_gateway import InProcessConnector, make_football, make_lab
from tests.integration.test_gateway_db import (
    ACCEPT,
    FOOTBALL_MANIFEST,
    LAB_MANIFEST,
    pin,
    rpc,
    tenant_id,
)
from tests.support.oidc import CLIENT_ID, ISSUER, SCOPE, LocalIssuer, StaticJwks


def event(tenant: UUID | None, **kw) -> AuditEvent:  # noqa: ANN003
    base = {"event_type": "tool_call", "decision": "allow", "outcome": "ok", "tool": "football.x"}
    return AuditEvent(tenant_id=tenant, **{**base, **kw})


def owner_rows(dsns: dict[str, str], where: str, params: tuple | None = None) -> list[dict]:
    with psycopg.connect(dsns["owner"], row_factory=psycopg.rows.dict_row) as conn:
        query = f"SELECT * FROM audit_events WHERE {where} ORDER BY id"  # noqa: S608
        return conn.execute(query, params).fetchall()


INSERT = (
    "INSERT INTO audit_events (event_type, tenant_id, decision, outcome, request_id) "
    "VALUES ('tool_call', %s, 'allow', 'ok', %s)"
)


# ------------------------------------------------------------------ database guarantees


@pytest.mark.anyio
async def test_a_tenant_can_only_write_audit_rows_for_itself(app_db, seeded) -> None:  # noqa: ANN001
    acme, globex = tenant_id(seeded, "acme"), tenant_id(seeded, "globex")
    async with app_db.tenant_tx(acme) as conn:
        await conn.execute(INSERT, (acme, "own-row"))
    with pytest.raises(InsufficientPrivilege):  # row-level security WITH CHECK
        async with app_db.tenant_tx(acme) as conn:
            await conn.execute(INSERT, (globex, "forged-row"))
    async with app_db.tx() as conn:  # anonymous events carry no tenant
        await conn.execute(INSERT, (None, "anon-row"))
    assert owner_rows(seeded, "request_id = 'forged-row'") == []
    assert len(owner_rows(seeded, "request_id IN ('own-row', 'anon-row')")) == 2


@pytest.mark.anyio
async def test_a_tenant_reads_only_its_own_audit_trail(app_db, seeded) -> None:  # noqa: ANN001
    acme, globex = tenant_id(seeded, "acme"), tenant_id(seeded, "globex")
    async with app_db.tenant_tx(globex) as conn:
        await conn.execute(INSERT, (globex, "visibility-globex"))
    async with app_db.tenant_tx(acme) as conn:
        await conn.execute(INSERT, (acme, "visibility-acme"))
    async with app_db.tx() as conn:
        await conn.execute(INSERT, (None, "visibility-anon"))
    async with app_db.tenant_tx(acme) as conn:
        rows = await (
            await conn.execute(
                "SELECT request_id FROM audit_events WHERE request_id LIKE 'visibility-%'"
            )
        ).fetchall()
    assert [r["request_id"] for r in rows] == ["visibility-acme"]


@pytest.mark.anyio
async def test_the_audit_trail_is_append_only(app_db, seeded) -> None:  # noqa: ANN001
    acme = tenant_id(seeded, "acme")
    async with app_db.tenant_tx(acme) as conn:
        await conn.execute(INSERT, (acme, "immutable-row"))
    for statement in ("UPDATE audit_events SET outcome = 'x'", "DELETE FROM audit_events"):
        with pytest.raises(InsufficientPrivilege):  # the app role has no such privilege
            async with app_db.tenant_tx(acme) as conn:
                await conn.execute(statement)
    with psycopg.connect(seeded["owner"], autocommit=True) as conn:  # even the OWNER is blocked
        with pytest.raises(RaiseException, match="append-only"):
            conn.execute(
                "UPDATE audit_events SET outcome = 'tampered' WHERE request_id = 'immutable-row'"
            )
        with pytest.raises(RaiseException, match="append-only"):
            conn.execute("DELETE FROM audit_events WHERE request_id = 'immutable-row'")
    assert owner_rows(seeded, "request_id = 'immutable-row'")[0]["outcome"] == "ok"


# ------------------------------------------------------------------ the writer


@pytest.mark.anyio
async def test_logger_batches_events_for_several_tenants_and_drains_on_stop(app_db, seeded) -> None:  # noqa: ANN001
    acme, globex = tenant_id(seeded, "acme"), tenant_id(seeded, "globex")
    audit = AuditLogger(app_db, flush_interval_s=0.05)
    await audit.start()
    for i in range(7):
        audit.emit(event(acme if i % 2 else globex, request_id=f"batch-{i}", args_hash="h"))
    audit.emit(
        event(
            None,
            event_type="auth_failed",
            decision="deny",
            outcome="expired",
            request_id="batch-anon",
        )
    )
    await audit.stop()
    rows = owner_rows(seeded, "request_id LIKE 'batch-%'")
    assert len(rows) == 8
    assert {r["tenant_id"] for r in rows} == {acme, globex, None}


class _BrokenDb:
    @asynccontextmanager
    async def tx(self):  # noqa: ANN202
        raise ConnectionError("database down")
        yield

    tenant_tx = lambda self, _t: self.tx()  # noqa: E731


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def capture_audit_errors():  # noqa: ANN201
    handler, log = _Capture(), logging.getLogger("shared.audit.logger")
    log.addHandler(handler)
    return handler, log


@pytest.mark.anyio
async def test_a_full_queue_never_blocks_the_caller_and_the_event_is_not_lost() -> None:
    handler, log = capture_audit_errors()
    try:
        audit = AuditLogger(_BrokenDb(), queue_size=2)  # type: ignore[arg-type]  # worker not started
        for i in range(5):
            audit.emit(event(None, request_id=f"full-{i}"))
        assert audit.dropped == 3
        assert sum("audit_fallback why=queue_full" in line for line in handler.lines) == 3
        assert "full-4" in " ".join(handler.lines)  # the dropped event content is in the log
    finally:
        log.removeHandler(handler)


@pytest.mark.anyio
async def test_a_database_outage_falls_back_to_the_log_and_the_worker_survives() -> None:
    handler, log = capture_audit_errors()
    try:
        audit = AuditLogger(_BrokenDb(), flush_interval_s=0.01)  # type: ignore[arg-type]
        await audit.start()
        audit.emit(event(None, request_id="outage-1"))
        await audit.flush()
        audit.emit(event(None, request_id="outage-2"))  # the worker must still be alive
        await audit.stop()
        assert audit.failed == 2
        text = " ".join(handler.lines)
        assert "why=db_error" in text and "outage-1" in text and "outage-2" in text
    finally:
        log.removeHandler(handler)


# ------------------------------------------------------------------ end to end, real HTTP


@pytest.mark.anyio
async def test_end_to_end_every_outcome_is_audited_and_no_token_is_ever_stored(
    app_db, seeded
) -> None:  # noqa: ANN001
    football, (lab, _) = make_football(), make_lab()
    await pin(seeded, FOOTBALL_MANIFEST, football)
    await pin(seeded, LAB_MANIFEST, lab)
    connectors = {
        "football": InProcessConnector(BackendSpec(name="football", url="x"), football),
        "lab": InProcessConnector(BackendSpec(name="lab", url="x"), lab),
    }
    issuer = LocalIssuer()
    validator = TokenValidator(ISSUER, frozenset({CLIENT_ID}), SCOPE, JwksCache(StaticJwks(issuer)))
    app = create_app(
        Settings(public_base_url="http://127.0.0.1:9000"),
        db=app_db,
        connector_factory=lambda s: connectors[s.name],
        resolver=BearerTokenResolver(validator, DbPrincipalLookup(app_db)),
    )

    alice, bob = issuer.token(sub="sub-alice"), issuer.token(sub="sub-bob")
    expired = issuer.token(sub="sub-alice", exp=1)

    async def call(client, token, rid, name, args):  # noqa: ANN001, ANN202
        return await client.post(
            "/mcp",
            json=rpc("tools/call", {"name": name, "arguments": args}),
            headers={**ACCEPT, "Authorization": f"Bearer {token}", "X-Request-ID": rid},
        )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9000") as c:
            await call(c, alice, "e2e-ok", "football.get_fixtures", {"competition": "PL"})
            await call(c, alice, "e2e-bad", "lab.echo", {"text": 123, "api_key": "sk-live-SECRET"})
            await call(c, bob, "e2e-denied", "lab.echo", {"text": "private note about someone"})
            await call(c, alice, "e2e-unknown", "football.nope", {})
            await call(c, expired, "e2e-expired", "football.get_fixtures", {})
            await c.post(
                "/mcp", json=rpc("tools/list"), headers={**ACCEPT, "X-Request-ID": "e2e-anon"}
            )
    # leaving the lifespan drained the audit queue

    rows = {r["request_id"]: r for r in owner_rows(seeded, "request_id LIKE 'e2e-%'")}
    assert {k: (v["event_type"], v["decision"], v["outcome"]) for k, v in rows.items()} == {
        "e2e-ok": ("tool_call", "allow", "ok"),
        "e2e-bad": ("tool_call", "allow", "invalid_args"),
        "e2e-denied": ("tool_call", "deny", "denied"),
        "e2e-unknown": ("tool_call", "deny", "unknown_tool"),
        "e2e-expired": (
            "auth_failed",
            "deny",
            "expired",
        ),  # precise reason is for us, not the caller
        "e2e-anon": ("auth_failed", "deny", "no_credentials"),
    }
    acme = tenant_id(seeded, "acme")
    assert rows["e2e-ok"]["tenant_id"] == acme and rows["e2e-ok"]["subject"] == "sub-alice"
    assert rows["e2e-ok"]["redacted_args"] == {"competition": "PL"}
    assert rows["e2e-expired"]["tenant_id"] is None and rows["e2e-anon"]["tenant_id"] is None
    assert rows["e2e-bad"]["redacted_args"] == {"text": {"type": "int"}, "api_key": "[REDACTED]"}
    assert rows["e2e-denied"]["redacted_args"] == {"text": {"type": "string", "len": 26}}
    assert all(r["args_hash"] for k, r in rows.items() if r["event_type"] == "tool_call")

    everything = json.dumps(list(rows.values()), default=str)
    for secret in (alice, bob, expired, "Bearer", "sk-live-SECRET", "private note about someone"):
        assert secret not in everything
