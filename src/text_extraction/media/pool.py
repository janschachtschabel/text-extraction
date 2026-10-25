"""Subtitles and metadata of a platform video, from child processes (rulings M3 and M4).

youtube-transcript-api and yt-dlp parse what a platform sends and make requests we cannot see
one by one, so they run in ``DocumentPool`` children (``create_media_pool``: target
``text_extraction.media.child``, ``MEDIA_CONCURRENCY`` children of ``MEDIA_MEMORY_LIMIT_MB``),
killed at the caller's deadline. The parent never imports them.

``MediaFetcher`` owns the pool and one ``EgressProxy`` (``browser_proxy.py``), the children's
only way out: each child call gets a ``ProxyContext`` of its own with
``MEDIA_EXTRACT_MAX_BYTES``, closed when the call returns. The proxy checks every connection
against the address policy (``is_public_address``, or anything with
``FETCH_ALLOW_PRIVATE_NETWORKS``), redirects and the player's sub-requests included.

``probe(target, languages, deadline)``:

- YouTube: ``youtube_transcript`` first (list, then fetch the chosen track), then ``ytdlp_info``
  for the metadata. yt-dlp fetches the track itself only when youtube-transcript-api failed
  (not when it found the video without a track we take). Each tier runs at most once, and
  after YouTube answered HTTP 429 yt-dlp is not asked at all: the probe is ``rate_limited``
  with the answer's ``retry_after``, for the caller to report to the outbound gate.
- Every other platform: ``ytdlp_info`` with the track.

A failure becomes the probe's ``error`` code, never an exception, except ``ConversionTimeout``
(the deadline passed: the child was killed, and the caller answers ``timeout``) and a pool that
is shut down. The codes: ``blocked`` (a bot check, a blocked IP, a PO token demanded),
``rate_limited`` (HTTP 429), ``unavailable`` (private, removed, age- or region-restricted),
``empty_track`` (a 200 caption body without text), ``too_large`` (over
``MEDIA_SUBTITLE_MAX_BYTES``), ``track_failed``, ``extractor_error``, ``crashed`` and
``out_of_memory``; a request the proxy refused during a failed call gives its refusal reason
instead (``blocked_address``, ``bytes_cap``, ``dns_error``, ``connect_error``), unless the call
was ``blocked``, ``rate_limited`` or ``too_large``. ``blocked`` on
the probe is True when no track came and a platform refused the service (``blocked``,
``rate_limited``, or an empty YouTube caption body): the answer for ruling D1. ``rate_limited``
on the probe is True when any call ended ``rate_limited``.

The proxy's tunnels live ``call_seconds`` plus a grace from their start (``create_media_fetcher``:
the deadline of the pipeline the fetcher serves), and a probe never runs longer than
``call_seconds``, so no tunnel it opens dies before its deadline.

Logs and errors carry codes and type names only, never a URL, a video id or a library's message
(which quotes them).
"""

import asyncio
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast, get_args

import structlog
from prometheus_client import CollectorRegistry

from text_extraction.browser_proxy import TUNNEL_GRACE_SECONDS, EgressProxy, ProxyContext
from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    DocumentPool,
)
from text_extraction.fetch import USER_AGENT
from text_extraction.media.captions import CaptionFormat
from text_extraction.media.select import TrackRef
from text_extraction.media.sites import EXTRACTORS, MediaTarget
from text_extraction.media.transcript import Chapter
from text_extraction.media.types import TranscriptSource
from text_extraction.settings import Settings

__all__ = [
    "MEDIA_TARGET",
    "FetchedTrack",
    "MediaFetcher",
    "MediaProbe",
    "create_media_fetcher",
    "create_media_pool",
]

_log = structlog.get_logger(__name__)

