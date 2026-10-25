"""A ``DocumentPool`` target for the tests: the functions a test child may call."""

import os
import subprocess
import sys
import time
from collections.abc import Callable
from fractions import Fraction
from typing import Any

_calls = 0


def echo(payload: Any) -> Any:
    return payload


def sleep_forever(_payload: Any) -> None:
    while True:
        time.sleep(1)


def allocate(payload: dict[str, int]) -> int:
    block = bytearray(payload["megabytes"] * 1024 * 1024)
    return len(block)


def crash(_payload: Any) -> None:
    os._exit(3)


def count_calls(_payload: Any) -> int:
    global _calls
    _calls += 1
    return _calls


def pid(_payload: Any) -> int:
    return os.getpid()


def fail(_payload: Any) -> None:
    raise ValueError("the converter broke")


def environment(_payload: Any) -> dict[str, str]:
    return dict(os.environ)


def has_module(payload: dict[str, str]) -> bool:
    return payload["name"] in sys.modules


def spawn_sleeper(payload: dict[str, str]) -> None:
    """Start a grandchild, write its pid to ``payload["pid_file"]``, then hang."""
    sleeper = subprocess.Popen(["sleep", "60"])  # noqa: S607 - a test grandchild
    with open(payload["pid_file"], "w") as file:
        file.write(str(sleeper.pid))
    sleep_forever(None)


def fraction(_payload: Any) -> Fraction:
    return Fraction(1, 3)  # not a builtin type: the parent must refuse to unpickle it


FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "echo": echo,
    "sleep_forever": sleep_forever,
    "allocate": allocate,
    "crash": crash,
    "count_calls": count_calls,
    "pid": pid,
    "fail": fail,
    "environment": environment,
    "has_module": has_module,
    "spawn_sleeper": spawn_sleeper,
    "fraction": fraction,
}
