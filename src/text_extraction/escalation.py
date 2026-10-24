"""When a fast result is worth a second try in the browser, and which result to keep.

``needs_browser`` and ``better`` are pure rules over result envelopes; the pipeline that runs
the browser calls them. ``unblock_applies`` says whether unblock mode may be used at all.
"""

from typing import Literal

from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.settings import Settings

# Added to a fast result that stays as the answer because the browser could not be used.
BROWSER_UNAVAILABLE_WARNING = "browser_unavailable"

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

_STATUS_RANK: dict[Literal["success", "partial", "failed"], int] = {
    "failed": 0,
    "partial": 1,
    "success": 2,
}


def _is_ok_html(result: ExtractResult) -> bool:
    media_type = (result.content_type or "").split(";", 1)[0].strip().lower()
    status = result.upstream_status
    return media_type in _HTML_TYPES and status is not None and 200 <= status < 300


def needs_browser(envelope: Envelope[ExtractResult], *, unblock: bool) -> bool:
    """Whether a fast result should be tried again in the browser.

    Only a fast result of a page that was fetched and assessed (``quality`` is set) can escalate.
    It does when the quality gate recommends a browser and the answer is 2xx HTML (a browser
    cannot do better on a text file or an error status). In unblock mode it also does when the
    page was refused as bot protection, or when the body of a 403/429/503 answer is a challenge.
    """
    result = envelope.data
    if result is None or result.mode_used != "fast" or result.quality is None:
        return False
    if result.quality.browser_recommended and _is_ok_html(result):
        return True
    if not unblock:
        return False
    is_bot_protection = (
        envelope.error is not None and envelope.error.code == ErrorCode.BOT_PROTECTION
    )
    return is_bot_protection or result.quality.challenge_status


def _score(envelope: Envelope[ExtractResult]) -> tuple[int, int]:
    return _STATUS_RANK[envelope.status], envelope.data.word_count if envelope.data else 0


def better(
    fast: Envelope[ExtractResult], rendered: Envelope[ExtractResult]
) -> Envelope[ExtractResult]:
    """The result to answer with: the higher status wins (success, partial, failed), then the
    one with more words; on a tie the rendered one."""
    return fast if _score(fast) > _score(rendered) else rendered


def unblock_applies(mode: ComplianceMode, settings: Settings) -> bool:
    """Unblock mode needs the compliance checks switched off and the operator's consent."""
    return mode == "ignore" and settings.browser_unblock_enabled
