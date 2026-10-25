"""``POST /v1/extract``: the public route in front of the extraction pipeline and the browser
workers.

**The flow** (``_flow``) of one request:

1. The request is checked first: browser options while browser mode is off answer 400 (B9), so
   does ``ocr: always`` while the service has no OCR (F11), and ``transcribe: true`` while it
   has no ASR (``ASR_ENABLED`` and ``JOBS_ENABLED``, M9); an ``evaluate`` action of a non-admin
   key 403, a compliance mode the key may not choose 403.
2. ``mode: browser``, a screenshot, and an ``auto`` request with actions, cookies or headers
   (R8) go to the browser workers at once (``browser_dispatch.py``).
3. Otherwise the fast pipeline runs. An ``auto`` result that needs the browser
   (``escalation.needs_browser``, in unblock mode for admins also a bot wall; R9) goes to the
   browser workers with the fast result as its draft, and the worker keeps the better of both.
4. If no browser worker answers in time, ``auto`` answers its fast result with the warning
   ``browser_unavailable``, without its fingerprint (it was never compared or stored, and the
   browser may answer the next request), and ``browser`` answers ``failed``/``timeout``,
   retryable. The browser job is deleted, as it is once the request answers with its result
   (one with a screenshot is kept as long as the screenshot).
5. While the service has OCR, a document that needs it goes to the Docling workers once the fast
   phase is done (``docling_dispatch.DoclingHandoff``, F12), with its text layer as the draft.

With ``wait_seconds`` the route can hand a slow request over to a job (ruling P1). The flow keeps
running in this pod, as the same task, and the job only stores its result. A request with
``transcribe: true`` and no ``wait_seconds`` waits 20 seconds (M9): a transcription is always a
job, and a video with subtitles still answers at once.

1. the flow runs as a task, and the route waits ``wait_seconds`` for it (the wait does not
   cancel it);
2. if the flow waits for a browser or a Docling job by then (or will, step 2 above), the route
   answers 202 with that job and detaches the task: the pool's worker writes the job's result;
3. otherwise an ``extract`` job with one URL is created and marked running with a lease of
   ``EXTRACT_TIMEOUT_SECONDS + 10`` seconds, and the route answers 202;
4. a done-callback of the task stores its envelope as item 0 of the job. A task that raised is
   stored as ``internal_error`` (a ``ServiceError`` keeps its own code). A flow whose fast result
   escalates (or needs OCR) after the hand-over reuses the job for the browser (or Docling; its
   lease is cleared) and stores nothing: that pool's worker writes item 0;
5. a pod that dies in between leaves the lease to expire; ``GET /v1/jobs/{id}`` then shows the
   item as failed (``jobs_api.py``).

If the job cannot be created because Valkey fails, the route does not answer 503 while the
extraction is still running: it keeps waiting for the task and answers with its envelope. It
does the same without trying while the pod holds ``EXTRACT_MAX_BACKGROUND`` extractions or
Valkey's memory is high (``memory_guard.py``). The
tasks are kept in ``app.state.background``, so that the lifespan can wait for them.

An extract job is not a batch: it does not count toward ``BATCH_MAX_OPEN_PER_CLIENT``.

``respond`` is shared with ``POST /v1/screenshot`` (``screenshot_api.py``), and the hand-over
(``answer_or_hand_over``, ``create_p1_job``) with ``POST /v1/extract/file`` (``file_api.py``),
whose P1 job has kind ``file`` and keeps the upload's bytes as the input of its item.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine
from functools import partial
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from text_extraction.auth import Principal, require_role
from text_extraction.browser_dispatch import (
    BrowserDispatcher,
    BrowserJobKind,
    Handover,
    job_options,
)
from text_extraction.browser_options import EvaluateAction
from text_extraction.compliance import ComplianceMode, ComplianceReport, resolve_mode
from text_extraction.docling_dispatch import DoclingDispatcher, DoclingHandoff
from text_extraction.documents.routing import OCR_DISABLED_MESSAGE, ocr_available
from text_extraction.documents.types import OcrOption
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.escalation import BROWSER_UNAVAILABLE_WARNING, needs_browser, unblock_for
from text_extraction.extract_models import (
    ExtractRequest,
    ExtractResult,
    Timings,
    failed_extract,
)
from text_extraction.file_models import UploadInputMeta
from text_extraction.job_models import JobAccepted, JobKind, JobOptions, JobState
from text_extraction.jobs import InputBudgetExceeded, JobLost, JobStore
from text_extraction.pipeline import Pipeline
from text_extraction.pipeline_media import (
    TRANSCRIBE_WAIT_SECONDS,
    TRANSCRIPTION_DISABLED_MESSAGE,
    asr_available,
)
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
BROWSER_DISABLED_MESSAGE = "Browser mode is not enabled on this service."
_EVALUATE_MESSAGE = "The evaluate action (JavaScript) is for admin keys only."
_BROWSER_TIMEOUT_MESSAGE = "No browser rendered the page in time. Try again later."

type _Background = set[asyncio.Task[Any]]
type _Answer = Envelope[ExtractResult] | None


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
    `upstream_status`, `mode_used`, `compliance`, `timings` and as much of the result as was
    reached. 4xx answers are about the request itself (invalid body, key, rate limit, a
    compliance mode the caller may not choose, browser options while browser mode is off,
    `ocr: always` while the service has no OCR, an `evaluate` action of a non-admin key); 5xx
    are faults of this service.

    `mode: browser`, `screenshot`, browser options and an `auto` page that needs a browser are
    rendered by a browser worker. Such a request can take the fast phase plus
    `BROWSER_QUEUE_WAIT_SECONDS` and the render (`BROWSER_TIMEOUT_SECONDS` + 5 s). Its result
    comes back through a job item: one over `JOB_ITEM_MAX_BYTES` (2 MB by default) as JSON
    answers without `content`, with the warning `content_not_stored_too_large`.

    With `wait_seconds`, an extraction that takes longer becomes a job: 202 with the job's id
    and `status_url` (`GET /v1/jobs/{id}`). Without it the request always waits for the result.
    """
    return await respond(request, body, principal, "extract")


