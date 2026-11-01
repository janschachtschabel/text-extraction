"""The ASR worker (entry point ``text-extraction-asr-worker``): transcribes the media of the
``asr`` pool with faster-whisper, or on an external OpenAI-compatible endpoint, and stores the
results in the job (rulings M10, M11).

It is the extract worker's ``Worker`` (``worker.py``: slot loops, leases, exhausted items, result
writes, metrics, ``/ready`` and the shutdown) with another runner; a transcript of a job with
``pii`` moves on to the ``nlp`` pool there (``pii_dispatch.PiiMove``), and the item's input,
draft and key are then left to the nlp worker. The ``asr_dispatch`` module
docstring has the contract of the pool's items: their job, input, draft and key. This process
never imports faster-whisper, CTranslate2, PyAV or yt-dlp: only its children do
(``media/asr_child.py``), so a crash, a hang or a memory blow-up of a download or a
transcription ends there.

**The backends** (``ASR_BACKEND``). ``local``: faster-whisper with the model on
``ASR_MODELS_PATH``; the caller's key of an external endpoint (``JobStore.asr_key``) is not
read. ``openai``: the child decodes as for ``local``, encodes the audio as Opus and posts it in
chunks of at most 20 minutes to ``ASR_EXTERNAL_BASE_URL`` (``media/asr_external.py``, called
directly, not through the egress proxy: the endpoint is the operator's), with the key the item
has: the caller's ``X-ASR-Api-Key`` (stored with the job) wins over ``ASR_EXTERNAL_API_KEY``;
without either the item fails ``transcription_failed``, not retryable, before anything is
downloaded (the API answers such requests 400, so this is a fallback). The key travels in the
child call's payload only: never in the children's environment, a log line, a result or the
job's view. No model is needed.

**The runner** (``AsrRunner``) leases an item for ``asr_lease_seconds`` (``2 *
ASR_TIMEOUT_SECONDS + 10``). ``ASR_TIMEOUT_SECONDS`` is the budget of the whole item, from the
claim: the wait for a turn, the download (``MEDIA_DOWNLOAD_TIMEOUT_SECONDS`` or what is left of
the budget, whichever ends first) and the transcription (the rest). For each item it:

1. reads the item's input (``MediaInputMeta`` and an upload's bytes) and its draft (a platform
   video's metadata result, None for a file or an upload). An input that expired before the
   claim answers the draft with ``asr_unavailable`` (``draft_without_asr``), neither
   fingerprinted nor cached; without a draft the item fails ``timeout``, retryable;
2. checks the duration the platform said (the draft's probe): over
   ``MEDIA_MAX_DURATION_SECONDS`` it is ``media_too_long`` and nothing is downloaded;
3. puts the media into a directory of its own under ``ASR_SCRATCH_PATH`` (``item-<random>``,
   mode 0700; the file is ``upload.<kind>`` or ``media.<ext>``, never the caller's file name):
   an upload's bytes are written there; a URL's media is downloaded by a child through the
   worker's ``EgressProxy``, each child call with a ``ProxyContext`` of its own (its own
   credentials and byte cap: ``MEDIA_EXTRACT_MAX_BYTES`` for ``platform_info``,
   ``MEDIA_MAX_BYTES`` plus 5 MB of slack for headers and segment overhead for ``download``;
   the child stops a file at ``MEDIA_MAX_BYTES``). A platform video is two calls:
   ``platform_info`` (yt-dlp's info, with the extractor of the input as its only allowed one)
   in the turn of the canonical URL's host at the outbound gate, then ``download`` of that info
   outside it (the media comes from the platform's CDN). A media file is one ``download`` in
   its host's turn, whose answer status goes to the gate and into the result
   (``upstream_status``; a platform video's comes from its draft). Once it holds the turn, an
   item that got its result meanwhile (a copy that ran twice) downloads nothing;
4. checks the duration again (yt-dlp's or PyAV's): over the limit it is ``media_too_long``;
5. checks whether the item has its result already (a copy that ran twice after a crash): then
   it stops before the long transcription and its result stands;
6. transcribes the file in a child (``transcribe``) by the item's deadline: the child refuses
   a container longer than ``MEDIA_MAX_DURATION_SECONDS`` before decoding it, PyAV decodes the
   first audio stream to 16 kHz mono float32 (stopping past the limit too) and faster-whisper
   reads that array in windows of at most 10 minutes (``media/asr_windows.py``) with
   ``ASR_MODEL`` from ``ASR_MODELS_PATH`` (``ASR_COMPUTE_TYPE``, ``ASR_THREADS``), in the first
   requested language or the one it detects; with the external backend each chunk gets
   ``ASR_EXTERNAL_TIMEOUT_SECONDS`` or what is left of the item's budget, whichever is less;
7. runs ``Pipeline.transcribed_result``: the transcript in the result's shape, the draft's
   details, ``transcript_source: asr``; a URL's result goes to the fingerprints and the result
   cache, an upload's does not. Media without speech is ``no_transcript_available``: a platform
   video answers its draft (the details) with that error, a file or an upload fails;
8. deletes the scratch directory on every path, a crash or a cancellation included, and leaves
   the input, the draft and the key to the ``Worker``, which deletes them after the result
   write. ``clean_scratch`` removes what a killed process left, at start-up.

A gate refusal of a download (``OutboundBusy``, ``CircuitOpen``) defers the item as in the extract
worker. Failures map to codes, never to a library's text: a download that ran out of
``MEDIA_DOWNLOAD_TIMEOUT_SECONDS`` is ``timeout``, retryable; an item that ran out of
``ASR_TIMEOUT_SECONDS`` (in the download or the transcription) is ``timeout``, not retryable (the
same media would take as long again); a decoder error is ``transcription_failed``; a child that
crashed is ``transcription_failed``, retryable, one out of memory not; a model missing on the volume
is ``internal_error``, retryable; the external endpoint refusing the key (401, 403) is
``transcription_failed``, not retryable, one that is busy, failing or slow (429, 5xx, a chunk's
timeout, not reachable) retryable, any other answer not (the message names the status, never the
endpoint's text); the item's budget ending during the external transcription is ``timeout``, not
retryable; a file over ``MEDIA_MAX_BYTES`` ``too_large``; a target's HTTP
error ``target_http_error`` with its status (retryable for 408, 425, 429 and 5xx); a live stream or
bytes that are no media ``unsupported_content_type``; a request the proxy refused ``blocked_url``,
``too_large``, ``dns_error`` or ``connection_error`` (also when the child saw the proxy's own 403 or
502 as the URL's status); any other failed download ``transcription_failed``, retryable. With a
draft, the draft answers with the failure's error and ``asr_unavailable``, and keeps its fingerprint
only when the failure is final.

**The children** (``DocumentPool``, ``ASR_CONCURRENCY`` of them with ``ASR_MEMORY_LIMIT_MB`` of
address space) get the thread count (``OMP_NUM_THREADS``) and Hugging Face's offline switch
through their own environment, never this process's secrets. Each loads the model once and
keeps it.

**The process** serves only the internal port. Before anything else it checks the model's
files (``model_store``, manifest ``media/models.toml``, only the folder of ``ASR_MODEL``; not with
the external backend): a missing one ends the process with exit code 1 and a log line naming
``ASR_MODELS_PATH``. The external backend without ``ASR_EXTERNAL_BASE_URL`` ends it with exit
code 2 (``invalid_configuration``). Then
it starts the proxy and the children, and each child runs the self-check: PyAV's FFmpeg must be
an LGPL build (``ffmpeg_gpl_findings`` on ``av._core.library_meta``, PyAV 19.0.1's per-library
``configuration`` and ``license``; ``av.library_versions`` has the versions only), so neither
``--enable-gpl``, ``--enable-nonfree`` nor a GPL or non-free external library such as libx264
(decision D5: the PyPI wheel of av bundles libx264 and libx265 and fails the check, by design;
the ASR image builds PyAV against its own LGPL FFmpeg). Every library PyAV reports is read,
with ``_`` read as ``-`` in its switches; no GPL or non-free encoder may be among PyAV's codecs
(``av.codecs_available``: libx264, libx264rgb, libx265, libxvid, libfdk_aac, libxavs,
libxavs2, libdavs2); and neither ``ffmpeg`` nor ``rtmpdump`` may be on the child's ``PATH``
(yt-dlp would hand them protocols it does not download itself, bypassing these checks; the
child also allows only ``https?``, ``m3u8_native`` and ``http_dash_segments`` formats). The ASR
image (Task 11) ships neither program. Last, the built-in clip (``media/asr_check.ogg``,
German speech) must transcribe to its words; with the external backend it is decoded and
encoded as Ogg Opus instead (``opus_check``: the encoding runs locally, so PyAV's FFmpeg must
have the ``libopus`` encoder), and nothing is sent to the endpoint. Children that do not start
(``asr_start_failed``) or a failed check (``asr_self_check_failed``) end the process before
``/ready`` with exit code 1. On SIGTERM the loops stop claiming, the items in flight get 30 s,
then they are cancelled and the children killed. A cancelled item goes back to the queue at once
(``Worker`` with ``give_back_on_cancel``: the front of its domain, ``attempts + 1``), not when
its lease of hours expires; another worker runs it again from the start (a download, a
transcription and, with the external backend, the endpoint's bill again). An item given back
``JOB_ITEM_MAX_ATTEMPTS`` times is failed as lost. A scale-down or a rolling update thus
interrupts the transcriptions in flight: the Helm chart counts leased items in the pool's
scaling metric and keeps a long scale-down stabilisation window for that reason.

- ``--download-models`` fills ``ASR_MODELS_PATH`` with the files of ``ASR_MODEL`` (the Helm init
  container and the compose one-shot service run it; idempotent) and exits 0, or 1; with the
  external backend there is nothing to download (``asr_models_not_needed``, exit 0).
- ``--check`` checks the model, FFmpeg's licence, the programs on ``PATH`` and the built-in
  clip in one child (the external backend: no model, the clip encoded instead of transcribed),
  then exits 0, or 1 on any failure (the image build runs it). It leaves
  ``ASR_SCRATCH_PATH`` alone: run next to a live worker (a probe, a debug shell), it must not
  delete the scratch of the items in flight.

The names used of the pinned libraries (``media/asr_child.py``): faster-whisper 1.2.1
``WhisperModel(path, device="cpu", compute_type=, cpu_threads=, local_files_only=True)`` and
``WhisperModel.transcribe(audio, language=, vad_filter=True, condition_on_previous_text=False)``
(segments ``start``, ``end``, ``text``; info ``language``, ``duration``); PyAV 19.0.1 ``av.open``,
``av.AudioResampler(format="flt", layout="mono", rate=16000)``, ``av.error.InvalidDataError``;
yt-dlp's ``YoutubeDL.extract_info``, ``process_ie_result`` and ``sanitize_info``.

Metrics: ``text_extraction_asr_transcriptions_total{backend, outcome}`` (``success`` or the error
code), ``text_extraction_asr_audio_seconds_total{backend}`` (audio transcribed, the external
backend's cost basis) and ``text_extraction_asr_seconds{backend}`` (time of the transcription
calls); ``backend`` is ``ASR_BACKEND``. Logs carry job ids,
sources, platforms, codes and type names only, never a URL, a video id, a file name, a key, a
library's message or transcript text.
"""

