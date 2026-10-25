"""The entry point of a ``DocumentPool`` child: ``python -m text_extraction.documents.pool_child
<fd> <target> <memory_limit_mb>``.

A plain interpreter of its own, not a ``multiprocessing`` child: it does not re-run the
parent's ``__main__`` (the whole API under the real entry point), and it imports nothing but
the standard library before its memory limit is set. ``fd`` is one end of a socket pair that
the parent keeps the other end of; the messages are ``multiprocessing.connection`` frames of
pickled tuples. Stdin and stdout are not part of the protocol, because converters print.

Messages:
- child to parent once: ``("ready",)``, or ``("failed", type_name, message)`` when the target
  does not load;
- parent to child: ``(function_name, payload)``;
- child to parent: ``("ok", result)``, ``("error", type_name, message)``, ``("oom",)`` or
  ``("unknown", function_name)``.
"""

import importlib
import pickle
import resource
import signal
import sys
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

_MESSAGE_LIMIT = 500  # characters of an exception message that reach the parent


def main(argv: list[str]) -> None:
    # The parent stops its children itself; a Ctrl+C or a signal to the process group must not
    # end a conversion that the parent still waits for.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fd, target, memory_limit_mb = int(argv[0]), argv[1], int(argv[2])
    _limit_memory(memory_limit_mb)
    conn = Connection(fd)
    try:
        functions: dict[str, Callable[[Any], Any]] = importlib.import_module(target).FUNCTIONS
    except BaseException as exc:  # also MemoryError and SystemExit: start() reports it
        conn.send_bytes(_dump(("failed", type(exc).__name__, _message(exc))))
        return
    conn.send_bytes(_dump(("ready",)))
    while True:
        try:
            request = conn.recv_bytes()
        except EOFError:
            return  # the parent is gone
        conn.send_bytes(_answer(functions, request))


def _limit_memory(megabytes: int) -> None:
    limit = megabytes * 1024 * 1024
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _answer(functions: dict[str, Callable[[Any], Any]], request: bytes) -> bytes:
    try:
        name, payload = pickle.loads(request)  # noqa: S301 - from our own parent
        function = functions.get(name)
        if function is None:
            return _dump(("unknown", name))
        return _dump(("ok", function(payload)))
    except MemoryError:
        return _dump(("oom",))
    except Exception as exc:
        return _dump(("error", type(exc).__name__, _message(exc)))


def _dump(message: tuple[Any, ...]) -> bytes:
    return pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)


def _message(exc: BaseException) -> str:
    return str(exc)[:_MESSAGE_LIMIT]


if __name__ == "__main__":
    main(sys.argv[1:])
