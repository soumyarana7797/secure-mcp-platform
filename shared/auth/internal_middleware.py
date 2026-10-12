"""Backend-side ASGI middleware: only requests carrying a valid gateway token get through."""

import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from shared.auth.internal import InternalTokenError, InternalTokenVerifier, internal_identity_var

logger = logging.getLogger(__name__)


class InternalAuthMiddleware:
    def __init__(self, app: ASGIApp, verifier: InternalTokenVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        try:
            if scheme.lower() != "bearer" or not token:
                raise InternalTokenError("no_credentials")
            identity = self.verifier.verify(token.strip())
        except InternalTokenError as exc:
            logger.warning("backend rejected request reason=%s", exc.reason)
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        ctx = internal_identity_var.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            internal_identity_var.reset(ctx)
