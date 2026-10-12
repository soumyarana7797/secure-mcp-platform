"""Gateway -> backend identity: forged, replayed, misaddressed and misused tokens."""

import base64
import hashlib
import hmac
import json
import time
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.football_server.config import Settings as FootballSettings
from apps.football_server.server import create_app as create_football_app
from apps.gateway.router.connector import InternalBackendAuth
from shared.auth.internal import (
    INTERNAL_ISSUER,
    SYSTEM_SUBJECT,
    InternalTokenError,
    InternalTokenSigner,
    InternalTokenVerifier,
)
from shared.auth.principal import Principal, principal_var
from tests.integration.test_gateway import (
    InProcessConnector,
    make_football,
    make_principal,
    make_rig,
)
from tests.support.oidc import LocalIssuer

KEY = Ed25519PrivateKey.generate()
PUBLIC_B64 = base64.b64encode(
    KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
).decode()
ALICE = Principal("sub-alice", uuid4(), uuid4(), "acme", frozenset({"analyst"}))


class Clock:
    now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def reason(verifier: InternalTokenVerifier, token: str) -> str:
    with pytest.raises(InternalTokenError) as exc:
        verifier.verify(token)
    return exc.value.reason


def make(clock: Clock | None = None):
    clock = clock or Clock()
    signer = InternalTokenSigner(KEY, clock=clock)
    verifier = InternalTokenVerifier(KEY.public_key(), "mcp-football", clock=clock)
    return signer, verifier, clock


def test_roundtrip_carries_the_identity_the_backend_must_trust() -> None:
    signer, verifier, _ = make()
    identity = verifier.verify(
        signer.mint(audience="mcp-football", principal=ALICE, tool="get_fixtures", request_id="r1")
    )
    assert (identity.subject, identity.tenant_id, identity.user_id) == (
        "sub-alice",
        ALICE.tenant_id,
        ALICE.user_id,
    )
    assert (identity.tool, identity.request_id, identity.purpose) == ("get_fixtures", "r1", "call")


def test_tokens_live_for_60_seconds_only() -> None:
    signer, verifier, clock = make()
    token = signer.mint(audience="mcp-football", principal=ALICE)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 60
    clock.now += 60 + 6  # beyond the 5s leeway
    assert reason(verifier, token) == "expired"


def test_a_token_addressed_to_another_backend_is_rejected() -> None:
    signer, verifier, _ = make()  # verifier is the football backend
    assert reason(verifier, signer.mint(audience="mcp-lab", principal=ALICE)) == "wrong_audience"


def test_a_token_issued_in_the_future_is_rejected() -> None:
    signer, verifier, clock = make()
    token = signer.mint(audience="mcp-football", principal=ALICE)
    clock.now -= 3600  # verifier's clock is an hour behind the token's issue time
    assert reason(verifier, token) == "issued_in_the_future"


def test_a_token_signed_by_someone_else_is_rejected() -> None:
    _, verifier, clock = make()
    attacker = InternalTokenSigner(Ed25519PrivateKey.generate(), clock=clock)
    assert (
        reason(verifier, attacker.mint(audience="mcp-football", principal=ALICE)) == "bad_signature"
    )


def test_tampering_is_detected() -> None:
    signer, verifier, _ = make()
    header, payload, signature = signer.mint(audience="mcp-football", principal=ALICE).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["tid"] = str(uuid4())  # try to move to another tenant
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    assert reason(verifier, f"{header}.{forged}.{signature}") == "bad_signature"


def test_alg_none_and_hmac_confusion_are_rejected() -> None:
    _, verifier, _ = make()

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    claims = {
        "iss": INTERNAL_ISSUER,
        "aud": "mcp-football",
        "sub": "x",
        "purpose": "discovery",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    payload = b64(json.dumps(claims).encode())
    none_token = f"{b64(json.dumps({'alg': 'none'}).encode())}.{payload}."
    assert reason(verifier, none_token) == "alg_not_allowed"

    header = b64(json.dumps({"alg": "HS256"}).encode())
    public_pem = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    mac = hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert reason(verifier, f"{header}.{payload}.{b64(mac)}") == "alg_not_allowed"


def test_a_users_cognito_style_token_is_not_a_valid_internal_token() -> None:
    _, verifier, _ = make()
    assert reason(verifier, LocalIssuer().token()) == "alg_not_allowed"  # RS256, not EdDSA


def test_a_call_token_without_a_user_or_tenant_is_rejected() -> None:
    _, verifier, clock = make()
    now = int(clock())
    bare = jwt.encode(
        {
            "iss": INTERNAL_ISSUER,
            "aud": "mcp-football",
            "sub": "x",
            "purpose": "call",
            "iat": now,
            "exp": now + 60,
        },
        KEY,
        algorithm="EdDSA",
    )
    assert reason(verifier, bare) == "missing_identity"


def test_discovery_tokens_have_a_system_identity_and_no_tenant() -> None:
    signer, verifier, _ = make()
    identity = verifier.verify(signer.mint(audience="mcp-football", principal=None))
    assert (identity.subject, identity.purpose) == (SYSTEM_SUBJECT, "discovery")
    assert identity.tenant_id is None and identity.user_id is None


# ---------------------------------------------------------------- the backend as a real HTTP app

RPC = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def football_app():
    api_provider = make_football()  # only used for its tools; no upstream call is made on list
    settings = FootballSettings(data_api_token="t", internal_public_key_b64=PUBLIC_B64)
    return create_football_app(settings, provider=api_provider._tool_manager and _NullProvider())


class _NullProvider:
    async def fixtures(self, *a):  # noqa: ANN002, ANN202
        return []

    async def live(self, *a):  # noqa: ANN002, ANN202
        return []

    async def aclose(self) -> None:
        return None


async def post(token: str | None, host: str = "127.0.0.1:9002") -> httpx.Response:
    app = football_app()
    headers = {**ACCEPT, "Host": host}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9002"
        ) as c:
            return await c.post("/mcp", json=RPC, headers=headers)


