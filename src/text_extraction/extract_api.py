"""``POST /v1/extract``: the public route in front of the extraction pipeline and the browser
workers.

**The flow** (``_flow``) of one request:

1. The request is checked first: browser options while browser mode is off answer 400 (B9), so
   does ``ocr: always`` while the service has no OCR (F11), and ``transcribe: true`` while it
   has no ASR (``ASR_ENABLED`` and ``JOBS_ENABLED``, M9) or transcribes on an external endpoint
   without a key for it (neither ``X-ASR-Api-Key`` nor ``ASR_EXTERNAL_API_KEY``, M11); an
   ``evaluate`` action of a non-admin key 403, a compliance mode the key may not choose 403.
2. ``mode: browser``, a screenshot, and an ``auto`` request with actions, cookies or headers
   (R8) go to the browser workers at once (``browser_dispatch.py``).
3. Otherwise the fast pipeline runs. An ``auto`` result that needs the browser
   (``escalation.needs_browser``, in unblock mode for admins also a bot wall or a refusal; R9,
   B16) goes to the browser workers with the fast result as its draft, and the worker keeps the
   better of both. So does a YouTube video's details whose subtitles could not be read, while
   ``MEDIA_TRANSCRIPT_PANEL_ENABLED`` is on and the request has no ``transcribe``
   (``escalation.transcript_panel_applies``, P13-S1): the worker tries the video's transcript
   view.
4. If no browser worker answers in time, ``auto`` answers its fast result with the warning
   ``browser_unavailable``, without its fingerprint (it was never compared or stored, and the
   browser may answer the next request), and ``browser`` answers ``failed``/``timeout``,
   retryable. The browser job is deleted, as it is once the request answers with its result
   (one with a screenshot is kept as long as the screenshot).
5. While the service has OCR, a document that needs it goes to the Docling workers once the fast
   phase is done (``docling_dispatch.DoclingHandoff``, F12), with its text layer as the draft.
6. While the service has ASR, media that needs a transcription (``transcribe: true``: a platform
   video without subtitles, a media file) goes to the ASR workers once the fast phase is done
   (``asr_dispatch.AsrHandoff``, M9), with the video's metadata as the draft and the
   ``X-ASR-Api-Key`` header as the job's secret. Nothing waits for a transcription: the route
   answers 202 with the ASR job at once.
7. With ``pii`` (while the service has PII, ``PII_ENABLED`` and ``JOBS_ENABLED``; else 400; with
   a screenshot, or ``annotate`` with ``format: html``, 422), the envelope the flow ends with (a
   fresh or cached fast result, a browser or Docling job's result, an OCR or browser fallback)
   goes to the nlp workers (``pii_dispatch.PiiHandoff``, N11) as the last step, unless it is
   processed already: a browser or Docling job of such a request moves its result on to pool
   ``nlp`` in the same job, and the request waits ``PII_SYNC_WAIT_SECONDS`` longer for it. The
   cache and the fingerprints keep the unprocessed extraction (N9). Every envelope the route
   answers or stores passes ``pii.fields.fail_closed`` (N12): one that still owes the step is
   ``pii_failed`` with its text withheld.
8. With ``structured`` or ``chunking``, the envelope the flow ends with (after the PII step, on its
   final, possibly masked content) gets the post stage (``structured.post.post_at_finish``, ruling
   S13) unless it had it already (a browser, Docling, nlp or llm job's result is done by its
   worker): the patterns, on the app's post pool (``POST_CONCURRENCY`` threads, ruling R21); then an
   envelope that needs the LLM goes to the llm workers (``llm_dispatch.LlmHandoff``, D5 a) with the
   caller's ``X-LLM-Api-Key`` and the request waits ``LLM_SYNC_WAIT_SECONDS`` longer for it; last
   ``finish_post``, on the post pool, as the guard. While the memory guard is high, when Valkey
   fails or nobody answered in time, the answer is the content without the LLM work:
   ``partial``/``llm_failed``, retryable, never a 5xx. An envelope that still owes the PII step gets
   none: the guard withholds it. The cache keeps the DOM stage's result: a cache hit gets a post
   stage of its own. A P1 job's write (``_store_result``) runs the post stage too, for an envelope
   that lacks it, without the LLM, before the PII guard.

With ``wait_seconds`` the route can hand a slow request over to a job (ruling P1). The flow keeps
running in this pod, as the same task, and the job only stores its result. A request with
``transcribe: true`` and no ``wait_seconds`` waits 20 seconds (M9): a transcription is always a
job, and a video with subtitles still answers at once. A request with ``pii`` and no
``wait_seconds`` waits ``PII_SYNC_WAIT_SECONDS`` (D4 a): a slow nlp worker turns it into a job;
one that may need the LLM waits ``LLM_SYNC_WAIT_SECONDS`` (the largest default applies).

1. the flow runs as a task, and the route waits ``wait_seconds`` for it (the wait does not
   cancel it);
2. if the flow waits for a browser, a Docling, an nlp or an llm job by then (or will, step 2
   above), the route answers 202 with that job and detaches the task: the pool's worker writes
   the job's result.
   A flow that dispatched an ASR job answers None and leaves the job to the route
   (``Handover.adopt``), which answers 202 with it as soon as the flow ends, whether or not
   ``wait_seconds`` has passed;
3. otherwise an ``extract`` job with one URL is created and marked running with a lease of
   ``EXTRACT_TIMEOUT_SECONDS + 10`` seconds, and the route answers 202;
4. a done-callback of the task stores its envelope as item 0 of the job. A task that raised is
   stored as ``internal_error`` (a ``ServiceError`` keeps its own code). A flow whose fast result
   escalates (or needs OCR, owes the PII step or needs the LLM) after the hand-over reuses the
   job for the browser (or Docling, nlp or llm; its lease is cleared) and stores nothing: that
   pool's worker writes item 0;
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
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry
from redis.exceptions import RedisError
from starlette.datastructures import State

from text_extraction.asr_dispatch import ASR_KEY_HEADER, AsrDispatcher, AsrHandoff, asr_key_of
from text_extraction.auth import Principal, require_role
from text_extraction.browser_dispatch import (
    BrowserDispatcher,
    BrowserJobKind,
    Handover,
    job_options,
)
from text_extraction.browser_options import EvaluateAction
from text_extraction.chunking.types import ChunkingOptions
from text_extraction.compliance import ComplianceMode, ComplianceReport, resolve_mode
from text_extraction.conversion_pool import ConversionPool
from text_extraction.convert import OutputFormat
from text_extraction.docling_dispatch import DoclingDispatcher, DoclingHandoff
from text_extraction.documents.routing import OCR_DISABLED_MESSAGE, ocr_available
from text_extraction.documents.types import OcrOption
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.escalation import (
    BROWSER_UNAVAILABLE_WARNING,
    needs_browser,
    transcript_panel_applies,
    unblock_for,
)
from text_extraction.extract_models import (
    ExtractRequest,
    ExtractResult,
    Timings,
    failed_extract,
)
from text_extraction.file_models import UploadInputMeta
from text_extraction.job_models import JobAccepted, JobKind, JobOptions, JobState
from text_extraction.jobs import InputBudgetExceeded, JobLost, JobStore
from text_extraction.llm_dispatch import LlmDispatcher, LlmHandoff, UploadInput
from text_extraction.netguard import InvalidUrlError, validate_url
from text_extraction.pii.fields import ANNOTATE_HTML_MESSAGE, fail_closed
from text_extraction.pii.types import PiiOptions
from text_extraction.pii_dispatch import PiiDispatcher, PiiHandoff, pii_available
from text_extraction.pipeline import Pipeline
from text_extraction.pipeline_media import (
    TRANSCRIBE_WAIT_SECONDS,
    TRANSCRIPTION_DISABLED_MESSAGE,
    asr_available,
)
from text_extraction.pool_dispatch import GracefulShutdown
from text_extraction.queue_metrics import JobMetrics
from text_extraction.ratelimit import rate_limited
from text_extraction.result_cache import MemoryPressure
from text_extraction.settings import Settings
from text_extraction.structured.checks import (
    LLM_KEY_HEADER,
    check_selector_access,
    check_structured_request,
    llm_key_of,
    wants_llm,
)
from text_extraction.structured.post import LlmHop, post_at_finish
from text_extraction.structured.types import StructuredOptions
from text_extraction.tracing import current_trace_id
from text_extraction.vision.checks import check_describe_request
from text_extraction.vision.types import DescribeImagesOptions

_log = structlog.get_logger(__name__)

router = APIRouter()

# Seconds on top of ``EXTRACT_TIMEOUT_SECONDS`` that the lease of an extract job lasts.
_LEASE_MARGIN_SECONDS = 10

_INTERNAL_MESSAGE = "Internal server error."
_CANCELLED_MESSAGE = "The extraction was cancelled."
BROWSER_DISABLED_MESSAGE = "Browser mode is not enabled on this service."
_EVALUATE_MESSAGE = "The evaluate action (JavaScript) is for admin keys only."
_BROWSER_TIMEOUT_MESSAGE = "No browser rendered the page in time. Try again later."
PII_DISABLED_MESSAGE = "PII handling is not enabled on this service."
PII_SCREENSHOT_MESSAGE = "A screenshot cannot be masked; ask for it without pii."

type _Background = set[asyncio.Task[Any]]
type _Answer = Envelope[ExtractResult] | None

ASR_KEY_MISSING_MESSAGE = (
    "This service transcribes with an external endpoint and has no API key for it: send yours "
    f"in the {ASR_KEY_HEADER} header."
)
ASR_KEY_DESCRIPTION = (
    "Your API key of the external transcription endpoint, where the service transcribes with "
    "one (`ASR_BACKEND=openai`); it wins over the service's own key. A secret: stored only with "
    "a job that waits for a transcription, deleted with its outcome, never shown or logged. "
    "Required with `transcribe: true` (and for audio and video uploads) where the service has "
    "no key of its own: without it the request is 400."
)
LLM_KEY_DESCRIPTION = (
    "Your API key of the LLM endpoint, for `structured` and `chunking` work that calls the LLM "
    "(`structured.strategy` `llm` or `auto`, `chunking.strategy` `semantic`); it wins over the "
    "service's own key (`LLM_API_KEY`). A secret: stored only with a job whose request may need "
    "the LLM, deleted with its outcome, never shown or logged. Required for such work where the "
    "service has no key of its own: without it the request is 400. 1 to 1024 visible ASCII "
    "characters, else 400."
)


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
    asr_key: Annotated[
        str | None, Header(alias=ASR_KEY_HEADER, description=ASR_KEY_DESCRIPTION)
    ] = None,
    llm_key: Annotated[
        str | None, Header(alias=LLM_KEY_HEADER, description=LLM_KEY_DESCRIPTION)
    ] = None,
) -> Envelope[ExtractResult] | JSONResponse:
    """Fetch a URL and return its main content as Markdown, text or HTML.

    Every outcome of the target answers 200: `status` is `success`, `partial` (content, but
    too short) or `failed`, with `error.code`, `error.retryable` and, in `data`,
    `upstream_status`, `mode_used`, `compliance`, `timings` and as much of the result as was
    reached. 4xx answers are about the request itself (invalid body, key, rate limit, a
    compliance mode the caller may not choose, browser options while browser mode is off,
    `ocr: always` while the service has no OCR, `transcribe: true` without the
    `X-ASR-Api-Key` header where the service transcribes externally and has no key of its own,
    an `evaluate` action of a non-admin key); 5xx are faults of this service.

    `mode: browser`, `screenshot`, browser options and an `auto` page that needs a browser are
    rendered by a browser worker. Such a request can take the fast phase plus
    `BROWSER_QUEUE_WAIT_SECONDS` and the render (`BROWSER_TIMEOUT_SECONDS` + 5 s). Its result
    comes back through a job item: one over `JOB_ITEM_MAX_BYTES` (2 MB by default) as JSON
    answers without `content`, with the warning `content_not_stored_too_large`.

    With `wait_seconds`, an extraction that takes longer becomes a job: 202 with the job's id
    and `status_url` (`GET /v1/jobs/{id}`). Without it the request always waits for the result.

    `transcribe: true` (where the service has speech recognition) waits 20 s by default: a
    video with subtitles answers 200 as usual, and media that needs a transcription (a video
    without subtitles, an audio or video file) answers 202 at once with a job, whose draft is
    the video's metadata. While the key has `ASR_MAX_OPEN_PER_CLIENT` transcriptions waiting, or
    the service is busy, the answer is the metadata with the warning `asr_unavailable` (a media
    file: `failed`/`rate_limited`, retryable).

    `pii` (where the service has PII handling, else 400; not with `screenshot`, and `annotate`
    not with `format: html`: 422) finds personal data in the result and handles it as the mode
    says, reported in `data.pii`. It waits `PII_SYNC_WAIT_SECONDS` (20 s) by default, then
    answers 202 with a job. A result that could not be processed answers `failed`/`pii_failed`
    (retryable) with its content, title, metadata and links withheld; one over
    `JOB_ITEM_MAX_BYTES` comes back without `content`, as from a browser.

    `structured` and `chunking` that may call the LLM (`structured.strategy` `llm` or `auto`,
    `chunking.strategy` `semantic`) wait `LLM_SYNC_WAIT_SECONDS` (30 s) by default, then answer
    202 with a job; the largest default of `transcribe`, `pii` and the LLM applies. 400 while the
    service has no LLM (`strategy` `llm`, `learn_selectors`, `semantic`), for `semantic` without
    `LLM_EMBEDDING_MODEL`, for LLM work without a key (neither `X-LLM-Api-Key` nor
    `LLM_API_KEY`) and for an `X-LLM-Api-Key` that is not 1 to 1024 visible ASCII characters; 422
    for `chunking` with `format: html`, for `pii` with a strategy or option that reads the raw
    page (`metadata`, `selectors`, `tables`, `learn_selectors`, `selectors`) and for
    `learn_selectors` with a strategy but `llm` or `auto`.
    """
    return await respond(
        request.app.state,
        body,
        principal,
        "extract",
        asr_key=asr_key_of(asr_key),
        llm_key=llm_key_of(llm_key),
    )


def renders_first(body: ExtractRequest) -> bool:
    """Whether the request goes to the browser without a fast phase: ``browser``, a screenshot,
    or browser options (the fast fetch could not use them; R8)."""
    return body.mode == "browser" or body.screenshot or not body.secrets().empty()


def check_ocr_request(ocr: OcrOption, settings: Settings) -> None:
    """400 for ``ocr: always`` while the service has no OCR (F11): the request could not get
    what it asks for. ``auto`` and ``never`` are always fine."""
    if ocr == "always" and not ocr_available(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, OCR_DISABLED_MESSAGE, 400)


def check_transcribe_request(
    transcribe: bool, settings: Settings, *, key: str | None = None
) -> None:
    """400 for ``transcribe: true`` while the service has no ASR (M9), or transcribes on an
    external endpoint without a key for it: neither the caller's ``key`` (``X-ASR-Api-Key``)
    nor ``ASR_EXTERNAL_API_KEY`` (M11). The request could not get what it asks for."""
    if not transcribe:
        return
    if not asr_available(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, TRANSCRIPTION_DISABLED_MESSAGE, 400)
    check_asr_key(settings, key)


def check_asr_key(settings: Settings, key: str | None) -> None:
    """400 when the service transcribes on an external endpoint (``ASR_BACKEND=openai``) and
    has no key for it: neither the caller's ``key`` nor ``ASR_EXTERNAL_API_KEY``. The message
    names the header, never a value."""
    if key is None and asr_key_missing(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, ASR_KEY_MISSING_MESSAGE, 400)


def asr_key_missing(settings: Settings) -> bool:
    """Whether a transcription needs the caller's key: the external backend without
    ``ASR_EXTERNAL_API_KEY``."""
    return settings.asr_backend == "openai" and settings.asr_external_api_key is None


def check_pii_request(
    pii: PiiOptions | None, req_format: OutputFormat, screenshot: bool, settings: Settings
) -> None:
    """400 for ``pii`` while the service has no PII handling (``PII_ENABLED`` and
    ``JOBS_ENABLED``), 422 for ``pii`` with a screenshot (a picture cannot be masked) and for
    ``annotate`` with ``format: html`` (offsets into HTML text nodes are not stable; N2, N4)."""
    if pii is None:
        return
    if not pii_available(settings):
        raise ServiceError(ErrorCode.INVALID_REQUEST, PII_DISABLED_MESSAGE, 400)
    if screenshot:
        raise ServiceError(ErrorCode.INVALID_REQUEST, PII_SCREENSHOT_MESSAGE, 422)
    if pii.mode == "annotate" and req_format == "html":
        raise ServiceError(ErrorCode.INVALID_REQUEST, ANNOTATE_HTML_MESSAGE, 422)


def default_wait_seconds(
    settings: Settings, *, transcribe: bool = False, pii: bool, llm: bool
) -> float | None:
    """The wait of a request without ``wait_seconds``: for ``transcribe: true`` 20 (M9), with
    ``pii`` ``PII_SYNC_WAIT_SECONDS`` (D4 a), for a request that may need the LLM
    ``LLM_SYNC_WAIT_SECONDS`` (S13); the largest of those that apply. None when none applies: the
    request waits for its result."""
    waits = [
        seconds
        for applies, seconds in (
            (transcribe, TRANSCRIBE_WAIT_SECONDS),
            (pii, settings.pii_sync_wait_seconds),
            (llm, settings.llm_sync_wait_seconds),
        )
        if applies
    ]
    return max(waits) if waits else None


def request_wait_seconds(body: ExtractRequest, settings: Settings) -> float | None:
    """The request's ``wait_seconds``; none set, the largest default that applies
    (``default_wait_seconds``)."""
    if body.wait_seconds is not None:
        return body.wait_seconds
    return default_wait_seconds(
        settings,
        transcribe=body.transcribe,
        pii=body.pii is not None,
        llm=wants_llm(body.structured, body.chunking, settings, describe=body.describe_images),
    )


def check_browser_request(body: ExtractRequest, principal: Principal, settings: Settings) -> None:
    """400 for browser work while browser mode is off (B9), 403 for an ``evaluate`` action of a
    non-admin key (B6)."""
    if renders_first(body) and not settings.browser_enabled:
        raise ServiceError(ErrorCode.INVALID_REQUEST, BROWSER_DISABLED_MESSAGE, 400)
    if principal.role != "admin" and any(isinstance(a, EvaluateAction) for a in body.actions):
        raise ServiceError(ErrorCode.FORBIDDEN, _EVALUATE_MESSAGE, 403)


async def respond(
    state: State,
    body: ExtractRequest,
    principal: Principal,
    kind: BrowserJobKind,
    *,
    asr_key: str | None = None,
    llm_key: str | None = None,
) -> Envelope[ExtractResult] | JSONResponse:
    """Run the flow for ``body``, or hand it over to a job after ``wait_seconds`` (the module
    docstring). ``kind`` is the kind of a browser job the request creates; ``asr_key`` the
    caller's ``X-ASR-Api-Key``, stored with an ASR job only; ``llm_key`` the caller's
    ``X-LLM-Api-Key``, stored with every job of a request that may need the LLM. ``state`` is
    the public app's state: the core of ``POST /v1/extract`` and the routes that share its flow
    (ruling P10-S1).

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), then one ``check_rate`` hit."""
    settings: Settings = state.settings
    # Checked now, so that a refused request is a 4xx and never a job.
    compliance = resolve_mode(body.compliance, principal, settings)
    check_browser_request(body, principal, settings)
    check_selector_access(body.structured, principal)
    check_ocr_request(body.ocr, settings)
    check_transcribe_request(body.transcribe, settings, key=asr_key)
    check_pii_request(body.pii, body.format, body.screenshot, settings)
    check_structured_request(
        body.structured, body.chunking, body.pii, body.format, settings, llm_key=llm_key
    )
    check_describe_request(body.describe_images, body.pii, body.metadata, settings, llm_key=llm_key)
    if renders_first(body) and not _plain_url(body.url):
        # An invalid URL fails at once with `invalid_url`, as in fast mode: no job, no wait for
        # a browser worker (the fast pipeline refuses it before any fetch).
        pipeline: Pipeline = state.pipeline
        return await pipeline.run(body, principal)
    wait_seconds = request_wait_seconds(body, settings)
    handover = Handover()
    wait_until = handover_time(wait_seconds, settings)
    # Only a request that may need the LLM keeps the caller's key with its jobs.
    llm = wants_llm(body.structured, body.chunking, settings, describe=body.describe_images)
    job_llm_key = llm_key if llm else None
    flow = partial(
        _flow,
        state,
        body,
        principal,
        compliance,
        handover,
        kind,
        wait_until,
        asr_key=asr_key,
        llm_key=job_llm_key,
    )
    options = job_options(body, compliance)
    return await answer_or_hand_over(
        state,
        flow,
        handover,
        wait_seconds=wait_seconds,
        kind=kind,
        renders_first=renders_first(body),
        hand_over=partial(
            create_p1_job,
            state.job_store,
            settings,
            principal,
            options,
            body.url,
            llm_key=job_llm_key,
        ),
        pii=body.pii,
        structured=body.structured,
        chunking=body.chunking,
        describe=body.describe_images,
    )


def _plain_url(url: str) -> bool:
    try:
        validate_url(url)
    except InvalidUrlError:
        return False
    return True


def handover_time(wait_seconds: float | None, settings: Settings) -> float | None:
    """When (``time.monotonic()``) the route of a request with ``wait_seconds`` turns it into a
    job; None when it never does."""
    if wait_seconds is None or not settings.jobs_enabled:
        return None
    return time.monotonic() + wait_seconds


async def wait_for_flow(task: asyncio.Task[Any], seconds: float) -> None:
    """Wait ``seconds`` for the flow's task, without cancelling it."""
    await asyncio.wait({task}, timeout=seconds)


