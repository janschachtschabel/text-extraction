"""The load test's ``docker stats`` sampling (`tests/load/stats.py`): parsing one line of
``docker stats --format '{{json .}}'`` and the sampler that writes them as JSONL."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from tests.load import stats as stats_module
from tests.load.stats import Sampler, StatSample, parse_line


def docker_line(name: str, cpu: str, memory: str) -> str:
    """A line as ``docker stats --no-stream --format '{{json .}}'`` prints it."""
    return json.dumps(
        {
            "BlockIO": "0B / 0B",
            "CPUPerc": cpu,
            "Container": "3f1c2a9b8d7e",
            "ID": "3f1c2a9b8d7e",
            "MemPerc": "7.69%",
            "MemUsage": memory,
            "Name": name,
            "NetIO": "1.2kB / 0B",
            "PIDs": "12",
        }
    )


def test_parse_line_reads_cpu_and_memory() -> None:
    api = parse_line(docker_line("text-extraction-api-1", "153.27%", "1.2GiB / 15.6GiB"), t=10.0)
    worker = parse_line(
        docker_line("text-extraction-worker-1", "0.00%", "512MiB / 15.6GiB"), t=12.0
    )

    assert api == StatSample(
        t=10.0, container="text-extraction-api-1", cpu_percent=153.27, mem_mib=1228.8
    )
    assert worker.container == "text-extraction-worker-1"
    assert worker.cpu_percent == 0.0
    assert worker.mem_mib == 512.0
    assert worker.t == 12.0


@pytest.mark.parametrize(
    ("memory", "mib"),
    [("2048KiB / 15.6GiB", 2.0), ("1048576B / 15.6GiB", 1.0), ("0B / 0B", 0.0)],
)
def test_parse_line_reads_every_memory_unit(memory: str, mib: float) -> None:
    assert parse_line(docker_line("valkey", "1.5%", memory), t=0.0).mem_mib == mib


def test_parse_line_takes_the_time_of_a_sampled_line() -> None:
    """A line of ``stats.jsonl`` carries the time the sampler stamped on it."""
    line = json.dumps({**json.loads(docker_line("api", "1%", "1MiB / 1GiB")), "t": 99.5})

    assert parse_line(line).t == 99.5


@pytest.mark.parametrize(
    "line",
    [
        docker_line("stopped", "--", "-- / --"),  # a container that is not running
        docker_line("api", "12%", "12 parsecs / 1GiB"),
        docker_line("api", "12%", "12MiB / 1GiB"),  # no time: neither given nor stamped
        "not json",
    ],
)
def test_parse_line_refuses_a_line_without_a_reading(line: str) -> None:
    with pytest.raises(ValueError, match=r"^(not a|the line has no)"):
        parse_line(line)


def test_sampler_writes_stamped_jsonl_until_it_stops(tmp_path: Path) -> None:
    lines = [
        docker_line("text-extraction-api-1", "20.00%", "100MiB / 1GiB"),
        docker_line("stopped", "--", "-- / --"),  # skipped, not fatal
    ]
    script = "import sys; sys.stdout.write(sys.argv[1])"
    command = [sys.executable, "-c", script, "\n".join(lines) + "\n"]
    out = tmp_path / "stats.jsonl"

    async def sample() -> Sampler:
        async with Sampler(out, interval=0.05, command=command) as sampler:
            await asyncio.sleep(0.5)
        return sampler

    sampler = asyncio.run(sample())

    written = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(written) >= 2
    assert {line["Name"] for line in written} == {"text-extraction-api-1"}
    assert [parse_line(json.dumps(line)) for line in written] == sampler.samples
    times = [sample.t for sample in sampler.samples]
    assert times == sorted(times)
    assert sampler.samples[0].mem_mib == 100.0


def test_sampler_survives_a_command_that_cannot_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No docker on the machine: the run goes on without samples, with a note on stderr."""
    out = tmp_path / "stats.jsonl"

    async def sample() -> Sampler:
        async with Sampler(out, interval=0.05, command=[str(tmp_path / "no-docker")]) as sampler:
            await asyncio.sleep(0.2)
        return sampler

    sampler = asyncio.run(sample())

    assert sampler.samples == []
    assert out.read_text() == ""
    assert "stats:" in capsys.readouterr().err


class _Clock:
    """A wall clock for the sampler: the given readings in turn, then the last one again;
    ``read`` is set once the last one was handed out. Its ``monotonic`` is the real one, which
    the sampler's pacing uses."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)
        self.monotonic = time.monotonic
        self.read = asyncio.Event()

    def time(self) -> float:
        if len(self._readings) > 1:
            return self._readings.pop(0)
        self.read.set()
        return self._readings[0]


def test_a_reading_is_stamped_with_the_midpoint_of_its_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``docker stats --no-stream`` takes a second or two; its reading describes that span, so
    the stamp is its middle, not its end. The wall clock is injected (the call starts at 1000.0
    and ends at 1002.0), so a slow child process cannot move the stamp."""
    line = docker_line("text-extraction-api-1", "1%", "1MiB / 1GiB")
    command = [sys.executable, "-c", f"print({line!r})"]
    clock = _Clock(1000.0, 1002.0)
    monkeypatch.setattr(stats_module, "time", clock)
    out = tmp_path / "stats.jsonl"

    async def sample() -> Sampler:
        async with Sampler(out, interval=60, command=command) as sampler:
            # The call's end is stamped: its reading is kept before the sampler awaits again.
            # Only a hung child could take 30 s.
            await asyncio.wait_for(clock.read.wait(), timeout=30)
        return sampler

    sampler = asyncio.run(sample())

    assert [reading.t for reading in sampler.samples] == [1001.0]
    assert json.loads(out.read_text())["t"] == 1001.0
