"""User server: identity comes only from the gateway token; the DB role is least-privilege."""

import base64
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import httpx2
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from psycopg.errors import InsufficientPrivilege

from apps.gateway.config import Settings as GatewaySettings
from apps.gateway.main import create_app as create_gateway
from apps.gateway.router.connector import InternalBackendAuth
from apps.user_server.config import Settings
from apps.user_server.server import build_mcp, create_app
from shared.auth.internal import (
    InternalIdentity,
    InternalTokenSigner,
    internal_identity_var,
)
from shared.auth.principal import Principal
from shared.registry.manifest import BackendManifest, ToolManifest
from shared.registry.models import BackendSpec
from tests.integration.test_gateway_db import pin, rpc, tenant_id

KEY = Ed25519PrivateKey.generate()
PUBLIC_B64 = base64.b64encode(
    KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
).decode()
BASE = "http://127.0.0.1:9003"
ACCEPT = {"Accept": "application/json, text/event-stream"}
USER_MANIFEST = BackendManifest(
    name="user",
    url=f"{BASE}/mcp",
    audience="mcp-user",
    tools={"get_user_profile": ToolManifest(permission="users:read")},
)


def user_id(dsns: dict[str, str], sub: str):  # noqa: ANN201
    with psycopg.connect(dsns["owner"]) as conn:
        return conn.execute("SELECT id FROM users WHERE cognito_sub = %s", (sub,)).fetchone()[0]


def identity(dsns, sub: str, tenant: str, purpose: str = "call") -> InternalIdentity:  # noqa: ANN001
    return InternalIdentity(
        subject=sub,
        user_id=user_id(dsns, sub),
        tenant_id=tenant_id(dsns, tenant),
        tenant_slug=tenant,
        purpose=purpose,
        tool="get_user_profile",
        request_id=None,
    )


async def call_as(user_db, who: InternalIdentity | None):  # noqa: ANN001, ANN201
    token = internal_identity_var.set(who)
    try:
        async with Client(build_mcp(user_db)) as client:
            return await client.call_tool("get_user_profile", {})
    finally:
        internal_identity_var.reset(token)


# ------------------------------------------------------------------ the tool


@pytest.mark.anyio
async def test_returns_the_callers_own_profile_and_nothing_sensitive(user_db, seeded) -> None:  # noqa: ANN001
    result = await call_as(user_db, identity(seeded, "sub-alice", "acme"))
    profile = result.structured_content
    assert not result.is_error
    assert profile["display_name"] == "Alice" and profile["roles"] == ["analyst"]
    assert profile["tenant"] == {"slug": "acme", "name": "Acme Corp"}
    assert "cognito_sub" not in str(profile) and "sub-alice" not in str(profile)


@pytest.mark.anyio
async def test_the_tool_has_no_argument_through_which_to_ask_for_someone_else(user_db) -> None:  # noqa: ANN001
    async with Client(build_mcp(user_db)) as client:
        (tool,) = (await client.list_tools()).tools
    assert tool.input_schema.get("properties", {}) == {}


@pytest.mark.anyio
async def test_extra_arguments_naming_another_user_are_ignored_or_rejected_never_honoured(
    user_db,
    seeded,  # noqa: ANN001
) -> None:
    bob_id = str(user_id(seeded, "sub-bob"))
    token = internal_identity_var.set(identity(seeded, "sub-alice", "acme"))
    try:
        async with Client(build_mcp(user_db)) as client:
            result = await client.call_tool(
                "get_user_profile", {"user_id": bob_id, "tenant_id": str(uuid4())}
            )
    finally:
        internal_identity_var.reset(token)
    assert "Bob" not in str(result.structured_content) and "Bob" not in str(result.content)
    if not result.is_error:
        assert result.structured_content["display_name"] == "Alice"


@pytest.mark.anyio
async def test_each_tenant_gets_its_own_user(user_db, seeded) -> None:  # noqa: ANN001
    carol = await call_as(user_db, identity(seeded, "sub-carol", "globex"))
    assert carol.structured_content["display_name"] == "Carol"
    assert carol.structured_content["tenant"]["slug"] == "globex"


@pytest.mark.anyio
async def test_a_user_id_paired_with_the_wrong_tenant_finds_nothing(user_db, seeded) -> None:  # noqa: ANN001
    forged = InternalIdentity(
        "sub-alice",
        user_id(seeded, "sub-alice"),
        tenant_id(seeded, "globex"),
        "globex",
        "call",
        "get_user_profile",
        None,
    )
    result = await call_as(user_db, forged)
    assert result.is_error and "profile not found" in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["none", "discovery"])
async def test_calls_without_a_real_user_identity_are_refused(user_db, seeded, kind: str) -> None:  # noqa: ANN001
    who = None
    if kind == "discovery":
        who = InternalIdentity("system:catalog", None, None, None, "discovery", None, None)
    result = await call_as(user_db, who)
    assert result.is_error and "no authenticated user" in result.content[0].text


@pytest.mark.anyio
async def test_a_discovery_purpose_identity_is_refused_even_if_it_carries_ids(
    user_db, seeded
) -> None:  # noqa: ANN001
    sneaky = identity(seeded, "sub-alice", "acme", purpose="discovery")
    result = await call_as(user_db, sneaky)
    assert result.is_error and "no authenticated user" in result.content[0].text


@pytest.mark.anyio
async def test_a_disabled_user_has_no_profile(user_db, seeded) -> None:  # noqa: ANN001
    who = identity(seeded, "sub-bob", "acme")
    with psycopg.connect(seeded["owner"], autocommit=True) as conn:
        conn.execute("UPDATE users SET status = 'disabled' WHERE cognito_sub = 'sub-bob'")
    result = await call_as(user_db, who)
    assert result.is_error and "profile not found" in result.content[0].text


