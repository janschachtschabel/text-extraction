"""The media branches of the pipeline: platform videos, audio and video files, and the HTML5
players of a page (rulings M1, M2, M5, M6, M8, M9).

``pipeline.py`` keeps the turn, the compliance checks and the cache; ``MediaSteps`` does the
rest:

- ``platform(run, target)``, for a URL of a supported platform (``media/sites.py``), called in
  the turn of the canonical URL's host after that URL passed the compliance checks. The media
  children probe the video (``MediaFetcher.probe``, by the run's deadline). Then, in this order:

  1. YouTube that answered HTTP 429 (``rate_limited``) is reported to the outbound gate as the
     fast fetcher reports a 429, ``Retry-After`` included, so the host pauses; without any
     metadata the answer is ``failed``/``rate_limited``, retryable.
  2. When both libraries failed on YouTube without a refusal of the service and gave no
     metadata, the watch page is fetched with the fast fetcher, in the same turn (its status
     goes to the gate), and its ``ytInitialPlayerResponse`` gives title, description, author,
     duration, date and keywords (M2's last tier 2). A redirect ends that attempt. On Vimeo,
     after an ``extractor_error`` without metadata, the player page is fetched the same way:
     its ``og:`` tags give title and description, and its 404, 410 or a 403 in its own words
     (a removed, private or domain-restricted video) makes the error ``unavailable``, not
     retryable; any other 403 (a block of the service's address) leaves the error as it is.
  3. A target with ``page_fallback`` (a ZDF or 3sat ``.html`` page, which may be an article)
     whose probe found no video (no track, no title, no description, and the extractor said
     so: ``no_video`` or ``unavailable``) is ``NO_VIDEO``: the caller extracts the URL as a
     page. A refusal (``blocked``, ``rate_limited``, ``blocked_address``), a failure that may
     pass (the children, the network, a 5xx) or an ``extractor_error`` is answered as for any
     video.
  3b. A live stream that is still running is ``unsupported_content_type``.
  4. A subtitle track becomes the transcript (``media/captions.py``, ``media/transcript.py``):
     ``# <title>``, ``## <chapter>`` headings, paragraphs; ``content_type`` ``text/vtt``.
     Quality is judged like ``text/plain``.
  5. A proxy refusal of the children (``blocked_address``, the SSRF guard) is ``failed`` with
     ``blocked_url``.
  6. Without a track (or with one that cannot be read) the answer is the metadata
     (``transcript_source`` ``metadata_only``): title and description, one paragraph per line
     of the description, ``content_type`` ``text/html``, ``partial`` with
     ``no_transcript_available``. Only a probe that succeeded and found no usable track says
     the video "has no subtitles" (not retryable), and only that answer is fingerprinted and
     cached; the cache serves it with this error again (``cached_details_only``). A platform
     that refused the service (D1) adds ``subtitles_blocked``; any other
     failure of the probe (``_PROBE_FAILURES``: the children, the network, the track, a limit)
     says what failed, mostly with ``subtitles_failed``, and is retryable when it may pass.
     Neither is compared or cached. Without title and description it is ``failed``: retryable
     after a refusal, else with the failure's own code (``internal_error``, ``dns_error``,
     ``connection_error``, ``too_large`` or ``no_transcript_available``). With
     ``transcribe: true`` this answer is the draft of an ``AsrNeeded`` for the caller to hand
     on, unless the platform says the video is longer than ``MEDIA_MAX_DURATION_SECONDS``: then
     it is answered at once with ``media_too_long`` (``asr_failed``, final, so stored), as the
     ASR worker would answer it.

  The description, uploader, date, keywords and licence go to ``metadata`` (a licence with
  source ``platform`` and confidence ``high``; YouTube's "Creative Commons Attribution license"
  is ``CC-BY-3.0``, the only Creative Commons licence YouTube offers). ``final_url`` is the
  canonical URL; ``url`` stays the request's.
- ``media_file(run, fetched, kind)``, for a fetched body that sniffs as audio or video
  (``media/sniff.py``): ``failed``/``no_transcript_available`` without ``transcribe: true``,
  else an ``AsrNeeded`` without a draft.
- ``tracks(run, html, result)``, after an HTML page's conversion: the ``<track>`` of each
  player (``media/html5.py``), up to ``MEDIA_MAX_TRACKS``, each fetched with the fast fetcher in
  a turn of its host (SSRF checks and gate as for any fetch, no compliance files: a
  sub-resource of a page that passed; a redirect to another host ends it; the page's
  Crawl-delay on the page's host; the wait for the turn and the fetch bounded by the time the
  run has left, so a busy host skips the track instead of failing the page), at most
  ``MEDIA_SUBTITLE_MAX_BYTES``, appended as ``## Transcript`` (with its label). A track that
  fails is skipped; ``tracks_failed`` when every one failed, ``tracks_truncated`` with more
  players than the limit. The players' embeds are listed (D4 a). A page with neither keeps
  ``media: null`` and is returned unchanged; a page judged a challenge, an error page or a
  login wall is left alone.

- ``panel(run, draft, body, warnings=...)``, in the browser worker after a render of a YouTube
  video's watch page with its transcript view steps (P13-S5): ``panel_result`` makes the
  transcript of the ``get_panel`` body (``media/panel.py``) under the draft's title
  (``transcript_source`` ``transcript_panel``, ``mode_used`` ``browser``), judged and
  fingerprinted like any transcript; without text in it, the draft with the warning
  ``transcript_panel_unavailable``. The rendered page itself is never read.

``AsrNeeded`` goes to the run's ``asr_handoff`` once the deadline's steps are done, with
``without_asr``'s answer as the stand-in (never stored); without a handoff that answer is
final: the draft with ``asr_unavailable`` and without its fingerprint, or, for a file,
``failed``/``no_transcript_available``.

The ASR worker's results (``Pipeline.transcribed_result``) are built here as well:
``transcribed`` makes the result of a transcription (the draft's title, metadata, uploader and
languages for a platform video; ``transcript_source: asr`` and ``asr_model``), ``no_speech`` the
answer when the speech recognition heard nothing (the draft, final, or ``failed`` with
``no_transcript_available``), ``asr_failed``
the answer when the transcription failed (the draft with ``asr_unavailable`` and the failure's
error, without its fingerprint when the failure is retryable; without a draft ``failed``).

Metrics (M15), per registry: ``text_extraction_media_extractions_total{platform, source,
outcome}``, ``text_extraction_media_seconds{platform}`` and, one count per transcript view
render, ``text_extraction_media_transcript_panel_total{outcome}`` (``transcript``,
``unavailable``, ``empty``; P13-S7). Logs carry the platform, the host and
codes only: never a URL, a video id or a file name.
"""

import asyncio
import hashlib
import html
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import CollectorRegistry, Counter, Histogram
from pydantic import BaseModel

from text_extraction.compliance import ComplianceReport
from text_extraction.conversion_pool import ConversionPool
from text_extraction.convert import OutputFormat, decode_html
from text_extraction.documents.formats import from_markdown
from text_extraction.documents.pool import ConversionTimeout
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.fetch import Fetcher, FetchError, FetchResult
from text_extraction.fingerprint import compute_fingerprint
from text_extraction.licenses import License, normalize_license
from text_extraction.media.captions import CaptionError, Cue, parse_captions
from text_extraction.media.html5 import PageTrack, page_media, track_format
from text_extraction.media.panel import (
    SUBTITLES_BLOCKED_WARNING,
    SUBTITLES_FAILED_WARNING,
    TRANSCRIPT_PANEL_UNAVAILABLE,
    UNREAD_SUBTITLES,
    PanelError,
    parse_panel,
)
from text_extraction.media.pool import FetchedTrack, MediaFetcher, MediaProbe
from text_extraction.media.select import track_language
from text_extraction.media.sites import MediaTarget
from text_extraction.media.sniff import media_type_of
from text_extraction.media.transcript import Transcription, build_transcript
from text_extraction.media.types import (
    LanguageCode,
    MediaInfo,
    MediaKind,
    Platform,
    TranscriptSource,
)
from text_extraction.metadata import (
    MAX_FIELD_LENGTH,
    MAX_KEYWORDS,
    PageMetadata,
    detect_language,
    page_tree,
)
from text_extraction.outbound import (
    CircuitOpen,
    DomainGate,
    OutboundBusy,
    normalize_host,
    parse_retry_after,
)
from text_extraction.quality import Quality, assess, count_words, quality_error
from text_extraction.settings import Settings

