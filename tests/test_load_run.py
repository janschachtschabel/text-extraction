"""The load generator (`tests/load/run_load.py`) against a fake service: what it sends per kind
of scenario, how it follows jobs, the window it records and the files it writes. The API key
never reaches its output."""

import asyncio
import io
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.load import run_load
from tests.load.report import ScenarioResult
from tests.load.scenarios import GENERATED_DOCX, Scenario, upload_bytes

FARM = "http://farm:18000"  # a compose service: a single-label name
KEY = "load-key-secret-4711"


def envelope(status: str = "success", code: str | None = None) -> dict[str, Any]:
    error = {"code": code, "message": "x", "retryable": False} if code else None
    return {"status": status, "error": error, "data": {"word_count": 600}}


def accepted(job_id: str = "j1") -> dict[str, Any]:
    return {
        "status": "success",
        "error": None,
        "data": {"id": job_id, "state": "queued", "status_url": f"/v1/jobs/{job_id}"},
    }


def job(state: str, *, total: int = 1, failed: int = 0, partial: int = 0) -> dict[str, Any]:
    done = total if state == "completed" else 0
    return {
        "status": "success",
        "error": None,
        "data": {
            "id": "j1",
            "state": state,
            "progress": {
                "total": total,
                "done": done,
                "success": done - failed - partial,
                "partial": partial,
                "failed": failed,
            },
            "items": [],
            "next_offset": None,
        },
    }


def scenario(
    kind: str,
    endpoint: str,
    *,
    count: int = 3,
    concurrency: int = 2,
    expected: frozenset[str] = frozenset({"success"}),
) -> Scenario:
    def body(index: int, farm: str) -> dict[str, object]:
        if kind == "upload":
            return {"file": "tests/documents/corpus/lesson.docx", "format": "markdown"}
        if kind == "mcp":
            return {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": "extract_url", "arguments": {"url": f"{farm}/p/{index}.html"}},
            }
        return {"url": f"{farm}/p/{index}.html", "mode": "fast"}

    return Scenario(
        name=f"test-{kind}",
        kind=kind,  # type: ignore[arg-type]
        endpoint=endpoint,
        count=count,
        concurrency=concurrency,
        body=body,
        settle_seconds=0,
        expected=expected,  # type: ignore[arg-type]
    )


def run(
    handler: Callable[[httpx.Request], Any], plan: Scenario
) -> tuple[ScenarioResult, list[dict[str, Any]]]:
    sink = io.StringIO()

    async def go() -> ScenarioResult:
        async with httpx.AsyncClient(
            base_url="http://api.load.test", transport=httpx.MockTransport(handler)
        ) as client:
            return await run_load.run_scenario(client, plan, FARM, sink=sink, poll_seconds=0)

    result = asyncio.run(go())
    return result, [json.loads(line) for line in sink.getvalue().splitlines()]


def test_sync_scenario_sends_each_body_and_records_latency_and_outcome() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["url"].endswith("/p/2.html"):
            return httpx.Response(200, json=envelope("failed", "upstream_error"))
        return httpx.Response(200, json=envelope())

    result, records = run(handler, scenario("sync", "/v1/extract"))

    assert sorted(body["url"] for body in seen) == [f"{FARM}/p/{n}.html" for n in range(3)]
    assert result.name == "test-sync"
    assert result.requests == 3
    assert result.outcomes == {"success": 2, "failed": 1}
    assert result.unexpected == 1  # the scenario expects success only
    assert result.start <= result.end <= result.settle_end
    assert sorted(record["index"] for record in records) == [0, 1, 2]
    failed = next(record for record in records if record["index"] == 2)
    assert failed["outcome"] == "failed"
    assert failed["error_code"] == "upstream_error"
    assert failed["scenario"] == "test-sync"
    assert failed["expected"] is False
    assert all(record["expected"] for record in records if record["index"] != 2)
    assert all(record["latency_ms"] >= 0 for record in records)


