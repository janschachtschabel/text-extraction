"""Bucket classifier of the live regression harness.

Maps one answer of ``POST /v1/extract`` to the bucket the summary counts it in. The mapping
reads the result envelope only: ``status``, ``error.code``, ``data.word_count`` and
``data.compliance.decision``.
"""

from dataclasses import dataclass
from typing import Any, Literal

Bucket = Literal[
    "ok",
    "too_short",
    "likely_error_page",
    "no_content",
    "target_http_error",
    "connection_error",
    "blocked",
    "rate_limited",
    "service_error",
    "invalid_request",
    "unknown",
]

BUCKETS: tuple[Bucket, ...] = (
    "ok",
    "too_short",
    "likely_error_page",
    "no_content",
    "target_http_error",
    "connection_error",
    "blocked",
    "rate_limited",
    "service_error",
    "invalid_request",
    "unknown",
)

# Every value of `text_extraction.envelope.ErrorCode` maps to exactly one bucket. A test fails
# when a new code is missing here, so nothing falls into `unknown` silently.
CODE_BUCKETS: dict[str, Bucket] = {
    # The caller's request, or the URL it named, cannot be served.
    "invalid_url": "invalid_request",
    "blocked_url": "invalid_request",  # SSRF guard: the URL points at a non-public address
    "invalid_request": "invalid_request",
    "unauthorized": "invalid_request",
    "forbidden": "invalid_request",
    "not_found": "invalid_request",
    # Compliance (counts as blocked only under `enforce`: the codes appear only there).
    "blocked_by_robots": "blocked",
    "tdm_reserved": "blocked",
    # The target could not be reached in time.
    "dns_error": "connection_error",
    "connection_error": "connection_error",
    "timeout": "connection_error",
    "target_http_error": "target_http_error",
    # The target answered, but not with the content.
    "bot_protection": "likely_error_page",
    "login_required": "likely_error_page",
    "likely_error_page": "likely_error_page",
    "empty_content": "no_content",
    "unsupported_content_type": "no_content",
    "too_large": "no_content",
    "conversion_failed": "no_content",
    "no_transcript_available": "no_content",
    "media_too_long": "no_content",
    "transcription_failed": "no_content",
    "content_too_short": "too_short",
    # Our own faults and limits.
    "rate_limited": "rate_limited",
    "internal_error": "service_error",
    "llm_failed": "service_error",
}


@dataclass(frozen=True)
class EnvelopeFields:
    """The envelope fields the harness records; ``None`` where the answer had none."""

    status: str | None
    error_code: str | None
    word_count: int | None
    compliance_decision: str | None


def read_envelope(payload: object) -> EnvelopeFields | None:
    """The harness's view of a decoded response body, or ``None`` when it is no envelope."""
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return None
    error = _mapping(payload.get("error"))
    data = _mapping(payload.get("data"))
    compliance = _mapping(data.get("compliance"))
    code = error.get("code")
    words = data.get("word_count")
    decision = compliance.get("decision")
    return EnvelopeFields(
        status=payload["status"],
        error_code=code if isinstance(code, str) else None,
        # bool is an int in Python; a flag is no word count.
        word_count=words if isinstance(words, int) and not isinstance(words, bool) else None,
        compliance_decision=decision if isinstance(decision, str) else None,
    )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def classify(http_status: int | None, payload: object, *, min_words: int = 50) -> Bucket:
    """The bucket of one answer.

    ``http_status`` is ``None`` when the request got no answer at all; ``payload`` is the
    decoded JSON body, or ``None`` when the body was not JSON.
    """
    fields = read_envelope(payload)
    if http_status is None or fields is None or http_status >= 500:
        return "service_error"
    if http_status == 429:
        return "rate_limited"
    if http_status >= 400:
        return "invalid_request"
    if http_status != 200:
        return "service_error"

    code_bucket = None if fields.error_code is None else CODE_BUCKETS.get(fields.error_code)
    if fields.status == "failed":
        if fields.compliance_decision == "blocked":
            return "blocked"
        return "unknown" if code_bucket is None else code_bucket
    if fields.status not in ("success", "partial"):
        return "unknown"
    # Content came back. Only a code other than the "too short" notice can still spoil it.
    if code_bucket is not None and code_bucket != "too_short":
        return code_bucket
    return "ok" if (fields.word_count or 0) >= min_words else "too_short"
