"""The real ASR children (``media/asr_child.py``): PyAV's decoding and probe, the download of a
media file through the egress proxy, faster-whisper with the model on ``ASR_MODELS_PATH``
(filled by ``text-extraction-asr-worker --download-models``), the windows of long media (with a
fake ``WhisperModel`` in this process, and with the real model), and a whole item on the real
proxy and children (a private address refused). Marker ``asr``: ``uv sync --extra
asr``, then ``uv run pytest -m asr``. Without the extra ``asr`` the module is skipped; the clip's
transcription is skipped while the model is not on the volume.
"""

import asyncio
import importlib.util
import json
import os
import re
import secrets
import threading
import wave
from collections.abc import Coroutine, Iterable, Iterator
from dataclasses import dataclass
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, ClassVar

import pytest
from prometheus_client import CollectorRegistry

from tests.fakes import fetched
from tests.test_worker import Harness, error_of, submit_transcription
from text_extraction.asr_worker import (
    AsrRunner,
    create_asr_pool,
    create_asr_proxy,
    ffmpeg_gpl_findings,
)
from text_extraction.documents.pool import ConversionError, DocumentPool
from text_extraction.envelope import ErrorCode
from text_extraction.outbound import DomainGate
from text_extraction.settings import Settings
from text_extraction.worker import Worker

pytestmark = [
    pytest.mark.asr,
    pytest.mark.skipif(
        importlib.util.find_spec("faster_whisper") is None
        or importlib.util.find_spec("av") is None,
        reason="the extra asr is not installed (uv sync --extra asr)",
    ),
]

CORPUS = Path(__file__).parents[1] / "media" / "corpus"
CLIP = CORPUS / "speech_de.ogg"
_WORD = re.compile(r"\w+")


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "asr_concurrency": 1,
        "asr_models_path": Path(os.environ.get("ASR_MODELS_PATH", "/models")),
        "asr_scratch_path": tmp_path / "scratch",
    }
    values.update(overrides)
    return Settings(**values)


def run[T](work: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(work)


def words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def recall(expected: str, content: str) -> float:
    wanted = words(expected)
    found = set(words(content))
    return sum(1 for word in wanted if word in found) / len(wanted)


async def in_child(settings: Settings, function: str, payload: dict[str, Any]) -> Any:
    """``function`` in a real ASR child (its memory limit and environment included)."""
    pool: DocumentPool = create_asr_pool(settings)
    await pool.start()
    try:
        deadline = asyncio.get_running_loop().time() + settings.asr_timeout_seconds
        return await pool.run(function, payload, deadline)
    finally:
        await pool.shutdown()


async def in_child_with_memory(
    settings: Settings, function: str, payload: dict[str, Any]
) -> tuple[Any, dict[str, int]]:
    """``in_child``, plus the child's peak memory afterwards (``child_memory``)."""
    pool: DocumentPool = create_asr_pool(settings.model_copy(update={"asr_concurrency": 1}))
    await pool.start()
    try:
        deadline = asyncio.get_running_loop().time() + settings.asr_timeout_seconds
        reply = await pool.run(function, payload, deadline)
        return reply, child_memory(pool)
    finally:
        await pool.shutdown()


def child_memory(pool: DocumentPool) -> dict[str, int]:
    """``VmPeak`` (address space, which ``ASR_MEMORY_LIMIT_MB`` caps) and ``VmHWM`` (resident
    memory) of the pool's one child over its life so far, in KiB (Linux ``/proc``)."""
    (child,) = pool._children
    status = Path(f"/proc/{child.process.pid}/status").read_text(encoding="ascii")
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    return {name: int(fields[name].split()[0]) for name in ("VmPeak", "VmHWM")}


def report_memory(label: str, memory: dict[str, int], settings: Settings) -> None:
    """One line with the child's peak memory against ``ASR_MEMORY_LIMIT_MB``, on stdout (run
    pytest with ``-s``) and in the GitHub step summary when there is one."""
    limit = settings.asr_memory_limit_mb
    line = (
        f"{label} ({settings.asr_model}): child VmPeak {memory['VmPeak'] // 1024} MiB, "
        f"VmHWM {memory['VmHWM'] // 1024} MiB, ASR_MEMORY_LIMIT_MB {limit}"
    )
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(line + "\n")


def transcribe_payload(settings: Settings, path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "model_path": str(settings.asr_models_path / settings.asr_model),
        "compute_type": settings.asr_compute_type,
        "threads": settings.asr_threads,
        "language": "de",
        "max_duration": settings.media_max_duration_seconds,
    }


def test_transcribes_german_clip(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    if not (settings.asr_models_path / settings.asr_model / "model.bin").is_file():
        pytest.skip(f"no {settings.asr_model} model under {settings.asr_models_path}")

    reply = run(in_child(settings, "transcribe", transcribe_payload(settings, CLIP)))

    text = " ".join(segment[2] for segment in reply["segments"])
    expected = (CORPUS / "speech_de.expected.txt").read_text(encoding="utf-8")
    assert recall(expected, text) >= 0.8
    assert reply["language"] == "de"
    assert 29.0 <= reply["duration"] <= 31.0
    assert all(start <= end for start, end, _text in reply["segments"])


def test_random_bytes_fail_cleanly(tmp_path: Path) -> None:
    """Bytes that are no media fail as ``DecodeFailed`` (``transcription_failed``) before the
    model is loaded."""
    settings = settings_for(tmp_path, asr_models_path=tmp_path / "models")
    model = settings.asr_models_path / settings.asr_model
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"")  # never loaded
    path = tmp_path / "upload.ogg"
    path.write_bytes(b"OggS" + secrets.token_bytes(64 * 1024))

    with pytest.raises(ConversionError) as failure:
        run(in_child(settings, "transcribe", transcribe_payload(settings, path)))

    assert failure.value.type_name == "DecodeFailed"


def test_missing_model_is_named(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, asr_models_path=tmp_path / "empty")

    with pytest.raises(ConversionError) as failure:
        run(in_child(settings, "transcribe", transcribe_payload(settings, CLIP)))

    assert failure.value.type_name == "ModelsMissing"


def test_media_over_the_limit_stops_the_decoding(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, asr_models_path=tmp_path / "models")
    model = settings.asr_models_path / settings.asr_model
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"")
    payload = {**transcribe_payload(settings, CLIP), "max_duration": 10}

    with pytest.raises(ConversionError) as failure:
        run(in_child(settings, "transcribe", payload))

    assert failure.value.type_name == "MediaTooLong"


