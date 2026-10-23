import hashlib
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from text_extraction.settings import ApiKey, ConfigError, Settings, get_settings, load_settings

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
    "FETCH_TIMEOUT_SECONDS",
    "FETCH_MAX_BYTES",
    "FETCH_MAX_REDIRECTS",
    "FETCH_ALLOW_PRIVATE_NETWORKS",
    "OUTBOUND_MAX_CONCURRENCY_PER_HOST",
    "OUTBOUND_MIN_DELAY_MS",
    "OUTBOUND_MAX_CRAWL_DELAY_SECONDS",
    "OUTBOUND_MAX_WAIT_SECONDS",
    "OUTBOUND_BACKOFF_SECONDS",
    "OUTBOUND_MAX_BACKOFF_SECONDS",
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_OPEN_SECONDS",
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
    assert settings.fetch_timeout_seconds == 20
    assert settings.fetch_max_bytes == 20_000_000
    assert settings.fetch_max_redirects == 5
    assert settings.fetch_allow_private_networks is False
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


API_KEYS_HINT = "API_KEYS must be a JSON list of {name, role, sha256} objects"


@pytest.mark.parametrize("raw", ["{not json", "", "   ", '{"name": "x"}', '"text"', "42"])
def test_malformed_api_keys_env_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("API_KEYS", raw)

    with pytest.raises(ConfigError, match=re.escape(API_KEYS_HINT)):
        load_settings()


def test_missing_api_keys_file_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("API_KEYS_FILE", str(missing))

    with pytest.raises(ConfigError, match="API_KEYS_FILE") as info:
        load_settings()

    assert str(missing) in str(info.value)


def test_invalid_json_api_keys_file_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text("{garbage")
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    with pytest.raises(ConfigError, match="API_KEYS_FILE") as info:
        load_settings()

    assert str(keys_file) in str(info.value)


def test_wrong_shape_api_keys_file_names_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"name": "not-a-list"}))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    with pytest.raises(ConfigError) as info:
        load_settings()

    assert "API_KEYS_FILE" in str(info.value)
    assert str(keys_file) in str(info.value)


def test_config_error_for_missing_keys() -> None:
    with pytest.raises(ConfigError, match="AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE"):
        load_settings()


def test_load_settings_returns_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    assert load_settings().auth_mode == "none"


def test_get_settings_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        get_settings()


SECRET = "my-super-secret-raw-key"  # noqa: S105


def test_raw_secret_in_env_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", json.dumps([{"name": "x", "role": "user", "sha256": SECRET}]))

    with pytest.raises(ValidationError) as validation_info:
        Settings()
    with pytest.raises(ConfigError) as config_info:
        load_settings()

    assert SECRET not in str(validation_info.value)
    assert SECRET not in str(config_info.value)
    assert "sha256" in str(config_info.value)


def test_raw_secret_in_file_is_not_echoed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps([{"name": "x", "role": "user", "sha256": SECRET}]))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    with pytest.raises(ValidationError) as validation_info:
        Settings()
    with pytest.raises(ConfigError) as config_info:
        load_settings()

    assert SECRET not in str(validation_info.value)
    assert SECRET not in str(config_info.value)
    assert str(keys_file) in str(config_info.value)


def hash_entry(name: str, sha256: str) -> dict[str, object]:
    return {"name": name, "role": "user", "sha256": sha256}


@pytest.mark.parametrize("value", ["verbose", "LOUD", "", "10"])
def test_invalid_log_level_is_a_config_error(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_LEVEL", value)

    with pytest.raises(ConfigError, match="log_level"):
        load_settings()


@pytest.mark.parametrize("value", ["debug", "Info", "WARNING", " error ", "critical"])
def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_LEVEL", value)

    assert load_settings().log_level == value.strip().upper()


@pytest.mark.parametrize("name", ["PUBLIC_PORT", "INTERNAL_PORT"])
@pytest.mark.parametrize("value", ["70000", "0", "-1", "65536", "http"])
def test_invalid_port_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match=name.lower()):
        load_settings()


@pytest.mark.parametrize("value", ["1", "65535"])
def test_port_bounds_are_accepted(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PUBLIC_PORT", value)

    assert load_settings().public_port == int(value)


@pytest.mark.parametrize("value", ["0", "-5"])
def test_rate_limit_per_minute_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", value)

    with pytest.raises(ConfigError, match="rate_limit_per_minute"):
        load_settings()


@pytest.mark.parametrize("value", [0, -1])
def test_api_key_rate_limit_must_be_positive(value: int) -> None:
    with pytest.raises(ValidationError, match="rate_limit_per_minute"):
        ApiKey(name="x", role="user", sha256=sha256_hex("x"), rate_limit_per_minute=value)


def test_api_key_name_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="name"):
        ApiKey(name="", role="user", sha256=sha256_hex("x"))