MEDIA_TARGET: Final = "text_extraction.media.child"
_RESULT_SLACK_BYTES: Final = 2 * 1024 * 1024  # metadata and track lists next to the track
_BLOCKING: Final = frozenset({"blocked", "rate_limited"})
# Codes a proxy refusal during the same call does not replace: the platform's refusal of the
# service (D1) and the child's own cap say more than a refused side request.
_KEEP_OVER_REFUSAL: Final = frozenset({*_BLOCKING, "too_large"})
# youtube-transcript-api's exceptions by type name (its RequestBlocked covers HTTP 429 too).
_YTA_CODES: Final = {
    "RequestBlocked": "blocked",
    "IpBlocked": "blocked",
    "PoTokenRequired": "blocked",
    "VideoUnavailable": "unavailable",
    "VideoUnplayable": "unavailable",
    "AgeRestricted": "unavailable",
    "InvalidVideoId": "unavailable",
}
# yt-dlp raises DownloadError for everything; its message names the cause.
_YTDLP_MESSAGES: Final = (
    ("sign in to confirm", "blocked"),
    ("not a bot", "blocked"),
    ("http error 429", "rate_limited"),
    ("too many requests", "rate_limited"),
    ("private video", "unavailable"),
    ("video unavailable", "unavailable"),
    ("is not available", "unavailable"),
    ("has been removed", "unavailable"),
    ("confirm your age", "unavailable"),
    ("age-restricted", "unavailable"),
    ("age restricted", "unavailable"),
    ("geo restrict", "unavailable"),
    ("geo-restrict", "unavailable"),
    ("not available in your country", "unavailable"),
)


@dataclass(frozen=True, slots=True)
class FetchedTrack:
    """The chosen subtitle track as the platform sent it; ``parse_captions(body, format,
    charset)`` makes cues of it. ``key`` is the platform's language key, ``language`` its
    normalised language."""

    key: str
    language: str | None
    source: TranscriptSource
    format: CaptionFormat
    body: bytes
    charset: str | None = None


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """What the children found out about a video. ``manual`` and ``automatic`` are the tracks
    by language key.

    ``error`` is the code of the failure behind what the probe lacks (the codes are in the
    module docstring): without ``track``, why there is none (yt-dlp's failure, else
    youtube-transcript-api's; None when the video simply has no track we take). With
    ``track`` it is normally None, but on YouTube it is set when youtube-transcript-api gave
    the track and yt-dlp then failed: the track is good, the metadata fields are empty, and
    ``error`` says why. ``blocked`` is True only without ``track``."""

    title: str | None = None
    description: str | None = None
    uploader: str | None = None
    upload_date: str | None = None  # YYYYMMDD
    duration: float | None = None  # seconds
    license: str | None = None
    chapters: list[Chapter] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    original_language: str | None = None
    manual: dict[str, list[TrackRef]] = field(default_factory=dict)
    automatic: dict[str, list[TrackRef]] = field(default_factory=dict)
    track: FetchedTrack | None = None
    is_live: bool = False
    blocked: bool = False
    error: str | None = None
    # YouTube answered HTTP 429 (``Retry-After`` as sent, None without one), or yt-dlp said so.
    rate_limited: bool = False
    retry_after: str | None = None


def create_media_pool(settings: Settings) -> DocumentPool:
    """The media children of a process (``MEDIA_CONCURRENCY``, ``MEDIA_MEMORY_LIMIT_MB``); not
    started yet. A reply holds at most ``MEDIA_SUBTITLE_MAX_BYTES`` of track and the metadata."""
    return DocumentPool(
        settings.media_concurrency,
        settings.media_memory_limit_mb,
        target=MEDIA_TARGET,
        max_result_bytes=settings.media_subtitle_max_bytes + _RESULT_SLACK_BYTES,
    )


@dataclass(slots=True)
class _Outcome:
    """One child call: its reply (None when it failed) and its error code."""

    reply: Mapping[str, Any] | None
    error: str | None
    failed: bool = False
    track: FetchedTrack | None = None

    @classmethod
    def from_reply(cls, reply: object) -> "_Outcome":
        if not isinstance(reply, dict):
            return cls(None, "extractor_error", failed=True)
        error = reply.get("error")
        return cls(reply, error if isinstance(error, str) else None, track=_track(reply))

    @property
    def retry_after(self) -> str | None:
        return _str((self.reply or {}).get("retry_after"))


