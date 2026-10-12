"""Backend registry: which MCP servers the gateway may route to."""

from pydantic import BaseModel, Field


class BackendSpec(BaseModel, frozen=True):
    # Names become the tool-name prefix ("football.get_fixtures"), so keep them dot-free.
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    url: str
    audience: str = ""  # `aud` of internal tokens minted for this backend
    timeout_s: float = Field(5.0, gt=0, le=120)
    tool_timeouts: dict[str, float] = {}

    def timeout_for(self, tool_name: str) -> float:
        return self.tool_timeouts.get(tool_name, self.timeout_s)