async def _wait_unless_shut_down(
    shutdown: GracefulShutdown, task: asyncio.Task[Any], handover: Handover, seconds: float
) -> None:
    """``wait_for_flow``, cut short once the pod's graceful shutdown has begun while the flow
    waits for a last-hop pool (``nlp``, ``llm``; issue #19): the route then answers as at the
    end of its wait, 202 with that job, instead of being cut off by the shutdown."""

    async def last_hop_at_shutdown() -> None:
        await shutdown.wait()
        await handover.in_last_hop()

    waiting = asyncio.ensure_future(wait_for_flow(task, seconds))
    cut = asyncio.ensure_future(last_hop_at_shutdown())
    try:
        await asyncio.wait({waiting, cut}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiting.cancel()
        cut.cancel()


async def answer_or_hand_over(
    state: Any,
    flow: Callable[[], Coroutine[Any, Any, _Answer]],
    handover: Handover,
    *,
    wait_seconds: float | None,
    kind: JobKind,
    renders_first: bool,
    hand_over: Callable[[], Awaitable[str | None]],
    pii: PiiOptions | None = None,
    structured: StructuredOptions | None = None,
    chunking: ChunkingOptions | None = None,
    describe: DescribeImagesOptions | None = None,
) -> Envelope[ExtractResult] | JSONResponse:
    """Run ``flow``, or answer 202 with a job after ``wait_seconds`` (the module docstring):
    the pool job the flow dispatched (of ``kind``), or a P1 job that ``hand_over`` creates (of
    kind ``file`` for an upload, else ``extract``). ``renders_first`` says that the flow goes to
    the browser at once. Every envelope answered or stored in the P1 job passes
    ``pii.fields.fail_closed`` with the request's ``pii`` (N12); one stored in the P1 job gets
    the post stage of the request's ``structured``, ``chunking`` and ``describe_images``
    (``describe``) first, if it lacks it (S13). Shared with ``POST /v1/extract/file``
    (``file_api.py``)."""
    settings: Settings = state.settings
    if wait_seconds is None or not settings.jobs_enabled:
        return await _answer(state, await flow(), handover, kind, pii)

    background: _Background = state.background
    task = asyncio.create_task(flow())
    background.add(task)
    try:
        if wait_seconds > 0:
            await _wait_unless_shut_down(state.graceful_shutdown, task, handover, wait_seconds)
    except asyncio.CancelledError:
        task.cancel()  # nobody is waiting for it any more, and no job exists
        background.discard(task)
        raise
    if task.done():
        background.discard(task)
        return await _answer(state, task.result(), handover, kind, pii)
    if renders_first or handover.claimed == "pool":
        return await _detach(state, task, handover, kind, pii)

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
            answer = await task
        finally:
            background.discard(task)
        return await _answer(state, answer, handover, kind, pii)
    p1_kind: JobKind = "file" if kind == "file" else "extract"
    metrics: JobMetrics = state.job_metrics
    metrics.created.labels(kind=p1_kind).inc()
    task.add_done_callback(
        partial(
            _store_when_done,
            background,
            store,
            metrics,
            job_id,
            handed_over_at,
            kind=p1_kind,
            pii=pii,
            structured=structured,
            chunking=chunking,
            describe=describe,
            settings=settings,
            threads=state.post_pool,
            registry=state.registry,
        )
    )
    _log.info("extract_job_accepted", job_id=job_id, kind=p1_kind)
    return _accepted(job_id, p1_kind, "running")


def _answered(answer: _Answer) -> Envelope[ExtractResult]:
    if answer is None:  # pragma: no cover - only a flow that handed over answers None
        raise RuntimeError("the flow answered nothing without a hand-over")
    return answer


async def _answer(
    state: Any,
    answer: _Answer,
    handover: Handover,
    kind: JobKind,
    pii: PiiOptions | None = None,
) -> Envelope[ExtractResult] | JSONResponse:
    """The flow's envelope, past the PII guard; for a flow that answered None after leaving a
    pool job to the route (``Handover.adopt``: an ASR job), 202 with that job."""
    if answer is None and handover.detached and handover.pool_job is not None:
        return await _pool_job_accepted(state, handover, kind)
    return fail_closed(_answered(answer), pii)


async def _pool_job_accepted(state: Any, handover: Handover, kind: JobKind) -> JSONResponse:
    """202 with the pool job the flow handed over, counted as a job of ``kind``."""
    job_id = handover.pool_job
    if job_id is None:  # pragma: no cover - only called with a pool job
        raise RuntimeError("no pool job to answer with")
    metrics: JobMetrics = state.job_metrics
    metrics.created.labels(kind=kind).inc()
    _log.info(f"{handover.pool}_job_accepted", job_id=job_id, kind=kind)
    return _accepted(job_id, kind, await _job_state(state.job_store, job_id))


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
    state: Any,
    task: asyncio.Task[_Answer],
    handover: Handover,
    kind: JobKind,
    pii: PiiOptions | None = None,
) -> Envelope[ExtractResult] | JSONResponse:
    """``wait_seconds`` ran out while the flow dispatches or waits for a pool job (a browser, a
    Docling, an nlp or an llm job): answer 202 with that job and let the flow stop waiting. If the
    dispatch failed, wait for the flow's own answer."""
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
            answer = await task
        finally:
            background.discard(task)
        return await _answer(state, answer, handover, kind, pii)
    task.add_done_callback(partial(_forget, background))
    return await _pool_job_accepted(state, handover, kind)


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
    *,
    asr_key: str | None = None,
    llm_key: str | None = None,
) -> _Answer:
    """The envelope of the request, or None when its job's result is written by a pool's
    worker (after a hand-over); see the module docstring. ``wait_until`` is when the route
    turns the request into a job (``handover_time``); ``asr_key`` the caller's key of the
    external transcription endpoint, ``llm_key`` its LLM key (None unless the request may need
    the LLM), kept with every job the flow creates. With ``pii`` the envelope goes through the
    nlp workers (step 7), then it gets the post stage (step 8)."""
    started = time.perf_counter()
    answer = await _extraction(
        state,
        body,
        principal,
        compliance,
        handover,
        kind,
        wait_until,
        started,
        asr_key=asr_key,
        llm_key=llm_key,
    )
    if answer is None:
        return None
    if body.pii is not None:
        handoff = pii_handoff(
            state,
            kind="extract",
            options=job_options(body, compliance),
            principal=principal,
            url=body.url,
            wait_until=wait_until,
            llm_key=llm_key,
        )
        # Without PII on this pod the route's guard withholds the text.
        if handoff is not None:
            processed = await handoff(answer, handover)
            if processed is None:
                return None
            answer = timed(processed, started)
    return await post_stage(
        state,
        answer,
        started,
        handover,
        kind="extract",
        options=job_options(body, compliance),
        principal=principal,
        url=body.url,
        wait_until=wait_until,
        llm_key=llm_key,
    )


