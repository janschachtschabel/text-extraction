""".env.example is the template for docker compose: it names every setting, and a copy with
only the secrets filled in loads (a renamed or new setting fails here until the file follows)."""

import re
from pathlib import Path

import pytest

from text_extraction.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"

# Read by docker compose only: docker-compose.build.yml starts Valkey with VALKEY_PASSWORD and
# builds VALKEY_URL from it; the root docker-compose.yml reads the rest.
_COMPOSE_ONLY = {
    "VALKEY_PASSWORD",
    "DOMAIN",
    "TEXT_EXTRACTION_VERSION",
    "API_PORT",
    "TRAEFIK_NETWORK",
    "TRAEFIK_ROUTER",
    "TRAEFIK_ENTRYPOINT",
    "TRAEFIK_CERTRESOLVER",
}


def _text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def _active() -> dict[str, str]:
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", _text(), re.MULTILINE))


def _commented() -> set[str]:
    return set(re.findall(r"^#\s*([A-Z][A-Z0-9_]*)=", _text(), re.MULTILINE))


def test_env_example_lists_every_setting() -> None:
    settings = {name.upper() for name in Settings.model_fields}
    listed = set(_active()) | _commented()
    assert sorted(settings - listed) == []
    assert sorted(listed - settings - _COMPOSE_ONLY) == []


def test_env_example_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in _active().items():
        if name not in _COMPOSE_ONLY:
            monkeypatch.setenv(name, value)
    settings = Settings()
    assert settings.auth_mode == "api_key"


def test_env_example_spaces_requests_to_one_host_by_500_ms() -> None:
    assert _active()["OUTBOUND_MIN_DELAY_MS"] == "500"
