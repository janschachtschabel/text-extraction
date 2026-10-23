"""``POST /v1/extract``: the public route in front of the extraction pipeline."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from text_extraction.auth import Principal, require_role
from text_extraction.envelope import Envelope
from text_extraction.pipeline import ExtractRequest, ExtractResult, Pipeline
from text_extraction.ratelimit import rate_limited

router = APIRouter()


@router.post("/v1/extract", dependencies=[Depends(rate_limited)])
async def extract(
    body: ExtractRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("user"))],
) -> Envelope[ExtractResult]:
    """Fetch a URL and return its main content as Markdown, text or HTML.

    Every outcome of the target answers 200: `status` is `success`, `partial` (content, but
    too short) or `failed`, with `error.code`, `error.retryable` and, in `data`,
    `upstream_status`, `compliance`, `timings` and as much of the result as was reached.
    4xx answers are about the request itself (invalid body, key, rate limit, a compliance mode
    the caller may not choose); 5xx are faults of this service.
    """
    pipeline: Pipeline = request.app.state.pipeline
    return await pipeline.run(body, principal)
