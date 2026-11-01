from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORBIT_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    database_url: str = "sqlite:///data/orbit.sqlite"
    rentcast_api_key: SecretStr | None = Field(default=None, validation_alias="RENTCAST_API_KEY")
    rentcast_monthly_limit: int = Field(default=45, ge=0, le=50)
    timezone: str | None = None
    scheduler_enabled: bool = True
    backup_keep_days: int = Field(default=30, ge=1)
    allowed_hosts: str = "127.0.0.1,localhost"
    # The address browsers use, when an HTTPS proxy hides it from Orbit (Tailscale under Docker).
    public_url: str | None = None
    webhook_port: int | None = Field(default=None, ge=1, le=65535)
    webhook_bind: str = "127.0.0.1"
    plaid_client_id: SecretStr = Field(default=SecretStr(""), validation_alias="PLAID_CLIENT_ID")
    plaid_secret: SecretStr = Field(default=SecretStr(""), validation_alias="PLAID_SECRET")
    plaid_env: Literal["sandbox", "production"] = Field(
        default="sandbox", validation_alias="PLAID_ENV"
    )
    plaid_webhook_url: str | None = Field(default=None, validation_alias="PLAID_WEBHOOK_URL")
    fernet_key: SecretStr = Field(default=SecretStr(""), validation_alias="FERNET_KEY")

    @property
    def plaid_webhook_usable(self) -> bool:
        try:
            url = urlsplit(self.plaid_webhook_url or "")
            return bool(
                self.webhook_port is not None
                and url.scheme == "https"
                and url.hostname
                and url.path == "/plaid/webhook"
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
        except ValueError:
            return False

    @property
    def plaid_configured(self) -> bool:
        return bool(
            self.plaid_client_id.get_secret_value() and self.plaid_secret.get_secret_value()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