def test_an_accepted_answer_is_followed_until_its_job_completes() -> None:
    polls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=accepted())
        polls.append(request.url.path)
        state = "completed" if len(polls) >= 3 else "running"
        return httpx.Response(200, json=job(state, total=1000, failed=2))

    result, records = run(handler, scenario("batch", "/v1/batch", count=1, concurrency=1))

    assert polls == ["/v1/jobs/j1"] * 3
    assert result.outcomes == {"partial": 1}  # 2 of 1,000 items failed
    assert records[0]["http_status"] == 202


def test_a_job_whose_items_all_failed_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=accepted())
        return httpx.Response(200, json=job("completed", total=1, failed=1))

    result, _ = run(handler, scenario("site", "/v1/site-structure", count=1, concurrency=1))

    assert result.outcomes == {"failed": 1}


def test_a_refused_request_or_a_client_failure_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        index = int(json.loads(request.content)["url"].rsplit("/", 1)[1].split(".")[0])
        if index == 0:
            return httpx.Response(429, json=envelope("failed", "rate_limited"))
        if index == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(500, text="no envelope")

    result, records = run(handler, scenario("sync", "/v1/extract"))

    assert result.outcomes == {"error": 3}
    codes = {record["index"]: record["error_code"] for record in records}
    assert codes == {0: "rate_limited", 1: "client_ConnectError", 2: "http_500"}


def test_upload_scenario_posts_the_file_and_the_form_fields() -> None:
    parts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/extract/file"
        assert request.headers["content-type"].startswith("multipart/form-data")
        parts.append(request.content)
        return httpx.Response(200, json=envelope())

    result, _ = run(handler, scenario("upload", "/v1/extract/file", count=1, concurrency=1))

    content = Path("tests/documents/corpus/lesson.docx").read_bytes()
    assert result.outcomes == {"success": 1}
    assert content in parts[0]
    assert b'name="file"; filename="lesson.docx"' in parts[0]
    assert b'name="format"\r\n\r\nmarkdown' in parts[0]


def test_upload_scenario_posts_a_generated_file() -> None:
    parts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parts.append(request.content)
        return httpx.Response(200, json=envelope())

    plan = replace(
        scenario("upload", "/v1/extract/file", count=1, concurrency=1),
        body=lambda index, farm: {"file": GENERATED_DOCX, "format": "markdown"},
    )
    result, _ = run(handler, plan)

    assert result.outcomes == {"success": 1}
    assert upload_bytes(GENERATED_DOCX) in parts[0]
    assert b'name="file"; filename="report.docx"' in parts[0]


def test_mcp_scenario_posts_json_rpc_and_reads_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp"
        assert "application/json" in request.headers["accept"]
        assert "text/event-stream" in request.headers["accept"]
        message = json.loads(request.content)
        is_error = message["id"] == 1
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": message["id"], "result": {"isError": is_error}},
        )

    result, _ = run(handler, scenario("mcp", "/mcp"))

    assert result.outcomes == {"success": 2, "failed": 1}


def test_concurrency_bounds_the_requests_in_flight() -> None:
    in_flight = most = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, most
        in_flight += 1
        most = max(most, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, json=envelope())

    result, _ = run(handler, scenario("sync", "/v1/extract", count=10, concurrency=3))

    assert most == 3
    assert result.outcomes == {"success": 10}


