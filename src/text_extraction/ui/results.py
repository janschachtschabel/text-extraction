"""The UI's result view (rulings P10-S14 to P10-S17): every forwarded answer becomes one
fragment with the HTTP status, the envelope's status and error (with the 422 ``details``), the
result's content (as source, rendered Markdown and a download), its title, language, word
count, metadata, structured values, chunks, images, links and timeline, the raw JSON and the
curl line of the request with ``$API_KEY`` in place of the key.

Everything is rendered by autoescaping templates: no field of a result reaches the page as HTML,
except the Markdown rendering of `render_markdown`, which escapes it itself."""

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

from pydantic import ValidationError

from text_extraction.browser_models import ScreenshotResult
from text_extraction.envelope import ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobItem
from text_extraction.ui.i18n import Language
from text_extraction.ui.markdown import is_http, render_markdown
from text_extraction.ui.security import RUN_PREFIX
from text_extraction.ui.templating import language_context, templates
from text_extraction.ui.texts import warning_text
from text_extraction.ui.timeline import RequestedMode, TimelineStep, timeline

DOWNLOADS: Final = {
    "markdown": ("text/markdown", ".md"),
    "text": ("text/plain", ".txt"),
    # Never an HTML media type: the page's own markup is saved, never shown.
    "html": ("text/plain", ".html"),
}
DOWNLOAD_NAME: Final = "result"
# Each copy of a large content in one fragment is bounded: the source shown, the raw JSON and the
# chunks show at most SHOWN_MAX_CHARS (and only such a content is rendered as Markdown); the
# download is the full copy, up to DOWNLOAD_MAX_CHARS.
SHOWN_MAX_CHARS: Final = 200_000
DOWNLOAD_MAX_CHARS: Final = 10_000_000
# The headers a curl line names, with the shell variable that stands for each value.
_CURL_HEADERS: Final = (
    ("authorization", "Authorization: Bearer $API_KEY"),
    ("x-llm-api-key", "X-LLM-Api-Key: $LLM_API_KEY"),
    ("x-asr-api-key", "X-ASR-Api-Key: $ASR_API_KEY"),
)


def _quoted(text: str) -> str:
    """``text`` in single quotes for a POSIX shell."""
    return "'" + text.replace("'", "'\\''") + "'"


def curl_line(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: Sequence[str],
    json_body: Mapping[str, Any] | None = None,
    form: Mapping[str, str] | None = None,
    file_name: str | None = None,
) -> str:
    """The curl command of a forwarded request. ``headers`` are the lower-case names of the
    key headers the request carried; each is written with a shell variable (``$API_KEY``,
    ``$LLM_API_KEY``, ``$ASR_API_KEY``), never with its value."""
    parts = ["curl", "-X", method, _quoted(base_url.rstrip("/") + path)]
    for name, line in _CURL_HEADERS:
        if name in headers:
            parts.extend(["-H", f'"{line}"'])
    if json_body is not None:
        parts.extend(["-H", _quoted("Content-Type: application/json")])
        parts.extend(["-d", _quoted(json.dumps(json_body))])
    if file_name is not None:
        parts.extend(["-F", _quoted(f"file=@{file_name}")])
    for name, value in (form or {}).items():
        parts.extend(["-F", _quoted(f"{name}={value}")])
    return " ".join(parts)


@dataclass(frozen=True)
class Row:
    """A label and its text in a definition list; ``link`` when the text is an http(s) URL."""

    label: str
    text: str
    link: bool = False