__all__ = [
    "ASR_UNAVAILABLE_WARNING",
    "NO_SPEECH_WARNING",
    "NO_VIDEO",
    "PLAYLIST_MESSAGE",
    "SUBTITLES_BLOCKED_WARNING",
    "SUBTITLES_FAILED_WARNING",
    "TRACKS_FAILED_WARNING",
    "TRACKS_TRUNCATED_WARNING",
    "TRANSCRIBE_WAIT_SECONDS",
    "TRANSCRIPTION_DISABLED_MESSAGE",
    "TRANSCRIPTION_NOT_HANDED_MESSAGE",
    "AsrNeeded",
    "MediaInputMeta",
    "MediaSteps",
    "NoVideo",
    "TranscribedContent",
    "asr_available",
    "asr_failed",
    "cached_details_only",
    "media_too_long_message",
    "no_speech",
    "panel_result",
    "transcribed",
    "transcribed_content",
    "transcription_final",
    "without_asr",
]

_log = structlog.get_logger(__name__)

# ``wait_seconds`` of a request with ``transcribe: true`` that sets none (M9): a video with
# subtitles still answers 200, and a transcription is always a job.
TRANSCRIBE_WAIT_SECONDS: Final = 20
TRANSCRIPTION_DISABLED_MESSAGE: Final = "Transcription is not enabled on this service."
TRANSCRIPTION_NOT_HANDED_MESSAGE: Final = (
    "The audio or video needs transcription, which this extraction cannot hand on."
)
PLAYLIST_MESSAGE: Final = "The URL is a playlist, not one video. Extract the videos one by one."
_LIVE_MESSAGE: Final = "The URL is a live stream that is still running."
ASR_UNAVAILABLE_WARNING: Final = "asr_unavailable"
# The speech recognition ran and heard no speech: the answer is final (also for the cache).
NO_SPEECH_WARNING: Final = "no_speech"
TRACKS_TRUNCATED_WARNING: Final = "tracks_truncated"
TRACKS_FAILED_WARNING: Final = "tracks_failed"

_TRANSCRIBE_HINT: Final = " Send transcribe: true to transcribe it."
_NO_SUBTITLES_MESSAGE: Final = "The video has no subtitles: the answer has its details only."
_BLOCKED_MESSAGE: Final = (
    "The platform refused the subtitles: the answer has the video's details only."
)
_NOTHING_MESSAGE: Final = "The platform refused the subtitles and the video's details."
_NOTHING_AT_ALL_MESSAGE: Final = "The video has no subtitles and no details."
_DETAILS_ONLY: Final = " The answer has the video's details only."
_TRY_AGAIN: Final = " Try again later."
_BLOCKED_ADDRESS_MESSAGE: Final = (
    "The media extraction tried to reach an address that is not public, which is not allowed."
)
_FILE_MESSAGE: Final = "The URL is an audio or video file without subtitles."
_TIMEOUT_MESSAGE: Final = "The video's subtitles did not arrive in time."
_UNAVAILABLE_MESSAGE: Final = "The media extraction is not available. Try again later."
_SITE_NAMES: Final[dict[Platform, str]] = {
    "youtube": "YouTube",
    "vimeo": "Vimeo",
    "ard": "ARD Mediathek",
    "zdf": "ZDF",
    "arte": "ARTE",
    "3sat": "3sat",
}
# Seconds kept between the children's deadline and the extraction's, so the timeout is ours.
_PROBE_MARGIN_SECONDS: Final = 0.5
# A fetch gets the time left minus this; with less than the minimum it is not made.
_FETCH_MARGIN_SECONDS: Final = 0.25
_MIN_SECONDS: Final = 1.0
_SECONDS_BUCKETS: Final = (0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 60)
_PLAYER_RESPONSE: Final = re.compile(r"ytInitialPlayerResponse\s*=\s*\{")
_DATE: Final = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")


@dataclass(frozen=True)
class NoVideo:
    """The answer of ``MediaSteps.platform`` for a ``page_fallback`` target without a video:
    the URL is a page after all."""


