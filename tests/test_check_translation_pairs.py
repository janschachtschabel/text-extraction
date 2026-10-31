"""The pull-request check ``scripts/check_translation_pairs.py`` (ruling P12-S12.2): an English
file of a pair that changed without its German partner fails; a German-only change passes; the
trailer ``I18n-Unchanged: <path>`` in a commit of the range exempts that English path."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_translation_pairs.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"

EN_GUIDE = "docs/user-guide/en/usage.md"
DE_GUIDE = "docs/user-guide/de/usage.md"
EN_CATALOGUE = "src/text_extraction/ui/locales/en.json"
DE_CATALOGUE = "src/text_extraction/ui/locales/de.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_translation_pairs", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_translation_pairs", module)
    spec.loader.exec_module(module)
    return module


check_translation_pairs = _load()


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - git from PATH, with the test's own arguments
        ["git", "-c", "commit.gpgsign=false", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str, **files: str) -> str:
    for path, text in files.items():
        _write(repo, path, text)
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with both READMEs, a guide page and the catalogues, all committed; the
    working directory is its root."""
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.org")
    for path, text in {
        "README.md": "# Readme\n",
        "README.de.md": "# Liesmich\n",
        EN_GUIDE: "# Usage\n",
        DE_GUIDE: "# Nutzung\n",
        EN_CATALOGUE: '{"a.b": "A"}\n',
        DE_CATALOGUE: '{"a.b": "A"}\n',
        "src/code.py": "x = 1\n",
    }.items():
        _write(tmp_path, path, text)
    _git(tmp_path, "add", "--all")
    _git(tmp_path, "commit", "--quiet", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def test_english_only_change_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _base(repo)
    _commit(repo, "docs: reword usage", **{EN_GUIDE: "# Usage\n\nMore.\n"})

    assert check_translation_pairs.main(["--base", base]) == 1
    out = capsys.readouterr().out
    assert EN_GUIDE in out
    assert DE_GUIDE in out


def test_both_changed_passes(repo: Path) -> None:
    base = _base(repo)
    _commit(repo, "docs: reword usage", **{EN_GUIDE: "# Usage\n\nMore.\n"})
    _commit(repo, "docs: translate", **{DE_GUIDE: "# Nutzung\n\nMehr.\n"})
    _commit(repo, "docs: readme", **{"README.md": "# R\n", "README.de.md": "# L\n"})

    assert check_translation_pairs.main(["--base", base]) == 0


def test_german_only_change_passes(repo: Path) -> None:
    base = _base(repo)
    _commit(repo, "docs: fix a German typo", **{DE_GUIDE: "# Nutzung\n\nKorrektur.\n"})
    _commit(repo, "code", **{"src/code.py": "x = 2\n"})

    assert check_translation_pairs.main(["--base", base]) == 0


def test_trailer_exempts_a_path(repo: Path) -> None:
    base = _base(repo)
    _commit(
        repo,
        f"docs: fix a typo\n\nI18n-Unchanged: {EN_GUIDE}",
        **{EN_GUIDE: "# Usage\n\nTypo fixed.\n"},
    )
    assert check_translation_pairs.main(["--base", base]) == 0

    # The trailer exempts only the path it names.
    _commit(repo, "docs: readme", **{"README.md": "# Readme, changed\n"})
    assert check_translation_pairs.main(["--base", base]) == 1


def test_catalogue_pair_is_checked(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _base(repo)
    _commit(repo, "ui: new key", **{EN_CATALOGUE: '{"a.b": "A", "c.d": "C"}\n'})

    assert check_translation_pairs.main(["--base", base]) == 1
    assert DE_CATALOGUE in capsys.readouterr().out

    _commit(repo, "ui: German key", **{DE_CATALOGUE: '{"a.b": "A", "c.d": "C"}\n'})
    assert check_translation_pairs.main(["--base", base]) == 0


def test_a_new_english_page_needs_its_german_partner(repo: Path) -> None:
    base = _base(repo)
    _commit(repo, "docs: new page", **{"docs/user-guide/en/mcp.md": "# MCP\n"})

    assert check_translation_pairs.main(["--base", base]) == 1


def test_a_non_ascii_path_is_checked(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """git quotes such a path by default ("docs/user-guide/en/\\303\\274.md"); the check must
    see the real name, so the page still needs its German partner."""
    base = _base(repo)
    _commit(repo, "docs: new page", **{"docs/user-guide/en/ü.md": "# Ü\n"})

    assert check_translation_pairs.main(["--base", base]) == 1
    assert "docs/user-guide/de/ü.md" in capsys.readouterr().out

    _commit(repo, "docs: its German page", **{"docs/user-guide/de/ü.md": "# Ü\n"})
    assert check_translation_pairs.main(["--base", base]) == 0


def test_ci_runs_the_check_on_pull_requests() -> None:
    workflow: dict[Any, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["ci"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    assert "pull_request' && '0'" in checkout["with"]["fetch-depth"]
    check = next(step for step in steps if "check_translation_pairs.py" in step.get("run", ""))
    assert check["if"] == "github.event_name == 'pull_request'"
    assert check["run"] == (
        "uv run python scripts/check_translation_pairs.py "
        "--base ${{ github.event.pull_request.base.sha }}"
    )
