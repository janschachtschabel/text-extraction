"""``POST /v1/extract``: the public route in front of the extraction pipeline.

With ``wait_seconds`` the route can hand a slow extraction over to a job (ruling P1). The
extraction keeps running in this pod, as the same task, and the job only stores its result:

1. the pipeline runs as a task, and the route waits ``wait_seconds`` for it (the wait does not
   cancel it);
2. if it is still running, an ``extract`` job with one URL is created and marked running with a
   lease of ``EXTRACT_TIMEOUT_SECONDS + 10`` seconds, and the route answers 202;
3. a done-callback of the task stores its envelope as item 0 of the job. A task that raised is
   stored as ``internal_error`` (a ``ServiceError`` keeps its own code);
4. a pod that dies in between leaves the lease to expire; ``GET /v1/jobs/{id}`` then shows the
   item as failed (``jobs_api.py``).

If the job cannot be created because Valkey fails, the route does not answer 503 while the
extraction is still running: it keeps waiting for the task and answers with its envelope. It
does the same without trying while the pod holds ``EXTRACT_MAX_BACKGROUND`` extractions or
Valkey's memory is high (``memory_guard.py``). The
tasks are kept in ``app.state.background``, so that the lifespan can wait for them.

An extract job is not a batch: it does not count toward ``BATCH_MAX_OPEN_PER_CLIENT``.
"""

import asyncio
import contextlib
import time
from functools import partial
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from text_extraction.auth import Principal, require_role
from text_extraction.compliance import resolve_mode
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.job_models import JobAccepted, JobOptions
from text_extraction.jobs import JobLost, JobStore
from text_extraction.pipeline import Pipeline
from text_extraction.queue_metrics import JobMetrics
from text_extraction.ratelimit import rate_limited
from text_extraction.result_cache import MemoryPressure
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

router = APIRouter()

# Seconds on top of ``EXTRACT_TIMEOUT_SECONDS`` that the lease of an extract job lasts.
_LEASE_MARGIN_SECONDS = 10

_INTERNAL_MESSAGE = "Internal server error."
_CANCELLED_MESSAGE = "The extraction was cancelled."

type _Background = set[asyncio.Task[Any]]


@router.post(
    "/v1/extract",
    dependencies=[Depends(rate_limited)],
    response_model=Envelope[ExtractResult],
    responses={
        202: {
            "model": Envelope[JobAccepted],
            "description": "With `wait_seconds`: the extraction takes longer and became a job.",
        }
    },
)
async def extract(
    body: ExtractRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[ExtractResult] | JSONResponse:
    """Fetch a URL and return its main content as Markdown, text or HTML.

    Every outcome of the target answers 200: `status` is `success`, `partial` (content, but
    too short) or `failed`, with `error.code`, `error.retryable` and, in `data`,
    `upstream_status`, `compliance`, `timings` and as much of the result as was reached.
    4xx answers are about the request itself (invalid body, key, rate limit, a compliance mode
    the caller may not choose); 5xx are faults of this service.

    With `wait_seconds`, an extraction that takes longer becomes a job: 202 with the job's id
    and `status_url` (`GET /v1/jobs/{id}`). Without it the request always waits for the result.
    """
    pipeline: Pipeline = request.app.state.pipeline
    settings: Settings = request.app.state.settings
    if body.wait_seconds is None or not settings.jobs_enabled:
        return await pipeline.run(body, principal)

    # The caller's compliance mode is checked now, so that a refused one is a 403 and never a job.
    compliance = resolve_mode(body.compliance, principal, settings)
    background: _Background = request.app.state.background
    task = asyncio.create_task(pipeline.run(body, principal))
    background.add(task)
    try:
        if body.wait_seconds > 0:
            await asyncio.wait({task}, timeout=body.wait_seconds)
    except asyncio.CancelledError:
        task.cancel()  # nobody is waiting for it any more, and no job exists
        background.discard(task)
        raise
    if task.done():
        background.discard(task)
        return task.result()

    store: JobStore = request.app.state.job_store
    options = JobOptions(
        mode=body.mode,
        format=body.format,
        metadata=body.metadata,
        links=body.links,
        compliance=compliance,
        cache=body.cache,
    )
    job_id: str | None = None
    handed_over_at = time.monotonic()
    guard: MemoryPressure = request.app.state.memory_guard
    # At the cap this pod holds no more extractions for jobs, and while Valkey is nearly full it
    # stores no new job: the request waits for its own extraction.
    if len(background) <= settings.extract_max_background and not guard.high:
        try:
            job_id = await _hand_over(store, settings, principal, options, body.url)
        except asyncio.CancelledError:
            task.cancel()  # nobody is waiting for it any more, and no job is left
            background.discard(task)
            raise
    if job_id is None:
        try:
            return await task
        finally:
            background.discard(task)
    metrics: JobMetrics = request.app.state.job_metrics
    metrics.created.labels(kind="extract").inc()
    task.add_done_callback(
        partial(_store_when_done, background, store, metrics, job_id, handed_over_at)
    )
    _log.info("extract_job_accepted", job_id=job_id)
    accepted = Envelope(
        status="success",
        data=JobAccepted(
            id=job_id, kind="extract", state="running", total=1, status_url=f"/v1/jobs/{job_id}"
        ),
        trace_id=current_trace_id(),
    )
    return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))