NO_VIDEO: Final = NoVideo()
# Probe errors that say a ``page_fallback`` page has no video: the extractor's own words for it
# (``media/pool.py``). A refusal, a failure that may pass, an extractor error that names no
# missing video, or a probe that succeeded says nothing of the kind.
_NO_VIDEO_ERRORS: Final = frozenset({"no_video", "unavailable"})
# What Vimeo's player answers for a removed, private or domain-restricted video. yt-dlp words
# its 403 as a TLS fingerprint block; a 403 counts only with the player's own words, since a
# block of the service's address (a bot check) answers 403 too. The fetcher asks for German
# first, so the player answers in German, else in English.
_GONE_STATUSES: Final = frozenset({404, 410})
_REFUSAL_SCAN_BYTES: Final = 65_536
_PLAYER_REFUSALS: Final = re.compile(
    r"video isn'?t available|privacy settings|nicht verfügbar|datenschutzeinstellungen",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ProbeFailure:
    """How a probe's error code is answered when no transcript came: ``code`` and ``reason``
    without any of the video's details, ``reason`` and ``warning`` with them (``partial``,
    ``no_transcript_available``). Such an answer is never fingerprinted, so never cached."""

    code: ErrorCode
    retryable: bool
    reason: str
    warning: str | None = SUBTITLES_FAILED_WARNING


_UNREADABLE: Final = _ProbeFailure(
    ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "The platform's answer could not be read."
)
_CHILD_FAILED: Final = _ProbeFailure(
    ErrorCode.INTERNAL_ERROR, True, "The media extraction failed on our side."
)
_TRACK_FAILED: Final = _ProbeFailure(
    ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "The subtitles could not be fetched or read."
)
# The probe's error codes (``media/pool.py``) but the refusals (``blocked``, ``rate_limited``,
# ``subtitles_blocked``) and ``blocked_address`` (``blocked_url``); an unknown one reads as
# ``extractor_error``.
_PROBE_FAILURES: Final[dict[str, _ProbeFailure]] = {
    "extractor_error": _UNREADABLE,
    "crashed": _CHILD_FAILED,
    "out_of_memory": _CHILD_FAILED,
    "connect_error": _ProbeFailure(
        ErrorCode.CONNECTION_ERROR, True, "The platform could not be reached."
    ),
    "platform_error": _ProbeFailure(
        ErrorCode.TARGET_HTTP_ERROR, True, "The platform answered with a server error."
    ),
    "no_video": _ProbeFailure(
        ErrorCode.NO_TRANSCRIPT_AVAILABLE, False, "The page has no video.", warning=None
    ),
    "dns_error": _ProbeFailure(
        ErrorCode.DNS_ERROR, True, "The platform's host name could not be resolved."
    ),
    "track_failed": _TRACK_FAILED,
    "empty_track": _ProbeFailure(
        ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "The subtitles came without text."
    ),
    "too_large": _ProbeFailure(
        ErrorCode.TOO_LARGE, False, "The subtitles are larger than MEDIA_SUBTITLE_MAX_BYTES."
    ),
    "bytes_cap": _ProbeFailure(
        ErrorCode.TOO_LARGE, False, "The media extraction needed more than MEDIA_EXTRACT_MAX_BYTES."
    ),
    "unavailable": _ProbeFailure(
        ErrorCode.NO_TRANSCRIPT_AVAILABLE,
        False,
        "The video is private, removed or restricted.",
        warning=None,
    ),
}


def asr_available(settings: Settings) -> bool:
    """Whether the service transcribes: the ASR workers are on, and so are jobs (a
    transcription is always a job)."""
    return settings.asr_enabled and settings.jobs_enabled


class MediaInputMeta(BaseModel):
    """What the ASR worker needs to know about the media of an item; stored as JSON as the
    item's input (with the bytes of an upload, an empty body otherwise)."""

    source: Literal["platform", "file", "upload"]
    platform: Platform
    url: str | None  # the canonical URL of a platform video, the final URL of a file
    extractor: str | None  # the yt-dlp extractor of a platform video
    kind: MediaKind | None  # what the bytes of a file or an upload are
    file_name: str | None  # uploads only
    duration: float | None  # seconds, when the platform said
    languages: list[LanguageCode]
    compliance: ComplianceReport | None  # the checks the URL passed
    attempts: int  # fetches of the target so far


@dataclass(frozen=True)
class AsrNeeded:
    """Media that needs speech recognition. ``draft`` is the metadata result of a platform
    video (None for a file or an upload); it is neither cached nor fingerprinted yet."""

    draft: Envelope[ExtractResult] | None
    input: MediaInputMeta


class MediaRequest(Protocol):
    """The options of a request that the media steps read."""

    @property
    def format(self) -> OutputFormat: ...

    @property
    def metadata(self) -> bool: ...

    @property
    def links(self) -> bool: ...

    @property
    def transcribe(self) -> bool: ...

    @property
    def languages(self) -> list[str]: ...


class MediaRun(Protocol):
    """The part of the pipeline's run state the media steps use."""

    @property
    def req(self) -> MediaRequest: ...

    host: str | None
    report: ComplianceReport
    deadline: asyncio.Timeout | None
    final_url: str | None
    attempts: int
    fetch_ms: int
    convert_ms: int
    warnings: list[str]

    def result(self, **fields: Any) -> ExtractResult: ...

    def failed(
        self, code: ErrorCode, message: str, *, retryable: bool = False
    ) -> Envelope[ExtractResult]: ...


class _Answering(Protocol):
    """What building an answer needs of a run: its ``result`` (an upload's run has no more)."""

    def result(self, **fields: Any) -> ExtractResult: ...


def without_asr(
    needed: AsrNeeded, run: _Answering, *, message: str = TRANSCRIPTION_DISABLED_MESSAGE
) -> Envelope[ExtractResult]:
    """The answer while the transcription is not done: the draft with the warning
    ``asr_unavailable`` and without its fingerprint (the transcription may come next time), or,
    for media without one, ``failed``/``no_transcript_available`` with ``message``."""
    draft = needed.draft
    if draft is None or draft.data is None:
        media = MediaInfo(platform=needed.input.platform, transcript_source=None)
        return _failure(run, ErrorCode.NO_TRANSCRIPT_AVAILABLE, message, media=media)
    data = draft.data.model_copy(
        update={"warnings": [*draft.data.warnings, ASR_UNAVAILABLE_WARNING], "fingerprint": None}
    )
    return draft.model_copy(update={"data": data})


# --- the ASR worker's results ----------------------------------------------------------------


@dataclass(frozen=True)
class TranscribedContent:
    """A transcription in the request's shape: content, quality verdict and language."""

    content: str
    quality: Quality
    language: str | None


def transcribed_content(
    fmt: OutputFormat, transcription: Transcription, title: str | None
) -> TranscribedContent:
    """The transcript of ``transcription`` under ``title`` (``# <title>``, paragraphs); the
    language Whisper heard, else the one the text reads as. CPU-bound: runs on the conversion
    pool."""
    content = build_transcript(transcription.segments, title, [], fmt)
    language = transcription.language or (detect_language(None, None, content) if content else None)
    return TranscribedContent(content, assess(None, content), language)


def transcribed(
    run: _Answering,
    meta: MediaInputMeta,
    draft: Envelope[ExtractResult] | None,
    transcription: Transcription,
    built: TranscribedContent,
    *,
    metadata: bool,
    links: bool,
    kept: bool,
) -> ExtractResult:
    """The result of a transcription (M6): the draft's title, metadata (with the language
    heard), uploader and subtitle languages for a platform video, its warnings but
    ``asr_unavailable``; ``transcript_source: asr`` with the model. ``kept`` (the quality
    verdict did not fail) fingerprints the content."""
    details = draft.data if draft is not None and draft.data is not None else None
    language = built.language
    page: PageMetadata | None = None
    if metadata:
        page = (
            details.metadata.model_copy(update={"language": language})
            if details is not None and details.metadata is not None
            else PageMetadata(language=language)
        )
    known = details.media if details is not None else None
    raw = hashlib.sha256(built.content.encode()).hexdigest()
    warnings = [w for w in (details.warnings if details else []) if w != ASR_UNAVAILABLE_WARNING]
    return run.result(
        content_type=media_type_of(transcription.kind) if transcription.kind else None,
        language=language,
        word_count=built.quality.word_count,
        title=details.title if details is not None else None,
        content=built.content,
        quality=built.quality,
        metadata=page,
        links=[] if links else None,
        fingerprint=compute_fingerprint(raw, built.content) if kept else None,
        warnings=list(dict.fromkeys(warnings)),
        media=MediaInfo(
            platform=meta.platform,
            transcript_source="asr",
            # The language heard, else the one the text reads as (an external endpoint may
            # answer a language's name, which is no code).
            transcript_language=transcription.language or language or None,
            available_languages=known.available_languages if known is not None else [],
            duration_seconds=transcription.duration,
            uploader=known.uploader if known is not None else None,
            asr_model=transcription.model,
        ),
    )


_NO_SPEECH_DETAILS_MESSAGE: Final = (
    "The video has no speech that could be transcribed: the answer has its details only."
)
_NO_SPEECH_MESSAGE: Final = "The audio has no speech that could be transcribed."
_NO_SPEECH_DETAILS_ERROR: Final = ErrorInfo(
    code=ErrorCode.NO_TRANSCRIPT_AVAILABLE, message=_NO_SPEECH_DETAILS_MESSAGE, retryable=False
)
_NOT_TRANSCRIBED_DETAILS_ERROR: Final = ErrorInfo(
    code=ErrorCode.NO_TRANSCRIPT_AVAILABLE,
    message="The video has no subtitles and could not be transcribed: the answer has its "
    "details only.",
    retryable=False,
)


def _details_only_error(message: str, settings: Settings, *, retryable: bool) -> ErrorInfo:
    """The error of a platform video's details without a transcript (M6): ``message``, with
    the hint to send ``transcribe: true`` when the service transcribes."""
    if asr_available(settings):
        message += _TRANSCRIBE_HINT
    return ErrorInfo(code=ErrorCode.NO_TRANSCRIPT_AVAILABLE, message=message, retryable=retryable)


def transcription_final(result: ExtractResult) -> bool:
    """Whether a stored platform video's details say that a transcription will not do better:
    the speech recognition heard no speech (``no_speech``), or it failed for a reason of the
    media's own (``asr_unavailable``). A stored details-only result carries ``asr_unavailable``
    only after such a failure (``asr_failed`` keeps the fingerprint only when the failure is
    ``final``, ``media_too_long`` for example), since every other answer without a transcription
    drops its fingerprint and is never stored (``without_asr``, a retryable or a caller's
    ``asr_failed`` such as a refused key, an expired input). So the marker of a stored result is
    that warning, as ``no_speech`` is."""
    return NO_SPEECH_WARNING in result.warnings or ASR_UNAVAILABLE_WARNING in result.warnings


def media_too_long_message(duration: float | None, limit: int) -> str:
    """The message of ``media_too_long`` for media ``duration`` seconds long."""
    length = f"is {round(duration)} s long, " if duration is not None else "is "
    return (
        f"The media {length}longer than the {limit} s that are transcribed "
        "(MEDIA_MAX_DURATION_SECONDS)."
    )


def cached_details_only(
    result: ExtractResult, settings: Settings
) -> Envelope[ExtractResult] | None:
    """The envelope of a stored platform video's details without a transcript
    (``metadata_only``): ``partial`` with ``no_transcript_available``, never a quality verdict;
    None for any other result. Three such answers are stored (the others carry no
    fingerprint), and each is served with its fresh answer's message:

    - the video without subtitles (with the hint to send ``transcribe: true`` when the service
      transcribes);
    - with ``no_speech``, the one the speech recognition heard nothing in;
    - with ``asr_unavailable``, the draft of a transcription that failed for a reason of the
      media's own (``asr_failed`` with ``final``, for example ``media_too_long``): the warning
      belonged to the item that tried and is dropped, and the message says the video could not
      be transcribed instead of asking for ``transcribe: true`` again. The failure's own code
      is not stored."""
    if result.media is None or result.media.transcript_source != "metadata_only":
        return None
    if NO_SPEECH_WARNING in result.warnings:
        error = _NO_SPEECH_DETAILS_ERROR
    elif transcription_final(result):  # failed for good
        error = _NOT_TRANSCRIBED_DETAILS_ERROR
        warnings = [w for w in result.warnings if w != ASR_UNAVAILABLE_WARNING]
        result = result.model_copy(update={"warnings": warnings})
    else:
        error = _details_only_error(_NO_SUBTITLES_MESSAGE, settings, retryable=False)
    return Envelope(status="partial", data=result, error=error)


def no_speech(
    run: _Answering,
    meta: MediaInputMeta,
    draft: Envelope[ExtractResult] | None,
    transcription: Transcription,
) -> Envelope[ExtractResult]:
    """The answer when the speech recognition ran and heard nothing: the draft (a platform
    video's details) with ``no_transcript_available``, final, so it keeps its fingerprint and
    no ``asr_unavailable``; without a draft ``failed`` with the same code, not retryable. Both
    carry the warning ``no_speech``: a stored details-only answer with it is a cache hit for a
    later ``transcribe: true`` request (the speech recognition would hear nothing again)."""
    if draft is not None and draft.data is not None:
        kept = [w for w in draft.data.warnings if w != ASR_UNAVAILABLE_WARNING]
        warnings = list(dict.fromkeys([*kept, NO_SPEECH_WARNING]))
        return draft.model_copy(
            update={
                "data": draft.data.model_copy(update={"warnings": warnings}),
                "error": _NO_SPEECH_DETAILS_ERROR,
            }
        )
    media = MediaInfo(
        platform=meta.platform,
        transcript_source="asr",
        duration_seconds=transcription.duration,
        asr_model=transcription.model,
    )
    result = run.result(
        media=media,
        content_type=media_type_of(transcription.kind) if transcription.kind else None,
    )
    warnings = list(dict.fromkeys([*result.warnings, NO_SPEECH_WARNING]))
    result = result.model_copy(update={"warnings": warnings})
    return _failed(result, ErrorCode.NO_TRANSCRIPT_AVAILABLE, _NO_SPEECH_MESSAGE, False)


def asr_failed(
    run: _Answering,
    meta: MediaInputMeta,
    draft: Envelope[ExtractResult] | None,
    error: ErrorInfo,
    *,
    upstream_status: int | None = None,
    final: bool = False,
) -> Envelope[ExtractResult]:
    """The answer when the transcription failed with ``error``: the draft (a platform video's
    details) keeps its status, adds ``asr_unavailable`` and takes the error, and keeps its
    fingerprint (so it is stored as the video's answer) only when the failure is ``final``:
    it belongs to the media (``media_too_long``, undecodable, the item's own budget) and is not
    retryable. A failure of the caller or of the endpoint (a refused or missing key) is never
    final, retryable or not: the result cache is shared by every caller. Without a draft
    ``failed`` with the error and ``upstream_status``."""
    if draft is not None and draft.data is not None:
        update: dict[str, Any] = {
            "warnings": list(dict.fromkeys([*draft.data.warnings, ASR_UNAVAILABLE_WARNING]))
        }
        if error.retryable or not final:
            update["fingerprint"] = None
        answered = error.model_copy(update={"message": error.message + _DETAILS_ONLY})
        return draft.model_copy(
            update={"data": draft.data.model_copy(update=update), "error": answered}
        )
    media = MediaInfo(platform=meta.platform, transcript_source=None)
    result = run.result(media=media, upstream_status=upstream_status)
    return _failed(result, error.code, error.message, error.retryable)


# --- YouTube's transcript view (P13-S5) -------------------------------------------------------

_PanelOutcome = Literal["transcript", "unavailable", "empty"]


def panel_result(
    draft: Envelope[ExtractResult],
    cues: Sequence[Cue] | None,
    fmt: OutputFormat,
    *,
    warnings: Sequence[str],
) -> Envelope[ExtractResult]:
    """The answer of a transcript view render for ``draft``, a YouTube video's details whose
    subtitles could not be read (P13-S5). ``warnings`` are the render's.

    With cues that have text (and without ``transcript_panel_unavailable`` in ``warnings``):
    the draft's result with the transcript under the draft's title as its content (``text/vtt``,
    ``mode_used`` ``browser``, ``transcript_source`` ``transcript_panel``), its quality, words
    and language read from that content, the draft's warnings without ``subtitles_blocked`` and
    ``subtitles_failed``, and a fingerprint of the cues' text. The envelope is ``success``
    without an error: the caller judges it by the quality verdict. The render's warnings say
    how the page went, not the transcript, and are left out.

    Otherwise the draft with the warning ``transcript_panel_unavailable`` and without a
    fingerprint: never stored. The page's own text is never part of either. Pure; CPU-bound
    (runs on the conversion pool)."""
    data = draft.data
    if data is None:  # pragma: no cover - only a draft with data needs the panel
        return draft
    texts = [cue.text for cue in cues or [] if cue.text.strip()]
    if cues is None or not texts or TRANSCRIPT_PANEL_UNAVAILABLE in warnings:
        noted = list(dict.fromkeys([*data.warnings, TRANSCRIPT_PANEL_UNAVAILABLE]))
        return draft.model_copy(
            update={"data": data.model_copy(update={"warnings": noted, "fingerprint": None})}
        )
    content = build_transcript(cues, data.title, [], fmt)
    quality = assess(None, content)
    language = detect_language(None, None, content)
    raw = hashlib.sha256("\n".join(texts).encode()).hexdigest()
    media = data.media or MediaInfo(platform="youtube", transcript_source=None)
    result = data.model_copy(
        update={
            "content": content,
            "content_type": "text/vtt",
            "quality": quality,
            "word_count": quality.word_count,
            "language": language,
            "mode_used": "browser",
            "unblocked": False,
            "metadata": (
                data.metadata.model_copy(update={"language": language})
                if data.metadata is not None
                else None
            ),
            "fingerprint": compute_fingerprint(raw, content),
            "warnings": [w for w in data.warnings if w not in UNREAD_SUBTITLES],
            "media": media.model_copy(
                update={"transcript_source": "transcript_panel", "transcript_language": language}
            ),
        }
    )
    return Envelope(status="success", data=result)


def _panel_answer(
    draft: Envelope[ExtractResult],
    body: bytes | None,
    fmt: OutputFormat,
    warnings: Sequence[str],
    max_bytes: int,
) -> tuple[Envelope[ExtractResult], _PanelOutcome]:
    """``panel_result`` for the ``get_panel`` body (None: none could be read) and the outcome
    to count. CPU-bound: runs on the conversion pool."""
    cues: list[Cue] | None = None
    if body is not None:
        try:
            cues = parse_panel(body, max_bytes=max_bytes)
        except PanelError:
            _log.info("media_transcript_panel_unreadable", bytes=len(body))
    answer = panel_result(draft, cues, fmt, warnings=warnings)
    media = answer.data.media if answer.data is not None else None
    if media is not None and media.transcript_source == "transcript_panel":
        return answer, "transcript"
    if cues is None or TRANSCRIPT_PANEL_UNAVAILABLE in warnings:
        return answer, "unavailable"
    return answer, "empty"


@dataclass(frozen=True)
class _Metrics:
    extractions: Counter
    seconds: Histogram
    panel: Counter


_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            extractions=Counter(
                "text_extraction_media_extractions_total",
                "Media extractions by platform, transcript source (none without a transcript) "
                "and outcome (success, partial, asr_needed, page: no video, extracted as a page, "
                "or the error code).",
                ["platform", "source", "outcome"],
                registry=registry,
            ),
            seconds=Histogram(
                "text_extraction_media_seconds",
                "Duration of media extractions (the probe, a watch page, the tracks).",
                ["platform"],
                buckets=_SECONDS_BUCKETS,
                registry=registry,
            ),
            panel=Counter(
                "text_extraction_media_transcript_panel_total",
                "Renders of YouTube's transcript view by outcome (transcript; unavailable: no "
                "answer or one that could not be read; empty: an answer without text).",
                ["outcome"],
                registry=registry,
            ),
        )
        _METRICS[registry] = metrics
    return metrics


