"""The ``DocumentPool`` target of the ASR children: ``DocumentPool(target=
"text_extraction.media.asr_child")`` (ruling M10).

The child imports this module, and with it faster-whisper, CTranslate2, PyAV and yt-dlp, once,
after its memory limit is set; the parent (``asr_worker.py``) never imports it. ``FUNCTIONS`` is
the allowlist of what a parent may call by name:

- ``platform_info``: yt-dlp's info of a platform video (``url``, with ``extractor`` as its only
  allowed extractor), its format chosen (``FORMAT``: the best audio, else the smallest video up
  to 360p, either only over plain HTTP(S), native HLS or native DASH segments, the protocols
  yt-dlp downloads itself; never RTMP or another protocol it hands to an outside program), as
  JSON (``YoutubeDL.sanitize_info``) for ``download``, with its ``duration``. A live stream that
  is still running raises ``IsLive``.
- ``download``: the media into the item's scratch directory (``scratch``) as ``media.<ext>``.
  A platform video downloads from that info (``YoutubeDL.process_ie_result``, as yt-dlp's
  ``--load-info-json`` does), with yt-dlp's own downloaders only (no FFmpeg, no fix-ups, no
  post-processors) and ``max_filesize``. A media file (``source: file``) is one GET, read in
  chunks up to ``max_bytes`` (``TooLarge`` past it), an HTTP error raises ``HttpStatus`` with
  the status. The bytes must sniff as audio or video (``media/sniff.py``), else ``NotMedia``.
  The reply is the file's ``path``, its ``kind``, its ``duration`` (yt-dlp's, else PyAV's
  probe; None when neither knows) and, for a media file, the HTTP ``status`` it answered with.
- ``transcribe``: the file at ``path`` with the model at ``model_path`` (``ModelsMissing``
  without its ``model.bin``). A container that says it is longer than ``max_duration`` seconds
  raises ``MediaTooLong`` (its seconds as the message) before anything is decoded. PyAV decodes
  the first audio stream to 16 kHz mono float32 (``decode_audio``; a packet that does not
  decode is skipped, a read error after some audio ends the decoding with what was decoded;
  past ``max_duration`` seconds of audio it raises ``MediaTooLong``, whatever the container
  said; nothing decoded, or a file PyAV cannot open, raises ``DecodeFailed``), and
  faster-whisper reads that array, never the path: ``WhisperModel(model_path, device="cpu",
  compute_type=, cpu_threads=, local_files_only=True)``, kept for the child's later calls, gets
  it in windows of at most 10 minutes cut in pauses (``media/asr_windows.py``: faster-whisper
  computes the features of a whole call at once, 3.4 GiB for an hour), each with
  ``vad_filter=True, condition_on_previous_text=False``, in ``language`` when Whisper knows it,
  else the language it detects in the first window with speech. The reply is the segments as
  ``[start, end, text]`` on the file's clock, the language and the audio's duration. With
  ``backend: openai`` there is no model: the decoded audio goes to the external endpoint
  (``media/asr_external.transcribe_external``: Ogg Opus chunks of at most 20 minutes, with
  ``base_url``, ``model``, ``api_key``, ``timeout`` per chunk, ``budget`` for all and our
  ``user_agent``); its failures raise ``ExternalRejected``, ``ExternalUnavailable``,
  ``ExternalFailed`` or ``BudgetSpent``, the same reply otherwise.
- ``opus_check``: the file at ``path`` (the built-in clip) decoded and encoded as Ogg Opus, for
  the external backend's self-check; the reply is the encoded size (``EncodeFailed`` without an
  Ogg Opus stream).
- ``ffmpeg``: PyAV's FFmpeg libraries with their ``configuration`` and ``license``
  (``av._core.library_meta`` of PyAV 19.0.1), the codecs PyAV offers (``av.codecs_available``),
  the outside programs yt-dlp could hand a download to that are on ``PATH`` (``ffmpeg``,
  ``rtmpdump``; the image ships neither) and PyAV's version, for the self-check
  (``asr_worker.ffmpeg_gpl_findings``).

PyAV opens files with ``protocol_whitelist=file``: a container that names other inputs (an HLS
playlist, for example) cannot make FFmpeg fetch anything or read through another protocol.
Whisper's language codes come from ``faster_whisper.tokenizer._LANGUAGE_CODES``, a private name
of faster-whisper 1.2.1 (pinned): an upgrade must check that it still exists.

A download's payload carries the proxy URL with the call's own credentials (the parent's
``EgressProxy``, the only way out), our user agent and the seconds the call has left. yt-dlp runs
with ruling M4's options (``media/child.py``: one allowed extractor, no playlist, no cache, no
cookies, no retries) and its request handlers take only http and https (``_HttpOnlyYoutubeDL``).
For YouTube's player challenges it gets Deno from the ``deno`` package as its JavaScript runtime
and yt-dlp-ejs's bundled scripts; no remote component is fetched. A reply holds plain values
only.

Before anything else is imported, logging is silenced (``child_logging``), and yt-dlp gets a
logger that drops everything: what the libraries print may quote the URL, the video id or the
speech.
"""

