"""The extraction tools (D2 a, rulings P10-S5 to P10-S8): ``extract_url``, ``extract_file``,
``screenshot_url``, ``extract_structured`` and ``chunk``.

Each tool lists the few arguments agents use plus one ``options`` object for any other field of
its route's REST body. A call validates the arguments, merges ``options`` under them (a key in
both is ``invalid_request``), passes ``wait_seconds = MCP_WAIT_SECONDS`` when the caller gives
none, validates the merged body with the REST model (unknown fields are refused, as in REST) and
runs the route's core with the ``X-ASR-Api-Key`` and ``X-LLM-Api-Key`` headers of the call's HTTP
request. The answer is the route's envelope; ``content`` is cut to ``max_chars`` from
``content_offset``, and a screenshot is attached as an image.
"""

import json
import re
from abc import abstractmethod
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal

from mcp_types import ImageContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, WithJsonSchema, field_validator
from starlette.datastructures import State
from starlette.responses import Response

from text_extraction.asr_dispatch import ASR_KEY_HEADER, asr_key_of
from text_extraction.auth import Principal
from text_extraction.browser_models import ScreenshotRequest
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.errors import BodyTooLarge, ServiceError
from text_extraction.extract_api import respond
from text_extraction.extract_models import ChunkRequest, ExtractRequest, StructuredRequest
from text_extraction.file_api import (
    MAX_FIELD_BYTES,
    MAX_OPTION_FIELD_BYTES,
    OPTION_FIELDS,
    check_upload_request,
    run_upload,
    sanitize_file_name,
    text_body_limit,
    upload_file_limit,
)
from text_extraction.file_models import FileForm
from text_extraction.jobs_api import check_jobs_enabled
from text_extraction.mcp_server.calls import ModelTool, call_header
from text_extraction.mcp_server.files import MAX_CONTENT_TYPE_CHARS, decode_file
from text_extraction.mcp_server.results import (
    answer_payload,
    image_over_limit_hint,
    job_hint,
    payload_result,
    png_content,
    slice_content,
)
from text_extraction.mcp_server.server import mcp_body_limit
from text_extraction.screenshot_api import read_artifact, run_screenshot
from text_extraction.settings import Settings
from text_extraction.structured.checks import LLM_KEY_HEADER, llm_key_of
from text_extraction.structured_api import run_chunk, run_structured

type Answer = Envelope[Any] | Response

# The arguments that are the tool's own, never a field of the REST body.
_TOOL_ONLY = frozenset(
    {"options", "content_offset", "max_chars", "content_base64", "file_name", "content_type"}
)
_ARTIFACT_URL = re.compile(r"/v1/jobs/([^/]+)/artifacts/([^/]+)")
_OPEN_WORLD = ToolAnnotations(open_world_hint=True)


def _object(description: str) -> Any:
    """A JSON object argument whose fields the REST model validates: ``{"type": "object"}``."""
    return WithJsonSchema({"type": "object", "description": description})


def options_argument(route: str) -> Any:
    return _object(
        f"Any further field of the REST body of {route} (see /docs), "
        'e.g. {"pii": {"mode": "mask"}}.'
    )


