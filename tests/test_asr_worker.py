"""The ASR worker (``asr_worker.py``) with fakes: its runner on the real ``Worker``, job store and
pipeline (fakeredis, a fake fetcher and media fetcher, the extract worker moving items to pool
``asr``), a fake pool instead of the ASR children, a fake egress proxy, the start-up checks and
the parent's imports in a fresh interpreter. Nothing here imports faster-whisper, CTranslate2 or
PyAV, so the suite runs without the extra ``asr``.

The real children (the model, PyAV, the clip) are in ``tests/asr`` (marker ``asr``)."""

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from tests.fakes import Answer as FetchAnswer
from tests.fakes import fetched
from tests.test_worker import (
    OWNER,
    YOUTUBE,
    Harness,
    asr_harness,
    error_of,
    scenario,
    submit_transcription,
    submit_upload,
)
from text_extraction import asr_worker, model_store
from text_extraction.asr_worker import (
    AsrRunner,
    SelfCheckFailed,
    clean_scratch,
    ffmpeg_gpl_findings,
    self_check,
)
from text_extraction.browser_proxy import ProxyContext
from text_extraction.documents.pool import (
    ConversionCrashed,
    ConversionError,
    ConversionOutOfMemory,
    ConversionTimeout,
)
from text_extraction.envelope import ErrorCode
from text_extraction.job_models import JobItem, JobOptions
from text_extraction.jobs import asr_lease_seconds
from text_extraction.outbound import DomainGate, lease_ttl_seconds
from text_extraction.pipeline_media import ASR_UNAVAILABLE_WARNING
from text_extraction.settings import Settings
from text_extraction.worker import Worker

LEAKY_PATH = "/srv/asr/item-x/upload.ogg"  # a path a library message may quote
OGG = b"OggS\x00\x02" + b"\x00" * 64
FILE_URL = "https://media.example.org/lecture.ogg"
CANONICAL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
SPEECH = (
    "Als Gregor Samsa eines Morgens aus unruhigen Träumen erwachte, fand er sich in seinem Bett "
    "zu einem ungeheueren Ungeziefer verwandelt. Er lag auf seinem panzerartig harten Rücken und "
    "sah, wenn er den Kopf ein wenig hob, seinen gewölbten, braunen, von bogenförmigen "
    "Versteifungen geteilten Bauch, auf dessen Höhe sich die Bettdecke, zum gänzlichen "
    "Niedergleiten bereit, kaum noch erhalten konnte."
)
SEGMENTS = [[0.0, 6.5, SPEECH[:120]], [6.5, 14.0, SPEECH[120:240]], [14.0, 30.0, SPEECH[240:]]]
LGPL_CONFIG = "--disable-static --enable-shared --enable-version3 --enable-libopus --disable-doc"
LGPL = {"configuration": LGPL_CONFIG, "license": "LGPL version 3 or later"}
LIBRARIES = {"libavutil": LGPL, "libavcodec": LGPL, "libavformat": LGPL, "libswresample": LGPL}


def transcript(**fields: Any) -> dict[str, Any]:
    """A reply of the child's ``"transcribe"``."""
    values: dict[str, Any] = {"segments": SEGMENTS, "language": "de", "duration": 30.0}
    values.update(fields)
    return values


