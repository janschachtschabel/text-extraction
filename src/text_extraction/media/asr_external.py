"""Speech recognition on an external, OpenAI-compatible endpoint (``ASR_BACKEND=openai``,
ruling M11), for the ASR children (``media/asr_child.py``): the parent never imports this module,
the OpenAI SDK or PyAV.

``transcribe_external`` decodes the media as the local backend does (16 kHz mono float32,
``asr_child.decode_audio``), splits it into chunks of at most ``CHUNK_SECONDS`` (20 minutes)
with the local backend's window logic (``media/asr_windows.window_bounds``: each chunk ends in
the quietest 20 ms of its last 30 s, so no word is cut or heard twice), encodes each chunk as
16 kHz mono Opus at 32 kbit/s in Ogg (``encode_ogg_opus``, PyAV's ``libopus`` encoder: about
6 MB per chunk, below the 25 MB that OpenAI takes) and posts it to ``<base_url>/audio/
transcriptions`` (``model``, ``response_format=verbose_json``, ``language`` when known). The
segments of a chunk are moved by the chunk's start onto the file's clock. An endpoint that
refuses ``verbose_json`` (400 or 422) gets the chunk again as ``json``, and so do the later
chunks; a ``json`` answer, or a ``verbose_json`` one without segments, is one segment over its
chunk. The language: the requested one, else the code the first chunk with speech answers with,
passed on to the later chunks, when it is one of Whisper's language codes (``languages``);
anything else (OpenAI answers a name such as ``german``) is not taken: no language is sent, and
the pipeline then reads the language from the text.

**The client** (OpenAI SDK 3.29, on httpx2) is the operator's endpoint, called directly, not
through the egress proxy (``ASR_EXTERNAL_BASE_URL`` is https-only operator configuration, see
``settings.py``): ``trust_env=False`` (no proxy, ``.netrc`` or CA settings from the environment,
which the child's allowlisted environment does not carry anyway), no redirects (a ``3xx`` is a
failure, so a request with the key never goes to another host), ``max_retries=0`` (the item's
outcome says whether to try again; retries would multiply the time a chunk takes) and our user
agent. Each chunk's whole request (upload, the endpoint's work, the answer) gets ``timeout``
(``ASR_EXTERNAL_TIMEOUT_SECONDS``) or what is left of ``budget`` (the item's
``ASR_TIMEOUT_SECONDS``), whichever is less. As for the LLM client (``llm/client.py``) the
``Authorization`` header is set and ``OpenAI-Organization`` and ``OpenAI-Project`` are omitted
explicitly, so the SDK's ``OPENAI_*`` environment never reaches the endpoint (issue #19; the
child's environment is allowlisted and carries none of it, and start-up refuses
``OPENAI_CUSTOM_HEADERS`` and ``OPENAI_LOG`` with the external backend).

**Failures** are raised as the types the worker maps, with a status code or a fixed word as the
message and outside the SDK's exception, so neither the message nor a traceback's context can
carry what the SDK's text quotes (the endpoint's answer, which may echo the key):

- ``ExternalRejected`` (``401``, ``403``): the key was refused; not retryable.
- ``ExternalUnavailable`` (``408``, ``409``, ``425``, ``429``, ``5xx``, ``timeout``,
  ``connection``): retryable.
- ``ExternalFailed`` (any other status, a redirect, an answer that is not a transcription):
  not retryable.
- ``BudgetSpent``: the item's budget ended before the last chunk was transcribed.

The key is an argument, never logged; the child silences all logging before it imports this.
"""

import io
import json
import threading
import time
from collections.abc import Callable, Container
from pathlib import Path
from typing import Any, Final, TypeGuard

import httpx2
import numpy as np
import openai

from text_extraction.media.asr_windows import SAMPLE_RATE, window_bounds
from text_extraction.media.captions import Cue
from text_extraction.media.transcript import Transcription

__all__ = [
    "CHUNK_SECONDS",
    "OPUS_BIT_RATE",
    "BudgetSpent",
    "ExternalFailed",
    "ExternalRejected",
    "ExternalUnavailable",
    "encode_ogg_opus",
    "transcribe_external",
]

CHUNK_SECONDS: Final = 1200
OPUS_BIT_RATE: Final = 32_000
_FILE_NAME: Final = "audio.ogg"
_CONTENT_TYPE: Final = "audio/ogg"
_REJECTED: Final = frozenset({401, 403})
_RETRYABLE: Final = frozenset({408, 409, 425, 429})
_NO_VERBOSE: Final = frozenset({"400", "422"})  # the endpoint takes no ``verbose_json``
# A chunk's request gets at least this long; a budget with less left sends nothing more.
_MIN_SECONDS: Final = 1.0

