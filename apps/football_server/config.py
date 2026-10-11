from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOOTBALL_", env_file=".env", extra="ignore")

    data_api_url: str = "http://127.0.0.1:9001"
    data_api_token: str  # required: from env (.env locally, Secrets Manager in AWS)
    provider_timeout_s: float = 3.0
    max_fixture_range_days: int = 14
    host: str = "127.0.0.1"
    port: int = 9002
    # Hostnames allowed in the HTTP Host header (DNS-rebinding protection). Add service names
    # such as "football-server:9002" when running in containers.
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*"]
