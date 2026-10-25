import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
    DocumentPool,
)

TARGET = "tests.document_pool_targets"
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
# The children start with an allowlisted environment; this makes the test target importable
# from any working directory.
TEST_ENV = {"PYTHONPATH": REPO_ROOT}
# Small enough to prove the limit (4096 MB cannot be allocated), large enough for a spawned
# interpreter with the test target to start under it.
MEMORY_LIMIT_MB = 512


def run_with_pool[T](
    scenario: Callable[[DocumentPool], Awaitable[T]],
    *,
    processes: int = 1,
    max_tasks: int = 50,
    memory_limit_mb: int = MEMORY_LIMIT_MB,
    max_result_bytes: int = 64 * 1024 * 1024,
    extra_env: Mapping[str, str] = TEST_ENV,
) -> T:
    async def main() -> T:
        pool = DocumentPool(
            processes,
            memory_limit_mb,
            max_tasks=max_tasks,
            target=TARGET,
            max_result_bytes=max_result_bytes,
            extra_env=extra_env,
        )
        await pool.start()
        try:
            return await scenario(pool)
        finally:
            await pool.shutdown()

    return asyncio.run(main())


def deadline_in(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


def is_running(pid: int) -> bool:
    """True while ``pid`` exists and is no zombie (a dead orphan may wait for its reaper)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_runs_in_child_and_returns() -> None:
    async def scenario(pool: DocumentPool) -> tuple[Any, int]:
        echoed = await pool.run("echo", {"kind": "pdf", "body": b"%PDF-1.7"}, deadline_in(5))
        return echoed, await pool.run("pid", b"", deadline_in(5))

    echoed, child_pid = run_with_pool(scenario)

    assert echoed == {"kind": "pdf", "body": b"%PDF-1.7"}
    assert child_pid != os.getpid()


def test_large_payloads_travel_both_ways() -> None:
    body = os.urandom(20_000_000)

    async def scenario(pool: DocumentPool) -> Any:
        return await pool.run("echo", body, deadline_in(5))

    assert run_with_pool(scenario) == body


def test_deadline_kills_child_and_next_call_works() -> None:
    async def scenario(pool: DocumentPool) -> tuple[float, Any, bool]:
        stuck = await pool.run("pid", b"", deadline_in(5))
        started = time.monotonic()
        with pytest.raises(ConversionTimeout):
            await pool.run("sleep_forever", b"", deadline_in(0.5))
        elapsed = time.monotonic() - started
        echoed = await pool.run("echo", b"next", deadline_in(5))
        return elapsed, echoed, is_running(stuck)

    elapsed, echoed, stuck_alive = run_with_pool(scenario)

    assert elapsed < 1.5
    assert echoed == b"next"
    assert not stuck_alive


def test_a_passed_deadline_does_not_take_a_child() -> None:
    async def scenario(pool: DocumentPool) -> tuple[int, int]:
        before = await pool.run("pid", b"", deadline_in(5))
        with pytest.raises(ConversionTimeout):
            await pool.run("echo", b"", deadline_in(-1))
        return before, await pool.run("pid", b"", deadline_in(5))

    before, after = run_with_pool(scenario)

    assert before == after


def test_memory_limit() -> None:
    async def scenario(pool: DocumentPool) -> tuple[int, Any]:
        small = await pool.run("allocate", {"megabytes": 16}, deadline_in(5))
        with pytest.raises(ConversionOutOfMemory):
            await pool.run("allocate", {"megabytes": 4096}, deadline_in(5))
        return small, await pool.run("echo", b"after", deadline_in(5))

    small, after = run_with_pool(scenario)

    assert small == 16 * 1024 * 1024
    assert after == b"after"


def test_memory_limit_is_the_configured_one() -> None:
    # 600 MB fits under 2048 MB but not under the 512 MB of test_memory_limit, so the refusal
    # there comes from the configured limit and not from the machine.
    async def scenario(pool: DocumentPool) -> Any:
        return await pool.run("allocate", {"megabytes": 600}, deadline_in(5))

    assert run_with_pool(scenario, memory_limit_mb=2048) == 600 * 1024 * 1024

    async def too_much(pool: DocumentPool) -> Any:
        return await pool.run("allocate", {"megabytes": 600}, deadline_in(5))

    with pytest.raises(ConversionOutOfMemory):
        run_with_pool(too_much)


def test_crash_is_reported_and_replaced() -> None:
    async def scenario(pool: DocumentPool) -> tuple[int, int]:
        before = await pool.run("pid", b"", deadline_in(5))
        with pytest.raises(ConversionCrashed):
            await pool.run("crash", b"", deadline_in(5))
        return before, await pool.run("pid", b"", deadline_in(5))

    before, after = run_with_pool(scenario)

    assert before != after


def test_errors_in_the_child_keep_their_type_name() -> None:
    async def scenario(pool: DocumentPool) -> tuple[ConversionError, int, int]:
        before = await pool.run("pid", b"", deadline_in(5))
        with pytest.raises(ConversionError) as caught:
            await pool.run("fail", b"secret payload", deadline_in(5))
        return caught.value, before, await pool.run("pid", b"", deadline_in(5))

    error, before, after = run_with_pool(scenario)

    assert error.type_name == "ValueError"
    assert error.message == "the converter broke"
    assert "secret payload" not in str(error)
    assert before == after  # an error is no reason to replace the child


def test_unknown_function_is_refused() -> None:
    async def scenario(pool: DocumentPool) -> None:
        await pool.run("no_such_function", b"", deadline_in(5))

    with pytest.raises(LookupError, match="no_such_function"):
        run_with_pool(scenario)


def test_child_recycled_after_max_tasks() -> None:
    async def scenario(pool: DocumentPool) -> list[int]:
        return [await pool.run("pid", b"", deadline_in(5)) for _ in range(3)]

    first, second, third = run_with_pool(scenario, max_tasks=2)

    assert first == second
    assert third != first


def test_waiting_for_a_free_child_counts_against_the_deadline() -> None:
    async def scenario(pool: DocumentPool) -> None:
        busy = asyncio.create_task(pool.run("sleep_forever", b"", deadline_in(5)))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        with pytest.raises(ConversionTimeout):
            await pool.run("echo", b"", deadline_in(0.3))
        assert time.monotonic() - started < 1
        busy.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy

    run_with_pool(scenario)


def test_cancelling_the_caller_kills_its_child() -> None:
    async def scenario(pool: DocumentPool) -> tuple[bool, Any]:
        stuck = await pool.run("pid", b"", deadline_in(5))
        task = asyncio.create_task(pool.run("sleep_forever", b"", deadline_in(30)))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        echoed = await pool.run("echo", b"next", deadline_in(5))
        return is_running(stuck), echoed

    stuck_alive, echoed = run_with_pool(scenario)

    assert not stuck_alive
    assert echoed == b"next"


def test_children_ignore_sigterm_and_sigint() -> None:
    async def scenario(pool: DocumentPool) -> tuple[int, int]:
        child = await pool.run("pid", b"", deadline_in(5))
        os.kill(child, signal.SIGTERM)
        os.kill(child, signal.SIGINT)
        await asyncio.sleep(0.2)
        return child, await pool.run("pid", b"", deadline_in(5))

    before, after = run_with_pool(scenario)

    assert before == after


def test_calls_spread_over_the_children() -> None:
    async def scenario(pool: DocumentPool) -> tuple[set[int], int]:
        children = set(pool.child_pids())
        busy = asyncio.create_task(pool.run("sleep_forever", b"", deadline_in(5)))
        await asyncio.sleep(0.1)
        free = await pool.run("pid", b"", deadline_in(2))
        busy.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy
        return children, free

    children, free = run_with_pool(scenario, processes=2)

    assert len(children) == 2
    assert free in children  # the other child was the busy one


def test_parent_never_imports_markitdown() -> None:
    async def scenario(pool: DocumentPool) -> Any:
        return await pool.run("echo", b"x", deadline_in(5))

    assert run_with_pool(scenario) == b"x"
    assert "markitdown" not in sys.modules
    assert "pandas" not in sys.modules


def test_broken_target_fails_start() -> None:
    async def main() -> None:
        pool = DocumentPool(1, MEMORY_LIMIT_MB, target="tests.no_such_target", extra_env=TEST_ENV)
        try:
            await pool.start()
        finally:
            await pool.shutdown()

    with pytest.raises(RuntimeError, match="ModuleNotFoundError"):
        asyncio.run(main())


def test_shutdown_kills_children() -> None:
    async def main() -> tuple[set[int], DocumentPool]:
        pool = DocumentPool(2, MEMORY_LIMIT_MB, target=TARGET, extra_env=TEST_ENV)
        await pool.start()
        pids = set(pool.child_pids())
        await pool.shutdown()
        await pool.shutdown()  # idempotent
        with pytest.raises(RuntimeError):
            await pool.run("echo", b"", deadline_in(5))
        return pids, pool

    pids, _pool = asyncio.run(main())

    assert len(pids) == 2
    assert not any(is_running(pid) for pid in pids)


def test_shutdown_kills_a_busy_child() -> None:
    async def main() -> bool:
        pool = DocumentPool(1, MEMORY_LIMIT_MB, target=TARGET, extra_env=TEST_ENV)
        await pool.start()
        child = await pool.run("pid", b"", deadline_in(5))
        busy = asyncio.create_task(pool.run("sleep_forever", b"", deadline_in(30)))
        await asyncio.sleep(0.2)
        await pool.shutdown()
        with pytest.raises(RuntimeError, match="shut down"):
            await busy
        return is_running(child)

    assert not asyncio.run(main())


def test_results_over_the_limit_are_refused() -> None:
    async def scenario(pool: DocumentPool) -> tuple[int, int, Any]:
        before = await pool.run("pid", b"", deadline_in(5))
        with pytest.raises(ConversionOutOfMemory):
            await pool.run("echo", b"x" * 2_000_000, deadline_in(5))
        after = await pool.run("pid", b"", deadline_in(5))
        return before, after, await pool.run("echo", b"x" * 500_000, deadline_in(5))

    before, after, small = run_with_pool(scenario, max_result_bytes=1_000_000)

    assert before != after  # the child that made the oversized result is replaced
    assert small == b"x" * 500_000


def test_results_of_other_types_are_refused() -> None:
    async def scenario(pool: DocumentPool) -> tuple[ConversionError, int, int]:
        before = await pool.run("pid", b"", deadline_in(5))
        with pytest.raises(ConversionError) as caught:
            await pool.run("fraction", b"", deadline_in(5))
        return caught.value, before, await pool.run("pid", b"", deadline_in(5))

    error, before, after = run_with_pool(scenario)

    assert error.type_name == "UnpicklingError"
    assert before == after


def test_payloads_that_cannot_be_pickled_are_refused() -> None:
    async def scenario(pool: DocumentPool) -> tuple[int, int]:
        before = await pool.run("pid", b"", deadline_in(5))
        with pytest.raises(TypeError):
            await pool.run("echo", {"function": lambda: None}, deadline_in(5))
        return before, await pool.run("pid", b"", deadline_in(5))

    before, after = run_with_pool(scenario)

    assert before == after


def test_children_do_not_import_the_parents_main_module(tmp_path: Path) -> None:
    (tmp_path / "pool_main_marker.py").write_text("")
    script = tmp_path / "main.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio
            import sys

            sys.path[:0] = [{str(tmp_path)!r}, {REPO_ROOT!r}]
            import pool_main_marker  # noqa: F401

            from text_extraction.documents.pool import DocumentPool


            async def main() -> None:
                pool = DocumentPool(
                    1, 512, target={TARGET!r}, extra_env={{"PYTHONPATH": {REPO_ROOT!r}}}
                )
                await pool.start()
                deadline = asyncio.get_running_loop().time() + 5
                try:
                    print(await pool.run("has_module", {{"name": "pool_main_marker"}}, deadline))
                    print(await pool.run("has_module", {{"name": "asyncio"}}, deadline))
                finally:
                    await pool.shutdown()


            asyncio.run(main())
            """
        )
    )

    done = subprocess.run(  # noqa: S603 - our own test script
        [sys.executable, str(script)], capture_output=True, text=True, timeout=30, check=False
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "False"]