def renders_first(body: ExtractRequest) -> bool:
    """Whether the request goes to the browser without a fast phase: ``browser``, a screenshot,
    or browser options (the fast fetch could not use them; R8)."""
    return body.mode == "browser" or body.screenshot or not body.secrets().empty()


def check_ocr_request(ocr: OcrOption, settings: Settings) -> None:
    """400 for ``ocr: always`` while the service has no OCR (F11): the request could not get
    what it asks for. ``auto`` and ``never`` are always fine."""
    if ocr == "always" and not ocr_available(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, OCR_DISABLED_MESSAGE, 400)


def check_transcribe_request(transcribe: bool, settings: Settings) -> None:
    """400 for ``transcribe: true`` while the service has no ASR (M9): the request could not
    get what it asks for."""
    if transcribe and not asr_available(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, TRANSCRIPTION_DISABLED_MESSAGE, 400)


def request_wait_seconds(body: ExtractRequest) -> float | None:
    """The request's ``wait_seconds``; with ``transcribe: true`` and none set, 20 (M9)."""
    if body.wait_seconds is None and body.transcribe:
        return TRANSCRIBE_WAIT_SECONDS
    return body.wait_seconds


def check_browser_request(body: ExtractRequest, principal: Principal, settings: Settings) -> None:
    """400 for browser work while browser mode is off (B9), 403 for an ``evaluate`` action of a
    non-admin key (B6)."""
    if renders_first(body) and not settings.browser_enabled:
        raise ServiceError(ErrorCode.INVALID_REQUEST, BROWSER_DISABLED_MESSAGE, 400)
    if principal.role != "admin" and any(isinstance(a, EvaluateAction) for a in body.actions):
        raise ServiceError(ErrorCode.FORBIDDEN, _EVALUATE_MESSAGE, 403)


