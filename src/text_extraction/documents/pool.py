"""Child processes for document conversions (ruling F3): a conversion that runs too long or uses
too much memory is killed, not waited for.

Documents are hostile input in a way HTML is not: a PDF can make pdfminer spin for minutes, a
spreadsheet can expand to gigabytes. Python cannot stop a thread, so ``ConversionPool`` is no
containment for them. ``DocumentPool`` keeps ``processes`` children instead. Each runs one call
at a time and is replaced when it is killed, dies, runs out of memory or has served
``max_tasks`` calls.

- **The child.** A plain interpreter, ``python -m text_extraction.documents.pool_child``, in a
  session of its own, not a ``multiprocessing`` child: that one would re-run the parent's
  ``__main__`` (the whole API, about 1 s and 300 MB of address space) before its limit is set.
  It gets an allowlisted environment (no API keys, no Valkey password, no LLM keys) and talks
  over a socket pair in ``multiprocessing.connection`` frames; stdin and stdout are not used.
- **What a child may run.** ``target`` is a module path; the child imports it and calls the
  functions of its ``FUNCTIONS`` dict by name. Nothing callable is pickled from the parent, and
  the parent never imports the target, so the heavy converter libraries live in the children
  only. The child parses hostile files, so the parent unpickles its replies with builtin types
  only and at most ``max_result_bytes`` of them.
- **Memory.** The child sets ``RLIMIT_AS`` to ``memory_limit_mb`` before it imports the target.
  A ``MemoryError`` in a call, or a result over ``max_result_bytes``, is
  ``ConversionOutOfMemory``, and the child is replaced, as its heap may be left fragmented.
- **Deadline.** ``run`` takes the caller's deadline (``loop.time()``). Waiting for a free child
  counts against it, as with ``ConversionPool``. When it passes, the child's process group gets
  SIGKILL (so do tools the converter started, such as tesseract) and the caller
  ``ConversionTimeout``. A cancelled caller's child is killed too: it may be mid-conversion, and
  nobody waits for its answer any more.
- **Signals.** Children ignore SIGINT and SIGTERM: the parent stops them in ``shutdown``, and an
  exit hook kills them if the parent exits without it.

The blocking socket I/O, pickling and process starts run on short-lived threads of their own,
never on the event loop or asyncio's default executor (which also resolves host names, see
``conversion_pool``).
"""

import asyncio
import atexit
import concurrent.futures
import contextlib
import io
import os
import pickle
import signal
import socket
import subprocess
import sys
import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from multiprocessing.connection import Connection
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

_CLOSED = "the document pool is shut down"
_CHILD_MODULE = "text_extraction.documents.pool_child"
_MESSAGE_LIMIT = 500  # characters of an exception message that reach the caller
_RESPAWN_BACKOFF_SECONDS = 1.0  # between attempts to start a child while that keeps failing
# What a child inherits from the parent's environment; ``extra_env`` adds to it.
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "TESSDATA_PREFIX",
    "OMP_NUM_THREADS",
)


class ConversionTimeout(Exception):  # noqa: N818 - the name is part of the task's interface
    """The deadline passed before a child was free or before it answered."""


class ConversionCrashed(Exception):  # noqa: N818 - the name is part of the task's interface
    """The child died during the call (a crash in native code, killed by the system)."""


class ConversionOutOfMemory(Exception):  # noqa: N818 - the name is part of the task's interface
    """The call reached the child's memory limit, or its result was over the size limit."""


class ConversionError(Exception):
    """The function raised in the child.

    ``message`` is the exception's own text, cut to 500 characters. The pool adds nothing from
    the payload, but a converter's message may quote the document: callers log ``type_name``
    only.
    """

    def __init__(self, type_name: str, message: str) -> None:
        super().__init__(f"{type_name}: {message}" if message else type_name)
        self.type_name = type_name
        self.message = message


