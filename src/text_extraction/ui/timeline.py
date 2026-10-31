"""What happened to one result, from its fields only (ruling P10-S17): the item's last claim
(D8), compliance, fetch, render, convert, PII, LLM and the outcome, in that order. Each step's
label and detail are `Message`s (P12-S5); the values of the result (a status, an error code, a
content type, a converter) are shown as they are."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Literal

from text_extraction.browser_models import ScreenshotResult
from text_extraction.envelope import ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobItem
from text_extraction.ui.i18n import Message

RequestedMode = Literal["fast", "auto", "browser"]


# A part of a step's detail: a text (`Message`) or a value of the result shown as it is.
Part = Message | str


@dataclass(frozen=True)
class TimelineStep:
    """One step: its name (``timeline.<step>``), what the fields say about it (its parts, shown
    joined by ", ") and its milliseconds (None when the result has no time for it)."""

    label: Message
    detail: tuple[Part, ...]
    ms: int | None


def _step(name: str, detail: Sequence[Part], ms: int | None) -> TimelineStep:
    return TimelineStep(Message(f"timeline.{name}"), tuple(detail), ms)


def _count(count: int, one: str, many: str) -> Message:
    return Message(one if count == 1 else many, {"count": count})


def _claim(item: JobItem | None) -> list[TimelineStep]:
    if item is None or item.run is None:
        return []
    run = item.run
    claimed = run.claimed_at
    if claimed.tzinfo is not None:
        claimed = claimed.astimezone(UTC)
    when = claimed.strftime("%Y-%m-%d %H:%M:%S UTC")
    params = {"pool": run.pool, "attempt": run.attempts, "when": when}
    return [_step("claim", [Message("timeline.claim_detail", params)], None)]


def _compliance(result: ExtractResult | ScreenshotResult) -> TimelineStep:
    report = result.compliance
    checks: list[Part] = [Message("timeline.mode", {"mode": report.mode})]
    if report.robots != "not_checked":
        checks.append(Message("timeline.robots", {"robots": report.robots}))
    if report.ai_txt not in ("not_checked", "absent"):
        checks.append(Message("timeline.ai_txt", {"ai_txt": report.ai_txt}))
    if report.tdm_reserved:
        checks.append(Message("timeline.tdm_reserved"))
    if report.noai:
        checks.append(Message("timeline.noai"))
    checks.extend(report.reasons)
    detail = Message(
        "timeline.compliance_detail", {"decision": report.decision, "checks": tuple(checks)}
    )
    return _step("compliance", [detail], result.timings.compliance_ms)


def _fetch(result: ExtractResult | ScreenshotResult) -> TimelineStep:
    if result.mode_used == "upload":
        return _step("upload", [Message("timeline.no_fetch")], None)
    parts: list[Part] = [_count(result.attempts, "timeline.attempt", "timeline.attempts")]
    if result.upstream_status is not None:
        parts.append(Message("timeline.http", {"status": result.upstream_status}))
    if result.final_url is not None and result.final_url != result.url:
        parts.append(Message("timeline.final_url", {"url": result.final_url}))
    if isinstance(result, ExtractResult):
        parts.append(Message("timeline.cache", {"cache": result.cache}))
    return _step("fetch", parts, result.timings.fetch_ms)


def _render(
    result: ExtractResult | ScreenshotResult, requested_mode: RequestedMode | None
) -> list[TimelineStep]:
    if result.mode_used != "browser":
        return []
    parts: list[Part] = []
    if requested_mode == "auto":
        parts.append(Message("timeline.escalated"))
    if result.unblocked:
        parts.append(Message("timeline.unblock"))
    return [_step("render", parts, result.timings.render_ms)]


def _convert(result: ExtractResult) -> TimelineStep:
    parts: list[Part] = [result.content_type] if result.content_type else []
    document = result.document
    if document is not None:
        parts.append(document.converter)
        if document.pages is not None:
            pages = _count(document.pages, "timeline.page", "timeline.pages")
            converted = document.pages_converted
            if converted is not None and converted != document.pages:
                pages = Message("timeline.pages_of", {"converted": converted, "pages": pages})
            parts.append(pages)
        if document.ocr:
            parts.append(Message("timeline.ocr"))
    return _step("convert", parts, result.timings.convert_ms)


def _pii(result: ExtractResult) -> list[TimelineStep]:
    if result.pii is None:
        return []
    count = sum(result.pii.entities.values())
    entities = _count(count, "timeline.entity", "timeline.entities")
    detail = Message("timeline.pii_detail", {"entities": entities, "mode": result.pii.mode})
    return [_step("pii", [detail], result.timings.pii_ms)]


def _llm(result: ExtractResult | ScreenshotResult) -> list[TimelineStep]:
    if result.timings.llm_ms <= 0:
        return []
    return [_step("llm", [], result.timings.llm_ms)]


def _outcome(
    result: ExtractResult | ScreenshotResult, status: str | None, error: ErrorInfo | None
) -> TimelineStep:
    parts: list[Part] = [status] if status else []
    if error is not None:
        parts.append(str(error.code))
        if error.retryable:
            parts.append(Message("timeline.retryable"))
    return _step("outcome", parts, result.timings.total_ms)


def timeline(
    item: JobItem | None,
    result: ExtractResult | ScreenshotResult,
    *,
    status: str | None = None,
    error: ErrorInfo | None = None,
    requested_mode: RequestedMode | None = None,
) -> list[TimelineStep]:
    """The steps of ``result`` (of the job item ``item``, if any). ``status`` and ``error`` are
    the envelope's (by default those of ``item.result``); ``requested_mode`` is the request's
    ``mode``, which tells a render after ``auto`` ("escalated from fast") from one asked for.
    A screenshot result has no convert, PII or cache fields; its steps leave them out."""
    if item is not None and item.result is not None:
        status = status or item.result.status
        error = error or item.result.error
    steps = _claim(item)
    steps.append(_compliance(result))
    steps.append(_fetch(result))
    steps.extend(_render(result, requested_mode))
    if isinstance(result, ExtractResult):
        steps.append(_convert(result))
        steps.extend(_pii(result))
    steps.extend(_llm(result))
    steps.append(_outcome(result, status, error))
    return steps
