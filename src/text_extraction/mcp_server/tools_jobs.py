"""The job tools (D2 a, D4 a, rulings P10-S5 to P10-S7): ``batch_extract``, ``site_structure``,
``get_job``, ``get_job_screenshot`` and ``cancel_job``.

``batch_extract`` and ``site_structure`` build their route's body as the extraction tools do
(``options`` merged under the arguments, the route's body limit, the REST model) and always
answer a job. The other three take a ``job_id`` and nothing else of a REST body. Every tool runs
``check_jobs_enabled`` (503 ``internal_error`` as REST) before its route's core, and the cores
answer 404 ``not_found`` for a job the caller may not see.

``get_job`` waits for the job, when asked, by reading its progress once a second until it is
completed or ``wait_seconds`` (at most ``MCP_WAIT_SECONDS``) are up, then answers one page whose
items share ``max_chars`` of content.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import structlog
from fastmcp.server.dependencies import get_http_request
from mcp_types import ImageContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import State

from text_extraction.auth import Principal
from text_extraction.envelope import Envelope
from text_extraction.job_models import BatchRequest, JobView
from text_extraction.jobs import SCREENSHOT_ARTIFACT
from text_extraction.jobs_api import (
    batch_body_limit,
    check_jobs_enabled,
    delete_job,
    read_job,
    start_batch,
)
from text_extraction.mcp_server.calls import ModelTool
from text_extraction.mcp_server.results import (
    fit_answer,
    payload_result,
    png_content,
    slice_items,
)
from text_extraction.mcp_server.tools_extract import (
    FORMAT_DESCRIPTION,
    Answer,
    ExtractionTool,
    Format,
    SlicedArguments,
    call_keys,
    check_body_size,
    lean_schema,
    options_argument,
)
from text_extraction.screenshot_api import read_artifact
from text_extraction.settings import Settings
from text_extraction.site.types import SiteStructureRequest
from text_extraction.site_api import start_site
from text_extraction.tracing import current_trace_id

_log = structlog.get_logger(__name__)

# The most items of one ``get_job`` page, and its default: each item's result shares
# ``max_chars``, so a page stays readable.
MAX_PAGE_ITEMS = 50
DEFAULT_PAGE_ITEMS = 5
# How often ``get_job`` reads the job's progress while it waits, in seconds.
_POLL_SECONDS = 1.0
# An offset past every item: a read of the job's progress without its items.
_NO_ITEMS = 2**62


def _job_id() -> Any:
    return Field(description="The id of the job (data.id of the answer that started it).")


class _JobArguments(BaseModel):
    """The arguments of a job tool; unknown ones are refused."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class BatchExtractArguments(_JobArguments):
    urls: list[str] = Field(
        description="The http(s) URLs to extract, at most BATCH_MAX_URLS; each becomes one item."
    )
    format: Format = Field(default="markdown", description=FORMAT_DESCRIPTION)
    mode: Literal["fast", "auto", "browser"] = Field(
        default="auto",
        description="fast: HTTP only; auto: a browser when the page needs one; browser: always.",
    )
    options: Annotated[dict[str, Any] | None, options_argument("POST /v1/batch")] = None


class SiteStructureArguments(_JobArguments):
    url: str = Field(description="The http(s) URL of the start page.")
    depth: int = Field(
        default=2, description="Link distance from the start page to follow (SITE_MAX_DEPTH)."
    )
    max_pages: int = Field(
        default=100, description="Most page items, the start page included (SITE_MAX_PAGES)."
    )
    scope: Literal["host", "path"] = Field(
        default="host",
        description="host: the start page's host; path: also only under the start URL's directory.",
    )
    options: Annotated[dict[str, Any] | None, options_argument("POST /v1/site-structure")] = None


class JobIdArguments(_JobArguments):
    job_id: str = _job_id()


class GetJobArguments(SlicedArguments):
    job_id: str = _job_id()
    offset: int = Field(default=0, ge=0, description="Index of the first item of the page.")
    limit: int = Field(
        default=DEFAULT_PAGE_ITEMS,
        ge=1,
        le=MAX_PAGE_ITEMS,
        description="Items per page; they share max_chars of content.",
    )
    wait_seconds: float = Field(  # a number here, never None
        default=0,
        ge=0,
        le=60,
        description="Seconds to wait until the job is completed (at most MCP_WAIT_SECONDS); "
        "the page is answered then, or when the time is up.",
    )


class BatchExtractTool(ExtractionTool):
    args_model = BatchExtractArguments
    waits = False

    def __init__(self) -> None:
        super().__init__(
            "batch_extract",
            "Queue many URLs as one job (POST /v1/batch); each URL becomes one item with its "
            "own result. Poll get_job for progress and results.",
        )

    def check_body(self, body: dict[str, Any], settings: Settings) -> None:
        """``POST /v1/batch``'s body limit (``batch_body_limit``)."""
        check_body_size(body, batch_body_limit(settings))

    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        check_jobs_enabled(state.settings)
        asr, llm = call_keys()
        return await start_batch(
            state, principal, BatchRequest.model_validate(body), asr_key=asr, llm_key=llm
        )


