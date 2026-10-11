from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    registry_path: str = "config/registry.toml"
    host: str = "127.0.0.1"
    port: int = 9000
    # Allowed HTTP Host headers (DNS-rebinding protection). Add the public name behind the ALB.
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*"]
    catalog_refresh_s: float = 30.0
    discovery_timeout_s: float = 5.0
