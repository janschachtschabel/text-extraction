"""The event-loop lag of a worker process (package 11, ruling P11-S7).

A background task sleeps ``SAMPLE_SECONDS`` (0.5 s) at a time and records how much later than
asked it woke up: the time the loop spent on other work without yielding, such as packing and
encoding the parts of a large sitemap. The samples go into the histogram
``text_extraction_event_loop_lag_seconds`` of the process's registry, which the internal port
exports (``/metrics``). The extract worker runs it from the start to the end of the process.
"""

import asyncio
import contextlib
from typing import Final
from weakref import WeakKeyDictionary

from prometheus_client import CollectorRegistry, Histogram

LAG_METRIC: Final = "text_extraction_event_loop_lag_seconds"
SAMPLE_SECONDS: Final = 0.5
# From a busy but healthy loop (milliseconds) to one held for longer than a lease's margin.
_BUCKETS: Final = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# One histogram per registry, so several workers (tests) can share one.
_HISTOGRAMS: WeakKeyDictionary[CollectorRegistry, Histogram] = WeakKeyDictionary()


def _histogram_for(registry: CollectorRegistry) -> Histogram:
    histogram = _HISTOGRAMS.get(registry)
    if histogram is None:
        histogram = Histogram(
            LAG_METRIC,
            f"How much later than asked the event loop woke from a {SAMPLE_SECONDS} s sleep: "
            "the time it ran other work without yielding.",
            buckets=_BUCKETS,
            registry=registry,
        )
        _HISTOGRAMS[registry] = histogram
    return histogram


class LoopLag:
    """Samples the running loop's lag every ``interval`` seconds into ``registry``'s
    histogram, from ``start`` to ``stop``."""

    def __init__(self, registry: CollectorRegistry, *, interval: float = SAMPLE_SECONDS) -> None:
        self._histogram = _histogram_for(registry)
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """True while the sampling task runs (started and not stopped)."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start sampling on the running loop (again after a ``stop``)."""
        if not self.running:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop sampling and wait for the task to end."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            asked = loop.time() + self._interval
            await asyncio.sleep(self._interval)
            self._histogram.observe(max(loop.time() - asked, 0.0))
