"""``POST /v1/batch`` and ``GET /v1/jobs/{id}``: the public routes in front of the job store and
the fair queue.

Besides the job keys and the queue keys, each owner has one key of its own,
``jobs:{owner:<name>}:open``: a set of the ids of the owner's batches that were accepted and have
not been seen finished. It is how ``BATCH_MAX_OPEN_PER_CLIENT`` is counted. A job counts as open
only while it exists and is not completed; the ids of the others are removed on each check. The
name is escaped (``%``, ``{`` and ``}``), so the braces enclose exactly the name. No Lua script
touches this key, so it shares no slot with a job or a queue.
"""

import asyncio
import re
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal, authenticate, require_role
from text_extraction.compliance import resolve_mode
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.job_models import (
    BatchRequest,
    JobAccepted,
    JobOptions,
    JobProgress,
    JobView,
)
from text_extraction.jobs import JobRecord, JobStore
from text_extraction.ratelimit import rate_limited
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

router = APIRouter()

# The most items one page of a job may have: the most ``JobStore.items`` is asked for.
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100

_JOB_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
_OWNER_ESCAPES = {"%": "%25", "{": "%7B", "}": "%7D"}


def _open_key(owner: str) -> str:
    for char, code in _OWNER_ESCAPES.items():
        owner = owner.replace(char, code)
    return f"jobs:{{owner:{owner}}}:open"


def _text(value: bytes | str) -> str:
    return value if isinstance(value, str) else value.decode()


async def _require_jobs_enabled(
    request: Request, _principal: Annotated[Principal, Depends(authenticate)]
) -> None:
    """Dependency: 503 while ``JOBS_ENABLED`` is false (after authentication)."""
    settings: Settings = request.app.state.settings
    if not settings.jobs_enabled:
        raise ServiceError(ErrorCode.INTERNAL_ERROR, "Jobs are disabled on this deployment.", 503)


def _unavailable() -> ServiceError:
    return ServiceError(
        ErrorCode.INTERNAL_ERROR,
        "The job backend is unavailable. Try again later.",
        503,
        retryable=True,
    )


def _log_unavailable(error: Exception) -> None:
    # Type and text only: the text names host and port, never credentials or URLs of a batch.
    _log.warning("jobs_backend_unavailable", error_type=type(error).__name__, error=str(error))


def _too_many_open(limit: int) -> ServiceError:
    return ServiceError(
        ErrorCode.RATE_LIMITED,
        f"At most {limit} batches may be unfinished at a time. Retry when one has completed.",
        429,
        retryable=True,
    )


def _check_size(body: BatchRequest, max_urls: int) -> None:
    if len(body.urls) > max_urls:
        raise RequestValidationError(
            [
                {
                    "type": "too_long",
                    "loc": ("body", "urls"),
                    "msg": f"A batch may have at most {max_urls} URLs.",
                }
            ]
        )


async def _open_count(valkey: Redis, store: JobStore, owner: str) -> int:
    """The owner's unfinished batches. Ids of jobs that are completed or gone are removed."""
    key = _open_key(owner)
    ids = [_text(raw) for raw in await valkey.smembers(key)]
    records = await asyncio.gather(*(store.get(job_id) for job_id in ids))
    finished = [
        job_id
        for job_id, record in zip(ids, records, strict=True)
        if record is None or record.state == "completed"
    ]
    if finished:
        await valkey.srem(key, *finished)
    return len(ids) - len(finished)


async def _add_open(valkey: Redis, owner: str, job_id: str, ttl_seconds: int) -> int:
    """Put the job into the owner's open set; the size of the set afterwards."""
    key = _open_key(owner)
    async with valkey.pipeline(transaction=True) as pipe:
        pipe.sadd(key, job_id)
        pipe.expire(key, ttl_seconds)
        pipe.scard(key)
        size: int = (await pipe.execute())[-1]
    return size


async def _discard(valkey: Redis, store: JobStore, owner: str, job_id: str) -> None:
    """Delete a job that was created but will not run. Failing to do so only leaves the job to
    expire, so the error is logged and not raised over the one that caused the discard."""
    try:
        await store.delete(job_id)
        await valkey.srem(_open_key(owner), job_id)
    except (RedisError, OSError) as error:
        _log_unavailable(error)