@pytest.mark.anyio
async def test_backend_rejects_direct_calls_that_bypass_the_gateway() -> None:
    assert (await post(None)).status_code == 401
    assert (await post("garbage")).status_code == 401
    assert (await post(LocalIssuer().token())).status_code == 401  # a real-looking user token
    wrong_aud = InternalTokenSigner(KEY).mint(audience="mcp-lab", principal=ALICE)
    assert (await post(wrong_aud)).status_code == 401
    attacker = InternalTokenSigner(Ed25519PrivateKey.generate())
    assert (await post(attacker.mint(audience="mcp-football", principal=ALICE))).status_code == 401


@pytest.mark.anyio
async def test_backend_accepts_a_genuine_gateway_token() -> None:
    token = InternalTokenSigner(KEY).mint(audience="mcp-football", principal=ALICE)
    response = await post(token)
    assert response.status_code == 200
    assert {t["name"] for t in response.json()["result"]["tools"]} == {
        "get_fixtures",
        "get_live_scores",
    }
    discovery = InternalTokenSigner(KEY).mint(audience="mcp-football", principal=None)
    assert (await post(discovery)).status_code == 200


def test_backend_refuses_to_start_without_a_verification_key() -> None:
    with pytest.raises(RuntimeError, match="INTERNAL_PUBLIC_KEY"):
        create_football_app(FootballSettings(data_api_token="t", internal_public_key_b64=""))


# ---------------------------------------------------------------- what the gateway actually sends


class RecordingConnector(InProcessConnector):
    def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        super().__init__(*a, **kw)
        self.seen: list[dict[str, str]] = []

    def connect(self, headers=None):  # noqa: ANN001, ANN201
        self.seen.append(dict(headers or {}))
        return super().connect(headers)


@pytest.mark.anyio
async def test_gateway_sends_a_scoped_internal_token_and_never_the_users_token() -> None:
    clock = Clock()
    signer = InternalTokenSigner(KEY, clock=clock)
    rig = await make_rig(backend_auth=InternalBackendAuth(signer))
    recorder = RecordingConnector(
        rig.connectors["football"].spec, rig.connectors["football"].server
    )
    rig.connectors["football"] = recorder
    await rig.start()  # discovery now goes through the recorder too
    recorder.seen.clear()

    user_token = LocalIssuer().token()
    principal_var.set(make_principal("analyst"))
    from mcp import Client

    async with Client(rig.server) as client:
        await client.call_tool("football.get_fixtures", {"competition": "PL"})

    (headers,) = recorder.seen
    sent = headers["Authorization"].removeprefix("Bearer ")
    assert sent != user_token and user_token not in json.dumps(headers)
    verifier = InternalTokenVerifier(KEY.public_key(), "mcp-football", clock=clock)
    identity = verifier.verify(sent)
    assert identity.tool == "get_fixtures" and identity.purpose == "call"
    assert identity.subject == "sub-test"


@pytest.mark.anyio
async def test_discovery_uses_a_system_token_not_a_user_identity() -> None:
    clock = Clock()
    rig = await make_rig(backend_auth=InternalBackendAuth(InternalTokenSigner(KEY, clock=clock)))
    recorder = RecordingConnector(
        rig.connectors["football"].spec, rig.connectors["football"].server
    )
    rig.connectors["football"] = recorder
    await rig.start()
    sent = recorder.seen[0]["Authorization"].removeprefix("Bearer ")
    identity = InternalTokenVerifier(KEY.public_key(), "mcp-football", clock=clock).verify(sent)
    assert identity.subject == SYSTEM_SUBJECT and identity.purpose == "discovery"
