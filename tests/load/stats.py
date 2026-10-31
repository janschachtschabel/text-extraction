"""``docker stats`` sampling for the load test (ruling P11-S2).

``Sampler`` runs ``docker stats --no-stream --format '{{json .}}'`` every 2 s while a load run
lasts and writes each container's line to a JSONL file, stamped with ``t``: the wall-clock time
(epoch seconds) at which the reading came back. ``parse_line`` turns a line into a
``StatSample``: CPU in percent of one core (``CPUPerc``, 100 = one core busy) and memory in MiB
(``MemUsage``, the cgroup's usage without page cache, as docker reports it).
"""

import asyncio
import contextlib
import json
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Self

DOCKER_STATS: tuple[str, ...] = ("docker", "stats", "--no-stream", "--format", "{{json .}}")
INTERVAL_SECONDS = 2.0

_MIB_PER_UNIT = {
    "B": 1 / 1024 / 1024,
    "KiB": 1 / 1024,
    "MiB": 1.0,
    "GiB": 1024.0,
    "TiB": 1024.0 * 1024,
}
_SIZE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB)\s*")
_PERCENT = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)%\s*")


@dataclass(frozen=True)
class StatSample:
    """One container's reading: ``t`` in epoch seconds, CPU in percent of one core, memory in
    MiB."""

    t: float
    container: str
    cpu_percent: float
    mem_mib: float


def _mib(size: str) -> float:
    match = _SIZE.fullmatch(size)
    if match is None:
        raise ValueError(f"not a memory size: {size!r}")
    return float(match.group(1)) * _MIB_PER_UNIT[match.group(2)]


def parse_line(line: str, *, t: float | None = None) -> StatSample:
    """The sample of one ``docker stats --format '{{json .}}'`` line. Its time is ``t``, else
    the line's own ``t`` (a line the sampler wrote). A line without a reading (a container that
    is not running shows ``--``), without a time or that is no JSON is a ``ValueError``."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("not a JSON line") from error
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    try:
        name = str(data["Name"])
        cpu = _PERCENT.fullmatch(str(data["CPUPerc"]))
        used = str(data["MemUsage"]).split("/", 1)[0]
        stamp = t if t is not None else float(data["t"])
    except KeyError as error:
        raise ValueError(f"the line has no {error.args[0]}") from None
    if cpu is None:
        raise ValueError(f"not a CPU percentage: {data['CPUPerc']!r}")
    return StatSample(t=stamp, container=name, cpu_percent=float(cpu.group(1)), mem_mib=_mib(used))


@dataclass
class Sampler:
    """Samples ``command`` (``docker stats`` by default) every ``interval`` seconds into
    ``path`` (JSONL, one stamped line per container and reading) from entry until exit. The
    readings are also kept in ``samples``. A reading that fails or a line without a reading is
    skipped with a note on stderr: one bad second must not end a load run."""

    path: Path
    interval: float = INTERVAL_SECONDS
    command: Sequence[str] = DOCKER_STATS
    samples: list[StatSample] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None
    _sink: IO[str] | None = None

    async def __aenter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sink = self.path.open("w", encoding="utf-8")
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._sink is not None:
            self._sink.close()

    async def _run(self) -> None:
        while True:
            started = time.monotonic()
            await self._read_once()
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - started)))

    async def _read_once(self) -> None:
        assert self._sink is not None
        process = await asyncio.create_subprocess_exec(
            *self.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        stamp = time.time()
        if process.returncode != 0:
            note = err.decode(errors="replace").strip()[:200]
            print(f"stats: exit {process.returncode}: {note}", file=sys.stderr)
            return
        for line in out.decode(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                sample = parse_line(line, t=stamp)
            except ValueError:
                continue  # a container that is not running, or a line without a reading
            self.samples.append(sample)
            self._sink.write(json.dumps({**json.loads(line), "t": stamp}) + "\n")
        self._sink.flush()