@dataclass(frozen=True)
class _Built:
    content: str
    quality: Quality
    language: str | None


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


class MediaSteps:
    """The media steps of one pipeline (see the module docstring). ``media_fetcher`` probes
    platform videos, ``fetcher`` and ``gate`` fetch watch pages and tracks, ``envelope`` turns a
    result into its envelope (the quality verdict decides the status), ``threads`` runs the
    CPU-bound steps."""

    def __init__(
        self,
        media_fetcher: MediaFetcher,
        fetcher: Fetcher,
        gate: DomainGate,
        settings: Settings,
        envelope: Callable[[ExtractResult], Envelope[ExtractResult]],
        registry: CollectorRegistry,
        *,
        threads: ConversionPool,
    ) -> None:
        self._media = media_fetcher
        self._fetcher = fetcher
        self._gate = gate
        self._settings = settings
        self._judge = envelope
        self._threads = threads
        self._metrics = _metrics_for(registry)

    # --- platform videos -------------------------------------------------------------------

    async def platform(
        self, run: MediaRun, target: MediaTarget
    ) -> Envelope[ExtractResult] | AsrNeeded | NoVideo:
        """The answer for a platform video, in the turn of its canonical host (held by the
        caller), an ``AsrNeeded``, or ``NO_VIDEO`` for a page without one."""
        started = time.perf_counter()
        outcome = await self._platform(run, target)
        self._observe(target.platform, outcome, started)
        return outcome

    async def _platform(
        self, run: MediaRun, target: MediaTarget
    ) -> Envelope[ExtractResult] | AsrNeeded | NoVideo:
        deadline = self._deadline(run)
        if deadline - asyncio.get_running_loop().time() < _MIN_SECONDS:
            return run.failed(
                ErrorCode.TIMEOUT, "Too little time was left to read the video.", retryable=True
            )
        run.attempts += 1
        started = time.perf_counter()
        try:
            probe = await self._media.probe(
                target, list(run.req.languages), deadline - _PROBE_MARGIN_SECONDS
            )
        except ConversionTimeout:
            return run.failed(ErrorCode.TIMEOUT, _TIMEOUT_MESSAGE, retryable=True)
        except RuntimeError as error:  # the media children are shut down or do not start
            _log.error(
                "media_fetcher_unavailable",
                platform=target.platform,
                error_type=type(error).__name__,
            )
            return run.failed(ErrorCode.INTERNAL_ERROR, _UNAVAILABLE_MESSAGE, retryable=True)
        finally:
            run.fetch_ms += _ms(started)
        host = urlsplit(target.canonical_url).hostname or ""
        if probe.rate_limited:
            # As the fast fetcher reports a 429, while the turn is held: the host pauses.
            retry_after = parse_retry_after(probe.retry_after, now=time.time())
            await self._gate.record(host, status=429, retry_after=retry_after)
        elif (
            target.platform == "youtube"
            and probe.track is None
            and probe.title is None
            and not probe.blocked
            and probe.error != "blocked_address"
        ) or (
            target.platform == "vimeo"
            and probe.track is None
            and probe.title is None
            and probe.error == "extractor_error"
        ):
            probe = await self._watch_page(run, target, probe)
        if probe.title is None and probe.description is None and probe.rate_limited:
            seconds = parse_retry_after(probe.retry_after, now=time.time())
            after = f" Retry after {max(round(seconds), 1)} s." if seconds is not None else ""
            return run.failed(
                ErrorCode.RATE_LIMITED,
                f"The platform asked for fewer requests.{after}",
                retryable=True,
            )
        if target.page_fallback and _no_video(probe):
            _log.info("media_no_video", platform=target.platform, error_code=probe.error)
            return NO_VIDEO
        if probe.is_live:
            return run.failed(ErrorCode.UNSUPPORTED_CONTENT_TYPE, _LIVE_MESSAGE)
        if probe.track is not None:
            transcript = await self._transcript(run, target, probe, probe.track)
            if transcript is not None:
                return transcript
            probe = replace(probe, track=None, error="track_failed")  # it had no readable cue
        if probe.error == "blocked_address" and not (probe.blocked or probe.rate_limited):
            # The proxy refused a request of the children: an SSRF guard, not the platform.
            return run.failed(ErrorCode.BLOCKED_URL, _BLOCKED_ADDRESS_MESSAGE)
        draft = await self._metadata_only(run, target, probe)
        if not run.req.transcribe:
            return draft
        meta = MediaInputMeta(
            source="platform",
            platform=target.platform,
            url=target.canonical_url,
            extractor=target.extractor,
            kind=None,
            file_name=None,
            duration=probe.duration,
            languages=list(run.req.languages),
            compliance=run.report,
            attempts=run.attempts,
        )
        limit = self._settings.media_max_duration_seconds
        if probe.duration is not None and probe.duration > limit:
            # Known too long: answered now as the ASR worker would, without a job. Final, so
            # the details keep their fingerprint and the next request is a hit.
            message = media_too_long_message(probe.duration, limit)
            too_long = ErrorInfo(code=ErrorCode.MEDIA_TOO_LONG, message=message, retryable=False)
            return asr_failed(run, meta, draft, too_long, final=True)
        return AsrNeeded(draft=draft, input=meta)

    async def _transcript(
        self, run: MediaRun, target: MediaTarget, probe: MediaProbe, track: FetchedTrack
    ) -> Envelope[ExtractResult] | None:
        """The transcript result of the track; None when the track has no readable cue."""
        started = time.perf_counter()
        try:
            built = await self._threads.run(self._built_transcript, run.req.format, probe, track)
        finally:
            run.convert_ms += _ms(started)
        if built is None:
            return None
        failed = self._fails(built.quality)
        result = self._result(
            run,
            target,
            probe,
            built,
            content_type="text/vtt",
            source=track.source,
            transcript_language=track.language,
            fingerprint=None if failed else hashlib.sha256(track.body).hexdigest(),
            warnings=[],
        )
        return self._judge(result)

    def _built_transcript(
        self, fmt: OutputFormat, probe: MediaProbe, track: FetchedTrack
    ) -> _Built | None:
        """Cues, transcript and quality of a track. CPU-bound: runs on the conversion pool."""
        try:
            cues = parse_captions(
                track.body,
                track.format,
                track.charset,
                max_bytes=self._settings.media_subtitle_max_bytes,
            )
        except CaptionError:
            _log.info("media_track_unreadable", format=track.format)
            return None
        content = build_transcript(cues, probe.title, probe.chapters, fmt)
        if not any(cue.text.strip() for cue in cues):
            return None
        language = track.language or probe.original_language
        return _Built(content, assess(None, content), language)

    async def _metadata_only(
        self, run: MediaRun, target: MediaTarget, probe: MediaProbe
    ) -> Envelope[ExtractResult]:
        """The answer without a transcript: title and description (M6)."""
        started = time.perf_counter()
        try:
            built = await self._threads.run(_built_metadata, run.req.format, probe)
        finally:
            run.convert_ms += _ms(started)
        refused = probe.blocked or probe.rate_limited
        failure = (
            None
            if refused or probe.error is None
            else _PROBE_FAILURES.get(probe.error, _UNREADABLE)
        )
        warnings = [SUBTITLES_BLOCKED_WARNING] if refused else []
        if failure is not None and failure.warning is not None:
            warnings.append(failure.warning)
        # A refusal may lift and a failure may pass: such an answer is neither compared nor
        # cached. Only a probe that succeeded without a usable track is.
        kept = not refused and failure is None
        fingerprint = hashlib.sha256(built.content.encode()).hexdigest() if kept else None
        result = self._result(
            run,
            target,
            probe,
            built,
            content_type="text/html",
            source="metadata_only",
            transcript_language=None,
            fingerprint=fingerprint if built.content else None,
            warnings=warnings,
        )
        if not built.content:
            if failure is not None:
                message = failure.reason + (_TRY_AGAIN if failure.retryable else "")
                return _failed(result, failure.code, message, failure.retryable)
            message = _NOTHING_MESSAGE if refused else _NOTHING_AT_ALL_MESSAGE
            return _failed(result, ErrorCode.NO_TRANSCRIPT_AVAILABLE, message, refused)
        retryable = False
        if refused:
            message = _BLOCKED_MESSAGE
        elif failure is not None:
            message = failure.reason + _DETAILS_ONLY
            retryable = failure.retryable
        else:
            message = _NO_SUBTITLES_MESSAGE
        error = _details_only_error(message, self._settings, retryable=retryable)
        return Envelope(status="partial", data=result, error=error)

    async def panel(
        self,
        run: MediaRun,
        draft: Envelope[ExtractResult],
        body: bytes | None,
        *,
        warnings: Sequence[str],
    ) -> Envelope[ExtractResult]:
        """The answer of a transcript view render for ``draft`` (P13-S5): ``body`` (the
        ``get_panel`` answer, None when the render could not read one) is parsed on the
        conversion pool, ``panel_result`` makes the answer, and a transcript is judged by the
        quality verdict (one that fails loses its fingerprint). Counted once in
        ``text_extraction_media_transcript_panel_total`` (P13-S7)."""
        started = time.perf_counter()
        try:
            answer, outcome = await self._threads.run(
                _panel_answer,
                draft,
                body,
                run.req.format,
                tuple(warnings),
                self._settings.media_subtitle_max_bytes,
            )
        finally:
            run.convert_ms += _ms(started)
        self._metrics.panel.labels(outcome=outcome).inc()
        _log.info("media_transcript_panel", outcome=outcome)
        result = answer.data
        if outcome != "transcript" or result is None or result.quality is None:
            return answer
        if self._fails(result.quality):
            result = result.model_copy(update={"fingerprint": None})
        return self._judge(result)

    def _result(
        self,
        run: MediaRun,
        target: MediaTarget,
        probe: MediaProbe,
        built: _Built,
        *,
        content_type: str,
        source: TranscriptSource,
        transcript_language: str | None,
        fingerprint: str | None,
        warnings: list[str],
    ) -> ExtractResult:
        """A platform video's result; ``fingerprint`` is the raw hash to fingerprint with, None
        for none."""
        req = run.req
        language = built.language
        if language is None and built.content:
            language = detect_language(None, None, built.content)
        return run.result(
            final_url=target.canonical_url,
            content_type=content_type,
            language=language,
            word_count=built.quality.word_count,
            title=probe.title,
            content=built.content,
            quality=built.quality,
            metadata=_platform_metadata(target, probe, language) if req.metadata else None,
            links=[] if req.links else None,
            fingerprint=(
                compute_fingerprint(fingerprint, built.content) if fingerprint is not None else None
            ),
            warnings=[*run.warnings, *warnings],
            media=MediaInfo(
                platform=target.platform,
                transcript_source=source,
                transcript_language=transcript_language,
                available_languages=_languages(probe.manual),
                duration_seconds=probe.duration,
                uploader=probe.uploader,
            ),
        )

    async def _watch_page(
        self, run: MediaRun, target: MediaTarget, probe: MediaProbe
    ) -> MediaProbe:
        """The probe with what the video's page says (YouTube's watch page, Vimeo's player),
        fetched in the held turn; the probe unchanged when the page cannot be had or says
        nothing. A 429 marks it rate-limited; Vimeo's player refusing the video (404, 410, a 403
        in its own words) marks it unavailable."""
        host = urlsplit(target.canonical_url).hostname or ""
        budget = self._fetch_budget(run)
        if budget < _MIN_SECONDS:
            return probe

        async def no_redirect(_url: str) -> None:
            raise FetchError(ErrorCode.TARGET_HTTP_ERROR, "The watch page redirected.")

        run.attempts += 1
        started = time.perf_counter()
        try:
            fetched = await self._fetcher.fetch(
                target.canonical_url,
                max_bytes=self._settings.convert_max_bytes,
                timeout_seconds=budget,
                on_redirect=no_redirect,
            )
        except FetchError as error:
            await self._record_failure(host, error)
            _log.info("media_watch_page_failed", host=host, error_code=error.code.value)
            return probe
        finally:
            run.fetch_ms += _ms(started)
        await self._record(host, fetched)
        if fetched.status == 429:
            return replace(probe, rate_limited=True, retry_after=fetched.headers.get("retry-after"))
        if target.platform == "vimeo" and _player_refused(fetched):
            return replace(probe, error="unavailable")
        if not 200 <= fetched.status < 300:
            return probe
        parse = player_details if target.platform == "youtube" else og_details
        started = time.perf_counter()
        try:
            details = await self._threads.run(parse, fetched.content, fetched.charset)
        finally:
            run.convert_ms += _ms(started)
        return replace(probe, **details) if details else probe

    # --- media files -----------------------------------------------------------------------

    async def media_file(
        self, run: MediaRun, fetched: FetchResult, kind: MediaKind
    ) -> Envelope[ExtractResult] | AsrNeeded:
        """A fetched audio or video file (its header checks passed): an ``AsrNeeded`` with
        ``transcribe: true``, else ``failed``/``no_transcript_available``."""
        started = time.perf_counter()
        outcome: Envelope[ExtractResult] | AsrNeeded
        if run.req.transcribe:
            outcome = AsrNeeded(
                draft=None,
                input=MediaInputMeta(
                    source="file",
                    platform="file",
                    url=fetched.final_url,
                    extractor=None,
                    kind=kind,
                    file_name=None,
                    duration=None,
                    languages=list(run.req.languages),
                    compliance=run.report,
                    attempts=run.attempts,
                ),
            )
        else:
            message = _FILE_MESSAGE + (
                _TRANSCRIBE_HINT
                if asr_available(self._settings)
                else f" {TRANSCRIPTION_DISABLED_MESSAGE}"
            )
            media = MediaInfo(platform="file", transcript_source=None)
            outcome = _failure(run, ErrorCode.NO_TRANSCRIPT_AVAILABLE, message, media=media)
        self._observe("file", outcome, started)
        return outcome

    # --- HTML5 tracks and embeds -----------------------------------------------------------

    async def tracks(
        self,
        run: MediaRun,
        html: str,
        result: ExtractResult,
        *,
        raw_sha256: str | None = None,
    ) -> ExtractResult:
        """``result`` (an HTML page's, not yet judged) with its players' transcripts appended
        and ``media`` set; unchanged when the page has neither tracks nor embeds, or is a
        challenge, an error page or a login wall. ``raw_sha256`` is the hash of the page's
        bytes, for the fingerprint of the longer content."""
        if result.content is None or not self._worth_reading(result.quality):
            return result
        started = time.perf_counter()
        base = result.final_url or run.final_url or ""
        limit = self._settings.media_max_tracks
        found = await self._threads.run(page_media, html, base, list(run.req.languages), limit)
        run.convert_ms += _ms(started)
        if found.empty:
            return result
        sections: list[str] = []
        language: str | None = None
        for track in found.tracks:
            section = await self._track_section(run, track)
            if section:
                sections.append(section)
                language = language or track.language
        warnings = list(result.warnings)
        if found.truncated and limit > 0:
            warnings.append(TRACKS_TRUNCATED_WARNING)
        if found.tracks and not sections:
            warnings.append(TRACKS_FAILED_WARNING)
        update: dict[str, Any] = {
            "warnings": warnings,
            "media": MediaInfo(
                platform="html5",
                transcript_source="manual_subtitles" if sections else None,
                transcript_language=language,
                available_languages=found.track_languages,
                embeds=found.embeds,
            ),
        }
        if sections:
            update.update(self._appended(result, sections, raw_sha256))
        if found.tracks:
            self._observe_tracks(update["media"], sections, started)
        return result.model_copy(update=update)

    def _appended(
        self, result: ExtractResult, sections: list[str], raw_sha256: str | None
    ) -> dict[str, Any]:
        """Content, word count, quality and fingerprint once the sections follow the page."""
        content = _append(result.content or "", sections, result.format)
        word_count = result.word_count + sum(count_words(section) for section in sections)
        quality = (
            result.quality.model_copy(update={"word_count": word_count})
            if result.quality is not None
            else None
        )
        raw = raw_sha256 or (result.fingerprint.raw_sha256 if result.fingerprint else None)
        fingerprint = (
            None
            if raw is None or (quality is not None and self._fails(quality))
            else compute_fingerprint(raw, content)
        )
        return {
            "content": content,
            "word_count": word_count,
            "quality": quality,
            "fingerprint": fingerprint,
        }

    async def _track_section(self, run: MediaRun, track: PageTrack) -> str | None:
        """The ``## Transcript`` section of a track in the request's format; None when the
        track cannot be fetched or read."""
        fetched = await self._fetch_track(run, track)
        if fetched is None:
            return None
        started = time.perf_counter()
        try:
            return await self._threads.run(
                partial(self._section, fetched.content, fetched.charset, track, run.req.format)
            )
        finally:
            run.convert_ms += _ms(started)

    def _section(
        self, body: bytes, charset: str | None, track: PageTrack, fmt: OutputFormat
    ) -> str | None:
        """CPU-bound: runs on the conversion pool."""
        try:
            cues = parse_captions(
                body, track_format(body), charset, max_bytes=self._settings.media_subtitle_max_bytes
            )
        except CaptionError:
            return None
        return _track_section(cues, track.label, fmt)

    async def _fetch_track(self, run: MediaRun, track: PageTrack) -> FetchResult | None:
        """The track's answer, fetched in a turn of its host; None when it failed. The wait
        for the turn and the fetch together get the time the run has left (minus a margin), so
        a busy host costs the track, never the page. A track on the page's host keeps the
        robots.txt Crawl-delay the page's turns keep."""
        host = urlsplit(track.url).hostname or ""
        budget = self._fetch_budget(run)
        if budget < _MIN_SECONDS:
            return None
        page_host = normalize_host(urlsplit(run.final_url or "").hostname or "")
        crawl_delay = run.report.crawl_delay if normalize_host(host) == page_host else None

        async def same_host(url: str) -> None:
            if normalize_host(urlsplit(url).hostname or "") != normalize_host(host):
                raise FetchError(ErrorCode.TARGET_HTTP_ERROR, "The track redirected elsewhere.")

        loop = asyncio.get_running_loop()
        bound = asyncio.timeout(budget)
        started = time.perf_counter()
        try:
            async with bound, self._gate.slot(host, crawl_delay):
                when = bound.when()
                left = (when if when is not None else loop.time()) - loop.time()
                if left <= 0:
                    _log.info("media_track_failed", host=host, error_code="timeout")
                    return None
                try:
                    fetched = await self._fetcher.fetch(
                        track.url,
                        max_bytes=self._settings.media_subtitle_max_bytes,
                        timeout_seconds=left,
                        on_redirect=same_host,
                    )
                except FetchError as error:
                    await self._record_failure(host, error)
                    _log.info("media_track_failed", host=host, error_code=error.code.value)
                    return None
                await self._record(host, fetched)
        except (OutboundBusy, CircuitOpen):
            _log.info("media_track_failed", host=host, error_code=ErrorCode.RATE_LIMITED.value)
            return None
        except TimeoutError:
            if not bound.expired():
                raise
            _log.info("media_track_failed", host=host, error_code=ErrorCode.TIMEOUT.value)
            return None
        finally:
            run.fetch_ms += _ms(started)
        if not 200 <= fetched.status < 300:
            _log.info("media_track_failed", host=host, upstream_status=fetched.status)
            return None
        return fetched

    # --- helpers ---------------------------------------------------------------------------

    def _worth_reading(self, quality: Quality | None) -> bool:
        """A page whose verdict is a failure other than its length (a challenge, an error page,
        a login wall) is not worth its tracks."""
        if quality is None:
            return True
        verdict = quality_error(quality, self._settings.quality_min_words)
        return verdict is None or verdict[0] != "failed" or verdict[1] == ErrorCode.EMPTY_CONTENT

    def _fails(self, quality: Quality) -> bool:
        verdict = quality_error(quality, self._settings.quality_min_words)
        return verdict is not None and verdict[0] == "failed"

    def _deadline(self, run: MediaRun) -> float:
        when = run.deadline.when() if run.deadline is not None else None
        if when is None:
            when = asyncio.get_running_loop().time() + self._settings.extract_timeout_seconds
        return when

    def _fetch_budget(self, run: MediaRun) -> float:
        left = self._deadline(run) - asyncio.get_running_loop().time() - _FETCH_MARGIN_SECONDS
        return min(self._settings.fetch_timeout_seconds, left)

    async def _record(self, host: str, fetched: FetchResult) -> None:
        retry_after = parse_retry_after(fetched.headers.get("retry-after"), now=time.time())
        await self._gate.record(host, status=fetched.status, retry_after=retry_after)

    async def _record_failure(self, host: str, error: FetchError) -> None:
        """An answer's status counts; DNS and connection errors count as the host's failures."""
        if error.upstream_status is not None:
            await self._gate.record(host, status=error.upstream_status, retry_after=None)
        elif error.code in (ErrorCode.DNS_ERROR, ErrorCode.CONNECTION_ERROR):
            await self._gate.record(host, status=None, retry_after=None)

    def _observe(
        self,
        platform: Platform,
        outcome: Envelope[ExtractResult] | AsrNeeded | NoVideo,
        started: float,
    ) -> None:
        if isinstance(outcome, AsrNeeded):
            source, label = "none", "asr_needed"
        elif isinstance(outcome, NoVideo):
            source, label = "none", "page"
        else:
            media = outcome.data.media if outcome.data is not None else None
            source = (media.transcript_source if media is not None else None) or "none"
            label = (
                outcome.error.code.value
                if outcome.status == "failed" and outcome.error is not None
                else outcome.status
            )
        self._metrics.extractions.labels(platform=platform, source=source, outcome=label).inc()
        self._metrics.seconds.labels(platform=platform).observe(time.perf_counter() - started)

    def _observe_tracks(self, media: MediaInfo, sections: list[str], started: float) -> None:
        source = media.transcript_source or "none"
        outcome = "success" if sections else TRACKS_FAILED_WARNING
        self._metrics.extractions.labels(platform="html5", source=source, outcome=outcome).inc()
        self._metrics.seconds.labels(platform="html5").observe(time.perf_counter() - started)