def test_duration_probe() -> None:
    from text_extraction.media import asr_child

    assert asr_child.probe_duration(CLIP) == pytest.approx(30.0, abs=0.1)
    audio = asr_child.decode_audio(CLIP, max_seconds=60)
    assert audio.dtype.name == "float32"
    assert audio.ndim == 1
    assert len(audio) == pytest.approx(30.0 * 16_000, rel=0.01)


def test_ffmpeg_reports_each_library(tmp_path: Path) -> None:
    """The licence check reads what the child reports. The PyPI wheel of av fails it (libx264
    and libx265), the ASR image's LGPL build passes: both are fine here."""
    reply = run(in_child(settings_for(tmp_path), "ffmpeg", {}))

    libraries = reply["libraries"]
    for name in ("libavcodec", "libavformat", "libavutil", "libswresample"):
        assert isinstance(libraries[name]["configuration"], str)
        assert isinstance(libraries[name]["license"], str)
    findings = ffmpeg_gpl_findings(libraries)
    assert all(not finding.endswith("not reported") for finding in findings)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def media_server() -> Iterator[str]:
    handler = partial(_Quiet, directory=str(CORPUS))
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_file_download_through_the_proxy(tmp_path: Path, media_server: str) -> None:
    settings = settings_for(tmp_path, fetch_allow_private_networks=True)
    scratch = tmp_path / "scratch" / "item-test"
    scratch.mkdir(parents=True)

    async def download(max_bytes: int, name: str) -> Any:
        proxy = create_asr_proxy(settings, CollectorRegistry())
        await proxy.start()
        context = proxy.open_context(max_bytes=max_bytes + 1_000_000)
        scheme, _, address = proxy.url.partition("://")
        payload = {
            "source": "file",
            "url": f"{media_server}/{name}",
            "extractor": None,
            "proxy": f"{scheme}://{context.username}:{context.password}@{address}",
            "max_bytes": max_bytes,
            "timeout": 30.0,
            "user_agent": "text-extraction/test",
            "scratch": str(scratch),
            "info": None,
        }
        try:
            return await in_child(settings, "download", payload)
        finally:
            context.close()
            await proxy.stop()

    reply = run(download(10_000_000, "speech_de.ogg"))
    assert (reply["kind"], reply["status"]) == ("ogg", 200)
    assert reply["duration"] == pytest.approx(30.0, abs=0.1)
    assert Path(reply["path"]).parent == scratch
    assert Path(reply["path"]).read_bytes() == CLIP.read_bytes()

    with pytest.raises(ConversionError) as too_large:
        run(download(50_000, "speech_de.ogg"))
    assert too_large.value.type_name == "TooLarge"

    with pytest.raises(ConversionError) as missing:
        run(download(10_000_000, "missing.ogg"))
    assert (missing.value.type_name, missing.value.message) == ("HttpStatus", "404")

    with pytest.raises(ConversionError) as not_media:
        run(download(10_000_000, "speech_de.expected.txt"))
    assert not_media.value.type_name == "NotMedia"


