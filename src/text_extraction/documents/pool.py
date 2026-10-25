"""Child processes for document conversions (ruling F3): a conversion that runs too long or uses
too much memory is killed, not waited for.

Documents are hostile input in a way HTML is not: a PDF can make pdfminer spin for minutes, a
spreadsheet can expand to gigabytes. Python cannot stop a thread, so ``ConversionPool`` is no
containment for them. ``DocumentPool`` keeps ``processes`` children instead (start method
``spawn``, so no state of the parent is shared). Each runs one call at a time and is replaced
when it is killed, dies, runs out of memory or has served ``max_tasks`` calls.

- **What a child may run.** ``target`` is a module path; the child imports it and calls the
  functions of its ``FUNCTIONS`` dict by name. Nothing callable is pickled from the parent, and
  the parent never imports the target, so the heavy converter libraries live in the children
  only. Payloads and results travel pickled over a duplex pipe, one per child.
- **Memory.** The child sets ``RLIMIT_AS`` to ``memory_limit_mb`` before it imports the target.
  A ``MemoryError`` in a call is ``ConversionOutOfMemory``, and the child is replaced, as its
  heap may be left fragmented.
- **Deadline.** ``run`` takes the caller's deadline (``loop.time()``). Waiting for a free child
  counts against it, as with ``ConversionPool``. When it passes, the child gets SIGKILL and the
  caller ``ConversionTimeout``. A cancelled caller's child is killed too: it may be
  mid-conversion, and nobody waits for its answer any more.
- **Signals.** Children ignore SIGINT and SIGTERM, so a Ctrl+C or a signal to the process group
  does not kill a conversion that the parent still waits for: the parent stops them in
  ``shutdown``, and an exit hook kills them if the parent exits without it.

The blocking pipe I/O and process starts run on short-lived threads of their own, never on the
event loop or asyncio's default executor (which also resolves host names, see
``conversion_pool``).
"""

import asyncio
import atexit
import concurrent.futures
import importlib
import pickle
import resource
import signal
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any

_CLOSED = "the document pool is shut down"
_MESSAGE_LIMIT = 500  # characters of a child's exception message that reach the parent


class ConversionTimeout(Exception):  # noqa: N818 - the name is part of the task's interface
    """The deadline passed before a child was free or before it answered."""


class ConversionCrashed(Exception):  # noqa: N818 - the name is part of the task's interface
    """The child died during the call (a crash in native code, killed by the system)."""


class ConversionOutOfMemory(Exception):  # noqa: N818 - the name is part of the task's interface
    """The call reached the child's memory limit."""


class ConversionError(Exception):
    """The function raised in the child. ``message`` is the exception's own text, cut to 500
    characters; the pool adds nothing from the payload."""

    def __init__(self, type_name: str, message: str) -> None:
        super().__init__(f"{type_name}: {message}" if message else type_name)
        self.type_name = type_name
        self.message = message


# --- the child -----------------------------------------------------------------------------


def _child_main(conn: Connection, target: str, memory_limit_mb: int) -> None:
    """The child's entry point: limit memory, import the target, then answer calls until the
    parent closes the pipe."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _limit_memory(memory_limit_mb)
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


# --- the parent ----------------------------------------------------------------------------


@dataclass(eq=False)
class _Child:
    process: BaseProcess
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
        start_timeout: float = 60.0,
    ) -> None:
        if processes < 1:
            raise ValueError("processes must be at least 1")
        if memory_limit_mb < 1:
            raise ValueError("memory_limit_mb must be at least 1")
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")
        self._processes = processes
        self._memory_limit_mb = memory_limit_mb
        self._max_tasks = max_tasks
        self._target = target
        self._start_timeout = start_timeout
        self._context = get_context("spawn")
        # Free children; None stands for a slot whose child could not be started yet.
        self._idle: asyncio.Queue[_Child | None] = asyncio.Queue()
        self._lock = threading.Lock()  # guards _children and _closed across the I/O threads
        self._children: set[_Child] = set()  # every child that is started and not yet reaped
        self._tasks: set[asyncio.Task[None]] = set()  # replacements in progress
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
        does not offer, and ``RuntimeError`` once the pool is shut down.
        """
        if self._closed:
            raise RuntimeError(_CLOSED)
        if not self._started:
            raise RuntimeError("the document pool is not started")
        if deadline <= asyncio.get_running_loop().time():
            raise ConversionTimeout("the deadline passed before the conversion started")
        request = _dump((function_name, payload))
        child = await self._take(deadline)
        child.exchange = exchange = _start_thread(partial(_exchange, child.conn, request))
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

    def child_pids(self) -> list[int]:
        """The process ids of the children (for tests and diagnostics)."""
        with self._lock:
            return [child.process.pid for child in self._children if child.process.pid]

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
        except Exception:
            child = None  # the slot stays empty; the next caller that takes it tries again
        finally:
            self._idle.put_nowait(child)

    def _spawn(self) -> _Child:
        """Start a child and wait for its readiness message (blocking; runs on a thread)."""
        ours, theirs = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_child_main,
            args=(theirs, self._target, self._memory_limit_mb),
            name="document-pool",
            daemon=True,
        )
        child = _Child(process, ours)
        with self._lock:
            if self._closed:
                ours.close()
                theirs.close()
                raise RuntimeError(_CLOSED)
            try:
                process.start()
            except BaseException:
                ours.close()
                theirs.close()
                raise
            self._children.add(child)
        theirs.close()  # the child has its copy; ours must go, or its death is never seen
        try:
            if not ours.poll(self._start_timeout):
                raise RuntimeError(
                    f"a document pool child did not start within {self._start_timeout:g} s"
                )
            reply = pickle.loads(ours.recv_bytes())  # noqa: S301 - from our own child
        except (EOFError, OSError):
            self._reap(child)
            raise RuntimeError(
                f"a document pool child exited at start (exit code {process.exitcode})"
            ) from None
        except BaseException:
            self._reap(child)
            raise
        if reply[0] != "ready":
            self._reap(child)
            raise RuntimeError(f"a document pool child failed to start: {reply[1]}: {reply[2]}")
        return child

    def _reap(self, child: _Child) -> None:
        """Kill, join and close ``child`` once (blocking; runs on a thread)."""
        with self._lock:
            if child not in self._children:
                return
            self._children.discard(child)
        _kill(child.process)
        if child.exchange is not None:
            # The killed child ends the call's recv; only then may its pipe be closed.
            concurrent.futures.wait([child.exchange])
        child.process.join()
        child.conn.close()

    def _reap_all(self, children: list[_Child]) -> None:
        for child in children:
            self._reap(child)

    def _kill_now(self) -> None:
        for child in list(self._children):
            _kill(child.process)


def _exchange(conn: Connection, request: bytes) -> tuple[Any, ...]:
    """Send a call, wait for its answer (blocking; runs on a thread). Raises ``EOFError`` or
    ``OSError`` when the child dies."""
    conn.send_bytes(request)
    answer = conn.recv_bytes()
    try:
        reply: tuple[Any, ...] = pickle.loads(answer)  # noqa: S301 - from our own child
    except Exception as exc:  # a result the parent cannot unpickle
        return ("error", type(exc).__name__, _message(exc))
    return reply


def _kill(process: BaseProcess) -> None:
    if process.pid is not None:
        process.kill()  # a no-op once the process is reaped


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
    # Runs before multiprocessing's own exit hook (registered earlier, so run later), which
    # sends SIGTERM to daemonic children and joins them: ours ignore SIGTERM.
    for pool in list(_live_pools):
        pool._kill_now()
