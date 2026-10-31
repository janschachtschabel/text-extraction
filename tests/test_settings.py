import hashlib
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from text_extraction.settings import (
    ApiKey,
    ConfigError,
    Settings,
    get_settings,
    llm_available,
    load_settings,
)

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
    "BROWSER_ENABLED",
    "BROWSER_TIMEOUT_SECONDS",
    "BROWSER_QUEUE_WAIT_SECONDS",
    "BROWSER_CONCURRENCY",
    "BROWSER_IDLE_WAIT_SECONDS",
    "BROWSER_RECYCLE_AFTER_PAGES",
    "BROWSER_MAX_BYTES",
    "BROWSER_BLOCKED_RESOURCES",
    "BROWSER_AUTOCONSENT",
    "BROWSER_EXECUTABLE_PATH",
    "BROWSER_UNBLOCK_ENABLED",
    "BROWSER_UNBLOCK_CHANNEL",
    "BROWSER_UNBLOCK_HEADLESS",
    "BROWSER_UNBLOCK_TIMEOUT_SECONDS",
    "BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS",
    "BROWSER_UNBLOCK_COOKIE_TTL_SECONDS",
    "SCREENSHOT_MAX_BYTES",
    "SCREENSHOT_MAX_HEIGHT_PX",
    "ARTIFACT_TTL_SECONDS",
    "DOCUMENT_MAX_BYTES",
    "DOCUMENT_MAX_PAGES",
    "DOCUMENT_MAX_UNCOMPRESSED_BYTES",
    "DOCUMENT_CONCURRENCY",
    "DOCUMENT_MEMORY_LIMIT_MB",
    "DOCLING_ENABLED",
    "DOCLING_TIMEOUT_SECONDS",
    "DOCLING_SYNC_WAIT_SECONDS",
    "DOCLING_INPUT_TTL_SECONDS",
    "DOCLING_INPUT_BUDGET_BYTES",
    "DOCLING_MAX_OPEN_PER_CLIENT",
    "DOCLING_CONCURRENCY",
    "DOCLING_THREADS",
    "DOCLING_MAX_PAGES",
    "DOCLING_MEMORY_LIMIT_MB",
    "DOCLING_OCR_LANGUAGES",
    "DOCLING_MODELS_PATH",
    "MEDIA_ENABLED",
    "MEDIA_CONCURRENCY",
    "MEDIA_MEMORY_LIMIT_MB",
    "MEDIA_EXTRACT_MAX_BYTES",
    "MEDIA_SUBTITLE_MAX_BYTES",
    "MEDIA_MAX_TRACKS",
    "MEDIA_MAX_DURATION_SECONDS",
    "MEDIA_MAX_BYTES",
    "MEDIA_UPLOAD_MAX_BYTES",
    "MEDIA_DOWNLOAD_TIMEOUT_SECONDS",
    "ASR_ENABLED",
    "ASR_BACKEND",
    "ASR_MODEL",
    "ASR_MODELS_PATH",
    "ASR_COMPUTE_TYPE",
    "ASR_THREADS",
    "ASR_CONCURRENCY",
    "ASR_MEMORY_LIMIT_MB",
    "ASR_TIMEOUT_SECONDS",
    "ASR_INPUT_TTL_SECONDS",
    "ASR_MAX_OPEN_PER_CLIENT",
    "ASR_SCRATCH_PATH",
    "ASR_EXTERNAL_BASE_URL",
    "ASR_EXTERNAL_MODEL",
    "ASR_EXTERNAL_API_KEY",
    "ASR_EXTERNAL_TIMEOUT_SECONDS",
    "PII_ENABLED",
    "PII_DEFAULT_ENTITIES",
    "PII_SCORE_THRESHOLD",
    "PII_FALLBACK_LANGUAGE",
    "PII_SYNC_WAIT_SECONDS",
    "PII_TIMEOUT_SECONDS",
    "PII_MAX_CHARS",
    "PII_MAX_ENTITIES",
    "PII_CONCURRENCY",
    "PII_MEMORY_LIMIT_MB",
    "PII_MODELS_PATH",
    "PII_MODEL_SIZE",
    "PII_HASH_KEY",
    "LLM_ENABLED",
    "LLM_BASE_URL",
    "LLM_ALLOW_HTTP",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_EMBEDDING_MODEL",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_INPUT_TOKENS",
    "LLM_CONCURRENCY",
    "LLM_SYNC_WAIT_SECONDS",
    "SELECTOR_LEARN_TTL_SECONDS",
    "STRUCTURED_REGEX_TIMEOUT_SECONDS",
    "CHUNK_MAX_CHUNKS",
    "SITE_MAX_DEPTH",
    "SITE_MAX_PAGES",
    "SITE_MAX_SITEMAP_FILES",
    "SITE_MAX_SITEMAP_URLS",
    "SITE_SITEMAP_MAX_BYTES",
    "SITE_MAX_LINKS_PER_PAGE",
    "MCP_ENABLED",
    "MCP_ALLOWED_ORIGINS",
    "MCP_WAIT_SECONDS",
    "MCP_MAX_CONTENT_CHARS",
    "MCP_IMAGE_MAX_BYTES",
    "UI_ENABLED",
    "UI_TREE_MAX_URLS",
    "UI_BATCH_FILE_MAX_BYTES",
    "UI_BATCH_FILE_MAX_ROWS",
    "UI_IMAGE_MAX_BYTES",
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
        ("FETCH_TIMEOUT_SECONDS", "0.5", "fetch_timeout_seconds"),
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
    monkeypatch.setenv("FETCH_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("FETCH_MAX_BYTES", "2048")
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", "2048")  # at most FETCH_MAX_BYTES
    monkeypatch.setenv("FETCH_MAX_REDIRECTS", "0")
    monkeypatch.setenv("FETCH_ALLOW_PRIVATE_NETWORKS", "true")

    settings = load_settings()

    assert settings.fetch_timeout_seconds == 1
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

    monkeypatch.setenv("EXTRACT_TIMEOUT_SECONDS", "2")
    assert load_settings().extract_timeout_seconds == 2

    # Below 2 s, the fetch could be left with less than its 1 s minimum on every request.
    for value in ("0", "1.9"):
        monkeypatch.setenv("EXTRACT_TIMEOUT_SECONDS", value)
        with pytest.raises(ConfigError, match="extract_timeout_seconds"):
            load_settings()


def test_conversion_budget_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    settings = load_settings()
    assert settings.convert_max_bytes == 5_242_880
    assert settings.convert_concurrency == 2

    monkeypatch.setenv("CONVERT_MAX_BYTES", "1023")
    with pytest.raises(ConfigError, match="convert_max_bytes"):
        load_settings()

    monkeypatch.setenv("CONVERT_MAX_BYTES", "1024")
    monkeypatch.setenv("CONVERT_CONCURRENCY", "0")
    with pytest.raises(ConfigError, match="convert_concurrency"):
        load_settings()


def test_shutdown_drain_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().shutdown_drain_seconds == 5

    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0")
    assert load_settings().shutdown_drain_seconds == 0

    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "-1")
    with pytest.raises(ConfigError, match="shutdown_drain_seconds"):
        load_settings()


