"""Tool results (ruling P10-S6): the route's envelope as ``structuredContent``, the same JSON as
the last text content, one-line hints (and a screenshot as an image) before it, and ``isError``
only where REST answers 4xx or 5xx. A target's ``failed`` (REST 200) is a normal result.
``content`` is cut to the caller's share (``slice_content``); then the whole answer is bounded
by ``MCP_MAX_ANSWER_CHARS`` (``fit_answer``, M2, which reverses ruling P10-S6 for the lists):
a job page drops its trailing items, then the lists of a result are cut."""

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi.exceptions import RequestValidationError
from fastmcp.tools import ToolResult
from mcp_types import ImageContent, TextContent
from pydantic import ValidationError
from starlette.responses import Response

from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.errors import ServiceError
from text_extraction.tracing import current_trace_id

INTERNAL_MESSAGE = "Internal server error."
INVALID_MESSAGE = "The request is invalid."
# The warning of a result whose ``content`` an MCP answer returns in part (P10-S6).
TRUNCATED_WARNING = "mcp_content_truncated"
# The warning of a result whose lists an MCP answer returns in part (M2).
ANSWER_TRUNCATED_WARNING = "mcp_answer_truncated"
# Room for the warning in a result's ``warnings`` (its quotes and comma included).
_WARNING_ROOM = len(ANSWER_TRUNCATED_WARNING) + 3
# The lists of a result that ``fit_answer`` cuts, in this order (M2).
_CUT_LISTS: tuple[tuple[str, ...], ...] = (
    ("chunks",),
    ("links",),
    ("structured", "tables"),
    ("site", "links"),
)


def payload_result(
    payload: dict[str, Any],
    *,
    is_error: bool,
    hints: Sequence[str] = (),
    images: Sequence[ImageContent] = (),
) -> ToolResult:
    """The result of an envelope (``payload``): the hints, the images, then the JSON."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=hint) for hint in hints
    ]
    content.extend(images)
    content.append(TextContent(type="text", text=text))
    return ToolResult(content=content, structured_content=payload, is_error=is_error)


def _validation_details(error: RequestValidationError | ValidationError) -> list[dict[str, Any]]:
    """``loc`` and ``msg`` of each error, as REST gives them; never the input."""
    items = (
        error.errors()
        if isinstance(error, RequestValidationError)
        else error.errors(include_url=False, include_input=False, include_context=False)
    )
    return [{"loc": list(item["loc"]), "msg": item["msg"]} for item in items]


def error_info(error: Exception) -> ErrorInfo:
    """What a failed call tells the caller: a ``ServiceError``'s code, message and
    ``retryable``; ``invalid_request`` with the details of a validation error; else
    ``internal_error`` with a generic message (the caller logs the traceback)."""
    if isinstance(error, ServiceError):
        return ErrorInfo(code=error.code, message=error.message, retryable=error.retryable)
    if isinstance(error, RequestValidationError | ValidationError):
        return ErrorInfo(
            code=ErrorCode.INVALID_REQUEST,
            message=INVALID_MESSAGE,
            details=_validation_details(error),
        )
    return ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message=INTERNAL_MESSAGE)


def error_result(error: Exception) -> ToolResult:
    """The failed envelope of ``error`` (``error_info``) as an error result."""
    envelope: Envelope[None] = Envelope(
        status="failed", error=error_info(error), trace_id=current_trace_id()
    )
    return payload_result(envelope.model_dump(mode="json"), is_error=True)


def answer_payload(answer: Envelope[Any] | Response) -> tuple[dict[str, Any], bool]:
    """The envelope of a route core's answer as JSON, and whether it is an error: an envelope
    (200, or 202 with a job), or a response whose body is an envelope (``JSONResponse``), an
    error when its status is 4xx or 5xx."""
    if isinstance(answer, Envelope):
        return answer.model_dump(mode="json"), False
    payload = json.loads(bytes(answer.body))
    if not isinstance(payload, dict):
        raise TypeError("The answer of a route core is not an envelope.")
    return payload, answer.status_code >= 400


def envelope_result(answer: Envelope[Any] | Response, *, hints: Sequence[str] = ()) -> ToolResult:
    """The result of a route core's answer (``answer_payload``); ``hints`` are one-line texts
    put before the JSON."""
    payload, is_error = answer_payload(answer)
    return payload_result(payload, is_error=is_error, hints=hints)


def job_hint(payload: Mapping[str, Any], wait_seconds: int) -> str | None:
    """The hint of an answer that is a job (its ``data`` has a ``status_url``), else None."""
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("status_url"), str):
        return None
    job_id, kind = data.get("id"), data.get("kind")
    return (
        f"Continues as job {job_id} ({kind}): call get_job with job_id={job_id} and "
        f"wait_seconds up to {wait_seconds}."
    )


def slice_content(envelope: dict[str, Any], *, offset: int, share: int) -> str | None:
    """Cut ``data.content`` of ``envelope`` (in place) to ``share`` characters from ``offset``;
    characters are Unicode code points (a Python ``str`` slice), never split.
    A content that is not returned whole gets the warning ``mcp_content_truncated``. Returns the
    hint to read on, or None when nothing follows (or there is no content)."""
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return None
    content: str = data["content"]
    total = len(content)
    end = min(offset + share, total)
    if offset == 0 and end == total:
        return None
    data["content"] = content[offset:end]
    warnings = data.get("warnings")
    if isinstance(warnings, list):
        if TRUNCATED_WARNING not in warnings:
            warnings.append(TRUNCATED_WARNING)
    else:
        data["warnings"] = [TRUNCATED_WARNING]
    if end >= total:
        return None
    return f"Cut at {end} of {total} characters; call again with content_offset={end}."


def slice_items(envelope: dict[str, Any], *, offset: int, max_chars: int) -> list[str]:
    """Cut the ``content`` of each item's result of a job page (``data.items``, in place) to
    an equal share of ``max_chars`` from ``offset`` (``slice_content``), so a page of many items
    holds no more than one result. Returns the hints, one per item that has more to read."""
    data = envelope.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return []
    share = max(1, max_chars // len(items))
    hints: list[str] = []
    for item in items:
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            continue
        hint = slice_content(result, offset=offset, share=share)
        if hint is not None:
            hints.append(f"Item {item.get('index')}: {hint}")
    return hints


def _size(value: object) -> int:
    """Characters of ``value`` as ``payload_result`` writes it."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _warn(data: dict[str, Any]) -> None:
    warnings = data.get("warnings")
    if not isinstance(warnings, list):
        data["warnings"] = warnings = []
    if ANSWER_TRUNCATED_WARNING not in warnings:
        warnings.append(ANSWER_TRUNCATED_WARNING)