def test_platform_download_from_its_info_through_the_proxy(
    tmp_path: Path, media_server: str
) -> None:
    """``platform_info`` then ``download`` of that info, as for a platform video; yt-dlp's
    generic extractor stands in for a platform's, with the clip as its direct media URL."""
    settings = settings_for(tmp_path, fetch_allow_private_networks=True)
    scratch = tmp_path / "scratch" / "item-test"
    scratch.mkdir(parents=True)

    async def calls() -> tuple[Any, Any]:
        proxy = create_asr_proxy(settings, CollectorRegistry())
        await proxy.start()
        pool: DocumentPool = create_asr_pool(settings)
        await pool.start()
        try:
            replies = []
            payload: dict[str, Any] = {
                "source": "platform",
                "url": f"{media_server}/speech_de.ogg",
                "extractor": "generic",
                "max_bytes": 10_000_000,
                "timeout": 30.0,
                "user_agent": "text-extraction/test",
                "scratch": str(scratch),
                "info": None,
            }
            for function in ("platform_info", "download"):
                context = proxy.open_context(max_bytes=20_000_000)
                scheme, _, address = proxy.url.partition("://")
                payload["proxy"] = f"{scheme}://{context.username}:{context.password}@{address}"
                deadline = asyncio.get_running_loop().time() + 60
                try:
                    replies.append(await pool.run(function, payload, deadline))
                finally:
                    context.close()
                payload["info"] = replies[0]["info"]
            return replies[0], replies[1]
        finally:
            await pool.shutdown()
            await proxy.stop()

    info, downloaded = run(calls())

    assert isinstance(info["info"], str)
    assert downloaded["kind"] == "ogg"
    assert downloaded["duration"] == pytest.approx(30.0, abs=0.1)
    assert Path(downloaded["path"]).read_bytes() == CLIP.read_bytes()
    assert sorted(entry.name for entry in scratch.iterdir()) == ["media.ogg"]


# --- long media in windows ------------------------------------------------------------------

LONG_TILES = 21  # the clip and 2 s of silence, 21 times: 672 s, past one window (600 s)


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The clip 21 times with 2 s of silence after each, as 16 kHz mono 16-bit WAV (672 s)."""
    import numpy as np

    from text_extraction.media import asr_child

    clip = asr_child.decode_audio(CLIP, max_seconds=60)
    tile = np.concatenate([clip, np.zeros(2 * 16_000, dtype=np.float32)])
    audio = np.tile(tile, LONG_TILES)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    path = tmp_path_factory.mktemp("long") / "long.wav"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(16_000)
        file.writeframes(pcm.tobytes())
    return path


@dataclass
class _Segment:
    start: float
    end: float
    text: str


@dataclass
class _Info:
    language: str
    duration: float


class FakeWhisperModel:
    """faster-whisper's ``WhisperModel`` in this process: records what each call got."""

    calls: ClassVar[list[Any]] = []

    def __init__(self, path: str, **kwargs: Any) -> None:
        self.path = path

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterable[_Segment], _Info]:
        self.calls.append(audio)
        seconds = len(audio) / 16_000
        return iter([_Segment(0.0, seconds, "hallo")]), _Info("de", seconds)


def empty_model(settings: Settings) -> None:
    model = settings.asr_models_path / settings.asr_model
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"")  # never loaded