def test_job_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    settings = load_settings()
    assert settings.job_ttl_seconds == 86400
    assert settings.job_item_max_bytes == 2_000_000

    monkeypatch.setenv("JOB_TTL_SECONDS", "60")
    monkeypatch.setenv("JOB_ITEM_MAX_BYTES", "10000")
    settings = load_settings()
    assert settings.job_ttl_seconds == 60
    assert settings.job_item_max_bytes == 10_000

    monkeypatch.setenv("JOB_TTL_SECONDS", "59")
    with pytest.raises(ConfigError, match="job_ttl_seconds"):
        load_settings()
    monkeypatch.setenv("JOB_TTL_SECONDS", "60")
    monkeypatch.setenv("JOB_ITEM_MAX_BYTES", "9999")
    with pytest.raises(ConfigError, match="job_item_max_bytes"):
        load_settings()


def test_job_item_max_attempts_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().job_item_max_attempts == 5

    monkeypatch.setenv("JOB_ITEM_MAX_ATTEMPTS", "1")
    assert load_settings().job_item_max_attempts == 1

    monkeypatch.setenv("JOB_ITEM_MAX_ATTEMPTS", "0")
    with pytest.raises(ConfigError, match="job_item_max_attempts"):
        load_settings()


def test_scheduler_claim_scan_budget_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().scheduler_claim_scan_budget == 200

    monkeypatch.setenv("SCHEDULER_CLAIM_SCAN_BUDGET", "1")
    assert load_settings().scheduler_claim_scan_budget == 1

    for value in ("0", "5001"):
        monkeypatch.setenv("SCHEDULER_CLAIM_SCAN_BUDGET", value)
        with pytest.raises(ConfigError, match="scheduler_claim_scan_budget"):
            load_settings()


def test_extract_max_background_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().extract_max_background == 32

    monkeypatch.setenv("EXTRACT_MAX_BACKGROUND", "1")
    assert load_settings().extract_max_background == 1

    monkeypatch.setenv("EXTRACT_MAX_BACKGROUND", "0")
    with pytest.raises(ConfigError, match="extract_max_background"):
        load_settings()


def test_request_max_body_bytes_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().request_max_body_bytes == 65536

    monkeypatch.setenv("REQUEST_MAX_BODY_BYTES", "1024")
    assert load_settings().request_max_body_bytes == 1024

    monkeypatch.setenv("REQUEST_MAX_BODY_BYTES", "1023")
    with pytest.raises(ConfigError, match="request_max_body_bytes"):
        load_settings()


def test_valkey_memory_guard_ratio_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().valkey_memory_guard_ratio == 0.8

    monkeypatch.setenv("VALKEY_MEMORY_GUARD_RATIO", "1.0")
    assert load_settings().valkey_memory_guard_ratio == 1.0

    for value in ("0.09", "1.01"):
        monkeypatch.setenv("VALKEY_MEMORY_GUARD_RATIO", value)
        with pytest.raises(ConfigError, match="valkey_memory_guard_ratio"):
            load_settings()