async def _hand_over(
    store: JobStore, settings: Settings, principal: Principal, options: JobOptions, url: str
) -> str | None:
    """Create the extract job and mark it running; its id, or None when Valkey fails (logged).
    A job that was created but could not be marked, or whose creator is cancelled, is deleted
    again, if that works."""
    job_id: str | None = None
    try:
        job_id = await store.create("extract", principal, options, [url])
        lease = settings.extract_timeout_seconds + _LEASE_MARGIN_SECONDS
        await store.mark_running(job_id, lease_seconds=lease)
    except (RedisError, OSError, JobLost) as error:
        _log.warning(
            "extract_job_handover_failed", error_type=type(error).__name__, error=str(error)
        )
        if job_id is not None:
            await _delete_quietly(store, job_id)
        return None
    except asyncio.CancelledError:
        if job_id is not None:
            await asyncio.shield(_delete_quietly(store, job_id))
        raise
    return job_id


async def _delete_quietly(store: JobStore, job_id: str) -> None:
    with contextlib.suppress(RedisError, OSError):  # else the job expires on its own
        await store.delete(job_id)


def _failure(code: ErrorCode, message: str, *, retryable: bool) -> Envelope[ExtractResult]:
    error = ErrorInfo(code=code, message=message, retryable=retryable)
    return Envelope[ExtractResult](status="failed", error=error, trace_id=current_trace_id())


def _envelope_of(task: asyncio.Task[Envelope[ExtractResult]]) -> Envelope[ExtractResult]:
    """What the finished task produced; a failure becomes a failed envelope."""
    if task.cancelled():
        return _failure(ErrorCode.INTERNAL_ERROR, _CANCELLED_MESSAGE, retryable=True)
    error = task.exception()
    if error is None:
        return task.result()
    if isinstance(error, ServiceError):
        return _failure(error.code, error.message, retryable=error.retryable)
    _log.error("extract_job_failed", exc_info=error)
    return _failure(ErrorCode.INTERNAL_ERROR, _INTERNAL_MESSAGE, retryable=False)


def _store_when_done(
    background: _Background,
    store: JobStore,
    metrics: JobMetrics,
    job_id: str,
    created_at: float,
    task: asyncio.Task[Envelope[ExtractResult]],
) -> None:
    """Done-callback of a handed-over extraction: store its envelope in the job. The storing
    task is added to ``background`` before the extraction leaves it, so a shutdown that waits
    for the set never misses the write."""
    storing = asyncio.create_task(
        _store_result(store, metrics, job_id, created_at, _envelope_of(task))
    )
    background.add(storing)
    storing.add_done_callback(background.discard)
    background.discard(task)


async def _store_result(
    store: JobStore,
    metrics: JobMetrics,
    job_id: str,
    created_at: float,
    envelope: Envelope[ExtractResult],
) -> None:
    """Store the envelope as item 0 and observe the job's duration (``created_at`` is a
    ``time.monotonic()`` reading from just before the job was created). A Valkey failure is logged
    and not raised: the job's lease then runs out and ``GET /v1/jobs/{id}`` reports the item as
    lost."""
    try:
        # The first and only write of an extract job completes it ("last"); a repeat is not counted.
        if await store.write_result(job_id, 0, envelope) == "last":
            metrics.duration.labels(kind="extract").observe(max(time.monotonic() - created_at, 0.0))
    except JobLost:
        _log.warning("extract_job_gone", job_id=job_id)
    except (RedisError, OSError) as error:
        _log.warning(
            "extract_job_result_not_stored",
            job_id=job_id,
            error_type=type(error).__name__,
            error=str(error),
        )