import argparse
import asyncio
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Final, Literal, Protocol, get_args
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

import structlog
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram
from redis.asyncio import Redis

from text_extraction import model_store
from text_extraction.asr_dispatch import asr_queue, draft_without_asr
from text_extraction.browser_proxy import TUNNEL_GRACE_SECONDS, EgressProxy, ProxyContext
from text_extraction.conversion_pool import ConversionPool
from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
    DocumentPool,
)
from text_extraction.envelope import Envelope, ErrorCode
from text_extraction.extract_models import ExtractRequest, ExtractResult, failed_extract
from text_extraction.fetch import USER_AGENT, Fetcher
from text_extraction.file_models import form_of
from text_extraction.internal import Readiness, create_internal_app
from text_extraction.jobs import JobRecord, JobStore, asr_lease_seconds, job_principal
from text_extraction.llm_dispatch import create_llm_move
from text_extraction.media.captions import Cue
from text_extraction.media.transcript import Transcription
from text_extraction.media.types import MediaKind
from text_extraction.memory_guard import MemoryGuard
from text_extraction.outbound import CircuitOpen, DomainGate, OutboundBusy
from text_extraction.pii_dispatch import create_pii_move
from text_extraction.pipeline import Pipeline, create_outbound, create_pipeline
from text_extraction.pipeline_media import MediaInputMeta, media_too_long_message
from text_extraction.scheduler import QueueItem
from text_extraction.server import EXIT_CONFIG_ERROR, _Server, _server, configured_settings
from text_extraction.settings import Settings
from text_extraction.valkey import create_valkey
from text_extraction.worker import Worker, _serve

__all__ = [
    "ASR_TARGET",
    "AsrRunner",
    "AsrStartFailed",
    "AsrWorkerParts",
    "SelfCheckFailed",
    "build_asr_worker",
    "clean_scratch",
    "create_asr_pool",
    "create_asr_proxy",
    "ffmpeg_gpl_findings",
    "main",
    "self_check",
]

_log = structlog.get_logger(__name__)

