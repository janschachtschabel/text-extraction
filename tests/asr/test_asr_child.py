"""The real ASR children (``media/asr_child.py``): PyAV's decoding and probe, the download of a
media file through the egress proxy, and faster-whisper with the model on ``ASR_MODELS_PATH``
(filled by ``text-extraction-asr-worker --download-models``). Marker ``asr``: ``uv sync --extra
asr``, then ``uv run pytest -m asr``. Without the extra ``asr`` the module is skipped; the clip's
transcription is skipped while the model is not on the volume.
"""

import asyncio
import importlib.util
import os
import re
import secrets
import threading
from collections.abc import Coroutine, Iterator
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from text_extraction.asr_worker import create_asr_pool, create_asr_proxy, ffmpeg_gpl_findings
from text_extraction.documents.pool import ConversionError, DocumentPool
from text_extraction.settings import Settings

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
    assert reply["kind"] == "ogg"
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
