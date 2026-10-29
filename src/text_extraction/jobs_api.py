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
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.asr_dispatch import ASR_KEY_HEADER, asr_key_of
from text_extraction.auth import Principal, authenticate, require_role
from text_extraction.compliance import resolve_mode
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.extract_api import (
    ASR_KEY_DESCRIPTION,
    BROWSER_DISABLED_MESSAGE,
    check_ocr_request,
    check_pii_request,
    check_transcribe_request,
)
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import (
    BatchRequest,
    JobAccepted,
    JobItem,
    JobOptions,
    JobProgress,
    JobView,
)
from text_extraction.jobs import JobRecord, JobStore, owner_key
from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.ratelimit import rate_limited
from text_extraction.result_cache import MemoryPressure
from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings
from text_extraction.tracing import current_trace_id
from text_extraction.valkey import reply_text

_log = structlog.get_logger(__name__)

router = APIRouter()

# The most items one page of a job may have: the most ``JobStore.items`` is asked for.
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100

# What a batch body may take besides its URLs (the options, JSON syntax), in bytes.
_BODY_ALLOWANCE = 4096
# JSON quotes and a comma around each URL.
_BYTES_AROUND_URL = 8

_LOST_MESSAGE = "The pod running this extraction stopped."

_JOB_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)


def _open_key(owner: str) -> str:
    return owner_key(owner, "open")


async def require_jobs_enabled(
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


def _memory_high() -> ServiceError:
    return ServiceError(
        ErrorCode.INTERNAL_ERROR,
        "The job backend is nearly out of memory and accepts no new batches for now. "
        "Try again later.",
        503,
        retryable=True,
    )


def _too_many_open(limit: int) -> ServiceError:
    return ServiceError(
        ErrorCode.RATE_LIMITED,
        f"At most {limit} batches may be unfinished at a time. Retry when one has completed.",
        429,
        retryable=True,
    )


def _too_long(loc: tuple[str, ...], message: str) -> RequestValidationError:
    return RequestValidationError([{"type": "too_long", "loc": loc, "msg": message}])


def _check_size(body: BatchRequest, max_urls: int) -> None:
    if len(body.urls) > max_urls:
        raise _too_long(("body", "urls"), f"A batch may have at most {max_urls} URLs.")


def batch_body_limit(settings: Settings) -> int:
    """The largest body of ``POST /v1/batch``, in bytes: ``BATCH_MAX_URLS`` URLs of the longest
    allowed length plus the options. The body-limit middleware enforces it (413)."""
    return settings.batch_max_urls * (MAX_URL_LENGTH + _BYTES_AROUND_URL) + _BODY_ALLOWANCE


async def _open_count(valkey: Redis, store: JobStore, owner: str) -> int:
    """The owner's unfinished batches. Ids of jobs that are completed or gone are removed."""
    key = _open_key(owner)
    ids = [reply_text(raw) for raw in await valkey.smembers(key)]
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
    dependencies=[
        Depends(require_jobs_enabled),
        Depends(rate_limited),
    ],
)
async def create_batch(
    body: BatchRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
    asr_key: Annotated[
        str | None, Header(alias=ASR_KEY_HEADER, description=ASR_KEY_DESCRIPTION)
    ] = None,
) -> Envelope[JobAccepted]:
    """Queue many URLs as one job and answer 202 with its id and `status_url`.

    Every URL becomes one item with its own result, also a URL given twice and an invalid one
    (that item fails with `invalid_url`). Items of different clients and hosts are scheduled
    fairly across the worker pods. Poll `GET /v1/jobs/{id}` for progress and results.
    4xx answers: an invalid body or more URLs than `BATCH_MAX_URLS` (422), a body larger than
    `BATCH_MAX_URLS` URLs of the longest allowed length can take (413, before it is read whole),
    a compliance mode the caller may not choose (403), `BATCH_MAX_OPEN_PER_CLIENT` unfinished
    batches (429, retryable).
    503 when Valkey is unavailable, jobs are disabled, or Valkey's memory is at
    `VALKEY_MEMORY_GUARD_RATIO` of its maximum (retryable).

    `mode: browser` queues the URLs for the browser workers (400 while browser mode is off);
    `ocr: always` answers 400 while the service has no OCR, `transcribe: true` while it has no
    ASR, or transcribes on an external endpoint without a key of its own and the request has no
    `X-ASR-Api-Key` header (that key, 1 to 1024 visible ASCII characters, else 400, serves all
    items of the batch: stored with the job while it lives, never shown or logged);
    `auto` items that need a browser move there on their own. A batch takes no `actions`,
    `cookies`, `headers` or `screenshot` (422). `pii` (400 while the service has no PII
    handling, 422 for `annotate` with `format: html`) sends every item's result through the nlp
    workers before it is stored: the processed result, or `pii_failed` with its content
    withheld.
    """
    settings: Settings = request.app.state.settings
    valkey: Redis = request.app.state.valkey
    store: JobStore = request.app.state.job_store
    queue: FairQueue = request.app.state.job_queue
    guard: MemoryPressure = request.app.state.memory_guard

    _check_size(body, settings.batch_max_urls)
    if body.mode == "browser":  # B9: the browser workers take the items, if there are any
        if not settings.browser_enabled:
            raise ServiceError(ErrorCode.INVALID_REQUEST, BROWSER_DISABLED_MESSAGE, 400)
        queue = request.app.state.browser_queue
    check_ocr_request(body.ocr, settings)  # 400 for `ocr: always` while there is no OCR
    key = asr_key_of(asr_key)
    # 400 while there is no ASR (M9), or no key for an external endpoint (M11).
    check_transcribe_request(body.transcribe, settings, key=key)
    # 400 while there is no PII handling, 422 for annotate with HTML (N4).
    check_pii_request(body.pii, body.format, False, settings)
    options = JobOptions(
        mode=body.mode,
        format=body.format,
        metadata=body.metadata,
        links=body.links,
        compliance=resolve_mode(body.compliance, principal, settings),
        cache=body.cache,
        ocr=body.ocr,
        transcribe=body.transcribe,
        languages=body.languages,
        pii=body.pii,
    )
    if guard.high:
        raise _memory_high()
    limit = settings.batch_max_open_per_client
    try:
        if await _open_count(valkey, store, principal.name) >= limit:
            raise _too_many_open(limit)
        job_id = await store.create(
            "batch", principal, options, body.urls, trace_id=current_trace_id()
        )
        try:
            # Counting again after the add keeps the limit under concurrent requests: of
            # several that passed the check above, those beyond the limit give their job back.
            if await _add_open(valkey, principal.name, job_id, settings.job_ttl_seconds) > limit:
                raise _too_many_open(limit)
            if key is not None and body.transcribe:
                # The caller's key of the external endpoint, for any item that moves to the ASR
                # workers, while the job lives (M11); the job's deletion takes it along.
                await store.put_asr_key(job_id, key, whole_job=True)
            # Enqueued exactly once and never retried: a retry could queue some items twice.
            await queue.enqueue(job_id, principal.name, body.urls)
        except BaseException:
            # Also on cancellation, and shielded from a second one: the job must not be left
            # queued (it would hold a slot of the client's quota until it expires).
            await asyncio.shield(_discard(valkey, store, principal.name, job_id))
            raise
    except (RedisError, OSError) as error:
        _log_unavailable(error)
        raise _unavailable() from None
    # Counted only here: a job that was discarded above never reaches a worker.
    request.app.state.job_metrics.created.labels(kind="batch").inc()
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


