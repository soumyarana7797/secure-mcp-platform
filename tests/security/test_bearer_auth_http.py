"""Bearer authentication through the real HTTP stack (middleware, resolver, validator)."""

import httpx
import pytest

from apps.gateway.config import Settings
from apps.gateway.main import create_app
from shared.auth.jwt_validator import JwksCache, TokenValidator
from shared.auth.principal import Principal
from shared.auth.resolver import BearerTokenResolver
from tests.integration.test_gateway import POLICY, make_principal, make_rig
from tests.support.oidc import CLIENT_ID, ISSUER, SCOPE, LocalIssuer, StaticJwks

RPC = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"Accept": "application/json, text/event-stream"}
BASE = "http://127.0.0.1:9000"


async def start(issuer: LocalIssuer, users: dict[str, Principal]):
    validator = TokenValidator(ISSUER, frozenset({CLIENT_ID}), SCOPE, JwksCache(StaticJwks(issuer)))

    async def lookup(sub: str) -> Principal | None:
        return users.get(sub)

    rig = await make_rig()
    settings = Settings(public_base_url=BASE, cognito_issuer=ISSUER, required_scope=SCOPE)
    return create_app(
        settings,
        registry=rig.registry(),
        connector_factory=lambda spec: rig.connectors[spec.name],
        resolver=BearerTokenResolver(validator, lookup),
        policy=POLICY,
    )


async def post(app, token: str | None = None, header: str | None = None) -> httpx.Response:
    headers = dict(ACCEPT)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if header:
        headers["Authorization"] = header
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
        return await c.post("/mcp", json=RPC, headers=headers)


@pytest.fixture
def issuer() -> LocalIssuer:
    return LocalIssuer()


@pytest.mark.anyio
async def test_valid_token_for_a_provisioned_user_sees_their_tools(issuer: LocalIssuer) -> None:
    app = await start(issuer, {"sub-alice": make_principal("viewer")})
    async with app.router.lifespan_context(app):
        response = await post(app, issuer.token(sub="sub-alice"))
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names == {"football.get_fixtures", "football.get_live_scores"}


@pytest.mark.anyio
async def test_no_credentials_gets_a_discovery_challenge(issuer: LocalIssuer) -> None:
    app = await start(issuer, {})
    async with app.router.lifespan_context(app):
        response = await post(app)
    challenge = response.headers["www-authenticate"]
    assert response.status_code == 401
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource"' in challenge
    assert "error=" not in challenge  # RFC 6750: no error code when no credentials were sent


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        {"exp": 1},  # expired
        {"iss": "https://evil.test"},
        {"client_id": "other-app"},
        {"token_use": "id"},
    ],
)
async def test_bad_tokens_get_a_generic_401_that_reveals_nothing(
    issuer: LocalIssuer, mutation: dict
) -> None:
    app = await start(issuer, {"sub-alice": make_principal("viewer")})
    async with app.router.lifespan_context(app):
        response = await post(app, issuer.token(**mutation))
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    text = response.text + response.headers["www-authenticate"]
    for leak in ("expired", "issuer", "client", "token_use", "signature"):
        assert leak not in text.lower().replace("invalid_token", "")


@pytest.mark.anyio
async def test_missing_scope_is_403_insufficient_scope(issuer: LocalIssuer) -> None:
    app = await start(issuer, {"sub-alice": make_principal("viewer")})
    async with app.router.lifespan_context(app):
        response = await post(app, issuer.token(scope="openid"))
    assert response.status_code == 403
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


@pytest.mark.anyio
async def test_valid_token_for_an_unprovisioned_user_is_403(issuer: LocalIssuer) -> None:
    app = await start(issuer, {})  # nobody provisioned
    async with app.router.lifespan_context(app):
        response = await post(app, issuer.token(sub="sub-stranger"))
    assert response.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "token abc", "Bearer a b c"]
)
async def test_wrong_authorization_schemes_are_rejected(issuer: LocalIssuer, header: str) -> None:
    app = await start(issuer, {"sub-alice": make_principal("viewer")})
    async with app.router.lifespan_context(app):
        response = await post(app, header=header)
    assert response.status_code in (401, 403)


@pytest.mark.anyio
async def test_token_claims_cannot_grant_roles_or_tenant(issuer: LocalIssuer) -> None:
    """Even a token that CLAIMS admin/another tenant only gets what the database says."""
    app = await start(issuer, {"sub-alice": make_principal("nobody")})  # DB: no permissions
    async with app.router.lifespan_context(app):
        response = await post(
            app, issuer.token(roles=["admin"], tenant_id="other", **{"cognito:groups": ["admin"]})
        )
    assert response.status_code == 200
    assert response.json()["result"]["tools"] == []


@pytest.mark.anyio
async def test_protected_resource_metadata_is_public_and_points_at_the_issuer(
    issuer: LocalIssuer,
) -> None:
    app = await start(issuer, {})
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as c:
            first = await c.get("/.well-known/oauth-protected-resource")
            second = await c.get("/.well-known/oauth-protected-resource/mcp")
    for response in (first, second):
        assert response.status_code == 200
        assert response.json() == {
            "resource": f"{BASE}/mcp",
            "authorization_servers": [ISSUER],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
        }


def test_cognito_mode_requires_issuer_and_clients() -> None:
    with pytest.raises(ValueError, match="requires cognito_issuer"):
        Settings(auth_mode="cognito")
    Settings(auth_mode="cognito", cognito_issuer=ISSUER, cognito_client_ids=[CLIENT_ID])