def test_browser_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = load_settings()

    assert settings.browser_enabled is False
    assert settings.browser_timeout_seconds == 30
    assert settings.browser_queue_wait_seconds == 10
    assert settings.browser_concurrency == 4
    assert settings.browser_idle_wait_seconds == 8
    assert settings.browser_recycle_after_pages == 200
    assert settings.browser_max_bytes == 50_000_000
    assert settings.browser_blocked_resources == "image,media,font"
    assert settings.browser_autoconsent is True
    assert settings.browser_executable_path is None
    assert settings.browser_unblock_enabled is False
    assert settings.browser_unblock_channel == "chrome"
    assert settings.browser_unblock_headless is False
    assert settings.browser_unblock_timeout_seconds == 60
    assert settings.browser_unblock_challenge_wait_seconds == 20
    assert settings.browser_unblock_cookie_ttl_seconds == 1800
    assert settings.screenshot_max_bytes == 10_485_760
    assert settings.screenshot_max_height_px == 16384
    assert settings.artifact_ttl_seconds == 3600


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("BROWSER_TIMEOUT_SECONDS", ["5", "120"], ["4.9", "120.1"]),
        ("BROWSER_QUEUE_WAIT_SECONDS", ["0", "60"], ["-1", "61"]),
        ("BROWSER_CONCURRENCY", ["1", "64"], ["0"]),
        ("BROWSER_IDLE_WAIT_SECONDS", ["0", "30"], ["-1", "31"]),
        ("BROWSER_RECYCLE_AFTER_PAGES", ["1"], ["0"]),
        ("BROWSER_MAX_BYTES", ["1000000"], ["999999"]),
        ("BROWSER_UNBLOCK_CHANNEL", ["chrome", "chromium"], ["firefox"]),
        ("BROWSER_UNBLOCK_TIMEOUT_SECONDS", ["10", "180"], ["9", "181"]),
        ("BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS", ["0", "60"], ["-1", "61"]),
        ("BROWSER_UNBLOCK_COOKIE_TTL_SECONDS", ["0", "1800"], ["-1"]),
        ("SCREENSHOT_MAX_HEIGHT_PX", ["1080", "32768"], ["1079", "32769"]),
        ("SCREENSHOT_MAX_BYTES", ["1", "10485760"], ["0"]),
        ("ARTIFACT_TTL_SECONDS", ["60", "86400"], ["59"]),
    ],
)
def test_browser_bounds(
    monkeypatch: pytest.MonkeyPatch, name: str, good: list[str], bad: list[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    for value in good:
        monkeypatch.setenv(name, value)
        load_settings()
    for value in bad:
        monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError, match=name.lower()):
            load_settings()


def test_browser_switches_and_path_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("BROWSER_ENABLED", "true")
    monkeypatch.setenv("BROWSER_UNBLOCK_ENABLED", "true")
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "/opt/chromium/chrome")

    settings = load_settings()

    assert settings.browser_enabled is True
    assert settings.browser_unblock_enabled is True
    assert settings.browser_executable_path == Path("/opt/chromium/chrome")


