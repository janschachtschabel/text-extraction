import asyncio
import subprocess
import sys
import threading
import time

import pytest

from text_extraction.conversion_pool import ConversionPool


def test_runs_the_function_on_a_pool_thread() -> None:
    pool = ConversionPool(2)

    async def scenario() -> str:
        return await pool.run(lambda: threading.current_thread().name)

    try:
        assert asyncio.run(scenario()).startswith("convert")
    finally:
        pool.shutdown()


def test_errors_reach_the_caller() -> None:
    pool = ConversionPool(1)

    def broken() -> None:
        raise ValueError("bad page")

    try:
        with pytest.raises(ValueError, match="bad page"):
            asyncio.run(pool.run(broken))
    finally:
        pool.shutdown()


def test_at_most_the_configured_number_of_conversions_run_at_once() -> None:
    pool = ConversionPool(2)
    lock = threading.Lock()
    running = 0
    peak = 0

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1

    async def scenario() -> None:
        await asyncio.gather(*(pool.run(work) for _ in range(8)))

    try:
        asyncio.run(scenario())
    finally:
        pool.shutdown()

    assert peak == 2


def test_waiting_for_a_slot_can_time_out_without_running_the_work() -> None:
    pool = ConversionPool(1)
    release = threading.Event()
    ran: list[str] = []

    async def scenario() -> None:
        busy = asyncio.ensure_future(pool.run(release.wait, 30))
        await asyncio.sleep(0.05)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await pool.run(ran.append, "late")
        release.set()
        await busy

    try:
        asyncio.run(scenario())
    finally:
        pool.shutdown()

    assert ran == []


def test_a_cancelled_caller_keeps_the_slot_until_its_thread_is_done() -> None:
    pool = ConversionPool(1)
    release = threading.Event()
    order: list[str] = []

    def first() -> None:
        release.wait(30)
        order.append("first done")

    async def scenario() -> None:
        abandoned = asyncio.ensure_future(pool.run(first))
        await asyncio.sleep(0.05)
        abandoned.cancel()
        second = asyncio.ensure_future(pool.run(order.append, "second ran"))
        await asyncio.sleep(0.2)
        assert order == []  # the thread still runs, so the slot is still taken
        release.set()
        await second

    try:
        asyncio.run(scenario())
    finally:
        pool.shutdown()

    assert order == ["first done", "second ran"]


def test_a_running_conversion_delays_the_interpreter_exit() -> None:
    """What `shutdown` cannot do: concurrent.futures joins its threads at exit, so a conversion
    that is already running holds the process until it ends. The docstring says so."""
    program = """
import asyncio, time
from text_extraction.conversion_pool import ConversionPool

pool = ConversionPool(1)


async def main() -> None:
    asyncio.ensure_future(pool.run(time.sleep, 2.0))
    await asyncio.sleep(0.2)
    pool.shutdown()


asyncio.run(main())
"""
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - our own interpreter and program text
        [sys.executable, "-c", program], check=True, capture_output=True, timeout=60
    )
    elapsed = time.monotonic() - started

    assert completed.stderr == b""
    assert elapsed > 1.5  # the sleeping conversion is joined, not abandoned