async def visible_job(store: JobStore, job_id: str, principal: Principal) -> JobRecord:
    """The job, when it exists and the caller may see it: its owner, or any admin. Otherwise
    404 ``not_found``, the same for a malformed id, an unknown or expired job and another
    caller's. Valkey errors propagate."""
    if not _JOB_ID.fullmatch(job_id):
        raise _not_found()
    record = await store.get(job_id)
    if record is None or (record.owner != principal.name and principal.role != "admin"):
        raise _not_found()
    return record


def backend_unavailable(error: Exception) -> ServiceError:
    """Log a Valkey failure of a job route and return its 503."""
    _log_unavailable(error)
    return _unavailable()


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


async def _all_failed_error(store: JobStore, record: JobRecord) -> ErrorInfo:
    """The error of a job whose items all failed: the most frequent error code among its first
    ``MAX_PAGE_SIZE`` items (a tie goes to the code seen first), retryable when any of those
    items is. The per-item codes are not counted in the job record, and reading every item of a
    large batch would load all its results; so a larger job is judged by its first items, the
    same whichever page the caller reads."""
    codes: Counter[ErrorCode] = Counter()
    retryable = False
    for item in await store.items(record.id, 0, MAX_PAGE_SIZE):
        error = item.result.error if item.result is not None else None
        if error is not None:
            codes[error.code] += 1
            retryable = retryable or error.retryable
    code = codes.most_common(1)[0][0] if codes else ErrorCode.INTERNAL_ERROR
    return ErrorInfo(
        code=code, message=f"All {record.progress.total} items failed.", retryable=retryable
    )


def _is_lost(record: JobRecord) -> bool:
    """A job that is still ``running`` after its lease ran out: the pod that ran its extraction
    stopped before it stored the result (P1). A completed job keeps its past ``lease_until``
    and is not lost."""
    return (
        record.state == "running"
        and record.lease_until is not None
        and record.lease_until < time.time()
    )


def _lost_view(record: JobRecord, items: list[JobItem]) -> tuple[JobRecord, list[JobItem]]:
    """The record and items of a lost job as the API shows them (nothing is stored): every item
    without a result is done and failed with a retryable ``internal_error``, and the job is
    completed, at the time its lease ran out."""
    error = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message=_LOST_MESSAGE, retryable=True)
    lost = Envelope[ExtractResult](status="failed", error=error)
    shown = [
        item if item.state == "done" else item.model_copy(update={"state": "done", "result": lost})
        for item in items
    ]
    total = record.progress.total
    progress = JobProgress(
        total=total,
        done=total,
        success=record.progress.success,
        partial=record.progress.partial,
        failed=total - record.progress.success - record.progress.partial,
    )
    ended = record.lease_until if record.lease_until is not None else time.time()
    finished_at = datetime.fromtimestamp(ended, tz=UTC)
    return replace(record, state="completed", finished_at=finished_at, progress=progress), shown


@router.get(
    "/v1/jobs/{job_id}", dependencies=[Depends(require_jobs_enabled), Depends(rate_limited)]
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
    every item failed; `error` then has the most frequent error code among the first items and
    the message "All N items failed.". `next_offset` is set while more items follow. A job that
    does not exist, has expired or belongs to another caller (admins see all) answers 404.
    """
    store: JobStore = request.app.state.job_store
    try:
        record = await visible_job(store, job_id, principal)
        total = record.progress.total
        items = await store.items(job_id, offset, limit) if offset < total else []
        lost = _is_lost(record)
        if lost:
            record, items = _lost_view(record, items)
        status = _envelope_status(record)
        failure: ErrorInfo | None = None
        if status == "failed":
            failure = (
                ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=f"All {total} items failed.",
                    retryable=True,
                )
                if lost
                else await _all_failed_error(store, record)
            )
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
        status=status,
        data=view,
        error=failure,
        trace_id=current_trace_id(),
    )