# The ``DocumentPool`` target of the ASR children.
ASR_TARGET: Final = "text_extraction.media.asr_child"
# What the items in flight get after SIGTERM before they are cancelled.
GRACEFUL_SECONDS: Final = 30
# Importing faster-whisper and CTranslate2 takes a while on a cold volume.
_CHILD_START_SECONDS: Final = 180.0
# The scratch directory of one item: ``clean_scratch`` removes only these.
_SCRATCH_PREFIX: Final = "item-"
# The platform's info (one request or a few) gets at most this much of the download's time.
_INFO_SECONDS: Final = 120.0
# A download's proxy context holds the media and this much more: headers, redirects, manifests.
_DOWNLOAD_SLACK_BYTES: Final = 5_000_000
# A child call gets at least this long, even when the budget is nearly spent.
_MIN_CALL_SECONDS: Final = 1.0
# The built-in clip of the self-check and words its transcript must have.
_CHECK_CLIP: Final = "asr_check.ogg"
_CHECK_WORDS: Final = ("gregor", "morgens")
_CHECK_LANGUAGE: Final = "de"
# The FFmpeg libraries PyAV decodes with; each must be reported (every reported one is checked).
_FFMPEG_LIBRARIES: Final = ("libavcodec", "libavformat", "libavutil", "libswresample")
# Configure switches of FFmpeg 7.1 that make a build GPL or non-free (spelled with ``-``; a
# configuration's ``_`` spellings are read as ``-``): the switches themselves, libpostproc, and
# configure's EXTERNAL_LIBRARY_GPL_LIST, EXTERNAL_LIBRARY_GPLV3_LIST,
# EXTERNAL_LIBRARY_NONFREE_LIST and HWACCEL_LIBRARY_NONFREE_LIST.
_FFMPEG_GPL_SWITCHES: Final = frozenset(
    f"--enable-{name}"
    for name in (
        "gpl",
        "nonfree",
        "postproc",
        "avisynth",
        "frei0r",
        "libcdio",
        "libdavs2",
        "libdvdnav",
        "libdvdread",
        "librubberband",
        "libvidstab",
        "libx264",
        "libx265",
        "libxavs",
        "libxavs2",
        "libxvid",
        "libsmbclient",
        "decklink",
        "libfdk-aac",
        "libtls",
        "cuda-nvcc",
        "cuda-sdk",
        "libnpp",
    )
)
# Codecs of GPL or non-free libraries (``av.codecs_available``): whatever the configuration
# says, PyAV must not be able to open one.
_FFMPEG_GPL_CODECS: Final = frozenset(
    {
        "libx264",
        "libx264rgb",
        "libx265",
        "libxvid",
        "libfdk_aac",
        "libxavs",
        "libxavs2",
        "libdavs2",
    }
)
# Programs yt-dlp would run when it finds them on PATH (merging, fix-ups, RTMP): the ASR image
# ships neither, so no download leaves our own HTTP handlers.
_FORBIDDEN_TOOLS: Final = ("ffmpeg", "rtmpdump")
_SECONDS_BUCKETS: Final = (5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 14_400)
# Statuses of a media file's answer that may pass when retried later.
_RETRYABLE_STATUSES: Final = frozenset({408, 425, 429})

INPUT_EXPIRED_MESSAGE: Final = "The media expired before a worker transcribed it. Send it again."
_ITEM_TIMEOUT_MESSAGE: Final = (
    "The media was not downloaded and transcribed within ASR_TIMEOUT_SECONDS."
)
_DOWNLOAD_TIMEOUT_MESSAGE: Final = (
    "The media did not download within MEDIA_DOWNLOAD_TIMEOUT_SECONDS. Try again later."
)
_DOWNLOAD_FAILED_MESSAGE: Final = "The media could not be downloaded. Try again later."
_CRASHED_MESSAGE: Final = "The transcription failed on our side. Try again later."
_OUT_OF_MEMORY_MESSAGE: Final = "The transcription needed more memory than it may use."
_DECODE_MESSAGE: Final = "The audio could not be decoded."
_FAILED_MESSAGE: Final = "The transcription failed."
_MODELS_MISSING_MESSAGE: Final = "Transcription is not available at the moment. Try again later."
_NO_KEY_MESSAGE: Final = (
    "The service transcribes with an external endpoint and has no API key for it: send yours "
    "in the X-ASR-Api-Key header."
)
_REJECTED_MESSAGE: Final = "The transcription endpoint refused the API key{detail}."
_UNAVAILABLE_MESSAGE: Final = "The transcription endpoint is unavailable{detail}. Try again later."
_EXTERNAL_FAILED_MESSAGE: Final = (
    "The transcription endpoint could not transcribe the audio{detail}."
)
# What an external failure's message may say (the child's messages are a status or these words).
_EXTERNAL_DETAILS: Final = {
    "timeout": " (timeout after ASR_EXTERNAL_TIMEOUT_SECONDS)",
    "connection": " (not reachable)",
}
_NOT_MEDIA_MESSAGE: Final = "The file is not audio or video that can be transcribed."
_LIVE_MESSAGE: Final = "The URL is a live stream that is still running."
_BLOCKED_ADDRESS_MESSAGE: Final = (
    "The download tried to reach an address that is not public, which is not allowed."
)
_DNS_MESSAGE: Final = "The media's host name could not be resolved. Try again later."
_CONNECT_MESSAGE: Final = "The media's host could not be reached. Try again later."
_BUSY_MESSAGE: Final = "Too many requests to the media's site are waiting. Try again later."
_PAUSED_MESSAGE: Final = "Requests to the media's site are paused. Try again later."
# Failures of a download whose own type says more than a refused request of the proxy.
_KEEP_OVER_REFUSAL: Final = frozenset({"TooLarge", "NotMedia", "IsLive", "MediaTooLong"})


class AsrChildren(Protocol):
    """The part of ``DocumentPool`` the runner and the self-check use (a fake in tests)."""

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any: ...


class StartableChildren(AsrChildren, Protocol):
    """``AsrChildren`` with the start of the children (a fake in tests)."""

    async def start(self) -> None: ...


class Egress(Protocol):
    """The part of ``EgressProxy`` the runner uses (a fake in tests)."""

    @property
    def url(self) -> str: ...

    def open_context(self, max_bytes: int | None = None) -> ProxyContext: ...


def create_asr_pool(settings: Settings) -> DocumentPool:
    """The ASR children of a worker (``ASR_CONCURRENCY``, ``ASR_MEMORY_LIMIT_MB``); not started
    yet. The thread count and Hugging Face's offline switch go through the children's own
    environment; Deno (yt-dlp's JavaScript runtime) keeps its cache in the temporary folder."""
    temp = Path(tempfile.gettempdir())
    return DocumentPool(
        settings.asr_concurrency,
        settings.asr_memory_limit_mb,
        target=ASR_TARGET,
        start_timeout=_CHILD_START_SECONDS,
        extra_env={
            "OMP_NUM_THREADS": str(settings.asr_threads),
            # Nothing is fetched: the model comes from the volume only.
            "HF_HUB_OFFLINE": "1",
            "HF_HOME": str(temp / "text-extraction-hf"),
            "DENO_DIR": str(temp / "text-extraction-deno"),
            "DENO_NO_UPDATE_CHECK": "1",
        },
    )


def create_asr_proxy(settings: Settings, registry: CollectorRegistry) -> EgressProxy:
    """The children's only way out; its tunnels live a whole download and a grace."""
    return EgressProxy(
        settings,
        registry=registry,
        tunnel_timeout=settings.media_download_timeout_seconds + TUNNEL_GRACE_SECONDS,
    )


