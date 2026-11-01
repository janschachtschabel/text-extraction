"""The release notes of a tag (``scripts/release_notes.py``): the tag's section of CHANGELOG.md
with absolute links, then the published images and where to start; and the ``release`` workflow
that puts them on the GitHub release page."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
BLOB = "https://github.com/janschachtschabel/text-extraction/blob"

CHANGELOG = """# Changelog

Intro with a [link](docs/x.md).

## [Unreleased]

### Fixed

- Something later.

## [0.2.0] - 2026-12-01

### Added

- Package 14, see [packages](docs/packages.md#status) and [the site](https://example.org/).
- A [section](#added) of this file.

## [0.1.0] - 2026-11-01

### Added

- The first one.
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("release_notes", module)
    spec.loader.exec_module(module)
    return module


release_notes = _load()


def test_notes_hold_the_section_of_the_version() -> None:
    notes = release_notes.release_notes(CHANGELOG, "0.2.0")
    assert notes.startswith("### Added\n\n- Package 14")
    assert "## [0.2.0]" not in notes
    assert "The first one" not in notes
    assert "Something later" not in notes
    assert "Intro" not in notes


def test_the_last_section_ends_at_the_end_of_the_file() -> None:
    notes = release_notes.release_notes(CHANGELOG, "0.1.0")
    assert notes.startswith("### Added\n\n- The first one.\n")


def test_wrapped_lines_are_joined() -> None:
    # GitHub keeps every line break of a release text: a wrapped list item would break in two.
    changelog = (
        "## [1.0.0] - 2026-12-24\n\n### Added\n\n- One item that\n  goes on here.\n"
        "- Two.\n\nA paragraph\nin two lines.\n\n```\nkept\nas is\n```\n"
    )
    notes = release_notes.release_notes(changelog, "1.0.0")
    assert "- One item that goes on here.\n- Two.\n" in notes
    assert "A paragraph in two lines.\n" in notes
    assert "```\nkept\nas is\n```" in notes


def test_relative_links_point_at_the_tag() -> None:
    notes = release_notes.release_notes(CHANGELOG, "0.2.0")
    assert f"[packages]({BLOB}/v0.2.0/docs/packages.md#status)" in notes
    assert "[the site](https://example.org/)" in notes
    assert f"[section]({BLOB}/v0.2.0/CHANGELOG.md#added)" in notes


def test_notes_end_with_the_images_and_the_start() -> None:
    notes = release_notes.release_notes(CHANGELOG, "0.2.0")
    images = notes[notes.index("### Images") :]
    for suffix in ("", "-browser", "-docling", "-asr", "-nlp"):
        assert f"`ghcr.io/janschachtschabel/text-extraction{suffix}:0.2.0`" in images
    assert "`0.2`" in images
    assert "docker-compose.release.yml" in images
    assert f"{BLOB}/main/docs/user-guide/en/installation.md" in images
    assert f"[German]({BLOB}/main/docs/user-guide/de/installation.md)" in images
    assert f"{BLOB}/main/deploy/helm/text-extraction/README.md" in images


@pytest.mark.parametrize("version", ["9.9.9", "0.1", "Unreleased"])
def test_unknown_version_raises(version: str) -> None:
    with pytest.raises(LookupError):
        release_notes.release_notes(CHANGELOG, version)


def test_the_cli_prints_the_notes_of_the_real_changelog() -> None:
    done = subprocess.run(  # noqa: S603 - the repo's own script
        [sys.executable, str(SCRIPT), "0.1.0"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    assert "Package 13, YouTube transcript panel" in done.stdout
    assert "ghcr.io/janschachtschabel/text-extraction-browser:0.1.0" in done.stdout


def test_the_cli_exits_1_for_a_version_without_a_section() -> None:
    done = subprocess.run(  # noqa: S603 - the repo's own script
        [sys.executable, str(SCRIPT), "9.9.9"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert done.returncode == 1
    assert "9.9.9" in done.stderr
    assert done.stdout == ""


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_workflow_runs_on_release_tags_and_by_hand() -> None:
    workflow = _workflow()
    triggers = workflow[True]  # YAML reads the key `on` as True
    assert triggers["push"] == {"tags": ["v*.*.*"]}
    assert triggers["workflow_dispatch"]["inputs"]["tag"]["required"] is True
    assert workflow["permissions"] == {"contents": "write"}


def test_the_workflow_creates_or_updates_the_page() -> None:
    (job,) = _workflow()["jobs"].values()
    script = "\n".join(step.get("run", "") for step in job["steps"])
    env = {k: v for step in job["steps"] for k, v in step.get("env", {}).items()}
    # The tag reaches the shell through the environment, never spliced into the script.
    assert "${{" not in script
    assert env["TAG"] == "${{ inputs.tag || github.ref_name }}"
    assert "scripts/release_notes.py" in script
    assert "gh release create" in script
    assert "--verify-tag" in script
    assert "gh release edit" in script
    assert "--prerelease" in script
