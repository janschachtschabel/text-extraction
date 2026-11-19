"""SearXNG's settings.yml as this project ships it (package 25): the template that
``text-extraction-init`` renders into the compose stack and the Helm chart mounts. Only engines
with an official, free API are on (ruling D2 a), SEARCH_SOURCES names exactly them, and the
instance is a private backend of the API: JSON on, limiter, image proxy and Valkey off."""

from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from text_extraction.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
HELM_COPY = ROOT / "deploy/helm/text-extraction/files/searxng-settings.yml"
D2_ENGINES = [
    "wikipedia",
    "wikibooks",
    "wikiversity",
    "wikinews",
    "mwmbl",
    "openalex",
    "arxiv",
    "crossref",
]


def _text() -> str:
    return files("text_extraction").joinpath("data/searxng-settings.yml").read_text("utf-8")


def _settings() -> dict[str, Any]:
    return yaml.safe_load(_text())  # type: ignore[no-any-return]


def test_keeps_exactly_the_d2_engines() -> None:
    assert _settings()["use_default_settings"]["engines"]["keep_only"] == D2_ENGINES


def test_search_sources_default_equals_the_kept_engines() -> None:
    default = Settings.model_fields["search_sources"].default
    assert default == _settings()["use_default_settings"]["engines"]["keep_only"]


def test_every_kept_engine_is_switched_on() -> None:
    # Five of the eight are `disabled: true` in SearXNG's own settings.yml (2026.11.2): the
    # override switches each one on, so a search that names no engine reaches all eight too.
    engines = {engine["name"]: engine for engine in _settings()["engines"]}
    assert sorted(engines) == sorted(D2_ENGINES)
    for name in D2_ENGINES:
        assert engines[name]["disabled"] is False, name
    assert engines["wikipedia"]["display_type"] == ["list"]


def test_json_is_on() -> None:
    assert set(_settings()["search"]["formats"]) == {"html", "json"}


def test_a_private_backend_without_limiter_proxy_or_valkey() -> None:
    settings = _settings()
    server = settings["server"]
    assert server["limiter"] is False
    assert server["public_instance"] is False
    assert server["image_proxy"] is False
    assert settings["valkey"] == {"url": False}
    # The container's port is never published; the file binds nothing public either.
    assert server.get("bind_address", "127.0.0.1") == "127.0.0.1"
    # No autocomplete requests to a scraping backend.
    assert settings["search"]["autocomplete"] == ""


def test_secret_key_is_the_placeholder_init_replaces() -> None:
    # SearXNG refuses to start with this value, so an unrendered file never runs with a known key.
    assert _settings()["server"]["secret_key"] == "ultrasecretkey"  # noqa: S105 - placeholder
    assert _text().count("ultrasecretkey") == 1


def test_sources_answer_before_the_api_gives_up() -> None:
    # crossref waits 30 s by default; SearXNG caps every engine below SEARCH_TIMEOUT_SECONDS, so a
    # slow source lands in unresponsive_engines (partial) instead of failing the whole search.
    cap = _settings()["outgoing"]["max_request_timeout"]
    assert 0 < cap < Settings.model_fields["search_timeout_seconds"].default


def test_helm_chart_ships_the_same_file() -> None:
    assert HELM_COPY.read_text(encoding="utf-8") == _text()
