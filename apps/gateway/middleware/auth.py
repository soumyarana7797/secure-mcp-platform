"""Authenticates every non-public HTTP request and exposes the Principal to handlers.

Pure ASGI. Fails closed: no principal => 401; resolver infrastructure error => 503.
"""

import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from shared.auth.principal import principal_var
from shared.auth.resolver import PrincipalResolver

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})


async def _reject(send: Send, status: int, error: str, www_authenticate: bool) -> None:
    body = json.dumps({"error": error}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if www_authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    def __init__(self, app: ASGIApp, resolver: PrincipalResolver) -> None:
        self.app = app
        self.resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        try:
            principal = await self.resolver.resolve(headers)
        except Exception:
            logger.exception("identity resolution failed")
            await _reject(send, 503, "identity_unavailable", www_authenticate=False)
            return
        if principal is None:
            logger.warning("rejected unauthenticated request path=%s", scope["path"])
            await _reject(send, 401, "unauthorized", www_authenticate=True)
            return

        token = principal_var.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            principal_var.reset(token)
