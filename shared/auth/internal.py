"""Gateway -> backend identity. Short-lived, audience-bound tokens signed with Ed25519.

The gateway holds the PRIVATE key; backends hold only the PUBLIC key, so a compromised backend
cannot mint tokens. A user's own access token is never forwarded to a backend.
"""

import base64
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from shared.auth.principal import Principal

INTERNAL_ISSUER = "secure-mcp-gateway"
ALGORITHM = "EdDSA"
DEFAULT_TTL_S = 60
LEEWAY_S = 5
SYSTEM_SUBJECT = "system:catalog"


class InternalTokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class InternalIdentity:
    """Who the gateway says is calling. Backends must trust THIS, never caller-supplied IDs."""

    subject: str
    user_id: UUID | None
    tenant_id: UUID | None
    tenant_slug: str | None
    purpose: str  # "call" (on behalf of a user) or "discovery" (system listing tools)
    tool: str | None
    request_id: str | None


internal_identity_var: ContextVar[InternalIdentity | None] = ContextVar(
    "internal_identity", default=None
)


def load_private_key_b64(value: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(base64.b64decode(value), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("internal signing key must be Ed25519")
    return key


def load_public_key_b64(value: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(base64.b64decode(value))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("internal verification key must be Ed25519")
    return key


class InternalTokenSigner:
    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        *,
        ttl_s: int = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = private_key
        self._ttl = ttl_s
        self._clock = clock

    def mint(
        self,
        *,
        audience: str,
        principal: Principal | None,
        tool: str | None = None,
        request_id: str | None = None,
    ) -> str:
        now = int(self._clock())
        claims: dict[str, object] = {
            "iss": INTERNAL_ISSUER,
            "aud": audience,
            "sub": principal.subject if principal else SYSTEM_SUBJECT,
            "iat": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
            "purpose": "call" if principal else "discovery",
        }
        if principal:
            claims.update(
                uid=str(principal.user_id),
                tid=str(principal.tenant_id),
                tenant=principal.tenant_slug,
            )
        if tool:
            claims["tool"] = tool
        if request_id and request_id != "-":
            claims["rid"] = request_id
        return jwt.encode(claims, self._key, algorithm=ALGORITHM)


class InternalTokenVerifier:
    def __init__(
        self,
        public_key: Ed25519PublicKey,
        audience: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = public_key
        self._audience = audience
        self._clock = clock

    def verify(self, token: str) -> InternalIdentity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InternalTokenError("malformed") from exc
        if header.get("alg") != ALGORITHM:
            raise InternalTokenError("alg_not_allowed")
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=INTERNAL_ISSUER,
                # Time checks are done below against OUR clock (PyJWT would use the real one).
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.InvalidAudienceError as exc:
            raise InternalTokenError("wrong_audience") from exc
        except jwt.InvalidSignatureError as exc:
            raise InternalTokenError("bad_signature") from exc
        except jwt.PyJWTError as exc:
            raise InternalTokenError("invalid") from exc

        now = self._clock()
        if claims["exp"] < now - LEEWAY_S:
            raise InternalTokenError("expired")
        if claims["iat"] > now + LEEWAY_S:
            raise InternalTokenError("issued_in_the_future")

        purpose = claims.get("purpose")
        if purpose not in ("call", "discovery"):
            raise InternalTokenError("bad_purpose")
        if purpose == "call" and not (claims.get("uid") and claims.get("tid")):
            raise InternalTokenError("missing_identity")
        return InternalIdentity(
            subject=claims["sub"],
            user_id=UUID(claims["uid"]) if claims.get("uid") else None,
            tenant_id=UUID(claims["tid"]) if claims.get("tid") else None,
            tenant_slug=claims.get("tenant"),
            purpose=purpose,
            tool=claims.get("tool"),
            request_id=claims.get("rid"),
        )