# ------------------------------------------------------------------ the database role


@pytest.mark.anyio
async def test_the_service_role_is_least_privilege(user_db, seeded) -> None:  # noqa: ANN001
    acme = tenant_id(seeded, "acme")
    forbidden = (
        "SELECT cognito_sub FROM users",
        "SELECT * FROM users",  # includes cognito_sub
        "SELECT * FROM audit_events",
        "SELECT * FROM role_permissions",
        "SELECT * FROM mcp_tools",
        "UPDATE users SET roles = '{admin}'",
        "DELETE FROM users",
        "INSERT INTO tenants (slug, name) VALUES ('evil', 'x')",
    )
    for statement in forbidden:
        with pytest.raises(InsufficientPrivilege):
            async with user_db.tenant_tx(acme) as conn:
                await conn.execute(statement)


@pytest.mark.anyio
async def test_the_service_role_cannot_enumerate_tenants_or_users_without_a_tenant(user_db) -> None:  # noqa: ANN001
    async with user_db.tx() as conn:
        users = await (await conn.execute("SELECT count(*) AS n FROM users")).fetchone()
        tenants = await (await conn.execute("SELECT count(*) AS n FROM tenants")).fetchone()
    assert users["n"] == 0 and tenants["n"] == 0


# ------------------------------------------------------------------ real HTTP, real middleware


@pytest.mark.anyio
async def test_the_backend_rejects_anything_but_a_gateway_token(user_db) -> None:  # noqa: ANN001
    app = create_app(Settings(internal_public_key_b64=PUBLIC_B64), db=user_db)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            body = rpc("tools/call", {"name": "get_user_profile", "arguments": {}})
            assert (await c.post("/mcp", json=body, headers=ACCEPT)).status_code == 401
            forged = InternalTokenSigner(Ed25519PrivateKey.generate()).mint(
                audience="mcp-user", principal=None
            )
            bad = {**ACCEPT, "Authorization": f"Bearer {forged}"}
            assert (await c.post("/mcp", json=body, headers=bad)).status_code == 401


def test_user_server_refuses_to_start_without_a_key() -> None:
    with pytest.raises(RuntimeError, match="INTERNAL_PUBLIC_KEY"):
        create_app(Settings(internal_public_key_b64=""))


class AsgiConnector:
    """Connects to a backend ASGI app in-process but over the REAL HTTP client stack, so the
    gateway's internal token travels in a real Authorization header through the real middleware."""

    def __init__(self, spec: BackendSpec, app) -> None:  # noqa: ANN001
        self.spec, self.app = spec, app

    @asynccontextmanager
    async def connect(self, headers=None):  # noqa: ANN001, ANN201
        transport = httpx2.ASGITransport(app=self.app)
        async with httpx2.AsyncClient(
            transport=transport, base_url=BASE, headers=dict(headers or {})
        ) as http:
            async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
                yield client


@pytest.mark.anyio
async def test_end_to_end_through_the_gateway_each_user_gets_their_own_profile(
    app_db,
    user_db,
    seeded,  # noqa: ANN001
) -> None:
    backend = create_app(Settings(internal_public_key_b64=PUBLIC_B64), db=user_db)
    async with backend.router.lifespan_context(backend):
        spec = BackendSpec(name="user", url=f"{BASE}/mcp", audience="mcp-user")
        connector = AsgiConnector(spec, backend)
        async with connector.connect(
            {
                "Authorization": "Bearer "
                + InternalTokenSigner(KEY).mint(audience="mcp-user", principal=None)
            }
        ) as client:
            await pin(seeded, USER_MANIFEST, None, live=(await client.list_tools()).tools)

        users = {}
        with psycopg.connect(seeded["owner"]) as conn:
            for sub in ("sub-alice", "sub-bob", "sub-carol"):
                row = conn.execute(
                    "SELECT u.id, u.tenant_id, t.slug, u.roles FROM users u JOIN tenants t "
                    "ON t.id = u.tenant_id WHERE cognito_sub = %s",
                    (sub,),
                ).fetchone()
                users[sub] = Principal(sub, row[0], row[1], row[2], frozenset(row[3]))

        class WhoIs:
            async def resolve(self, headers):  # noqa: ANN001, ANN201
                return users.get(headers.get("x-test-user", ""))

        gateway = create_gateway(
            GatewaySettings(),
            db=app_db,
            connector_factory=lambda s: connector,
            resolver=WhoIs(),
            backend_auth=InternalBackendAuth(InternalTokenSigner(KEY)),
        )
        async with gateway.router.lifespan_context(gateway):
            transport = httpx.ASGITransport(app=gateway)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:9000"
            ) as c:

                async def post(user: str, method: str, params: dict | None = None):  # noqa: ANN202
                    return await c.post(
                        "/mcp", json=rpc(method, params), headers={**ACCEPT, "X-Test-User": user}
                    )

                alice_tools = (await post("sub-alice", "tools/list")).json()["result"]["tools"]
                bob_tools = (await post("sub-bob", "tools/list")).json()["result"]["tools"]
                call = {"name": "user.get_user_profile", "arguments": {}}
                alice = (await post("sub-alice", "tools/call", call)).json()["result"]
                bob_try = (await post("sub-bob", "tools/call", call)).json()

    assert [t["name"] for t in alice_tools] == ["user.get_user_profile"]  # alice has users:read
    assert bob_tools == []  # bob (viewer) does not
    assert alice["structuredContent"]["display_name"] == "Alice"
    assert alice["structuredContent"]["tenant"]["slug"] == "acme"
    assert "error" in bob_try and "Unknown tool" in bob_try["error"]["message"]