# --- results ---------------------------------------------------------------------------------


def _failure(
    run: _Answering, code: ErrorCode, message: str, *, media: MediaInfo, retryable: bool = False
) -> Envelope[ExtractResult]:
    return _failed(run.result(media=media), code, message, retryable)


def _failed(
    result: ExtractResult, code: ErrorCode, message: str, retryable: bool
) -> Envelope[ExtractResult]:
    error = ErrorInfo(code=code, message=message, retryable=retryable)
    return Envelope(status="failed", data=result, error=error)


def _no_video(probe: MediaProbe) -> bool:
    """Whether the probe found no video at all, and was not refused or cut short."""
    return (
        probe.track is None
        and probe.title is None
        and probe.description is None
        and not probe.blocked
        and not probe.rate_limited
        and not probe.is_live
        and probe.error in _NO_VIDEO_ERRORS
    )


def _built_metadata(fmt: OutputFormat, probe: MediaProbe) -> _Built:
    """Title and description in the content's shape: the description's lines are paragraphs.
    CPU-bound: runs on the conversion pool."""
    lines = [line for line in (probe.description or "").splitlines() if line.strip()]
    # One cue per line, far enough apart that each one is a paragraph of its own.
    cues = [Cue(index * 10.0, index * 10.0 + 1, line) for index, line in enumerate(lines)]
    content = build_transcript(cues, probe.title, [], fmt)
    return _Built(content, assess(None, content), probe.original_language)


