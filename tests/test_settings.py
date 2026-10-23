import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from text_extraction.settings import ApiKey, Settings, get_settings

SETTING_ENV_VARS = (
    "PUBLIC_HOST",
    "PUBLIC_PORT",
    "INTERNAL_HOST",
    "INTERNAL_PORT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "AUTH_MODE",
    "API_KEYS",
    "API_KEYS_FILE",
    "VALKEY_URL",
    "RATE_LIMIT_PER_MINUTE",
    "RATE_LIMIT_ENABLED",
)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def key_entry(name: str, secret: str, role: str = "user", **extra: object) -> dict[str, object]:
    return {"name": name, "role": role, "sha256": sha256_hex(secret), **extra}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SETTING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", json.dumps([key_entry("ci", "secret")]))

    settings = Settings()

    assert settings.public_host == "0.0.0.0"  # noqa: S104
    assert settings.public_port == 8080
    assert settings.internal_host == "0.0.0.0"  # noqa: S104
    assert settings.internal_port == 9090
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.auth_mode == "api_key"
    assert settings.api_keys_file is None
    assert settings.valkey_url == "redis://localhost:6379/0"
    assert settings.rate_limit_per_minute == 60
    assert settings.rate_limit_enabled is True
    assert settings.api_keys == [
        ApiKey(name="ci", role="user", sha256=sha256_hex("secret"), rate_limit_per_minute=None)
    ]


def test_api_keys_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "API_KEYS",
        json.dumps(
            [
                key_entry("alice", "a", rate_limit_per_minute=5),
                key_entry("root", "b", role="admin"),
            ]
        ),
    )

    settings = Settings()

    assert [(k.name, k.role, k.rate_limit_per_minute) for k in settings.api_keys] == [
        ("alice", "user", 5),
        ("root", "admin", None),
    ]


def test_api_keys_file_merged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps([key_entry("from-file", "f", role="admin")]))
    monkeypatch.setenv("API_KEYS", json.dumps([key_entry("from-env", "e")]))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    settings = Settings()

    assert [k.name for k in settings.api_keys] == ["from-env", "from-file"]
    assert settings.api_keys[1].role == "admin"


def test_api_keys_file_alone_satisfies_api_key_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps([key_entry("only-file", "f")]))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    assert [k.name for k in Settings().api_keys] == ["only-file"]


def test_api_key_mode_without_keys_fails() -> None:
    with pytest.raises(
        ValidationError, match="AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE"
    ):
        Settings()


@pytest.mark.parametrize("bad_hash", ["abc123", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
def test_invalid_sha256_rejected(monkeypatch: pytest.MonkeyPatch, bad_hash: str) -> None:
    entry = {"name": "bad", "role": "user", "sha256": bad_hash}
    monkeypatch.setenv("API_KEYS", json.dumps([entry]))

    with pytest.raises(ValidationError, match="sha256"):
        Settings()


def test_invalid_sha256_in_file_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps([{"name": "bad", "role": "user", "sha256": "xyz"}]))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    with pytest.raises(ValidationError, match="sha256"):
        Settings()


def test_duplicate_key_names_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", json.dumps([key_entry("same", "one"), key_entry("same", "two")]))

    with pytest.raises(ValidationError, match="duplicate API key name: same"):
        Settings()


def test_duplicate_key_names_across_env_and_file_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps([key_entry("same", "two")]))
    monkeypatch.setenv("API_KEYS", json.dumps([key_entry("same", "one")]))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    with pytest.raises(ValidationError, match="duplicate API key name: same"):
        Settings()


def test_auth_none_needs_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = Settings()

    assert settings.auth_mode == "none"
    assert settings.api_keys == []


def test_environment_overrides_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("auth_mode", "none")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("PUBLIC_PORT", "8000")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")

    settings = Settings()

    assert settings.log_format == "console"
    assert settings.public_port == 8000
    assert settings.rate_limit_enabled is False


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    assert get_settings() is get_settings()
