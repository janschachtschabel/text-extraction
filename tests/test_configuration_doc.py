"""docs/configuration.md documents every setting with its default, and every secret in its
"Secrets" table (ruling P11-S10): a renamed or new setting fails here until the doc follows."""

import re
from pathlib import Path
from typing import Any

from pydantic_core import PydanticUndefined

from text_extraction.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
CONFIGURATION = ROOT / "docs" / "configuration.md"

_ROW = re.compile(r"^\| `([A-Z0-9_]+)` \| ([^|]*) \|")


def _tables(header: str) -> dict[str, list[str]]:
    """The rows of every table whose header line starts with ``header``: variable -> the
    second cells of its rows."""
    rows: dict[str, list[str]] = {}
    in_table = False
    for line in CONFIGURATION.read_text(encoding="utf-8").splitlines():
        if line.startswith(header):
            in_table = True
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        match = _ROW.match(line)
        if in_table and match:
            rows.setdefault(match.group(1), []).append(match.group(2).strip())
    return rows


def _documented_forms(default: Any) -> set[str]:
    """How the doc writes a default in its "Default" column."""
    if default is None:
        return {"unset", "none"}
    if isinstance(default, bool):
        return {f"`{str(default).lower()}`"}
    if isinstance(default, list):
        return {f"`{','.join(str(item) for item in default)}`"} if default else {"empty"}
    return {f"`{default}`"}


def _docstring_secrets() -> set[str]:
    return set(re.findall(r"``([A-Z0-9_]+)``", Settings.__doc__ or ""))


def test_every_setting_is_documented_with_its_default() -> None:
    rows = _tables("| Variable | Default |")
    problems = []
    for name, field in Settings.model_fields.items():
        variable = name.upper()
        default = field.get_default(call_default_factory=True)
        assert default is not PydanticUndefined, f"{variable} has no default"
        if variable not in rows:
            problems.append(f"{variable}: no row")
        elif not _documented_forms(default) & set(rows[variable]):
            problems.append(f"{variable}: default {default!r}, documented {rows[variable]}")
    assert not problems, "\n".join(problems)


def test_every_documented_variable_is_a_setting() -> None:
    known = {name.upper() for name in Settings.model_fields}
    unknown = set(_tables("| Variable | Default |")) - known
    assert not unknown, f"documented but not a setting: {sorted(unknown)}"


def test_every_secret_is_in_the_secrets_table() -> None:
    secrets = _tables("| Variable | Content |")
    named = _docstring_secrets()
    assert named, "the Settings docstring names no secret"
    missing = sorted(named - set(secrets))
    assert not missing, f"secrets missing from the Secrets table: {missing}"


def test_every_secret_field_is_named_in_the_settings_docstring() -> None:
    secret_fields = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if not field.repr or "SecretStr" in str(field.annotation)
    } | {"API_KEYS", "API_KEYS_FILE"}
    missing = sorted(secret_fields - _docstring_secrets())
    assert not missing, f"secret settings the docstring does not name: {missing}"
