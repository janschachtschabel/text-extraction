"""``POST /v1/batch``, ``GET /v1/jobs/{id}`` and ``DELETE /v1/jobs/{id}``: the public routes in
front of the job store and the fair queue, and ``open_job``, which ``POST /v1/site-structure``
(``site_api.py``) shares.

Besides the job keys and the queue keys, each owner has one key of its own,
``jobs:{owner:<name>}:open``: a set of the ids of the owner's batches and site jobs that were
accepted and have not been seen finished. It is how ``BATCH_MAX_OPEN_PER_CLIENT`` is counted. A
job counts as open only while it exists and is not completed; the ids of the others are removed
on each check. The name is escaped (``%``, ``{`` and ``}``), so the braces enclose exactly the
name. No Lua script touches this key, so it shares no slot with a job or a queue.
"""

import asyncio
import re
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import State

from text_extraction.api_examples import BATCH_EXAMPLES
from text_extraction.asr_dispatch import ASR_KEY_HEADER, asr_key_of
from text_extraction.auth import Principal, authenticate, require_role
from text_extraction.compliance import resolve_mode
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.extract_api import (
    ASR_KEY_DESCRIPTION,
    BROWSER_DISABLED_MESSAGE,
    LLM_KEY_DESCRIPTION,
    check_ocr_request,
    check_pii_request,
    check_transcribe_request,
)
from text_extraction.extract_models import ExtractResult, with_own_fault_data
from text_extraction.job_models import (
    BatchRequest,
    JobAccepted,
    JobDeleted,
    JobItem,
    JobKind,
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
from text_extraction.structured.checks import (
    LLM_KEY_HEADER,
    check_selector_access,
    check_structured_request,
    llm_key_of,
    wants_llm,
)
from text_extraction.tracing import current_trace_id
from text_extraction.valkey import reply_text
from text_extraction.vision.checks import check_describe_request

_log = structlog.get_logger(__name__)

router = APIRouter()

# The most items one page of a job may have: the most ``JobStore.items`` is asked for.
MAX_PAGE_SIZE = 500
# Most bytes of stored results one page reads: 500 items of up to JOB_ITEM_MAX_BYTES (2 MB) made
# one page about 1 GB in the API pod. A page stops before the item that would pass it (the first
# item always comes), and `next_offset` continues from there.
PAGE_MAX_BYTES = 20_000_000
# The Retry-After of the 429 for too many unfinished batches and site jobs. When one completes
# is not known; a minute keeps a waiting client from polling hard.
OPEN_RETRY_AFTER_SECONDS = 60
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
    """Dependency: 400 while ``JOBS_ENABLED`` is false (after authentication)."""
    check_jobs_enabled(request.app.state.settings)


def check_jobs_enabled(settings: Settings) -> None:
    """400 ``invalid_request`` while ``JOBS_ENABLED`` is false, as for every switch that is off
    (C12): the deployment answers so until it is configured otherwise, which no retry changes."""
    if not settings.jobs_enabled:
        raise ServiceError(ErrorCode.INVALID_REQUEST, "Jobs are disabled on this deployment.", 400)


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
        "The job backend is nearly out of memory and accepts no new jobs for now. Try again later.",
        503,
        retryable=True,
    )


