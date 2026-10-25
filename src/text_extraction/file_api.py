"""``POST /v1/extract/file``: extract an uploaded file (ruling F17).

- The body is ``multipart/form-data`` with one file part ``file`` and the fields of
  ``FileForm``. Its limit is ``DOCUMENT_MAX_BYTES`` + 64 KiB (``upload_body_limit``, applied by
  ``BodyLimitMiddleware``): a larger ``Content-Length`` is 413 before anything is read, and a
  body without one is refused at the chunk that goes over the limit.
- The form is parsed with at most ``MAX_FILES`` file and ``MAX_FIELDS`` fields of at most
  ``MAX_FIELD_BYTES`` each; more, or a malformed body, is 422 ``invalid_request``.
- The file part is read into memory up to ``DOCUMENT_MAX_BYTES`` + 1 bytes and closed at once,
  which deletes its spooled file; the pipeline answers a longer one with ``too_large``.
- The part's file name is reduced to its base name (``sanitize_file_name``) and reported as
  ``document.file_name`` only: it is never logged.
- The usual auth (role ``user``) and inbound rate limit apply, before a byte of the body is read:
  a caller without a key, or over its rate limit, gets its 401 or 429 (the same answers as on
  every route) without the pod receiving or spooling the upload. ``ocr: always`` is 400 while
  the service has no OCR, as for ``/v1/extract``.

The conversion is ``Pipeline.extract_upload``: no compliance check, no result cache. With
``wait_seconds`` (25 by default) a slower upload becomes a job of kind ``file``, as a slow
``POST /v1/extract`` does (``extract_api.answer_or_hand_over``, ruling P1): the conversion keeps
running in this pod, the job keeps the upload's bytes as the input of its item
(``DOCLING_INPUT_TTL_SECONDS``, deleted once the result is stored), and the answer is 202. An
upload that needs OCR goes to the Docling workers (``docling_dispatch.DoclingHandoff``, F12): it
waits ``DOCLING_SYNC_WAIT_SECONDS`` without ``wait_seconds``, and with it the 202 carries its
Docling job. The job's item has no URL (``url: null``).
"""

import unicodedata
from collections.abc import Callable, Coroutine
from functools import partial
from http import HTTPStatus
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from text_extraction.auth import Principal, authenticate, require_role
from text_extraction.compliance import resolve_mode
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_api import (
    answer_or_hand_over,
    check_ocr_request,
    create_p1_job,
    docling_handoff,
    handover_time,
)
from text_extraction.extract_models import ExtractResult
from text_extraction.file_models import FILE_PART, FileForm, UploadInputMeta, upload_options
from text_extraction.job_models import JobAccepted, JobOptions
from text_extraction.pipeline import Pipeline
from text_extraction.pool_dispatch import Handover
from text_extraction.ratelimit import rate_limited
from text_extraction.settings import Settings

PATH: Final = "/v1/extract/file"
MAX_FILES: Final = 1
MAX_FIELDS: Final = 8
MAX_FIELD_BYTES: Final = 1024
MAX_FILE_NAME_CHARS: Final = 255
# What the body may have beyond the file: the part headers, the fields and the boundaries.
_FORM_OVERHEAD_BYTES: Final = 64 * 1024


def upload_body_limit(settings: Settings) -> int:
    """The body limit of the upload route: ``DOCUMENT_MAX_BYTES`` + 64 KiB."""
    return settings.document_max_bytes + _FORM_OVERHEAD_BYTES


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


class _UploadRoute(APIRoute):
    """Authenticates the caller and counts the request against its rate limit, then parses the
    form with the upload's limits, all before FastAPI reads the body (it reads the body before
    it runs any dependency, and its own parse allows 1,000 files and fields). Both checks read
    headers only. The parsed form is kept on the request, so FastAPI uses it, and closes it when
    the answer is sent."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def limited(request: Request) -> Response:
            principal = await require_role("user")(await authenticate(request))
            await rate_limited(request, principal)  # the route's only count of this request
            try:
                await request.form(
                    max_files=MAX_FILES, max_fields=MAX_FIELDS, max_part_size=MAX_FIELD_BYTES
                )
            except StarletteHTTPException as error:
                # Starlette refuses a form over the limits, or a malformed one, with a 400 whose
                # detail names the limit, never the content. A body over its limit (413) passes.
                if error.status_code != HTTPStatus.BAD_REQUEST:
                    raise
                raise ServiceError(
                    ErrorCode.INVALID_REQUEST, error.detail, HTTPStatus.UNPROCESSABLE_ENTITY
                ) from None
            return await handler(request)

        return limited


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
            "OpenDocument files and images where the service has OCR. At most "
            "`DOCUMENT_MAX_BYTES`.",
            json_schema_extra={"format": "binary"},
        ),
    ],
    form: Annotated[FileForm, Form()],
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[ExtractResult] | Response:
    """Extract an uploaded file as Markdown, text or HTML.

    The answer is the result envelope of `POST /v1/extract`, with `mode_used: "upload"`,
    `url: null`, `final_url: null`, `cache: "bypass"` and `compliance.decision: "skipped"`; a
    document's `document.file_name` is the part's file name without its path. The kind is
    decided by the bytes; the part's content type decides only where they do not. A file that
    cannot be converted answers 200 with `status: failed` (`unsupported_content_type`,
    `too_large`, `conversion_failed`, ...). 422 for a form with more than one file or eight
    fields, or unknown fields; 413 for a body over `DOCUMENT_MAX_BYTES` + 64 KiB; 400 for
    `ocr: always` while the service has no OCR.

    An upload still converting after `wait_seconds` (25 by default) becomes a job: 202 with the
    job's id and `status_url` (`GET /v1/jobs/{id}`, kind `file`, its item's `url` null). A
    document that needs OCR waits up to `DOCLING_SYNC_WAIT_SECONDS` for it without
    `wait_seconds`, then answers its text layer with the warning `ocr_unavailable`.
    """
    state = request.app.state
    settings: Settings = state.settings
    check_ocr_request(form.ocr, settings)
    try:
        body = await file.read(settings.document_max_bytes + 1)
    finally:
        await file.close()  # deletes the spooled part now, not when the answer is sent
    name = sanitize_file_name(file.filename)
    compliance = resolve_mode(None, principal, settings)
    options = upload_options(form, compliance)
    handover = Handover()
    flow = partial(
        _flow,
        state,
        body,
        name,
        form,
        principal,
        file.content_type,
        handover,
        options,
        handover_time(form.wait_seconds, settings),
    )
    meta = UploadInputMeta(file_name=name, content_type=file.content_type)
    return await answer_or_hand_over(
        state,
        flow,
        handover,
        wait_seconds=form.wait_seconds,
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
        ),
    )


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
) -> Envelope[ExtractResult] | None:
    """The upload's envelope, or None when the Docling worker writes its job's result."""
    pipeline: Pipeline = state.pipeline
    ocr = docling_handoff(
        state,
        handover,
        kind="file",
        options=options,
        principal=principal,
        url=None,
        wait_until=wait_until,
    )
    envelope = await pipeline.extract_upload(
        body, name, form, principal, content_type=content_type, ocr_handoff=ocr
    )
    return None if ocr is not None and ocr.handed_over else envelope
