"""``POST /v1/extract/file``: extract an uploaded file (ruling F17).

- The body is ``multipart/form-data`` with one file part ``file`` and the fields of
  ``FileForm``. Its limit is ``upload_file_limit`` + 192 KiB (``upload_body_limit``, applied by
  ``BodyLimitMiddleware``): a larger ``Content-Length`` is 413 before anything is read, and a
  body without one is refused at the chunk that goes over the limit. ``upload_file_limit`` is
  ``DOCUMENT_MAX_BYTES``, or the larger ``MEDIA_UPLOAD_MAX_BYTES`` while uploaded audio and video
  are transcribed (``MEDIA_ENABLED`` and the service has ASR); with ASR off the setting does not
  widen the path. A document over ``DOCUMENT_MAX_BYTES`` still answers ``too_large``.
- The form is parsed with at most ``MAX_FILES`` file and ``MAX_FIELDS`` fields of at most
  ``MAX_FIELD_BYTES`` (1 KiB) each, but ``structured`` and ``chunking`` (``OPTION_FIELDS``), whose
  JSON may describe a schema of 50 properties with its rules, of at most
  ``MAX_OPTION_FIELD_BYTES`` (64 KiB) each (ruling R18); more, or a malformed body, is 422
  ``invalid_request``. The body limit leaves room for a file at its limit and every field at
  its own.
- The file part is read into memory up to ``upload_file_limit`` + 1 bytes and closed at once,
  which deletes its spooled file; the pipeline answers a longer one with ``too_large``.
- The part's file name is reduced to its base name (``sanitize_file_name``) and reported as
  ``document.file_name`` only: it is never logged.
- The usual auth (role ``user``) and inbound rate limit apply, before a byte of the body is read:
  a caller without a key, or over its rate limit, gets its 401 or 429 (the same answers as on
  every route) without the pod receiving or spooling the upload. ``ocr: always`` is 400 while
  the service has no OCR, as for ``/v1/extract``.
- A part whose bytes are audio or video (``sniff_media``; no form field is needed) is for
  transcription (M14, ``Pipeline.extract_upload``): ``unsupported_content_type`` while the
  service has no ASR, ``too_large`` over ``MEDIA_UPLOAD_MAX_BYTES``, otherwise it goes to the
  ASR workers (``asr_dispatch.AsrHandoff``, built here because the bytes are in hand): a job of
  kind ``file`` on pool ``asr`` whose item keeps the bytes as its input
  (``ASR_INPUT_TTL_SECONDS``) and the ``X-ASR-Api-Key`` header as its secret, answered 202 at
  once (the form's ``wait_seconds`` does not apply). Where the service transcribes on an
  external endpoint without a key of its own (``ASR_EXTERNAL_API_KEY``), such a file without
  the header is 400 (M11), checked as soon as the bytes are read. Any other file over
  ``DOCUMENT_MAX_BYTES`` is ``too_large`` before its bytes are sniffed or hashed.

The conversion is ``Pipeline.extract_upload``: no compliance check, no result cache. With
``wait_seconds`` (25 by default) a slower upload becomes a job of kind ``file``, as a slow
``POST /v1/extract`` does (``extract_api.answer_or_hand_over``, ruling P1): the conversion keeps
running in this pod, the job keeps the upload's bytes as the input of its item
(``DOCLING_INPUT_TTL_SECONDS``, deleted once the result is stored), and the answer is 202. An
upload that needs OCR goes to the Docling workers (``docling_dispatch.DoclingHandoff``, F12): it
waits ``DOCLING_SYNC_WAIT_SECONDS`` without ``wait_seconds``, and with it the 202 carries its
Docling job. The job's item has no URL (``url: null``).

The form field ``pii`` (a mode, or the JSON object of ``POST /v1/extract`` as a string) sends the
envelope through the nlp workers last (``pii_dispatch.PiiHandoff``, N11), as for
``POST /v1/extract``: 400 while the service has no PII handling, 422 for ``annotate`` with
``format: html``; a slow nlp worker turns the upload into a job (202) after ``wait_seconds``. The
answer, and what a P1 job stores, passes ``pii.fields.fail_closed`` (N12).

The form fields ``structured`` and ``chunking`` (JSON strings) get the post stage after that
(``extract_api.post_stage``, ruling S13): on the final, possibly masked content, in the API pod
unless a Docling, nlp or llm worker ran it, before the PII guard; LLM work goes to the llm
workers (``llm_dispatch.LlmHandoff``), and a slow llm worker turns the upload into a job (202)
after ``wait_seconds`` (``LLM_SYNC_WAIT_SECONDS`` by default). A P1 job's write runs it too.
"""

