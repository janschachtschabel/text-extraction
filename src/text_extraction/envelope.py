"""Result envelope shared by every API answer."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel


class ErrorCode(StrEnum):
    """Machine-readable error codes (see docs/architecture.md)."""

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
