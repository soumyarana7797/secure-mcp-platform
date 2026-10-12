"""A local OIDC issuer for tests: real RSA keys, real signatures, and forgeable attacks."""

import base64
import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://issuer.test/pool-1"
CLIENT_ID = "client-abc"
SCOPE = "mcp-gateway/tools"


class LocalIssuer:
    def __init__(self, kid: str = "key-1") -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def token(self, *, headers: dict[str, Any] | None = None, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": "sub-alice",
            "iss": ISSUER,
            "client_id": CLIENT_ID,
            "token_use": "access",
            "scope": f"openid {SCOPE}",
            "iat": now,
            "exp": now + 600,
            "jti": "jti-1",
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}  # None removes a claim
        return jwt.encode(
            claims, self.private, algorithm="RS256", headers={"kid": self.kid, **(headers or {})}
        )

    def public_pem(self) -> bytes:
        return self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


def unsigned_token(claims: dict[str, Any], kid: str = "key-1") -> str:
    """alg=none: a classic forgery."""

    def b64(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': kid})}.{b64(claims)}."


class StaticJwks:
    """JwksSource backed by a mutable document; counts fetches."""

    def __init__(self, issuer: LocalIssuer) -> None:
        self.document = issuer.jwks()
        self.fetches = 0

    async def fetch(self) -> dict[str, Any]:
        self.fetches += 1
        return self.document