import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from text_extraction.documents.child_logging import silence_logging

silence_logging()

# After the silencing: the libraries log or warn while they load.
import av  # noqa: E402
import av.error  # noqa: E402
import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
from faster_whisper.tokenizer import _LANGUAGE_CODES  # noqa: E402
from yt_dlp.networking import Request  # noqa: E402
from yt_dlp.networking.exceptions import HTTPError  # noqa: E402

from text_extraction.media.asr_external import (  # noqa: E402
    encode_ogg_opus,
    transcribe_external,
)
from text_extraction.media.asr_windows import transcribe_windows  # noqa: E402
from text_extraction.media.child import (  # noqa: E402
    _http_url,
    _HttpOnlyYoutubeDL,
    _NotHttp,
    ytdlp_params,
)
from text_extraction.media.sniff import MEDIA_HEAD_BYTES, sniff_media  # noqa: E402
from text_extraction.model_store import ModelsMissing  # noqa: E402

SAMPLE_RATE: Final = 16_000
# The protocols yt-dlp downloads itself (no FFmpeg, no rtmpdump).
_PROTOCOLS: Final = "[protocol~='^(https?|m3u8_native|http_dash_segments)$']"
# A platform video's audio, or its smallest video when it has no audio of its own.
FORMAT: Final = f"bestaudio{_PROTOCOLS}/best[height<=360]{_PROTOCOLS}"
# Outside programs yt-dlp hands some downloads to; the self-check fails when one is on PATH.
TOOLS: Final = ("ffmpeg", "rtmpdump")
# PyAV opens local files only: no network or other protocol through a playlist.
_AV_OPTIONS: Final = {"protocol_whitelist": "file"}
_READ_CHUNK: Final = 1024 * 1024
_FILE_PART: Final = "media.part"
_FFMPEG_LIBRARIES: Final = ("libavcodec", "libavformat", "libavutil", "libswresample")


class TooLarge(Exception):  # noqa: N818 - a cap, not an error
    """The media is over ``max_bytes``."""


class NotMedia(Exception):  # noqa: N818 - a refusal
    """The bytes are no audio or video."""


class HttpStatus(Exception):  # noqa: N818 - the target's answer; the message is the status
    """The media file's URL answered with an HTTP error."""


class IsLive(Exception):  # noqa: N818 - a refusal
    """The platform video is a live stream that is still running."""


class MediaTooLong(Exception):  # noqa: N818 - a limit
    """The audio is longer than ``max_duration``."""


class DecodeFailed(Exception):  # noqa: N818 - the parent maps this type name
    """PyAV cannot open the file, or no audio of it decodes."""


class DownloadFailed(Exception):  # noqa: N818 - the parent maps this type name
    """yt-dlp ended without a file."""


class EncodeFailed(Exception):  # noqa: N818 - the self-check names this type
    """The Opus encoding gave no Ogg Opus stream."""


# --- downloads -----------------------------------------------------------------------------


def _js_runtimes() -> dict[str, dict[str, str]]:
    """Deno from the ``deno`` package for YouTube's player challenges; none without it."""
    try:
        import deno  # optional, and only for platform videos
    except ImportError:
        return {}
    return {"deno": {"path": str(deno.find_deno_bin())}}


def _params(payload: Mapping[str, Any]) -> dict[str, Any]:
    """yt-dlp's options for a platform call: ruling M4's, with the format to download and a
    JavaScript runtime."""
    params = ytdlp_params({**payload, "extractors": [payload["extractor"]]})
    params.update(
        {
            "format": FORMAT,
            "js_runtimes": _js_runtimes(),
            "remote_components": [],
            "ignore_no_formats_error": False,
        }
    )
    return params


def platform_info(payload: dict[str, Any]) -> dict[str, Any]:
    """See the module docstring."""
    with _HttpOnlyYoutubeDL(_params(payload)) as ydl:
        info = ydl.extract_info(payload["url"], download=False)
        if isinstance(info, dict) and info.get("_type") == "playlist":
            entries = [entry for entry in info.get("entries") or () if isinstance(entry, dict)]
            info = entries[0] if entries else None
        if not isinstance(info, dict):
            raise DownloadFailed("no video")
        if info.get("live_status") == "is_live" or info.get("is_live") is True:
            raise IsLive
        sanitized = ydl.sanitize_info(info)
    return {"info": json.dumps(sanitized), "duration": _number(info.get("duration"))}