async def post_stage(
    state: Any,
    envelope: Envelope[ExtractResult],
    started: float,
    handover: Handover,
    *,
    kind: Literal["extract", "file"],
    options: JobOptions,
    principal: Principal,
    url: str | None,
    wait_until: float | None,
    llm_key: str | None,
    upload: UploadInput | None = None,
) -> _Answer:
    """Step 8 of a sync flow (``_flow``, ``file_api._flow``): the post stage of the request's
    ``structured``, ``chunking`` and ``describe_images`` (``options``) for an envelope that
    still owes it (``structured.post.post_at_finish``), on the app's post pool (rulings R10,
    R21), timed into ``total_ms``. An envelope that needs the LLM goes to the llm workers
    (``llm_dispatch.LlmHandoff``, S13) with the caller's ``llm_key`` (and ``upload``, an image
    upload's bytes for its description, V-S7): the answer is their result, the content without
    the LLM work (``llm_failed``, retryable) when they could not take it, or None once the llm
    job is the route's or the P1 job's. Any other envelope comes back as it is."""
    handoff = llm_handoff(
        state,
        kind=kind,
        options=options,
        principal=principal,
        url=url,
        wait_until=wait_until,
        llm_key=llm_key,
        upload=upload,
    )

    finished = await post_at_finish(
        envelope,
        options.structured,
        options.chunking,
        options.pii,
        settings=state.settings,
        threads=state.post_pool,
        hop=_llm_hop(handoff, handover) if handoff is not None else None,
        describe=options.describe_images,
        registry=state.registry,
    )
    if finished is None or finished is envelope:
        return finished
    return timed(finished, started)


