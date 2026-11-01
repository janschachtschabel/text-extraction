"""Result envelope shared by every API answer."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel


class ErrorCode(StrEnum):
    """Machine-readable error codes; `error.retryable` says whether a retry may help.

    Outcomes of an extraction (200 with `status: failed`, some also on `partial`):
    `invalid_url` (no http(s) URL), `blocked_url` (a private or otherwise refused address),
    `blocked_by_robots` (robots.txt forbids it, `compliance: enforce`), `tdm_reserved` (the
    site reserves text and data mining), `dns_error`, `connection_error`, `timeout`,
    `target_http_error` (the site answered 4xx or 5xx, see `upstream_status`), `bot_protection`
    (a bot wall), `login_required`, `likely_error_page` (a 200 that reads as an error page),
    `empty_content`, `content_too_short` (`partial`), `unsupported_content_type`, `too_large`
    (over a byte or character limit), `conversion_failed`, `no_transcript_available`,
    `media_too_long`, `transcription_failed`, `llm_failed` (LLM work did not run or failed;
    turns `success` into `partial`), `pii_failed` (PII handling did not finish; no text is
    returned), `rate_limited` (too many requests, or the service holds the site back; the wait
    is in `Retry-After` or `error.details`), `internal_error` (a fault of the service).

    The API's own 4xx answers: `invalid_request` (400, 413, 415, 422), `unauthorized` (401),
    `forbidden` (403), `not_found` (404). See docs/architecture.md.
    """

    INVALID_URL = "invalid_url"
    BLOCKED_URL = "blocked_url"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"
    TDM_RESERVED = "tdm_reserved"
    DNS_ERROR = "dns_error"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    TARGET_HTTP_ERROR = "target_http_error"
    BOT_PROTECTION = "bot_protection"
    LOGIN_REQUIRED = "login_required"
    LIKELY_ERROR_PAGE = "likely_error_page"
    EMPTY_CONTENT = "empty_content"
    CONTENT_TOO_SHORT = "content_too_short"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    TOO_LARGE = "too_large"
    CONVERSION_FAILED = "conversion_failed"
    NO_TRANSCRIPT_AVAILABLE = "no_transcript_available"
    MEDIA_TOO_LONG = "media_too_long"
    TRANSCRIPTION_FAILED = "transcription_failed"
    LLM_FAILED = "llm_failed"
    PII_FAILED = "pii_failed"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    # The service's own 4xx answers.
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: list[dict[str, Any]] | None = None


class Envelope[T](BaseModel):
    """Uniform answer: `status` plus either `data` or `error`."""

    status: Literal["success", "partial", "failed"]
    data: T | None = None
    error: ErrorInfo | None = None
    trace_id: str | None = None