type Audio = np.ndarray[Any, np.dtype[np.float32]]


class ExternalRejected(Exception):  # noqa: N818 - the parent maps this type name
    """The endpoint refused the key (401, 403); the message is the status."""


class ExternalUnavailable(Exception):  # noqa: N818 - the parent maps this type name
    """The endpoint was busy, failed or did not answer in time; the message is the status,
    ``timeout`` or ``connection``."""


class ExternalFailed(Exception):  # noqa: N818 - the parent maps this type name
    """The endpoint answered something that is no transcription; the message is the status or
    ``unreadable``."""


class BudgetSpent(Exception):  # noqa: N818 - the parent maps this type name
    """The item's budget ended before the transcription."""


def encode_ogg_opus(samples: Audio) -> bytes:
    """16 kHz mono float32 ``samples`` as Opus at ``OPUS_BIT_RATE`` in Ogg (PyAV's ``libopus``;
    PyAV frames them for the encoder)."""
    import av  # in the ASR children only

    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=SAMPLE_RATE, layout="mono")
        stream.bit_rate = OPUS_BIT_RATE
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(samples, dtype=np.float32).reshape(1, -1),
            format="flt",
            layout="mono",
        )
        frame.sample_rate = SAMPLE_RATE
        frame.pts = 0
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def _decoder(max_duration: float | None) -> Callable[[str], Audio]:
    def decode(path: str) -> Audio:
        # The ASR child has imported it already (it imports this module).
        from text_extraction.media.asr_child import decode_audio

        return decode_audio(path, max_seconds=max_duration or float("inf"))

    return decode


def _client(base_url: str, api_key: str, *, timeout: float, user_agent: str | None) -> Any:
    """The OpenAI client of one transcription; see the module docstring."""
    http = httpx2.Client(follow_redirects=False, trust_env=False, timeout=timeout)
    # Issue #19: the SDK would take these from OPENAI_ORG_ID, OPENAI_PROJECT_ID and an
    # Authorization line of OPENAI_CUSTOM_HEADERS; omitted and set here, they never do.
    headers: dict[str, Any] = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Organization": openai.omit,
        "OpenAI-Project": openai.omit,
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    return openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
        http_client=http,
        default_headers=headers,
    )


def transcribe_external(
    path: str | Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    languages: Container[str],
    chunk_seconds: float = CHUNK_SECONDS,
    language: str | None = None,
    max_duration: float | None = None,
    budget: float | None = None,
    user_agent: str | None = None,
    decode: Callable[[str], Audio] | None = None,
    encode: Callable[[Audio], bytes] = encode_ogg_opus,
) -> Transcription:
    """The transcription of the media at ``path`` by the endpoint at ``base_url``; see the
    module docstring. ``languages`` are the codes a detected language must be one of
    (Whisper's, ``faster_whisper.tokenizer._LANGUAGE_CODES``), ``budget`` the seconds the item
    has left (None: no limit), ``max_duration`` the decoder's limit
    (``MEDIA_MAX_DURATION_SECONDS``). ``decode`` and ``encode`` default to PyAV."""
    if budget is not None and budget < _MIN_SECONDS:
        raise BudgetSpent
    deadline = time.monotonic() + budget if budget is not None else None
    audio = (decode or _decoder(max_duration))(str(path))
    client = _client(base_url, api_key, timeout=timeout, user_agent=user_agent)
    segments: list[Cue] = []
    chosen = language
    verbose = True
    try:
        for start, end in window_bounds(audio, window_seconds=chunk_seconds):
            chunk = encode(audio[start:end])
            seconds, capped = _chunk_timeout(timeout, deadline)
            data, verbose = _post(
                client, chunk, model, chosen, verbose, timeout=seconds, capped=capped
            )
            offset = start / SAMPLE_RATE
            cues = _segments(data, offset, (end - start) / SAMPLE_RATE, verbose)
            segments.extend(cues)
            if chosen is None and cues:  # a chunk of silence or music does not decide it
                answered = data.get("language")
                if isinstance(answered, str) and answered in languages:
                    chosen = answered
    finally:
        client.close()
    return Transcription(
        segments=segments,
        language=chosen or "",
        duration=len(audio) / SAMPLE_RATE,
        model=model,
    )