def _too_many_open(limit: int) -> ServiceError:
    return ServiceError(
        ErrorCode.RATE_LIMITED,
        f"At most {limit} batches and site jobs may be unfinished at a time. "
        "Retry when one has completed.",
        429,
        retryable=True,
        headers={"Retry-After": str(OPEN_RETRY_AFTER_SECONDS)},
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
    """The owner's unfinished batches and site jobs. Ids of jobs that are completed or gone are
    removed."""
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


async def _remove(valkey: Redis, store: JobStore, owner: str, job_id: str) -> None:
    """Delete the job with all its keys, then remove it from ``owner``'s open set. Valkey
    errors propagate."""
    await store.delete(job_id)
    await valkey.srem(_open_key(owner), job_id)


async def _discard(valkey: Redis, store: JobStore, owner: str, job_id: str) -> None:
    """Delete a job that was created but will not run. Failing to do so only leaves the job to
    expire, so the error is logged and not raised over the one that caused the discard."""
    try:
        await _remove(valkey, store, owner, job_id)
    except (RedisError, OSError) as error:
        _log_unavailable(error)


async def open_job(
    state: State,
    principal: Principal,
    kind: JobKind,
    options: JobOptions,
    urls: list[str],
) -> str:
    """Create a job that will be queued, and count it as one of the owner's open jobs: the part
    that ``POST /v1/batch`` and ``POST /v1/site-structure`` share. Returns the job's id.

    503 while Valkey's memory is high; 429 (retryable) while the owner has
    ``BATCH_MAX_OPEN_PER_CLIENT`` unfinished jobs, batches and site jobs alike. The job is
    created with one item per URL, then added to the owner's open set; the count after the add
    keeps the limit under concurrent requests, and a job beyond it is given back. A job whose
    add fails is given back as well. The caller stores what else the job needs and enqueues it,
    and gives the job back with ``discard_job`` when that fails. Valkey errors propagate.
    ``state`` is the public app's state. The caller has run ``principal_for``,
    ``check_jobs_enabled`` and ``check_rate`` (``start_batch``, ``start_site``)."""
    settings: Settings = state.settings
    valkey: Redis = state.valkey
    store: JobStore = state.job_store
    guard: MemoryPressure = state.memory_guard
    if guard.high:
        raise _memory_high()
    limit = settings.batch_max_open_per_client
    if await _open_count(valkey, store, principal.name) >= limit:
        raise _too_many_open(limit)
    job_id = await store.create(kind, principal, options, urls, trace_id=current_trace_id())
    try:
        # Counting again after the add keeps the limit under concurrent requests: of several
        # that passed the check above, those beyond the limit give their job back.
        if await _add_open(valkey, principal.name, job_id, settings.job_ttl_seconds) > limit:
            raise _too_many_open(limit)
    except BaseException:
        await discard_job(state, principal, job_id)
        raise
    return job_id


async def discard_job(state: State, principal: Principal, job_id: str) -> None:
    """Give back a job from ``open_job`` that will not run: delete it with all its keys and
    remove it from the owner's open set. Also on cancellation, and shielded from a second one:
    the job must not be left queued (it would hold a slot of the client's quota until it
    expires). A Valkey error is logged, not raised."""
    valkey: Redis = state.valkey
    store: JobStore = state.job_store
    await asyncio.shield(_discard(valkey, store, principal.name, job_id))


@router.post(
    "/v1/batch",
    status_code=202,
    dependencies=[
        Depends(require_jobs_enabled),
        Depends(rate_limited),
    ],
)
async def create_batch(
    body: Annotated[BatchRequest, Body(openapi_examples=BATCH_EXAMPLES)],
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
    asr_key: Annotated[
        str | None, Header(alias=ASR_KEY_HEADER, description=ASR_KEY_DESCRIPTION)
    ] = None,
    llm_key: Annotated[
        str | None, Header(alias=LLM_KEY_HEADER, description=LLM_KEY_DESCRIPTION)
    ] = None,
) -> Envelope[JobAccepted]:
    """Queue many URLs as one job and answer 202 with its id and `status_url`.

    Every URL becomes one item with its own result, also a URL given twice and an invalid one
    (that item fails with `invalid_url`). Items of different clients and hosts are scheduled
    fairly across the worker pods. Poll `GET /v1/jobs/{id}` for progress and results.
    4xx answers: an invalid body or more URLs than `BATCH_MAX_URLS` (422), a body larger than
    `BATCH_MAX_URLS` URLs of the longest allowed length can take (413, before it is read whole),
    a compliance mode the caller may not choose (403), `BATCH_MAX_OPEN_PER_CLIENT` unfinished
    batches and site jobs together (429, retryable), jobs disabled (400).
    503 when Valkey is unavailable or Valkey's memory is at `VALKEY_MEMORY_GUARD_RATIO` of its
    maximum (retryable).

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

    `structured` and `chunking` and the header `X-LLM-Api-Key` are checked as for
    `POST /v1/extract` (400 without LLM, embedding model or key; 422 for the refused
    combinations); the key serves all items of the batch, as long as the job.
    """
    return await start_batch(request.app.state, principal, body, asr_key=asr_key, llm_key=llm_key)


async def start_batch(
    state: State,
    principal: Principal,
    body: BatchRequest,
    *,
    asr_key: str | None,
    llm_key: str | None,
) -> Envelope[JobAccepted]:
    """The core of ``POST /v1/batch`` (ruling P10-S1). ``asr_key`` and ``llm_key`` are the raw
    values of the ``X-ASR-Api-Key`` and ``X-LLM-Api-Key`` headers (None without them).

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), ``check_jobs_enabled`` (503), then one ``check_rate`` hit."""
    settings: Settings = state.settings
    store: JobStore = state.job_store
    queue: FairQueue = state.job_queue

    _check_size(body, settings.batch_max_urls)
    if body.mode == "browser":  # B9: the browser workers take the items, if there are any
        if not settings.browser_enabled:
            raise ServiceError(ErrorCode.INVALID_REQUEST, BROWSER_DISABLED_MESSAGE, 400)
        queue = state.browser_queue
    check_ocr_request(body.ocr, settings)  # 400 for `ocr: always` while there is no OCR
    key = asr_key_of(asr_key)
    # 400 while there is no ASR (M9), or no key for an external endpoint (M11).
    check_transcribe_request(body.transcribe, settings, key=key)
    # 400 while there is no PII handling, 422 for annotate with HTML (N4).
    check_pii_request(body.pii, body.format, False, settings)
    # 422 for a non-admin key's XPath selectors (R4).
    check_selector_access(body.structured, principal)
    caller_llm_key = llm_key_of(llm_key)
    # 400 without LLM, embedding model or key; 422 for the combinations of S12.
    check_structured_request(
        body.structured, body.chunking, body.pii, body.format, settings, llm_key=caller_llm_key
    )
    # 400 without LLM or key, 422 with pii or without metadata (V-S2).
    check_describe_request(
        body.describe_images, body.pii, body.metadata, settings, llm_key=caller_llm_key
    )
    llm = wants_llm(body.structured, body.chunking, settings, describe=body.describe_images)
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
        structured=body.structured,
        chunking=body.chunking,
        describe_images=body.describe_images,
    )
    try:
        job_id = await open_job(state, principal, "batch", options, body.urls)
        try:
            if key is not None and body.transcribe:
                # The caller's key of the external endpoint, for any item that moves to the ASR
                # workers, while the job lives (M11); the job's deletion takes it along.
                await store.put_asr_key(job_id, key, whole_job=True)
            if caller_llm_key is not None and llm:
                # The caller's key of the LLM endpoint, for any item that moves to the llm
                # workers, while the job lives (ruling S9); the job's deletion takes it along.
                await store.put_llm_key(job_id, caller_llm_key)
            # Enqueued exactly once and never retried: a retry could queue some items twice.
            await queue.enqueue(job_id, principal.name, body.urls)
        except BaseException:
            await discard_job(state, principal, job_id)
            raise
    except (RedisError, OSError) as error:
        _log_unavailable(error)
        raise _unavailable() from None
    # Counted only here: a job that was discarded above never reaches a worker.
    state.job_metrics.created.labels(kind="batch").inc()
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


