from typing import Any, Literal

import pytest

from text_extraction.compliance import ComplianceReport
from text_extraction.documents.types import DocumentInfo
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.escalation import (
    BROWSER_UNAVAILABLE_WARNING,
    REFUSAL_STATUSES,
    better,
    needs_browser,
    needs_transcript_panel,
    transcript_panel_applies,
    unblock_applies,
)
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.media.types import MediaInfo, Platform, TranscriptSource
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
    content_type: str | None = "text/html",
    upstream_status: int | None = 200,
    **extra: Any,
) -> Envelope[ExtractResult]:
    result = ExtractResult(
        url="https://example.org/",
        final_url="https://example.org/",
        upstream_status=upstream_status,
        content_type=content_type,
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


def soft_404_quality() -> Quality:
    return quality(soft_404=True, browser_recommended=True, word_count=8)


def test_only_2xx_html_escalates_on_a_recommendation() -> None:
    html = envelope(status="failed", code=ErrorCode.LIKELY_ERROR_PAGE, q=soft_404_quality())
    xhtml = envelope(
        status="failed",
        code=ErrorCode.LIKELY_ERROR_PAGE,
        q=soft_404_quality(),
        content_type="application/xhtml+xml; charset=utf-8",
    )
    text = envelope(
        status="failed",
        code=ErrorCode.LIKELY_ERROR_PAGE,
        q=soft_404_quality(),
        content_type="text/plain",
    )
    no_type = envelope(
        status="failed", code=ErrorCode.LIKELY_ERROR_PAGE, q=soft_404_quality(), content_type=None
    )
    error_status = envelope(
        status="failed",
        code=ErrorCode.LIKELY_ERROR_PAGE,
        q=soft_404_quality(),
        upstream_status=404,
    )

    for unblock in (False, True):
        assert needs_browser(html, unblock=unblock)
        assert needs_browser(xhtml, unblock=unblock)
        assert not needs_browser(text, unblock=unblock)
        assert not needs_browser(no_type, unblock=unblock)
        assert not needs_browser(error_status, unblock=unblock)


@pytest.mark.parametrize("cache", ["hit", "revalidated"])
def test_a_stored_result_never_escalates(cache: str) -> None:
    # B5: only the better of fast and rendered is stored, so a stored result is final; a fast
    # result cached while browser mode was off waits for its entry to expire.
    shell = envelope(
        status="partial",
        words=40,
        q=quality(js_heavy=True, bot_challenge=True, browser_recommended=True, word_count=40),
        code=ErrorCode.BOT_PROTECTION,
    )
    assert needs_browser(shell, unblock=True)
    assert shell.data is not None
    stored = shell.model_copy(update={"data": shell.data.model_copy(update={"cache": cache})})

    assert not needs_browser(stored, unblock=False)
    assert not needs_browser(stored, unblock=True)


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


def refusal(upstream_status: int, **extra: Any) -> Envelope[ExtractResult]:
    """A fast page answer refused with ``upstream_status``, its body no challenge."""
    return envelope(
        status="failed",
        code=ErrorCode.TARGET_HTTP_ERROR,
        words=0,
        q=quality(visible_chars=9, word_count=1),
        upstream_status=upstream_status,
        **extra,
    )


def test_refusal_statuses() -> None:
    assert frozenset({403, 429, 503}) == REFUSAL_STATUSES


@pytest.mark.parametrize("status", [403, 429, 503])
def test_plain_refusal_escalates_only_under_unblock(status: int) -> None:
    refused = refusal(status)
    text = refusal(status, content_type="text/plain")

    assert refused.data is not None
    assert refused.data.quality is not None
    assert refused.data.quality.challenge_status is False
    assert needs_browser(refused, unblock=True)
    assert needs_browser(text, unblock=True)  # whatever its content type (P12-S1)
    assert not needs_browser(refused, unblock=False)
    assert not needs_browser(text, unblock=False)


@pytest.mark.parametrize("status", [401, 404, 410, 500, 502])
def test_other_error_statuses_never_escalate(status: int) -> None:
    assert not needs_browser(refusal(status), unblock=True)
    assert not needs_browser(refusal(status), unblock=False)


def test_refusal_of_a_document_media_or_cached_result_does_not_escalate() -> None:
    document = refusal(
        403,
        document=DocumentInfo(
            kind="pdf", converter="markitdown", pages=1, pages_converted=1, ocr=False
        ),
    )
    media = refusal(403, media=MediaInfo(platform="youtube", transcript_source=None))
    fresh = refusal(403)
    assert fresh.data is not None
    cached = [
        fresh.model_copy(update={"data": fresh.data.model_copy(update={"cache": cache})})
        for cache in ("hit", "revalidated")
    ]

    for final in (document, media, *cached):
        assert not needs_browser(final, unblock=True)
    page_with_players = refusal(403, media=MediaInfo(platform="html5", transcript_source=None))
    assert needs_browser(page_with_players, unblock=True)


def test_non_success_html_escalates_under_unblock() -> None:
    short = envelope(
        status="partial",
        code=ErrorCode.CONTENT_TOO_SHORT,
        words=12,
        q=quality(word_count=12, browser_recommended=False),
    )
    empty = envelope(
        status="failed",
        code=ErrorCode.EMPTY_CONTENT,
        words=0,
        q=quality(word_count=0, browser_recommended=False),
    )
    plain = envelope(
        status="partial",
        code=ErrorCode.CONTENT_TOO_SHORT,
        words=12,
        q=quality(word_count=12),
        content_type="text/plain",
    )

    for unfinished in (short, empty):
        assert needs_browser(unfinished, unblock=True)
        assert not needs_browser(unfinished, unblock=False)
    assert not needs_browser(plain, unblock=True)  # a browser renders no better text file
    assert not needs_browser(envelope(q=quality()), unblock=True)  # a success is final


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


# --- the transcript panel (P13-S1) ------------------------------------------------------------


def video(
    *,
    warnings: list[str],
    platform: Platform = "youtube",
    source: TranscriptSource = "metadata_only",
    mode_used: Literal["fast", "browser"] = "fast",
    cache: str = "miss",
) -> Envelope[ExtractResult]:
    """A platform video's answer: its details only (``metadata_only``) by default."""
    answer = envelope(
        status="partial",
        code=ErrorCode.NO_TRANSCRIPT_AVAILABLE,
        mode_used=mode_used,
        q=quality(word_count=20),
        upstream_status=None,
        media=MediaInfo(platform=platform, transcript_source=source),
    )
    assert answer.data is not None
    data = answer.data.model_copy(update={"warnings": warnings, "cache": cache})
    return answer.model_copy(update={"data": data})


@pytest.mark.parametrize("warning", ["subtitles_blocked", "subtitles_failed"])
def test_a_youtube_video_whose_subtitles_could_not_be_read_needs_the_panel(warning: str) -> None:
    assert needs_transcript_panel(video(warnings=[warning]))


def test_a_video_youtube_says_has_no_subtitles_needs_no_panel() -> None:
    assert not needs_transcript_panel(video(warnings=[]))
    assert not needs_transcript_panel(video(warnings=["asr_unavailable"]))


@pytest.mark.parametrize(
    "answer",
    [
        video(warnings=["subtitles_blocked"], platform="vimeo"),
        video(warnings=["subtitles_blocked"], mode_used="browser"),
        video(warnings=["subtitles_blocked"], cache="hit"),
        video(warnings=["subtitles_blocked"], cache="revalidated"),
        video(warnings=["subtitles_failed"], source="auto_subtitles"),
        video(warnings=["subtitles_failed"], source="transcript_panel"),
    ],
    ids=["vimeo", "browser", "hit", "revalidated", "transcript", "panel"],
)
def test_other_answers_need_no_panel(answer: Envelope[ExtractResult]) -> None:
    assert not needs_transcript_panel(answer)


def test_a_page_or_nothing_needs_no_panel() -> None:
    page = envelope()
    assert page.data is not None
    page = page.model_copy(
        update={"data": page.data.model_copy(update={"warnings": ["subtitles_blocked"]})}
    )
    assert not needs_transcript_panel(page)
    assert not needs_transcript_panel(Envelope(status="failed", data=None))


def test_needs_browser_stays_false_for_a_panel_answer() -> None:
    assert not needs_browser(video(warnings=["subtitles_blocked"]), unblock=True)


@pytest.mark.parametrize(
    ("enabled", "transcribe", "expected"),
    [(True, False, True), (False, False, False), (True, True, False)],
)
def test_the_panel_applies_with_its_switch_and_without_transcribe(
    enabled: bool, transcribe: bool, expected: bool
) -> None:
    answer = video(warnings=["subtitles_blocked"])
    switch = settings(media_transcript_panel_enabled=enabled)

    assert transcript_panel_applies(answer, switch, transcribe=transcribe) is expected
    assert not transcript_panel_applies(video(warnings=[]), switch, transcribe=transcribe)
