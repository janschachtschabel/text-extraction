"""When a fast result is worth a second try in the browser, and which result to keep.

``needs_browser`` and ``better`` are pure rules over result envelopes; the pipeline that runs
the browser calls them. ``unblock_applies`` says whether unblock mode may be used at all.
``needs_transcript_panel`` says when a YouTube video whose subtitles could not be read gets a
second try in YouTube's new transcript view (P13-S1); ``transcript_panel_applies`` adds the
switch and the request's ``transcribe``.
"""

from typing import Final, Literal

from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractResult
from text_extraction.media.panel import UNREAD_SUBTITLES
from text_extraction.settings import Settings

# Added to a fast result that stays as the answer because the browser could not be used.
BROWSER_UNAVAILABLE_WARNING = "browser_unavailable"

# Statuses a site refuses a fetch with that unblock mode tries again in the browser (P12-S1):
# a bot wall's 403, a rate limit's 429, a protection's 503. Their bodies may be a challenge.
REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({403, 429, 503})

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


def _is_refusal(envelope: Envelope[ExtractResult], result: ExtractResult) -> bool:
    """A page answer refused with a 403, 429 or 503, whatever its body or content type."""
    return (
        envelope.error is not None
        and envelope.error.code == ErrorCode.TARGET_HTTP_ERROR
        and result.upstream_status in REFUSAL_STATUSES
    )


def needs_browser(envelope: Envelope[ExtractResult], *, unblock: bool) -> bool:
    """Whether a fast result should be tried again in the browser.

    Only a fast result of a page that was fetched and assessed (``quality`` is set) can escalate,
    never a document's, a platform video's or a media file's.
    It does when the quality gate recommends a browser and the answer is 2xx HTML (a browser
    cannot do better on a text file or an error status). In unblock mode (maximum extraction,
    P12-S1) it also does on every answer refused with a 403, 429 or 503, whatever its body, on
    any 2xx HTML result that is not ``success``, and when the page was refused as bot
    protection. A refusal of a 429 or 503 leaves the host's pause in place: the render takes its
    turn once it has ended (P12-S2).

    A stored result (``cache`` ``hit`` or ``revalidated``) never escalates: only the better of the
    fast and the rendered result is stored, so it is final. A fast result stored while browser
    mode was off escalates only once its entry has expired.
    """
    result = envelope.data
    if result is None or result.mode_used != "fast" or result.quality is None:
        return False
    if result.document is not None:  # a browser renders no better PDF (ruling F9)
        return False
    if result.media is not None and result.media.platform != "html5":
        return False  # a platform video's or a media file's answer is final (M1)
    if result.cache in ("hit", "revalidated"):
        return False
    if unblock and _is_refusal(envelope, result):
        return True  # judged before the recommendation, which a refusal's body seldom has
    if result.quality.browser_recommended and _is_ok_html(result):
        return True
    if not unblock:
        return False
    is_bot_protection = (
        envelope.error is not None and envelope.error.code == ErrorCode.BOT_PROTECTION
    )
    unfinished = envelope.status != "success" and _is_ok_html(result)
    return is_bot_protection or unfinished or result.quality.challenge_status


def needs_transcript_panel(envelope: Envelope[ExtractResult]) -> bool:
    """Whether a fast result is a YouTube video's details whose subtitles could not be read
    (``subtitles_blocked`` or ``subtitles_failed``), to be tried in YouTube's transcript view in
    the browser (P13-S1). A video YouTube says has no subtitles, another platform, a transcript
    and a stored result (``cache`` ``hit`` or ``revalidated``) are not."""
    result = envelope.data
    if result is None or result.mode_used != "fast" or result.media is None:
        return False
    return (
        result.media.platform == "youtube"
        and result.media.transcript_source == "metadata_only"
        and result.cache not in ("hit", "revalidated")
        and not UNREAD_SUBTITLES.isdisjoint(result.warnings)
    )


def transcript_panel_applies(
    envelope: Envelope[ExtractResult], settings: Settings, *, transcribe: bool
) -> bool:
    """``needs_transcript_panel`` while ``MEDIA_TRANSCRIPT_PANEL_ENABLED`` is on, for a request
    without ``transcribe`` (a transcription does better). The callers add ``mode: auto`` and
    ``BROWSER_ENABLED``, as for ``needs_browser``."""
    return (
        settings.media_transcript_panel_enabled
        and not transcribe
        and needs_transcript_panel(envelope)
    )


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


def unblock_for(principal: Principal, mode: ComplianceMode, settings: Settings) -> bool:
    """Whether unblock mode applies to a request of ``principal`` under the resolved ``mode``:
    ``unblock_applies``, and for admins only, even where the operator's default is ``ignore``
    (R9). A job's items run with its owner as the principal."""
    return principal.role == "admin" and unblock_applies(mode, settings)