@router.post(
    "/v1/batch",
    status_code=202,
    dependencies=[Depends(_require_jobs_enabled), Depends(rate_limited)],
)
async def create_batch(
    body: BatchRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[JobAccepted]:
    """Queue many URLs as one job and answer 202 with its id and `status_url`.

    Every URL becomes one item with its own result, also a URL given twice and an invalid one
    (that item fails with `invalid_url`). Items of different clients and hosts are scheduled
    fairly across the worker pods. Poll `GET /v1/jobs/{id}` for progress and results.
    4xx answers: an invalid body or more URLs than `BATCH_MAX_URLS` (422), a compliance mode the
    caller may not choose (403), `BATCH_MAX_OPEN_PER_CLIENT` unfinished batches (429, retryable).
    503 when Valkey is unavailable or jobs are disabled.
    """
    settings: Settings = request.app.state.settings
    valkey: Redis = request.app.state.valkey
    store: JobStore = request.app.state.job_store
    queue: FairQueue = request.app.state.job_queue

    _check_size(body, settings.batch_max_urls)
    options = JobOptions(
        mode=body.mode,
        format=body.format,
        metadata=body.metadata,
        links=body.links,
        compliance=resolve_mode(body.compliance, principal, settings),
        cache=body.cache,
    )
    limit = settings.batch_max_open_per_client
    try:
        if await _open_count(valkey, store, principal.name) >= limit:
            raise _too_many_open(limit)
        job_id = await store.create("batch", principal, options, body.urls)
        try:
            # Counting again after the add keeps the limit under concurrent requests: of
            # several that passed the check above, those beyond the limit give their job back.
            if await _add_open(valkey, principal.name, job_id, settings.job_ttl_seconds) > limit:
                raise _too_many_open(limit)
            # Enqueued exactly once and never retried: a retry could queue some items twice.
            await queue.enqueue(job_id, principal.name, body.urls)
        except BaseException:
            await _discard(valkey, store, principal.name, job_id)
            raise
    except (RedisError, OSError) as error:
        _log_unavailable(error)
        raise _unavailable() from None
    _log.info("batch_accepted", job_id=job_id, total=len(body.urls))
    data = JobAccepted(
        id=job_id,
        kind="batch",
        state="queued",
        total=len(body.urls),
        status_url=f"/v1/jobs/{job_id}",
    )
    return Envelope(status="success", data=data, trace_id=current_trace_id())


def _not_found() -> ServiceError:
    return ServiceError(ErrorCode.NOT_FOUND, "Job not found.", 404)


def _envelope_status(record: JobRecord) -> Literal["success", "partial", "failed"]:
    """`success` while the job runs and when all items succeeded; once it is completed,
    `failed` when every item failed and `partial` when some failed or are partial."""
    progress: JobProgress = record.progress
    if record.state != "completed":
        return "success"
    if progress.failed == progress.total:
        return "failed"
    if progress.failed or progress.partial:
        return "partial"
    return "success"


@router.get(
    "/v1/jobs/{job_id}", dependencies=[Depends(_require_jobs_enabled), Depends(rate_limited)]
)
async def get_job(
    job_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
    offset: Annotated[int, Query(ge=0, description="Index of the first item of the page.")] = 0,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page.")
    ] = DEFAULT_PAGE_SIZE,
) -> Envelope[JobView]:
    """Status and progress of a job with one page of its items.

    The envelope `status` is `success` while the job is queued or running and when all items
    succeeded, `partial` when the finished job has failed or partial items, and `failed` when
    every item failed. `next_offset` is set while more items follow. A job that does not exist,
    has expired or belongs to another caller (admins see all) answers 404.
    """
    store: JobStore = request.app.state.job_store
    if not _JOB_ID.fullmatch(job_id):
        raise _not_found()
    try:
        record = await store.get(job_id)
        if record is None or (record.owner != principal.name and principal.role != "admin"):
            raise _not_found()
        total = record.progress.total
        items = await store.items(job_id, offset, limit) if offset < total else []
    except (RedisError, OSError) as error:
        _log_unavailable(error)
        raise _unavailable() from None
    view = JobView(
        id=record.id,
        kind=record.kind,
        state=record.state,
        created_at=record.created_at,
        finished_at=record.finished_at,
        progress=record.progress,
        items=items,
        next_offset=offset + limit if offset + limit < total else None,
    )
    return Envelope(
        status=_envelope_status(record),
        data=view,
        trace_id=current_trace_id(),
    )
