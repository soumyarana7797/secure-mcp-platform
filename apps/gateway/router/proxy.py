"""Forwards tool calls: look up, authorize, validate, forward with a timeout, map errors."""

import asyncio
import logging
import time
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp import MCPError
from mcp_types import INVALID_PARAMS, CallToolResult, TextContent

from apps.gateway.router.catalog import ToolCatalog
from shared.auth.principal import principal_var

logger = logging.getLogger(__name__)

MAX_REPORTED_ERRORS = 5


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
    def __init__(self, catalog: ToolCatalog) -> None:
        self._catalog = catalog

    async def call_tool(
        self,
        public_name: str,
        arguments: dict[str, Any] | None,
        permissions: frozenset[str],
    ) -> CallToolResult:
        started = time.perf_counter()
        outcome = "ok"
        principal = principal_var.get()
        try:
            entry = self._catalog.get(public_name)
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

            try:
                async with asyncio.timeout(entry.timeout_s):
                    async with entry.connector.connect() as client:
                        result = await client.call_tool(entry.original_name, args)
            except TimeoutError:
                outcome = "timeout"
                return error_result(f"{public_name} timed out after {entry.timeout_s:g}s")
            except Exception:
                outcome = "backend_error"
                logger.exception("backend call failed tool=%s", public_name)
                return error_result(f"Backend '{entry.backend}' is unavailable")
            if result.is_error:
                outcome = "tool_error"
            return result
        finally:
            logger.info(
                "tool_call tool=%s outcome=%s tenant=%s user=%s ms=%.1f",
                public_name,
                outcome,
                principal.tenant_slug if principal else "-",
                principal.user_id if principal else "-",
                (time.perf_counter() - started) * 1000,
            )