# --- metrics -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Metrics:
    transcriptions: Counter
    audio_seconds: Counter
    seconds: Histogram


_METRICS: WeakKeyDictionary[CollectorRegistry, _Metrics] = WeakKeyDictionary()


def _metrics_for(registry: CollectorRegistry) -> _Metrics:
    metrics = _METRICS.get(registry)
    if metrics is None:
        metrics = _Metrics(
            transcriptions=Counter(
                "text_extraction_asr_transcriptions_total",
                "Transcriptions by backend and outcome (success or the error code).",
                ["backend", "outcome"],
                registry=registry,
            ),
            audio_seconds=Counter(
                "text_extraction_asr_audio_seconds_total",
                "Seconds of audio transcribed, by backend.",
                ["backend"],
                registry=registry,
            ),
            seconds=Histogram(
                "text_extraction_asr_seconds",
                "Duration of transcriptions (the speech recognition), by backend.",
                ["backend"],
                buckets=_SECONDS_BUCKETS,
                registry=registry,
            ),
        )
        _METRICS[registry] = metrics
    return metrics


# --- the runner ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Failure:
    """Why an item has no transcription: its error and the target's status. ``final`` says
    that the failure belongs to the media (too long, too large, not media, undecodable, the
    item's own budget, out of memory, a refused address, the site's own error answer), so a
    platform video's details may be stored as its answer; a failure of the caller or of the
    endpoint (a refused or missing key, the endpoint's 4xx) or one that may pass is never
    final, whether retryable or not: the result cache is shared by every caller."""

    code: ErrorCode
    message: str
    retryable: bool
    upstream_status: int | None = None
    final: bool = False

    def envelope(self) -> Envelope[ExtractResult]:
        return failed_extract(self.code, self.message, retryable=self.retryable)


@dataclass(frozen=True)
class _Media:
    """The downloaded or written media of an item, in its scratch directory."""

    path: Path
    kind: MediaKind | None
    duration: float | None
    fetch_ms: int = 0
    status: int | None = None  # the media file's answer


class _Superseded(Exception):  # noqa: N818 - not an error: the item has its result
    """The item got its result while this copy downloaded."""


