"""Forwards tool calls: look up, authorize, validate, forward with a timeout, map errors."""

import asyncio
import logging
import time
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp import MCPError
from mcp_types import INVALID_PARAMS, CallToolResult, TextContent

from apps.gateway.router.catalog import CatalogTool, ToolCatalog
from apps.gateway.router.connector import BackendAuth, NoBackendAuth
from apps.gateway.router.resilience import GuardRegistry
from shared.audit.logger import AuditEvent, AuditSink, NullAudit
from shared.audit.redact import args_hash, redact_args
from shared.auth.principal import Principal, principal_var
from shared.observability.context import request_id_var
from shared.resilience.bulkhead import BulkheadFull
from shared.resilience.retry import retry

logger = logging.getLogger(__name__)

MAX_REPORTED_ERRORS = 5
# Outcomes where the authorization decision was a refusal (as opposed to a later failure).
DENIED_OUTCOMES = frozenset({"denied", "unknown_tool"})


def error_result(message: str) -> CallToolResult:
    """A tool-level error: the LLM can read it and adjust, unlike a protocol error."""
    return CallToolResult(content=[TextContent(text=message)], is_error=True)


def _branch_messages(err: ValidationError) -> list[str]:
    """Messages for one error; for anyOf/oneOf, list the branches (minus the optional-null one)."""
    if not err.context:
        return [err.message]
    messages = [m for sub in err.context for m in _branch_messages(sub)]
    meaningful = [m for m in messages if not m.endswith("is not of type 'null'")]
    return meaningful or messages


def _validation_messages(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
    out = []
    for err in errors[:MAX_REPORTED_ERRORS]:
        where = ".".join(str(p) for p in err.absolute_path) or "(arguments)"
        out.append(f"{where}: " + " / ".join(_branch_messages(err)))
    return out


class ToolProxy:
    def __init__(
        self,
        catalog: ToolCatalog,
        backend_auth: BackendAuth | None = None,
        audit: AuditSink | None = None,
        guards: GuardRegistry | None = None,
    ) -> None:
        self._catalog = catalog
        self._guards = guards or GuardRegistry()
        self._retry_attempts = self._guards.config.retry_attempts
        self._retry_base_delay_s = self._guards.config.retry_base_delay_s
        self._auth = backend_auth or NoBackendAuth()
        self._audit = audit or NullAudit()

    async def _invoke(
        self, entry: CatalogTool, args: dict[str, Any], principal: Principal | None
    ) -> CallToolResult:
        """One logical call. Read-only tools are retried on connection-level failures; every try
        gets a fresh signed token and all tries share the caller's overall deadline."""

        async def once() -> CallToolResult:
            headers = self._auth.headers(entry.spec, principal, entry.original_name)
            async with entry.connector.connect(headers) as client:
                return await client.call_tool(entry.original_name, args)

        attempts = self._retry_attempts if entry.retryable else 1
        return await retry(once, attempts=attempts, base_delay_s=self._retry_base_delay_s)

    async def call_tool(
        self,
        public_name: str,
        arguments: dict[str, Any] | None,
        permissions: frozenset[str],
    ) -> CallToolResult:
        started = time.perf_counter()
        outcome = "ok"
        principal = principal_var.get()
        entry = self._catalog.get(public_name)
        try:
            if entry is None:
                outcome = "unknown_tool"
                raise MCPError(INVALID_PARAMS, f"Unknown tool: {public_name}")
            if entry.permission not in permissions:
                # Same response as an unknown tool, so callers cannot probe which tools exist.
                outcome = "denied"
                raise MCPError(INVALID_PARAMS, f"Unknown tool: {public_name}")

            args = arguments or {}
            problems = _validation_messages(entry.tool.input_schema, args)
            if problems:
                outcome = "invalid_args"
                return error_result(f"Invalid arguments for {public_name}: " + "; ".join(problems))

            guard = self._guards.guard(entry.backend)
            if not guard.breaker.allow():
                outcome = "circuit_open"  # fail fast: do not queue behind a dead backend
                return error_result(
                    f"Backend '{entry.backend}' is temporarily unavailable. Try again shortly."
                )
            try:
                async with guard.bulkhead.slot(), asyncio.timeout(entry.timeout_s):
                    result = await self._invoke(entry, args, principal)
            except asyncio.CancelledError:
                guard.breaker.record_neutral()  # the caller went away: settle any trial slot
                outcome = "cancelled"
                raise
            except BulkheadFull:
                guard.breaker.record_neutral()  # overload is not evidence the backend is sick
                outcome = "overloaded"
                return error_result(f"Backend '{entry.backend}' is busy. Try again shortly.")
            except TimeoutError:
                guard.breaker.record_failure()
                outcome = "timeout"
                return error_result(f"{public_name} timed out after {entry.timeout_s:g}s")
            except Exception:
                guard.breaker.record_failure()
                outcome = "backend_error"
                logger.exception("backend call failed tool=%s", public_name)
                return error_result(f"Backend '{entry.backend}' is unavailable")
            # The backend answered. A tool-level error (bad input it rejected, a missing record)
            # is NOT counted against it: otherwise one tenant could open the circuit for all.
            guard.breaker.record_success()
            if result.is_error:
                outcome = "tool_error"
            return result
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "tool_call tool=%s outcome=%s tenant=%s user=%s ms=%.1f",
                public_name,
                outcome,
                principal.tenant_slug if principal else "-",
                principal.user_id if principal else "-",
                elapsed_ms,
            )
            try:
                self._audit.emit(
                    AuditEvent(
                        event_type="tool_call",
                        decision="deny" if outcome in DENIED_OUTCOMES else "allow",
                        outcome=outcome,
                        request_id=request_id_var.get(),
                        tenant_id=principal.tenant_id if principal else None,
                        user_id=principal.user_id if principal else None,
                        subject=principal.subject if principal else None,
                        tool=public_name,
                        latency_ms=round(elapsed_ms, 2),
                        args_hash=args_hash(arguments or {}),
                        redacted_args=redact_args(
                            arguments, entry.tool.input_schema if entry else None
                        ),
                    )
                )
            except Exception:  # auditing must never change what the caller receives
                logger.exception("failed to build audit event tool=%s", public_name)