def _llm_hop(handoff: LlmHandoff, handover: Handover) -> LlmHop:
    """The handoff as the hop of ``post_at_finish``: its answer is finished already (the llm
    worker's result, or ``finish_post`` with the reason), so no reason goes back."""

    async def hop(draft: Envelope[ExtractResult]) -> tuple[Envelope[ExtractResult] | None, None]:
        return await handoff(draft, handover), None

    return hop


async def _extraction(
    state: Any,
    body: ExtractRequest,
    principal: Principal,
    compliance: ComplianceMode,
    handover: Handover,
    kind: BrowserJobKind,
    wait_until: float | None,
    started: float,
    *,
    asr_key: str | None = None,
    llm_key: str | None = None,
) -> _Answer:
    """Steps 2 to 6 of the flow: the request's envelope before the PII step, or None after a
    hand-over."""
    if renders_first(body):
        return await _in_browser(
            state, body, principal, compliance, handover, kind, None, started, llm_key=llm_key
        )
    pipeline: Pipeline = state.pipeline
    options = job_options(body, compliance)
    ocr = docling_handoff(
        state,
        handover,
        kind="extract",
        options=options,
        principal=principal,
        url=body.url,
        wait_until=wait_until,
        llm_key=llm_key,
    )
    asr = (
        asr_handoff(
            state,
            handover,
            kind="extract",
            options=options,
            principal=principal,
            url=body.url,
            key=asr_key,
            llm_key=llm_key,
        )
        if body.transcribe
        else None
    )
    handoffs: dict[str, Any] = {}
    if ocr is not None:
        handoffs["ocr_handoff"] = ocr
    if asr is not None:
        handoffs["asr_handoff"] = asr
    # Without a handoff, the call of package 3.
    fast = await pipeline.run(body, principal, **handoffs)
    if (ocr is not None and ocr.handed_over) or (asr is not None and asr.handed_over):
        return None  # the Docling or ASR worker writes the job's result
    settings: Settings = state.settings
    unblock = unblock_for(principal, compliance, settings)
    escalates = needs_browser(fast, unblock=unblock) or transcript_panel_applies(
        fast, settings, transcribe=body.transcribe
    )
    if body.mode != "auto" or not settings.browser_enabled or not escalates:
        return fast
    return await _in_browser(
        state, body, principal, compliance, handover, kind, fast, started, llm_key=llm_key
    )


