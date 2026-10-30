"""Selector rules in killable child processes (ruling R19).

Every caller rule, every learned rule and every selector the LLM proposes for
``learn_selectors`` runs here, never in the parent: soupsieve backtracks without a budget (a CSS
selector such as ``span div div div div`` takes minutes on a few hundred nested elements), lxml
cannot stop an XPath, and Python cannot stop a thread. ``SelectorRunner`` keeps a
``DocumentPool`` of ``STRUCTURED_SELECTOR_PROCESSES`` children (target ``selector_child``, an
address space of ``STRUCTURED_SELECTOR_MEMORY_MB`` each) and gives one run, all rules of one
result on one page, ``STRUCTURED_SELECTOR_TIMEOUT_SECONDS``, capped by the caller's own
deadline. Waiting for a free child counts against it.

A run that does not finish in time is killed with its child (a fresh one replaces it): its
fields stay missing, with the warning ``selectors_timed_out``. A child that crashes or reaches
its memory limit, and a pool that cannot start children, give ``selectors_failed``. Neither
fails the result; ``learn_selectors`` rejects the proposals of such a run
(``selector_learning_failed``). R4's limits (``STRUCTURED_MAX_ELEMENTS``, XPath for admin keys,
one ``:has(``, no backslash) stay as policy.

The pool starts with its owner (the API, the extract, browser and llm workers) or on its first
run; a start that fails is ``selectors_failed`` for that run, and the next run starts a fresh
pool. A runner serves the event loop it runs on: on another one (tests that run each request on
an event loop of their own) it starts fresh children, as a pool's queue and tasks belong to one
loop. Nothing here logs a selector, a value or the page.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import structlog

from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
    DocumentPool,
)
from text_extraction.settings import Settings
from text_extraction.structured.rules import Found, RuleRequest

_log = structlog.get_logger(__name__)

SELECTOR_TARGET: Final = "text_extraction.structured.selector_child"
SELECTORS_TIMED_OUT_WARNING: Final = "selectors_timed_out"
SELECTORS_FAILED_WARNING: Final = "selectors_failed"
# A child serves this many runs before it is replaced (its heap may be fragmented); a selector
# run is far smaller than a document conversion, so more than ``DocumentPool``'s default.
_MAX_TASKS: Final = 500
# A run ends this long before the caller's deadline, so the stage after it still fits.
_DEADLINE_MARGIN_SECONDS: Final = 0.25


@dataclass(frozen=True)
class RulesOutcome:
    """What the rules of one run found, by field (a rule that raised, or found nothing, may be
    left out), and the run's warning (``selectors_timed_out``, ``selectors_failed``; None when
    it ran)."""

    found: dict[str, Found] = field(default_factory=dict)
    warning: str | None = None


class SelectorRunner:
    """Runs selector rules in the children of a ``DocumentPool`` made by ``pool_factory``;
    ``timeout_seconds`` is one run's deadline. ``start`` (optional) before the first run,
    ``shutdown`` last."""

    def __init__(self, pool_factory: Callable[[], DocumentPool], timeout_seconds: float) -> None:
        self._factory = pool_factory
        self._timeout_seconds = timeout_seconds
        self._pool: DocumentPool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Start the children now (before ``/ready``), on the running event loop."""
        await self._current().start()

    async def run(
        self,
        html: str,
        url: str,
        rules: Sequence[RuleRequest],
        *,
        deadline: float | None = None,
    ) -> RulesOutcome:
        """``rules`` on the page ``html`` (final URL ``url``), by ``deadline`` (``loop.time()``)
        at the latest; never raises for a rule or a child."""
        if not rules:
            return RulesOutcome()
        loop = asyncio.get_running_loop()
        until = loop.time() + self._timeout_seconds
        if deadline is not None:
            until = min(until, deadline - _DEADLINE_MARGIN_SECONDS)
        payload = {"html": html, "url": url, "rules": list(rules)}
        pool = self._current()
        try:
            await pool.start()
            reply = await pool.run("rules", payload, until)
        except ConversionTimeout:
            _log.warning("selectors_timed_out", rules=len(rules))
            return RulesOutcome(warning=SELECTORS_TIMED_OUT_WARNING)
        except (
            ConversionCrashed,
            ConversionOutOfMemory,
            ConversionError,
            LookupError,
            TypeError,
            RuntimeError,
            OSError,
        ) as error:
            # The type only: a child's message may quote the page.
            _log.warning("selectors_failed", error_type=type(error).__name__)
            return RulesOutcome(warning=SELECTORS_FAILED_WARNING)
        return RulesOutcome(found=_found(reply))

    async def shutdown(self) -> None:
        """Kill and reap the children. Safe to call more than once."""
        pool, self._pool, self._loop = self._pool, None, None
        if pool is not None:
            await pool.shutdown()

    def child_pids(self) -> list[int]:
        """The process ids of the children (for tests and diagnostics)."""
        return self._pool.child_pids() if self._pool is not None else []

    def _current(self) -> DocumentPool:
        loop = asyncio.get_running_loop()
        if self._pool is None or self._loop is not loop or self._pool.closed:
            if self._pool is not None and not self._pool.closed:
                self._pool.abandon()  # its event loop is not this one: it cannot be awaited
            # A closed pool (a start that failed shuts its pool down) is replaced, so the next
            # run tries a fresh start instead of failing on it for good.
            self._pool, self._loop = self._factory(), loop
        return self._pool


def _found(reply: Any) -> dict[str, Found]:
    """The child's answer, as far as it has the expected shape."""
    if not isinstance(reply, Mapping):
        return {}
    found: dict[str, Found] = {}
    for name, answer in reply.items():
        if (
            isinstance(name, str)
            and isinstance(answer, tuple)
            and len(answer) == 2
            and isinstance(answer[0], list)
            and isinstance(answer[1], bool)
        ):
            found[name] = (answer[0], answer[1])
    return found


def create_selector_runner(settings: Settings) -> SelectorRunner:
    """The selector children of a process (``STRUCTURED_SELECTOR_PROCESSES``,
    ``STRUCTURED_SELECTOR_MEMORY_MB``, ``STRUCTURED_SELECTOR_TIMEOUT_SECONDS``); not started
    yet."""

    def pool() -> DocumentPool:
        return DocumentPool(
            settings.structured_selector_processes,
            settings.structured_selector_memory_mb,
            max_tasks=_MAX_TASKS,
            target=SELECTOR_TARGET,
        )

    return SelectorRunner(pool, settings.structured_selector_timeout_seconds)