class _Arguments(BaseModel):
    """The arguments every extraction tool has; unknown ones are refused."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    wait_seconds: float | None = Field(
        default=None,
        ge=0,
        le=60,
        description="Seconds to wait for the result before it continues as a job "
        "(default and at most MCP_WAIT_SECONDS).",
    )


class SlicedArguments(_Arguments):
    """The arguments of a tool whose answer has ``content``."""

    content_offset: int = Field(
        default=0,
        ge=0,
        description="First character of content to return, to read on; characters are Unicode "
        "code points, and chunk offsets count in the whole content.",
    )
    max_chars: int | None = Field(
        default=None,
        ge=1,
        description="Most characters of content to return (default and maximum "
        "MCP_MAX_CONTENT_CHARS).",
    )

    @field_validator("max_chars")
    @classmethod
    def _within_the_setting(cls, value: int | None, info: ValidationInfo) -> int | None:
        settings = (info.context or {}).get("settings")
        limit = settings.mcp_max_content_chars if isinstance(settings, Settings) else None
        if value is not None and limit is not None and value > limit:
            raise ValueError(f"max_chars is at most {limit}")
        return value


Format = Literal["markdown", "text", "html"]
FORMAT_DESCRIPTION = "Format of content."


class ExtractUrlArguments(SlicedArguments):
    url: str = Field(description="The http(s) URL of a page or document.")
    format: Format = Field(default="markdown", description=FORMAT_DESCRIPTION)
    mode: Literal["fast", "auto", "browser"] = Field(
        default="auto",
        description="fast: HTTP only; auto: a browser when the page needs one; browser: always.",
    )
    options: Annotated[dict[str, Any] | None, options_argument("POST /v1/extract")] = None


class ExtractFileArguments(SlicedArguments):
    content_base64: str = Field(
        description="The file as base64; a data:<type>;base64, prefix and line breaks are fine."
    )

    @field_validator("content_base64")
    @classmethod
    def _within_the_body_limit(cls, value: str, info: ValidationInfo) -> str:
        settings = (info.context or {}).get("settings")
        if isinstance(settings, Settings) and len(value) > mcp_body_limit(settings):
            raise ValueError(f"content_base64 has at most {mcp_body_limit(settings)} characters")
        return value

    file_name: str | None = Field(default=None, max_length=1024, description="The file's name.")
    content_type: str | None = Field(
        default=None,
        max_length=MAX_CONTENT_TYPE_CHARS,
        description="The file's media type (default: the data: prefix's; the bytes decide).",
    )
    format: Format = Field(default="markdown", description=FORMAT_DESCRIPTION)
    options: Annotated[dict[str, Any] | None, options_argument("POST /v1/extract/file")] = None


class ScreenshotUrlArguments(_Arguments):
    url: str = Field(description="The http(s) URL to capture as a PNG.")
    full_page: bool = Field(default=True, description="The whole page, not the first viewport.")
    options: Annotated[dict[str, Any] | None, options_argument("POST /v1/screenshot")] = None


class ExtractStructuredArguments(SlicedArguments):
    url: str = Field(description="The http(s) URL of a page or document.")
    structured: Annotated[
        dict[str, Any],
        _object(
            'The fields to extract, e.g. {"schema": {"type": "object", "properties": '
            '{"name": {"type": "string"}}}, "strategy": "metadata"}.'
        ),
    ]
    format: Format = Field(default="markdown", description=FORMAT_DESCRIPTION)
    options: Annotated[dict[str, Any] | None, options_argument("POST /v1/structured")] = None


class ChunkArguments(SlicedArguments):
    url: str | None = Field(default=None, description="The URL to chunk; or give text.")
    text: str | None = Field(default=None, description="The text to chunk; or give url.")
    chunking: Annotated[
        dict[str, Any],
        _object('How to chunk, e.g. {"strategy": "markdown", "size": 800, "overlap": 80}.'),
    ]
    text_type: Literal["markdown", "plain"] = Field(default="markdown", description="What text is.")
    options: Annotated[dict[str, Any] | None, options_argument("POST /v1/chunk")] = None


def lean_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema of a tool's arguments, the required ones first and ``options`` last."""
    schema = model.model_json_schema()
    properties: dict[str, Any] = schema["properties"]
    required = set(schema.get("required", ()))
    order = sorted(properties, key=lambda name: (name not in required, name == "options"))
    schema["properties"] = {name: properties[name] for name in order}
    return schema


