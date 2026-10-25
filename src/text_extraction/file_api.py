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

The conversion is ``Pipeline.extract_upload``: no compliance check, no result cache. The upload
answers synchronously for now: its conversion in this pod ends within
``EXTRACT_TIMEOUT_SECONDS``. Handing a slow upload over to a job (``wait_seconds``, ruling P1) and
an OCR upload to its Docling job (F12) need the upload's bytes stored with the job, which the
Docling dispatch adds.
"""

import unicodedata
from collections.abc import Callable, Coroutine
from http import HTTPStatus
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from text_extraction.auth import Principal, authenticate, require_role
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_api import check_ocr_request
from text_extraction.extract_models import ExtractResult
from text_extraction.file_models import FILE_PART, FileForm
from text_extraction.job_models import JobAccepted
from text_extraction.pipeline import Pipeline
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
            "description": "The upload takes longer than `wait_seconds` and became a job.",
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
) -> Envelope[ExtractResult]:
    """Extract an uploaded file as Markdown, text or HTML.

    The answer is the result envelope of `POST /v1/extract`, with `mode_used: "upload"`,
    `url: null`, `final_url: null`, `cache: "bypass"` and `compliance.decision: "skipped"`; a
    document's `document.file_name` is the part's file name without its path. The kind is
    decided by the bytes; the part's content type decides only where they do not. A file that
    cannot be converted answers 200 with `status: failed` (`unsupported_content_type`,
    `too_large`, `conversion_failed`, ...). 422 for a form with more than one file or eight
    fields, or unknown fields; 413 for a body over `DOCUMENT_MAX_BYTES` + 64 KiB; 400 for
    `ocr: always` while the service has no OCR.
    """
    settings: Settings = request.app.state.settings
    check_ocr_request(form.ocr, settings)
    try:
        body = await file.read(settings.document_max_bytes + 1)
    finally:
        await file.close()  # deletes the spooled part now, not when the answer is sent
    pipeline: Pipeline = request.app.state.pipeline
    return await pipeline.extract_upload(
        body,
        sanitize_file_name(file.filename),
        form,
        principal,
        content_type=file.content_type,
    )