class AsrRunner:
    """The ``asr`` pool's ``ItemRunner`` (either backend); see the module docstring.
    ``children`` downloads and transcribes, ``egress`` is their proxy, ``gate`` the outbound
    gate whose turns the downloads take."""

    def __init__(
        self,
        pipeline: Pipeline,
        store: JobStore,
        settings: Settings,
        children: AsrChildren,
        egress: Egress,
        gate: DomainGate,
        registry: CollectorRegistry,
    ) -> None:
        self._pipeline = pipeline
        self._store = store
        self._settings = settings
        self._children = children
        self._egress = egress
        self._gate = gate
        self._metrics = _metrics_for(registry)
        self._backend = settings.asr_backend
        self.lease_seconds = asr_lease_seconds(settings)

    async def run(
        self,
        job: JobRecord,
        item: QueueItem,
        url: str,
        *,
        gate_refusals: Literal["fail", "raise"],
    ) -> Envelope[ExtractResult] | None:
        # The worker deletes the input, the draft and the key once the result is written
        # (``Worker._drop_input``): an item that runs again finds them.
        stored = await self._store.get_input(job.id, item.index, MediaInputMeta)
        draft = await self._store.draft(job.id, item.index)
        if stored is None:
            _log.info("asr_input_expired", draft=draft is not None)
            fallback = draft_without_asr(draft)
            if fallback is not None:
                return fallback
            return failed_extract(ErrorCode.TIMEOUT, INPUT_EXPIRED_MESSAGE, retryable=True)
        body, meta = stored
        request = (
            form_of(job.options)
            if job.kind == "file"
            else ExtractRequest(url=url, **job.options.model_dump())
        )
        try:
            outcome = await self._transcription(job, item, meta, body, gate_refusals)
        except _Superseded:
            _log.info("asr_item_superseded")
            # Its result stands: this answer is a repeat write, which changes nothing.
            return draft_without_asr(draft) or failed_extract(
                ErrorCode.INTERNAL_ERROR, _FAILED_MESSAGE, retryable=True
            )
        backend = self._backend
        if isinstance(outcome, _Failure):
            label = outcome.code.value
            answer: Transcription | Envelope[ExtractResult] = outcome.envelope()
            upstream = outcome.upstream_status
        else:
            label = "success" if outcome.has_speech else ErrorCode.NO_TRANSCRIPT_AVAILABLE.value
            answer, upstream = outcome, None
            self._metrics.audio_seconds.labels(backend=backend).inc(max(outcome.duration, 0.0))
        self._metrics.transcriptions.labels(backend=backend, outcome=label).inc()
        _log.info(
            "asr_item_done",
            source=meta.source,
            platform=meta.platform,
            backend=backend,
            outcome=label,
        )
        return await self._pipeline.transcribed_result(
            request,
            job_principal(job),
            meta,
            draft,
            answer,
            upstream_status=upstream,
            final=isinstance(outcome, _Failure) and outcome.final,
        )

    async def _transcription(
        self,
        job: JobRecord,
        item: QueueItem,
        meta: MediaInputMeta,
        body: bytes,
        gate_refusals: Literal["fail", "raise"],
    ) -> Transcription | _Failure:
        settings = self._settings
        if meta.duration is not None and meta.duration > settings.media_max_duration_seconds:
            return self._too_long(meta.duration)
        key: str | None = None
        if self._backend == "openai":
            # The caller's key (X-ASR-Api-Key) wins over the service's own; without either
            # nothing is downloaded.
            key = await self._store.asr_key(job.id) or settings.asr_external_api_key
            if key is None:
                return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _NO_KEY_MESSAGE, False)
        # ASR_TIMEOUT_SECONDS is the budget of the whole item: the wait for the turn, the
        # download, the decoding and the recognition. The lease holds twice that.
        deadline = asyncio.get_running_loop().time() + settings.asr_timeout_seconds

        async def superseded() -> None:
            if meta.source != "upload" and await self._store.has_result(job.id, item.index):
                raise _Superseded

        scratch = _new_scratch(settings)
        try:
            media = await self._media(meta, body, scratch, deadline, superseded, gate_refusals)
            if isinstance(media, _Failure):
                return media
            limit = settings.media_max_duration_seconds
            if media.duration is not None and media.duration > limit:
                return self._too_long(media.duration)
            await superseded()  # before the long transcription
            return await self._transcribe(meta, media, deadline, key)
        finally:
            # Synchronous, so that a cancelled item removes it too.
            shutil.rmtree(scratch, ignore_errors=True)

    async def _media(
        self,
        meta: MediaInputMeta,
        body: bytes,
        scratch: Path,
        deadline: float,
        superseded: Callable[[], Awaitable[None]],
        gate_refusals: Literal["fail", "raise"],
    ) -> _Media | _Failure:
        if meta.source == "upload":
            if meta.kind is None:
                return _Failure(
                    ErrorCode.UNSUPPORTED_CONTENT_TYPE, _NOT_MEDIA_MESSAGE, False, final=True
                )
            path = scratch / f"upload.{meta.kind}"
            await asyncio.to_thread(path.write_bytes, body)
            return _Media(path, meta.kind, None)
        if meta.url is None:
            return _Failure(ErrorCode.INTERNAL_ERROR, _DOWNLOAD_FAILED_MESSAGE, True)
        try:
            return await self._download(meta, meta.url, scratch, deadline, superseded)
        except (OutboundBusy, CircuitOpen) as refusal:
            if gate_refusals == "raise":
                raise
            message = _BUSY_MESSAGE if isinstance(refusal, OutboundBusy) else _PAUSED_MESSAGE
            return _Failure(ErrorCode.RATE_LIMITED, message, True)

    async def _download(
        self,
        meta: MediaInputMeta,
        url: str,
        scratch: Path,
        item_deadline: float,
        superseded: Callable[[], Awaitable[None]],
    ) -> _Media | _Failure:
        """The media of a URL, downloaded by a child into ``scratch``: a platform video's info
        in its host's turn, then the download outside it; a file in its host's turn. The
        download gets ``MEDIA_DOWNLOAD_TIMEOUT_SECONDS`` from its turn, or what is left of the
        item's budget. An item that got its result while it waited for the turn stops."""
        loop = asyncio.get_running_loop()
        settings = self._settings
        host = urlsplit(url).hostname or ""
        crawl_delay = meta.compliance.crawl_delay if meta.compliance is not None else None
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "source": meta.source,
            "url": url,
            "extractor": meta.extractor,
            "max_bytes": settings.media_max_bytes,
            "user_agent": USER_AGENT,
            "scratch": str(scratch),
            "info": None,
        }
        media_cap = settings.media_max_bytes + _DOWNLOAD_SLACK_BYTES
        if meta.source == "platform":
            async with self._gate.slot(host, crawl_delay):
                await superseded()
                deadline = min(item_deadline, loop.time() + settings.media_download_timeout_seconds)
                info = await self._call(
                    "platform_info",
                    {k: payload[k] for k in ("url", "extractor", "user_agent")},
                    min(deadline, loop.time() + _INFO_SECONDS),
                    item_deadline,
                    max_bytes=settings.media_extract_max_bytes,
                    host=host,
                )
            if isinstance(info, _Failure):
                return info
            duration = _number(info.get("duration"))
            if duration is not None and duration > settings.media_max_duration_seconds:
                return self._too_long(duration)
            payload["info"] = info.get("info")
            reply = await self._call(
                "download", payload, deadline, item_deadline, max_bytes=media_cap
            )
        else:
            async with self._gate.slot(host, crawl_delay):
                await superseded()
                deadline = min(item_deadline, loop.time() + settings.media_download_timeout_seconds)
                reply = await self._call(
                    "download", payload, deadline, item_deadline, max_bytes=media_cap, host=host
                )
        fetch_ms = round((time.perf_counter() - started) * 1000)
        if isinstance(reply, _Failure):
            return reply
        return self._downloaded(reply, scratch, fetch_ms)

    def _downloaded(
        self, reply: Mapping[str, Any], scratch: Path, fetch_ms: int
    ) -> _Media | _Failure:
        """The child's answer of a download: a file in the item's scratch directory."""
        raw_path = reply.get("path")
        kind = reply.get("kind")
        path = Path(raw_path) if isinstance(raw_path, str) else None
        if (
            path is None
            or not path.resolve().is_relative_to(scratch.resolve())
            or not path.is_file()
        ):
            _log.warning("asr_download_without_file")
            return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _DOWNLOAD_FAILED_MESSAGE, True)
        if kind not in get_args(MediaKind):
            return _Failure(
                ErrorCode.UNSUPPORTED_CONTENT_TYPE, _NOT_MEDIA_MESSAGE, False, final=True
            )
        status = reply.get("status")
        return _Media(
            path,
            kind,
            _number(reply.get("duration")),
            fetch_ms,
            status if isinstance(status, int) and not isinstance(status, bool) else None,
        )

    async def _call(
        self,
        function: str,
        payload: dict[str, Any],
        deadline: float,
        item_deadline: float,
        *,
        max_bytes: int,
        host: str | None = None,
    ) -> dict[str, Any] | _Failure:
        """A download call of a child with a proxy context of its own, capped at ``max_bytes``;
        a failure as the item's error. A timeout is the download's (retryable), or the item's
        when its budget was what ended the call. ``host`` (a turn held at it) gets the
        answer's status at the gate."""
        context = self._egress.open_context(max_bytes=max_bytes)
        try:
            loop = asyncio.get_running_loop()
            deadline = max(deadline, loop.time() + _MIN_CALL_SECONDS)
            call = {
                **payload,
                "proxy": _proxy_url(self._egress.url, context),
                "timeout": max(deadline - loop.time(), _MIN_CALL_SECONDS),
            }
            try:
                reply = await self._children.run(function, call, deadline)
            except ConversionTimeout:
                if deadline >= item_deadline:
                    return _Failure(ErrorCode.TIMEOUT, _ITEM_TIMEOUT_MESSAGE, False, final=True)
                return _Failure(ErrorCode.TIMEOUT, _DOWNLOAD_TIMEOUT_MESSAGE, True)
            except ConversionError as error:
                failure = self._download_failure(error, context)
                if host is not None:
                    await self._record(host, error, failure)
                return failure
            except ConversionCrashed:
                _log.warning("asr_child_crashed", function=function)
                return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _CRASHED_MESSAGE, True)
            except ConversionOutOfMemory:
                _log.warning("asr_child_out_of_memory", function=function)
                return _Failure(
                    ErrorCode.TRANSCRIPTION_FAILED, _OUT_OF_MEMORY_MESSAGE, False, final=True
                )
            if context.capped:
                return self._too_large()
            if host is not None and function == "download":
                await self._gate.record(host, status=200, retry_after=None)
            return reply if isinstance(reply, dict) else {}
        finally:
            context.close()

    def _download_failure(self, error: ConversionError, context: ProxyContext) -> _Failure:
        name = error.type_name
        # The type only: the message may quote the URL or the video id.
        _log.info("asr_download_failed", child_error=name)
        # A refusal also wins over an ``HttpStatus``: to a plain-HTTP request the proxy refuses
        # with its own 403 or 502, which the child reports as the URL's status.
        if name not in _KEEP_OVER_REFUSAL:
            if context.capped:
                return self._too_large()
            if context.refused:
                refused = _refusal(context.refused[0][1])
                if refused is not None:
                    return refused
        if name == "TooLarge":
            return self._too_large()
        if name in ("NotMedia", "IsLive"):
            message = _LIVE_MESSAGE if name == "IsLive" else _NOT_MEDIA_MESSAGE
            return _Failure(ErrorCode.UNSUPPORTED_CONTENT_TYPE, message, False, final=True)
        if name == "MediaTooLong":
            return self._too_long(_seconds_of(error.message))
        if name == "HttpStatus" and error.message.strip().isdecimal():
            status = int(error.message.strip())
            retryable = status in _RETRYABLE_STATUSES or status >= 500
            message = f"The media's site answered with HTTP status {status}."
            if retryable:
                message += " Try again later."
            return _Failure(
                ErrorCode.TARGET_HTTP_ERROR,
                message,
                retryable,
                upstream_status=status,
                final=not retryable,
            )
        return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _DOWNLOAD_FAILED_MESSAGE, True)

    async def _record(self, host: str, error: ConversionError, failure: _Failure) -> None:
        """A failed download's answer at the gate: its status, a host failure, or a 429 that
        yt-dlp reported of the platform."""
        if failure.upstream_status is not None:
            await self._gate.record(host, status=failure.upstream_status, retry_after=None)
        elif failure.code in (ErrorCode.DNS_ERROR, ErrorCode.CONNECTION_ERROR):
            await self._gate.record(host, status=None, retry_after=None)
        elif error.type_name == "DownloadError" and "HTTP Error 429" in error.message:
            await self._gate.record(host, status=429, retry_after=None)

    async def _transcribe(
        self, meta: MediaInputMeta, media: _Media, deadline: float, key: str | None
    ) -> Transcription | _Failure:
        """The transcription by the item's ``deadline`` (what the download left of
        ``ASR_TIMEOUT_SECONDS``): local, or on the external endpoint with ``key``."""
        settings = self._settings
        left = deadline - asyncio.get_running_loop().time()
        if left < _MIN_CALL_SECONDS:
            return _Failure(ErrorCode.TIMEOUT, _ITEM_TIMEOUT_MESSAGE, False, final=True)
        payload: dict[str, Any] = {
            "backend": self._backend,
            "path": str(media.path),
            "language": _primary_language(meta.languages),
            "max_duration": settings.media_max_duration_seconds,
        }
        if self._backend == "openai":
            # The key travels in the call only: never in the children's environment or a log.
            payload |= {
                "base_url": settings.asr_external_base_url,
                "model": settings.asr_external_model,
                "api_key": key,
                "timeout": settings.asr_external_timeout_seconds,
                "budget": left,
                "user_agent": USER_AGENT,
            }
            model = settings.asr_external_model
        else:
            payload |= {
                "model_path": str(settings.asr_models_path / settings.asr_model),
                "compute_type": settings.asr_compute_type,
                "threads": settings.asr_threads,
            }
            model = settings.asr_model
        started = time.perf_counter()
        try:
            reply = await self._children.run("transcribe", payload, deadline)
        except ConversionTimeout:
            # Not retryable: the same media would take as long again.
            return _Failure(ErrorCode.TIMEOUT, _ITEM_TIMEOUT_MESSAGE, False, final=True)
        except ConversionCrashed:
            _log.warning("asr_child_crashed", function="transcribe")
            return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _CRASHED_MESSAGE, True)
        except ConversionOutOfMemory:
            _log.warning("asr_child_out_of_memory", function="transcribe")
            return _Failure(
                ErrorCode.TRANSCRIPTION_FAILED, _OUT_OF_MEMORY_MESSAGE, False, final=True
            )
        except ConversionError as error:
            return self._transcription_failure(error)
        finally:
            seconds = time.perf_counter() - started
            self._metrics.seconds.labels(backend=self._backend).observe(seconds)
        try:
            transcription = _transcription(reply, model)
        except (KeyError, TypeError, ValueError):
            _log.warning("asr_reply_unreadable")
            return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _FAILED_MESSAGE, False)
        return Transcription(
            segments=transcription.segments,
            language=transcription.language,
            duration=transcription.duration,
            model=transcription.model,
            kind=media.kind,
            fetch_ms=media.fetch_ms,
            convert_ms=round(seconds * 1000),
            upstream_status=media.status,
        )

    def _transcription_failure(self, error: ConversionError) -> _Failure:
        name = error.type_name
        # The type only: the message may quote a path.
        _log.info("asr_transcription_failed", child_error=name)
        if name == "ModelsMissing":
            _log.error(
                "asr_models_missing",
                models_path=str(self._settings.asr_models_path),
                model=self._settings.asr_model,
            )
            return _Failure(ErrorCode.INTERNAL_ERROR, _MODELS_MISSING_MESSAGE, True)
        if name == "MediaTooLong":
            return self._too_long(_seconds_of(error.message))
        if name == "DecodeFailed":
            return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _DECODE_MESSAGE, False, final=True)
        if name == "BudgetSpent":
            return _Failure(ErrorCode.TIMEOUT, _ITEM_TIMEOUT_MESSAGE, False, final=True)
        detail = _external_detail(error.message)
        if name == "ExternalRejected":
            message = _REJECTED_MESSAGE.format(detail=detail)
            return _Failure(ErrorCode.TRANSCRIPTION_FAILED, message, False)
        if name == "ExternalUnavailable":
            message = _UNAVAILABLE_MESSAGE.format(detail=detail)
            return _Failure(ErrorCode.TRANSCRIPTION_FAILED, message, True)
        if name == "ExternalFailed":
            message = _EXTERNAL_FAILED_MESSAGE.format(detail=detail)
            return _Failure(ErrorCode.TRANSCRIPTION_FAILED, message, False)
        return _Failure(ErrorCode.TRANSCRIPTION_FAILED, _FAILED_MESSAGE, False)

    def _too_long(self, duration: float | None) -> _Failure:
        message = media_too_long_message(duration, self._settings.media_max_duration_seconds)
        return _Failure(ErrorCode.MEDIA_TOO_LONG, message, False, final=True)

    def _too_large(self) -> _Failure:
        limit = self._settings.media_max_bytes
        return _Failure(
            ErrorCode.TOO_LARGE,
            f"The media is larger than the {limit} bytes that are downloaded (MEDIA_MAX_BYTES).",
            False,
            final=True,
        )


