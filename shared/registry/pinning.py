"""Tool pinning: hash a tool's security-relevant definition so drift can be detected."""

import hashlib
import json
from typing import Any

from mcp_types import Tool
from pydantic import BaseModel, Field


def definition_hash(description: str | None, input_schema: dict[str, Any]) -> str:
    """Stable hash of what an LLM sees and what constrains its input."""
    canonical = json.dumps(
        {"description": description or "", "input_schema": input_schema},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class PinnedTool(BaseModel, frozen=True):
    """An admin-approved tool definition. This, not the live backend, is what clients are shown."""

    name: str
    description: str
    input_schema: dict[str, Any]
    schema_hash: str
    permission: str = Field(pattern=r"^[a-z_]+:[a-z_]+$")
    timeout_s: float | None = None
    annotations: dict[str, Any] | None = None
    enabled: bool = True


def pin_tool(tool: Tool, permission: str, timeout_s: float | None = None) -> PinnedTool:
    annotations = tool.annotations.model_dump(exclude_none=True) if tool.annotations else None
    description = tool.description or ""
    return PinnedTool(
        name=tool.name,
        description=description,
        input_schema=tool.input_schema,
        schema_hash=definition_hash(description, tool.input_schema),
        permission=permission,
        timeout_s=timeout_s,
        annotations=annotations,
    )