async def respond(
    request: Request, body: ExtractRequest, principal: Principal, kind: BrowserJobKind
) -> Envelope[ExtractResult] | JSONResponse:
    """Run the flow for ``body``, or hand it over to a job after ``wait_seconds`` (the module
    docstring). ``kind`` is the kind of a browser job the request creates."""
    state = request.app.state
    settings: Settings = state.settings
    # Checked now, so that a refused request is a 4xx and never a job.
    compliance = resolve_mode(body.compliance, principal, settings)
    check_browser_request(body, principal, settings)
    check_ocr_request(body.ocr, settings)
    check_transcribe_request(body.transcribe, settings)
    wait_seconds = request_wait_seconds(body)
    handover = Handover()
    wait_until = handover_time(wait_seconds, settings)
    flow = partial(_flow, state, body, principal, compliance, handover, kind, wait_until)
    options = job_options(body, compliance)
    return await answer_or_hand_over(
        state,
        flow,
        handover,
        wait_seconds=wait_seconds,
        kind=kind,
        renders_first=renders_first(body),
        hand_over=partial(create_p1_job, state.job_store, settings, principal, options, body.url),
    )


def handover_time(wait_seconds: float | None, settings: Settings) -> float | None:
    """When (``time.monotonic()``) the route of a request with ``wait_seconds`` turns it into a
    job; None when it never does."""
    if wait_seconds is None or not settings.jobs_enabled:
        return None
    return time.monotonic() + wait_seconds


async def wait_for_flow(task: asyncio.Task[Any], seconds: float) -> None:
    """Wait ``seconds`` for the flow's task, without cancelling it."""
    await asyncio.wait({task}, timeout=seconds)


async def answer_or_hand_over(
    state: Any,
    flow: Callable[[], Coroutine[Any, Any, _Answer]],
    handover: Handover,
    *,
    wait_seconds: float | None,
    kind: JobKind,
    renders_first: bool,
    hand_over: Callable[[], Awaitable[str | None]],
) -> Envelope[ExtractResult] | JSONResponse:
    """Run ``flow``, or answer 202 with a job after ``wait_seconds`` (the module docstring):
    the pool job the flow dispatched (of ``kind``), or a P1 job that ``hand_over`` creates (of
    kind ``file`` for an upload, else ``extract``). ``renders_first`` says that the flow goes to
    the browser at once. Shared with ``POST /v1/extract/file`` (``file_api.py``)."""
    settings: Settings = state.settings
    if wait_seconds is None or not settings.jobs_enabled:
        return _answered(await flow())

    background: _Background = state.background
    task = asyncio.create_task(flow())
    background.add(task)
    try:
        if wait_seconds > 0:
            await wait_for_flow(task, wait_seconds)
    except asyncio.CancelledError:
        task.cancel()  # nobody is waiting for it any more, and no job exists
        background.discard(task)
        raise
    if task.done():
        background.discard(task)
        return _answered(task.result())
    if renders_first or handover.claimed == "pool":
        return await _detach(state, task, handover, kind)

    store: JobStore = state.job_store
    job_id: str | None = None
    handed_over_at = time.monotonic()
    guard: MemoryPressure = state.memory_guard
    # At the cap this pod holds no more extractions for jobs, and while Valkey is nearly full it
    # stores no new job: the request waits for its own extraction.
    if (
        len(background) <= settings.extract_max_background
        and not guard.high
        and handover.claim_p1()
    ):
        try:
            job_id = await hand_over()
        except asyncio.CancelledError:
            task.cancel()  # nobody is waiting for it any more, and no job is left
            background.discard(task)
            raise
        finally:
            handover.settle(job_id)
    if job_id is None:
        try:
            return _answered(await task)
        finally:
            background.discard(task)
    p1_kind: JobKind = "file" if kind == "file" else "extract"
    metrics: JobMetrics = state.job_metrics
    metrics.created.labels(kind=p1_kind).inc()
    task.add_done_callback(
        partial(_store_when_done, background, store, metrics, job_id, handed_over_at, kind=p1_kind)
    )
    _log.info("extract_job_accepted", job_id=job_id, kind=p1_kind)
    return _accepted(job_id, p1_kind, "running")


def _answered(answer: _Answer) -> Envelope[ExtractResult]:
    if answer is None:  # pragma: no cover - only a flow that handed over answers None
        raise RuntimeError("the flow answered nothing without a hand-over")
    return answer


def _accepted(job_id: str, kind: JobKind, state: JobState) -> JSONResponse:
    accepted = Envelope(
        status="success",
        data=JobAccepted(
            id=job_id, kind=kind, state=state, total=1, status_url=f"/v1/jobs/{job_id}"
        ),
        trace_id=current_trace_id(),
    )
    return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))


