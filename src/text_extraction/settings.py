"""Environment-driven settings. Every setting is an env var with a documented default."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError


class ApiKey(BaseModel):
    """One API key. Only the SHA-256 of the key is stored, never the key itself."""

    model_config = ConfigDict(hide_input_in_errors=True)

    name: str
    role: Literal["user", "admin"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Lowercase hex SHA-256 of the key.")
    rate_limit_per_minute: int | None = None


_API_KEY_LIST = TypeAdapter(list[ApiKey])

_API_KEYS_HINT = "API_KEYS must be a JSON list of {name, role, sha256} objects"


class ConfigError(Exception):
    """The environment configuration is invalid. The message never contains input values."""


def _describe(error: ValidationError) -> str:
    """Join location and message of each error; input values are left out on purpose."""
    parts = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in item["loc"])
        parts.append(f"{location}: {item['msg']}" if location else item["msg"])
    return "; ".join(parts)


def _read_api_keys_file(path: Path) -> list[ApiKey]:
    try:
        raw = json.loads(path.read_text())
        return _API_KEY_LIST.validate_python(raw)
    except OSError as error:
        raise ValueError(f"API_KEYS_FILE {path}: cannot be read ({error.strerror})") from None
    except json.JSONDecodeError as error:
        raise ValueError(f"API_KEYS_FILE {path}: invalid JSON ({error.msg})") from None
    except ValidationError as error:
        raise ValueError(
            f"API_KEYS_FILE {path}: expected a JSON list of {{name, role, sha256}} objects "
            f"({_describe(error)})"
        ) from None


class Settings(BaseSettings):
    """Service settings. Secrets: ``API_KEYS`` and ``API_KEYS_FILE`` content."""

    model_config = SettingsConfigDict(
        case_sensitive=False, extra="ignore", hide_input_in_errors=True
    )

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
            keys.extend(_read_api_keys_file(self.api_keys_file))

        seen: set[str] = set()
        for key in keys:
            if key.name in seen:
                raise ValueError(f"duplicate API key name: {key.name}")
            seen.add(key.name)

        if self.auth_mode == "api_key" and not keys:
            raise ValueError("AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE")

        self.api_keys = keys
        return self


def load_settings() -> Settings:
    """Build ``Settings`` from the environment; any problem becomes a ``ConfigError``."""
    try:
        return Settings()
    except SettingsError:
        # The source error text can quote the raw value, so only the hint is reported.
        raise ConfigError(f"Invalid configuration: {_API_KEYS_HINT}") from None
    except ValidationError as error:
        hint = f" ({_API_KEYS_HINT})" if _mentions_api_keys(error) else ""
        raise ConfigError(f"Invalid configuration: {_describe(error)}{hint}") from None
    except OSError as error:
        raise ConfigError(f"Invalid configuration: cannot read file ({error.strerror})") from None


def _mentions_api_keys(error: ValidationError) -> bool:
    return any(item["loc"][:1] == ("api_keys",) for item in error.errors(include_input=False))


@lru_cache
def get_settings() -> Settings:
    return load_settings()
