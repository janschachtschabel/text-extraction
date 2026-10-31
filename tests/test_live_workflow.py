"""The live workflow's manual inputs, gate and full-regression mode (package 11)."""

from pathlib import Path
from typing import Any

import yaml

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


def test_full_run_has_room_for_four_hours() -> None:
    timeout = str(_job()["timeout-minutes"])
    assert timeout.startswith("${{")
    assert "330" in timeout
    assert "110" in timeout
    assert "inputs.url_set == 'wlo_4000'" in timeout


def test_manual_runs_of_different_modes_do_not_cancel_each_other() -> None:
    group = _workflow()["concurrency"]["group"]
    assert "inputs.mode" in group
