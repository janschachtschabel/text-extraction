"""The load workflow and its compose override (package 11): triggers, inputs, masking, the stack
it starts, the selector sweep and the local site farm."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

from tests.load import run_load
from tests.load.scenarios import SCENARIOS
from text_extraction.settings import Settings

BASH = shutil.which("bash") or "/bin/bash"
REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "load.yml"
OVERRIDE = REPO / "deploy" / "load" / "compose.load.yml"
BASE = REPO / "docker-compose.yml"
CI = REPO / ".github" / "workflows" / "ci.yml"
COMPOSE_ARGS = (
    "-f docker-compose.yml -f deploy/load/compose.load.yml"
    " --profile docling --profile asr --profile nlp --profile llm"
)
SELECTOR_SCENARIO = "structured-selectors-200"
TEST_SERVICES = {"farm", "fake-llm"}


def _load(path: Path) -> dict[Any, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _workflow() -> dict[Any, Any]:
    return _load(WORKFLOW)


def _triggers() -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean True.
    workflow = _workflow()
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert isinstance(triggers, dict)
    return triggers


def _job() -> dict[str, Any]:
    job = _workflow()["jobs"]["load"]
    assert isinstance(job, dict)
    return job


def _steps() -> list[dict[str, Any]]:
    steps = _job()["steps"]
    assert isinstance(steps, list)
    return steps


def _step(name: str) -> dict[str, Any]:
    matches = [s for s in _steps() if s.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def _override_services() -> dict[str, dict[str, Any]]:
    services = _load(OVERRIDE)["services"]
    assert isinstance(services, dict)
    return services


def _app_services() -> set[str]:
    """The base file's services that run the service's code (all but Valkey)."""
    return set(_load(BASE)["services"]) - {"valkey"}


def _interpolate(value: str) -> str:
    """Resolve compose's ``${NAME:-default}`` to its default, as with an empty environment."""
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", value)


def _app_env(service: str) -> dict[str, str]:
    environment = _override_services()[service]["environment"]
    assert isinstance(environment, dict)
    return {key: _interpolate(str(value)) for key, value in environment.items()}


# --- triggers, inputs and the job ---------------------------------------------------------------


def test_load_runs_only_by_hand_or_by_the_load_label() -> None:
    """A dispatch works only once the file is on the default branch, so a pull request that
    carries the label `load` runs it too (ruling R-P11-5); nothing else starts it."""
    triggers = _triggers()
    assert set(triggers) == {"workflow_dispatch", "pull_request"}
    assert triggers["pull_request"]["types"] == ["labeled", "synchronize", "opened", "reopened"]
    condition = " ".join(_job()["if"].split())
    assert "github.event_name != 'pull_request'" in condition
    assert "contains(github.event.pull_request.labels.*.name, 'load')" in condition
    assert "github.event.label.name == 'load'" in condition


def test_another_label_neither_runs_nor_cancels_a_load_run() -> None:
    concurrency = _workflow()["concurrency"]
    group = " ".join(concurrency["group"].split())
    assert group.startswith("load-${{ github.ref }}")
    assert "github.event.label.name != 'load'" in group
    assert "github.run_id" in group
    assert concurrency["cancel-in-progress"] is True


