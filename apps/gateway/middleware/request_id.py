"""Assigns every HTTP request an ID, exposes it in logs and echoes it in the response.

Written as pure ASGI (not BaseHTTPMiddleware) so streaming responses are not buffered.
"""

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shared.observability.context import request_id_var

HEADER = b"x-request-id"
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = dict(scope["headers"]).get(HEADER, b"").decode("latin-1")
        # Never trust a caller-supplied ID blindly: it ends up in logs.
        request_id = supplied if _SAFE.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((HEADER, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
