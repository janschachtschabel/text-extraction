"""``POST /v1/screenshot`` and ``GET /v1/jobs/{id}/artifacts/{name}``.

A screenshot request is an extraction request in ``browser`` mode with ``screenshot`` and
without metadata, and goes through the same flow (``extract_api.respond``) as a job of kind
``screenshot``; its answer is the extraction result without the text (``ScreenshotResult``).
The browser worker stores the PNG as an artifact of the job for ``ARTIFACT_TTL_SECONDS`` (B7),
and the artifact route serves it to the job's owner (and admins), like the job view.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from redis.exceptions import RedisError

from text_extraction.auth import Principal, require_role
from text_extraction.browser_models import ScreenshotRequest, ScreenshotResult, screenshot_view
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.extract_api import respond
from text_extraction.extract_models import ExtractRequest
from text_extraction.job_models import JobAccepted
from text_extraction.jobs import JobStore, valid_artifact_name
from text_extraction.jobs_api import backend_unavailable, require_jobs_enabled, visible_job
from text_extraction.ratelimit import rate_limited

router = APIRouter()

_MEDIA_TYPES = {".png": "image/png"}


@router.post(
    "/v1/screenshot",
    dependencies=[Depends(rate_limited)],
    response_model=Envelope[ScreenshotResult],
    responses={
        202: {
            "model": Envelope[JobAccepted],
            "description": "With `wait_seconds`: the capture takes longer and became a job.",
        }
    },
)
async def screenshot(
    body: ScreenshotRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[ScreenshotResult] | JSONResponse:
    """Render a URL in the browser and capture it as a PNG, 1920 px wide.

    `data.screenshot` references the file: download it from its `url`
    (`GET /v1/jobs/{id}/artifacts/screenshot.png`) within `ARTIFACT_TTL_SECONDS`; the download
    needs jobs to be enabled (`JOBS_ENABLED=true`, otherwise 503). The page's
    `quality` is reported but does not fail a stored screenshot. Outcomes of the target answer
    200 with `status: failed`, as for `/v1/extract`; without a browser worker in time the
    answer is `failed`/`timeout`, retryable. 400 while browser mode is off, 403 for an
    `evaluate` action of a non-admin key or a compliance mode the key may not choose. With
    `wait_seconds`: 202 with the job (kind `screenshot`), whose item holds the result.
    """
    req = ExtractRequest(
        url=body.url,
        mode="browser",
        metadata=False,
        screenshot=True,
        full_page=body.full_page,
        actions=body.actions,
        cookies=body.cookies,
        headers=body.headers,
        compliance=body.compliance,
        wait_seconds=body.wait_seconds,
    )
    answer = await respond(request, req, principal, "screenshot")
    if isinstance(answer, JSONResponse):
        return answer
    return Envelope(
        status=answer.status,
        data=screenshot_view(answer.data) if answer.data is not None else None,
        error=answer.error,
        trace_id=answer.trace_id,
    )


@router.get(
    "/v1/jobs/{job_id}/artifacts/{name}",
    dependencies=[Depends(require_jobs_enabled), Depends(rate_limited)],
    response_class=Response,
    responses={
        200: {
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
            "description": "The file (a screenshot is `screenshot.png`).",
        },
        404: {
            "model": Envelope[None],
            "description": "No such job (or another caller's) or no such file, or it expired.",
        },
        503: {
            "model": Envelope[None],
            "description": "Jobs are disabled (`JOBS_ENABLED=false`), or the job backend is "
            "unavailable (retryable).",
        },
    },
)
async def artifact(
    job_id: str,
    name: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Response:
    """Download a file of a job, such as its screenshot. Only the job's owner (and admins) may;
    another caller's job, an unknown or expired job and a missing file all answer 404."""
    store: JobStore = request.app.state.job_store
    if not valid_artifact_name(name):
        raise _no_file()
    try:
        await visible_job(store, job_id, principal)
        data = await store.artifact(job_id, name)
    except (RedisError, OSError) as error:
        raise backend_unavailable(error) from None
    if data is None:
        raise _no_file()
    suffix = name[name.rfind(".") :] if "." in name else ""
    media_type = _MEDIA_TYPES.get(suffix, "application/octet-stream")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _no_file() -> ServiceError:
    return ServiceError(ErrorCode.NOT_FOUND, "File not found.", 404)