def test_load_offers_the_sweep_and_model_inputs() -> None:
    inputs = _triggers()["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"selector_processes", "asr_model", "pii_model_size"}
    assert inputs["selector_processes"]["type"] == "string"
    assert inputs["selector_processes"]["default"] == "1,2,4"
    assert inputs["asr_model"]["type"] == "choice"
    assert inputs["asr_model"]["options"] == ["small", "large-v3-turbo"]
    assert inputs["asr_model"]["default"] == "large-v3-turbo"
    assert inputs["pii_model_size"]["type"] == "choice"
    assert inputs["pii_model_size"]["options"] == ["md", "lg"]
    # Ruling R-P11-6: a run without inputs measures the production defaults.
    assert inputs["pii_model_size"]["default"] == "lg"


def test_a_label_run_uses_the_input_defaults() -> None:
    """A pull request has no inputs: the job's env falls back to the dispatch defaults."""
    env = _job()["env"]
    assert env["SELECTOR_PROCESSES"] == "${{ inputs.selector_processes || '1,2,4' }}"
    assert env["ASR_MODEL"] == "${{ inputs.asr_model || 'large-v3-turbo' }}"
    assert env["PII_MODEL_SIZE"] == "${{ inputs.pii_model_size || 'lg' }}"


def test_load_has_two_hours_and_reads_the_repo_only() -> None:
    assert _job()["timeout-minutes"] == 120
    assert _workflow()["permissions"] == {"contents": "read"}
    checkout = _steps()[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["persist-credentials"] is False


def test_load_pins_the_compose_project_name() -> None:
    """The sizing report strips this prefix from the container names (report.service_of)."""
    assert _job()["env"]["COMPOSE_PROJECT_NAME"] == "text-extraction"


def test_load_stack_uses_the_override_and_all_profiles() -> None:
    assert _job()["env"]["COMPOSE"] == f"docker compose {COMPOSE_ARGS}"
    # Every compose call goes through $COMPOSE, so none misses the override or a profile.
    for step in _steps():
        assert "docker compose" not in step.get("run", ""), step.get("name")
    assert "$COMPOSE build" in _step("Build the images")["run"]
    assert "$COMPOSE up -d --wait" in _step("Run the load test")["run"]


def test_load_masks_the_api_key() -> None:
    run = _step("Create .env with a fresh Valkey password and API key")["run"]
    lines = run.splitlines()
    for name in ("api_key", "valkey_password"):
        uses = [i for i, line in enumerate(lines) if f"${name}" in line]
        assert uses, name
        assert lines[uses[0]].strip() == f'echo "::add-mask::${name}"', name
    assert "openssl rand -hex 32" in run
    assert "openssl rand -hex 24" in run
    # Only the hash of the key reaches the service.
    assert 'printf %s "$api_key" | sha256sum' in run


def test_load_uploads_no_inspect_output() -> None:
    uploads = [s for s in _steps() if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "load-results"
    assert upload["with"]["path"].split() == ["load-results/**"]
    for step in _steps():
        run = step.get("run", "")
        assert "inspect" not in run, step.get("name")
        assert "$COMPOSE config" not in run, step.get("name")  # it prints the interpolated .env


def test_load_writes_the_summary_and_always_tears_down() -> None:
    summary = _step("Add the summaries to the job summary")
    assert summary["if"] == "always()"
    assert "load-results/summary.md" in summary["run"]
    assert "$GITHUB_STEP_SUMMARY" in summary["run"]
    teardown = _steps()[-1]
    assert teardown["if"] == "always()"
    assert "$COMPOSE down -v" in teardown["run"]


# --- the run step: the selector sweep -----------------------------------------------------------


API_KEY = "a1" * 32
VALKEY_PASSWORD = "b2" * 24
# `docker compose ps -a --format json` while the stack is not ready: one JSON object per line.
PS_JSON = "\n".join(
    [
        '{"Service":"api","State":"running","Health":"healthy","ExitCode":0}',
        '{"Service":"worker","State":"running","Health":"unhealthy","ExitCode":0}',
        '{"Service":"docling-models","State":"exited","Health":"","ExitCode":0}',
        '{"Service":"nlp-models","State":"exited","Health":"","ExitCode":1}',
    ]
)


class Sweep(NamedTuple):
    code: int
    calls: list[str]  # docker/uv calls, then `log <path>` and `record <path>` of saved files
    output: str  # stdout and stderr
    files: dict[str, str]  # every file under load-results/, by path


def _run_sweep(values: str, *, fail_up: bool = False, leak: bool = True) -> Sweep:
    """Run the run step under stub `docker` and `uv`, with a .env and API_KEY as the workflow
    writes them. With ``leak`` the stub logs of every service carry both secrets."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        calls = Path(tmp) / "calls"
        up = "exit 1" if fail_up else "exit 0"
        (bin_dir / "docker").write_text(
            "#!/bin/sh\n"
            f'echo "docker $* SP=$STRUCTURED_SELECTOR_PROCESSES" >> "{calls}"\n'
            'case "$*" in\n'
            '  *" ps -a --format json"*) printf "%s\\n" "$STUB_PS_JSON";;\n'
            '  *" ps --all --services"*) printf "api\\nworker\\ndocling-models\\nnlp-models\\n";;\n'
            '  *" ps -a"*) echo "NAME STATUS"; echo "stub-ps-table";;\n'
            '  *" logs "*) for last; do :; done\n'
            '    if [ -n "$STUB_LEAK" ]; then\n'
            '      echo "$last started key=$API_KEY password=$STUB_VALKEY_PASSWORD"\n'
            '    else echo "$last started"; fi;;\n'
            # A stopped farm writes its record into the bind-mounted directory.
            '  *" stop farm"*) echo "{}" > load-results/farm/farm-record.json;;\n'
            f'  *" up "*) {up};;\n'
            "esac\n",
            encoding="utf-8",
        )
        (bin_dir / "uv").write_text(f'#!/bin/sh\necho "uv $*" >> "{calls}"\n', encoding="utf-8")
        for stub in bin_dir.iterdir():
            stub.chmod(0o755)
        (Path(tmp) / ".env").write_text(
            f"VALKEY_PASSWORD={VALKEY_PASSWORD}\nAUTH_MODE=api_key\n", encoding="utf-8"
        )
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "COMPOSE": f"docker compose {COMPOSE_ARGS}",
            "SELECTOR_PROCESSES": values,
            "INTERNAL_URLS": _job()["env"]["INTERNAL_URLS"],
            "API_KEY": API_KEY,
            "STUB_VALKEY_PASSWORD": VALKEY_PASSWORD,
            "STUB_PS_JSON": PS_JSON,
            "STUB_LEAK": "1" if leak else "",
        }
        done = subprocess.run(  # noqa: S603 - the repo's own workflow script under stubs
            [BASH, "-e", "-o", "pipefail", "-c", _step("Run the load test")["run"]],
            env=env,
            cwd=tmp,
            capture_output=True,
            text=True,
            check=False,
        )
        lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        logs = sorted(str(p.relative_to(tmp)) for p in Path(tmp).glob("load-results/logs/*/*"))
        records = sorted(str(p.relative_to(tmp)) for p in Path(tmp).glob("load-results/farm/*"))
        files = {
            str(p.relative_to(tmp)): p.read_text(encoding="utf-8")
            for p in Path(tmp).glob("load-results/**/*")
            if p.is_file()
        }
        farm_dir = Path(tmp) / "load-results" / "farm"
        if farm_dir.exists():
            # The farm runs as uid 10001 and writes into the runner's directory.
            assert farm_dir.stat().st_mode & 0o777 == 0o777
    lines += [f"log {p}" for p in logs] + [f"record {p}" for p in records]
    return Sweep(done.returncode, lines, done.stdout + done.stderr, files)


def test_the_full_profile_runs_once_and_every_value_gets_a_fresh_selector_run() -> None:
    """The full profile runs at the production default (1); its selector scenario is not part
    of the sweep. Every value, the first included, measures the selector scenario alone on
    freshly recreated containers, so the per-child memory compares like with like."""
    code, calls, _, files = _run_sweep("1, 2,4")

    assert code == 0
    ups = [c for c in calls if " up " in c]
    assert [c.rsplit("SP=", 1)[1] for c in ups] == ["1", "1", "2", "4"]
    assert all("--force-recreate" in c for c in ups)
    runs = [c for c in calls if c.startswith("uv ")]
    assert len(runs) == 4
    assert "--only" not in runs[0]
    assert "--out-dir load-results " in runs[0] + " "
    assert "--selector-processes 1" in runs[0]
    for value, run in zip(("1", "2", "4"), runs[1:], strict=True):
        assert f"--only {SELECTOR_SCENARIO}" in run
        assert f"--out-dir load-results/selector-processes-{value}" in run
        assert f"--selector-processes {value}" in run
    # The logs and the farm record of each run are saved before the next up recreates them.
    assert "log load-results/logs/full/api.log" in calls
    assert "log load-results/logs/selector-processes-1/api.log" in calls
    assert "log load-results/logs/selector-processes-4/worker.log" in calls
    assert "worker started" in files["load-results/logs/selector-processes-4/worker.log"]
    assert [c for c in calls if c.startswith("record ")] == [
        f"record load-results/farm/{run}.json"
        for run in ("full", "selector-processes-1", "selector-processes-2", "selector-processes-4")
    ]
    stops = [i for i, c in enumerate(calls) if " stop farm" in c]
    up_at = [i for i, c in enumerate(calls) if " up " in c]
    assert len(stops) == 4
    assert all(up > stop for up, stop in zip(up_at[1:], stops, strict=False))


def test_the_run_targets_the_farm_the_fake_llm_and_every_internal_port() -> None:
    calls = _run_sweep("1").calls
    runs = [c for c in calls if c.startswith("uv ")]
    assert len(runs) == 2  # the full profile and the sweep's one value
    for run in runs:
        _check_targets(run)


def _check_targets(run: str) -> None:
    assert "-m tests.load.run_load" in run
    assert "--base-url http://127.0.0.1:8080" in run
    assert "--farm-url http://farm:8000" in run
    assert run_load.is_local_url("http://farm:8000")
    assert "--fake-llm-url http://127.0.0.1:8000" in run
    internal = _job()["env"]["INTERNAL_URLS"].split(",")
    assert f"--internal-urls {','.join(internal)}" in run
    published = sorted(
        port.split(":")[1]
        for name, service in _override_services().items()
        if name not in TEST_SERVICES
        for port in service.get("ports", [])
    )
    assert sorted(url.rsplit(":", 1)[1] for url in internal) == published


def test_the_run_reads_valkey_inside_its_container() -> None:
    """The crawls' Valkey bytes come from valkey-cli in the valkey container, which
    authenticates from its own REDISCLI_AUTH: the password is on no command line."""
    for run in [c for c in _run_sweep("1").calls if c.startswith("uv ")]:
        assert f"--valkey-cli docker compose {COMPOSE_ARGS} exec -T valkey valkey-cli" in run
        assert "VALKEY_PASSWORD" not in run


def test_a_value_that_is_not_a_whole_number_starts_nothing() -> None:
    code, calls, stderr, _ = _run_sweep("1,x")
    assert code != 0
    assert calls == []
    assert "'x'" in stderr


def test_a_repeated_value_starts_nothing() -> None:
    """A second run of a value would overwrite the first one's results and logs."""
    code, calls, stderr, _ = _run_sweep("1,2,1")
    assert code != 0
    assert calls == []
    assert "'1'" in stderr
    assert "more than once" in stderr


def test_a_stack_that_does_not_come_up_fails_the_run_with_its_logs() -> None:
    code, calls, output, files = _run_sweep("1,2", fail_up=True)
    assert code != 0
    assert not [c for c in calls if c.startswith("uv ")]
    assert "log load-results/logs/full/api.log" in calls
    # The job log shows the state of every container and the tail of the ones that failed:
    # unhealthy, or exited with an error. Healthy ones and finished one-shots stay out.
    assert "stub-ps-table" in output
    assert "worker started" in output
    assert "nlp-models started" in output
    assert "api started" not in output
    assert "docling-models started" not in output
    _assert_no_secret(output, files)


def _assert_no_secret(output: str, files: dict[str, str]) -> None:
    for secret in (API_KEY, VALKEY_PASSWORD):
        assert secret not in output
        for path, text in files.items():
            assert secret not in text, path
    assert "key=*** password=***" in files["load-results/logs/full/worker.log"]


def test_logs_without_a_secret_pass_unchanged() -> None:
    code, _, output, files = _run_sweep("1,2", leak=False)
    assert code == 0, output
    assert files["load-results/logs/full/worker.log"] == "worker started\n"
    assert "removed a secret" not in output


def test_saved_logs_hold_no_secret() -> None:
    """The artifact is not masked like the job log: the saved logs are redacted."""
    code, _, output, files = _run_sweep("1")
    assert code == 0
    _assert_no_secret(output, files)


# --- the compose override -----------------------------------------------------------------------


def test_farm_sets_no_proxy() -> None:
    # Parsed, so the comments do not count; no key or value names a proxy.
    assert "proxy" not in yaml.safe_dump(_load(OVERRIDE)).lower()
    for name, service in _override_services().items():
        environment = service.get("environment", {})
        assert not [k for k in environment if k.lower().endswith("_proxy")], name


def test_override_covers_every_app_service_with_the_same_settings() -> None:
    services = _override_services()
    assert _app_services() <= set(services)
    assert set(services) - _app_services() == TEST_SERVICES
    first = _app_env("api")
    for name in _app_services() - {"api"}:
        env = _app_env(name)
        assert env == {k: v for k, v in first.items() if k != "MCP_ENABLED"}, name
    assert first["MCP_ENABLED"] == "true"


def test_override_lifts_the_limits_and_switches_every_feature_on() -> None:
    """We size the service, not the politeness limits; the farm is ours (ruling R-P11-4)."""
    env = _app_env("api")
    most_at_once = max(s.concurrency for s in SCENARIOS)
    assert int(env["OUTBOUND_MAX_CONCURRENCY_PER_HOST"]) >= most_at_once
    assert env["OUTBOUND_MIN_DELAY_MS"] == "0"
    assert env["RATE_LIMIT_ENABLED"] == "false"
    assert env["FETCH_ALLOW_PRIVATE_NETWORKS"] == "true"
    for feature in ("DOCLING_ENABLED", "ASR_ENABLED", "PII_ENABLED", "LLM_ENABLED"):
        assert env[feature] == "true", feature
    assert env["LLM_BASE_URL"] == "http://fake-llm:8000/v1"
    assert env["LLM_ALLOW_HTTP"] == "true"
    assert env["LLM_API_KEY"]
    assert env["LLM_EMBEDDING_MODEL"]


def test_override_takes_the_models_and_the_sweep_value_from_the_workflow() -> None:
    environment = _override_services()["api"]["environment"]
    assert environment["ASR_MODEL"] == "${ASR_MODEL:-large-v3-turbo}"
    assert environment["PII_MODEL_SIZE"] == "${PII_MODEL_SIZE:-lg}"
    assert environment["STRUCTURED_SELECTOR_PROCESSES"] == "${STRUCTURED_SELECTOR_PROCESSES:-1}"


def test_override_settings_are_real_and_valid() -> None:
    """Every name is a field of Settings (no typo is ignored silently) and the set validates."""
    env = _app_env("api")
    fields = {name.upper() for name in Settings.model_fields}
    assert set(env) <= fields, set(env) - fields
    # The API keys come from .env, which the workflow writes; here authentication is off.
    values: dict[str, Any] = {k.lower(): v for k, v in env.items()}
    settings = Settings(auth_mode="none", **values)
    assert settings.llm_enabled
    assert settings.pii_model_size == "lg"


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)s", duration)
    assert match, duration
    return int(match.group(1))


def test_slow_starts_under_contention_do_not_count_as_unhealthy() -> None:
    """Every model loads at once on a 4-vCPU runner, and the workers start beside them: the
    first load-label run saw the extract worker unhealthy 58 s after its start. The override
    gives every health check a long start period, polled every 5 s so a ready container counts
    as healthy at once, and more retries; the check itself stays the base file's."""
    base = _load(BASE)["services"]
    checked = {name for name, service in base.items() if "healthcheck" in service} - {"valkey"}
    assert checked == {name for name in _app_services() if not name.endswith("-models")}
    for name in checked:
        healthcheck = _override_services()[name]["healthcheck"]
        assert "test" not in healthcheck, name  # merged: the base file's test stays
        assert _seconds(healthcheck["start_period"]) >= 600, name
        assert healthcheck["start_interval"] == "5s", name
        assert healthcheck["retries"] > base[name]["healthcheck"]["retries"], name


def test_internal_ports_are_published_on_loopback_only() -> None:
    published: list[str] = []
    for name, service in _override_services().items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)
            published.append(port.split(":")[1])
        if name in _app_services():
            assert [p.rsplit(":", 1)[1] for p in service.get("ports", [])] in (["9090"], []), name
    assert len(published) == len(set(published))
    assert "ports" not in _override_services()["farm"]


@pytest.mark.parametrize(
    ("name", "module", "port"),
    [("farm", "tests.e2e.site_farm", "8000"), ("fake-llm", "tests.load.fake_llm", "8000")],
)
def test_test_services_run_from_the_api_image_with_the_tests_read_only(
    name: str, module: str, port: str
) -> None:
    service = _override_services()[name]
    assert service["image"] == "text-extraction:dev"
    assert "build" not in service
    assert service["command"][:3] == ["python", "-m", module]
    assert service["command"][service["command"].index("--port") + 1] == port
    assert service["working_dir"] == "/app"
    # Relative paths resolve against the first compose file's directory: the repo root.
    assert service["volumes"][0] == "./tests:/app/tests:ro"
    assert service["volumes"][1:] == (["./load-results/farm:/records"] if name == "farm" else [])
    assert service["read_only"] is True
    assert "env_file" not in service
    assert f"127.0.0.1:{port}" in " ".join(service["healthcheck"]["test"])


def test_farm_listens_on_every_interface_of_its_container() -> None:
    command = _override_services()["farm"]["command"]
    assert command[command.index("--bind") + 1] == "0.0.0.0"  # noqa: S104 - inside the container


def test_farm_serves_the_sitemap_sites() -> None:
    """The site scenarios start on the farm's ports 8001 to 8003, whose robots.txt lists the
    large sitemaps (``site_farm.SITEMAP_SITES``); the health check stays on 8000."""
    command = _override_services()["farm"]["command"]
    assert "--sitemap-sites" in command
    # The sitemaps are built for the origin the service asks for, http://farm:800N.
    assert command[command.index("--sitemap-host") + 1] == "farm"


def test_farm_writes_its_record_where_the_artifact_collects_it() -> None:
    """The record (every request the farm saw) is the evidence that the load reached the farm;
    the tmpfs would lose it with the container."""
    command = _override_services()["farm"]["command"]
    assert command[command.index("--record") + 1] == "/records/farm-record.json"


def test_ci_checks_the_override() -> None:
    job = _load(CI)["jobs"]["compose"]
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert f"docker compose {COMPOSE_ARGS} config" in " ".join(runs.split())


# --- the site farm's bind address ---------------------------------------------------------------


def test_farm_serves_one_host_on_the_bind_address(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    farm = subprocess.Popen(  # noqa: S603 - the repo's own farm
        [
            sys.executable,
            "-m",
            "tests.e2e.site_farm",
            "--bind",
            "127.0.0.1",
            "--port",
            "0",
            "--record",
            str(record),
        ],
        cwd=REPO,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert farm.stdout is not None
        # Port 0: the system picks a free port (no race with another process), the farm says it.
        ready, _, port = farm.stdout.readline().strip().partition(" port=")
        assert ready == "ready"
        assert port.isdigit()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/p/7.html", timeout=5) as answer:
            assert b"Reading room report 7" in answer.read()
    finally:
        farm.send_signal(signal.SIGTERM)
        farm.wait(timeout=10)
    hosts = json.loads(record.read_text(encoding="utf-8"))["hosts"]
    assert list(hosts) == ["127.0.0.1"]
    assert hosts["127.0.0.1"][0]["path"] == "/p/7.html"
