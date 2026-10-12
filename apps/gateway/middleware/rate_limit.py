"""Per-tenant and per-user rate limiting for authenticated requests.

Runs after AuthMiddleware (it needs the Principal). A rejected request gets 429 + Retry-After and
is audited. Health and discovery endpoints are public and never limited.
"""

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from apps.gateway.middleware.auth import PUBLIC_PATHS, PUBLIC_PREFIXES
from shared.audit.logger import AuditEvent, AuditSink
from shared.audit.throttle import AuditThrottle
from shared.auth.principal import principal_var
from shared.observability.context import request_id_var
from shared.rate_limit.limiter import RateLimiter
from shared.rate_limit.policies import PolicyStore


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        policies: PolicyStore,
        audit: AuditSink,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.policies = policies
        self.audit = audit
        self.throttle = AuditThrottle(interval_s=10.0)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"] if scope["type"] == "http" else ""
        principal = principal_var.get()
        if (
            scope["type"] != "http"
            or path in PUBLIC_PATHS
            or path.startswith(PUBLIC_PREFIXES)
            or principal is None
        ):
            await self.app(scope, receive, send)
            return

        # Tenant first: it protects everyone else from one noisy customer.
        for level, key, owner in (
            ("tenant", f"t:{principal.tenant_id}", principal.tenant_id),
            ("user", f"u:{principal.user_id}", principal.tenant_id),
        ):
            limit = await self.policies.limit_for(level, owner)
            decision = await self.limiter.check(key, limit)
            if not decision.allowed:
                # A flood is one audit record with a count, not one record per rejected request.
                record, suppressed = self.throttle.admit(f"{level}:{key}")
                if record:
                    self.audit.emit(
                        AuditEvent(
                            event_type="rate_limited",
                            decision="deny",
                            outcome=f"{level}_limit",
                            request_id=request_id_var.get(),
                            tenant_id=principal.tenant_id,
                            user_id=principal.user_id,
                            subject=principal.subject,
                            redacted_args={"suppressed_since_last": suppressed},
                        )
                    )
                body = json.dumps({"error": "rate_limited"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"retry-after", decision.retry_after_header.encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