def pii_handoff(
    state: Any,
    *,
    kind: Literal["extract", "file"],
    options: JobOptions,
    principal: Principal,
    url: str | None,
    wait_until: float | None,
    llm_key: str | None = None,
) -> PiiHandoff | None:
    """The PII handoff of one request (N11); None while the service has no PII."""
    dispatcher: PiiDispatcher | None = state.pii_dispatcher
    if dispatcher is None:
        return None
    return PiiHandoff(
        dispatcher,
        state.memory_guard,
        kind=kind,
        options=options,
        principal=principal,
        url=url,
        wait_until=wait_until,
        llm_key=llm_key,
    )


def llm_handoff(
    state: Any,
    *,
    kind: Literal["extract", "file"],
    options: JobOptions,
    principal: Principal,
    url: str | None,
    wait_until: float | None,
    llm_key: str | None = None,
    upload: UploadInput | None = None,
) -> LlmHandoff | None:
    """The LLM hop of one request (S13); None while the service has no LLM. ``upload`` is an
    upload's bytes of a request with ``describe_images`` (V-S7)."""
    dispatcher: LlmDispatcher | None = state.llm_dispatcher
    if dispatcher is None:
        return None
    return LlmHandoff(
        dispatcher,
        state.memory_guard,
        kind=kind,
        options=options,
        principal=principal,
        url=url,
        threads=state.post_pool,
        wait_until=wait_until,
        llm_key=llm_key,
        upload=upload,
    )


