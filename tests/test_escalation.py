from typing import Any, Literal

import pytest

from text_extraction.compliance import ComplianceReport
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.escalation import (
    BROWSER_UNAVAILABLE_WARNING,
    better,
    needs_browser,
    unblock_applies,
)
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.quality import Quality
from text_extraction.settings import Settings

Status = Literal["success", "partial", "failed"]


def quality(**fields: Any) -> Quality:
    values: dict[str, Any] = {
        "visible_chars": 5000,
        "word_count": 800,
        "script_count": 2,
        "js_heavy": False,
        "bot_challenge": False,
        "soft_404": False,
        "login_wall": False,
        "browser_recommended": False,
    }
    values.update(fields)
    return Quality(**values)


def envelope(
    *,
    status: Status = "success",
    code: ErrorCode | None = None,
    mode_used: Literal["fast", "browser"] = "fast",
    words: int = 800,
    q: Quality | None = None,
    **extra: Any,
) -> Envelope[ExtractResult]:
    result = ExtractResult(
        url="https://example.org/",
        final_url="https://example.org/",
        upstream_status=200,
        content_type="text/html",
        mode_used=mode_used,
        attempts=1,
        language=None,
        word_count=words,
        title=None,
        format="markdown",
        content="text",
        quality=q,
        timings=Timings(total_ms=1, compliance_ms=0, fetch_ms=0, convert_ms=0),
        compliance=ComplianceReport(mode="enforce"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="miss",
        warnings=[],
        **extra,
    )
    error = ErrorInfo(code=code, message="m") if code else None
    return Envelope(status=status, data=result, error=error)


def test_js_heavy_fast_result_needs_browser() -> None:
    shell = envelope(
        status="failed",
        code=ErrorCode.EMPTY_CONTENT,
        words=0,
        q=quality(js_heavy=True, browser_recommended=True, word_count=0),
    )

    assert needs_browser(shell, unblock=False)
    assert needs_browser(shell, unblock=True)
    assert not needs_browser(envelope(q=quality()), unblock=False)
    assert not needs_browser(envelope(q=quality()), unblock=True)


def test_browser_result_never_escalates() -> None:
    rendered = envelope(
        mode_used="browser",
        q=quality(
            js_heavy=True, bot_challenge=True, browser_recommended=True, challenge_status=True
        ),
        code=ErrorCode.BOT_PROTECTION,
        status="failed",
    )

    assert not needs_browser(rendered, unblock=False)
    assert not needs_browser(rendered, unblock=True)


def test_non_html_or_failed_fetch_does_not_escalate() -> None:
    failed_fetch = envelope(status="failed", code=ErrorCode.TARGET_HTTP_ERROR, q=None)
    no_data: Envelope[ExtractResult] = Envelope(
        status="failed", error=ErrorInfo(code=ErrorCode.TIMEOUT, message="m", retryable=True)
    )

    for unblock in (False, True):
        assert not needs_browser(failed_fetch, unblock=unblock)
        assert not needs_browser(no_data, unblock=unblock)
        assert not needs_browser(
            envelope(status="failed", code=ErrorCode.BOT_PROTECTION, q=None), unblock=unblock
        )


def test_bot_protection_and_challenge_status_escalate_only_under_unblock() -> None:
    blocked = envelope(status="failed", code=ErrorCode.BOT_PROTECTION, q=quality())
    challenge = envelope(
        status="failed",
        code=ErrorCode.TARGET_HTTP_ERROR,
        q=quality(challenge_status=True),
    )

    assert not needs_browser(blocked, unblock=False)
    assert needs_browser(blocked, unblock=True)
    assert not needs_browser(challenge, unblock=False)
    assert needs_browser(challenge, unblock=True)


@pytest.mark.parametrize(
    ("fast", "rendered", "winner"),
    [
        (("success", 40), ("partial", 500), "fast"),
        (("partial", 500), ("success", 40), "rendered"),
        (("partial", 5), ("failed", 900), "fast"),
        (("success", 175), ("success", 42), "fast"),
        (("success", 42), ("success", 175), "rendered"),
        (("success", 42), ("success", 42), "rendered"),
        (("failed", 0), ("failed", 0), "rendered"),
    ],
)
def test_better_prefers_status_then_words_then_rendered(
    fast: tuple[Status, int], rendered: tuple[Status, int], winner: str
) -> None:
    fast_envelope = envelope(status=fast[0], words=fast[1])
    rendered_envelope = envelope(status=rendered[0], words=rendered[1], mode_used="browser")

    chosen = better(fast_envelope, rendered_envelope)

    assert chosen is (fast_envelope if winner == "fast" else rendered_envelope)


def test_better_copes_with_missing_data() -> None:
    nothing: Envelope[ExtractResult] = Envelope(
        status="failed", error=ErrorInfo(code=ErrorCode.TIMEOUT, message="m")
    )
    page = envelope(status="failed", words=0)

    assert better(nothing, page) is page
    assert better(page, nothing) is nothing


def settings(**fields: Any) -> Settings:
    return Settings(auth_mode="none", **fields)


@pytest.mark.parametrize(
    ("mode", "enabled", "expected"),
    [
        ("ignore", True, True),
        ("ignore", False, False),
        ("enforce", True, False),
        ("report", True, False),
    ],
)
def test_unblock_applies_only_with_ignore_and_switch(
    mode: Literal["enforce", "report", "ignore"], enabled: bool, expected: bool
) -> None:
    assert unblock_applies(mode, settings(browser_unblock_enabled=enabled)) is expected


def test_warning_name() -> None:
    assert BROWSER_UNAVAILABLE_WARNING == "browser_unavailable"