class SiteStructureTool(ExtractionTool):
    args_model = SiteStructureArguments
    waits = False

    def __init__(self) -> None:
        super().__init__(
            "site_structure",
            "Map a site as a job (POST /v1/site-structure): its pages by following links, its "
            "robots.txt and sitemaps. Poll get_job; each item's data.site has the role, depth "
            "and links or sitemap entries.",
        )

    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        check_jobs_enabled(state.settings)
        return await start_site(state, principal, SiteStructureRequest.model_validate(body))


def _read_only() -> ToolAnnotations:
    return ToolAnnotations(read_only_hint=True, open_world_hint=False)


async def _wait_until_completed(
    state: State,
    principal: Principal,
    job_id: str,
    seconds: float,
    client_gone: Callable[[], Awaitable[bool]],
) -> Envelope[JobView] | None:
    """Read the job's progress (no items) once a second until it is completed or ``seconds``
    are up. A job the caller may not see is 404 at once (``read_job``).

    After each read that finds the job unfinished, ``client_gone`` tells whether the client
    disconnected; then the wait stops at once and the last read is returned, so an abandoned
    call reads nothing more. Returns None when the wait ended otherwise. A cancelled call stops
    in its sleep or read (nothing here catches the cancellation)."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        progress = await read_job(state, principal, job_id, _NO_ITEMS, 1)
        if progress.data is not None and progress.data.state == "completed":
            return None
        if await client_gone():
            return progress
        await asyncio.sleep(min(_POLL_SECONDS, left))
    return None


class GetJobTool(ModelTool):
    """Status, progress and one page of a job's items (``GET /v1/jobs/{id}``)."""

    args_model = GetJobArguments

    def __init__(self) -> None:
        super().__init__(
            name="get_job",
            description="Status and progress of a job with one page of its items and their "
            "results (GET /v1/jobs/{id}); wait_seconds waits until it is completed.",
            parameters=lean_schema(GetJobArguments),
            annotations=_read_only(),
        )

    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        assert isinstance(args, GetJobArguments)  # noqa: S101 - narrows the type
        settings: Settings = state.settings
        check_jobs_enabled(settings)
        wait = min(args.wait_seconds, settings.mcp_wait_seconds)
        request = get_http_request()  # the call guard has read the caller from it
        gone = await _wait_until_completed(
            state, principal, args.job_id, wait, request.is_disconnected
        )
        if gone is not None:
            _log.info("mcp_client_disconnected", tool=self.name, job_id=args.job_id)
            return payload_result(gone.model_dump(mode="json"), is_error=False)
        envelope = await read_job(state, principal, args.job_id, args.offset, args.limit)
        payload = envelope.model_dump(mode="json")
        max_chars = args.max_chars or settings.mcp_max_content_chars
        hints = slice_items(payload, offset=args.content_offset, max_chars=max_chars)
        hints.extend(fit_answer(payload, max_chars=settings.mcp_max_answer_chars))
        return payload_result(payload, is_error=False, hints=hints)


class GetJobScreenshotTool(ModelTool):
    """A job's screenshot (``GET /v1/jobs/{id}/artifacts/screenshot.png``) as an image."""

    args_model = JobIdArguments

    def __init__(self) -> None:
        super().__init__(
            name="get_job_screenshot",
            description="The PNG screenshot of a screenshot job "
            "(GET /v1/jobs/{id}/artifacts/screenshot.png), attached as an image.",
            parameters=lean_schema(JobIdArguments),
            annotations=_read_only(),
        )

    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        assert isinstance(args, JobIdArguments)  # noqa: S101 - narrows the type
        settings: Settings = state.settings
        check_jobs_enabled(settings)
        png = await read_artifact(state, principal, args.job_id, SCREENSHOT_ARTIFACT)
        url = f"/v1/jobs/{args.job_id}/artifacts/{SCREENSHOT_ARTIFACT}"
        content = png_content(png, limit=settings.mcp_image_max_bytes, url=url)
        file: dict[str, Any] = {"url": url, "bytes": len(png), "content_type": "image/png"}
        envelope = Envelope[dict[str, Any]](
            status="success", data=file, trace_id=current_trace_id()
        )
        hints = [content] if isinstance(content, str) else []
        images: list[ImageContent] = [] if isinstance(content, str) else [content]
        return payload_result(
            envelope.model_dump(mode="json"), is_error=False, hints=hints, images=images
        )


class CancelJobTool(ModelTool):
    """Cancel and delete a job (``DELETE /v1/jobs/{id}``, D4 a)."""

    args_model = JobIdArguments

    def __init__(self) -> None:
        super().__init__(
            name="cancel_job",
            description="Cancel a job and delete it with its results and files "
            "(DELETE /v1/jobs/{id}); afterwards get_job answers not_found.",
            parameters=lean_schema(JobIdArguments),
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        )

    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        assert isinstance(args, JobIdArguments)  # noqa: S101 - narrows the type
        check_jobs_enabled(state.settings)
        return await delete_job(state, principal, args.job_id)


def job_tools() -> list[ModelTool]:
    """The five job tools."""
    return [
        BatchExtractTool(),
        SiteStructureTool(),
        GetJobTool(),
        GetJobScreenshotTool(),
        CancelJobTool(),
    ]
