import asyncio
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
    DocumentPool,
)

TARGET = "tests.document_pool_targets"
# Small enough to prove the limit (4096 MB cannot be allocated), large enough for a spawned
# interpreter with the test target to start under it.
MEMORY_LIMIT_MB = 512


def run_with_pool[T](
    scenario: Callable[[DocumentPool], Awaitable[T]],
    *,
    processes: int = 1,
    max_tasks: int = 50,
    memory_limit_mb: int = MEMORY_LIMIT_MB,
) -> T:
    async def main() -> T:
        pool = DocumentPool(processes, memory_limit_mb, max_tasks=max_tasks, target=TARGET)
        await pool.start()
        try:
            return await scenario(pool)
        finally:
            await pool.shutdown()

    return asyncio.run(main())


def deadline_in(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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
    async def scenario(pool: DocumentPool) -> tuple[Any, set[int]]:
        busy = asyncio.create_task(pool.run("sleep_forever", b"", deadline_in(5)))
        await asyncio.sleep(0.1)
        echoed = await pool.run("echo", b"free", deadline_in(2))
        busy.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy
        pids = {await pool.run("pid", b"", deadline_in(5)) for _ in range(4)}
        return echoed, pids

    echoed, pids = run_with_pool(scenario, processes=2)

    assert echoed == b"free"
    assert len(pids) <= 2


def test_parent_never_imports_markitdown() -> None:
    async def scenario(pool: DocumentPool) -> Any:
        return await pool.run("echo", b"x", deadline_in(5))

    assert run_with_pool(scenario) == b"x"
    assert "markitdown" not in sys.modules
    assert "pandas" not in sys.modules


def test_broken_target_fails_start() -> None:
    async def main() -> None:
        pool = DocumentPool(1, MEMORY_LIMIT_MB, target="tests.no_such_target")
        try:
            await pool.start()
        finally:
            await pool.shutdown()

    with pytest.raises(RuntimeError, match="ModuleNotFoundError"):
        asyncio.run(main())


def test_shutdown_kills_children() -> None:
    async def main() -> tuple[set[int], DocumentPool]:
        pool = DocumentPool(2, MEMORY_LIMIT_MB, target=TARGET)
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
        pool = DocumentPool(1, MEMORY_LIMIT_MB, target=TARGET)
        await pool.start()
        child = await pool.run("pid", b"", deadline_in(5))
        busy = asyncio.create_task(pool.run("sleep_forever", b"", deadline_in(30)))
        await asyncio.sleep(0.2)
        await pool.shutdown()
        with pytest.raises(RuntimeError, match="shut down"):
            await busy
        return is_running(child)

    assert not asyncio.run(main())
