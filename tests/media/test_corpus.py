"""The media corpus end to end (package 6's "done when", ruling M6): ``POST /v1/extract`` of the
public app on fakeredis answers platform videos with their subtitles and an HTML page with its
``<track>``, and an audio upload through ``POST /v1/extract/file`` becomes a job that the ASR
worker transcribes.

Default suite: the caption fixtures in ``tests/media/captions`` (YouTube's json3 and rolling
automatic captions, ARD's TTML, ZDF's and a lecture's WebVTT, a Latin-1 SRT, an empty track) are
the subtitle tracks a fake ``MediaFetcher`` hands the pipeline, as the children would (the
sandbox reaches no platform). Marker ``asr``: ``tests/media/corpus/speech_de.ogg`` is uploaded,
answered 202, transcribed by the ASR worker (its runner on the real children and model, as
``text-extraction-asr-worker`` builds it) and read back from ``GET /v1/jobs/{id}``; it needs
``uv sync --extra asr`` and the model under ``ASR_MODELS_PATH`` (``--download-models``), and is
skipped without them.
"""

import asyncio
import importlib.util
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from tests.fakes import Answer, FakeFetcher, fetched
from tests.test_pipeline_media import FakeMediaFetcher, probe
from text_extraction.app import create_public_app
from text_extraction.conversion_pool import ConversionPool
from text_extraction.fetch import Fetcher
from text_extraction.media.captions import CaptionFormat
from text_extraction.media.pool import FetchedTrack, MediaFetcher
from text_extraction.media.types import TranscriptSource
from text_extraction.pipeline import create_pipeline
from text_extraction.settings import Settings

MEDIA = Path(__file__).parent
CAPTIONS = MEDIA / "captions"
CORPUS = MEDIA / "corpus"
_WORD = re.compile(r"\w+")
# The fixtures are a few cues each: judged by the default QUALITY_MIN_WORDS (50) they would all
# be content_too_short, which says nothing about the captions.
MIN_WORDS = 10


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "outbound_min_delay_ms": 0,
        "quality_min_words": MIN_WORDS,
    }
    values.update(overrides)
    return Settings(**values)


def public_app(
    settings: Settings,
    server: FakeServer,
    *,
    media: FakeMediaFetcher | None = None,
    answers: dict[str, Answer] | None = None,
) -> Any:
    """The public app on ``server``; its pipeline fetches from ``answers`` and probes videos
    with ``media``."""
    registry = CollectorRegistry()
    app = create_public_app(settings, FakeAsyncRedis(server=server), registry)
    app.state.pipeline = create_pipeline(
        settings,
        FakeAsyncRedis(server=server),
        cast(Fetcher, FakeFetcher(answers or {})),
        registry,
        ConversionPool(1),
        memory_guard=app.state.memory_guard,
        media_fetcher=cast(MediaFetcher | None, media),
    )
    return app


def extract(app: Any, body: dict[str, Any]) -> dict[str, Any]:
    with TestClient(app) as client:
        response = client.post("/v1/extract", json=body)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


# --- subtitles of platform videos (default suite) -------------------------------------------


@dataclass(frozen=True)
class Case:
    platform: str
    url: str
    fixture: str
    format: CaptionFormat
    source: TranscriptSource
    key: str  # the platform's track key (a child normalises its language)
    spoken: list[str]  # lines of the transcript, in order


CASES = [
    Case(
        "youtube",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "youtube.json3",
        "json3",
        "auto_subtitles",
        "de-orig",
        ["hallo und willkommen zu unserem Video über die Größe der Sonne"],
    ),
    Case(
        "youtube",
        "https://youtu.be/dQw4w9WgXcQ",
        "youtube_auto.vtt",
        "vtt",
        "auto_subtitles",
        "de-orig",
        ["hallo und herzlich willkommen zu", "unserem Video über die Größe der", "riesig & heiß"],
    ),
    Case(
        "ard",
        "https://www.ardmediathek.de/video/sendung/folge/ndr/Y3JpZDovL25kci5kZS9hYmM",
        "ard.ttml",
        "ttml",
        "manual_subtitles",
        "deu",
        [
            "Guten Abend, meine Damen und Herren.",
            "Willkommen zur Sendung.",
            "Heute: Forschung & Lehre",
            "Verschachtelt und über mehrere Zeilen.",
        ],
    ),
    Case(
        "zdf",
        "https://www.zdf.de/video/dokus/terra-x-100/folge-100",
        "zdf.vtt",
        "vtt",
        "manual_subtitles",
        "deu",
        [
            "Heute beginnt die Woche mit viel Sonnenschein.",
            "Im Norden bleibt es aber wolkig.",
            "Weitere Informationen finden Sie im Videotext.",
        ],
    ),
    Case(
        "vimeo",
        "https://player.vimeo.com/video/76979871",
        "manual.vtt",
        "vtt",
        "manual_subtitles",
        "de",
        [
            "Willkommen zur Vorlesung über Flüsse.",
            'Der Rhein ist 1.233 km lang und fließt durch "sechs" Länder.',
            "Rhein & Donau, 3 < 5 und 7 > 2.",
        ],
    ),
    Case(
        "arte",
        "https://www.arte.tv/de/videos/134660-000-A/die-muetter-von-cherson/",
        "latin1.srt",
        "srt",
        "manual_subtitles",
        "de",
        [
            "Die Größe des Gebäudes überrascht",
            "Wir müssen früh aufstehen, sagte die Bäuerin.",
            "Äpfel, Öl und Brötchen für alle.",
        ],
    ),
]