def _platform_metadata(
    target: MediaTarget, probe: MediaProbe, language: str | None
) -> PageMetadata:
    license = platform_license(target.platform, probe.license)
    return PageMetadata(
        title=probe.title[:MAX_FIELD_LENGTH] if probe.title else None,
        description=probe.description[:MAX_FIELD_LENGTH] if probe.description else None,
        authors=[probe.uploader[:MAX_FIELD_LENGTH]] if probe.uploader else [],
        published=_iso_date(probe.upload_date),
        site_name=_SITE_NAMES.get(target.platform),
        canonical_url=target.canonical_url,
        language=language,
        keywords=list(dict.fromkeys(tag[:200] for tag in probe.tags if tag.strip()))[:MAX_KEYWORDS],
        license=license,
        licenses=[license] if license is not None else [],
    )


def platform_license(platform: Platform, value: str | None) -> License | None:
    """The licence a platform states, with source ``platform`` and confidence ``high``.
    YouTube's only Creative Commons licence ("Creative Commons Attribution license (reuse
    allowed)") is CC BY 3.0."""
    if not value or not value.strip():
        return None
    license_id = normalize_license(value)
    if license_id is None and platform == "youtube" and "creative commons" in value.lower():
        license_id = "CC-BY-3.0"
    return License(
        id=license_id, url=None, label=value.strip()[:200], source="platform", confidence="high"
    )