import time
import unicodedata
from collections.abc import Callable, Coroutine
from functools import partial
from http import HTTPStatus
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from text_extraction.asr_dispatch import ASR_KEY_HEADER, asr_key_of
from text_extraction.auth import Principal, authenticate, require_role
from text_extraction.compliance import resolve_mode
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_api import (
    ASR_KEY_DESCRIPTION,
    LLM_KEY_DESCRIPTION,
    answer_or_hand_over,
    asr_handoff,
    check_asr_key,
    check_ocr_request,
    check_pii_request,
    create_p1_job,
    default_wait_seconds,
    docling_handoff,
    handover_time,
    pii_handoff,
    post_stage,
    timed,
)
from text_extraction.extract_models import ExtractResult
from text_extraction.file_models import FILE_PART, FileForm, UploadInputMeta, upload_options
from text_extraction.job_models import JobAccepted, JobOptions
from text_extraction.llm_dispatch import UploadInput
from text_extraction.media.sniff import MEDIA_HEAD_BYTES, sniff_media
from text_extraction.pipeline import Pipeline
from text_extraction.pipeline_media import asr_available
from text_extraction.pool_dispatch import Handover
from text_extraction.ratelimit import rate_limited
from text_extraction.settings import Settings
from text_extraction.structured.checks import (
    LLM_KEY_HEADER,
    check_selector_access,
    check_structured_request,
    llm_key_of,
    wants_llm,
)
from text_extraction.vision.checks import check_describe_request

PATH: Final = "/v1/extract/file"
MAX_FILES: Final = 1
MAX_FIELDS: Final = 8
MAX_FIELD_BYTES: Final = 1024
# `structured` and `chunking` carry JSON options: a schema of up to 50 properties with its
# selector and pattern rules does not fit 1 KiB (ruling R18).
OPTION_FIELDS: Final = frozenset({"structured", "chunking"})
MAX_OPTION_FIELD_BYTES: Final = 64 * 1024
MAX_FILE_NAME_CHARS: Final = 255
# What the body may have beyond the file: the part headers, the small fields and the boundaries,
# plus the option fields at their limit.
_FORM_OVERHEAD_BYTES: Final = 64 * 1024 + len(OPTION_FIELDS) * MAX_OPTION_FIELD_BYTES
# The body limit of `POST /v1/chunk`'s JSON (a text as an upload): the text plus 64 KiB.
_TEXT_OVERHEAD_BYTES: Final = 64 * 1024


def upload_file_limit(settings: Settings) -> int:
    """The most bytes of the file part that are read: ``DOCUMENT_MAX_BYTES``, or
    ``MEDIA_UPLOAD_MAX_BYTES`` where that is larger and uploaded audio and video are transcribed
    (``MEDIA_ENABLED`` and the service has ASR; M14). Otherwise the setting does not apply: with
    ASR off it is not even bounded by the input budget, and the upload would only be refused."""
    if settings.media_enabled and asr_available(settings):
        return max(settings.document_max_bytes, settings.media_upload_max_bytes)
    return settings.document_max_bytes


def upload_body_limit(settings: Settings) -> int:
    """The body limit of the upload route: ``upload_file_limit`` + 192 KiB (64 KiB for the part
    headers, the small fields and the boundaries, and 64 KiB for each option field)."""
    return upload_file_limit(settings) + _FORM_OVERHEAD_BYTES


def text_body_limit(settings: Settings) -> int:
    """The body limit of ``POST /v1/chunk`` (a JSON body whose text is chunked as an upload):
    ``upload_file_limit`` + 64 KiB."""
    return upload_file_limit(settings) + _TEXT_OVERHEAD_BYTES


def sanitize_file_name(name: str | None) -> str | None:
    """The part's file name as a result may show it: control and format characters removed
    (also a bidi override), the base name after the last ``/`` or ``\\``, surrounding
    whitespace stripped, at most 255 characters. None for no name, or one that is empty, ``.``
    or ``..`` after that."""
    if not name:
        return None
    visible = "".join(char for char in name if not unicodedata.category(char).startswith("C"))
    base = visible.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if base in ("", ".", ".."):
        return None
    return base[:MAX_FILE_NAME_CHARS]