def _refusal(reason: str) -> _Failure | None:
    """A request the proxy refused, as the item's error; None for a reason that says nothing
    about the target (a malformed request, the proxy's own authentication)."""
    if reason == "blocked_address":
        return _Failure(ErrorCode.BLOCKED_URL, _BLOCKED_ADDRESS_MESSAGE, False, final=True)
    if reason == "bytes_cap":
        return _Failure(
            ErrorCode.TOO_LARGE,
            "The download needed more bytes than it may use.",
            False,
            final=True,
        )
    if reason == "dns_error":
        return _Failure(ErrorCode.DNS_ERROR, _DNS_MESSAGE, True)
    if reason == "connect_error":
        return _Failure(ErrorCode.CONNECTION_ERROR, _CONNECT_MESSAGE, True)
    return None


def _transcription(reply: Any, model: str) -> Transcription:
    """The child's ``transcribe`` reply; raises ``KeyError``, ``TypeError`` or ``ValueError``
    when it is not the reply's shape."""
    segments = [Cue(float(start), float(end), str(text)) for start, end, text in reply["segments"]]
    language = reply.get("language")
    return Transcription(
        segments=segments,
        language=language if isinstance(language, str) else "",
        duration=float(reply.get("duration") or 0.0),
        model=model,
    )