async def _detach(
    state: Any, task: asyncio.Task[_Answer], handover: Handover, kind: JobKind
) -> Envelope[ExtractResult] | JSONResponse:
    """``wait_seconds`` ran out while the flow dispatches or waits for a pool job (a browser or
    a Docling job): answer 202 with that job and let the flow stop waiting. If the dispatch
    failed, wait for the flow's own answer."""
    background: _Background = state.background
    settled = asyncio.ensure_future(handover.settled())
    try:
        await asyncio.wait({task, settled}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()  # the flow deletes its job when cancelled
        background.discard(task)
        raise
    finally:
        settled.cancel()
    job_id = handover.pool_job
    # A flow that released its job (it gave up and deletes it) answers on its own.
    if task.done() or job_id is None or not handover.detach():
        try:
            return _answered(await task)
        finally:
            background.discard(task)
    task.add_done_callback(partial(_forget, background))
    metrics: JobMetrics = state.job_metrics
    metrics.created.labels(kind=kind).inc()
    _log.info(f"{handover.pool}_job_accepted", job_id=job_id, kind=kind)
    return _accepted(job_id, kind, await _job_state(state.job_store, job_id))


async def _job_state(store: JobStore, job_id: str) -> JobState:
    """The job's state now; ``queued`` when it cannot be read (``status_url`` tells the rest)."""
    try:
        record = await store.get(job_id)
    except (RedisError, OSError):
        return "queued"
    return record.state if record is not None else "queued"


def _forget(background: _Background, task: asyncio.Task[_Answer]) -> None:
    """Done-callback of a detached flow: its job belongs to the pool's worker now."""
    background.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        _log.error("detached_flow_failed", exc_info=error)


# --- the flow -----------------------------------------------------------------------------------


async def _flow(
    state: Any,
    body: ExtractRequest,
    principal: Principal,
    compliance: ComplianceMode,
    handover: Handover,
    kind: BrowserJobKind,
    wait_until: float | None = None,
) -> _Answer:
    """The envelope of the request, or None when its job's result is written by a pool's
    worker (after a hand-over); see the module docstring. ``wait_until`` is when the route
    turns the request into a job (``handover_time``)."""
    started = time.perf_counter()
    if renders_first(body):
        return await _in_browser(state, body, principal, compliance, handover, kind, None, started)
    pipeline: Pipeline = state.pipeline
    ocr = docling_handoff(
        state,
        handover,
        kind="extract",
        options=job_options(body, compliance),
        principal=principal,
        url=body.url,
        wait_until=wait_until,
    )
    if ocr is None:
        fast = await pipeline.run(body, principal)
    else:
        fast = await pipeline.run(body, principal, ocr_handoff=ocr)
        if ocr.handed_over:
            return None  # the Docling worker writes the job's result
    settings: Settings = state.settings
    unblock = unblock_for(principal, compliance, settings)
    if (
        body.mode != "auto"
        or not settings.browser_enabled
        or not needs_browser(fast, unblock=unblock)
    ):
        return fast
    return await _in_browser(state, body, principal, compliance, handover, kind, fast, started)


def docling_handoff(
    state: Any,
    handover: Handover,
    *,
    kind: Literal["extract", "file"],
    options: JobOptions,
    principal: Principal,
    url: str | None,
    wait_until: float | None,
) -> DoclingHandoff | None:
    """The OCR handoff of one request (F12); None while the service has no OCR."""
    dispatcher: DoclingDispatcher | None = state.docling_dispatcher
    if dispatcher is None:
        return None
    return DoclingHandoff(
        dispatcher,
        state.memory_guard,
        handover,
        kind=kind,
        options=options,
        principal=principal,
        url=url,
        wait_until=wait_until,
    )


async def _in_browser(
    state: Any,
    body: ExtractRequest,
    principal: Principal,
    compliance: ComplianceMode,
    handover: Handover,
    kind: BrowserJobKind,
    draft: Envelope[ExtractResult] | None,
    started: float,
) -> _Answer:
    """Dispatch the request to the browser workers and wait for their result. ``draft`` is the
    fast result of an escalating ``auto`` request (None when the request renders first)."""
    dispatcher: BrowserDispatcher = state.browser_dispatcher
    if not handover.claim_pool("browser"):  # the route is handing it over to a P1 job
        await handover.settled()
        if handover.job_id is not None:
            try:
                await dispatcher.submit(
                    kind, body, principal, compliance, job_id=handover.job_id, draft=draft
                )
            except (RedisError, OSError, JobLost) as error:
                _dispatch_failed(error)
                return _unavailable(body, compliance, draft, started)  # stored in the P1 job
            return None  # the browser worker writes item 0 of the P1 job
        # The P1 job could not be created: the route waits for this answer.
    job_id: str | None = None
    try:
        job_id = await dispatcher.submit(kind, body, principal, compliance, draft=draft)
    except (RedisError, OSError) as error:
        _dispatch_failed(error)
        if draft is None:
            raise _backend_unavailable() from None
        return _unavailable(body, compliance, draft, started)
    finally:
        if handover.claimed == "pool":
            handover.settle(job_id)
    unblock = unblock_for(principal, compliance, state.settings)
    try:
        result = await dispatcher.wait(job_id, unblock=unblock, detached=lambda: handover.detached)
    except asyncio.CancelledError:
        if handover.release():
            await asyncio.shield(dispatcher.abandon(job_id))
        raise
    except (RedisError, OSError) as error:
        _dispatch_failed(error)
        if not handover.release():
            return None  # the route answered 202 with the job: the worker writes its result
        await asyncio.shield(dispatcher.abandon(job_id))
        if draft is None:
            raise _backend_unavailable() from None
        return _unavailable(body, compliance, draft, started)
    if result is None:
        if not handover.release():
            return None  # the route answered 202 with the job: the worker writes its result
        await asyncio.shield(dispatcher.abandon(job_id))
        return _unavailable(body, compliance, draft, started)
    # Answered here: nobody reads the job again, unless the route answered 202 with it meanwhile.
    if handover.release():
        await asyncio.shield(dispatcher.answered(job_id, result))
    return _timed(result, started)


def _dispatch_failed(error: Exception) -> None:
    # Type and text only: the text names host and port, never credentials or URLs.
    _log.warning("browser_dispatch_failed", error_type=type(error).__name__, error=str(error))


def _backend_unavailable() -> ServiceError:
    return ServiceError(
        ErrorCode.INTERNAL_ERROR,
        "The job backend is unavailable. Try again later.",
        503,
        retryable=True,
    )


def _timed(envelope: Envelope[ExtractResult], started: float) -> Envelope[ExtractResult]:
    """The envelope with this request's trace id and its whole time in ``timings.total_ms``:
    the fast phase, the wait for a worker and the render."""
    data = envelope.data
    if data is not None:
        total = max(data.timings.total_ms, round((time.perf_counter() - started) * 1000))
        timings = data.timings.model_copy(update={"total_ms": total})
        data = data.model_copy(update={"timings": timings})
    return envelope.model_copy(update={"data": data, "trace_id": current_trace_id()})


def _unavailable(
    body: ExtractRequest,
    compliance: ComplianceMode,
    draft: Envelope[ExtractResult] | None,
    started: float,
) -> Envelope[ExtractResult]:
    """The answer when no browser worker answered: the fast result with the warning
    ``browser_unavailable`` and without its fingerprint, which was never compared (B5); without
    a fast result, ``failed``/``timeout``, retryable."""
    if draft is not None and draft.data is not None:
        data = draft.data.model_copy(
            update={
                "fingerprint": None,
                "warnings": [*draft.data.warnings, BROWSER_UNAVAILABLE_WARNING],
            }
        )
        return _timed(draft.model_copy(update={"data": data}), started)
    if draft is not None:  # pragma: no cover - needs_browser only passes results with data
        return _timed(draft, started)
    private = not body.secrets().empty() or body.screenshot
    result = ExtractResult(
        url=body.url,
        final_url=None,
        upstream_status=None,
        content_type=None,
        mode_used="browser",
        attempts=0,
        language=None,
        word_count=0,
        title=None,
        format=body.format,
        content=None,
        quality=None,
        timings=Timings(total_ms=0, compliance_ms=0, fetch_ms=0, convert_ms=0),
        compliance=ComplianceReport(mode=compliance, decision="skipped"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="bypass" if private or not body.cache else "miss",
        warnings=[],
    )
    error = ErrorInfo(code=ErrorCode.TIMEOUT, message=_BROWSER_TIMEOUT_MESSAGE, retryable=True)
    return _timed(Envelope(status="failed", data=result, error=error), started)


# --- the P1 job -----------------------------------------------------------------------------------


async def create_p1_job(
    store: JobStore,
    settings: Settings,
    principal: Principal,
    options: JobOptions,
    url: str | None,
    *,
    kind: Literal["extract", "file"] = "extract",
    upload: tuple[bytes, UploadInputMeta] | None = None,
) -> str | None:
    """Create the P1 job (of ``kind``; an upload's with its bytes as the input of item 0, for
    ``DOCLING_INPUT_TTL_SECONDS``, within ``DOCLING_INPUT_BUDGET_BYTES``) and mark it running;
    its id, or None when Valkey fails or the input budget is full (logged): the request then
    stays synchronous. A job that was created but could not be completed, or whose creator is
    cancelled, is deleted again, if that works."""
    job_id: str | None = None
    try:
        job_id = await store.create(kind, principal, options, [url], trace_id=current_trace_id())
        if upload is not None:
            body, meta = upload
            await store.put_input(job_id, 0, body, meta, settings.docling_input_ttl_seconds)
        lease = settings.extract_timeout_seconds + _LEASE_MARGIN_SECONDS
        await store.mark_running(job_id, lease_seconds=lease)
    except (RedisError, OSError, JobLost, InputBudgetExceeded) as error:
        # The type only for an upload: an error's text never gets near its bytes.
        text = {} if upload is not None else {"error": str(error)}
        _log.warning("extract_job_handover_failed", error_type=type(error).__name__, **text)
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


def _envelope_of(task: asyncio.Task[_Answer]) -> _Answer:
    """What the finished task produced; a failure becomes a failed envelope. None when the
    browser worker writes the job's result."""
    if task.cancelled():
        return failed_extract(ErrorCode.INTERNAL_ERROR, _CANCELLED_MESSAGE, retryable=True)
    error = task.exception()
    if error is None:
        return task.result()
    if isinstance(error, ServiceError):
        return failed_extract(error.code, error.message, retryable=error.retryable)
    _log.error("extract_job_failed", exc_info=error)
    return failed_extract(ErrorCode.INTERNAL_ERROR, _INTERNAL_MESSAGE, retryable=False)


def _store_when_done(
    background: _Background,
    store: JobStore,
    metrics: JobMetrics,
    job_id: str,
    created_at: float,
    task: asyncio.Task[_Answer],
    *,
    kind: JobKind = "extract",
) -> None:
    """Done-callback of a handed-over extraction: store its envelope in the job, unless the job
    went on to a pool's worker, which writes it. The storing task is added to ``background``
    before the extraction leaves it, so a shutdown that waits for the set never misses the
    write."""
    envelope = _envelope_of(task)
    if envelope is not None:
        storing = asyncio.create_task(
            _store_result(store, metrics, job_id, created_at, envelope, kind=kind)
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
    *,
    kind: JobKind = "extract",
) -> None:
    """Store the envelope as item 0 and observe the job's duration (``created_at`` is a
    ``time.monotonic()`` reading from just before the job was created); an upload's input is
    deleted then. A Valkey failure is logged and not raised: the job's lease then runs out and
    ``GET /v1/jobs/{id}`` reports the item as lost (an input expires on its own)."""
    try:
        # The first and only write of a P1 job completes it ("last"); a repeat is not counted.
        if await store.write_result(job_id, 0, envelope) == "last":
            metrics.duration.labels(kind=kind).observe(max(time.monotonic() - created_at, 0.0))
        if kind == "file":
            await store.delete_input(job_id, 0)
    except JobLost:
        _log.warning("extract_job_gone", job_id=job_id)
    except (RedisError, OSError) as error:
        _log.warning(
            "extract_job_result_not_stored",
            job_id=job_id,
            error_type=type(error).__name__,
            error=str(error),
        )