def prepare(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    plan: Sequence[Scenario],
) -> None:
    """A fake service, a fake docker stats with one api reading, no idle time."""
    stats = json.dumps(
        {"Name": "text-extraction-api-1", "CPUPerc": "50.00%", "MemUsage": "300MiB / 1GiB"}
    )
    monkeypatch.setattr(run_load, "SCENARIOS", tuple(plan))
    monkeypatch.setattr(run_load, "IDLE_SECONDS", 0)
    monkeypatch.setattr(run_load, "STATS_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(
        run_load,
        "STATS_COMMAND",
        [sys.executable, "-c", f"import time; time.sleep(0.01); print({stats!r})"],
    )
    monkeypatch.setattr(run_load, "_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setenv("LOAD_TEST_KEY", KEY)


def arguments(out_dir: Path, *extra: str) -> list[str]:
    return [
        "--base-url",
        "http://api.load.test",
        "--farm-url",
        FARM,
        "--api-key-env",
        "LOAD_TEST_KEY",
        "--out-dir",
        str(out_dir),
        *extra,
    ]


def test_main_writes_every_file_and_never_prints_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent_keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_keys.append(request.headers.get("authorization"))
        return httpx.Response(200, json=envelope())

    plan = replace(scenario("sync", "/v1/extract"), settle_seconds=1)  # long enough to sample
    prepare(monkeypatch, handler, [plan, scenario("mcp", "/mcp")])

    code = run_load.main(arguments(tmp_path, "--only", "test-sync", "--selector-processes", "2"))

    output = capsys.readouterr()
    assert code == 0
    assert sent_keys == [f"Bearer {KEY}"] * 3  # only the scenario named by --only ran
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "results.jsonl",
        "sizing.json",
        "stats.jsonl",
        "summary.md",
    ]
    for path in tmp_path.iterdir():
        assert KEY not in path.read_text()
    assert KEY not in output.out + output.err
    summary = (tmp_path / "summary.md").read_text()
    assert "2 selector processes" in summary
    assert "| test-sync | sync |" in summary
    assert "| idle | idle |" in summary  # the window of the stack at rest is named too
    assert summary in output.out
    sizing = json.loads((tmp_path / "sizing.json").read_text())
    assert sizing["selector_processes"] == 2
    # Keyed by compose service: the project prefix and the replica number are gone.
    assert sizing["services"]["api"]["cpu_request"] == "500m"


def test_main_fails_on_an_unexpected_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope("partial", "content_too_short"))

    prepare(monkeypatch, handler, [scenario("sync", "/v1/extract")])

    code = run_load.main(arguments(tmp_path))

    assert code == 1
    summary = (tmp_path / "summary.md").read_text()
    assert "| test-sync | sync |" in summary
    assert "partial 3 | 3 |" in summary  # the outcomes, then the unexpected ones


def test_main_writes_the_summary_when_a_late_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope())

    calls = 0

    async def llm_counts(client: httpx.AsyncClient, url: str | None) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls > 1:  # the count after the first scenario
            raise httpx.ConnectError("the fake LLM is gone")
        return {}

    prepare(monkeypatch, handler, [scenario("sync", "/v1/extract")])
    monkeypatch.setattr(run_load, "_llm_counts", llm_counts)

    with pytest.raises(httpx.ConnectError):
        run_load.main(arguments(tmp_path, "--fake-llm-url", "http://fake-llm:8000"))

    summary = (tmp_path / "summary.md").read_text()
    assert "| test-sync | sync |" in summary
    assert json.loads((tmp_path / "sizing.json").read_text())["selector_processes"] is None


