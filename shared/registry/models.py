"""Backend registry: which MCP servers the gateway may route to."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class BackendSpec(BaseModel, frozen=True):
    # Names become the tool-name prefix ("football.get_fixtures"), so keep them dot-free.
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    url: str
    timeout_s: float = Field(5.0, gt=0, le=120)
    tool_timeouts: dict[str, float] = {}

    def timeout_for(self, tool_name: str) -> float:
        return self.tool_timeouts.get(tool_name, self.timeout_s)


class Registry(BaseModel):
    backends: list[BackendSpec]

    def model_post_init(self, _: object) -> None:
        names = [b.name for b in self.backends]
        if len(names) != len(set(names)):
            raise ValueError("duplicate backend names in registry")

    @classmethod
    def from_toml(cls, path: str | Path) -> "Registry":
        with Path(path).open("rb") as f:
            return cls.model_validate(tomllib.load(f))