class _StartFailed(RuntimeError):  # noqa: N818 - a start that failed, named as such
    """A child did not get ready; ``type_name`` is the child's exception, when it sent one."""

    def __init__(self, message: str, type_name: str | None = None) -> None:
        super().__init__(message)
        self.type_name = type_name


@dataclass(eq=False)
class _Child:
    process: subprocess.Popen[bytes]
    conn: Connection
    calls: int = 0
    exchange: concurrent.futures.Future[tuple[Any, ...]] | None = None  # the call in flight


class DocumentPool:
    """``processes`` children that run the functions of ``target``. ``start`` before the first
    call (before ``/ready``), ``shutdown`` last."""

    def __init__(
        self,
        processes: int,
        memory_limit_mb: int,
        *,
        max_tasks: int = 50,
        target: str,
        max_result_bytes: int = 64 * 1024 * 1024,
        extra_env: Mapping[str, str] | None = None,
        start_timeout: float = 60.0,
    ) -> None:
        if processes < 1:
            raise ValueError("processes must be at least 1")
        if memory_limit_mb < 1:
            raise ValueError("memory_limit_mb must be at least 1")
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")
        if max_result_bytes < 1:
            raise ValueError("max_result_bytes must be at least 1")
        self._processes = processes
        self._memory_limit_mb = memory_limit_mb
        self._max_tasks = max_tasks
        self._target = target
        self._max_result_bytes = max_result_bytes
        self._start_timeout = start_timeout
        self._env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
        self._env.update(extra_env or {})
        # Free children; None stands for a slot whose child could not be started yet.
        self._idle: asyncio.Queue[_Child | None] = asyncio.Queue()
        self._lock = threading.Lock()  # guards _children and _closed across the I/O threads
        self._children: set[_Child] = set()  # every child that is started and not yet reaped
        self._tasks: set[asyncio.Task[None]] = set()  # replacements in progress
        self._closing = asyncio.Event()  # ends a replacement's back-off
        self._respawn_failing = False  # logged once per streak of failed starts
        self._started = False
        self._closed = False
        _live_pools.add(self)

    async def start(self) -> None:
        """Start all children and wait until each has imported the target. A target that does
        not load fails here, with its exception's type and message."""
        if self._closed:
            raise RuntimeError(_CLOSED)
        if self._started:
            return
        self._started = True
        spawned = await asyncio.gather(
            *(_in_thread(self._spawn) for _ in range(self._processes)), return_exceptions=True
        )
        failures = [result for result in spawned if isinstance(result, BaseException)]
        if failures:
            await self.shutdown()
            raise failures[0]
        for child in spawned:
            if isinstance(child, _Child):
                self._idle.put_nowait(child)

    async def run(
        self, function_name: str, payload: bytes | dict[str, Any], deadline: float
    ) -> Any:
        """``FUNCTIONS[function_name](payload)`` in a free child, by ``deadline`` (``loop.time()``).

        Raises ``ConversionTimeout``, ``ConversionCrashed``, ``ConversionOutOfMemory``,
        ``ConversionError`` for what the function raised, ``LookupError`` for a name the target
        does not offer, ``TypeError`` for a payload that cannot be pickled, and ``RuntimeError``
        once the pool is shut down.
        """
        if self._closed:
            raise RuntimeError(_CLOSED)
        if not self._started:
            raise RuntimeError("the document pool is not started")
        if deadline <= asyncio.get_running_loop().time():
            raise ConversionTimeout("the deadline passed before the conversion started")
        child = await self._take(deadline)
        call = partial(_exchange, child.conn, function_name, payload, self._max_result_bytes)
        child.exchange = exchange = _start_thread(call)
        try:
            async with asyncio.timeout_at(deadline):
                reply = await asyncio.wrap_future(exchange)
        except BaseException as exc:
            # The child may be mid-conversion: whatever ended the wait, it is killed.
            self._retire(child)
            if isinstance(exc, TimeoutError):
                raise ConversionTimeout(
                    "the conversion did not finish before the deadline"
                ) from None
            if isinstance(exc, (EOFError, OSError)):
                if self._closed:
                    raise RuntimeError(_CLOSED) from None
                raise ConversionCrashed("the conversion process died") from None
            raise
        child.exchange = None
        status = reply[0]
        if status == "oom":
            self._retire(child)
            raise ConversionOutOfMemory("the conversion reached the memory limit")
        if status == "too_large":
            self._retire(child)
            raise ConversionOutOfMemory("the conversion result is over the size limit")
        if status == "unpicklable":
            self._give_back(child)  # nothing was sent
            raise TypeError(f"the payload cannot be pickled ({reply[1]})")
        if status == "unknown":
            self._give_back(child)
            raise LookupError(f"the document pool target has no function {reply[1]!r}")
        child.calls += 1
        self._give_back(child)
        if status == "error":
            raise ConversionError(reply[1], reply[2])
        return reply[1]

    async def shutdown(self) -> None:
        """Kill and reap all children. Calls in flight and calls after it raise
        ``RuntimeError``. Safe to call more than once."""
        with self._lock:
            self._closed = True
            children = list(self._children)
        self._closing.set()
        for child in children:
            _kill(child.process)
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        with self._lock:
            children = list(self._children)
        for child in children:
            _kill(child.process)
        await _in_thread(partial(self._reap_all, children))
        self._idle.put_nowait(None)  # wakes a caller waiting for a child, who then gives up
        _live_pools.discard(self)

    def abandon(self) -> None:
        """Kill the children now, without waiting for them, and close the pool: for a pool
        whose event loop is gone, which ``shutdown`` cannot be awaited on."""
        with self._lock:
            self._closed = True
        self._kill_now()

    def child_pids(self) -> list[int]:
        """The process ids of the children (for tests and diagnostics)."""
        with self._lock:
            return [child.process.pid for child in self._children]

    async def _take(self, deadline: float) -> _Child:
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    child = await self._idle.get()
                    if self._closed:
                        self._idle.put_nowait(None)  # wake the next waiter too
                        raise RuntimeError(_CLOSED)
                    if child is not None:
                        return child
                    self._replace(None)  # an empty slot: start its child, then wait again
        except TimeoutError:
            raise ConversionTimeout("no conversion process was free before the deadline") from None

    def _give_back(self, child: _Child) -> None:
        if child.calls >= self._max_tasks:
            self._retire(child)
        else:
            self._idle.put_nowait(child)

    def _retire(self, child: _Child) -> None:
        """Kill ``child`` now; reap it and start its replacement in the background."""
        _kill(child.process)
        self._replace(child)

    def _replace(self, old: _Child | None) -> None:
        task = asyncio.get_running_loop().create_task(self._replacement(old))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _replacement(self, old: _Child | None) -> None:
        child: _Child | None = None
        try:
            if old is not None:
                await _in_thread(partial(self._reap, old))
            child = await _in_thread(self._spawn)
        except Exception as exc:
            # The slot stays empty; the next caller that takes it tries again, after a pause.
            if not self._closed:
                self._log_respawn_failure(exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._closing.wait(), _RESPAWN_BACKOFF_SECONDS)
        else:
            self._respawn_failing = False
        finally:
            self._idle.put_nowait(child)

    def _log_respawn_failure(self, exc: Exception) -> None:
        if self._respawn_failing:
            return
        self._respawn_failing = True
        # Types only: a child's message may quote the document it choked on.
        fields = {"error": type(exc).__name__}
        if isinstance(exc, _StartFailed) and exc.type_name:
            fields["child_error"] = exc.type_name
        _log.warning("document_pool_respawn_failed", **fields)

    def _spawn(self) -> _Child:
        """Start a child and wait for its readiness message (blocking; runs on a thread)."""
        ours, theirs = socket.socketpair()
        conn = Connection(ours.detach())
        child_fd = theirs.detach()
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError(_CLOSED)
                command = [sys.executable, "-m", _CHILD_MODULE, str(child_fd), self._target]
                process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
                    [*command, str(self._memory_limit_mb)],
                    pass_fds=(child_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,  # converters print; stderr stays for crashes
                    env=self._env,
                    start_new_session=True,  # its own process group, killed as a whole
                )
                child = _Child(process, conn)
                self._children.add(child)
        except BaseException:
            conn.close()
            raise
        finally:
            os.close(child_fd)  # the child has its copy; ours must go, or its death is never seen
        try:
            if not conn.poll(self._start_timeout):
                raise _StartFailed(
                    f"a document pool child did not start within {self._start_timeout:g} s"
                )
            reply = _load_reply(conn.recv_bytes(self._max_result_bytes))
        except (EOFError, OSError):
            self._reap(child)
            raise _StartFailed(
                f"a document pool child exited at start (exit code {process.returncode})"
            ) from None
        except BaseException:
            self._reap(child)
            raise
        if reply[0] != "ready":
            self._reap(child)
            raise _StartFailed(
                f"a document pool child failed to start: {reply[1]}: {reply[2]}", reply[1]
            )
        return child

    def _reap(self, child: _Child) -> None:
        """Kill, wait for and close ``child`` once (blocking; runs on a thread)."""
        with self._lock:
            if child not in self._children:
                return
            self._children.discard(child)
        _kill(child.process)  # also when it died by itself: its own children go with it
        if child.exchange is not None:
            # The killed child ends the call's recv; only then may its socket be closed.
            concurrent.futures.wait([child.exchange])
        child.process.wait()
        child.conn.close()

    def _reap_all(self, children: list[_Child]) -> None:
        for child in children:
            self._reap(child)

    def _kill_now(self) -> None:
        for child in list(self._children):
            _kill(child.process)