def _list_at(data: dict[str, Any], path: tuple[str, ...]) -> list[Any] | None:
    node: object = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, list) and node else None


def _cut_list(
    data: dict[str, Any], path: tuple[str, ...], over: int
) -> tuple[int, tuple[int, int] | None]:
    """Drop elements from the end of the list at ``path`` until ``over`` characters are gone
    (or the list is empty). Returns the characters dropped and, when it cut, (kept, total)."""
    values = _list_at(data, path)
    if values is None:
        return 0, None
    total = len(values)
    dropped = 0
    while values and dropped < over:
        dropped += _size(values.pop()) + 1  # the element and its comma
    return dropped, (len(values), total)


def _fit_result(result: dict[str, Any], over: int, prefix: str, rest: str) -> tuple[int, list[str]]:
    """Cut the lists of one result (``_CUT_LISTS`` in order) by about ``over`` characters."""
    data = result.get("data")
    if not isinstance(data, dict):
        return 0, []
    hints: list[str] = []
    saved = 0
    if ANSWER_TRUNCATED_WARNING not in (data.get("warnings") or []):
        over += _WARNING_ROOM  # the warning a cut adds
    for path in _CUT_LISTS:
        if saved >= over:
            break
        dropped, cut = _cut_list(data, path, over - saved)
        if cut is None:
            continue
        saved += dropped
        _warn(data)
        kept, total = cut
        hints.append(
            f"{prefix}{'.'.join(path)} cut to {kept} of {total} to fit MCP_MAX_ANSWER_CHARS; "
            f"{rest} returns them all."
        )
    return saved, hints


def _drop_items(data: dict[str, Any], items: list[Any], over: int) -> tuple[int, str | None]:
    """Drop trailing items of a job page (one stays) by about ``over`` characters; the page's
    ``next_offset`` then names the first dropped item."""
    dropped = 0
    while len(items) > 1 and dropped < over:
        dropped += _size(items.pop()) + 1
    if not dropped:
        return 0, None
    following = items[-1].get("index") if isinstance(items[-1], dict) else None
    if not isinstance(following, int):  # pragma: no cover - a page's items have indexes
        return dropped, None
    data["next_offset"] = following + 1
    return dropped, (
        f"Page cut to {len(items)} items to fit MCP_MAX_ANSWER_CHARS; "
        f"call get_job with offset={following + 1}."
    )


def fit_answer(payload: dict[str, Any], *, max_chars: int) -> list[str]:
    """Bound ``payload`` (in place) to about ``max_chars`` characters of JSON
    (``MCP_MAX_ANSWER_CHARS``, M2). A job page (``data.items``) drops trailing items first; then
    the lists of each result are cut from the end in the order of ``_CUT_LISTS``, with the
    warning ``mcp_answer_truncated``. ``content`` is never cut here (``slice_content`` has):
    an answer of content alone may stay over. Returns the hints of what was cut."""
    over = _size(payload) - max_chars
    if over <= 0:
        return []
    hints: list[str] = []
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if isinstance(data, dict) and isinstance(items, list) and items:
        dropped, hint = _drop_items(data, items, over)
        over -= dropped
        if hint is not None:
            hints.append(hint)
        for item in items:
            if over <= 0:
                break
            result = item.get("result") if isinstance(item, dict) else None
            if isinstance(result, dict):
                prefix = f"Item {item.get('index')}: "
                saved, cut = _fit_result(result, over, prefix, "GET /v1/jobs/{id}")
                over -= saved
                hints.extend(cut)
        return hints
    _, cut = _fit_result(payload, over, "", "the REST route")
    return cut


def image_over_limit_hint(size: object, url: str) -> str:
    """The hint of a screenshot of ``size`` bytes that is not attached."""
    return f"The screenshot ({size} bytes) is over MCP_IMAGE_MAX_BYTES; download it from {url}."


def png_content(data: bytes, *, limit: int, url: str) -> ImageContent | str:
    """A screenshot as image content when it is at most ``limit`` bytes
    (``MCP_IMAGE_MAX_BYTES``), else the hint to download it from ``url``."""
    if len(data) > limit:
        return image_over_limit_hint(len(data), url)
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type="image/png")
