"""Validates OAuth access tokens issued by Amazon Cognito (or any RS256 OIDC issuer).

We never merely decode a token: signature, issuer, expiry, token type, client and scope are all
verified. Failures carry a precise internal `reason` (for logs/audit) and a generic public
message, so a caller learns nothing about *why* a forged token failed.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

ALLOWED_ALGORITHMS = ("RS256",)  # pinned: never trust the token's own claim about how to verify it
MAX_TOKEN_BYTES = 8192
CLOCK_LEEWAY_S = 30


class TokenError(Exception):
    """A token that must be rejected."""

    def __init__(self, reason: str, *, insufficient_scope: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason  # internal: logs and audit only
        self.insufficient_scope = insufficient_scope

    @property
    def error_code(self) -> str:  # RFC 6750
        return "insufficient_scope" if self.insufficient_scope else "invalid_token"

    @property
    def status(self) -> int:
        return 403 if self.insufficient_scope else 401


@dataclass(frozen=True)
class VerifiedToken:
    subject: str
    client_id: str
    scopes: frozenset[str]
    jti: str | None


class JwksSource(Protocol):
    async def fetch(self) -> dict[str, Any]:
        """Return the issuer's JWKS document: {"keys": [...]}."""
        ...


class HttpJwksSource:
    def __init__(self, url: str, timeout_s: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout_s

    async def fetch(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            return response.json()


@dataclass
class JwksCache:
    source: JwksSource
    ttl_s: float = 3600.0
    min_refetch_interval_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _keys: dict[str, Any] = field(default_factory=dict)
    _fetched_at: float | None = None

    async def key_for(self, kid: str) -> Any | None:
        now = self.clock()
        stale = self._fetched_at is None or now - self._fetched_at > self.ttl_s
        if kid not in self._keys and (stale or self._can_refetch(now)):
            await self._refresh(now)
        return self._keys.get(kid)

    def _can_refetch(self, now: float) -> bool:
        # An unknown kid may mean the issuer rotated keys, but also that someone is probing us.
        return self._fetched_at is None or now - self._fetched_at >= self.min_refetch_interval_s

    async def _refresh(self, now: float) -> None:
        try:
            document = await self.source.fetch()
        except Exception:
            logger.exception("JWKS fetch failed")
            self._fetched_at = now  # back off too when the issuer is down
            return
        keys: dict[str, Any] = {}
        for jwk in document.get("keys", []):
            if jwk.get("kty") == "RSA" and jwk.get("kid") and jwk.get("use", "sig") == "sig":
                keys[jwk["kid"]] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        self._keys = keys
        self._fetched_at = now


@dataclass
class TokenValidator:
    issuer: str
    allowed_client_ids: frozenset[str]
    required_scope: str
    jwks: JwksCache

    async def validate(self, token: str) -> VerifiedToken:
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise TokenError("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenError("malformed") from exc

        # 1. Pin the algorithm BEFORE choosing a key (blocks alg=none and HS256/RS256 confusion).
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise TokenError("alg_not_allowed")
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise TokenError("missing_kid")

        key = await self.jwks.key_for(kid)
        if key is None:
            raise TokenError("unknown_kid")

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                issuer=self.issuer,
                leeway=CLOCK_LEEWAY_S,
                options={"require": ["exp", "iss", "sub"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError("bad_issuer") from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenError("bad_signature") from exc
        except jwt.ImmatureSignatureError as exc:
            raise TokenError("not_yet_valid") from exc
        except jwt.PyJWTError as exc:
            raise TokenError("invalid") from exc

        if claims.get("token_use") != "access":  # Cognito: ID tokens are not API credentials
            raise TokenError("wrong_token_use")
        # Cognito access tokens carry `client_id`, not `aud`.
        client_id = claims.get("client_id")
        if client_id not in self.allowed_client_ids:
            raise TokenError("bad_client")
        scopes = frozenset(str(claims.get("scope", "")).split())
        if self.required_scope not in scopes:
            raise TokenError("missing_scope", insufficient_scope=True)
        return VerifiedToken(claims["sub"], client_id, scopes, claims.get("jti"))
