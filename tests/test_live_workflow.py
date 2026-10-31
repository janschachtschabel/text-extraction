"""The live workflow's manual inputs, gate and full-regression mode (package 11)."""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

BASH = shutil.which("bash") or "/bin/bash"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "live.yml"


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _job() -> dict[str, Any]:
    job = _workflow()["jobs"]["live"]
    assert isinstance(job, dict)
    return job


def _steps() -> list[dict[str, Any]]:
    steps = _job()["steps"]
    assert isinstance(steps, list)
    return steps


def _dispatch_inputs() -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean True.
    workflow: dict[Any, Any] = _workflow()
    triggers = workflow[True] if True in workflow else workflow["on"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert isinstance(inputs, dict)
    return inputs


def _run_step() -> dict[str, Any]:
    matches = [
        s
        for s in _steps()
        if "tests.live.run_live" in s.get("run", "") and "--min-ok-rate" in s["run"]
    ]
    assert len(matches) == 1
    return matches[0]


def test_manual_run_offers_a_mode_input() -> None:
    mode = _dispatch_inputs()["mode"]
    assert mode["type"] == "choice"
    assert mode["options"] == ["auto", "fast", "browser"]
    assert mode["default"] == "auto"


def test_pull_request_gate_stays_auto_on_wlo_200() -> None:
    env = _job()["env"]
    assert env["URL_SET"] == "${{ inputs.url_set || 'wlo_200' }}"
    assert env["MIN_OK_RATE"] == "${{ inputs.min_ok_rate || '80' }}"
    assert env["MODE"] == "${{ inputs.mode || 'auto' }}"
    assert '--mode "$MODE"' in _run_step()["run"]


def test_full_run_skips_the_report_only_steps() -> None:
    report_only = [s for s in _steps() if "report only" in s.get("name", "")]
    assert len(report_only) == 4  # smoke, wlo_200 in browser mode, media, site structure
    for step in report_only:
        assert "env.URL_SET != 'wlo_4000'" in str(step["if"]), step["name"]


def _timeout_minutes(url_set: str) -> int:
    """Evaluate `${{ inputs.url_set == '<set>' && A || B }}` the way Actions does."""
    expression = str(_job()["timeout-minutes"])
    match = re.fullmatch(
        r"\$\{\{\s*inputs\.url_set == '(\w+)' && (\d+) \|\| (\d+)\s*\}\}", expression
    )
    assert match, expression
    full_set, if_full, otherwise = match.groups()
    return int(if_full) if url_set == full_set else int(otherwise)


def test_full_run_has_room_for_four_hours() -> None:
    assert _timeout_minutes("wlo_4000") == 330
    for url_set in ("smoke", "wlo_50", "wlo_200", ""):
        assert _timeout_minutes(url_set) == 110


def _run_arguments(mode: str, url_set: str = "wlo_200") -> list[str]:
    """Run the main step's script with a stub `uv` and return the arguments it was given."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "uv"
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
        stub.chmod(0o755)
        env = {
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "URL_SET": url_set,
            "MIN_OK_RATE": "80",
            "MODE": mode,
        }
        done = subprocess.run(  # noqa: S603 - the repo's own workflow script under a stub uv
            [BASH, "-e", "-c", _run_step()["run"]],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    return done.stdout.splitlines()


def test_only_the_auto_mode_run_is_gated() -> None:
    assert "--min-ok-rate" in _run_arguments("auto")
    assert "--min-ok-rate" not in _run_arguments("fast")
    assert "--min-ok-rate" not in _run_arguments("browser")
    assert "--min-ok-rate" not in _run_arguments("auto", url_set="smoke")


def test_browser_run_sends_as_many_requests_as_the_worker_renders_pages() -> None:
    steps = {s["name"]: s for s in _steps() if "name" in s}
    worker = steps["Start the service and the browser worker"]
    pages = worker["env"]["BROWSER_CONCURRENCY"]
    browser = _run_arguments("browser")
    assert browser[browser.index("--concurrency") + 1] == pages
    assert "--concurrency" not in _run_arguments("auto")
    assert "--concurrency" not in _run_arguments("fast")


def test_manual_runs_of_different_modes_do_not_cancel_each_other() -> None:
    group = _workflow()["concurrency"]["group"]
    assert "inputs.mode" in group