class AuthFirstRoute(APIRoute):
    """Authenticates the caller and counts the request against its rate limit before FastAPI
    reads the body (it reads and parses a body before it runs any dependency): a caller without
    a key, or over its rate limit, gets its 401 or 429 without the pod receiving a byte of the
    body. Both checks read headers only. The route counts the request itself: it takes no
    ``rate_limited`` dependency. ``read_body`` is the hook for what else is done with the body's
    start before the handler; the body limit of the path (``BodyLimitMiddleware``) applies to
    whatever is read. Used by the upload route and by ``POST /v1/chunk``."""

    async def read_body(self, request: Request) -> None:
        """Called after the checks, before FastAPI reads the body; the default does nothing."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def limited(request: Request) -> Response:
            principal = await require_role("user")(await authenticate(request))
            await rate_limited(request, principal)  # the route's only count of this request
            await self.read_body(request)
            return await handler(request)

        return limited


class _UploadRoute(AuthFirstRoute):
    """The upload route: after the checks the form is parsed with the upload's limits (FastAPI's
    own parse allows 1,000 files and fields). The parsed form is kept on the request, so FastAPI
    uses it, and closes it when the answer is sent."""

    async def read_body(self, request: Request) -> None:
        try:
            # Starlette has one part limit for every field: the larger one of the option fields;
            # the other fields are held to MAX_FIELD_BYTES below.
            form = await request.form(
                max_files=MAX_FILES, max_fields=MAX_FIELDS, max_part_size=MAX_OPTION_FIELD_BYTES
            )
        except StarletteHTTPException as error:
            # Starlette refuses a form over the limits, or a malformed one, with a 400 whose
            # detail names the limit, never the content. A body over its limit (413) passes.
            if error.status_code != HTTPStatus.BAD_REQUEST:
                raise
            raise ServiceError(
                ErrorCode.INVALID_REQUEST, error.detail, HTTPStatus.UNPROCESSABLE_ENTITY
            ) from None
        for name, value in form.multi_items():
            if (
                isinstance(value, str)
                and name not in OPTION_FIELDS
                and len(value.encode()) > MAX_FIELD_BYTES
            ):
                await form.close()  # its file part's spooled file
                raise ServiceError(
                    ErrorCode.INVALID_REQUEST,
                    f"Part exceeded maximum size of {MAX_FIELD_BYTES // 1024}KB.",
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )


router = APIRouter(route_class=_UploadRoute)


@router.post(
    PATH,
    response_model=Envelope[ExtractResult],
    responses={
        202: {
            "model": Envelope[JobAccepted],
            "description": "The upload takes longer than `wait_seconds` (25 by default) and "
            "became a job of kind `file`.",
        }
    },
)
async def extract_file(
    request: Request,
    file: Annotated[
        UploadFile,
        File(
            alias=FILE_PART,
            description="The file: PDF, DOCX, PPTX, XLSX, XLS, EPUB, CSV, HTML or plain text; "
            "OpenDocument files and images where the service has OCR; audio and video files "
            "where the service has speech recognition (they are transcribed as a job). At most "
            "`DOCUMENT_MAX_BYTES` (`MEDIA_UPLOAD_MAX_BYTES` for audio and video).",
            json_schema_extra={"format": "binary"},
        ),
    ],
    form: Annotated[FileForm, Form()],
    principal: Annotated[Principal, Depends(require_role("user"))],
    asr_key: Annotated[
        str | None, Header(alias=ASR_KEY_HEADER, description=ASR_KEY_DESCRIPTION)
    ] = None,
    llm_key: Annotated[
        str | None, Header(alias=LLM_KEY_HEADER, description=LLM_KEY_DESCRIPTION)
    ] = None,
) -> Envelope[ExtractResult] | Response:
    """Extract an uploaded file as Markdown, text or HTML.

    The answer is the result envelope of `POST /v1/extract`, with `mode_used: "upload"`,
    `url: null`, `final_url: null`, `cache: "bypass"` and `compliance.decision: "skipped"`; a
    document's `document.file_name` is the part's file name without its path. The kind is
    decided by the bytes; the part's content type decides only where they do not: `text/plain`
    is plain text, `text/markdown` and `text/x-markdown` are Markdown, and so is a text file
    named `*.md` or `*.markdown` sent as `application/octet-stream` or without a type (one named
    `*.txt` is plain text). A file that
    cannot be converted answers 200 with `status: failed` (`unsupported_content_type`,
    `too_large`, `conversion_failed`, ...). An audio or video file (decided by its bytes) is
    transcribed where the service has speech recognition, else `unsupported_content_type`
    ("Transcription is not enabled on this service."); over `MEDIA_UPLOAD_MAX_BYTES` it is
    `too_large`; otherwise it becomes a job at once (202, kind `file`), whose result has
    `media.platform: "upload"` (while the key has `ASR_MAX_OPEN_PER_CLIENT` transcriptions
    waiting, or the service is busy: `failed`/`rate_limited`, retryable). 422 for a form with
    more than one file or eight fields, a field over 1 KiB (`structured` and `chunking`: over
    64 KiB), or unknown fields; 413 for a body over
    `DOCUMENT_MAX_BYTES` + 192 KiB (the larger `MEDIA_UPLOAD_MAX_BYTES` + 192 KiB where audio and
    video are transcribed); 400 for `ocr: always` while the service has no OCR, `pii` while it
    has no PII handling, an `X-ASR-Api-Key` header that is not 1 to 1024 visible ASCII
    characters, or an audio or video file without that header where the service transcribes
    externally and has no key of its own; 422 for `pii` `annotate` with `format: html`.

    With `pii` the result's personal data is handled as for `POST /v1/extract` (`data.pii`); a
    result that could not be processed answers `failed`/`pii_failed` with its content withheld.

    `structured` and `chunking` (JSON strings) and the `X-LLM-Api-Key` header are checked as for
    `POST /v1/extract`: 400 without LLM, embedding model or key, 422 for the combinations that
    are refused there.

    An upload still converting after `wait_seconds` (25 by default) becomes a job: 202 with the
    job's id and `status_url` (`GET /v1/jobs/{id}`, kind `file`, its item's `url` null). A
    document that needs OCR waits up to `DOCLING_SYNC_WAIT_SECONDS` for it without
    `wait_seconds`, then answers its text layer with the warning `ocr_unavailable`.
    """
    settings: Settings = request.app.state.settings
    key, caller_llm_key = check_upload_request(
        form, principal, settings, asr_header=asr_key, llm_header=llm_key
    )
    try:
        body = await file.read(upload_file_limit(settings) + 1)
    finally:
        await file.close()  # deletes the spooled part now, not when the answer is sent
    if key is None and _transcribed(body, file.content_type, settings):
        check_asr_key(settings, key)  # 400 without any key for the external endpoint (M11)
    return await answer_upload(
        request,
        principal,
        form,
        body,
        sanitize_file_name(file.filename),
        file.content_type,
        asr_key=key,
        llm_key=caller_llm_key,
    )


def check_upload_request(
    form: FileForm,
    principal: Principal,
    settings: Settings,
    *,
    asr_header: str | None,
    llm_header: str | None,
) -> tuple[str | None, str | None]:
    """The checks of an upload that need no byte of the file, before it is read: ``ocr``,
    ``pii``, the selector access of the key, the ``X-ASR-Api-Key`` and ``X-LLM-Api-Key`` headers
    (``asr_header`` and ``llm_header``, as the route received them), ``structured`` and
    ``chunking`` (ruling S12) and ``describe_images`` (ruling V-S2). Returns the caller's ASR
    and LLM keys (None without the header). Shared with ``POST /v1/chunk``
    (``structured_api.py``)."""
    check_ocr_request(form.ocr, settings)
    check_pii_request(form.pii, form.format, False, settings)
    check_selector_access(form.structured, principal)
    key = asr_key_of(asr_header)
    caller_llm_key = llm_key_of(llm_header)
    check_structured_request(
        form.structured, form.chunking, form.pii, form.format, settings, llm_key=caller_llm_key
    )
    check_describe_request(
        form.describe_images, form.pii, form.metadata, settings, llm_key=caller_llm_key
    )
    return key, caller_llm_key


async def answer_upload(
    request: Request,
    principal: Principal,
    form: FileForm,
    body: bytes,
    name: str | None,
    content_type: str | None,
    *,
    asr_key: str | None,
    llm_key: str | None,
    short_ok: bool = False,
) -> Envelope[ExtractResult] | Response:
    """Convert the upload's ``body`` (``name`` is its sanitised file name, ``content_type`` the
    declared type) and answer, or hand it over to a job after ``form.wait_seconds`` (the module
    docstring). The request has passed ``check_upload_request``; ``asr_key`` and ``llm_key`` are
    its keys. ``short_ok`` is for a ``text`` of ``POST /v1/chunk`` (``structured_api.py``), which
    shares this flow: no word-count gate (``Pipeline.extract_upload``)."""
    state = request.app.state
    settings: Settings = state.settings
    # Only a request that may need the LLM keeps the caller's key with its jobs.
    llm = wants_llm(form.structured, form.chunking, settings, describe=form.describe_images)
    job_llm_key = llm_key if llm else None
    compliance = resolve_mode(None, principal, settings)
    options = upload_options(form, compliance)
    wait_seconds = form.wait_seconds
    if wait_seconds is None:  # D4 (a) and S13, as for ``POST /v1/extract``
        wait_seconds = default_wait_seconds(settings, pii=form.pii is not None, llm=llm)
    handover = Handover()
    flow = partial(
        _flow,
        state,
        body,
        name,
        form,
        principal,
        content_type,
        handover,
        options,
        handover_time(wait_seconds, settings),
        asr_key,
        job_llm_key,
        short_ok=short_ok,
    )
    meta = UploadInputMeta(file_name=name, content_type=content_type, short_ok=short_ok)
    return await answer_or_hand_over(
        state,
        flow,
        handover,
        wait_seconds=wait_seconds,
        kind="file",
        renders_first=False,
        hand_over=partial(
            create_p1_job,
            state.job_store,
            settings,
            principal,
            options,
            None,
            kind="file",
            upload=(body, meta),
            llm_key=job_llm_key,
        ),
        pii=form.pii,
        structured=form.structured,
        chunking=form.chunking,
        describe=form.describe_images,
    )


def _transcribed(body: bytes, content_type: str | None, settings: Settings) -> bool:
    """Whether the upload goes to the ASR workers: audio or video (the pipeline's sniff of its
    head) while the service transcribes uploads."""
    if not (settings.media_enabled and asr_available(settings)):
        return False
    return sniff_media(body[:MEDIA_HEAD_BYTES], content_type) is not None


async def _flow(
    state: Any,
    body: bytes,
    name: str | None,
    form: FileForm,
    principal: Principal,
    content_type: str | None,
    handover: Handover,
    options: JobOptions,
    wait_until: float | None,
    asr_key: str | None,
    llm_key: str | None = None,
    short_ok: bool = False,
) -> Envelope[ExtractResult] | None:
    """The upload's envelope, or None when the Docling, the ASR, the nlp or the llm worker writes
    its job's result. ``llm_key`` is the caller's LLM key (None unless the request may need the
    LLM), kept with every job the flow creates. With ``pii`` the envelope goes through the nlp
    workers (``pii_dispatch.PiiHandoff``), then it gets the post stage (``extract_api.post_stage``),
    whose LLM work goes to the llm workers (``llm_dispatch.LlmHandoff``); with
    ``describe_images`` the hop gets the upload's bytes, which a new llm job keeps as its input
    when they are an image the llm worker can describe (ruling V-S7)."""
    started = time.perf_counter()
    pipeline: Pipeline = state.pipeline
    ocr = docling_handoff(
        state,
        handover,
        kind="file",
        options=options,
        principal=principal,
        url=None,
        wait_until=wait_until,
        llm_key=llm_key,
    )
    asr = asr_handoff(
        state,
        handover,
        kind="file",
        options=options,
        principal=principal,
        url=None,
        body=body,
        key=asr_key,
        llm_key=llm_key,
    )
    envelope = await pipeline.extract_upload(
        body,
        name,
        form,
        principal,
        content_type=content_type,
        ocr_handoff=ocr,
        asr_handoff=asr,
        short_ok=short_ok,
    )
    if (ocr is not None and ocr.handed_over) or (asr is not None and asr.handed_over):
        return None
    pii = pii_handoff(
        state,
        kind="file",
        options=options,
        principal=principal,
        url=None,
        wait_until=wait_until,
        llm_key=llm_key,
    )
    # Without PII on this pod the route's guard withholds the text.
    if form.pii is not None and pii is not None:
        processed = await pii(envelope, handover)
        if processed is None:
            return None
        envelope = timed(processed, started)
    return await post_stage(
        state,
        envelope,
        started,
        handover,
        kind="file",
        options=options,
        principal=principal,
        url=None,
        wait_until=wait_until,
        llm_key=llm_key,
        upload=(
            UploadInput(body, UploadInputMeta(file_name=name, content_type=content_type))
            if form.describe_images is not None
            else None
        ),
    )
