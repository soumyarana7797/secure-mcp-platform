from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    environment: Literal["local", "aws"] = "local"
    # "disabled" rejects every MCP request (fail closed). "dev_header" trusts X-Dev-Sub and is
    # only allowed locally. Phase 4 adds "cognito".
    auth_mode: Literal["disabled", "dev_header"] = "disabled"
    database_url: str = ""  # runtime role (gateway_app); required unless a Database is injected

    host: str = "127.0.0.1"
    port: int = 9000
    # Allowed HTTP Host headers (DNS-rebinding protection). Add the public name behind the ALB.
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*"]
    catalog_refresh_s: float = 30.0
    discovery_timeout_s: float = 5.0

    @model_validator(mode="after")
    def _no_dev_auth_outside_local(self) -> Self:
        if self.auth_mode == "dev_header" and self.environment != "local":
            raise ValueError("auth_mode=dev_header is only allowed when environment=local")
        return self
