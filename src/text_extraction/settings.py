"""Environment-driven settings. Every setting is an env var with a documented default."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, TypeAdapter, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiKey(BaseModel):
    """One API key. Only the SHA-256 of the key is stored, never the key itself."""

    name: str
    role: Literal["user", "admin"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Lowercase hex SHA-256 of the key.")
    rate_limit_per_minute: int | None = None


_API_KEY_LIST = TypeAdapter(list[ApiKey])


class Settings(BaseSettings):
    """Service settings. Secrets: ``API_KEYS`` and ``API_KEYS_FILE`` content."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    public_host: str = "0.0.0.0"  # noqa: S104 - bind inside the container
    public_port: int = 8080
    internal_host: str = "0.0.0.0"  # noqa: S104 - bind inside the container
    internal_port: int = 9090
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    auth_mode: Literal["api_key", "none"] = "api_key"
    api_keys: list[ApiKey] = []  # secret; JSON list in env API_KEYS
    api_keys_file: Path | None = None  # JSON file with the same shape, merged with API_KEYS
    valkey_url: str = "redis://localhost:6379/0"  # secret when it carries a password
    rate_limit_per_minute: int = 60
    rate_limit_enabled: bool = True

    @model_validator(mode="after")
    def _merge_and_validate_api_keys(self) -> Self:
        keys = list(self.api_keys)
        if self.api_keys_file is not None:
            keys.extend(_API_KEY_LIST.validate_python(json.loads(self.api_keys_file.read_text())))

        seen: set[str] = set()
        for key in keys:
            if key.name in seen:
                raise ValueError(f"duplicate API key name: {key.name}")
            seen.add(key.name)

        if self.auth_mode == "api_key" and not keys:
            raise ValueError("AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE")

        self.api_keys = keys
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