def _exchange(
    conn: Connection, function_name: str, payload: Any, max_result_bytes: int
) -> tuple[Any, ...]:
    """Send a call, wait for its answer (blocking; runs on a thread). Raises ``EOFError`` or
    ``OSError`` when the child dies."""
    try:
        request = pickle.dumps((function_name, payload), protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        return ("unpicklable", type(exc).__name__)
    conn.send_bytes(request)
    try:
        answer = conn.recv_bytes(max_result_bytes)
    except OSError as exc:
        if exc.args == ("bad message length",):  # over the limit; the body was not read
            return ("too_large",)
        raise
    return _load_reply(answer)


class _BuiltinsOnly(pickle.Unpickler):
    """Unpickles builtin containers and scalars, and refuses every class or function."""

    def find_class(self, module: str, name: str) -> Any:
        raise pickle.UnpicklingError(f"a reply may hold builtin values only, not {module}.{name}")


def _load_reply(answer: bytes) -> tuple[Any, ...]:
    try:
        reply = _BuiltinsOnly(io.BytesIO(answer)).load()
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc)[:_MESSAGE_LIMIT])
    if not isinstance(reply, tuple) or not reply:
        return ("error", "UnpicklingError", "a reply must be a non-empty tuple")
    return reply


def _kill(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's process group, unless the child is reaped already."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


def _start_thread[T](function: Callable[[], T]) -> concurrent.futures.Future[T]:
    """Run ``function`` on a daemon thread of its own.

    Not an executor: its threads are joined when the interpreter exits, and one blocked on a
    stuck child would hold up the exit.
    """
    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def work() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=work, name="document-pool", daemon=True).start()
    return future


async def _in_thread[T](function: Callable[[], T]) -> T:
    return await asyncio.wrap_future(_start_thread(function))


_live_pools: weakref.WeakSet[DocumentPool] = weakref.WeakSet()


@atexit.register
def _kill_children_at_exit() -> None:
    # Children ignore SIGTERM and live in sessions of their own: a parent that exits without
    # ``shutdown`` must kill them, or a busy one would run on until its conversion ends.
    for pool in list(_live_pools):
        pool._kill_now()
