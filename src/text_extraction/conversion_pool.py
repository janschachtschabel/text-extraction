"""The worker threads for the CPU-heavy steps of an extraction: decoding, conversion, metadata,
licences, fingerprints.

They run on a pool of their own, ``CONVERT_CONCURRENCY`` threads, and never on asyncio's default
executor: that one also resolves host names (``loop.getaddrinfo``), and conversions that fill it
would starve DNS and time out fetches of unrelated hosts.

A caller first waits for a free slot, then its function runs at once. The wait is an ordinary
await, so the request's deadline also covers it, and no work is queued that nobody waits for any
more. Python cannot stop a running thread: a caller whose deadline passes gets its timeout, but
the thread finishes its work and keeps its slot until then. Bounded concurrency is the
containment; ``CONVERT_MAX_BYTES`` bounds the work of each call, and with it how long a
shutdown can be held up by a conversion that is already running (see ``shutdown``).
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any


class ConversionPool:
    """At most ``workers`` CPU-heavy calls at a time. Create once per app, ``shutdown`` last."""

    def __init__(self, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="convert")
        self._slots = asyncio.Semaphore(workers)

    async def run[T](self, function: Callable[..., T], *args: Any) -> T:
        """``function(*args)`` on a pool thread, once a slot is free."""
        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        try:
            future = self._executor.submit(function, *args)
        except BaseException:
            self._slots.release()
            raise
        # The slot is given back when the thread is done, not when the caller stops waiting.
        future.add_done_callback(partial(self._release, loop))
        return await asyncio.wrap_future(future)

    def shutdown(self) -> None:
        """Stop taking work and drop what is still queued, without waiting here.

        A conversion that is already running is not stopped and not abandoned either:
        ``concurrent.futures`` joins its worker threads when the interpreter exits, so the
        process ends only once that conversion is done. ``CONVERT_MAX_BYTES`` bounds the size,
        not the time: a pathological page can run to the end of the grace period, where the
        pod is killed. That is harmless, because pods keep no state.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _release(self, loop: asyncio.AbstractEventLoop, _future: Future[Any]) -> None:
        try:
            loop.call_soon_threadsafe(self._slots.release)
        except RuntimeError:
            self._slots.release()  # the loop is closed, so nothing waits on it any more