def test_long_media_reaches_the_model_in_bounded_windows(
    tmp_path: Path, long_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """faster-whisper builds the features of a whole call at once (3.4 GiB for an hour): the
    child hands it the decoded array in windows of at most ``WINDOW_SECONDS``, never a path."""
    import numpy as np

    from text_extraction.media import asr_child
    from text_extraction.media.asr_windows import WINDOW_SECONDS

    monkeypatch.setattr(asr_child, "WhisperModel", FakeWhisperModel)
    monkeypatch.setattr(asr_child, "_MODELS", {})
    monkeypatch.setattr(FakeWhisperModel, "calls", [])
    settings = settings_for(tmp_path, asr_models_path=tmp_path / "models")
    empty_model(settings)

    reply = asr_child.transcribe(transcribe_payload(settings, long_clip))

    calls = FakeWhisperModel.calls
    assert len(calls) == 2
    for audio in calls:
        assert isinstance(audio, np.ndarray)  # never a path
        assert (audio.dtype.name, audio.ndim) == ("float32", 1)
        assert len(audio) <= WINDOW_SECONDS * 16_000
    assert sum(len(audio) for audio in calls) == pytest.approx(672 * 16_000, rel=0.001)
    assert reply["duration"] == pytest.approx(672.0, abs=0.5)
    assert reply["segments"][1][0] == pytest.approx(len(calls[0]) / 16_000, abs=0.01)


def test_a_duration_over_the_limit_is_refused_before_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container's duration is read before any audio is decoded."""
    from text_extraction.media import asr_child

    def no_decoding(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("decoded")

    monkeypatch.setattr(asr_child, "decode_audio", no_decoding)
    settings = settings_for(tmp_path, asr_models_path=tmp_path / "models")
    empty_model(settings)
    payload = {**transcribe_payload(settings, CLIP), "max_duration": 10}

    with pytest.raises(asr_child.MediaTooLong) as too_long:
        asr_child.transcribe(payload)

    assert str(too_long.value) == "30"


def test_pyav_opens_local_files_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """``protocol_whitelist=file`` on every open: a playlist in the scratch directory cannot make
    FFmpeg fetch anything (FFmpeg's own default for nested opens also allows ``crypto`` and
    ``data``)."""
    import av

    from text_extraction.media import asr_child

    seen: list[Any] = []
    real_open = av.open

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("options"))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(av, "open", spy)  # the module asr_child calls

    asr_child.probe_duration(CLIP)
    asr_child.decode_audio(CLIP, max_seconds=60)

    assert seen == [{"protocol_whitelist": "file"}] * 2


def test_a_playlist_fetches_nothing(tmp_path: Path) -> None:
    from text_extraction.media import asr_child

    hits: list[str] = []

    class Recording(_Quiet):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_error(404)

    server = HTTPServer(("127.0.0.1", 0), Recording)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        playlist = tmp_path / "media.m3u8"
        playlist.write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n"
            f"http://127.0.0.1:{server.server_address[1]}/segment.ts\n#EXT-X-ENDLIST\n"
        )

        assert asr_child.probe_duration(playlist) is None
        with pytest.raises(asr_child.DecodeFailed):
            asr_child.decode_audio(playlist, max_seconds=60)
    finally:
        server.shutdown()
        server.server_close()
    assert hits == []


def test_the_child_reports_its_codecs_and_the_tools_on_path(tmp_path: Path) -> None:
    reply = run(in_child(settings_for(tmp_path), "ffmpeg", {}))

    assert "libopus" in reply["codecs"] or "opus" in reply["codecs"]
    assert all(isinstance(codec, str) for codec in reply["codecs"])
    assert set(reply["tools"]) <= {"ffmpeg", "rtmpdump"}