def downloaded(
    duration: float | None = 30.0, kind: str = "ogg"
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A ``"download"`` that writes a small file into the item's scratch directory."""

    def download(payload: dict[str, Any]) -> dict[str, Any]:
        path = Path(payload["scratch"]) / f"media.{kind}"
        path.write_bytes(OGG)
        return {"path": str(path), "duration": duration, "kind": kind}

    return download


Answer = dict[str, Any] | BaseException | Callable[[dict[str, Any]], Any]


class FakeChildren:
    """The ASR children: answers each function by name and records the calls (with whether the
    payload's ``path`` existed when the call came)."""

    def __init__(self, **answers: Answer) -> None:
        self.answers: dict[str, Answer] = {
            "platform_info": {"info": '{"id": "x"}', "duration": 212.0},
            "download": downloaded(),
            "transcribe": transcript(),
            "ffmpeg": {"libraries": LIBRARIES, "version": "19.0.1"},
        }
        self.answers.update(answers)
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.paths_existed: list[bool] = []
        self.on_call: Callable[[str], None] | None = None

    async def start(self) -> None:
        pass

    async def run(self, function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        self.calls.append((function_name, payload, deadline))
        if "path" in payload:
            self.paths_existed.append(Path(payload["path"]).is_file())  # noqa: ASYNC240
        if self.on_call is not None:
            self.on_call(function_name)
        answer = self.answers[function_name]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(payload)
        return answer

    async def shutdown(self) -> None:
        pass

    def names(self) -> list[str]:
        return [name for name, _payload, _deadline in self.calls]

    def payload(self, name: str) -> dict[str, Any]:
        (payload,) = [payload for called, payload, _ in self.calls if called == name]
        return payload


def note(held: dict[str, list[str]], name: str, hosts: list[str]) -> None:
    """Note the turns held when the child function ``name`` is called first."""
    held.setdefault(name, list(hosts))


class FakeEgress:
    """The worker's egress proxy: a URL and one context per child call (recorded)."""

    url = "http://127.0.0.1:3128"

    def __init__(self) -> None:
        self.contexts: list[tuple[ProxyContext, int | None]] = []
        self.refusal: tuple[str, str] | None = None

    def open_context(self, max_bytes: int | None = None) -> ProxyContext:
        context = ProxyContext(max_bytes or 1, lambda _context: None)
        if self.refusal is not None:
            context._refuse(*self.refusal)
        self.contexts.append((context, max_bytes))
        return context


class SpyGate(DomainGate):
    """The outbound gate, noting the turns held."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.held: list[str] = []
        self.turns: list[tuple[str, float | None]] = []

    @asynccontextmanager
    async def slot(self, host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        async with super().slot(host, crawl_delay):
            self.turns.append((host, crawl_delay))
            self.held.append(host)
            try:
                yield
            finally:
                self.held.remove(host)


def settings_for(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "asr_scratch_path": tmp_path / "scratch",
        "asr_models_path": tmp_path / "models",
        **overrides,
    }


class Asr:
    """An ASR worker next to a harness: its runner on the harness's store, queue ``asr`` and
    pipeline, with ``children`` as its pool."""

    def __init__(self, harness: Harness, children: FakeChildren | None = None) -> None:
        self.harness = harness
        self.children = children or FakeChildren()
        self.egress = FakeEgress()
        self.gate = SpyGate(harness.valkey, harness.settings)
        self.runner = AsrRunner(
            harness.pipeline,
            harness.store,
            harness.settings,
            self.children,
            self.egress,
            self.gate,
            harness.registry,
        )
        self.worker = Worker(
            harness.settings,
            harness.valkey,
            self.runner,
            harness.store,
            harness.asr_queue,
            harness.registry,
            clock=harness.clock,
            concurrency=1,
        )

    async def process_next(self) -> None:
        claim = await self.harness.asr_queue.claim(self.runner.lease_seconds)
        assert claim.item is not None, "nothing queued on asr"
        await self.worker.process(claim.item)

    def scratch_entries(self) -> list[Path]:
        root = self.harness.settings.asr_scratch_path
        return sorted(root.iterdir()) if root.exists() else []


async def moved(harness: Harness, urls: list[str] | None = None) -> str:
    """A batch whose item the extract worker moved to ``asr`` (a YouTube video by default)."""
    job_id = await submit_transcription(harness, urls or [YOUTUBE])
    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
    assert (await harness.asr_queue.counts()).pending == 1
    return job_id


async def moved_upload(harness: Harness) -> str:
    job_id = await submit_upload(harness, OGG, "audio/ogg")
    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]
    assert (await harness.asr_queue.counts()).pending == 1
    return job_id


def file_harness(tmp_path: Path, **overrides: Any) -> Harness:
    """A harness whose fetcher serves an Ogg file at ``FILE_URL``."""
    from tests.test_pipeline_media import FakeMediaFetcher, probe

    answers: dict[str, FetchAnswer] = {FILE_URL: fetched(FILE_URL, OGG, content_type="audio/ogg")}
    return Harness(
        answers,
        media_fetcher=FakeMediaFetcher(probe(track=None)),
        asr_enabled=True,
        **settings_for(tmp_path, **overrides),
    )


async def only_item(harness: Harness, job_id: str) -> JobItem:
    (item,) = await harness.items(job_id)
    assert item.result is not None
    return item


# --- the runner ---------------------------------------------------------------------------------


@scenario
async def test_runner_upload_item(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    job_id = await moved_upload(harness)

    await asr.process_next()

    assert asr.children.names() == ["transcribe"]  # nothing to download
    assert asr.children.paths_existed == [True]
    path = Path(asr.children.payload("transcribe")["path"])
    assert path.is_relative_to(harness.settings.asr_scratch_path)
    assert "notes" not in path.name  # never the caller's file name
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "success"
    data = item.result.data
    assert data is not None
    assert data.media is not None
    assert (data.media.platform, data.media.transcript_source) == ("upload", "asr")
    assert data.media.asr_model == "large-v3-turbo"
    assert data.media.transcript_language == "de"
    assert data.media.duration_seconds == 30.0
    assert (data.url, data.mode_used, data.content_type) == (None, "upload", "audio/ogg")
    assert data.content is not None
    assert "Gregor Samsa" in data.content
    assert data.language == "de"
    assert asr.scratch_entries() == []  # the scratch file is gone
    assert await harness.valkey.keys("res:*") == []  # an upload is never cached


@scenario
async def test_runner_url_item_writes_cache(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    job_id = await moved(harness)

    await asr.process_next()

    assert asr.children.names() == ["platform_info", "download", "transcribe"]
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "success"
    data = item.result.data
    assert data is not None
    assert data.media is not None
    assert (data.media.platform, data.media.transcript_source) == ("youtube", "asr")
    assert data.media.asr_model == "large-v3-turbo"
    assert data.media.uploader == "Science Channel"  # the draft's details
    assert data.title == "Photosynthesis for kids"
    assert data.content is not None
    assert data.content.startswith("# Photosynthesis for kids")
    assert (data.url, data.final_url, data.mode_used) == (YOUTUBE, CANONICAL, "fast")
    assert data.content_type == "audio/ogg"
    assert data.metadata is not None
    assert data.metadata.description == "How plants eat light.\nA lesson for grade 5."
    assert ASR_UNAVAILABLE_WARNING not in data.warnings
    assert data.fingerprint is not None
    assert await harness.valkey.keys("res:*") != []  # cached under the canonical URL
    assert await harness.valkey.keys("fp:*") != []
    assert asr.scratch_entries() == []
    assert await harness.store.draft(job_id, 0) is None


@scenario
async def test_a_cached_transcription_answers_the_next_request(tmp_path: Path) -> None:
    """The ASR result is filed under the API's key of the video: the next request (with or
    without ``transcribe``) is a hit."""
    from text_extraction.extract_models import ExtractRequest

    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    await moved(harness)
    await asr.process_next()

    again = await harness.pipeline.run(
        ExtractRequest(url="https://youtu.be/dQw4w9WgXcQ", compliance="enforce"), OWNER
    )

    assert again.data is not None
    assert again.data.cache == "hit"
    assert again.data.media is not None
    assert again.data.media.transcript_source == "asr"


@scenario
async def test_too_long_before_transcription(tmp_path: Path) -> None:
    harness = file_harness(tmp_path)
    asr = Asr(harness, FakeChildren(download=downloaded(duration=4 * 3600.0)))
    job_id = await moved(harness, [FILE_URL])

    await asr.process_next()

    assert "transcribe" not in asr.children.names()
    code, retryable, message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.MEDIA_TOO_LONG, False)
    assert "10800" in message
    assert asr.scratch_entries() == []


@scenario
async def test_a_known_duration_over_the_limit_downloads_nothing(tmp_path: Path) -> None:
    """The platform said how long the video is (the draft's probe): over the limit it is not even
    downloaded."""
    harness = asr_harness(**settings_for(tmp_path, media_max_duration_seconds=120))
    asr = Asr(harness)
    job_id = await moved(harness)  # the probe says 212 s

    await asr.process_next()

    assert asr.children.names() == []
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "partial"  # the draft answers
    assert item.result.error is not None
    assert (item.result.error.code, item.result.error.retryable) == (
        ErrorCode.MEDIA_TOO_LONG,
        False,
    )
    data = item.result.data
    assert data is not None
    assert ASR_UNAVAILABLE_WARNING in data.warnings
    assert data.media is not None
    assert data.media.transcript_source == "metadata_only"


@scenario
async def test_platform_info_duration_over_the_limit_downloads_nothing(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path, media_max_duration_seconds=600))
    asr = Asr(harness, FakeChildren(platform_info={"info": "{}", "duration": 4 * 3600.0}))
    job_id = await moved(harness)

    await asr.process_next()

    assert asr.children.names() == ["platform_info"]
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.error is not None
    assert item.result.error.code == ErrorCode.MEDIA_TOO_LONG


@scenario
async def test_child_timeout_not_retryable(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness, FakeChildren(transcribe=ConversionTimeout("deadline")))
    job_id = await moved_upload(harness)

    await asr.process_next()

    code, retryable, message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.TIMEOUT, False)
    assert "ASR_TIMEOUT_SECONDS" in message
    assert asr.scratch_entries() == []


@scenario
async def test_download_failure_retryable_draft_answers(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness, FakeChildren(download=ConversionError("DownloadError", "HTTP Error 403")))
    job_id = await moved(harness)

    await asr.process_next()

    assert "transcribe" not in asr.children.names()
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "partial"
    assert item.result.error is not None
    assert item.result.error.retryable is True
    assert "HTTP Error 403" not in item.result.error.message  # a library's text never leaves
    data = item.result.data
    assert data is not None
    assert ASR_UNAVAILABLE_WARNING in data.warnings
    assert data.fingerprint is None
    assert data.media is not None
    assert data.media.transcript_source == "metadata_only"
    assert await harness.valkey.keys("res:*") == []
    assert await harness.valkey.keys("fp:*") == []
    assert asr.scratch_entries() == []


@scenario
async def test_a_final_failure_keeps_the_drafts_fingerprint(tmp_path: Path) -> None:
    """A decoder error would fail again: the draft answers and is compared and stored."""
    harness = asr_harness(**settings_for(tmp_path))
    failing = ConversionError("DecodeFailed", "Invalid data found")
    asr = Asr(harness, FakeChildren(transcribe=failing))
    job_id = await moved(harness)

    await asr.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.error is not None
    assert (item.result.error.code, item.result.error.retryable) == (
        ErrorCode.TRANSCRIPTION_FAILED,
        False,
    )
    assert item.result.data is not None
    assert item.result.data.fingerprint is not None
    assert ASR_UNAVAILABLE_WARNING in item.result.data.warnings


@scenario
async def test_expired_upload_input_timeout_retryable(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    job_id = await moved_upload(harness)
    await harness.store.delete_input(job_id, 0)  # its TTL ran out while it waited

    await asr.process_next()

    assert asr.children.names() == []
    code, retryable, _message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.TIMEOUT, True)


@scenario
async def test_an_expired_input_answers_the_draft(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    job_id = await moved(harness)
    await harness.store.delete_input(job_id, 0)

    await asr.process_next()

    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert ASR_UNAVAILABLE_WARNING in item.result.data.warnings
    assert item.result.data.fingerprint is None


@scenario
async def test_scratch_deleted_on_crash(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness, FakeChildren(transcribe=ConversionCrashed("died")))
    job_id = await moved_upload(harness)

    await asr.process_next()

    assert asr.children.paths_existed == [True]
    assert asr.scratch_entries() == []
    code, retryable, _message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.TRANSCRIPTION_FAILED, True)


@scenario
async def test_scratch_deleted_when_the_item_is_cancelled(tmp_path: Path) -> None:
    """SIGTERM past the grace period cancels the item: its scratch directory goes with it."""
    harness = asr_harness(**settings_for(tmp_path))
    started = asyncio.Event()

    async def hang(payload: dict[str, Any]) -> Any:
        started.set()
        await asyncio.sleep(3600)

    children = FakeChildren()

    async def run(function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        return await hang(payload)

    children.run = run  # type: ignore[method-assign]
    asr = Asr(harness, children)
    await moved_upload(harness)

    task = asyncio.create_task(asr.process_next())
    await started.wait()
    assert len(asr.scratch_entries()) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert asr.scratch_entries() == []


@scenario
async def test_out_of_memory_is_a_final_failure(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness, FakeChildren(transcribe=ConversionOutOfMemory("limit")))
    job_id = await moved_upload(harness)

    await asr.process_next()

    code, retryable, _message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.TRANSCRIPTION_FAILED, False)


@pytest.mark.parametrize(
    ("type_name", "code", "retryable"),
    [
        ("ModelsMissing", ErrorCode.INTERNAL_ERROR, True),
        ("DecodeFailed", ErrorCode.TRANSCRIPTION_FAILED, False),
        ("MediaTooLong", ErrorCode.MEDIA_TOO_LONG, False),
        ("ValueError", ErrorCode.TRANSCRIPTION_FAILED, False),
    ],
)
@scenario
async def test_transcription_failures_map_to_codes(
    tmp_path: Path, type_name: str, code: ErrorCode, retryable: bool
) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness, FakeChildren(transcribe=ConversionError(type_name, LEAKY_PATH)))
    job_id = await moved_upload(harness)

    with capture_logs() as logs:
        await asr.process_next()

    found = error_of(await only_item(harness, job_id))
    assert found[:2] == (code, retryable)
    assert LEAKY_PATH not in found[2]
    assert LEAKY_PATH not in json.dumps(logs, default=str)


@pytest.mark.parametrize(
    ("refusal", "code", "retryable"),
    [
        ("blocked_address", ErrorCode.BLOCKED_URL, False),
        ("bytes_cap", ErrorCode.TOO_LARGE, False),
        ("dns_error", ErrorCode.DNS_ERROR, True),
        ("connect_error", ErrorCode.CONNECTION_ERROR, True),
    ],
)
@scenario
async def test_proxy_refusals_of_a_download(
    tmp_path: Path, refusal: str, code: ErrorCode, retryable: bool
) -> None:
    harness = file_harness(tmp_path)
    asr = Asr(harness, FakeChildren(download=ConversionError("URLError", "refused")))
    asr.egress.refusal = ("169.254.169.254", refusal)
    job_id = await moved(harness, [FILE_URL])

    await asr.process_next()

    assert error_of(await only_item(harness, job_id))[:2] == (code, retryable)
    assert asr.scratch_entries() == []


@pytest.mark.parametrize(
    ("type_name", "message", "code", "retryable"),
    [
        ("TooLarge", "", ErrorCode.TOO_LARGE, False),
        ("NotMedia", "", ErrorCode.UNSUPPORTED_CONTENT_TYPE, False),
        ("HttpStatus", "404", ErrorCode.TARGET_HTTP_ERROR, False),
        ("HttpStatus", "503", ErrorCode.TARGET_HTTP_ERROR, True),
        ("IsLive", "", ErrorCode.UNSUPPORTED_CONTENT_TYPE, False),
        ("DownloadError", "timed out", ErrorCode.TRANSCRIPTION_FAILED, True),
    ],
)
@scenario
async def test_download_failures_map_to_codes(
    tmp_path: Path, type_name: str, message: str, code: ErrorCode, retryable: bool
) -> None:
    harness = file_harness(tmp_path)
    asr = Asr(harness, FakeChildren(download=ConversionError(type_name, message)))
    job_id = await moved(harness, [FILE_URL])

    await asr.process_next()

    item = await only_item(harness, job_id)
    assert error_of(item)[:2] == (code, retryable)
    if type_name == "HttpStatus":
        assert item.result is not None
        assert item.result.data is not None
        assert item.result.data.upstream_status == int(message)


@scenario
async def test_download_timeout_is_retryable(tmp_path: Path) -> None:
    harness = file_harness(tmp_path)
    asr = Asr(harness, FakeChildren(download=ConversionTimeout("deadline")))
    job_id = await moved(harness, [FILE_URL])

    await asr.process_next()

    code, retryable, message = error_of(await only_item(harness, job_id))
    assert (code, retryable) == (ErrorCode.TIMEOUT, True)
    assert "MEDIA_DOWNLOAD_TIMEOUT_SECONDS" in message


@scenario
async def test_a_file_download_holds_the_file_hosts_turn(tmp_path: Path) -> None:
    harness = file_harness(tmp_path)
    children = FakeChildren()
    asr = Asr(harness, children)
    held: dict[str, list[str]] = {}
    children.on_call = lambda name: note(held, name, asr.gate.held)
    job_id = await moved(harness, [FILE_URL])

    await asr.process_next()

    assert held == {"download": ["media.example.org"], "transcribe": []}
    payload = children.payload("download")
    assert (payload["source"], payload["url"], payload["extractor"]) == ("file", FILE_URL, None)
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.status == "success"
    assert item.result.data is not None
    assert item.result.data.media is not None
    assert item.result.data.media.platform == "file"


@scenario
async def test_a_platform_download_holds_the_page_hosts_turn_for_the_info_only(
    tmp_path: Path,
) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    children = FakeChildren()
    asr = Asr(harness, children)
    held: dict[str, list[str]] = {}
    children.on_call = lambda name: note(held, name, asr.gate.held)
    await moved(harness)

    await asr.process_next()

    assert held == {"platform_info": ["www.youtube.com"], "download": [], "transcribe": []}
    info = children.payload("platform_info")
    assert (info["url"], info["extractor"]) == (CANONICAL, "youtube")
    download = children.payload("download")
    assert (download["source"], download["info"]) == ("platform", '{"id": "x"}')


@scenario
async def test_download_payload_goes_through_the_proxy_with_its_caps(tmp_path: Path) -> None:
    harness = file_harness(tmp_path)
    asr = Asr(harness)
    await moved(harness, [FILE_URL])

    await asr.process_next()

    settings = harness.settings
    payload = asr.children.payload("download")
    (context, max_bytes), *_ = asr.egress.contexts
    assert payload["proxy"] == f"http://{context.username}:{context.password}@127.0.0.1:3128"
    assert payload["max_bytes"] == settings.media_max_bytes
    assert max_bytes is not None
    assert max_bytes >= settings.media_max_bytes  # the media and the platform's pages
    assert 0 < payload["timeout"] <= settings.media_download_timeout_seconds
    assert payload["user_agent"].startswith("text-extraction/")
    assert Path(payload["scratch"]).parent == settings.asr_scratch_path
    _name, _payload, deadline = asr.children.calls[0]
    assert deadline - asyncio.get_running_loop().time() <= settings.media_download_timeout_seconds


@scenario
async def test_transcribe_payload(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path, asr_model="small", asr_threads=3))
    asr = Asr(harness)
    options = JobOptions(compliance="enforce", transcribe=True, languages=["de-AT", "en"])
    job_id = await harness.store.create("batch", OWNER, options, [YOUTUBE])
    await harness.queue.enqueue(job_id, OWNER.name, [YOUTUBE])
    await harness.worker.process((await harness.claim()).item)  # type: ignore[arg-type]

    await asr.process_next()

    settings = harness.settings
    payload = asr.children.payload("transcribe")
    assert payload["model_path"] == str(settings.asr_models_path / "small")
    assert (payload["compute_type"], payload["threads"]) == ("int8", 3)
    assert payload["language"] == "de"  # the first requested language's primary subtag
    assert payload["max_duration"] == settings.media_max_duration_seconds
    _name, _payload, deadline = asr.children.calls[-1]
    remaining = deadline - asyncio.get_running_loop().time()
    assert settings.asr_timeout_seconds - 5 < remaining <= settings.asr_timeout_seconds
    item = await only_item(harness, job_id)
    assert item.result is not None
    assert item.result.data is not None
    assert item.result.data.media is not None
    assert item.result.data.media.asr_model == "small"


@scenario
async def test_an_item_that_has_its_result_is_not_transcribed_twice(tmp_path: Path) -> None:
    """A crash window can queue an item twice; the copy that downloads second finds the result
    of the first before it starts the long transcription."""
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    job_id = await moved(harness)
    first = await only_item_result_after_download(harness, asr, job_id)

    await asr.process_next()

    assert "transcribe" not in asr.children.names()
    item = await only_item(harness, job_id)
    assert item.result == first
    assert asr.scratch_entries() == []
    assert (await harness.asr_queue.counts()).leased == 0


async def only_item_result_after_download(harness: Harness, asr: Asr, job_id: str) -> Any:
    """Let another worker write the item's result while this one downloads."""
    other = harness.store
    result = await harness.store.draft(job_id, 0)
    assert result is not None

    def write_meanwhile(payload: dict[str, Any]) -> dict[str, Any]:
        asyncio.get_running_loop().create_task(other.write_result(job_id, 0, result))
        return downloaded()(payload)

    async def run(function_name: str, payload: dict[str, Any], deadline: float) -> Any:
        if function_name == "download":
            reply = write_meanwhile(payload)
            await asyncio.sleep(0.01)
            return reply
        return await FakeChildren.run(asr.children, function_name, payload, deadline)

    asr.children.run = run  # type: ignore[method-assign]
    asr.children.calls.clear()
    return result


@scenario
async def test_the_callers_key_is_never_read_or_logged_by_the_local_backend(
    tmp_path: Path,
) -> None:
    from tests.test_worker import queue_one_transcription

    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    key = "sk-caller-secret-key"
    await queue_one_transcription(harness, key=key)

    with capture_logs() as logs:
        await asr.process_next()

    assert key not in json.dumps([payload for _n, payload, _d in asr.children.calls])
    assert key not in json.dumps(logs, default=str)


@scenario
async def test_logs_carry_no_url_and_no_file_name(tmp_path: Path) -> None:
    harness = file_harness(tmp_path)
    asr = Asr(harness, FakeChildren(download=ConversionError("DownloadError", FILE_URL)))
    await moved(harness, [FILE_URL])

    with capture_logs() as logs:
        await asr.process_next()

    dumped = json.dumps(logs, default=str)
    assert "lecture" not in dumped
    assert FILE_URL not in dumped


@scenario
async def test_transcriptions_are_counted(tmp_path: Path) -> None:
    harness = asr_harness(**settings_for(tmp_path))
    asr = Asr(harness)
    await moved_upload(harness)

    await asr.process_next()

    labels = {"backend": "local", "outcome": "success"}
    assert harness.sample("text_extraction_asr_transcriptions_total", labels) == 1
    assert harness.sample("text_extraction_asr_audio_seconds_total", {"backend": "local"}) == 30
    assert harness.sample("text_extraction_asr_seconds_count", {"backend": "local"}) == 1


def test_runner_lease_covers_a_download_and_two_transcriptions() -> None:
    settings = Settings(auth_mode="none", asr_timeout_seconds=600)
    lease = asr_lease_seconds(settings)
    assert lease == 2 * 600 + 10
    assert lease > settings.media_download_timeout_seconds + settings.asr_timeout_seconds


def test_gate_lease_covers_a_download_while_asr_is_on() -> None:
    off = Settings(auth_mode="none")
    on = Settings(auth_mode="none", asr_enabled=True, media_download_timeout_seconds=900)
    assert lease_ttl_seconds(off) == 120
    assert lease_ttl_seconds(on) >= 900 + 30


# --- the scratch directory ---------------------------------------------------------------------


def test_clean_scratch_removes_what_a_dead_process_left(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    (root / "item-0123").mkdir(parents=True)
    (root / "item-0123" / "media.webm.part").write_bytes(b"x")
    (root / "other").mkdir()  # not ours

    clean_scratch(Settings(auth_mode="none", asr_scratch_path=root))

    assert sorted(entry.name for entry in root.iterdir()) == ["other"]


# --- the FFmpeg licence and the self-check ----------------------------------------------------


def test_ffmpeg_licence_check_passes_an_lgpl_build() -> None:
    assert ffmpeg_gpl_findings(LIBRARIES) == []


@pytest.mark.parametrize(
    ("configuration", "license", "finding"),
    [
        (LGPL_CONFIG + " --enable-gpl", "GPL version 2 or later", "--enable-gpl"),
        (LGPL_CONFIG + " --enable-nonfree", "nonfree and unredistributable", "--enable-nonfree"),
        # The PyPI wheel of av 19.0.1: no --enable-gpl, but GPL encoders linked in.
        (LGPL_CONFIG + " --enable-libx264 --enable-libx265", "LGPL version 3 or later", "libx264"),
        (LGPL_CONFIG + " --enable-libfdk-aac", "LGPL version 3 or later", "libfdk-aac"),
        (LGPL_CONFIG, "GPL version 3 or later", "GPL version 3 or later"),
    ],
    ids=["gpl", "nonfree", "x264", "fdk-aac", "license"],
)
def test_ffmpeg_licence_check_refuses_gpl_and_nonfree(
    configuration: str, license: str, finding: str
) -> None:
    libraries = {**LIBRARIES, "libavcodec": {"configuration": configuration, "license": license}}

    findings = ffmpeg_gpl_findings(libraries)

    assert any(finding in item for item in findings), findings
    assert all(item.startswith("libavcodec") for item in findings)


def test_ffmpeg_licence_check_refuses_an_empty_answer() -> None:
    assert ffmpeg_gpl_findings({}) != []


def test_self_check_passes(tmp_path: Path) -> None:
    children = FakeChildren(
        transcribe=transcript(segments=[[0.0, 5.0, "Als Gregor Samsa eines Morgens"]])
    )
    settings = Settings(auth_mode="none", **settings_for(tmp_path))

    asyncio.run(self_check(children, settings, children=2))

    assert children.names().count("transcribe") == 2
    assert children.names().count("ffmpeg") == 2
    path = Path(children.calls[-1][1]["path"])
    assert path.name == "asr_check.ogg"
    assert path.read_bytes().startswith(b"OggS")


@pytest.mark.parametrize(
    "answers",
    [
        {"transcribe": transcript(segments=[[0.0, 5.0, "Guten Tag"]])},
        {"transcribe": ConversionError("ModelsMissing", "no model.bin")},
        {"ffmpeg": {"libraries": {"libavcodec": {**LGPL, "configuration": "--enable-gpl"}}}},
    ],
    ids=["wrong-text", "error", "gpl-ffmpeg"],
)
def test_self_check_fails(tmp_path: Path, answers: dict[str, Any]) -> None:
    children = FakeChildren(**answers)
    settings = Settings(auth_mode="none", **settings_for(tmp_path))

    with capture_logs() as logs, pytest.raises(SelfCheckFailed):
        asyncio.run(self_check(children, settings, children=1))

    (failed,) = [line for line in logs if line["event"] == "asr_self_check_failed"]
    assert failed["models_path"] == str(settings.asr_models_path)


# --- the process -------------------------------------------------------------------------------


def test_missing_model_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("ASR_MODELS_PATH", str(tmp_path))

    with pytest.raises(SystemExit) as exit_info:
        asr_worker.main([])

    assert exit_info.value.code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (missing,) = [line for line in lines if line["event"] == "asr_models_missing"]
    assert missing["models_path"] == str(tmp_path)
    assert missing["model"] == "large-v3-turbo"
    assert missing["level"] == "error"


def test_check_exits_1_without_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_MODELS_PATH", str(tmp_path))

    with pytest.raises(SystemExit) as exit_info:
        asr_worker.main(["--check"])

    assert exit_info.value.code == 1


def test_download_models_fetches_only_the_configured_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("ASR_MODELS_PATH", str(tmp_path))
    monkeypatch.setenv("ASR_MODEL", "small")
    seen: list[tuple[Path, model_store.Manifest]] = []

    def download(root: Path, manifest: model_store.Manifest, **_kwargs: Any) -> bool:
        seen.append((root, manifest))
        return True

    monkeypatch.setattr(model_store, "download_models", download)

    with pytest.raises(SystemExit) as exit_info:
        asr_worker.main(["--download-models"])

    assert exit_info.value.code == 0
    ((root, manifest),) = seen
    assert root == tmp_path
    assert manifest == model_store.load_manifest("asr").only("small")


def test_a_failed_download_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("ASR_MODELS_PATH", str(tmp_path))

    def download(root: Path, manifest: model_store.Manifest, **_kwargs: Any) -> bool:
        raise model_store.ModelDownloadFailed("large-v3-turbo/model.bin: HTTP status 404")

    monkeypatch.setattr(model_store, "download_models", download)

    with pytest.raises(SystemExit) as exit_info:
        asr_worker.main(["--download-models"])

    assert exit_info.value.code == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    (failed,) = [line for line in lines if line["event"] == "asr_models_download_failed"]
    assert failed["models_path"] == str(tmp_path)


def test_asr_pool_gets_its_settings_through_its_own_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VALKEY_PASSWORD", "parent-secret")
    monkeypatch.setenv("ASR_EXTERNAL_API_KEY", "operator-secret")
    settings = Settings(
        auth_mode="none", asr_threads=3, asr_concurrency=2, **settings_for(tmp_path)
    )

    pool = asr_worker.create_asr_pool(settings)

    env = pool._env
    assert env["OMP_NUM_THREADS"] == "3"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "VALKEY_PASSWORD" not in env
    assert "ASR_EXTERNAL_API_KEY" not in env
    assert (pool._processes, pool._memory_limit_mb) == (2, 4096)
    assert pool._target == "text_extraction.media.asr_child"


def test_asr_proxy_tunnels_outlive_a_download() -> None:
    from prometheus_client import CollectorRegistry

    settings = Settings(auth_mode="none", media_download_timeout_seconds=900)

    proxy = asr_worker.create_asr_proxy(settings, CollectorRegistry())

    assert proxy.tunnel_timeout > 900


def test_worker_parent_imports_nothing_heavy(tmp_path: Path) -> None:
    """The worker process imports neither faster-whisper, CTranslate2, PyAV nor yt-dlp: only its
    children do."""
    script = f"""
import sys
from pathlib import Path
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from text_extraction import asr_worker
from text_extraction.settings import Settings

settings = Settings(auth_mode="none", asr_enabled=True, asr_scratch_path=Path({str(tmp_path)!r}))
parts = asr_worker.build_asr_worker(settings, FakeAsyncRedis(), CollectorRegistry())
forbidden = {{"faster_whisper", "ctranslate2", "av", "yt_dlp", "youtube_transcript_api",
             "onnxruntime", "tokenizers", "huggingface_hub", "deno"}}
loaded = sorted({{name.split(".")[0] for name in sys.modules}} & forbidden)
parts.conversion_pool.shutdown()
print(",".join(loaded))
"""
    result = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == ""
