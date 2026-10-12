from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KNOWLEDGE_", env_file=".env", extra="ignore")

    server_database_url: str = ""  # runtime role `knowledge_svc` (read-only, RLS applies)
    internal_public_key_b64: str = ""
    internal_audience: str = "mcp-knowledge"
    # Cosine-similarity floor, chosen from measured scores for the hashing embedder
    # (relevant >= 0.18, nonsense <= 0.13 at 4096 dims). Re-measure if the embedder changes.
    min_score: float = 0.15
    host: str = "127.0.0.1"
    port: int = 9004
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*"]