def test_secrets_in_the_environment_do_not_reach_the_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEYS", "secret-key")
    monkeypatch.setenv("VALKEY_PASSWORD", "secret-password")
    monkeypatch.setenv("TESSDATA_PREFIX", "/models/tessdata")

    async def scenario(pool: DocumentPool) -> Any:
        return await pool.run("environment", b"", deadline_in(5))

    environment = run_with_pool(scenario, extra_env={**TEST_ENV, "DOCLING_THREADS": "4"})

    assert "API_KEYS" not in environment
    assert "VALKEY_PASSWORD" not in environment
    assert environment["TESSDATA_PREFIX"] == "/models/tessdata"
    assert environment["DOCLING_THREADS"] == "4"


def test_a_timeout_also_kills_the_childs_own_children(tmp_path: Path) -> None:
    pid_file = tmp_path / "sleeper.pid"

    async def scenario(pool: DocumentPool) -> None:
        with pytest.raises(ConversionTimeout):
            await pool.run("spawn_sleeper", {"pid_file": str(pid_file)}, deadline_in(1))

    run_with_pool(scenario)

    sleeper = int(pid_file.read_text())
    end = time.monotonic() + 2
    while is_running(sleeper) and time.monotonic() < end:
        time.sleep(0.05)
    assert not is_running(sleeper)


