"""The admin-authored manifest (config/registry.toml): intended backends and tool permissions."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class ToolManifest(BaseModel):
    permission: str = Field(pattern=r"^[a-z_]+:[a-z_]+$")
    timeout_s: float | None = Field(None, gt=0, le=120)


class BackendManifest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    url: str
    audience: str
    timeout_s: float = Field(5.0, gt=0, le=120)
    tools: dict[str, ToolManifest]


class Manifest(BaseModel):
    backends: list[BackendManifest]

    def model_post_init(self, _: object) -> None:
        names = [b.name for b in self.backends]
        if len(names) != len(set(names)):
            raise ValueError("duplicate backend names in manifest")

    @classmethod
    def from_toml(cls, path: str | Path) -> "Manifest":
        with Path(path).open("rb") as f:
            return cls.model_validate(tomllib.load(f))