def _chunk_timeout(timeout: float, deadline: float | None) -> tuple[float, bool]:
    """The chunk's timeout, and whether the item's budget (not the chunk's own) is what ends
    it."""
    if deadline is None:
        return timeout, False
    left = deadline - time.monotonic()
    if left < _MIN_SECONDS:
        raise BudgetSpent
    return (left, True) if left < timeout else (timeout, False)


def _post(
    client: Any,
    chunk: bytes,
    model: str,
    language: str | None,
    verbose: bool,
    *,
    timeout: float,
    capped: bool,
) -> tuple[dict[str, Any], bool]:
    """One chunk's answer as JSON, and whether it is ``verbose_json``; ``verbose`` is whether to
    ask for that first."""
    if verbose:
        failure, data = _request(client, chunk, model, language, "verbose_json", timeout, capped)
        if failure is None:
            return data, True
        if not (isinstance(failure, ExternalFailed) and str(failure) in _NO_VERBOSE):
            raise failure
    failure, data = _request(client, chunk, model, language, "json", timeout, capped)
    if failure is not None:
        raise failure
    return data, False


def _request(
    client: Any,
    chunk: bytes,
    model: str,
    language: str | None,
    response_format: str,
    timeout: float,
    capped: bool,
) -> tuple[Exception | None, dict[str, Any]]:
    """The request, with its failure as a value: raised by the caller, outside the SDK's
    exception (see the module docstring). ``timeout`` bounds the whole request: the SDK's own
    timeout bounds each phase (connect, each write, each read), so an answer that trickles in
    could outlast it; the request runs on a thread of its own that is given up at the bound (its
    own timeouts end it soon after, and the client is closed when the transcription ends)."""
    extra: dict[str, Any] = {"language": language} if language else {}
    outcome: list[object] = []

    def call() -> None:
        try:
            raw = client.audio.transcriptions.with_raw_response.create(
                model=model,
                file=(_FILE_NAME, chunk, _CONTENT_TYPE),
                response_format=response_format,
                timeout=timeout,
                **extra,
            )
            outcome.append(json.loads(raw.text))
        except BaseException as error:  # mapped by the caller, never printed
            outcome.append(error)

    worker = threading.Thread(target=call, name="asr-external-request", daemon=True)
    worker.start()
    worker.join(timeout)
    if not outcome:  # the whole request outlasted its bound
        return _timed_out(capped), {}
    found = outcome[0]
    if isinstance(found, BaseException):
        return _failure_of(found, capped), {}
    if not isinstance(found, dict):
        return ExternalFailed("unreadable"), {}
    return None, found


def _timed_out(capped: bool) -> Exception:
    return BudgetSpent() if capped else ExternalUnavailable("timeout")


def _failure_of(error: BaseException, capped: bool) -> Exception:
    """The failure an exception of the request maps to; one that is not the request's own (a
    ``MemoryError``, say) is raised as it is."""
    if isinstance(error, openai.APITimeoutError):
        return _timed_out(capped)
    if isinstance(error, openai.APIConnectionError):
        return ExternalUnavailable("connection")
    if isinstance(error, openai.APIStatusError):
        return _status_failure(error.status_code)
    if isinstance(error, openai.OpenAIError | httpx2.HTTPError | ValueError | UnicodeError):
        return ExternalFailed("unreadable")
    raise error


def _status_failure(status: int) -> Exception:
    if status in _REJECTED:
        return ExternalRejected(str(status))
    if status in _RETRYABLE or status >= 500:
        return ExternalUnavailable(str(status))
    return ExternalFailed(str(status))


def _segments(data: dict[str, Any], offset: float, length: float, verbose: bool) -> list[Cue]:
    """A chunk's answer as cues on the file's clock: its segments (``verbose_json``), else its
    text over the chunk's length; empty texts are dropped."""
    found = data.get("segments") if verbose else None
    cues: list[Cue] = []
    if isinstance(found, list):
        for segment in found:
            if not isinstance(segment, dict):
                continue
            start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
            if not (_number(start) and _number(end) and isinstance(text, str)):
                continue
            start, end = float(start), max(float(end), float(start))
            if text.strip():
                cues.append(Cue(round(start + offset, 3), round(end + offset, 3), text.strip()))
        if cues:
            return cues
    text = data.get("text")
    if not isinstance(text, str):
        raise ExternalFailed("unreadable")
    if not text.strip():
        return []
    return [Cue(round(offset, 3), round(offset + length, 3), text.strip())]


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)
