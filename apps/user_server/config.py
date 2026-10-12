from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="USER_", env_file=".env", extra="ignore")

    server_database_url: str = ""  # runtime role `user_svc` (least privilege, RLS applies)
    # Ed25519 public key (PEM, base64) of the gateway. Without it the server refuses to start.
    internal_public_key_b64: str = ""
    internal_audience: str = "mcp-user"
    host: str = "127.0.0.1"
    port: int = 9003
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*"]
