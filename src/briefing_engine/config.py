"""Settings read from the environment and from `.env`.

Secrets are held as `SecretStr`, so they never appear in logs or reprs.
"""

from functools import lru_cache
from typing import Literal, Self
from urllib.parse import quote

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://briefing:briefing@127.0.0.1:5432/briefing"
    # On AWS the database URL is built from parts instead: RDS keeps the password in
    # Secrets Manager and ECS hands it over as DB_PASSWORD, so no file or Terraform state
    # ever holds a full connection string. When DB_HOST is set, these win over DATABASE_URL.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "briefing"
    db_user: str = "briefing"
    db_password: SecretStr | None = None

    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    score_model: str = "claude-haiku-5-5"
    # The write-up reads only the items placed in the briefing, so a stronger model is cheap
    # here. Leave it empty to send the ranked list with the scorer's reasons instead.
    writeup_model: str = "claude-sonnet-5-5"

    # "smtp" (Mailpit locally, or any SMTP server) or "ses" (Amazon SES through its API,
    # with the task role's credentials: no SMTP password to store).
    mail_transport: Literal["smtp", "ses"] = "smtp"
    aws_region: str | None = None  # for SES; boto3 also reads AWS_REGION itself

    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_use_ssl: bool = False
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    mail_from: str = "Briefing Engine <briefing@briefing.test>"

    # Where `briefing web` listens; the email links here so items can be marked.
    web_base_url: str = "http://127.0.0.1:8000"

    # `briefing api`. In a container, API_HOST=0.0.0.0 (the port is still published only
    # on 127.0.0.1 by compose.yaml).
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    # Signs pagination cursors. Without it a random key is made at start-up, so cursors
    # stop working when the API restarts.
    cursor_hmac_key: SecretStr | None = None
    log_format: str = "text"  # or "json", for the API and worker in a container

    @model_validator(mode="after")
    def _database_url_from_parts(self) -> Self:
        if self.db_host:
            if self.db_password is None:
                raise ValueError("DB_HOST is set but DB_PASSWORD is not")
            password = quote(self.db_password.get_secret_value(), safe="")
            self.database_url = (
                f"postgresql+psycopg://{quote(self.db_user, safe='')}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}?sslmode=require"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