def _shown(value: Any) -> str:
    """A value as text: a string as it is, anything else as its JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _licence_text(licence: Mapping[str, Any]) -> str:
    parts = [str(licence[name]) for name in ("id", "label", "url") if licence.get(name)]
    if licence.get("source"):
        parts.append(f"(from {licence['source']})")
    return " ".join(parts)


def _metadata_rows(metadata: Mapping[str, Any]) -> list[Row]:
    rows: list[Row] = []
    for label, name in (
        ("Description", "description"),
        ("Published", "published"),
        ("Modified", "modified"),
        ("Site", "site_name"),
        ("Language", "language"),
    ):
        if metadata.get(name):
            rows.append(Row(label, _shown(metadata[name])))
    if metadata.get("authors"):
        rows.append(Row("Authors", ", ".join(map(_shown, metadata["authors"]))))
    if metadata.get("keywords"):
        rows.append(Row("Keywords", ", ".join(map(_shown, metadata["keywords"]))))
    canonical = metadata.get("canonical_url")
    if canonical:
        rows.append(Row("Canonical", _shown(canonical), link=is_http(canonical)))
    licence = metadata.get("license")
    if isinstance(licence, Mapping):
        rows.append(Row("Licence", _licence_text(licence)))
    others = [item for item in metadata.get("licenses") or [] if isinstance(item, Mapping)]
    if len(others) > 1:
        rows.append(Row("Licences found", "; ".join(_licence_text(item) for item in others)))
    if metadata.get("lrmi"):
        rows.append(Row("LRMI", _shown(metadata["lrmi"])))
    return rows


def _typed_result(data: Mapping[str, Any]) -> ExtractResult | ScreenshotResult | None:
    """The result as its model, for the timeline; None for data of another shape."""
    for model in (ExtractResult, ScreenshotResult):
        try:
            return model.model_validate(data)
        except ValidationError:
            continue
    return None


def _error_of(envelope: Mapping[str, Any]) -> ErrorInfo | None:
    error = envelope.get("error")
    if not isinstance(error, Mapping):
        return None
    try:
        return ErrorInfo.model_validate(error)
    except ValidationError:
        return None


def _details(error: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for detail in error.get("details") or []:
        if not isinstance(detail, Mapping):
            continue
        loc = ".".join(str(part) for part in detail.get("loc") or [])
        lines.append(f"{loc}: {detail.get('msg', '')}" if loc else str(detail.get("msg", "")))
    return lines


@dataclass(frozen=True)
class Download:
    """The content as a ``data:`` URI to save, its file name, its suffix and, when the content
    was cut to ``DOWNLOAD_MAX_CHARS``, the note that says so."""

    href: str
    name: str
    suffix: str
    note: str | None = None


def _download(data: Mapping[str, Any]) -> Download | None:
    content = data.get("content")
    media = DOWNLOADS.get(str(data.get("format")))
    if not isinstance(content, str) or media is None:
        return None
    media_type, suffix = media
    note = None
    if len(content) > DOWNLOAD_MAX_CHARS:
        note = (
            f"The download is cut at {DOWNLOAD_MAX_CHARS} of {len(content)} characters; the curl "
            "command reads all of it."
        )
        content = content[:DOWNLOAD_MAX_CHARS]
    encoded = base64.b64encode(content.encode()).decode("ascii")
    href = f"data:{media_type};charset=utf-8;base64,{encoded}"
    return Download(href=href, name=f"{DOWNLOAD_NAME}{suffix}", suffix=suffix, note=note)


def _content_note(content: str) -> str | None:
    if len(content) <= SHOWN_MAX_CHARS:
        return None
    where = (
        "the download has all of them"
        if len(content) <= DOWNLOAD_MAX_CHARS
        else f"the download has the first {DOWNLOAD_MAX_CHARS}"
    )
    return f"Showing the first {SHOWN_MAX_CHARS} of {len(content)} characters; {where}."


def _chunks(data: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str | None]:
    """The chunks whose texts fit ``SHOWN_MAX_CHARS`` together, and a note on the rest."""
    chunks = [chunk for chunk in data.get("chunks") or [] if isinstance(chunk, Mapping)]
    shown: list[Mapping[str, Any]] = []
    budget = SHOWN_MAX_CHARS
    for chunk in chunks:
        budget -= len(_shown(chunk.get("text") or ""))
        if budget < 0:
            break
        shown.append(chunk)
    rest = len(chunks) - len(shown)
    return shown, (f"{rest} more chunks not shown; see the download." if rest else None)


def _raw_json(envelope: Mapping[str, Any]) -> tuple[str, str | None]:
    text = json.dumps(envelope, indent=2, ensure_ascii=False)
    if len(text) <= SHOWN_MAX_CHARS:
        return text, None
    note = f"The raw JSON is cut at {SHOWN_MAX_CHARS} of {len(text)} characters."
    return text[:SHOWN_MAX_CHARS], note


def _images(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = data.get("metadata")
    images = metadata.get("images") if isinstance(metadata, Mapping) else None
    return [
        {
            "url": image.get("url"),
            "http": is_http(image.get("url")),
            "alt": _shown(image.get("alt") or ""),
            "description": image.get("description"),
        }
        for image in images or []
        if isinstance(image, Mapping)
    ]


def _links(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "url": _shown(link.get("url")),
            "http": is_http(link.get("url")),
            "text": _shown(link.get("text") or ""),
            "kind": link.get("kind"),
        }
        for link in data.get("links") or []
        if isinstance(link, Mapping)
    ]


def _structured(data: Mapping[str, Any]) -> dict[str, Any] | None:
    structured = data.get("structured")
    if not isinstance(structured, Mapping):
        return None
    sources = structured.get("sources") or {}
    values = structured.get("values") or {}
    missing = [_shown(name) for name in structured.get("missing") or []]
    # A field without a value (null, or an empty text or list) is listed under missing.
    empty = [name for name, value in values.items() if value in (None, "", [], {})]
    return {
        "values": [
            {"name": name, "value": _shown(value), "source": sources.get(name, "")}
            for name, value in values.items()
            if name not in empty
        ],
        "missing": missing + [name for name in empty if name not in missing],
        "tables": len(structured.get("tables") or []),
    }


def _warnings(data: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    return [(_shown(name), warning_text(_shown(name))) for name in data.get("warnings") or []]


def result_context(
    *,
    op: str,
    http_status: int,
    envelope: Mapping[str, Any] | None,
    raw: str = "",
    curl: str | None = None,
    requested_mode: RequestedMode | None = None,
    screenshot: str | None = None,
    screenshot_note: str | None = None,
    retry_after: str | None = None,
    item: JobItem | None = None,
) -> dict[str, Any]:
    """The context of ``result.html`` for one REST answer: ``envelope`` is its JSON object (None
    for another body, then ``raw`` is shown), ``screenshot`` the ``data:`` URI of its PNG (or
    ``screenshot_note`` why it could not be read), ``retry_after`` the seconds of a 429."""
    envelope = envelope or {}
    data = envelope.get("data")
    data = data if isinstance(data, Mapping) else None
    error = envelope.get("error")
    raw_json, raw_note = _raw_json(envelope) if envelope else (raw, None)
    context: dict[str, Any] = {
        "op": op,
        "http_status": http_status,
        "status": envelope.get("status"),
        "error": error if isinstance(error, Mapping) else None,
        "details": _details(error) if isinstance(error, Mapping) else [],
        "retry_after": retry_after,
        "raw_json": raw_json,
        "raw_note": raw_note,
        "curl": curl,
        "screenshot": screenshot,
        "screenshot_note": screenshot_note,
        "job": None,
        "info": None,
        "data": None,
    }
    if data is None:
        return context
    if http_status == 202 and "status_url" in data:
        context["job"] = data
        query = [("id", str(data.get("id")))] + (
            [("mode", requested_mode)] if requested_mode else []
        )
        context["progress_url"] = f"{RUN_PREFIX}/progress?{urlencode(query)}"
        return context
    if op in ("info", "login"):
        context["info"] = data
        return context
    context["data"] = data
    context["download"] = _download(data)
    content = data.get("content")
    if isinstance(content, str):
        context["shown_content"] = content[:SHOWN_MAX_CHARS]
        context["content_note"] = _content_note(content)
    markdown = (
        data.get("format") == "markdown"
        and isinstance(content, str)
        and 0 < len(content) <= SHOWN_MAX_CHARS
    )
    context["markdown"] = render_markdown(content) if markdown and content else None
    metadata = data.get("metadata")
    context["metadata"] = _metadata_rows(metadata) if isinstance(metadata, Mapping) else []
    context["structured"] = _structured(data)
    context["chunks"], context["chunks_note"] = _chunks(data)
    context["images"] = _images(data)
    compliance = data.get("compliance")
    context["compliance_mode"] = compliance.get("mode") if isinstance(compliance, Mapping) else None
    context["unblocked"] = data.get("unblocked") is True
    context["image_report"] = data.get("image_descriptions")
    context["links"] = _links(data)
    context["warnings"] = _warnings(data)
    typed = _typed_result(data)
    steps: list[TimelineStep] = []
    if typed is not None:
        steps = timeline(
            item,
            typed,
            status=str(envelope.get("status") or "") or None,
            error=_error_of(envelope),
            requested_mode=requested_mode,
        )
    context["steps"] = steps
    return context


def render_result(
    *,
    op: str,
    http_status: int,
    envelope: Mapping[str, Any] | None,
    raw: str = "",
    curl: str | None = None,
    requested_mode: RequestedMode | None = None,
    screenshot: str | None = None,
    screenshot_note: str | None = None,
    retry_after: str | None = None,
    item: JobItem | None = None,
    language: Language = "en",
) -> str:
    """``result.html`` rendered in ``language`` with `result_context` of the other
    arguments."""
    context = result_context(
        op=op,
        http_status=http_status,
        envelope=envelope,
        raw=raw,
        curl=curl,
        requested_mode=requested_mode,
        screenshot=screenshot,
        screenshot_note=screenshot_note,
        retry_after=retry_after,
        item=item,
    )
    return templates.get_template("result.html").render({**language_context(language), **context})