def test_no_descriptors_leak_over_timeouts_and_crashes() -> None:
    async def scenario(pool: DocumentPool) -> tuple[int, int]:
        await pool.run("echo", b"", deadline_in(5))
        baseline = open_fds()
        for _ in range(3):
            with pytest.raises(ConversionTimeout):
                await pool.run("sleep_forever", b"", deadline_in(0.2))
            with pytest.raises(ConversionCrashed):
                await pool.run("crash", b"", deadline_in(5))
        await pool.run("echo", b"", deadline_in(5))  # the last replacement is in place
        return baseline, open_fds()

    baseline, after = run_with_pool(scenario)

    assert after == baseline


def test_a_failing_respawn_is_logged_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(pool: DocumentPool) -> None:
        def refuse() -> None:
            raise OSError("no processes left")

        monkeypatch.setattr(pool, "_spawn", refuse)
        with pytest.raises(ConversionCrashed):
            await pool.run("crash", b"", deadline_in(5))
        with pytest.raises(ConversionTimeout):
            await pool.run("echo", b"", deadline_in(0.5))  # retries the spawn, fails again

    with capture_logs() as logs:
        run_with_pool(scenario)

    failures = [log for log in logs if log["event"] == "document_pool_respawn_failed"]
    assert failures == [
        {"event": "document_pool_respawn_failed", "log_level": "warning", "error": "OSError"}
    ]