def docling_handoff(
    state: Any,
    handover: Handover,
    *,
    kind: Literal["extract", "file"],
    options: JobOptions,
    principal: Principal,
    url: str | None,
    wait_until: float | None,
    llm_key: str | None = None,
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
        llm_key=llm_key,
    )


def asr_handoff(
    state: Any,
    handover: Handover,
    *,
    kind: Literal["extract", "file"],
    options: JobOptions,
    principal: Principal,
    url: str | None,
    body: bytes = b"",
    key: str | None = None,
    llm_key: str | None = None,
) -> AsrHandoff | None:
    """The ASR handoff of one request (M9); None while the service has no ASR."""
    dispatcher: AsrDispatcher | None = state.asr_dispatcher
    if dispatcher is None:
        return None
    return AsrHandoff(
        dispatcher,
        state.memory_guard,
        handover,
        kind=kind,
        options=options,
        principal=principal,
        url=url,
        body=body,
        key=key,
        llm_key=llm_key,
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
    *,
    llm_key: str | None = None,
) -> _Answer:
    """Dispatch the request to the browser workers and wait for their result. ``draft`` is the
    fast result of an escalating ``auto`` request (None when the request renders first);
    ``llm_key`` the caller's LLM key, kept with a new browser job."""
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
        job_id = await dispatcher.submit(
            kind, body, principal, compliance, draft=draft, llm_key=llm_key
        )
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
        result = await dispatcher.wait(
            job_id,
            unblock=unblock,
            detached=lambda: handover.detached,
            pii=body.pii is not None,
            llm=wants_llm(
                body.structured, body.chunking, state.settings, describe=body.describe_images
            ),
        )
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
    return timed(result, started)


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


