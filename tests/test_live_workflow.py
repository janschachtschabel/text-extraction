"""The live workflow's manual inputs, gate, full-regression mode and concurrency groups
(package 11)."""

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


# --- the concurrency group, evaluated the way Actions does ----------------------------------------


_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||[(),])|(?P<name>[\w.\-]+))"
)


def _evaluate(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate the subset of the Actions expression syntax that the group uses: string
    literals, property paths (a missing one is null), `==`, `!=` (strings case-insensitively),
    `&&` and `||` (they return an operand, as in Actions), parentheses and `format`."""
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression.rstrip()):
        match = _TOKEN.match(expression, position)
        assert match, expression[position:]
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        position = match.end()

    def peek() -> tuple[str, str] | None:
        return tokens[0] if tokens else None

    def take(value: str | None = None) -> tuple[str, str]:
        token = tokens.pop(0)
        assert value is None or token[1] == value, (token, value)
        return token

    def lookup(path: str) -> Any:
        value: Any = context
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def truthy(value: Any) -> bool:
        return value not in (None, False, 0, "")

    def equal(left: Any, right: Any) -> bool:
        if isinstance(left, str) and isinstance(right, str):
            return left.casefold() == right.casefold()
        return bool(left == right)

    def primary() -> Any:
        kind, value = take()
        if kind == "str":
            return value[1:-1].replace("''", "'")
        if value == "(":
            inner = disjunction()
            take(")")
            return inner
        if value == "format":
            take("(")
            arguments = [disjunction()]
            while peek() == ("op", ","):
                take(",")
                arguments.append(disjunction())
            take(")")
            text = arguments[0]
            for index, argument in enumerate(arguments[1:]):
                text = text.replace(f"{{{index}}}", "" if argument is None else str(argument))
            return text
        assert kind == "name", value
        return lookup(value)

    def comparison() -> Any:
        left = primary()
        while peek() in (("op", "=="), ("op", "!=")):
            operator = take()[1]
            right = primary()
            left = equal(left, right) if operator == "==" else not equal(left, right)
        return left

    def conjunction() -> Any:
        left = comparison()
        while peek() == ("op", "&&"):
            take()
            right = comparison()
            left = right if truthy(left) else left
        return left

    def disjunction() -> Any:
        left = conjunction()
        while peek() == ("op", "||"):
            take()
            right = conjunction()
            left = left if truthy(left) else right
        return left

    result = disjunction()
    assert not tokens, tokens
    return result


def _group(event: str, *, ref: str = "refs/heads/main", **values: Any) -> str:
    """The concurrency group of a run: the template with every `${{ }}` evaluated."""
    inputs = {key: value for key, value in values.items() if key in _dispatch_inputs()}
    github = {
        "ref": ref,
        "run_id": values.get("run_id", "1"),
        "event_name": event,
        "event": {"action": values.get("action"), "label": {"name": values.get("label")}},
    }
    context = {"github": github, "inputs": inputs if event == "workflow_dispatch" else {}}
    template = str(_workflow()["concurrency"]["group"])
    return re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda match: str(_evaluate(match.group(1), context) or ""),
        template,
        flags=re.S,
    )


def _dispatch(url_set: str, mode: str = "auto", service_ref: str = "") -> str:
    return _group(
        "workflow_dispatch",
        url_set=url_set,
        mode=mode,
        min_ok_rate="80",
        service_ref=service_ref,
    )


def test_the_weekly_run_and_a_gate_dispatch_never_cancel_a_full_regression() -> None:
    """A full auto run on wlo_4000 takes hours: Monday's run and a wlo_200 dispatch on the
    same ref must not cancel it, nor it them."""
    full = _dispatch("wlo_4000")
    assert full != _group("schedule")
    assert full != _dispatch("wlo_200")
    assert full != _dispatch("smoke")
    # The three modes of the full regression keep groups of their own.
    assert len({full, _dispatch("wlo_4000", "fast"), _dispatch("wlo_4000", "browser")}) == 3
    # A second full auto run on the same ref still replaces the first.
    assert full == _dispatch("wlo_4000")


def test_the_gate_keeps_its_cancel_behaviour() -> None:
    """A new push to a pull request cancels the run in progress; another label neither cancels
    nor joins it; a gate dispatch and the weekly run on one ref share a group, as before."""
    ref = "refs/pull/25/merge"
    push = _group("pull_request", ref=ref, action="synchronize")
    assert push == "live-refs/pull/25/merge"
    assert _group("pull_request", ref=ref, action="labeled", label="live") == push
    other = _group("pull_request", ref=ref, action="labeled", label="docs", run_id="7")
    assert other == "live-refs/pull/25/merge-other-label-7"
    assert _group("schedule") == _dispatch("wlo_200") == "live-refs/heads/main"
    assert _dispatch("wlo_200", service_ref="v0.1.0") == "live-refs/heads/main-service-v0.1.0"