def download(payload: dict[str, Any]) -> dict[str, Any]:
    """See the module docstring."""
    scratch = Path(payload["scratch"])
    status: int | None = None
    if payload["source"] == "platform":
        path, duration = _platform_download(payload, scratch)
    else:
        (path, status), duration = _file_download(payload, scratch), None
    with path.open("rb") as file:
        head = file.read(MEDIA_HEAD_BYTES)
    kind = sniff_media(head, None)
    if kind is None:
        path.unlink(missing_ok=True)
        raise NotMedia
    final = path.with_name(f"media.{kind}")
    path.replace(final)
    if duration is None:
        duration = probe_duration(final)
    reply: dict[str, Any] = {"path": str(final), "kind": kind, "duration": duration}
    if status is not None:
        reply["status"] = status
    return reply


def _platform_download(payload: Mapping[str, Any], scratch: Path) -> tuple[Path, float | None]:
    info = json.loads(payload["info"])
    if not isinstance(info, dict):
        raise DownloadFailed("no info")
    params = _params(payload)
    params.update(
        {
            "skip_download": False,
            "outtmpl": {"default": "media.%(ext)s"},
            "paths": {"home": str(scratch), "temp": str(scratch)},
            "max_filesize": payload["max_bytes"],
            "hls_prefer_native": True,
            "fixup": "never",
            "postprocessors": [],
            "overwrites": True,
            "writeinfojson": False,
            "writesubtitles": False,
            "writethumbnail": False,
        }
    )
    with _HttpOnlyYoutubeDL(params) as ydl:
        result = ydl.process_ie_result(info, download=True)
    files = [entry for entry in scratch.iterdir() if entry.is_file() and entry.stem == "media"]
    files = [entry for entry in files if not entry.name.endswith((".part", ".ytdl"))]
    if not files:
        size = _number(info.get("filesize") or info.get("filesize_approx"))
        if size is not None and size > payload["max_bytes"]:
            raise TooLarge
        raise DownloadFailed("no file")
    path = files[0]
    if path.stat().st_size > payload["max_bytes"]:
        raise TooLarge
    duration = _number(result.get("duration")) if isinstance(result, dict) else None
    return path, duration