@pytest.mark.parametrize(
    "farm",
    [
        "http://farm:18000",
        "http://localhost:18000",
        "http://127.0.0.2:18000",
        "http://10.1.2.3",
        "http://192.168.0.7:8080/",
        "http://[::1]:18000",
        "https://farm",
    ],
)
def test_a_local_farm_url_is_accepted(farm: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOAD_TEST_KEY", KEY)

    args = run_load.parse_args(["--farm-url", farm, "--api-key-env", "LOAD_TEST_KEY"])

    assert args.farm_url == farm


@pytest.mark.parametrize(
    "farm",
    [
        "https://example.org",
        "http://farm.example.com:18000",
        "http://8.8.8.8",
        "http://[2001:4860:4860::8888]",
        "ftp://farm",
        "farm:18000",
        "http://",
        # Link-local: Python counts it as private, but it is the cloud metadata range, not a farm.
        "http://169.254.169.254",
        "http://169.254.0.1:18000",
        "http://[fe80::1]:18000",
    ],
)
def test_a_farm_url_that_is_not_local_is_refused(
    farm: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The load goes to our own farm only, never to a site on the internet."""
    monkeypatch.setenv("LOAD_TEST_KEY", KEY)

    with pytest.raises(SystemExit):
        run_load.parse_args(["--farm-url", farm, "--api-key-env", "LOAD_TEST_KEY"])

    assert "--farm-url" in capsys.readouterr().err


def test_main_refuses_an_unknown_scenario_and_a_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOAD_TEST_KEY", raising=False)
    common = ["--farm-url", FARM, "--out-dir", str(tmp_path)]

    with pytest.raises(SystemExit):
        run_load.main([*common, "--only", "no-such-scenario"])
    with pytest.raises(SystemExit):
        run_load.main([*common, "--api-key-env", "LOAD_TEST_KEY"])


def test_llm_table_gives_tokens_per_scenario_request() -> None:
    usage = {
        "structured-llm-50": (
            50,
            {
                "chat_completions": {
                    "requests": 60,
                    "prompt_bytes": 200_000,
                    "completion_bytes": 2_000,
                },
                "embeddings": {"requests": 0, "prompt_bytes": 0, "completion_bytes": 0},
            },
        ),
        "chunk-semantic-50": (
            50,
            {"embeddings": {"requests": 50, "prompt_bytes": 100_000, "completion_bytes": 0}},
        ),
    }

    table = run_load._llm_table(usage)

    # 200,000 bytes / 50 requests / 4 bytes per token = 1,000 prompt tokens per request;
    # the completion from the messages' bytes only: 2,000 / 50 / 4 = 10.
    assert "| structured-llm-50 | chat_completions | 60 | 200000 | 2000 | 1000 | 10 |" in table
    assert "| structured-llm-50 | embeddings |" not in table  # no calls, no row
    # An embedding has no completion.
    assert "| chunk-semantic-50 | embeddings | 50 | 100000 | 0 | 500 | - |" in table


# --- the site structure items (ruling P11-S7) -----------------------------------------------------

# A stand-in for ``valkey-cli`` in the valkey container: ``--scan --pattern P`` lists the keys
# that match P, and commands on stdin answer one line each. It logs its calls to ``calls.log``.
VALKEY_STUB = """
import fnmatch, sys
from pathlib import Path
here = Path(__file__).parent
sizes = {"job:{j1}:meta": 300, "job:{j1}:results": 20_000_000, "job:{j2}:meta": 7}
with (here / "calls.log").open("a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if (here / "fail").exists():
    sys.exit("connection refused")
if sys.argv[1:3] == ["--scan", "--pattern"]:
    pattern = sys.argv[3].replace("{", "[{]").replace("}", "[}]")
    for key in sizes:
        if fnmatch.fnmatchcase(key, pattern):
            print(key)
else:
    mode = (here / "mode").read_text() if (here / "mode").exists() else "raw"
    for line in sys.stdin:
        command, sub, key, *rest = line.split()
        assert (command, sub, rest) == ("MEMORY", "USAGE", ["SAMPLES", "0"]), line
        size = sizes.get(key)
        if mode == "deny" and key.endswith(":results"):
            print("NOPERM User load has no permissions to run the 'memory' command")
        elif mode == "tty":
            print("(nil)" if size is None else f"(integer) {size}")
        else:
            print("" if size is None else size)
"""


def valkey_stub(tmp_path: Path) -> list[str]:
    script = tmp_path / "valkey_cli.py"
    script.write_text(VALKEY_STUB)
    return [sys.executable, str(script)]


def run_site(
    tmp_path: Path, kind: str = "site", *, total: int = 1111
) -> tuple[ScenarioResult, list[dict[str, Any]]]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=accepted("j1"))
        answer = job("completed", total=total)
        answer["data"]["created_at"] = "2026-10-31T10:00:00.250000Z"
        answer["data"]["finished_at"] = "2026-10-31T10:01:02.500000Z"
        return httpx.Response(200, json=answer)

    plan = scenario(kind, "/v1/site-structure", count=1, concurrency=1)
    sink = io.StringIO()

    async def go() -> ScenarioResult:
        async with httpx.AsyncClient(
            base_url="http://api.load.test", transport=httpx.MockTransport(handler)
        ) as client:
            return await run_load.run_scenario(
                client, plan, FARM, sink=sink, poll_seconds=0, valkey_cli=valkey_stub(tmp_path)
            )

    result = asyncio.run(go())
    return result, [json.loads(line) for line in sink.getvalue().splitlines()]


def test_a_crawl_records_its_items_and_the_valkey_bytes_of_its_keys(tmp_path: Path) -> None:
    """MEMORY USAGE of every key of the job (``job:{<id>}:*``), read when the crawl completed."""
    result, [record] = run_site(tmp_path)

    assert result.outcomes == {"success": 1}
    assert record["job_id"] == "j1"
    assert record["items"] == 1111
    # The job's own times, not the 2 s polls: created to finished.
    assert record["job_seconds"] == 62.25
    assert record["valkey_bytes"] == 20_000_300  # the keys of job j1 only
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls[0] == "--scan --pattern job:{j1}:*"


def test_the_valkey_bytes_are_read_for_crawls_only(tmp_path: Path) -> None:
    _, [record] = run_site(tmp_path, "batch")

    assert record["items"] == 1111
    assert record["valkey_bytes"] is None
    assert not (tmp_path / "calls.log").exists()


def test_a_failed_valkey_read_leaves_the_bytes_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "fail").touch()

    result, [record] = run_site(tmp_path)

    assert result.outcomes == {"success": 1}  # the crawl's outcome is the job's
    assert record["valkey_bytes"] is None
    assert "valkey" in capsys.readouterr().err


def test_an_error_reply_leaves_the_bytes_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An error answer to one MEMORY USAGE (an ACL denial, say) must not give a low sum."""
    (tmp_path / "mode").write_text("deny")

    _, [record] = run_site(tmp_path)

    assert record["valkey_bytes"] is None
    assert "valkey" in capsys.readouterr().err


def test_terminal_style_answers_are_read_too(tmp_path: Path) -> None:
    (tmp_path / "mode").write_text("tty")

    assert run_load.valkey_job_bytes(valkey_stub(tmp_path), "j1") == 20_000_300


LAG = "text_extraction_event_loop_lag_seconds"


def lag_text(buckets: dict[str, int], total: float) -> str:
    """The metrics text of a lag histogram with these cumulative bucket counts."""
    lines = [f"# HELP {LAG} Event-loop lag.", f"# TYPE {LAG} histogram"]
    lines += [f'{LAG}_bucket{{le="{le}"}} {count}' for le, count in buckets.items()]
    lines += [f"{LAG}_count {buckets['+Inf']}", f"{LAG}_sum {total}"]
    return "\n".join(lines) + "\n"


def test_the_lag_of_a_scenario_is_the_difference_of_two_reads() -> None:
    before = lag_text({"0.01": 10, "0.1": 10, "1.0": 10, "5.0": 10, "+Inf": 10}, 0.05)
    after = lag_text({"0.01": 50, "0.1": 55, "1.0": 57, "5.0": 58, "+Inf": 58}, 4.05)

    lag = run_load.lag_between(before, after)

    assert lag is not None
    assert lag.samples == 48
    assert lag.mean_ms == round(4000 / 48)
    assert lag.over_100ms == 3  # 58 - 55
    assert lag.max_bound_s == 5.0  # the highest sample is at most 5 s, above 1 s
    assert lag.total_s == 4.0  # the time the loop was held, over all samples
    assert run_load.lag_between("", after) == run_load.lag_between(
        lag_text({"+Inf": 0}, 0), after
    )  # no read before: from zero
    assert run_load.lag_between(before, "other_metric 1\n") is None  # not exported


def test_no_lag_without_samples_or_after_a_restart() -> None:
    """Zero samples give no bound; counts that went backwards (the worker restarted between
    the reads) give no difference either."""
    before = lag_text({"0.1": 50, "+Inf": 50}, 1.0)

    assert run_load.lag_between(before, before) is None
    assert run_load.lag_between(before, lag_text({"0.1": 4, "+Inf": 4}, 0.01)) is None
    assert run_load.lag_between(before, lag_text({"0.1": 49, "+Inf": 60}, 3.0)) is None


def test_a_scenarios_lag_needs_the_read_before_it() -> None:
    """A failed read before the window leaves the lag unknown, not the lag since the start."""
    before = {"127.0.0.1:9091": lag_text({"0.1": 10, "+Inf": 10}, 0.01)}
    after = {
        "127.0.0.1:9091": lag_text({"0.1": 20, "+Inf": 20}, 0.02),
        "127.0.0.1:9092": lag_text({"0.1": 30, "+Inf": 30}, 0.03),  # its read before failed
        "127.0.0.1:9090": "other_metric 1\n",  # exports no lag: no row
    }

    lags = run_load.scenario_lags(before, after)

    assert set(lags) == {"127.0.0.1:9091", "127.0.0.1:9092"}
    assert lags["127.0.0.1:9091"] == run_load.lag_between(
        before["127.0.0.1:9091"], after["127.0.0.1:9091"]
    )
    assert lags["127.0.0.1:9092"] is None
    table = run_load._lag_table({"sitemap-200k": lags})
    assert "| sitemap-200k | 127.0.0.1:9092 | - | - | - | - | - |" in table


def test_the_lag_above_the_last_bound_says_so() -> None:
    after = lag_text({"0.01": 1, "5.0": 1, "+Inf": 2}, 9.0)

    lag = run_load.lag_between("", after)

    assert lag is not None
    assert lag.max_bound_s == float("inf")


def test_the_site_and_lag_tables() -> None:
    records = [
        run_load.Record(
            "sitemap-200k", 0, 202, "success", None, 14_000, True, "j1", 3, 25_165_824, 12.34
        ),
        run_load.Record("site-crawl", 0, 202, "success", None, 61_000, True, "j2", 100, None),
        run_load.Record("site-crawl-max-pages", 0, 200, "error", "job_timeout", 9, False, "j3"),
        run_load.Record("sync-fast-c10", 0, 200, "success", None, 300, True),
    ]
    lags = {
        "sitemap-200k": {"127.0.0.1:9091": run_load.Lag(10, 120, 2, 2.5, 1.2)},
        "site-crawl": {"127.0.0.1:9091": run_load.Lag(4, 1, 0, 0.005, 0.004)},
    }

    site = run_load._site_table(records, {"sitemap-200k", "site-crawl", "site-crawl-max-pages"})
    lag = run_load._lag_table(lags)

    assert "| sitemap-200k | 3 | 12.3 | 14.0 | 25165824 | 24.0 |" in site
    assert "| site-crawl | 100 | - | 61.0 | - | - |" in site
    assert "| site-crawl-max-pages | - | - | 0.0 | - | - |" in site  # a crawl that timed out
    assert "None" not in site
    assert "sync-fast-c10" not in site
    assert "JOB_TTL_SECONDS" in site
    assert "| sitemap-200k | 127.0.0.1:9091 | 10 | 120 | 1.2 | 2 | 2.5 |" in lag
    assert "| site-crawl | 127.0.0.1:9091 | 4 | 1 | 0.0 | 0 | 0.005 |" in lag
    assert run_load._lag_table({}) == ""
    assert run_load._site_table(records[3:], {"sitemap-200k", "site-crawl"}) == ""


def test_main_reads_valkey_through_the_given_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=accepted("j1"))
        return httpx.Response(200, json=job("completed", total=4))

    prepare(monkeypatch, handler, [scenario("site", "/v1/site-structure", count=1, concurrency=1)])
    stub = " ".join(valkey_stub(tmp_path))
    out = tmp_path / "out"
    out.mkdir()

    code = run_load.main(arguments(out, "--valkey-cli", stub))

    assert code == 0
    summary = (out / "summary.md").read_text()
    assert "| test-site | 4 |" in summary
    assert "| 20000300 |" in summary