class MediaFetcher:
    """Probes platform videos in the media children (see the module docstring). ``start``
    before ``/ready`` (``probe`` starts it otherwise), ``stop`` at shutdown.

    ``allowed_extractors`` are the yt-dlp extractors a target may name: the platforms' by
    default. Tests allow ``generic`` and ``html5`` to extract a local page.

    ``call_seconds`` (default ``EXTRACT_TIMEOUT_SECONDS``) is the longest a probe runs: its
    deadline is moved forward to that many seconds from its start. The proxy's tunnels must live
    at least that long (``create_media_fetcher`` makes them so)."""

    def __init__(
        self,
        settings: Settings,
        pool: DocumentPool,
        proxy: EgressProxy,
        *,
        allowed_extractors: Collection[str] = frozenset(EXTRACTORS.values()),
        call_seconds: float | None = None,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._proxy = proxy
        self._allowed = frozenset(allowed_extractors)
        self._call_seconds = (
            call_seconds if call_seconds is not None else settings.extract_timeout_seconds
        )
        self._lock = asyncio.Lock()
        self._started = False

    @property
    def call_seconds(self) -> float:
        return self._call_seconds

    @property
    def pool(self) -> DocumentPool:
        return self._pool

    @property
    def proxy(self) -> EgressProxy:
        return self._proxy

    async def start(self) -> None:
        """Start the proxy and the children; at once when they run already."""
        async with self._lock:
            if self._started:
                return
            await self._proxy.start()
            try:
                await self._pool.start()
            except BaseException:
                await self._proxy.stop()
                raise
            self._started = True

    async def stop(self) -> None:
        """Kill the children and stop the proxy. Safe to call more than once."""
        await self._pool.shutdown()
        await self._proxy.stop()

    async def probe(
        self, target: MediaTarget, languages: Sequence[str], deadline: float
    ) -> MediaProbe:
        """Subtitles and metadata of ``target`` by ``deadline`` (``loop.time()``; at most
        ``call_seconds`` from now). Raises ``ConversionTimeout`` when the deadline passes,
        ``RuntimeError`` once stopped."""
        if target.extractor not in self._allowed:
            raise ValueError("the target names an extractor that is not allowed")
        if target.video_id is None:
            raise ValueError("the target is a playlist, not one video")
        deadline = min(deadline, asyncio.get_running_loop().time() + self._call_seconds)
        await self.start()
        payload: dict[str, Any] = {
            "url": target.canonical_url,
            "video_id": target.video_id,
            "extractors": [target.extractor],
            "user_agent": USER_AGENT,
            "languages": list(languages),
            "subtitle_max_bytes": self._settings.media_subtitle_max_bytes,
        }
        if target.platform != "youtube":
            info = await self._call("ytdlp_info", {**payload, "fetch_track": True}, deadline)
            return _probe(info, info.track, info.error, blocked=info.error in _BLOCKING)
        listing = await self._call("youtube_transcript", payload, deadline)
        if listing.error == "rate_limited":
            # YouTube asked us to slow down: yt-dlp would call it again at once.
            return _probe(listing, None, "rate_limited", blocked=True)
        # yt-dlp fetches the track only when youtube-transcript-api failed (or its track was
        # empty); a listing without a track we take would be the same there.
        retry_track = listing.track is None and listing.error not in (None, "too_large")
        info = await self._call("ytdlp_info", {**payload, "fetch_track": retry_track}, deadline)
        track = listing.track or info.track
        if track is None:
            error = info.error or listing.error
        else:
            error = info.error if info.failed else None
        codes = (listing.error, info.error)
        blocked = track is None and any(
            code in _BLOCKING or code == "empty_track" for code in codes
        )
        source = info if not info.failed else listing
        return _probe(source, track, error, blocked=blocked, codes=codes)

    async def _call(self, function: str, payload: dict[str, Any], deadline: float) -> _Outcome:
        context = self._proxy.open_context(max_bytes=self._settings.media_extract_max_bytes)
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            call = {
                **payload,
                "proxy": _proxy_url(self._proxy.url, context),
                "timeout": max(remaining, 0.0),
            }
            try:
                reply = await self._pool.run(function, call, deadline)
            except ConversionError as error:
                code = _failure_code(error)
                return _Outcome(None, _refusal(context, code), failed=True)
            except ConversionCrashed:
                _log.warning("media_child_crashed", function=function)
                return _Outcome(None, "crashed", failed=True)
            except ConversionOutOfMemory:
                _log.warning("media_child_out_of_memory", function=function)
                return _Outcome(None, "out_of_memory", failed=True)
            outcome = _Outcome.from_reply(reply)
            if outcome.error is not None:
                outcome.error = _refusal(context, outcome.error)
            return outcome
        finally:
            context.close()


def create_media_fetcher(
    settings: Settings, registry: CollectorRegistry, *, call_seconds: float | None = None
) -> MediaFetcher:
    """The media fetcher of a process: its pool and its egress proxy, neither started yet.
    ``call_seconds`` is the deadline of the process's pipeline (default
    ``EXTRACT_TIMEOUT_SECONDS``; the browser worker's is longer): no probe runs longer, and a
    tunnel of the proxy lives that long plus a grace."""
    seconds = call_seconds if call_seconds is not None else settings.extract_timeout_seconds
    proxy = EgressProxy(settings, registry=registry, tunnel_timeout=seconds + TUNNEL_GRACE_SECONDS)
    return MediaFetcher(settings, create_media_pool(settings), proxy, call_seconds=seconds)


def _probe(
    outcome: _Outcome,
    track: FetchedTrack | None,
    error: str | None,
    *,
    blocked: bool,
    codes: Sequence[str | None] = (),
) -> MediaProbe:
    reply = outcome.reply or {}
    rate_limited = "rate_limited" in (error, outcome.error, *codes)
    return MediaProbe(
        title=_str(reply.get("title")),
        description=_str(reply.get("description")),
        uploader=_str(reply.get("uploader")),
        upload_date=_str(reply.get("upload_date")),
        duration=_float(reply.get("duration")),
        license=_str(reply.get("license")),
        chapters=_chapters(reply.get("chapters")),
        tags=[tag for tag in _list(reply.get("tags")) if isinstance(tag, str)],
        original_language=_str(reply.get("original_language")),
        manual=_tracks(reply.get("manual")),
        automatic=_tracks(reply.get("automatic")),
        track=track,
        is_live=reply.get("is_live") is True,
        blocked=blocked,
        error=error,
        rate_limited=rate_limited,
        retry_after=outcome.retry_after if rate_limited else None,
    )


def _track(reply: Mapping[str, Any]) -> FetchedTrack | None:
    track = reply.get("track")
    if not isinstance(track, dict):
        return None
    key, body, source, fmt = (track.get(name) for name in ("key", "body", "source", "format"))
    if (
        not isinstance(key, str)
        or not isinstance(body, bytes)
        or source not in get_args(TranscriptSource)
        or fmt not in get_args(CaptionFormat)
    ):
        return None
    return FetchedTrack(
        key=key,
        language=_str(track.get("language")),
        source=cast(TranscriptSource, source),
        format=cast(CaptionFormat, fmt),
        body=body,
        charset=_str(track.get("charset")),
    )


def _tracks(value: object) -> dict[str, list[TrackRef]]:
    if not isinstance(value, dict):
        return {}
    return {
        key: [
            TrackRef(ref["ext"], _str(ref.get("name")))
            for ref in _list(refs)
            if isinstance(ref, dict) and isinstance(ref.get("ext"), str)
        ]
        for key, refs in value.items()
        if isinstance(key, str)
    }


def _chapters(value: object) -> list[Chapter]:
    return [
        Chapter(start, title)
        for chapter in _list(value)
        if isinstance(chapter, dict)
        and (start := _float(chapter.get("start"))) is not None
        and (title := _str(chapter.get("title"))) is not None
    ]


def _failure_code(error: ConversionError) -> str:
    """The code of an exception a child raised, by its type name and, for yt-dlp's
    ``DownloadError``, its message (read here, never logged)."""
    if error.type_name in _YTA_CODES:
        return _YTA_CODES[error.type_name]
    message = error.message.lower()
    for needle, code in _YTDLP_MESSAGES:
        if needle in message:
            return code
    return "extractor_error"


def _refusal(context: ProxyContext, code: str) -> str:
    """A failed call's code, or the reason of the first request the proxy refused during it,
    unless the code is one that says more (``blocked``, ``rate_limited``, ``too_large``)."""
    if context.refused and code not in _KEEP_OVER_REFUSAL:
        return context.refused[0][1]
    return code


def _proxy_url(url: str, context: ProxyContext) -> str:
    scheme, _, address = url.partition("://")
    return f"{scheme}://{context.username}:{context.password}@{address}"


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []
