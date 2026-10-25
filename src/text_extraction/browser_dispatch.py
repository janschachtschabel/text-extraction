"""The API's side of browser work: a sync request's page goes to the browser workers as a job.

API pods never start a browser (B1). A request that needs one (``mode: browser``, a screenshot,
browser options, or an ``auto`` result that escalates) is handed to the ``browser`` pool:

1. ``BrowserDispatcher.submit`` creates a job of kind ``extract`` or ``screenshot`` with one URL
   and the caller's secrets (B6), stores the fast result as the item's draft for an escalating
   ``auto`` request (B5), and queues item 0 on pool ``browser`` under the client
   ``<key name>#live``, so that the key's own batches do not queue it. An ``auto`` request that
   was handed over to a P1 job during its fast phase reuses that job instead: once the item is
   queued, its lease is cleared (the browser has its own time, and the lost view must not end
   it) and the browser worker writes item 0.
2. ``wait`` polls the job every 0.2 s, rising to 0.5 s. It gives up when no worker has claimed
   the item (the job is still ``queued``) after ``BROWSER_QUEUE_WAIT_SECONDS``, or when the item
   has no result ``BROWSER_TIMEOUT_SECONDS`` + 5 s after the claim
   (``BROWSER_UNBLOCK_TIMEOUT_SECONDS`` + 5 s in unblock mode).
3. A request that gives up calls ``abandon``, which deletes the job: a worker that claims the
   item later finds no job and drops it.
4. A request that answers with the result calls ``answered``: nobody can read the job any
   more, so it is deleted (its secrets and drafts too). A job whose result has a screenshot is
   kept only as long as the screenshot (``ARTIFACT_TTL_SECONDS``), since the artifact route
   needs the job's record. The worker has written the result by then and touches the job no
   more; a late copy of the item finds no job (or its result) and is dropped. A job answered
   with 202 keeps ``JOB_TTL_SECONDS``.

``PoolDispatcher`` (``pool_dispatch.py``) does the job, queue and wait handling that this pool
shares with the ``docling`` pool; ``Handover`` lives there too.

``text_extraction_browser_dispatch_total{outcome}`` counts how each dispatch ended
(``DISPATCH_OUTCOMES``): ``rendered`` (the worker's result came back), ``not_claimed`` (no worker
claimed the item within ``BROWSER_QUEUE_WAIT_SECONDS``), ``no_result`` (claimed, but no result
by the render deadline + 5 s, or the job was gone), ``handed_over`` (the request answered 202 and
the worker writes the job's result) and ``backend_error`` (Valkey failed while submitting or
waiting). ``not_claimed``, ``no_result`` and ``backend_error`` answer an ``auto`` request with its
fast result (``browser_unavailable``) and a ``browser`` request with ``timeout`` (or 503).

Logs carry job ids only, never a URL, header, cookie or action.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from prometheus_client import CollectorRegistry

from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.jobs import JobStore
from text_extraction.pool_dispatch import Handover, PoolDispatcher, job_options, live_client
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings

__all__ = [
    "DISPATCH_OUTCOMES",
    "BrowserDispatcher",
    "BrowserJobKind",
    "Handover",
    "job_options",
    "live_client",
]

# Seconds on top of the render deadline that a request waits for the result after the claim.
_RESULT_MARGIN_SECONDS = 5

type BrowserJobKind = Literal["extract", "screenshot"]

# The outcomes of ``text_extraction_browser_dispatch_total``; see the module docstring.
DISPATCH_OUTCOMES = ("rendered", "not_claimed", "no_result", "handed_over", "backend_error")


class BrowserDispatcher(PoolDispatcher):
    """Puts sync browser requests on the ``browser`` pool and waits for their results; see the
    module docstring. ``queue`` is the ``browser`` pool's queue; ``registry`` gets the dispatch
    counter (a registry of its own when None). ``clock`` (monotonic seconds) and ``sleep`` can
    be replaced in tests."""

    def __init__(
        self,
        store: JobStore,
        queue: FairQueue,
        settings: Settings,
        *,
        registry: CollectorRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            "browser",
            store,
            queue,
            settings,
            registry,
            outcomes=DISPATCH_OUTCOMES,
            clock=clock,
            sleep=sleep,
        )

    async def submit(
        self,
        kind: BrowserJobKind,
        req: ExtractRequest,
        principal: Principal,
        compliance: ComplianceMode,
        *,
        job_id: str | None = None,
        draft: Envelope[ExtractResult] | None = None,
    ) -> str:
        """Queue ``req`` on the ``browser`` pool and return its job's id. Without ``job_id`` a
        new job is created with the request's secrets; with it, that job (the P1 job of the
        request) is reused and its lease cleared. A reused job gets no secrets: only an ``auto``
        request without them has a fast phase (R8). ``draft`` is the fast result to compare the
        render with. Valkey errors propagate; a new job is deleted again first."""
        return await self.submit_job(
            kind,
            job_options(req, compliance),
            principal,
            req.url,
            job_id=job_id,
            draft=draft,
            secrets=req.secrets(),
        )

    async def wait(
        self, job_id: str, *, unblock: bool, detached: Callable[[], bool]
    ) -> Envelope[ExtractResult] | None:
        """The result of the job's item 0, or None when no worker claimed it within
        ``BROWSER_QUEUE_WAIT_SECONDS``, it had no result within the render deadline + 5 s after
        the claim, the job is gone, or ``detached()`` turned true. Valkey errors propagate.
        Each call counts its outcome once."""
        settings = self._settings
        render_seconds = (
            settings.browser_unblock_timeout_seconds
            if unblock
            else settings.browser_timeout_seconds
        )
        return await self.wait_for(
            job_id,
            answered="rendered",
            claim_within=settings.browser_queue_wait_seconds,
            result_within=render_seconds + _RESULT_MARGIN_SECONDS,
            detached=detached,
        )
