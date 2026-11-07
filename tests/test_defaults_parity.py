"""The shipped defaults equal the code's: every active ``KEY=value`` of ``.env.example`` and every
Helm ``config:`` entry (``values.yaml`` and the production example) parses to what ``Settings``
defaults to, unless the difference is listed, with its reason, in ``INTENTIONAL_DIFFERENCES``.
A default changed in one place only fails here instead of drifting silently."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from text_extraction.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
HELM_VALUES = (
    ROOT / "deploy/helm/text-extraction/values.yaml",
    ROOT / "deploy/helm/text-extraction/values-production.example.yaml",
)

# Settings that may differ from the code's default on purpose, one entry per name. Extend it here
# (and only here) with a reason when a shipped file deliberately deviates.
INTENTIONAL_DIFFERENCES: dict[str, str] = {
    # docker compose starts the optional components, so .env.example turns them on (package 19);
    # the code's defaults are off so that a bare process starts the smallest service. Helm
    # switches the same components with `workers.<pool>.enabled`, not with config.
    "DOCLING_ENABLED": ".env.example: compose runs the Docling worker",
    "ASR_ENABLED": ".env.example: compose runs the ASR worker",
    "PII_ENABLED": ".env.example: compose runs the NLP worker",
    "LLM_ENABLED": ".env.example: compose runs the LLM worker",
    # Deployment-specific values with no useful code default.
    "API_KEYS": "secret: a placeholder key list in .env.example",
    "ASR_EXTERNAL_BASE_URL": "operator's endpoint, unset in the code",
    "LLM_EMBEDDING_MODEL": "operator's model name, unset in the code",
    "LLM_VISION_MODEL": "operator's model name, unset in the code",
    # Read by docker compose only (not a Settings field).
    "VALKEY_PASSWORD": "compose only",
    # api_key is the code default, but the check below needs a Settings without keys: it is
    # compared with the field default directly (see test_auth_mode_is_the_code_default).
    "AUTH_MODE": "compared with the field default directly",
}


def _env_example() -> dict[str, str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.MULTILINE))


def _helm_config(path: Path) -> dict[str, str]:
    values: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {name: str(value) for name, value in (values.get("config") or {}).items()}


def _entries() -> list[Any]:
    sources: list[tuple[str, dict[str, str]]] = [(".env.example", _env_example())]
    sources += [(str(path.relative_to(ROOT)), _helm_config(path)) for path in HELM_VALUES]
    return [
        pytest.param(name, value, id=f"{source}:{name}")
        for source, entries in sources
        for name, value in entries.items()
        if name not in INTENTIONAL_DIFFERENCES
    ]


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.setenv("AUTH_MODE", "none")  # api_key would need keys; the field is checked apart
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(("name", "value"), _entries())
def test_a_shipped_value_equals_the_code_default(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    field = name.lower()
    assert field in Settings.model_fields, f"{name} is not a setting"
    default = getattr(_settings(monkeypatch), field)
    monkeypatch.setenv(name, value)
    assert getattr(Settings(_env_file=None), field) == default  # type: ignore[call-arg]


def test_auth_mode_is_the_code_default() -> None:
    assert Settings.model_fields["auth_mode"].default == "api_key"
    shipped = {_env_example()["AUTH_MODE"]} | {
        _helm_config(path)["AUTH_MODE"] for path in HELM_VALUES
    }
    assert shipped == {"api_key"}


def test_every_difference_is_still_a_difference(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale allow-list entry hides nothing: drop it when the file follows the code."""
    shipped = _env_example()
    for path in HELM_VALUES:
        shipped |= _helm_config(path)
    stale = []
    for name in INTENTIONAL_DIFFERENCES:
        if name not in shipped or name in {"AUTH_MODE", "VALKEY_PASSWORD", "API_KEYS"}:
            continue
        monkeypatch.undo()
        default = getattr(_settings(monkeypatch), name.lower())
        monkeypatch.setenv(name, shipped[name])
        if getattr(Settings(_env_file=None), name.lower()) == default:  # type: ignore[call-arg]
            stale.append(name)
    assert stale == []
