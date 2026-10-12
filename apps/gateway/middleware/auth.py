"""Authenticates every non-public HTTP request and exposes the Principal to handlers.

Pure ASGI. Fails closed: no/invalid credentials => 401/403; resolver outage => 503. Error bodies
are generic; the precise reason goes to the log and the `on_reject` hook (audit).
"""

import json
import logging
from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from shared.auth.principal import principal_var
from shared.auth.resolver import AuthFailure, PrincipalResolver

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})
PUBLIC_PREFIXES = ("/.well-known/",)  # OAuth discovery must be readable before logging in

RejectHook = Callable[[Scope, int, str], Awaitable[None]]


def _challenge(resource_metadata_url: str, error: str | None, description: str | None) -> bytes:
    parts = [f'resource_metadata="{resource_metadata_url}"']
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    return ("Bearer " + ", ".join(parts)).encode()


async def _reject(send: Send, status: int, error: str, challenge: bytes | None) -> None:
    body = json.dumps({"error": error}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if challenge:
        headers.append((b"www-authenticate", challenge))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        resolver: PrincipalResolver,
        resource_metadata_url: str = "",
        on_reject: RejectHook | None = None,
    ) -> None:
        self.app = app
        self.resolver = resolver
        self.metadata_url = resource_metadata_url
        self.on_reject = on_reject

    async def _notify(self, scope: Scope, status: int, reason: str) -> None:
        if self.on_reject:
            try:
                await self.on_reject(scope, status, reason)
            except Exception:
                logger.exception("auth reject hook failed")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"] if scope["type"] == "http" else ""
        if scope["type"] != "http" or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        try:
            principal = await self.resolver.resolve(headers)
        except AuthFailure as failure:
            logger.warning("auth rejected status=%s reason=%s", failure.status, failure.reason)
            await self._notify(scope, failure.status, failure.reason)
            challenge = _challenge(self.metadata_url, failure.error, failure.description)
            await _reject(send, failure.status, failure.error, challenge)
            return
        except Exception:
            logger.exception("identity resolution failed")
            await _reject(send, 503, "identity_unavailable", None)
            return
        if principal is None:
            logger.warning("auth rejected status=401 reason=no_credentials path=%s", path)
            await self._notify(scope, 401, "no_credentials")
            await _reject(send, 401, "unauthorized", _challenge(self.metadata_url, None, None))
            return

        token = principal_var.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            principal_var.reset(token)