def timed(envelope: Envelope[ExtractResult], started: float) -> Envelope[ExtractResult]:
    """The envelope with this request's trace id and its whole time in ``timings.total_ms``:
    the fast phase, the wait for a worker, the render and the PII hop."""
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
        return timed(draft.model_copy(update={"data": data}), started)
    if draft is not None:  # pragma: no cover - needs_browser only passes results with data
        return timed(draft, started)
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
    return timed(Envelope(status="failed", data=result, error=error), started)


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
    llm_key: str | None = None,
) -> str | None:
    """Create the P1 job (of ``kind``; an upload's with its bytes as the input of item 0, for
    ``DOCLING_INPUT_TTL_SECONDS``, within ``DOCLING_INPUT_BUDGET_BYTES``; with the caller's
    ``llm_key`` for the llm worker, when the request may need the LLM) and mark it running;
    its id, or None when Valkey fails or the input budget is full (logged): the request then
    stays synchronous. A job that was created but could not be completed, or whose creator is
    cancelled, is deleted again, if that works."""
    job_id: str | None = None
    try:
        job_id = await store.create(kind, principal, options, [url], trace_id=current_trace_id())
        if upload is not None:
            body, meta = upload
            await store.put_input(job_id, 0, body, meta, settings.docling_input_ttl_seconds)
        if llm_key is not None:
            await store.put_llm_key(job_id, llm_key)
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
    pii: PiiOptions | None = None,
    structured: StructuredOptions | None = None,
    chunking: ChunkingOptions | None = None,
    describe: DescribeImagesOptions | None = None,
    settings: Settings | None = None,
    threads: ConversionPool | None = None,
    registry: CollectorRegistry | None = None,
) -> None:
    """Done-callback of a handed-over extraction: store its envelope in the job, unless the job
    went on to a pool's worker, which writes it. The storing task is added to ``background``
    before the extraction leaves it, so a shutdown that waits for the set never misses the
    write."""
    envelope = _envelope_of(task)
    if envelope is not None:
        storing = asyncio.create_task(
            _store_result(
                store,
                metrics,
                job_id,
                created_at,
                envelope,
                kind=kind,
                pii=pii,
                structured=structured,
                chunking=chunking,
                describe=describe,
                settings=settings,
                threads=threads,
                registry=registry,
            )
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
    pii: PiiOptions | None = None,
    structured: StructuredOptions | None = None,
    chunking: ChunkingOptions | None = None,
    describe: DescribeImagesOptions | None = None,
    settings: Settings | None = None,
    threads: ConversionPool | None = None,
    registry: CollectorRegistry | None = None,
) -> None:
    """Store the envelope as item 0, after the post stage of the request's ``structured``,
    ``chunking`` and ``describe_images`` (``describe``; with ``settings`` and ``threads``, the post
    pool, and ``registry``, the app's, for the images' counter; for an envelope that lacks it:
    the flow's own answer has it, S13) and past the PII guard
    (``pii.fields.fail_closed`` with the request's ``pii``, N12), and observe the job's duration
    (``created_at`` is a ``time.monotonic()`` reading from just before the job was created); an
    upload's input is deleted then, and so is the caller's LLM key. A Valkey failure is logged and
    not raised: the job's lease then runs out and ``GET /v1/jobs/{id}`` reports the item as lost (an
    input expires on its own)."""
    if settings is not None:
        if threads is None:  # a caller's mistake: the guard would be skipped without a word
            raise TypeError("_store_result needs threads together with settings")
        # The guard: LLM work an envelope still owes here (a task that raised, a pod without
        # the hop) is ``llm_failed``; the flow's own answer has had its post stage.
        finished = await post_at_finish(
            envelope,
            structured,
            chunking,
            pii,
            settings=settings,
            threads=threads,
            describe=describe,
            registry=registry,
        )
        if finished is not None:  # always, without a hop
            envelope = finished
    envelope = fail_closed(envelope, pii)
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
    # The pod ran the whole request: no llm worker will read the caller's LLM key any more.
    with contextlib.suppress(RedisError, OSError):  # else it expires with the job
        await store.drop_llm_key(job_id)
