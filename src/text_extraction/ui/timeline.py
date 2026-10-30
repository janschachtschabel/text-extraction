"""What happened to one result, from its fields only (ruling P10-S17): the item's last claim
(D8), compliance, fetch, render, convert, PII, LLM and the outcome, in that order."""

from dataclasses import dataclass
from datetime import UTC
from typing import Literal

from text_extraction.browser_models import ScreenshotResult
from text_extraction.envelope import ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobItem

RequestedMode = Literal["fast", "auto", "browser"]


@dataclass(frozen=True)
class TimelineStep:
    """One step: its name, what the fields say about it and its milliseconds (None when the
    result has no time for it)."""

    label: str
    detail: str
    ms: int | None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _claim(item: JobItem | None) -> list[TimelineStep]:
    if item is None or item.run is None:
        return []
    run = item.run
    claimed = run.claimed_at
    if claimed.tzinfo is not None:
        claimed = claimed.astimezone(UTC)
    when = claimed.strftime("%Y-%m-%d %H:%M:%S UTC")
    detail = f"{run.pool} pool, claim {run.attempts}, {when}"
    return [TimelineStep("Claim", detail, None)]


def _compliance(result: ExtractResult | ScreenshotResult) -> TimelineStep:
    report = result.compliance
    parts = [f"mode {report.mode}"]
    if report.robots != "not_checked":
        parts.append(f"robots {report.robots}")
    if report.ai_txt not in ("not_checked", "absent"):
        parts.append(f"ai.txt {report.ai_txt}")
    if report.tdm_reserved:
        parts.append("TDM reserved")
    if report.noai:
        parts.append("noai")
    parts.extend(report.reasons)
    detail = f"{report.decision} ({', '.join(parts)})"
    return TimelineStep("Compliance", detail, result.timings.compliance_ms)


def _fetch(result: ExtractResult | ScreenshotResult) -> TimelineStep:
    if result.mode_used == "upload":
        return TimelineStep("Upload", "no fetch", None)
    parts = [_plural(result.attempts, "attempt")]
    if result.upstream_status is not None:
        parts.append(f"HTTP {result.upstream_status}")
    if result.final_url is not None and result.final_url != result.url:
        parts.append(f"final URL {result.final_url}")
    if isinstance(result, ExtractResult):
        parts.append(f"cache {result.cache}")
    return TimelineStep("Fetch", ", ".join(parts), result.timings.fetch_ms)


def _render(
    result: ExtractResult | ScreenshotResult, requested_mode: RequestedMode | None
) -> list[TimelineStep]:
    if result.mode_used != "browser":
        return []
    parts = []
    if requested_mode == "auto":
        parts.append("escalated from fast")
    if result.unblocked:
        parts.append("unblock mode")
    return [TimelineStep("Render", ", ".join(parts), result.timings.render_ms)]


def _convert(result: ExtractResult) -> TimelineStep:
    parts = [result.content_type] if result.content_type else []
    document = result.document
    if document is not None:
        parts.append(document.converter)
        if document.pages is not None:
            pages = _plural(document.pages, "page")
            converted = document.pages_converted
            if converted is not None and converted != document.pages:
                pages = f"{converted} of {pages}"
            parts.append(pages)
        if document.ocr:
            parts.append("OCR")
    return TimelineStep("Convert", ", ".join(parts), result.timings.convert_ms)


def _pii(result: ExtractResult) -> list[TimelineStep]:
    if result.pii is None:
        return []
    count = sum(result.pii.entities.values())
    entities = f"{count} entity" if count == 1 else f"{count} entities"
    detail = f"{entities} ({result.pii.mode})"
    return [TimelineStep("PII", detail, result.timings.pii_ms)]


def _llm(result: ExtractResult | ScreenshotResult) -> list[TimelineStep]:
    if result.timings.llm_ms <= 0:
        return []
    return [TimelineStep("LLM", "", result.timings.llm_ms)]


def _outcome(
    result: ExtractResult | ScreenshotResult, status: str | None, error: ErrorInfo | None
) -> TimelineStep:
    parts = [status] if status else []
    if error is not None:
        parts.append(str(error.code))
        if error.retryable:
            parts.append("retryable")
    return TimelineStep("Outcome", ", ".join(parts), result.timings.total_ms)


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
