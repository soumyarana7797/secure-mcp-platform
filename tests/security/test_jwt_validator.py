"""Attacks against token validation. Every forged or misused token must be rejected."""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.auth.jwt_validator import JwksCache, TokenError, TokenValidator
from tests.support.oidc import CLIENT_ID, ISSUER, SCOPE, LocalIssuer, StaticJwks, unsigned_token


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_validator(
    issuer: LocalIssuer, clock: Clock | None = None
) -> tuple[TokenValidator, StaticJwks]:
    source = StaticJwks(issuer)
    cache = JwksCache(source, clock=clock or Clock())
    return TokenValidator(ISSUER, frozenset({CLIENT_ID}), SCOPE, cache), source


async def reason(validator: TokenValidator, token: str) -> str:
    with pytest.raises(TokenError) as exc:
        await validator.validate(token)
    return exc.value.reason


@pytest.fixture
def issuer() -> LocalIssuer:
    return LocalIssuer()


@pytest.mark.anyio
async def test_a_valid_token_is_accepted(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    verified = await validator.validate(issuer.token())
    assert verified.subject == "sub-alice" and SCOPE in verified.scopes
    assert verified.client_id == CLIENT_ID


@pytest.mark.anyio
async def test_expired_token_is_rejected_but_small_clock_skew_is_tolerated(
    issuer: LocalIssuer,
) -> None:
    validator, _ = make_validator(issuer)
    now = int(time.time())
    assert await reason(validator, issuer.token(exp=now - 3600)) == "expired"
    await validator.validate(issuer.token(exp=now - 10))  # within the 30s leeway


@pytest.mark.anyio
async def test_token_not_yet_valid_is_rejected(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    assert await reason(validator, issuer.token(nbf=int(time.time()) + 3600)) == "not_yet_valid"


@pytest.mark.anyio
async def test_wrong_issuer_is_rejected(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    assert await reason(validator, issuer.token(iss="https://evil.test/pool")) == "bad_issuer"


@pytest.mark.anyio
async def test_token_for_another_app_client_is_rejected(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    assert await reason(validator, issuer.token(client_id="someone-elses-app")) == "bad_client"
    assert await reason(validator, issuer.token(client_id=None)) == "bad_client"


@pytest.mark.anyio
async def test_id_tokens_are_not_accepted_as_access_tokens(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    assert await reason(validator, issuer.token(token_use="id")) == "wrong_token_use"
    assert await reason(validator, issuer.token(token_use=None)) == "wrong_token_use"


@pytest.mark.anyio
async def test_missing_scope_is_a_403_style_error(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    with pytest.raises(TokenError) as exc:
        await validator.validate(issuer.token(scope="openid email"))
    assert exc.value.reason == "missing_scope"
    assert exc.value.status == 403 and exc.value.error_code == "insufficient_scope"


@pytest.mark.anyio
async def test_alg_none_forgery_is_rejected(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    forged = unsigned_token(
        {
            "sub": "sub-admin",
            "iss": ISSUER,
            "client_id": CLIENT_ID,
            "token_use": "access",
            "scope": SCOPE,
            "exp": int(time.time()) + 600,
        }
    )
    assert await reason(validator, forged) == "alg_not_allowed"


@pytest.mark.anyio
async def test_hs256_algorithm_confusion_is_rejected(issuer: LocalIssuer) -> None:
    """Attacker signs with HMAC using the issuer's PUBLIC key as the secret."""
    validator, _ = make_validator(issuer)

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": issuer.kid}).encode())
    payload = b64(
        json.dumps(
            {
                "sub": "sub-admin",
                "iss": ISSUER,
                "client_id": CLIENT_ID,
                "token_use": "access",
                "scope": SCOPE,
                "exp": int(time.time()) + 600,
            }
        ).encode()
    )
    signature = hmac.new(
        issuer.public_pem(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    assert await reason(validator, f"{header}.{payload}.{b64(signature)}") == "alg_not_allowed"


@pytest.mark.anyio
async def test_token_signed_by_an_attacker_key_with_a_valid_kid_is_rejected(
    issuer: LocalIssuer,
) -> None:
    validator, _ = make_validator(issuer)
    attacker = LocalIssuer(kid=issuer.kid)  # same kid, different private key
    assert await reason(validator, attacker.token()) == "bad_signature"


@pytest.mark.anyio
async def test_tampering_with_the_payload_after_signing_is_rejected(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    header, payload, signature = issuer.token(sub="sub-bob").split(".")
    padded = payload + "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    claims["sub"] = "sub-admin"
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    assert await reason(validator, f"{header}.{forged_payload}.{signature}") == "bad_signature"


@pytest.mark.anyio
@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "x" * 20000, "..", "Bearer abc"])
async def test_malformed_tokens_are_rejected(issuer: LocalIssuer, garbage: str) -> None:
    validator, _ = make_validator(issuer)
    assert await reason(validator, garbage) in {"malformed", "invalid"}


@pytest.mark.anyio
async def test_token_without_kid_or_with_unknown_kid_is_rejected(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    no_kid = jwt.encode(
        {"sub": "s", "iss": ISSUER, "exp": int(time.time()) + 60}, issuer.private, algorithm="RS256"
    )
    assert await reason(validator, no_kid) == "missing_kid"
    assert await reason(validator, issuer.token(headers={"kid": "nope"})) == "unknown_kid"


@pytest.mark.anyio
async def test_token_missing_required_claims_is_rejected(issuer: LocalIssuer) -> None:
    validator, _ = make_validator(issuer)
    assert await reason(validator, issuer.token(exp=None)) == "invalid"
    assert await reason(validator, issuer.token(sub=None)) == "invalid"


# ---------------------------------------------------------------- key cache behaviour


@pytest.mark.anyio
async def test_keys_are_cached_between_requests(issuer: LocalIssuer) -> None:
    validator, source = make_validator(issuer)
    for _ in range(5):
        await validator.validate(issuer.token())
    assert source.fetches == 1


@pytest.mark.anyio
async def test_key_rotation_is_picked_up_after_the_refetch_interval() -> None:
    old, new = LocalIssuer("key-old"), LocalIssuer("key-new")
    clock = Clock()
    validator, source = make_validator(old, clock)
    await validator.validate(old.token())  # primes the cache
    source.document = {"keys": old.jwks()["keys"] + new.jwks()["keys"]}  # issuer rotates
    clock.now += 31
    assert (await validator.validate(new.token())).subject == "sub-alice"
    assert source.fetches == 2


@pytest.mark.anyio
async def test_unknown_kids_cannot_be_used_to_hammer_the_identity_provider(
    issuer: LocalIssuer,
) -> None:
    validator, source = make_validator(issuer)
    await validator.validate(issuer.token())
    for i in range(50):
        assert (
            await reason(validator, issuer.token(headers={"kid": f"random-{i}"})) == "unknown_kid"
        )
    assert source.fetches == 1  # throttled: no refetch inside the interval


@pytest.mark.anyio
async def test_identity_provider_outage_rejects_unknown_keys_without_crashing() -> None:
    class Down:
        async def fetch(self):  # noqa: ANN202
            raise ConnectionError("issuer unreachable")

    issuer = LocalIssuer()
    validator = TokenValidator(ISSUER, frozenset({CLIENT_ID}), SCOPE, JwksCache(Down()))
    assert await reason(validator, issuer.token()) == "unknown_kid"


@pytest.mark.anyio
async def test_a_key_that_is_not_in_the_jwks_never_validates() -> None:
    validator, _ = make_validator(LocalIssuer("key-1"))
    stranger = LocalIssuer("key-1")
    stranger.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await reason(validator, stranger.token()) == "bad_signature"
