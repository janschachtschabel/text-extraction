"""Bucket classifier of the live regression harness (`tests/live/classify.py`)."""

from typing import Any

import pytest

from tests.live.classify import BUCKETS, CODE_BUCKETS, classify
from text_extraction.envelope import ErrorCode


def envelope(
    status: str,
    code: str | None = None,
    *,
    words: int = 120,
    decision: str = "allowed",
) -> dict[str, Any]:
    """The JSON shape of an `Envelope[ExtractResult]`, reduced to the fields the harness reads."""
    return {
        "status": status,
        "data": {"word_count": words, "compliance": {"mode": "report", "decision": decision}},
        "error": None if code is None else {"code": code, "message": "x", "retryable": False},
    }


def test_ok_for_success_with_enough_words() -> None:
    assert classify(200, envelope("success"), min_words=50) == "ok"


def test_partial_with_enough_words_is_ok() -> None:
    # The service's own minimum may be higher than the harness's.
    body = envelope("partial", "content_too_short", words=60)
    assert classify(200, body, min_words=50) == "ok"


def test_too_short_when_partial_and_below_the_minimum() -> None:
    body = envelope("partial", "content_too_short", words=30)
    assert classify(200, body, min_words=50) == "too_short"


def test_too_short_when_success_is_below_the_harness_minimum() -> None:
    assert classify(200, envelope("success", words=10), min_words=50) == "too_short"


def test_likely_error_page_for_bot_protection() -> None:
    assert classify(200, envelope("failed", "bot_protection", words=0)) == "likely_error_page"


def test_no_content_for_empty_content() -> None:
    assert classify(200, envelope("failed", "empty_content", words=0)) == "no_content"


def test_target_http_error() -> None:
    assert classify(200, envelope("failed", "target_http_error", words=0)) == "target_http_error"


def test_connection_error_for_timeouts() -> None:
    assert classify(200, envelope("failed", "timeout", words=0)) == "connection_error"


def test_blocked_for_compliance_codes() -> None:
    body = envelope("failed", "blocked_by_robots", words=0, decision="blocked")
    assert classify(200, body) == "blocked"


def test_blocked_decision_wins_over_a_generic_code() -> None:
    body = envelope("failed", "internal_error", words=0, decision="blocked")
    assert classify(200, body) == "blocked"


def test_rate_limited_for_http_429() -> None:
    body = envelope("failed", "rate_limited")
    body["data"] = None
    assert classify(429, body) == "rate_limited"


def test_rate_limited_for_an_outbound_gate_envelope() -> None:
    assert classify(200, envelope("failed", "rate_limited", words=0)) == "rate_limited"


def test_service_error_for_http_5xx() -> None:
    assert classify(503, envelope("failed", "internal_error")) == "service_error"


def test_service_error_for_a_non_json_body() -> None:
    assert classify(200, None) == "service_error"
    assert classify(502, None) == "service_error"


def test_service_error_when_the_request_never_got_an_answer() -> None:
    assert classify(None, None) == "service_error"


def test_service_error_for_internal_error_code() -> None:
    assert classify(200, envelope("failed", "internal_error", words=0)) == "service_error"


@pytest.mark.parametrize("http_status", [400, 401, 403, 404, 422])
def test_invalid_request_for_other_4xx(http_status: int) -> None:
    body = envelope("failed", "invalid_request")
    body["data"] = None
    assert classify(http_status, body) == "invalid_request"


def test_failed_without_an_error_is_unknown() -> None:
    assert classify(200, envelope("failed")) == "unknown"


def test_unknown_error_code_is_unknown() -> None:
    assert classify(200, envelope("failed", "brand_new_code", words=0)) == "unknown"


def test_every_error_code_maps_to_a_known_bucket() -> None:
    """A new `ErrorCode` must be mapped here, not fall into the `unknown` fallback."""
    missing = {code.value for code in ErrorCode} - set(CODE_BUCKETS)
    assert not missing, f"unmapped error codes: {sorted(missing)}"
    assert set(CODE_BUCKETS.values()) <= set(BUCKETS) - {"unknown", "ok"}


def test_every_non_ok_bucket_is_reachable_from_a_code() -> None:
    reachable = set(CODE_BUCKETS.values()) | {"ok", "unknown"}
    assert set(BUCKETS) == reachable