def _external_detail(message: str) -> str:
    """What an external failure's message adds to the answer: its HTTP status or one of
    ``_EXTERNAL_DETAILS``; anything else (never expected) adds nothing, so no text of the child
    reaches the answer."""
    text = message.strip()
    if text.isdecimal() and len(text) == 3:
        return f" (HTTP {text})"
    return _EXTERNAL_DETAILS.get(text, "")


def _seconds_of(message: str) -> float | None:
    """The seconds a child's ``MediaTooLong`` carries as its message, None without."""
    try:
        seconds = float(message.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _primary_language(languages: Sequence[str]) -> str | None:
    """The first requested language's primary subtag (``de`` of ``de-AT``), None without."""
    if not languages:
        return None
    return languages[0].split("-")[0].lower() or None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _proxy_url(url: str, context: ProxyContext) -> str:
    scheme, _, address = url.partition("://")
    return f"{scheme}://{context.username}:{context.password}@{address}"


# --- the scratch directory -----------------------------------------------------------------------


def _new_scratch(settings: Settings) -> Path:
    """A directory of its own for one item under ``ASR_SCRATCH_PATH`` (mode 0700)."""
    root = settings.asr_scratch_path
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=root))


def clean_scratch(settings: Settings) -> None:
    """Remove the items' directories a killed process left under ``ASR_SCRATCH_PATH``; other
    entries stay."""
    root = settings.asr_scratch_path
    if not root.is_dir():
        return
    removed = 0
    for entry in root.iterdir():
        if not entry.name.startswith(_SCRATCH_PREFIX):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed += 1
    if removed:
        _log.info("asr_scratch_cleaned", entries=removed)


# --- start-up: the models, FFmpeg's licence and the self-check -----------------------------------


class SelfCheckFailed(Exception):  # noqa: N818 - a failed check, named as such
    """FFmpeg is not an LGPL build, or the built-in clip did not transcribe to its words."""


class AsrStartFailed(Exception):  # noqa: N818 - a failed start, named as such
    """The ASR children did not start (faster-whisper or PyAV do not load)."""


def ffmpeg_gpl_findings(
    libraries: Mapping[str, Mapping[str, str]], codecs: Collection[str] = ()
) -> list[str]:
    """What makes PyAV's FFmpeg other than an LGPL build (decision D5), one finding per
    problem; empty for an LGPL build. ``libraries`` is PyAV's ``av._core.library_meta``: per
    library its ``configuration`` (the configure switches) and ``license``; every library it
    reports is checked, and one of PyAV's four that is missing is a finding too. ``codecs``
    is ``av.codecs_available``: a GPL or non-free codec in it is a finding (``codec <name>``)
    whatever the configuration says."""
    findings: list[str] = []
    for name in _FFMPEG_LIBRARIES:
        if name not in libraries:
            findings.append(f"{name}: not reported")
    for name in sorted(libraries):
        meta = libraries[name]
        configuration = str(meta.get("configuration") or "").replace("_", "-")
        switches = set(configuration.split())
        findings.extend(f"{name}: {switch}" for switch in sorted(switches & _FFMPEG_GPL_SWITCHES))
        license = str(meta.get("license") or "").strip()
        if not license.startswith("LGPL"):
            findings.append(f"{name}: license {license or 'unknown'}")
    found = {str(codec).lower() for codec in codecs}
    findings.extend(f"codec {codec}" for codec in sorted(found & _FFMPEG_GPL_CODECS))
    return findings


def _check_clip() -> Path:
    return Path(str(resources.files("text_extraction.media").joinpath(_CHECK_CLIP)))


async def self_check(pool: AsrChildren, settings: Settings, *, children: int) -> None:
    """In each of ``children`` children (they run one call at a time, so as many calls at once
    reach each child once): FFmpeg's licence and the programs on ``PATH``, then the built-in
    clip: transcribed by the local backend (which loads the model), or decoded and encoded as
    Opus for the external backend (nothing is sent; no model is needed). Raises
    ``SelfCheckFailed`` when a call fails, FFmpeg is not an LGPL build, the transcript lacks the
    expected words or the encoding gave nothing."""
    payload = {
        "backend": "local",
        "path": str(_check_clip()),
        "model_path": str(settings.asr_models_path / settings.asr_model),
        "compute_type": settings.asr_compute_type,
        "threads": settings.asr_threads,
        "language": _CHECK_LANGUAGE,
        "max_duration": settings.media_max_duration_seconds,
    }
    deadline = asyncio.get_running_loop().time() + settings.asr_timeout_seconds

    async def check_one() -> None:
        libraries = await pool.run("ffmpeg", {}, deadline)
        findings = ffmpeg_gpl_findings(
            libraries.get("libraries") or {}, libraries.get("codecs") or ()
        )
        tools = list(libraries.get("tools") or ())
        if tools:
            _log.error(
                "asr_self_check_failed",
                reason="tools_on_path",
                tools=tools,
                models_path=str(settings.asr_models_path),
            )
            raise SelfCheckFailed("ffmpeg or rtmpdump is on the children's PATH")
        if findings:
            _log.error(
                "asr_self_check_failed",
                reason="ffmpeg_not_lgpl",
                findings=findings,
                pyav=libraries.get("version"),
                models_path=str(settings.asr_models_path),
            )
            raise SelfCheckFailed("PyAV's FFmpeg is not an LGPL build")
        if settings.asr_backend == "openai":
            await check_encoding()
            return
        reply = await pool.run("transcribe", payload, deadline)
        text = " ".join(str(segment[2]) for segment in reply["segments"]).casefold()
        if not all(word in text for word in _CHECK_WORDS):
            _log.error(
                "asr_self_check_failed",
                reason="text_not_found",
                models_path=str(settings.asr_models_path),
            )
            raise SelfCheckFailed("the built-in clip's words were not found")

    async def check_encoding() -> None:
        call = {"path": payload["path"], "max_duration": payload["max_duration"]}
        reply = await pool.run("opus_check", call, deadline)
        size = reply.get("bytes") if isinstance(reply, dict) else None
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            _log.error(
                "asr_self_check_failed",
                reason="opus_encoding_failed",
                models_path=str(settings.asr_models_path),
            )
            raise SelfCheckFailed("the built-in clip did not encode as Opus")

    outcomes = await asyncio.gather(*(check_one() for _ in range(children)), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, SelfCheckFailed):
            raise outcome
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            # The built-in clip and our own model: the child's message quotes no caller content
            # (only our paths), and without it a failed start says no more than the type.
            conversion = outcome if isinstance(outcome, ConversionError) else None
            _log.error(
                "asr_self_check_failed",
                error=type(outcome).__name__,
                child_error=conversion.type_name if conversion else None,
                child_message=conversion.message if conversion else None,
                models_path=str(settings.asr_models_path),
            )
            raise SelfCheckFailed("the built-in clip failed") from None
    _log.info("asr_self_check_passed", children=children)