def subtitles(case: Case) -> FakeMediaFetcher:
    """The probe of a German video whose chosen track is the fixture."""
    track = FetchedTrack(
        key=case.key,
        language="de",
        source=case.source,
        format=case.format,
        body=(CAPTIONS / case.fixture).read_bytes(),
    )
    return FakeMediaFetcher(probe(track=track, original_language="de"))


@pytest.mark.parametrize("case", CASES, ids=[case.fixture for case in CASES])
def test_a_platform_video_answers_with_its_subtitles(case: Case) -> None:
    settings = make_settings()
    media = subtitles(case)
    app = public_app(settings, FakeServer(), media=media)

    answer = extract(app, {"url": case.url, "format": "text"})

    assert (answer["status"], answer["error"]) == ("success", None), answer["error"]
    data = answer["data"]
    assert data["media"]["transcript_source"] == case.source
    assert data["media"]["transcript_language"] == "de"
    assert (data["mode_used"], data["content_type"], data["language"]) == ("fast", "text/vtt", "de")
    content = data["content"]
    assert content.startswith("Photosynthesis for kids")  # the title, as text
    positions = [content.find(line) for line in case.spoken]
    assert -1 not in positions, content
    assert positions == sorted(positions)
    for line in case.spoken:  # rolling duplicates of automatic captions are gone
        assert content.count(line) == 1
    assert "<" not in content.replace("3 < 5", "")  # no tags or entities left
    assert "&amp;" not in content
    assert data["metadata"]["description"] == "How plants eat light.\nA lesson for grade 5."
    ((target, _languages, _deadline),) = media.calls  # one probe, of the canonical URL
    assert data["media"]["platform"] == target.platform == case.platform
    assert data["final_url"] == target.canonical_url


def test_a_platform_video_has_chapters_and_markdown_by_default() -> None:
    case = CASES[4]  # the lecture
    app = public_app(make_settings(), FakeServer(), media=subtitles(case))

    data = extract(app, {"url": case.url})["data"]

    assert data["format"] == "markdown"
    assert data["content"].startswith("# Photosynthesis for kids\n")
    assert "Willkommen zur Vorlesung über Flüsse." in data["content"]


def test_an_empty_track_answers_the_videos_details() -> None:
    """A track without a cue is no transcript: the details answer, retryable, not cached."""
    track = FetchedTrack(
        key="de",
        language="de",
        source="manual_subtitles",
        format="vtt",
        body=(CAPTIONS / "empty.vtt").read_bytes(),
    )
    app = public_app(
        make_settings(),
        FakeServer(),
        media=FakeMediaFetcher(probe(track=track, original_language="de")),
    )

    answer = extract(app, {"url": "https://www.3sat.de/wissen/nano/sendung-100.html"})

    assert answer["status"] == "partial"
    assert (answer["error"]["code"], answer["error"]["retryable"]) == (
        "no_transcript_available",
        True,
    )
    data = answer["data"]
    assert data["media"]["transcript_source"] == "metadata_only"
    assert "subtitles_failed" in data["warnings"]
    assert "How plants eat light." in data["content"]
    assert data["fingerprint"] is None


# --- an HTML page with a <track> (default suite) --------------------------------------------

PAGE = "https://lessons.example/rivers.html"
TRACK = "https://lessons.example/subs/rivers.de.vtt"
PARAGRAPH = (
    "Rivers carry water from the mountains to the sea and shape the land on their way. "
    "They give people water to drink, fields to farm and a road for ships."
)