def test_the_childs_peak_memory_is_read_from_proc(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    _reply, memory = run(in_child_with_memory(settings, "ffmpeg", {}))

    assert set(memory) == {"VmPeak", "VmHWM"}
    assert 0 < memory["VmHWM"] <= memory["VmPeak"] <= settings.asr_memory_limit_mb * 1024


def test_platform_formats_are_plain_http_and_native_streams() -> None:
    from text_extraction.media import asr_child

    for protocol in ("https?", "m3u8_native", "http_dash_segments"):
        assert protocol in asr_child.FORMAT
    assert asr_child.FORMAT.count("[protocol~=") == 2  # on both alternatives


def test_transcribes_a_long_clip_across_windows(tmp_path: Path, long_clip: Path) -> None:
    settings = settings_for(tmp_path)
    if not (settings.asr_models_path / settings.asr_model / "model.bin").is_file():
        pytest.skip(f"no {settings.asr_model} model under {settings.asr_models_path}")

    reply, memory = run(
        in_child_with_memory(settings, "transcribe", transcribe_payload(settings, long_clip))
    )
    report_memory("long clip, 672 s", memory, settings)

    expected = (CORPUS / "speech_de.expected.txt").read_text(encoding="utf-8")
    # The last tile starts at 640 s, in the second window. Whisper may join its first sentence
    # with the end of the tile before into one segment that starts earlier, so take every
    # segment that reaches into the last tile.
    late = " ".join(text for _start, end, text in reply["segments"] if end > 640.0)
    assert recall(expected, late) >= 0.8
    assert reply["duration"] == pytest.approx(672.0, abs=0.5)
    starts = [start for start, _end, _text in reply["segments"]]
    assert starts == sorted(starts)
    assert reply["language"] == "de"


# --- a whole item on the real proxy and children --------------------------------------------


def test_a_download_from_a_private_address_is_blocked(tmp_path: Path) -> None:
    """Default settings (no private networks): the real egress proxy refuses 127.0.0.1, so the
    item fails as ``blocked_url`` and leaves nothing in its scratch directory."""
    from tests.test_pipeline_media import FakeMediaFetcher, probe

    url = "http://127.0.0.1:9/lecture.ogg"
    ogg = b"OggS\x00\x02" + b"\x00" * 64
    harness = Harness(
        {url: fetched(url, ogg, content_type="audio/ogg")},
        media_fetcher=FakeMediaFetcher(probe(track=None)),
        asr_enabled=True,
        asr_scratch_path=tmp_path / "scratch",
        asr_models_path=tmp_path / "models",
    )
    assert harness.settings.fetch_allow_private_networks is False

    async def scenario() -> Any:
        job_id = await submit_transcription(harness, [url])
        await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
        proxy = create_asr_proxy(harness.settings, CollectorRegistry())
        pool: DocumentPool = create_asr_pool(harness.settings)
        await proxy.start()
        await pool.start()
        try:
            runner = AsrRunner(
                harness.pipeline,
                harness.store,
                harness.settings,
                pool,
                proxy,
                DomainGate(harness.valkey, harness.settings),
                harness.registry,
            )
            worker = Worker(
                harness.settings,
                harness.valkey,
                runner,
                harness.store,
                harness.asr_queue,
                harness.registry,
                clock=harness.clock,
                concurrency=1,
            )
            claim = await harness.asr_queue.claim(runner.lease_seconds)
            assert claim.item is not None
            await worker.process(claim.item)
        finally:
            await pool.shutdown()
            await proxy.stop()
        (item,) = await harness.items(job_id)
        return item

    item = run(scenario())

    assert error_of(item)[:2] == (ErrorCode.BLOCKED_URL, False)
    scratch = harness.settings.asr_scratch_path
    assert not scratch.exists() or list(scratch.iterdir()) == []


# --- the external backend (ASR_BACKEND=openai) with real PyAV -------------------------------

EXTERNAL_KEY = "sk-external-key-in-the-child-4a1b"


def opus_facts(data: bytes) -> tuple[str, str, int, float]:
    """Container, codec, channels and duration of an encoded chunk, read back with PyAV."""
    import io

    import av

    with av.open(io.BytesIO(data), mode="r") as container:
        stream = container.streams.audio[0]
        duration = float((container.duration or 0) / av.time_base)
        return container.format.name, stream.codec_context.name, stream.channels, duration


def test_opus_encoding_is_16k_mono_32kbit_in_ogg() -> None:
    from text_extraction.media import asr_child
    from text_extraction.media.asr_external import encode_ogg_opus

    audio = asr_child.decode_audio(CLIP, max_seconds=60)

    data = encode_ogg_opus(audio)

    container, codec, channels, duration = opus_facts(data)
    assert (container, codec, channels) == ("ogg", "opus", 1)
    assert duration == pytest.approx(len(audio) / 16_000, abs=0.1)
    assert b"OpusHead" in data[:4096]
    # 32 kbit/s (Opus is variable; Ogg pages add a little).
    assert 0.5 * 32_000 / 8 * duration < len(data) < 1.5 * 32_000 / 8 * duration


class _Endpoint:
    """The OpenAI-compatible endpoint on pytest-httpserver: keeps each posted file and the
    authorization it came with; answers one segment per chunk."""

    def __init__(self, server: Any) -> None:
        from werkzeug import Request, Response

        self.posts: list[tuple[str | None, bytes, dict[str, str]]] = []

        def handle(request: Request) -> Response:
            self.posts.append(
                (
                    request.headers.get("Authorization"),
                    request.files["file"].read(),
                    dict(request.form),
                )
            )
            body = {
                "text": "Gregor Samsa.",
                "language": "de",
                "segments": [{"start": 0.5, "end": 2.0, "text": " Gregor Samsa."}],
            }
            return Response(json.dumps(body), content_type="application/json")

        server.expect_request("/v1/audio/transcriptions", method="POST").respond_with_handler(
            handle
        )
        self.base_url = server.url_for("/v1")


def test_long_audio_goes_out_in_chunks_cut_in_pauses(long_clip: Path, httpserver: Any) -> None:
    """The long clip (672 s) in chunks of at most 300 s: each posted file is Ogg Opus of its
    chunk's length, and the segments are on the file's clock."""
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    from text_extraction.media.asr_external import transcribe_external

    endpoint = _Endpoint(httpserver)

    transcription = transcribe_external(
        long_clip,
        base_url=endpoint.base_url,
        model="whisper-1",
        api_key=EXTERNAL_KEY,
        timeout=30.0,
        languages=_LANGUAGE_CODES,
        chunk_seconds=300,
        max_duration=7200,
    )

    assert len(endpoint.posts) == 3
    durations = [opus_facts(data)[3] for _auth, data, _form in endpoint.posts]
    assert all(duration <= 300.1 for duration in durations)
    assert sum(durations) == pytest.approx(LONG_TILES * 32, abs=0.5)
    starts = [cue.start for cue in transcription.segments]
    assert starts[0] == pytest.approx(0.5)
    assert starts[1] == pytest.approx(durations[0] + 0.5, abs=0.1)
    assert starts[2] == pytest.approx(durations[0] + durations[1] + 0.5, abs=0.1)
    assert all(auth == f"Bearer {EXTERNAL_KEY}" for auth, _data, _form in endpoint.posts)


def test_the_child_encodes_the_clip_for_the_self_check(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, asr_models_path=tmp_path / "no-models")

    reply = run(in_child(settings, "opus_check", {"path": str(CLIP), "max_duration": 60}))

    assert reply["bytes"] > 10_000


def test_an_upload_with_the_callers_key_through_the_real_child(
    tmp_path: Path, httpserver: Any
) -> None:
    """A whole item on the real children with the external backend and no model: the endpoint
    sees the caller's key (not the operator's), gets Ogg Opus, and the result is the
    transcript; neither key is in a log line or the result."""
    from structlog.testing import capture_logs

    from tests.test_worker import submit_upload

    endpoint = _Endpoint(httpserver)
    harness = Harness(
        asr_enabled=True,
        asr_backend="openai",
        asr_external_base_url="https://asr.example.org/v1",
        asr_external_api_key="sk-operator-key-never-sent-77",
        asr_scratch_path=tmp_path / "scratch",
        asr_models_path=tmp_path / "no-models",
    )
    # The test endpoint speaks plain HTTP, which the setting itself does not allow.
    settings = harness.settings.model_copy(update={"asr_external_base_url": endpoint.base_url})

    async def scenario() -> Any:
        job_id = await submit_upload(harness, CLIP.read_bytes(), "audio/ogg")
        await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
        await harness.store.put_asr_key(job_id, EXTERNAL_KEY)
        proxy = create_asr_proxy(settings, CollectorRegistry())
        pool: DocumentPool = create_asr_pool(settings)
        await pool.start()
        try:
            runner = AsrRunner(
                harness.pipeline,
                harness.store,
                settings,
                pool,
                proxy,
                DomainGate(harness.valkey, settings),
                harness.registry,
            )
            worker = Worker(
                settings,
                harness.valkey,
                runner,
                harness.store,
                harness.asr_queue,
                harness.registry,
                clock=harness.clock,
                concurrency=1,
            )
            claim = await harness.asr_queue.claim(runner.lease_seconds)
            assert claim.item is not None
            with capture_logs() as logs:
                await worker.process(claim.item)
        finally:
            await pool.shutdown()
        (item,) = await harness.items(job_id)
        return item, logs

    item, logs = run(scenario())

    ((auth, data, form),) = endpoint.posts
    assert auth == f"Bearer {EXTERNAL_KEY}"
    assert opus_facts(data)[:3] == ("ogg", "opus", 1)
    assert form["model"] == "whisper-1"
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.media is not None
    assert item.result.data.media.asr_model == "whisper-1"
    assert "Gregor Samsa" in (item.result.data.content or "")
    dumped = json.dumps(logs, default=str) + item.result.model_dump_json()
    assert EXTERNAL_KEY not in dumped
    assert "sk-operator-key-never-sent-77" not in dumped