def _shown(item: JobItem, record: JobRecord) -> JobItem:
    """``item`` as the API shows it: a result that failed without data (a fault of the
    service's own) gets the envelope's fields with nothing the run did not reach (C14)."""
    if item.result is None:
        return item
    result = with_own_fault_data(
        item.result,
        item.url,
        format=record.options.format,
        compliance=record.options.compliance,
    )
    return item if result is item.result else item.model_copy(update={"result": result})


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
    the message "All N items failed.". `next_offset` is set while more items follow; a page also
    stops early once its results reach about 20 MB, and `next_offset` then continues it. A `site`
    job (`POST /v1/site-structure`) grows while it runs: each page, robots.txt and sitemap it
    finds becomes an item, so its `total` rises until it is completed. Each item's `run` is its
    last claim by a worker: the pool (`extract`, `browser`, `docling`, `asr`, `nlp`, `llm`), the
    claim number (more than 1 after a lost lease or a deferral) and the time; it is null before
    the first claim and for an item that ran in the API pod, and a result's `attempts` counts
    the fetches within one run. A job that does not exist, has expired or belongs to another
    caller (admins see all) answers 404.
    """
    return await read_job(request.app.state, principal, job_id, offset, limit)


async def read_job(
    state: State, principal: Principal, job_id: str, offset: int, limit: int
) -> Envelope[JobView]:
    """The core of ``GET /v1/jobs/{id}`` (ruling P10-S1): the job's status and progress with
    the page of ``limit`` items from ``offset`` (1 to ``MAX_PAGE_SIZE``; the route's query
    checks them, other callers bound them themselves).

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), ``check_jobs_enabled`` (503), then one ``check_rate`` hit."""
    store: JobStore = state.job_store
    try:
        record = await visible_job(store, job_id, principal)
        total = record.progress.total
        items = (
            await store.items(job_id, offset, limit, max_bytes=PAGE_MAX_BYTES)
            if offset < total
            else []
        )
        lost = _is_lost(record)
        if lost:
            record, items = _lost_view(record, items)
        items = [_shown(item, record) for item in items]
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
        next_offset=end if (end := offset + (len(items) or limit)) < total else None,
    )
    return Envelope(
        status=status,
        data=view,
        error=failure,
        trace_id=current_trace_id(),
    )


@router.delete(
    "/v1/jobs/{job_id}",
    name="delete_job",
    dependencies=[Depends(require_jobs_enabled), Depends(rate_limited)],
)
async def remove_job(
    job_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[JobDeleted]:
    """Cancel a job and delete it with everything stored for it: its results, its files and the
    keys it keeps for the caller.

    Any kind of job can be deleted, running or completed. A batch or site job no longer counts
    toward `BATCH_MAX_OPEN_PER_CLIENT`, also when an admin deletes another caller's job. Items
    still queued are dropped when a worker reaches them, and an item that is running finishes
    without its result being stored. Afterwards `GET /v1/jobs/{id}` and a second delete answer
    404. A job that does not exist, has expired or belongs to another caller (admins may delete
    all) answers 404. 400 when jobs are disabled, 503 when Valkey is unavailable (retryable).
    """
    return await delete_job(request.app.state, principal, job_id)


async def delete_job(state: State, principal: Principal, job_id: str) -> Envelope[JobDeleted]:
    """The core of ``DELETE /v1/jobs/{id}`` (ruling P10-S10): delete the job with all its keys
    (``JobStore.delete``) and remove it from its owner's open set, the record's owner, so an
    admin's delete frees the owner's slot. 404 as ``visible_job``; a Valkey error is a 503
    (retryable), and the job may then still be there. Workers drop the job's queued items
    (``job_item_dropped``, ``job_gone``); an item in flight, or the pod's task of a handed-over
    request (P1), stores nothing once it ends (``JobLost``).

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), ``check_jobs_enabled`` (503), then one ``check_rate`` hit."""
    valkey: Redis = state.valkey
    store: JobStore = state.job_store
    try:
        record = await visible_job(store, job_id, principal)
        await _remove(valkey, store, record.owner, job_id)
    except (RedisError, OSError) as error:
        raise backend_unavailable(error) from None
    _log.info("job_deleted", job_id=job_id, kind=record.kind)
    return Envelope(
        status="success",
        data=JobDeleted(id=job_id, kind=record.kind),
        trace_id=current_trace_id(),
    )
