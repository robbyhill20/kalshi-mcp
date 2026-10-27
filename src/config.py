from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid", case_sensitive=False)

    kalshi_api_key_id: SecretStr
    kalshi_private_key: SecretStr
    kalshi_env: Literal["production", "demo"] = "production"
    dry_run: bool = True
    trading_enabled: bool = False
    require_trade_approval: bool = True
    database_path: Path = Path("data/kalshi.sqlite3")
    public_base_url: str = Field(default="http://localhost:8000",
                                validation_alias=AliasChoices("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL", "public_base_url"))
    admin_token: SecretStr = Field(min_length=32)
    mcp_read_token: SecretStr = Field(min_length=32)
    mcp_director_token: SecretStr = Field(min_length=32)
    # Only the owner can configure these; no tool can modify them.
    max_contracts: int = Field(default=100, ge=1, le=10000)
    max_order_cost_cents: int = Field(default=10000, ge=1, le=1000000)
    max_daily_order_cost_cents: int = Field(default=10000, ge=1, le=100000000)
    fee_buffer_cents_per_contract: int = Field(default=0, ge=0, le=100)
    snapshot_max_age_seconds: int = Field(default=5, ge=1, le=30)
    api_timeout_seconds: int = Field(default=10, ge=1, le=30)
    oauth_redirect_hosts: str = "grok.com,accounts.x.ai,console.x.ai"

    @model_validator(mode="after")
    def safe_config(self):
        url = urlsplit(self.public_base_url)
        if (url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
        )) or url.path not in {"", "/"} or url.query or url.fragment or url.username:
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin (HTTP loopback for testing only)")
        self.public_base_url = self.public_base_url.rstrip("/")
        secrets = [self.admin_token, self.mcp_read_token, self.mcp_director_token]
        if len({s.get_secret_value() for s in secrets}) != 3:
            raise ValueError("Owner, reader and director tokens must be different")
        if not self.kalshi_api_key_id.get_secret_value() or not self.kalshi_private_key.get_secret_value():
            raise ValueError("Kalshi credentials are required even for dry run")
        return self

    @property
    def api_origin(self):
        return {"production": "https://external-api.kalshi.com",
                "demo": "https://external-api.demo.kalshi.co"}[self.kalshi_env]

    @property
    def mcp_url(self):
        return self.public_base_url + "/mcp"


def load_settings():
    try:
        return Settings()
    except ValidationError:
        # Pydantic validation tracebacks may otherwise include raw environment values.
        raise ValueError("Invalid server configuration. Check required secrets, HTTPS URL and owner limits.") from None