def test_duplicate_sha256_rejected_and_names_the_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = sha256_hex("same-secret")
    monkeypatch.setenv(
        "API_KEYS", json.dumps([hash_entry("first", digest), hash_entry("second", digest)])
    )

    with pytest.raises(ConfigError, match="first and second share the same sha256") as info:
        load_settings()

    assert digest not in str(info.value)


def test_duplicate_sha256_across_env_and_file_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    digest = sha256_hex("same-secret")
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps([hash_entry("from-file", digest)]))
    monkeypatch.setenv("API_KEYS", json.dumps([hash_entry("from-env", digest)]))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    with pytest.raises(ConfigError, match="from-env and from-file share the same sha256") as info:
        load_settings()

    assert digest not in str(info.value)


@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("FETCH_TIMEOUT_SECONDS", "0", "fetch_timeout_seconds"),
        ("FETCH_MAX_BYTES", "1023", "fetch_max_bytes"),
        ("FETCH_MAX_REDIRECTS", "-1", "fetch_max_redirects"),
        ("FETCH_MAX_REDIRECTS", "21", "fetch_max_redirects"),
    ],
)
def test_fetch_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, field: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match=field):
        load_settings()


def test_fetch_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("FETCH_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("FETCH_MAX_BYTES", "2048")
    monkeypatch.setenv("FETCH_MAX_REDIRECTS", "0")
    monkeypatch.setenv("FETCH_ALLOW_PRIVATE_NETWORKS", "true")

    settings = load_settings()

    assert settings.fetch_timeout_seconds == 2.5
    assert settings.fetch_max_bytes == 2048
    assert settings.fetch_max_redirects == 0
    assert settings.fetch_allow_private_networks is True


def test_outbound_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = load_settings()

    assert settings.outbound_max_concurrency_per_host == 2
    assert settings.outbound_min_delay_ms == 1000
    assert settings.outbound_max_crawl_delay_seconds == 10
    assert settings.outbound_max_wait_seconds == 10
    assert settings.outbound_backoff_seconds == 30
    assert settings.outbound_max_backoff_seconds == 300
    assert settings.circuit_failure_threshold == 5
    assert settings.circuit_open_seconds == 60
    assert settings.outbound_max_connections == 200
    assert settings.outbound_max_keepalive == 50


@pytest.mark.parametrize(
    ("name", "value", "field"),
    [
        ("OUTBOUND_MAX_CONCURRENCY_PER_HOST", "0", "outbound_max_concurrency_per_host"),
        ("OUTBOUND_MIN_DELAY_MS", "-1", "outbound_min_delay_ms"),
        ("OUTBOUND_MAX_CRAWL_DELAY_SECONDS", "-1", "outbound_max_crawl_delay_seconds"),
        ("OUTBOUND_MAX_WAIT_SECONDS", "-1", "outbound_max_wait_seconds"),
        ("OUTBOUND_BACKOFF_SECONDS", "0.5", "outbound_backoff_seconds"),
        ("OUTBOUND_MAX_BACKOFF_SECONDS", "0", "outbound_max_backoff_seconds"),
        ("CIRCUIT_FAILURE_THRESHOLD", "0", "circuit_failure_threshold"),
        ("CIRCUIT_OPEN_SECONDS", "0", "circuit_open_seconds"),
        ("OUTBOUND_MAX_CONNECTIONS", "0", "outbound_max_connections"),
        ("OUTBOUND_MAX_KEEPALIVE", "-1", "outbound_max_keepalive"),
    ],
)
def test_outbound_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, field: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match=field):
        load_settings()


def test_quality_min_words_default_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().quality_min_words == 50

    monkeypatch.setenv("QUALITY_MIN_WORDS", "0")
    assert load_settings().quality_min_words == 0

    monkeypatch.setenv("QUALITY_MIN_WORDS", "-1")
    with pytest.raises(ConfigError, match="quality_min_words"):
        load_settings()


def test_result_cache_and_fingerprint_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    settings = load_settings()
    assert settings.result_cache_ttl_seconds == 3600
    assert settings.result_store_ttl_seconds == 86400
    assert settings.fingerprint_ttl_seconds == 2_592_000

    monkeypatch.setenv("RESULT_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("RESULT_STORE_TTL_SECONDS", "0")
    monkeypatch.setenv("FINGERPRINT_TTL_SECONDS", "0")
    settings = load_settings()
    assert settings.result_cache_ttl_seconds == 0
    assert settings.result_store_ttl_seconds == 0
    assert settings.fingerprint_ttl_seconds == 0

    monkeypatch.setenv("FINGERPRINT_TTL_SECONDS", "-1")
    with pytest.raises(ConfigError, match="fingerprint_ttl_seconds"):
        load_settings()


def test_extract_timeout_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().extract_timeout_seconds == 25

    monkeypatch.setenv("EXTRACT_TIMEOUT_SECONDS", "7.5")
    assert load_settings().extract_timeout_seconds == 7.5

    monkeypatch.setenv("EXTRACT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigError, match="extract_timeout_seconds"):
        load_settings()
