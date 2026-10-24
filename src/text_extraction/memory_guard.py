"""Valkey memory guard of the API and worker pods.

Valkey runs with ``noeviction``: when it is full it refuses writes, and with them the rate
limiter, the outbound gate and the job reads stop working. Results of job items are the bulk of
what fills it, so the API stops taking on new bulk while Valkey is nearly full:

- ``POST /v1/batch`` answers 503 ``internal_error`` (retryable);
- ``POST /v1/extract`` with ``wait_seconds`` does not hand a slow extraction over to a job and
  keeps waiting for it inline;
- the result cache skips its writes, on the API pods and on the workers (whose cache writes are
  the second copy of every batch result, the larger share of a flood).

Workers keep storing the results of the items they have in the job store, so accepted jobs still
complete.

A background task reads ``INFO memory`` every ``interval`` seconds (``used_memory`` and
``maxmemory``); the guard holds while ``used_memory`` is at least ``VALKEY_MEMORY_GUARD_RATIO`` of
``maxmemory``. Without a ``maxmemory`` (0) there is no guard. When the read fails the last values
stay and the outage is logged once. The values are exported as
``text_extraction_valkey_used_memory_bytes`` and ``text_extraction_valkey_max_memory_bytes`` from
the first successful read on; API pods and workers export them alike (one Valkey: use
``max()`` over the pods).
"""

import asyncio
import contextlib
from collections.abc import Iterable, Iterator
from typing import Final

import structlog
from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from redis.asyncio import Redis

_log = structlog.get_logger(__name__)

# Seconds between two reads of Valkey's memory use.
REFRESH_SECONDS: Final = 5.0

_USED: Final = (
    "text_extraction_valkey_used_memory_bytes",
    "Memory Valkey uses for data (INFO memory used_memory), as last read by this API pod.",
)
_MAX: Final = (
    "text_extraction_valkey_max_memory_bytes",
    "Valkey's maxmemory (INFO memory maxmemory; 0: no limit), as last read by this API pod.",
)


class MemoryGuard(Collector):
    """Whether Valkey is nearly full (``high``), fed by a background task; also the collector of
    the two memory gauges."""

    def __init__(
        self,
        valkey: Redis,
        ratio: float,
        registry: CollectorRegistry,
        *,
        interval: float = REFRESH_SECONDS,
    ) -> None:
        self._valkey = valkey
        self._ratio = ratio
        self._interval = interval
        self._used: int | None = None
        self._max: int | None = None
        self._high = False
        self._down = False
        self._task: asyncio.Task[None] | None = None
        registry.register(self)

    @property
    def high(self) -> bool:
        """True while the last read had ``used_memory`` at or above the ratio of a non-zero
        ``maxmemory``. False before the first successful read."""
        return self._high

    @property
    def running(self) -> bool:
        """True while the refresh task is running (started and not stopped)."""
        return self._task is not None and not self._task.done()

    def describe(self) -> Iterable[GaugeMetricFamily]:
        return [GaugeMetricFamily(*_USED), GaugeMetricFamily(*_MAX)]

    def collect(self) -> Iterator[GaugeMetricFamily]:
        if self._used is None or self._max is None:
            return
        yield GaugeMetricFamily(*_USED, value=self._used)
        yield GaugeMetricFamily(*_MAX, value=self._max)

    async def refresh(self) -> None:
        """Read Valkey's memory use once. A failure keeps the last values (and the last verdict)
        and is logged on the first failure of an outage; the first success after it is logged
        too, and so is every change of the verdict."""
        try:
            info = await self._valkey.info("memory")
            used, maximum = int(info["used_memory"]), int(info["maxmemory"])
        except Exception as error:  # a failing read must never end the loop
            if not self._down:
                self._down = True
                _log.warning(
                    "valkey_memory_unavailable", error_type=type(error).__name__, error=str(error)
                )
            return
        if self._down:
            self._down = False
            _log.info("valkey_memory_recovered")
        self._used, self._max = used, maximum
        high = maximum > 0 and used >= self._ratio * maximum
        if high != self._high:
            self._high = high
            event = "valkey_memory_high" if high else "valkey_memory_normal"
            log = _log.warning if high else _log.info
            log(event, used_bytes=used, max_bytes=maximum, ratio=self._ratio)

    async def _run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        """Start the refresh task in the running event loop; a second call does nothing."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="valkey-memory-guard")

    async def stop(self) -> None:
        """Cancel the refresh task and wait until it has ended. Call it before the Valkey client
        is closed."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