def _file_download(payload: Mapping[str, Any], scratch: Path) -> tuple[Path, int | None]:
    """One GET through the proxy, read up to ``max_bytes``; the file and the HTTP status."""
    params = ytdlp_params({**payload, "extractors": ["generic"]})
    max_bytes = int(payload["max_bytes"])
    path = scratch / _FILE_PART
    with _HttpOnlyYoutubeDL(params) as ydl:
        try:
            url = _http_url(payload["url"])
        except _NotHttp:
            raise NotMedia from None
        try:
            response = ydl.urlopen(Request(url, headers={"User-Agent": payload["user_agent"]}))
        except HTTPError as error:
            raise HttpStatus(str(error.status)) from None
        with response:
            status = getattr(response, "status", None)
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.strip().isdecimal() and int(declared) > max_bytes:
                raise TooLarge
            total = 0
            with path.open("wb") as file:
                while chunk := response.read(_READ_CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise TooLarge
                    file.write(chunk)
    return path, status if isinstance(status, int) else None


# --- PyAV ----------------------------------------------------------------------------------


def probe_duration(path: Path | str) -> float | None:
    """The media's duration in seconds from its container (or its first audio stream); None
    when neither says or PyAV cannot open it."""
    try:
        with av.open(str(path), options=dict(_AV_OPTIONS)) as container:
            if container.duration:
                return float(container.duration / av.time_base)
            stream = next(iter(container.streams.audio), None)
            if stream is not None and stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    except (av.error.FFmpegError, OSError, ValueError):
        return None
    return None


def decode_audio(path: Path | str, *, max_seconds: float) -> np.ndarray[Any, Any]:
    """The first audio stream as 16 kHz mono float32 samples; see the module docstring."""
    limit = int(max_seconds * SAMPLE_RATE)
    try:
        container = av.open(str(path), options=dict(_AV_OPTIONS))
    except (av.error.FFmpegError, OSError, ValueError):
        raise DecodeFailed("not readable") from None
    with container:
        stream = next(iter(container.streams.audio), None)
        if stream is None:
            raise DecodeFailed("no audio stream")
        expected = int(container.duration / av.time_base * SAMPLE_RATE) if container.duration else 0
        buffer = _Samples(min(max(expected, SAMPLE_RATE), limit + 1))
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        try:
            for packet in container.demux(stream):
                try:
                    frames = packet.decode()
                except (av.error.InvalidDataError, ValueError):
                    continue  # a damaged packet: the rest may decode
                for frame in frames:
                    for out in resampler.resample(frame):
                        buffer.add(out.to_ndarray().reshape(-1), limit)
            for out in resampler.resample(None):
                buffer.add(out.to_ndarray().reshape(-1), limit)
        except av.error.FFmpegError:
            if buffer.size == 0:
                raise DecodeFailed("no audio decoded") from None
    if buffer.size == 0:
        raise DecodeFailed("no audio decoded")
    return buffer.samples()


class _Samples:
    """A growing float32 buffer: one copy per doubling, not one per frame."""

    def __init__(self, capacity: int) -> None:
        self._data = np.empty(capacity, dtype=np.float32)
        self.size = 0

    def add(self, chunk: np.ndarray[Any, Any], limit: int) -> None:
        end = self.size + len(chunk)
        if end > limit:
            raise MediaTooLong
        if end > len(self._data):
            grown = np.empty(min(max(end, 2 * len(self._data)), limit + 1), dtype=np.float32)
            grown[: self.size] = self._data[: self.size]
            self._data = grown
        self._data[self.size : end] = chunk
        self.size = end

    def samples(self) -> np.ndarray[Any, Any]:
        return self._data[: self.size]


# --- faster-whisper ------------------------------------------------------------------------

_MODELS: dict[tuple[str, str, int], WhisperModel] = {}


def _model(path: str, compute_type: str, threads: int) -> WhisperModel:
    key = (path, compute_type, threads)
    model = _MODELS.get(key)
    if model is None:
        _MODELS.clear()  # one model per child
        model = WhisperModel(
            path,
            device="cpu",
            compute_type=compute_type,
            cpu_threads=threads,
            local_files_only=True,
        )
        _MODELS[key] = model
    return model


def transcribe(payload: dict[str, Any]) -> dict[str, Any]:
    """See the module docstring."""
    external = payload.get("backend") == "openai"
    model_path = Path(payload.get("model_path") or "")
    if not external and not (model_path / "model.bin").is_file():
        raise ModelsMissing(model_path.name)
    max_duration = float(payload["max_duration"])
    declared = probe_duration(payload["path"])
    if declared is not None and declared > max_duration:
        raise MediaTooLong(str(round(declared)))  # nothing decoded
    language = payload.get("language")
    known = language if language in _LANGUAGE_CODES else None
    if external:
        return _external(payload, max_duration, known)
    audio = decode_audio(payload["path"], max_seconds=max_duration)
    model = _model(str(model_path), payload["compute_type"], int(payload["threads"]))
    return transcribe_windows(model, audio, language=known)


def _external(
    payload: Mapping[str, Any], max_duration: float, language: str | None
) -> dict[str, Any]:
    """``transcribe`` on the external endpoint (``media/asr_external.py``), as the same reply."""
    transcription = transcribe_external(
        payload["path"],
        base_url=payload["base_url"],
        model=payload["model"],
        api_key=payload["api_key"],
        timeout=float(payload["timeout"]),
        languages=_LANGUAGE_CODES,
        language=language,
        max_duration=max_duration,
        budget=float(payload["budget"]),
        user_agent=payload.get("user_agent"),
        decode=lambda path: decode_audio(path, max_seconds=max_duration),
    )
    return {
        "segments": [[cue.start, cue.end, cue.text] for cue in transcription.segments],
        "language": transcription.language,
        "duration": transcription.duration,
    }


def opus_check(payload: dict[str, Any]) -> dict[str, Any]:
    """See the module docstring."""
    audio = decode_audio(payload["path"], max_seconds=float(payload["max_duration"]))
    encoded = encode_ogg_opus(audio)
    if not encoded.startswith(b"OggS") or b"OpusHead" not in encoded[:4096]:
        raise EncodeFailed("no Ogg Opus")
    return {"bytes": len(encoded)}


def ffmpeg(_payload: dict[str, Any]) -> dict[str, Any]:
    """See the module docstring."""
    meta: Mapping[str, Mapping[str, Any]] = av._core.library_meta
    libraries = {
        name: {
            "configuration": str(meta[name].get("configuration") or ""),
            "license": str(meta[name].get("license") or ""),
        }
        for name in meta
    }
    return {
        "libraries": libraries,
        "codecs": sorted(av.codecs_available),
        "tools": [tool for tool in TOOLS if shutil.which(tool)],
        "version": av.__version__,
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "platform_info": platform_info,
    "download": download,
    "transcribe": transcribe,
    "opus_check": opus_check,
    "ffmpeg": ffmpeg,
}