async def start_children(pool: StartableChildren, settings: Settings, *, children: int) -> None:
    """Start ``pool``'s children, then the self-check in each. Children that do not start are
    logged with the models' path (never more than the error's type) and raise
    ``AsrStartFailed``; a failed check raises ``SelfCheckFailed``."""
    try:
        await pool.start()
    except Exception as error:
        _log.error(
            "asr_start_failed",
            models_path=str(settings.asr_models_path),
            error_type=type(error).__name__,
        )
        raise AsrStartFailed("the ASR children did not start") from None
    await self_check(pool, settings, children=children)


def _manifest(settings: Settings) -> model_store.Manifest:
    return model_store.load_manifest("asr").only(settings.asr_model)


def models_missing(settings: Settings) -> bool:
    """Whether a file of ``ASR_MODEL`` is missing on the volume; logs the volume's path and the
    model when one is."""
    root = settings.asr_models_path
    missing = model_store.missing_models(root, _manifest(settings))
    if missing:
        _log.error(
            "asr_models_missing",
            models_path=str(root),
            model=settings.asr_model,
            missing=len(missing),
        )
    return bool(missing)


# --- the process ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AsrWorkerParts:
    """What one ASR worker process runs on: the worker, its memory guard, its children and
    their proxy, and the fetcher and conversion pool of its pipeline to close at the end."""

    worker: Worker
    guard: MemoryGuard
    asr_pool: DocumentPool
    proxy: EgressProxy
    fetcher: Fetcher
    conversion_pool: ConversionPool
    settings: Settings
    post_pool: ConversionPool  # the post stage's (POST_CONCURRENCY, ruling R21)

    async def start(self) -> None:
        """Clean the scratch directory, start the proxy and the children, then the self-check
        in each (before ``/ready``)."""
        clean_scratch(self.settings)
        await self.proxy.start()
        await start_children(self.asr_pool, self.settings, children=self.settings.asr_concurrency)


def build_asr_worker(
    settings: Settings, valkey: Redis, registry: CollectorRegistry
) -> AsrWorkerParts:
    """The ASR worker of a process, nothing started yet. Its result cache skips writes while
    the memory guard reports Valkey as nearly full, as in the extract worker."""
    guard = MemoryGuard(valkey, settings.valkey_memory_guard_ratio, registry)
    gate, fetcher = create_outbound(settings, valkey)
    conversion_pool = ConversionPool(settings.convert_concurrency)
    post_pool = ConversionPool(settings.post_concurrency, name="post")
    pipeline = create_pipeline(
        settings, valkey, fetcher, registry, conversion_pool, gate=gate, memory_guard=guard
    )
    asr_pool = create_asr_pool(settings)
    proxy = create_asr_proxy(settings, registry)
    store = JobStore(valkey, settings)
    worker = Worker(
        settings,
        valkey,
        AsrRunner(pipeline, store, settings, asr_pool, proxy, gate, registry),
        store,
        asr_queue(valkey, settings),
        registry,
        concurrency=settings.asr_concurrency,
        give_back_on_cancel=True,  # the lease lasts hours: a cancelled item must not wait for it
        pii_move=create_pii_move(settings, valkey, store, guard, registry),
        llm_move=create_llm_move(settings, valkey, store, guard, registry),
        post_pool=post_pool,
    )
    return AsrWorkerParts(
        worker=worker,
        guard=guard,
        asr_pool=asr_pool,
        proxy=proxy,
        fetcher=fetcher,
        conversion_pool=conversion_pool,
        settings=settings,
        post_pool=post_pool,
    )


async def serve(parts: AsrWorkerParts, internal: _Server, readiness: Readiness) -> None:
    """Start the children and check them, run the worker until a signal, then let the items in
    flight finish for ``GRACEFUL_SECONDS``. The caller kills the children and closes the rest."""
    await _serve(
        parts.worker,
        internal,
        readiness,
        graceful_seconds=GRACEFUL_SECONDS,
        guard=parts.guard,
        startup=parts.start,
    )


async def _run(settings: Settings) -> None:
    valkey = create_valkey(settings.valkey_url)
    registry = REGISTRY
    parts = build_asr_worker(settings, valkey, registry)
    readiness = Readiness()
    internal = _server(
        create_internal_app(settings, valkey, registry, readiness=readiness),
        settings.internal_host,
        settings.internal_port,
        GRACEFUL_SECONDS,
    )
    try:
        await serve(parts, internal, readiness)
    finally:
        await parts.asr_pool.shutdown()  # after the loops: their items are given back
        await parts.proxy.stop()
        clean_scratch(settings)
        await parts.fetcher.aclose()
        parts.conversion_pool.shutdown()
        parts.post_pool.shutdown()
        await valkey.aclose()
        _log.info("valkey_closed")


async def _check(settings: Settings) -> int:
    """FFmpeg's licence and the built-in clip in one child: 0 when both pass, else 1. The
    scratch directory is left alone: ``--check`` may run in a pod whose worker transcribes."""
    pool = create_asr_pool(settings.model_copy(update={"asr_concurrency": 1}))
    try:
        await start_children(pool, settings, children=1)
    except (SelfCheckFailed, AsrStartFailed):
        return 1  # logged, with the models' path
    finally:
        await pool.shutdown()
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="text-extraction-asr-worker",
        description="Run the ASR worker (pool asr) of text-extraction.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--download-models",
        action="store_true",
        help="Download the files of ASR_MODEL into ASR_MODELS_PATH, then exit (0: in place).",
    )
    actions.add_argument(
        "--check",
        action="store_true",
        help="Check FFmpeg's licence and transcribe the built-in clip once, then exit "
        "(0: both passed).",
    )
    args = parser.parse_args(argv)
    settings = configured_settings()
    external = settings.asr_backend == "openai"
    if external and settings.asr_external_base_url is None:
        _log.error(
            "invalid_configuration",
            message="Invalid configuration: ASR_BACKEND=openai needs ASR_EXTERNAL_BASE_URL",
        )
        sys.exit(EXIT_CONFIG_ERROR)
    if args.download_models:
        if external:
            _log.info("asr_models_not_needed", backend=settings.asr_backend)
            sys.exit(0)
        sys.exit(_download(settings))
    if not external and models_missing(settings):
        sys.exit(1)
    if args.check:
        sys.exit(asyncio.run(_check(settings)))
    try:
        asyncio.run(_run(settings))
    except (SelfCheckFailed, AsrStartFailed):
        sys.exit(1)  # logged, with the models' path


def _download(settings: Settings) -> int:
    try:
        model_store.download_models(settings.asr_models_path, _manifest(settings))
    except (model_store.ModelDownloadFailed, OSError) as error:
        _log.error(
            "asr_models_download_failed",
            models_path=str(settings.asr_models_path),
            model=settings.asr_model,
            reason=str(error) if isinstance(error, model_store.ModelDownloadFailed) else None,
            error_type=type(error).__name__,
        )
        return 1
    return 0
