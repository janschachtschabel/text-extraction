"""The release metadata agrees: one version everywhere, a changelog section for it, Dependabot."""

import re
import tomllib
from pathlib import Path

import yaml

import text_extraction

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _project_version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def _chart() -> dict[str, str]:
    path = ROOT / "deploy" / "helm" / "text-extraction" / "Chart.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _lock_version() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (package,) = [p for p in lock["package"] if p["name"] == "text-extraction"]
    return str(package["version"])


def test_the_three_versions_are_equal_and_semver() -> None:
    chart = _chart()
    version = _project_version()
    assert SEMVER.match(version)
    assert chart["version"] == version
    assert chart["appVersion"] == version
    assert text_extraction.__version__ == version


def test_the_lock_carries_the_project_version() -> None:
    # `uv sync --locked` in CI fails when the lock's own entry lags behind pyproject.toml.
    assert _lock_version() == _project_version()


def test_the_changelog_has_a_section_for_the_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(_project_version())}\] - \S", changelog, re.MULTILINE)
    assert "Keep a Changelog" in changelog


def test_the_changelog_names_every_package_from_zero_to_eleven() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for number in range(12):
        assert re.search(rf"^- Package {number}\b", changelog, re.MULTILINE), number


def test_dependabot_watches_uv_actions_docker_and_compose() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert config["version"] == 2
    updates = {u["package-ecosystem"]: u for u in config["updates"]}
    assert set(updates) == {"uv", "github-actions", "docker", "docker-compose"}
    for update in updates.values():
        assert update["directory"] == "/"
        assert update["schedule"]["interval"] == "weekly"
        assert update["open-pull-requests-limit"] <= 5
        # CONTRIBUTING.md: a release gets two weeks before an upgrade takes it.
        assert update["cooldown"]["default-days"] == 14
        groups = update["groups"]
        assert groups, "minor and patch updates are grouped"
        for group in groups.values():
            assert set(group["update-types"]) == {"minor", "patch"}