def _iso_date(value: str | None) -> str | None:
    match = _DATE.match(value or "")
    return f"{match[1]}-{match[2]}-{match[3]}" if match else None


def _languages(manual: dict[str, Any]) -> list[str]:
    """The languages of the manual tracks, without repeats."""
    found: dict[str, None] = {}
    for key in manual:
        language = track_language(key)
        if language is not None:
            found.setdefault(language)
    return list(found)


def _track_section(cues: Sequence[Cue], label: str | None, fmt: OutputFormat) -> str | None:
    """``## Transcript`` (with the label) and the paragraphs of a track, in ``fmt``; None
    without text."""
    if not any(cue.text.strip() for cue in cues):
        return None
    heading = build_transcript(
        [], f"Transcript ({label})" if label else "Transcript", [], "markdown"
    )
    body = build_transcript(cues, None, [], "markdown")
    section = from_markdown(f"#{heading}\n\n{body}", fmt)
    if fmt == "html":
        section = section.removeprefix("<html><body>").removesuffix("</body></html>").strip()
    return section or None


def _append(content: str, sections: list[str], fmt: OutputFormat) -> str:
    """The page's content with the sections after it: before ``</body>`` of an HTML result."""
    if fmt == "html":
        joined = "\n".join(sections)
        at = content.rfind("</body>")
        if at < 0:
            return f"{content}\n{joined}"
        return f"{content[:at]}{joined}\n{content[at:]}"
    separator = "\n\n" if fmt == "markdown" else "\n"
    return (
        separator.join([content.rstrip("\n"), *sections]) if content else separator.join(sections)
    )