def merge_options(args: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
    """The REST body of a call: ``options`` with the tool's ``args`` over them. A key in both is
    ``invalid_request`` ("<key> is given twice.")."""
    if not options:
        return dict(args)
    twice = sorted(set(args) & set(options))
    if twice:
        raise ServiceError(ErrorCode.INVALID_REQUEST, f"{twice[0]} is given twice.", 422)
    return {**options, **args}


def _body(args: BaseModel) -> dict[str, Any]:
    """The REST body of a call's arguments: those the caller gave, ``options`` merged."""
    given = args.model_dump(exclude_unset=True, exclude=set(_TOOL_ONLY))
    return merge_options(given, getattr(args, "options", None))


def _bounded_wait(body: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """``body`` with ``wait_seconds`` set to ``MCP_WAIT_SECONDS`` when the caller gives none, and
    cut to it when the caller gives more (P10-S7), so a call ends within the setting plus the
    route's hand-over time. A value that is no number is left to the REST model to refuse."""
    wait = body.get("wait_seconds")
    if wait is None:
        return {**body, "wait_seconds": settings.mcp_wait_seconds}
    try:
        over = float(wait) > settings.mcp_wait_seconds
    except (TypeError, ValueError):
        return body
    return {**body, "wait_seconds": settings.mcp_wait_seconds} if over else body


def _json_bytes(value: object) -> int:
    """The size of ``value`` as compact JSON in UTF-8, as a client would send it."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-8", "surrogatepass"))


def check_body_size(body: Mapping[str, Any], limit: int) -> None:
    """413 ``invalid_request`` with the REST message (``BodyTooLarge``) when ``body`` as JSON is
    over ``limit`` bytes: a tool's body is held to its route's body limit."""
    if _json_bytes(body) > limit:
        raise ServiceError(ErrorCode.INVALID_REQUEST, BodyTooLarge(limit).detail, 413)


def check_form_fields(fields: Mapping[str, Any]) -> None:
    """422 ``invalid_request``, as the upload route answers, for a form field over its part
    limit: ``structured`` and ``chunking`` 64 KiB, every other field 1 KiB."""
    for name, value in fields.items():
        size = len(value.encode("utf-8", "surrogatepass")) if isinstance(value, str) else None
        if size is None:
            size = _json_bytes(value)
        limit = MAX_OPTION_FIELD_BYTES if name in OPTION_FIELDS else MAX_FIELD_BYTES
        if size > limit:
            raise ServiceError(
                ErrorCode.INVALID_REQUEST, f"Part exceeded maximum size of {limit // 1024}KB.", 422
            )


def call_keys() -> tuple[str | None, str | None]:
    """The raw ``X-ASR-Api-Key`` and ``X-LLM-Api-Key`` headers of the call (P10-S8)."""
    return call_header(ASR_KEY_HEADER), call_header(LLM_KEY_HEADER)


class ExtractionTool(ModelTool):
    """A tool in front of one route that extracts or starts a job: the body, the core, then the
    result. ``waits`` tools pass ``wait_seconds`` (``_bounded_wait``); a route that always
    answers a job (``POST /v1/batch``, ``POST /v1/site-structure``) has none."""

    route: ClassVar[str]
    waits: ClassVar[bool] = True

    def __init__(self, name: str, description: str) -> None:
        super().__init__(
            name=name,
            description=description,
            parameters=lean_schema(self.args_model),
            annotations=_OPEN_WORLD,
        )

    @abstractmethod
    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        """Validate ``body`` with the REST model and run the route's core."""

    def check_body(self, body: dict[str, Any], settings: Settings) -> None:
        """The route's body limit, before the core runs: ``REQUEST_MAX_BODY_BYTES``."""
        check_body_size(body, settings.request_max_body_bytes)

    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        settings: Settings = state.settings
        body = _body(args)
        self.check_body(body, settings)
        if self.waits:
            body = _bounded_wait(body, settings)
        answer = await self.answer(state, principal, args, body)
        payload, is_error = answer_payload(answer)
        hints: list[str] = []
        images: list[ImageContent] = []
        if not is_error:
            job = job_hint(payload, settings.mcp_wait_seconds)
            if job is not None:
                hints.append(job)
            if isinstance(args, SlicedArguments):
                share = args.max_chars or settings.mcp_max_content_chars
                cut = slice_content(payload, offset=args.content_offset, share=share)
                if cut is not None:
                    hints.append(cut)
            await self.attach(state, principal, payload, hints, images)
        return payload_result(payload, is_error=is_error, hints=hints, images=images)

    async def attach(
        self,
        state: State,
        principal: Principal,
        payload: dict[str, Any],
        hints: list[str],
        images: list[ImageContent],
    ) -> None:
        """Add images or hints to a result that is no error; the default adds none."""


class ExtractUrlTool(ExtractionTool):
    args_model = ExtractUrlArguments

    def __init__(self) -> None:
        super().__init__(
            "extract_url",
            "Fetch a web page or document (PDF, Office, EPUB, video page) at a URL and return "
            "its main content as Markdown or text, with title and metadata "
            "(POST /v1/extract). A slow one continues as a job: poll get_job.",
        )

    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        request = ExtractRequest.model_validate(body)
        asr, llm = call_keys()
        return await respond(
            state,
            request,
            principal,
            "extract",
            asr_key=asr_key_of(asr),
            llm_key=llm_key_of(llm),
        )


class ExtractFileTool(ExtractionTool):
    args_model = ExtractFileArguments

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            "extract_file",
            "Extract a file you send as base64 (PDF, DOCX, PPTX, XLSX, EPUB, CSV, HTML, text) "
            "as Markdown or text (POST /v1/extract/file). For a file at a URL use extract_url.",
        )
        self.parameters["properties"]["content_base64"]["maxLength"] = mcp_body_limit(settings)

    def check_body(self, body: dict[str, Any], settings: Settings) -> None:
        """The upload route's part limits; the file is held to its own limit (``decode_file``)."""
        check_form_fields(body)

    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        assert isinstance(args, ExtractFileArguments)  # noqa: S101 - narrows the type
        settings: Settings = state.settings
        form = FileForm.model_validate(body)
        asr, llm = call_keys()
        # As the route: the checks that need no byte of the file come first.
        check_upload_request(form, principal, settings, asr_header=asr, llm_header=llm)
        data, prefix_type = decode_file(args.content_base64, upload_file_limit(settings))
        return await run_upload(
            state,
            principal,
            form,
            data,
            sanitize_file_name(args.file_name),
            args.content_type or prefix_type,
            asr_header=asr,
            llm_header=llm,
        )


class ScreenshotUrlTool(ExtractionTool):
    args_model = ScreenshotUrlArguments

    def __init__(self) -> None:
        super().__init__(
            "screenshot_url",
            "Render a URL in the browser and capture it as a PNG, 1920 px wide "
            "(POST /v1/screenshot); the image is attached when it is small enough.",
        )

    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        return await run_screenshot(state, principal, ScreenshotRequest.model_validate(body))

    async def attach(
        self,
        state: State,
        principal: Principal,
        payload: dict[str, Any],
        hints: list[str],
        images: list[ImageContent],
    ) -> None:
        data = payload.get("data")
        ref = data.get("screenshot") if isinstance(data, dict) else None
        if not isinstance(ref, dict):
            return
        url, size = str(ref.get("url")), ref.get("bytes")
        location = _ARTIFACT_URL.fullmatch(url)
        limit: int = state.settings.mcp_image_max_bytes
        if location is None or not isinstance(size, int):
            hints.append(f"The screenshot is not attached; download it from {url}.")
            return
        if size > limit:
            hints.append(image_over_limit_hint(size, url))
            return
        try:
            check_jobs_enabled(state.settings)
            png = await read_artifact(state, principal, location[1], location[2])
        except ServiceError:
            hints.append(f"The screenshot could not be read now; download it from {url}.")
            return
        content = png_content(png, limit=limit, url=url)
        if isinstance(content, str):
            hints.append(content)
        else:
            images.append(content)


class ExtractStructuredTool(ExtractionTool):
    args_model = ExtractStructuredArguments

    def __init__(self) -> None:
        super().__init__(
            "extract_structured",
            "Extract the fields of a JSON schema (and the page's tables) from a URL "
            "(POST /v1/structured): from its metadata, your selectors and patterns, or the LLM.",
        )

    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        asr, llm = call_keys()
        return await run_structured(
            state, principal, StructuredRequest.model_validate(body), asr_key=asr, llm_key=llm
        )


class ChunkTool(ExtractionTool):
    args_model = ChunkArguments

    def check_body(self, body: dict[str, Any], settings: Settings) -> None:
        """``POST /v1/chunk``'s body limit: the upload limit + 64 KiB (``text_body_limit``)."""
        check_body_size(body, text_body_limit(settings))

    def __init__(self) -> None:
        super().__init__(
            "chunk",
            "Split the content of a URL, or a text you send, into chunks for retrieval "
            "(POST /v1/chunk), each with its offsets and heading path.",
        )

    async def answer(
        self, state: State, principal: Principal, args: BaseModel, body: dict[str, Any]
    ) -> Answer:
        asr, llm = call_keys()
        try:
            return await run_chunk(
                state, principal, ChunkRequest.model_validate(body), asr_key=asr, llm_key=llm
            )
        except BodyTooLarge as error:  # a text over the upload limit: 413 in REST
            raise ServiceError(ErrorCode.INVALID_REQUEST, error.detail, 413) from None


def extraction_tools(settings: Settings) -> list[ModelTool]:
    """The five extraction tools of a server with ``settings``."""
    return [
        ExtractUrlTool(),
        ExtractFileTool(settings),
        ScreenshotUrlTool(),
        ExtractStructuredTool(),
        ChunkTool(),
    ]
