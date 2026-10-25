"""The ``DocumentPool`` target of the media children: ``DocumentPool(target=
"text_extraction.media.child")`` (ruling M3).

The child imports this module, and with it yt-dlp, youtube-transcript-api and requests, once,
after its memory limit is set; the parent never imports it. ``FUNCTIONS`` is the allowlist of
what a parent may call by name:

- ``youtube_transcript``: youtube-transcript-api lists a YouTube video's caption tracks and
  fetches the chosen one (``select.choose_track``). Its automatic tracks are the speech
  recognition in the video's language (translations are separate there), so they are keyed
  ``<lang>-orig`` as yt-dlp keys them, and the first one's language stands for the original
  language. The fetched snippets go back as a ``json3`` track. A video whose captions are
  disabled answers empty lists. A failure after YouTube answered HTTP 429 answers ``error``
  ``rate_limited`` with the answer's ``retry_after`` (the library raises ``IpBlocked`` for it,
  as for a bot check); every other failure of the library is raised as it is, and the parent
  classifies it by its type name.
- ``ytdlp_info``: yt-dlp's info without a download (title, description, uploader, date,
  duration, licence, chapters, tags, original language, live status and the tracks), and with
  ``fetch_track`` the chosen track's bytes. A track that is an HLS playlist (``#EXTM3U``, as
  arte and Vimeo deliver subtitles) is read and its segments are fetched and concatenated.
  The track is at most ``subtitle_max_bytes``, else ``error`` is ``too_large``; a track that
  cannot be fetched is ``track_failed``, and so is a track or segment URL that is not
  http(s). Either way the metadata comes back. A failure of the extraction itself is raised as
  yt-dlp raises it (``DownloadError``).

Each call's payload carries the proxy URL with the call's own credentials (the parent's
``EgressProxy``, the only way out), our user agent, the requested languages and the seconds
the call has left. yt-dlp runs with ruling M4's options: one allowed extractor, no playlist, no
download, no cache, no cookies, no retries, no JavaScript runtime and no remote components;
its request handlers take only http and https (``_HttpOnlyYoutubeDL``), so no request of yt-dlp
or of an extractor can leave by another scheme past the proxy.
A reply holds plain values only; the track's URLs and yt-dlp's format list never leave the
child.

Before anything else is imported, logging is silenced (``child_logging``), and yt-dlp gets a
logger that drops everything: what the libraries print may quote the URL or the video id.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from text_extraction.documents.child_logging import silence_logging

silence_logging()

# After the silencing: the libraries log or warn while they load.
import requests  # noqa: E402
from youtube_transcript_api import TranscriptsDisabled, YouTubeTranscriptApi  # noqa: E402
from yt_dlp import YoutubeDL  # noqa: E402
from yt_dlp.networking import Request  # noqa: E402
from yt_dlp.networking.exceptions import RequestError  # noqa: E402

from text_extraction.media.select import TrackRef, choose_track, track_language  # noqa: E402

SOCKET_TIMEOUT_SECONDS: Final = 10.0  # one connection or read (M4)
MAX_TRACK_KEYS: Final = 500  # languages per kind of track (YouTube offers ~160 automatic ones)
MAX_FORMATS: Final = 10  # formats per track
MAX_TEXT: Final = 20_000  # characters of a description
MAX_SHORT_TEXT: Final = 1_000  # characters of a title, uploader, licence, chapter or tag
MAX_CHAPTERS: Final = 200
MAX_TAGS: Final = 50
MAX_SEGMENTS: Final = 2_000  # of an HLS subtitle playlist
_READ_CHUNK: Final = 64 * 1024
# The formats a track is taken in, best first, and the parser each one goes to.
_FORMATS: Final = {
    "vtt": "vtt",
    "json3": "json3",
    "ttml": "ttml",
    "xml": "ttml",  # ZDF's EBU-TT
    "dfxp": "ttml",
    "srt": "srt",
}
_HLS_EXTENSIONS: Final = frozenset({"m3u8"})
_HLS_PROTOCOLS: Final = frozenset({"m3u8", "m3u8_native"})
_HLS_MAGIC: Final = b"#EXTM3U"
# The only URL schemes a child's requests may have: the egress proxy carries HTTP alone.
HTTP_SCHEMES: Final = frozenset({"http", "https"})


class _TooLarge(Exception):  # noqa: N818 - a cap, not an error
    pass


class _BadPlaylist(Exception):  # noqa: N818
    pass


class _NotHttp(Exception):  # noqa: N818 - a refusal
    """A track or segment URL whose scheme is not http(s)."""


class _QuietLogger:
    """yt-dlp's logger: drops everything (messages quote URLs and video ids)."""

    def debug(self, _message: str) -> None:
        pass

    def info(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


# --- youtube-transcript-api ----------------------------------------------------------------


class _Session(requests.Session):
    """A session that never waits longer than ``timeout`` for a connection or a read, and
    ignores the environment's proxies and netrc. It notes an HTTP 429 answer (and its
    ``Retry-After``): youtube-transcript-api raises ``IpBlocked`` for it as for a bot check, and
    only the status tells the rate limit apart."""

    def __init__(self, timeout: float, user_agent: str, proxy: str) -> None:
        super().__init__()
        self.trust_env = False
        self.headers["User-Agent"] = user_agent
        self.proxies = {"http": proxy, "https": proxy}
        self._timeout = timeout
        self.rate_limited = False
        self.retry_after: str | None = None
        self.hooks["response"].append(self._note_status)

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return super().request(*args, **kwargs)

    def _note_status(self, response: requests.Response, *_args: Any, **_kwargs: Any) -> None:
        if response.status_code == 429:
            self.rate_limited = True
            value = response.headers.get("retry-after")
            self.retry_after = value[:MAX_SHORT_TEXT] if value else None


def youtube_transcript(payload: dict[str, Any]) -> dict[str, Any]:
    """See the module docstring. A failure after an HTTP 429 answer is ``rate_limited`` (with
    the answer's ``retry_after``), whatever the library raised for it."""
    session = _Session(_socket_timeout(payload), payload["user_agent"], payload["proxy"])
    try:
        return _youtube_transcript(payload, session)
    except Exception:
        if not session.rate_limited:
            raise
        return {**_empty_result(), "error": "rate_limited", "retry_after": session.retry_after}


def _youtube_transcript(payload: dict[str, Any], session: _Session) -> dict[str, Any]:
    result = _empty_result()
    try:
        listing = YouTubeTranscriptApi(http_client=session).list(payload["video_id"])
    except TranscriptsDisabled:
        return result
    transcripts: dict[str, Any] = {}
    manual: dict[str, list[TrackRef]] = {}
    automatic: dict[str, list[TrackRef]] = {}
    for transcript in listing:
        if transcript.is_generated:
            key = f"{transcript.language_code}-orig"
            kind = automatic
            if result["original_language"] is None:
                result["original_language"] = _short(transcript.language_code)
        else:
            key, kind = transcript.language_code, manual
        if isinstance(key, str) and key not in transcripts and len(kind) < MAX_TRACK_KEYS:
            kind[key] = [TrackRef("json3", _short(transcript.language))]
            transcripts[key] = transcript
    result["manual"], result["automatic"] = _refs(manual), _refs(automatic)
    if not payload.get("fetch_track", True):
        return result
    choice = choose_track(manual, automatic, result["original_language"], payload["languages"])
    if choice is None:
        return result
    key, source = choice
    events = [
        {
            "tStartMs": round(snippet.start * 1000),
            "dDurationMs": round(snippet.duration * 1000),
            "segs": [{"utf8": snippet.text}],
        }
        for snippet in transcripts[key].fetch()
    ]
    if not events:  # an empty caption body: what YouTube answers when it wants a PO token
        result["error"] = "empty_track"
        return result
    body = json.dumps({"events": events}, ensure_ascii=False).encode()
    if len(body) > payload["subtitle_max_bytes"]:
        result["error"] = "too_large"
        return result
    result["track"] = _track(key, source, "json3", body, "utf-8")
    return result


# --- yt-dlp --------------------------------------------------------------------------------


def ytdlp_params(payload: Mapping[str, Any]) -> dict[str, Any]:
    """yt-dlp's options for a call (ruling M4)."""
    return {
        "proxy": payload["proxy"],
        "socket_timeout": _socket_timeout(payload),
        "allowed_extractors": list(payload["extractors"]),
        "noplaylist": True,
        "playlist_items": "1",  # a page with several videos: the first
        "skip_download": True,
        "cachedir": False,
        "extractor_retries": 0,
        "retries": 0,
        "http_headers": {"User-Agent": payload["user_agent"]},
        "js_runtimes": {},  # none: the API image has no JavaScript runtime
        "remote_components": [],
        "check_formats": False,
        "ignore_no_formats_error": True,  # metadata is wanted even without a playable format
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
    }


class _HttpOnlyYoutubeDL(YoutubeDL):  # type: ignore[misc]
    """A ``YoutubeDL`` whose request handlers take http and https only.

    Every request yt-dlp makes, its extractors' and downloaders' included, goes through the
    handlers of this instance's request director. Its urllib handler would also take ``ftp``,
    ``data`` and (when enabled) ``file``, and connect for ``ftp`` without the proxy when the
    environment's ``no_proxy`` names the host; the websockets handler would take ``ws`` and
    ``wss``. Each handler keeps only the HTTP schemes, so any other URL has no handler and
    fails before a connection is made.
    """

    def build_request_director(self, handlers: Any, preferences: Any = None) -> Any:
        director = super().build_request_director(handlers, preferences)
        for handler in director.handlers.values():
            schemes = handler._SUPPORTED_URL_SCHEMES or ()  # None would mean every scheme
            handler._SUPPORTED_URL_SCHEMES = tuple(s for s in schemes if s in HTTP_SCHEMES)
        return director


def youtube_dl(payload: Mapping[str, Any]) -> YoutubeDL:
    """The ``YoutubeDL`` of a call: ruling M4's options, HTTP requests only."""
    return _HttpOnlyYoutubeDL(ytdlp_params(payload))


def ytdlp_info(payload: dict[str, Any]) -> dict[str, Any]:
    result = _empty_result()
    with youtube_dl(payload) as ydl:
        info = ydl.extract_info(payload["url"], download=False)
        if isinstance(info, dict) and info.get("_type") == "playlist":
            entries = [entry for entry in info.get("entries") or () if isinstance(entry, dict)]
            info = entries[0] if entries else info
        if not isinstance(info, dict):
            return result
        _metadata(info, result)
        manual = _ytdlp_tracks(info.get("subtitles"))
        automatic = _ytdlp_tracks(info.get("automatic_captions"))
        manual_refs = {key: refs for key, (refs, _formats) in manual.items()}
        automatic_refs = {key: refs for key, (refs, _formats) in automatic.items()}
        result["manual"], result["automatic"] = _refs(manual_refs), _refs(automatic_refs)
        if not payload.get("fetch_track", True):
            return result
        choice = choose_track(
            manual_refs, automatic_refs, result["original_language"], payload["languages"]
        )
        if choice is None:
            return result
        key, source = choice
        formats = (manual if source == "manual_subtitles" else automatic)[key][1]
        try:
            fmt, body, charset = _download_track(ydl, formats, payload["subtitle_max_bytes"])
        except _TooLarge:
            result["error"] = "too_large"
        except (RequestError, _BadPlaylist, _NotHttp, OSError):
            result["error"] = "track_failed"
        else:
            if fmt is None:
                result["error"] = "track_failed"  # no format we read
            elif not body.strip():
                result["error"] = "empty_track"
            else:
                result["track"] = _track(key, source, fmt, body, charset)
    return result


def _metadata(info: Mapping[str, Any], result: dict[str, Any]) -> None:
    result["title"] = _short(info.get("title"))
    result["description"] = _text(info.get("description"), MAX_TEXT)
    result["uploader"] = _short(info.get("uploader") or info.get("channel"))
    result["upload_date"] = _short(info.get("upload_date"))
    result["duration"] = _number(info.get("duration"))
    result["license"] = _short(info.get("license"))
    result["original_language"] = _short(info.get("language"))
    result["is_live"] = info.get("live_status") == "is_live" or info.get("is_live") is True
    chapters = info.get("chapters")
    if isinstance(chapters, list):
        result["chapters"] = [
            {"start": start, "title": title}
            for chapter in chapters[:MAX_CHAPTERS]
            if isinstance(chapter, dict)
            and (start := _number(chapter.get("start_time"))) is not None
            and (title := _short(chapter.get("title")))
        ]
    tags = info.get("tags")
    if isinstance(tags, list):
        result["tags"] = [tag for tag in map(_short, tags[:MAX_TAGS]) if tag]


type _Tracks = dict[str, tuple[list[TrackRef], list[dict[str, Any]]]]


def _ytdlp_tracks(tracks: object) -> _Tracks:
    """yt-dlp's ``subtitles`` or ``automatic_captions``: per key, the formats as refs (for the
    reply) and as yt-dlp's dicts (for the download, which stay in the child)."""
    found: _Tracks = {}
    if not isinstance(tracks, dict):
        return found
    for key, formats in tracks.items():
        if len(found) >= MAX_TRACK_KEYS:
            break
        if not isinstance(key, str) or not isinstance(formats, list):
            continue
        usable = [f for f in formats[:MAX_FORMATS] if isinstance(f, dict) and f.get("url")]
        refs = [TrackRef(_short(f.get("ext")) or "", _short(f.get("name"))) for f in usable]
        found[key] = (refs, usable)
    return found


def _download_track(
    ydl: YoutubeDL, formats: list[dict[str, Any]], max_bytes: int
) -> tuple[str | None, bytes, str | None]:
    """The best format we read: ``(parser format, body, charset)``; ``(None, b"", None)`` when
    the track has none."""
    ranked = sorted(
        (f for f in formats if _parser_format(f) is not None),
        key=lambda f: list(_FORMATS).index(_parser_format(f) or "vtt"),
    )
    if not ranked:
        return None, b"", None
    fmt = ranked[0]
    response = ydl.urlopen(Request(_http_url(fmt["url"]), headers=_headers(fmt)))
    body = _read_capped(response, max_bytes)
    charset = _charset(response.headers.get("Content-Type"))
    if _is_hls(fmt) or body.lstrip().startswith(_HLS_MAGIC):
        body, charset = _hls_track(ydl, response.url or fmt["url"], body, max_bytes, _headers(fmt))
        return "vtt", body, charset
    return _parser_format(fmt), body, charset


def _parser_format(fmt: Mapping[str, Any]) -> str | None:
    ext = str(fmt.get("ext") or "").lower()
    if _is_hls(fmt):
        return "vtt"  # the segments of an HLS subtitle playlist are WebVTT
    return _FORMATS.get(ext)


def _is_hls(fmt: Mapping[str, Any]) -> bool:
    ext = str(fmt.get("ext") or "").lower()
    return fmt.get("protocol") in _HLS_PROTOCOLS or ext in _HLS_EXTENSIONS


def _hls_track(
    ydl: YoutubeDL, base: str, playlist: bytes, max_bytes: int, headers: dict[str, str]
) -> tuple[bytes, str | None]:
    """The VTT segments of a media playlist, fetched and concatenated within ``max_bytes``."""
    lines = playlist.decode("utf-8", errors="replace").splitlines()
    if not lines or not lines[0].lstrip("﻿").strip().startswith("#EXTM3U"):
        raise _BadPlaylist
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        raise _BadPlaylist  # a master playlist: yt-dlp gives the media playlist of a track
    uris = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if not uris or len(uris) > MAX_SEGMENTS:
        raise _BadPlaylist
    parts: list[bytes] = []
    total = 0
    charset: str | None = None
    for index, uri in enumerate(uris):
        response = ydl.urlopen(Request(_http_url(urljoin(base, uri)), headers=headers))
        segment = _read_capped(response, max_bytes - total)
        if segment.lstrip().startswith(_HLS_MAGIC):
            raise _BadPlaylist  # no nested playlists
        if index == 0:
            charset = _charset(response.headers.get("Content-Type"))
        else:
            segment = _without_vtt_header(segment)
        total += len(segment)
        parts.append(segment.strip(b"\r\n"))
    body = b"\n\n".join(parts) + b"\n"
    if len(body) > max_bytes:
        raise _TooLarge
    return body, charset


def _http_url(url: object) -> str:
    """``url`` when its scheme is http or https; anything else raises ``_NotHttp``."""
    if not isinstance(url, str) or urlsplit(url).scheme.lower() not in HTTP_SCHEMES:
        raise _NotHttp
    return url


def _without_vtt_header(segment: bytes) -> bytes:
    """A later segment without its ``WEBVTT`` header block (and its ``X-TIMESTAMP-MAP``)."""
    text = segment.lstrip(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")
    if not text.startswith(b"WEBVTT"):
        return text
    _header, _blank, rest = text.partition(b"\n\n")
    return rest


def _read_capped(response: Any, max_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.strip().isdecimal() and int(declared) > max_bytes:
        raise _TooLarge
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(_READ_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _headers(fmt: Mapping[str, Any]) -> dict[str, str]:
    headers = fmt.get("http_headers")
    if not isinstance(headers, dict):
        return {}
    return {str(name): str(value) for name, value in headers.items()}


def _charset(content_type: str | None) -> str | None:
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            return value.strip().strip("\"'")[:40]
    return None


# --- the reply -----------------------------------------------------------------------------


def _empty_result() -> dict[str, Any]:
    return {
        "title": None,
        "description": None,
        "uploader": None,
        "upload_date": None,
        "duration": None,
        "license": None,
        "chapters": [],
        "tags": [],
        "original_language": None,
        "manual": {},
        "automatic": {},
        "track": None,
        "is_live": False,
        "error": None,
    }


def _refs(tracks: Mapping[str, Iterable[TrackRef]]) -> dict[str, list[dict[str, Any]]]:
    return {
        key: [{"ext": ref.ext, "name": ref.name} for ref in refs] for key, refs in tracks.items()
    }


def _track(key: str, source: str, fmt: str, body: bytes, charset: str | None) -> dict[str, Any]:
    return {
        "key": key,
        "language": track_language(key),
        "source": source,
        "format": fmt,
        "body": body,
        "charset": charset,
    }


def _socket_timeout(payload: Mapping[str, Any]) -> float:
    return max(1.0, min(SOCKET_TIMEOUT_SECONDS, float(payload.get("timeout", 10.0))))


def _short(value: object) -> str | None:
    return _text(value, MAX_SHORT_TEXT)


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "youtube_transcript": youtube_transcript,
    "ytdlp_info": ytdlp_info,
}