# --- YouTube's watch page and Vimeo's player ------------------------------------------------


def player_details(body: bytes, charset: str | None) -> dict[str, Any] | None:
    """Title, description, author, duration, date, keywords and live state from the
    ``ytInitialPlayerResponse`` of a YouTube watch page, as ``MediaProbe`` fields; None when the
    page has none. CPU-bound: runs on the conversion pool."""
    text = decode_html(body, charset)
    match = _PLAYER_RESPONSE.search(text)
    if match is None:
        return None
    try:
        data, _ = json.JSONDecoder(strict=False).raw_decode(text, match.end() - 1)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    details = data.get("videoDetails")
    if not isinstance(details, dict):
        return None
    microformat = data.get("microformat")
    renderer = (
        microformat.get("playerMicroformatRenderer") if isinstance(microformat, dict) else None
    )
    renderer = renderer if isinstance(renderer, dict) else {}
    title = _text(details.get("title"), MAX_FIELD_LENGTH)
    if title is None:
        return None
    seconds = details.get("lengthSeconds")
    date = _iso_date(_text(renderer.get("publishDate") or renderer.get("uploadDate"), 40))
    keywords = details.get("keywords")
    live = renderer.get("liveBroadcastDetails")
    return {
        "title": title,
        "description": _text(details.get("shortDescription"), 20_000),
        "uploader": _text(details.get("author"), MAX_FIELD_LENGTH),
        "duration": float(seconds) if isinstance(seconds, str) and seconds.isdigit() else None,
        "upload_date": date.replace("-", "") if date else None,
        "tags": [k[:200] for k in keywords[:MAX_KEYWORDS] if isinstance(k, str)]
        if isinstance(keywords, list)
        else [],
        "is_live": details.get("isLive") is True
        or (isinstance(live, dict) and live.get("isLiveNow") is True),
    }


def _player_refused(fetched: FetchResult) -> bool:
    """Whether Vimeo's player says the video is gone (404, 410) or refused (403 in its own
    words)."""
    if fetched.status in _GONE_STATUSES:
        return True
    if fetched.status != 403:
        return False
    head = fetched.content[:_REFUSAL_SCAN_BYTES]  # read in the event loop: kept small
    text = html.unescape(decode_html(head, fetched.charset)).replace("\u2019", "'")
    return _PLAYER_REFUSALS.search(text) is not None


def og_details(body: bytes, charset: str | None) -> dict[str, Any] | None:
    """Title and description from the ``og:`` tags of a page (Vimeo's player), as
    ``MediaProbe`` fields; None without an ``og:title``. CPU-bound: runs on the conversion
    pool."""
    declared: dict[str, str] = {}
    for tag in page_tree(decode_html(body, charset)).iter("meta"):
        key = (tag.get("property") or "").strip().lower()
        content = " ".join((tag.get("content") or "").split())
        if key in ("og:title", "og:description") and content:
            declared.setdefault(key, content)
    title = _text(declared.get("og:title"), MAX_FIELD_LENGTH)
    if title is None:
        return None
    return {"title": title, "description": _text(declared.get("og:description"), 20_000)}


def _text(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value.strip() else None