def test_a_page_with_a_track_gets_its_transcript() -> None:
    page = (
        '<!doctype html><html lang="de"><head><title>Flüsse</title></head><body><article>'
        f"<h1>Flüsse</h1><p>{PARAGRAPH}</p><p>{PARAGRAPH}</p>"
        '<video src="/media/rivers.mp4"><track kind="subtitles" src="/subs/rivers.de.vtt" '
        'srclang="de" label="Deutsch"></video></article></body></html>'
    )
    answers: dict[str, Answer] = {
        PAGE: fetched(PAGE, page),
        TRACK: fetched(TRACK, (CAPTIONS / "manual.vtt").read_bytes(), content_type="text/vtt"),
    }
    app = public_app(
        make_settings(), FakeServer(), media=FakeMediaFetcher(probe()), answers=answers
    )

    answer = extract(app, {"url": PAGE, "mode": "fast"})

    assert answer["status"] == "success"
    data = answer["data"]
    assert data["media"]["platform"] == "html5"
    assert data["media"]["transcript_source"] == "manual_subtitles"
    content = data["content"]
    page_text, _, transcript = content.partition("## Transcript")
    assert "Rivers carry water" in page_text
    assert "Willkommen zur Vorlesung über Flüsse." in transcript
    assert "NOTE" not in transcript
    assert "editorial team" not in transcript  # NOTE, STYLE and REGION blocks are dropped


# --- an audio upload, transcribed by the ASR worker (marker asr) ----------------------------


def words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def recall(expected: str, content: str) -> float:
    """The share of the expected words (with repeats) that occur in ``content``."""
    wanted = words(expected)
    found = set(words(content))
    return sum(1 for word in wanted if word in found) / len(wanted)


async def transcribe_next(settings: Settings, server: FakeServer) -> None:
    """Claim the next item of pool ``asr`` and run it as ``text-extraction-asr-worker`` would:
    its parts (pipeline, runner, real children and egress proxy) as ``build_asr_worker`` makes
    them. The children's self-check is left out: on a developer machine or a CI runner the
    PyPI wheel of PyAV and an ``ffmpeg`` on PATH would refuse it (decision D5); the ASR image
    runs it."""
    from text_extraction.asr_dispatch import asr_queue
    from text_extraction.asr_worker import build_asr_worker
    from text_extraction.jobs import asr_lease_seconds

    valkey = FakeAsyncRedis(server=server)
    parts = build_asr_worker(settings, valkey, CollectorRegistry())
    await parts.proxy.start()
    await parts.asr_pool.start()
    try:
        claim = await asr_queue(valkey, settings).claim(asr_lease_seconds(settings))
        assert claim.item is not None, "no item on pool asr"
        await parts.worker.process(claim.item)
    finally:
        await parts.asr_pool.shutdown()
        await parts.proxy.stop()
        await parts.fetcher.aclose()
        parts.conversion_pool.shutdown()


@pytest.mark.asr
@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None or importlib.util.find_spec("av") is None,
    reason="the extra asr is not installed (uv sync --extra asr)",
)
def test_an_uploaded_german_recording_is_transcribed(tmp_path: Path) -> None:
    settings = make_settings(
        quality_min_words=50,
        asr_enabled=True,
        asr_concurrency=1,
        asr_models_path=Path(os.environ.get("ASR_MODELS_PATH", "/models")),
        asr_scratch_path=tmp_path / "scratch",
    )
    if not (settings.asr_models_path / settings.asr_model / "model.bin").is_file():
        pytest.skip(f"no {settings.asr_model} model under {settings.asr_models_path}")
    clip = (CORPUS / "speech_de.ogg").read_bytes()
    server = FakeServer()

    with TestClient(public_app(settings, server)) as client:
        response = client.post(
            "/v1/extract/file", files={"file": ("speech_de.ogg", clip, "audio/ogg")}
        )
    assert response.status_code == 202, response.text
    accepted = response.json()["data"]
    assert accepted["kind"] == "file"

    asyncio.run(transcribe_next(settings, server))

    # Another API pod reads the job back: the result lives in Valkey only.
    with TestClient(public_app(settings, server)) as client:
        view = client.get(accepted["status_url"]).json()["data"]
    assert view["state"] == "completed"
    (item,) = view["items"]
    answer = item["result"]
    assert answer["status"] == "success", answer["error"]
    data = answer["data"]
    assert (data["url"], data["mode_used"], data["content_type"]) == (None, "upload", "audio/ogg")
    media = data["media"]
    assert (media["platform"], media["transcript_source"]) == ("upload", "asr")
    assert media["asr_model"] == settings.asr_model
    assert media["transcript_language"] == "de"
    assert 29.0 <= media["duration_seconds"] <= 31.0
    expected = (CORPUS / "speech_de.expected.txt").read_text(encoding="utf-8")
    assert recall(expected, data["content"]) >= 0.8