@pytest.mark.parametrize(
    ("value", "normalised"),
    [
        ("", ""),
        ("  ", ""),
        ("image", "image"),
        ("Image, MEDIA ,font", "image,media,font"),
        (
            "stylesheet,script,xhr,fetch,websocket,other",
            "stylesheet,script,xhr,fetch,websocket,other",
        ),
        ("image,image", "image"),
        ("image,,font,", "image,font"),
    ],
)
def test_browser_blocked_resources_are_normalised(
    monkeypatch: pytest.MonkeyPatch, value: str, normalised: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("BROWSER_BLOCKED_RESOURCES", value)

    assert load_settings().browser_blocked_resources == normalised


@pytest.mark.parametrize("value", ["document", "image,document", "bogus", "image;font"])
def test_invalid_browser_blocked_resources_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("BROWSER_BLOCKED_RESOURCES", value)

    with pytest.raises(ConfigError, match="browser_blocked_resources"):
        load_settings()


def test_document_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = load_settings()

    assert settings.document_max_bytes == 20_000_000
    assert settings.document_max_pages == 100
    assert settings.document_max_uncompressed_bytes == 200_000_000
    assert settings.document_concurrency == 2
    assert settings.document_memory_limit_mb == 2048
    assert settings.docling_enabled is False
    assert settings.docling_timeout_seconds == 300
    assert settings.docling_sync_wait_seconds == 20
    assert settings.docling_input_ttl_seconds == 3600
    assert settings.docling_input_budget_bytes == 200_000_000
    assert settings.docling_max_open_per_client == 5
    assert settings.docling_concurrency == 1
    assert settings.docling_threads == 4
    assert settings.docling_max_pages == 100
    assert settings.docling_memory_limit_mb == 6144
    assert settings.docling_ocr_languages == "deu,eng"
    assert settings.docling_models_path == Path("/models")


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("DOCUMENT_MAX_BYTES", ["1024", "20000000"], ["1023", "0", "-1"]),
        ("DOCUMENT_MAX_PAGES", ["1", "500"], ["0", "-1"]),
        ("DOCUMENT_CONCURRENCY", ["1", "8"], ["0"]),
        ("DOCUMENT_MEMORY_LIMIT_MB", ["256", "4096"], ["255", "0"]),
        ("DOCLING_TIMEOUT_SECONDS", ["30", "1800"], ["29", "1801"]),
        ("DOCLING_SYNC_WAIT_SECONDS", ["0", "25"], ["-1", "26"]),
        ("DOCLING_INPUT_TTL_SECONDS", ["600", "7200"], ["599", "0"]),
        ("DOCLING_MAX_OPEN_PER_CLIENT", ["1", "50"], ["0"]),
        ("DOCLING_INPUT_BUDGET_BYTES", ["20000000", "1000000000"], ["1023", "0"]),
    ],
)
def test_document_setting_bounds(
    monkeypatch: pytest.MonkeyPatch, name: str, good: list[str], bad: list[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    for value in good:
        monkeypatch.setenv(name, value)
        load_settings()
    for value in bad:
        monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError, match=name.lower()):
            load_settings()


def test_document_max_bytes_cannot_exceed_fetch_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", "30000000")
    monkeypatch.setenv("FETCH_MAX_BYTES", "20000000")

    with pytest.raises(ConfigError, match="DOCUMENT_MAX_BYTES") as error:
        load_settings()

    assert "30000000" not in str(error.value)
    assert "20000000" not in str(error.value)


def test_document_max_bytes_cannot_exceed_the_input_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document larger than the budget of all inputs could never be queued for OCR."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("DOCLING_INPUT_BUDGET_BYTES", "10000000")

    with pytest.raises(ConfigError, match="DOCLING_INPUT_BUDGET_BYTES"):
        load_settings()
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", "10000000")
    assert load_settings().docling_input_budget_bytes == 10_000_000


def test_document_max_bytes_may_equal_fetch_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", "5000000")
    monkeypatch.setenv("FETCH_MAX_BYTES", "5000000")

    assert load_settings().document_max_bytes == 5_000_000


@pytest.mark.parametrize("value", ["deu,eng", "eng", "deu_frak,eng", "chi_sim,chi_tra,eng"])
def test_docling_ocr_languages_accepts_tesseract_codes(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("DOCLING_OCR_LANGUAGES", value)

    assert load_settings().docling_ocr_languages == value


@pytest.mark.parametrize(
    "value", ["deu;rm -rf", "", "de", "deu,", ",eng", "deu eng", "DEU", "deu,,eng", "deu+eng"]
)
def test_docling_ocr_languages_validated(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("DOCLING_OCR_LANGUAGES", value)

    with pytest.raises(ConfigError, match="docling_ocr_languages") as error:
        load_settings()

    if value:
        assert value not in str(error.value)


def test_media_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = load_settings()

    assert settings.media_enabled is True
    assert settings.media_concurrency == 2
    assert settings.media_memory_limit_mb == 1024
    assert settings.media_extract_max_bytes == 30_000_000
    assert settings.media_subtitle_max_bytes == 5_000_000
    assert settings.media_max_tracks == 3
    assert settings.media_max_duration_seconds == 7200  # D6: 2 h
    assert settings.media_max_bytes == 500_000_000
    assert settings.media_upload_max_bytes == 25_000_000
    assert settings.media_download_timeout_seconds == 600
    assert settings.asr_enabled is False
    assert settings.asr_backend == "local"
    assert settings.asr_model == "large-v3-turbo"
    assert settings.asr_models_path == Path("/models")
    assert settings.asr_compute_type == "int8"
    assert settings.asr_threads == 4
    assert settings.asr_concurrency == 1
    assert settings.asr_memory_limit_mb == 4096
    assert settings.asr_timeout_seconds == 10_800  # D6: 3 h for the whole item
    assert settings.asr_input_ttl_seconds == 28_800  # D6: longer than the lease
    assert settings.asr_max_open_per_client == 3
    assert settings.asr_scratch_path == Path("/tmp/asr")  # noqa: S108 - the default of an emptyDir
    assert settings.asr_external_base_url is None
    assert settings.asr_external_model == "whisper-1"
    assert settings.asr_external_api_key is None
    assert settings.asr_external_timeout_seconds == 300


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("MEDIA_CONCURRENCY", ["1", "8"], ["0", "-1"]),
        ("MEDIA_MEMORY_LIMIT_MB", ["256", "4096"], ["255", "0"]),
        ("MEDIA_MAX_TRACKS", ["0", "10"], ["-1", "11"]),
        ("MEDIA_MAX_DURATION_SECONDS", ["60", "86400"], ["59", "0"]),
        ("MEDIA_DOWNLOAD_TIMEOUT_SECONDS", ["30", "3600"], ["29", "3601"]),
        ("ASR_BACKEND", ["local", "openai"], ["remote", "OPENAI "]),
        ("ASR_MODEL", ["small", "large-v3-turbo"], ["large-v3", "../small", ""]),
        ("ASR_TIMEOUT_SECONDS", ["60", "14400"], ["59", "14401"]),
        ("ASR_INPUT_TTL_SECONDS", ["600", "86400"], ["599", "0"]),
        ("ASR_THREADS", ["1", "16"], ["0"]),
        ("ASR_CONCURRENCY", ["1", "4"], ["0"]),
        ("ASR_MAX_OPEN_PER_CLIENT", ["1", "10"], ["0"]),
    ],
)
def test_media_setting_bounds(
    monkeypatch: pytest.MonkeyPatch, name: str, good: list[str], bad: list[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    for value in good:
        monkeypatch.setenv(name, value)
        load_settings()
    for value in bad:
        monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError, match=name.lower()):
            load_settings()


def test_asr_input_ttl_outlives_the_lease_while_asr_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """An upload waits in its input until the ASR worker reads it; a lease that outlives the
    input would let a reclaimed item find its bytes gone (lease: 2 x ASR_TIMEOUT_SECONDS + 10)."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_ENABLED", "true")
    monkeypatch.setenv("ASR_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("ASR_INPUT_TTL_SECONDS", "1211")
    load_settings()

    monkeypatch.setenv("ASR_INPUT_TTL_SECONDS", "1210")
    with pytest.raises(ConfigError, match="ASR_INPUT_TTL_SECONDS"):
        load_settings()

    monkeypatch.setenv("ASR_ENABLED", "false")
    load_settings()  # no ASR worker, no lease


def test_asr_defaults_keep_the_input_past_the_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_ENABLED", "true")

    settings = load_settings()

    assert settings.asr_input_ttl_seconds > 2 * settings.asr_timeout_seconds + 10


@pytest.mark.parametrize(
    "value", ["https://asr.example.org/v1", "https://asr.example.org:8443/v1", "HTTPS://A.org/v1"]
)
def test_asr_external_base_url_accepts_https(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_EXTERNAL_BASE_URL", value)

    assert load_settings().asr_external_base_url == value


@pytest.mark.parametrize(
    "value",
    [
        "http://asr.example.org/v1",
        "asr.example.org",
        "ftp://asr.example.org/",
        "https://",
        "https:///v1",
    ],
)
def test_asr_external_base_url_https_only(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_EXTERNAL_BASE_URL", value)

    with pytest.raises(ConfigError, match="asr_external_base_url") as error:
        load_settings()

    assert value not in str(error.value)


def test_asr_external_base_url_empty_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Helm chart renders an unset value as an empty string."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_EXTERNAL_BASE_URL", "")

    assert load_settings().asr_external_base_url is None


def test_media_upload_max_bytes_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("MEDIA_UPLOAD_MAX_BYTES", "1000000")
    assert load_settings().media_upload_max_bytes == 1_000_000
    monkeypatch.setenv("MEDIA_UPLOAD_MAX_BYTES", "999999")
    with pytest.raises(ConfigError, match="media_upload_max_bytes"):
        load_settings()

    # With ASR on, at most the input budget: a larger upload could never be queued.
    monkeypatch.setenv("ASR_ENABLED", "true")
    monkeypatch.setenv("MEDIA_UPLOAD_MAX_BYTES", "200000000")
    assert load_settings().media_upload_max_bytes == 200_000_000
    monkeypatch.setenv("MEDIA_UPLOAD_MAX_BYTES", "200000001")
    with pytest.raises(ConfigError, match="MEDIA_UPLOAD_MAX_BYTES") as error:
        load_settings()
    assert "200000001" not in str(error.value)
    monkeypatch.setenv("DOCLING_INPUT_BUDGET_BYTES", "300000000")
    assert load_settings().media_upload_max_bytes == 200_000_001


def test_media_upload_max_bytes_is_unbound_by_the_budget_without_asr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ASR no media upload is queued: a small input budget (OCR only) stays valid."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("DOCLING_INPUT_BUDGET_BYTES", "10000000")
    monkeypatch.setenv("DOCUMENT_MAX_BYTES", "10000000")

    assert load_settings().media_upload_max_bytes == 25_000_000
    monkeypatch.setenv("ASR_ENABLED", "true")
    with pytest.raises(ConfigError, match="MEDIA_UPLOAD_MAX_BYTES"):
        load_settings()


def test_asr_external_api_key_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_EXTERNAL_API_KEY", "sk-very-secret")

    settings = load_settings()

    assert settings.asr_external_api_key == "sk-very-secret"
    assert "sk-very-secret" not in repr(settings)
    assert "sk-very-secret" not in str(settings)


@pytest.mark.parametrize("value", ["", "   "])
def test_asr_external_api_key_empty_means_unset(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An empty key (a chart that renders an unset secret so) is no key: without the caller's
    header, ``transcribe: true`` is 400 on the external backend instead of sending no key."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_EXTERNAL_API_KEY", value)

    assert load_settings().asr_external_api_key is None


def test_pii_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = load_settings()

    assert settings.pii_enabled is False
    assert settings.pii_default_entities == [
        "PERSON",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "IBAN_CODE",
        "CREDIT_CARD",
        "IP_ADDRESS",
    ]
    assert settings.pii_score_threshold == 0.4
    assert settings.pii_fallback_language == "en"
    assert settings.pii_sync_wait_seconds == 20
    assert settings.pii_timeout_seconds == 300
    assert settings.pii_max_chars == 2_000_000
    assert settings.pii_max_entities == 1000
    assert settings.pii_concurrency == 1
    assert settings.pii_memory_limit_mb == 3072
    assert settings.pii_models_path == Path("/models")
    assert settings.pii_model_size == "lg"
    assert settings.pii_hash_key is None


def test_pii_default_entities_from_a_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PII_DEFAULT_ENTITIES", " person, URL ,email_address")

    assert load_settings().pii_default_entities == ["PERSON", "URL", "EMAIL_ADDRESS"]


@pytest.mark.parametrize("value", ["", "PERSON,PERSON", "PERSON,SSN"])
def test_pii_default_entities_invalid(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PII_DEFAULT_ENTITIES", value)

    with pytest.raises(ConfigError, match="pii_default_entities"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PII_SCORE_THRESHOLD", "1.5"),
        ("PII_SCORE_THRESHOLD", "-0.1"),
        ("PII_SYNC_WAIT_SECONDS", "0"),
        ("PII_SYNC_WAIT_SECONDS", "61"),
        ("PII_TIMEOUT_SECONDS", "9"),
        ("PII_TIMEOUT_SECONDS", "3601"),
        ("PII_MAX_CHARS", "9999"),
        ("PII_MAX_ENTITIES", "0"),
        ("PII_MAX_ENTITIES", "100001"),
        ("PII_CONCURRENCY", "0"),
        ("PII_MEMORY_LIMIT_MB", "511"),
        ("PII_MODEL_SIZE", "xl"),
        ("PII_FALLBACK_LANGUAGE", "fr"),
    ],
)
def test_pii_bounds(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError):
        load_settings()


def test_pii_hash_key_not_in_repr_and_min_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PII_HASH_KEY", "0123456789abcdef0123456789abcdef-secret")

    settings = load_settings()

    assert settings.pii_hash_key is not None
    assert settings.pii_hash_key.get_secret_value() == "0123456789abcdef0123456789abcdef-secret"
    assert "0123456789abcdef" not in repr(settings)
    assert "0123456789abcdef" not in str(settings)

    monkeypatch.setenv("PII_HASH_KEY", "short-key-0123456789")
    with pytest.raises(ConfigError) as error:
        load_settings()
    assert "short-key" not in str(error.value)

    monkeypatch.setenv("PII_HASH_KEY", "é" * 16)  # 32 bytes in UTF-8
    assert load_settings().pii_hash_key is not None


@pytest.mark.parametrize("value", ["", "   "])
def test_pii_hash_key_empty_means_unset(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A chart that renders an unset secret so must not make the service refuse to start."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PII_HASH_KEY", value)

    assert load_settings().pii_hash_key is None


def test_pii_enabled_needs_jobs_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """N15: the nlp hop is a job; start-up refuses PII_ENABLED without JOBS_ENABLED."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("PII_ENABLED", "true")
    monkeypatch.setenv("JOBS_ENABLED", "false")

    with pytest.raises(ConfigError, match="PII_ENABLED needs JOBS_ENABLED"):
        load_settings()

    monkeypatch.setenv("JOBS_ENABLED", "true")
    assert load_settings().pii_enabled is True
    monkeypatch.setenv("PII_ENABLED", "false")
    monkeypatch.setenv("JOBS_ENABLED", "false")
    assert load_settings().jobs_enabled is False


def test_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """S16: the LLM is off; every other value is the documented default."""
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = load_settings()

    assert settings.llm_enabled is False
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_allow_http is False
    assert settings.llm_model == "gpt-6-luna"
    assert settings.llm_api_key is None
    assert settings.llm_embedding_model is None
    assert settings.llm_temperature == 0
    assert settings.llm_max_tokens == 4096
    assert settings.llm_timeout_seconds == 120
    assert settings.llm_max_input_tokens == 32_000
    assert settings.llm_concurrency == 4
    assert settings.llm_sync_wait_seconds == 30
    assert settings.selector_learn_ttl_seconds == 2_592_000
    assert settings.structured_regex_timeout_seconds == 0.5
    assert settings.chunk_max_chunks == 2000


def test_selector_processes_default_is_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """P11-S5: the smallest value whose throughput is within 10 % of the best stays the default.

    The selector sweep of load run 41190110782 (commit ad80ae4, GitHub runner with 4 vCPU) gave
    23.2 / 24.0 / 22.9 req/s and p95 605 / 622 / 623 ms with 1 / 2 / 4 selector processes, at
    574 / 610 / 681 MiB of API memory: 1 is within 10 % of the best (24.0), so it stays.
    """
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.delenv("STRUCTURED_SELECTOR_PROCESSES", raising=False)

    assert Settings().structured_selector_processes == 1


@pytest.mark.parametrize(
    "value", ["https://llm.example.org/v1", "HTTPS://llm.example.org:8443/v1", "https://a/v1"]
)
def test_llm_base_url_accepts_https(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_BASE_URL", value)

    assert load_settings().llm_base_url == value


@pytest.mark.parametrize(
    "value",
    [
        "http://llm.example.org/v1",
        "llm.example.org",
        "ftp://llm.example.org/",
        "https://",
        "https:///v1",
        "",
        "   ",
    ],
)
def test_llm_base_url_must_be_https_unless_allowed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_BASE_URL", value)

    with pytest.raises(ConfigError, match="LLM_BASE_URL") as error:
        load_settings()

    assert "LLM_ALLOW_HTTP" in str(error.value)  # says what to do, not what was given
    if value.strip():
        assert value.strip() not in str(error.value)  # the URL may carry credentials


def test_llm_allow_http_lets_a_local_endpoint_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("LLM_ALLOW_HTTP", "true")

    assert load_settings().llm_base_url == "http://127.0.0.1:11434/v1"

    monkeypatch.setenv("LLM_BASE_URL", "ftp://127.0.0.1/v1")  # still only http(s)
    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        load_settings()


def test_llm_api_key_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_API_KEY", "sk-very-secret")

    settings = load_settings()

    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "sk-very-secret"
    assert "sk-very-secret" not in repr(settings)
    assert "sk-very-secret" not in str(settings)
    assert "sk-very-secret" not in settings.model_dump_json()


@pytest.mark.parametrize("name", ["LLM_API_KEY", "LLM_EMBEDDING_MODEL"])
@pytest.mark.parametrize("value", ["", "   "])
def test_llm_empty_key_and_embedding_model_mean_unset(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """The Helm chart renders an unset value as an empty string."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv(name, value)

    settings = load_settings()

    assert settings.llm_api_key is None
    assert settings.llm_embedding_model is None


def test_llm_vision_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")

    settings = load_settings()

    assert settings.llm_vision_model is None
    assert settings.llm_vision_max_image_bytes == 5_000_000


@pytest.mark.parametrize("value", ["", "   "])
def test_llm_vision_model_empty_is_unset(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The Helm chart renders an unset value as an empty string (V-S13)."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_VISION_MODEL", value)

    assert load_settings().llm_vision_model is None

    monkeypatch.setenv("LLM_VISION_MODEL", "vision-1")
    assert load_settings().llm_vision_model == "vision-1"


def test_llm_vision_max_image_bytes_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    for accepted in ("10000", "20000000"):
        monkeypatch.setenv("LLM_VISION_MAX_IMAGE_BYTES", accepted)
        assert load_settings().llm_vision_max_image_bytes == int(accepted)
    for refused in ("9999", "20000001"):
        monkeypatch.setenv("LLM_VISION_MAX_IMAGE_BYTES", refused)
        with pytest.raises(ConfigError, match="llm_vision_max_image_bytes"):
            load_settings()


def test_llm_enabled_needs_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """D5: the LLM hop is a job of pool llm; start-up refuses LLM_ENABLED without jobs."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("JOBS_ENABLED", "false")

    with pytest.raises(ConfigError, match="LLM_ENABLED needs JOBS_ENABLED"):
        load_settings()

    monkeypatch.setenv("JOBS_ENABLED", "true")
    assert load_settings().llm_enabled is True


@pytest.mark.parametrize("name", ["OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"])
def test_llm_enabled_refuses_openai_environment_the_sdk_reads(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Issue #19: the OpenAI SDK reads these from the environment whatever the client is given
    (an Authorization line would replace the caller's key; the log is plain text). Start-up
    refuses them while LLM_ENABLED is true, naming the variable."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv(name, "debug" if name == "OPENAI_LOG" else "Authorization: Bearer x")

    with pytest.raises(ConfigError, match=name) as info:
        load_settings()

    assert "Bearer x" not in str(info.value)  # the value is not echoed
    monkeypatch.setenv(name, " ")  # empty means unset
    assert load_settings().llm_enabled is True
    monkeypatch.setenv(name, "debug")
    monkeypatch.setenv("LLM_ENABLED", "false")
    assert load_settings().llm_enabled is False  # no LLM client is built


@pytest.mark.parametrize("name", ["OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"])
def test_external_asr_refuses_openai_environment_the_sdk_reads(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Issue #19: the external ASR client is the same SDK, so start-up refuses these while ASR
    runs on the external backend (ASR_ENABLED and ASR_BACKEND=openai), naming the variable."""
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_ENABLED", "true")
    monkeypatch.setenv("ASR_BACKEND", "openai")
    monkeypatch.setenv("ASR_EXTERNAL_BASE_URL", "https://asr.example.org/v1")
    monkeypatch.setenv(name, "debug" if name == "OPENAI_LOG" else "Authorization: Bearer x")

    with pytest.raises(ConfigError, match=name) as info:
        load_settings()

    assert "ASR_BACKEND=openai" in str(info.value)
    assert "Bearer x" not in str(info.value)
    monkeypatch.setenv("ASR_BACKEND", "local")  # the local backend calls no SDK
    assert load_settings().asr_backend == "local"
    monkeypatch.setenv("ASR_BACKEND", "openai")
    monkeypatch.setenv("ASR_ENABLED", "false")
    assert load_settings().asr_enabled is False


def test_llm_enabled_names_every_refused_openai_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-A: b")
    monkeypatch.setenv("OPENAI_LOG", "info")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-1")  # never sent (llm/client.py), so allowed
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-1")

    with pytest.raises(ConfigError) as info:
        load_settings()

    message = str(info.value)
    assert "OPENAI_CUSTOM_HEADERS" in message
    assert "OPENAI_LOG" in message
    assert "OPENAI_ORG_ID" not in message
    assert "OPENAI_PROJECT_ID" not in message


def test_llm_graceful_seconds_defaults_to_30(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #19: the llm worker's grace for items in flight, 30 s as before."""
    monkeypatch.setenv("AUTH_MODE", "none")
    assert load_settings().llm_graceful_seconds == 30


def test_llm_available_is_enabled_and_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert llm_available(load_settings()) is False
    monkeypatch.setenv("LLM_ENABLED", "true")
    assert llm_available(load_settings()) is True


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("LLM_TEMPERATURE", ["0", "2", "0.7"], ["-0.1", "2.1"]),
        ("LLM_MAX_TOKENS", ["1", "4096"], ["0"]),
        ("LLM_TIMEOUT_SECONDS", ["5", "600"], ["4.9", "601"]),
        ("LLM_MAX_INPUT_TOKENS", ["1000", "1000000"], ["999", "1000001"]),
        ("LLM_CONCURRENCY", ["1", "16"], ["0"]),
        ("LLM_SYNC_WAIT_SECONDS", ["1", "60"], ["0.5", "61"]),
        ("LLM_GRACEFUL_SECONDS", ["10", "900"], ["9", "901"]),
        ("SELECTOR_LEARN_TTL_SECONDS", ["60", "2592000"], ["59"]),
        ("STRUCTURED_REGEX_TIMEOUT_SECONDS", ["0.05", "5"], ["0.04", "5.1"]),
        ("CHUNK_MAX_CHUNKS", ["1", "2000"], ["0"]),
    ],
)
def test_llm_numeric_settings_are_bounded(
    monkeypatch: pytest.MonkeyPatch, name: str, good: list[str], bad: list[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    for value in good:
        monkeypatch.setenv(name, value)
        load_settings()
    for value in bad:
        monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError, match=name.lower()):
            load_settings()


# P9-S14: (variable, default, lowest, highest).
SITE_SETTINGS = (
    ("SITE_MAX_DEPTH", 5, 0, 10),
    ("SITE_MAX_PAGES", 1000, 1, 100_000),
    ("SITE_MAX_SITEMAP_FILES", 500, 1, 50_000),
    ("SITE_MAX_SITEMAP_URLS", 200_000, 1, 5_000_000),
    ("SITE_SITEMAP_MAX_BYTES", 52_428_800, 1024, 209_715_200),
    ("SITE_MAX_LINKS_PER_PAGE", 1000, 10, 50_000),
)


def test_site_settings_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    settings = load_settings()
    for name, default, _, _ in SITE_SETTINGS:
        assert getattr(settings, name.lower()) == default

    for name, _, lowest, highest in SITE_SETTINGS:
        for accepted in (lowest, highest):
            monkeypatch.setenv(name, str(accepted))
            assert getattr(load_settings(), name.lower()) == accepted
        for refused in (lowest - 1, highest + 1):
            monkeypatch.setenv(name, str(refused))
            with pytest.raises(ConfigError, match=name.lower()):
                load_settings()
        monkeypatch.delenv(name)

    monkeypatch.setenv("SITE_MAX_DEPTH", "11")
    with pytest.raises(ConfigError, match="site_max_depth"):
        load_settings()


# P10-S9: (variable, default, lowest, highest).
MCP_SETTINGS = (
    ("MCP_WAIT_SECONDS", 25, 0, 60),
    ("MCP_MAX_CONTENT_CHARS", 50_000, 1_000, 10_000_000),
    ("MCP_IMAGE_MAX_BYTES", 5_000_000, 0, 20_000_000),
)


def test_mcp_settings_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    settings = load_settings()
    assert settings.mcp_enabled is False
    assert settings.mcp_allowed_origins == []
    for name, default, _, _ in MCP_SETTINGS:
        assert getattr(settings, name.lower()) == default

    for name, _, lowest, highest in MCP_SETTINGS:
        for accepted in (lowest, highest):
            monkeypatch.setenv(name, str(accepted))
            assert getattr(load_settings(), name.lower()) == accepted
        for refused in (lowest - 1, highest + 1):
            monkeypatch.setenv(name, str(refused))
            with pytest.raises(ConfigError, match=name.lower()):
                load_settings()
        monkeypatch.delenv(name)

    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", " https://App.Example.org , http://localhost:3000/,,")
    settings = load_settings()
    assert settings.mcp_enabled is True
    assert settings.mcp_allowed_origins == ["https://app.example.org", "http://localhost:3000"]

    # A default port is dropped: a browser never sends it in Origin.
    monkeypatch.setenv(
        "MCP_ALLOWED_ORIGINS",
        "https://app.example:443,http://app.example:80,https://app.example:8443,http://b.example:443",
    )
    assert load_settings().mcp_allowed_origins == [
        "https://app.example",
        "http://app.example",
        "https://app.example:8443",
        "http://b.example:443",
    ]

    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "")
    assert load_settings().mcp_allowed_origins == []
    for wrong in ("app.example.org", "ftp://app.example.org", "https://a.example/path", "*"):
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", wrong)
        with pytest.raises(ConfigError, match="mcp_allowed_origins"):
            load_settings()
