"""Settings read from the environment and from `.env`.

Secrets are held as `SecretStr`, so they never appear in logs or reprs.
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://briefing:briefing@127.0.0.1:5432/briefing"

    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    score_model: str = "claude-haiku-5-5"

    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_use_ssl: bool = False
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    mail_from: str = "Briefing Engine <briefing@briefing.test>"

    # Where `briefing web` listens; the email links here so items can be marked.
    web_base_url: str = "http://127.0.0.1:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
