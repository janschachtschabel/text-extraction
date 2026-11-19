"""The CI checks of the web search (package 25): live.yml's steps against a real SearXNG, its
compose override, and image.yml's start of the root file's SearXNG with the init of the image
just built. They need the internet or Docker, so these tests check their wiring."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
LIVE_OVERRIDE = ROOT / "tests" / "live" / "compose.searxng.yml"
IMAGE_OVERRIDE = ROOT / "tests" / "image" / "compose.searxng-check.yml"
COMPOSE_FILE = "docker-compose.build.yml:tests/live/compose.searxng.yml"


class _ComposeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Reads compose's merge tags `!reset` and `!override` as ("!tag", value)."""


def _tagged(loader: yaml.SafeLoader, node: yaml.Node) -> tuple[str, Any]:
    if isinstance(node, yaml.SequenceNode):
        return node.tag, loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return node.tag, loader.construct_mapping(node)
    return node.tag, loader.construct_scalar(node)


_ComposeLoader.add_constructor("!reset", _tagged)
_ComposeLoader.add_constructor("!override", _tagged)


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)  # noqa: S506
    assert isinstance(loaded, dict)
    return loaded


def _steps(workflow: str, job: str) -> list[dict[str, Any]]:
    loaded = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    steps = loaded["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


def _named(steps: list[dict[str, Any]], name: str) -> tuple[int, dict[str, Any]]:
    [(index, step)] = [(i, s) for i, s in enumerate(steps) if s.get("name") == name]
    return index, step


# --- live.yml ------------------------------------------------------------------------------------


def test_live_override_keeps_the_hardening_and_mirrors_the_root_mount() -> None:
    searxng = _load(LIVE_OVERRIDE)["services"]["searxng"]

    # Only what the check needs: the port on the runner's loopback, the root file's read-only
    # folder mount, and the image's own entry point (the key comes from the rendered file).
    assert set(searxng) == {"entrypoint", "ports", "volumes"}
    assert searxng["entrypoint"] == ("!reset", "null")
    assert searxng["ports"] == ["127.0.0.1:8888:8080"]
    tag, volumes = searxng["volumes"]
    assert tag == "!override"
    assert len(volumes) == 1
    assert volumes[0].startswith("${SEARXNG_CONFIG_DIR:?")
    assert volumes[0].endswith("}:/etc/searxng:ro")


def test_live_web_search_steps_follow_the_gate_and_fail_the_job() -> None:
    steps = _steps("live.yml", "live")
    gate = next(i for i, s in enumerate(steps) if "--min-ok-rate" in s.get("run", ""))
    setup_at, setup = _named(steps, "Web search: start SearXNG and a search API")
    hits_at, hits = _named(steps, "Web search: hits through REST and MCP")
    down_at, down = _named(steps, "Web search: SearXNG stopped answers search_failed")

    assert gate < setup_at < hits_at < down_at
    for step in (setup, hits, down):
        assert "continue-on-error" not in step, step["name"]
        assert "!cancelled()" in step["if"], step["name"]
    assert setup["id"] == "search_setup"
    assert "env.URL_SET != 'wlo_4000'" in setup["if"]
    assert "inputs.service_ref == ''" in setup["if"]
    for step in (hits, down):
        assert "steps.search_setup.outcome == 'success'" in step["if"], step["name"]


def test_live_search_api_runs_from_source_with_search_and_mcp() -> None:
    _, setup = _named(_steps("live.yml", "live"), "Web search: start SearXNG and a search API")
    env = setup["env"]

    assert env["COMPOSE_FILE"] == COMPOSE_FILE
    assert env["SEARXNG_CONFIG_DIR"] == "${{ runner.temp }}/searxng-config"
    assert env["SEARCH_ENABLED"] == "true"
    assert env["SEARCH_URL"] == "http://127.0.0.1:8888"
    assert env["MCP_ENABLED"] == "true"
    # Beside the gate's API (8080/9090), the browser worker (9091) and the extract worker (9092).
    assert (env["PUBLIC_PORT"], env["INTERNAL_PORT"]) == ("8081", "9093")
    run = setup["run"]
    assert "uv run text-extraction-init" in run
    assert '--searxng-dir "$SEARXNG_CONFIG_DIR"' in run
    assert "docker compose up -d --wait" in run
    assert run.index("text-extraction-init") < run.index("docker compose up")
    assert "uv run text-extraction " in run
    assert "127.0.0.1:9093/ready" in run


def test_live_search_checks_hits_then_the_stopped_searxng() -> None:
    steps = _steps("live.yml", "live")
    _, hits = _named(steps, "Web search: hits through REST and MCP")
    _, down = _named(steps, "Web search: SearXNG stopped answers search_failed")

    assert "tests.live.run_search --base-url http://127.0.0.1:8081" in hits["run"]
    assert "--expect hits" in hits["run"]
    assert "--retries 3" in hits["run"]
    assert down["env"]["COMPOSE_FILE"] == COMPOSE_FILE
    run = down["run"]
    assert run.index("docker compose stop searxng") < run.index("--expect failed")
    assert "--retries" not in run
    # The summaries are picked up by the step that writes results/summary_*.md to the job summary.
    for step in (hits, down):
        assert "--summary-out results/summary_web_search" in step["run"]
    _, summary = _named(steps, "Add the summaries to the job summary")
    assert "results/summary_*.md" in summary["run"]


# --- image.yml -----------------------------------------------------------------------------------


def test_image_override_swaps_only_the_image_of_init() -> None:
    services = _load(IMAGE_OVERRIDE)["services"]

    assert services == {
        "init": {
            "image": "${CHECK_TAG:?set CHECK_TAG to the image built for the check}",
            "pull_policy": "never",
        }
    }


def test_image_check_starts_the_root_files_searxng() -> None:
    _, step = _named(
        _steps("image.yml", "image"), "Root compose file starts SearXNG with the settings of init"
    )

    assert step["if"] == "matrix.variant == 'api'"
    assert step["env"]["COMPOSE_FILE"] == "docker-compose.yml:tests/image/compose.searxng-check.yml"
    run = step["run"]
    assert "docker compose up -d --wait" in run
    assert 'ls -A /etc/searxng)" = settings.yml' in run
    assert "ultrasecretkey" in run
    assert "/healthz" in run
    assert "search?q=x&format=json" in run
    assert "jq -e" in run
    assert run.rstrip().endswith("docker compose down -v")


# --- ci.yml --------------------------------------------------------------------------------------


def test_ci_parses_both_overrides() -> None:
    _, step = _named(
        _steps("ci.yml", "compose"), "docker compose config (web search check overrides)"
    )

    assert "-f docker-compose.build.yml -f tests/live/compose.searxng.yml config" in step["run"]
    assert "-f tests/image/compose.searxng-check.yml config" in step["run"]
    assert step["env"]["SEARXNG_CONFIG_DIR"]
