"""The workflows pin every action to a commit and keep the registry write out of pull requests."""

import re
from pathlib import Path

import yaml

WORKFLOWS = sorted(Path(__file__).resolve().parents[1].joinpath(".github/workflows").glob("*.yml"))
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40} +# v\d+\.\d+\.\d+$")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S.*?)\s*$")


def _uses_lines() -> list[tuple[str, int, str]]:
    found = []
    for path in WORKFLOWS:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = USES.match(line)
            if match and not match.group(1).startswith("./"):
                found.append((path.name, number, match.group(1)))
    return found


def test_actions_are_pinned_to_commits() -> None:
    uses = _uses_lines()
    assert uses
    loose = [f"{name}:{number}: {value}" for name, number, value in uses if not PINNED.match(value)]
    assert not loose, "pin to a 40-hex commit with a `# vX.Y.Z` comment:\n" + "\n".join(loose)


def test_only_the_push_job_holds_packages_write() -> None:
    workflow = yaml.safe_load(Path(WORKFLOWS[0].parent / "image.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    writers = [
        name
        for name, job in workflow["jobs"].items()
        if (job.get("permissions") or {}).get("packages") == "write"
    ]
    assert len(writers) == 1
    push = workflow["jobs"][writers[0]]
    assert push["if"] == "github.event_name != 'pull_request'"
    assert "image" in push["needs"]
    assert workflow["jobs"]["image"]["permissions"]["packages"] == "read"
    assert "docker/login-action" not in str(workflow["jobs"]["image"])
