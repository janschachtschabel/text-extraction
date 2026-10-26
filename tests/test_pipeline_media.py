"""Media in the URL pipeline (``pipeline_media.py``, rulings M1, M2, M5, M6, M8, M9): platform
URLs answered from a fake ``MediaFetcher``, media files, HTML5 tracks and embeds.

The sandbox reaches no platform: the probes are canned ``MediaProbe`` values, pages and tracks
come from a fake fetcher or pytest-httpserver.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from pytest_httpserver import HTTPServer
from structlog.testing import capture_logs

from tests.fakes import (
    ARTICLE,
    PAGE,
    PARAGRAPH,
    Answer,
    FakeFetcher,
    RecordingGate,
    Redirect,
    fetched,
)
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker
from text_extraction.documents.pool import ConversionTimeout
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.escalation import needs_browser
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import Fetcher
from text_extraction.fingerprint import FingerprintStore, _store_key
from text_extraction.media.captions import CaptionFormat
from text_extraction.media.pool import FetchedTrack, MediaFetcher, MediaProbe
from text_extraction.media.select import TrackRef
from text_extraction.media.sites import MediaTarget
from text_extraction.media.transcript import Chapter
from text_extraction.media.types import MediaEmbed, TranscriptSource
from text_extraction.netguard import is_public_address
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline
from text_extraction.pipeline_media import AsrNeeded, MediaInputMeta
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
YT_ID = "dQw4w9WgXcQ"
YT = f"https://www.youtube.com/watch?v={YT_ID}"
YT_SHORT = f"https://youtu.be/{YT_ID}?t=30"
YT_ROBOTS = "https://www.youtube.com/robots.txt"
FILE = "https://cdn.example/lessons/photosynthesis.mp3?token=secret"
GOLDEN = Path(__file__).parent / "documents" / "golden" / "package4_answers.json"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x10" + b"\x00" * 64 + b"\xff\xfb\x90\x64" + b"\x00" * 400

SENTENCES = [
    "Plants use the light of the sun to make their own food.",
    "This process is called photosynthesis and it happens in the leaves.",
    "The green pigment chlorophyll catches the light energy.",
    "Water comes up from the roots and carbon dioxide comes in from the air.",
    "Out of these the plant builds sugar and it gives off oxygen.",
    "Without photosynthesis there would be no oxygen for us to breathe.",
    "That is why forests and oceans full of algae matter so much.",
]


def vtt(sentences: list[str] = SENTENCES) -> bytes:
    cues = [
        f"00:00:{index * 3:02d}.000 --> 00:00:{index * 3 + 2:02d}.500\n{sentence}"
        for index, sentence in enumerate(sentences)
    ]
    return ("WEBVTT\n\n" + "\n\n".join(cues) + "\n").encode()


def track(
    source: TranscriptSource = "manual_subtitles",
    key: str = "en",
    body: bytes | None = None,
    fmt: CaptionFormat = "vtt",
) -> FetchedTrack:
    return FetchedTrack(
        key=key,
        language=key.split("-")[0],
        source=source,
        format=fmt,
        body=body if body is not None else vtt(),
    )


def probe(**fields: Any) -> MediaProbe:
    values: dict[str, Any] = {
        "title": "Photosynthesis for kids",
        "description": "How plants eat light.\nA lesson for grade 5.",
        "uploader": "Science Channel",
        "upload_date": "20240131",
        "duration": 212.0,
        "license": "Creative Commons Attribution license (reuse allowed)",
        "chapters": [],
        "tags": ["biology", "plants"],
        "original_language": "en",
        "manual": {"en": [TrackRef("vtt", "English")], "deu": [TrackRef("vtt", "Deutsch")]},
        "automatic": {"en-orig": [TrackRef("vtt", None)]},
        "track": track(),
    }
    values.update(fields)
    return MediaProbe(**values)


type Probe = MediaProbe | BaseException | Callable[[MediaTarget], Awaitable[MediaProbe]]


class FakeMediaFetcher:
    """Answers every probe with ``answer``; notes the calls."""

    def __init__(self, answer: Probe) -> None:
        self.answer = answer
        self.calls: list[tuple[MediaTarget, list[str], float]] = []
        self.call_seconds = 25.0

    async def probe(self, target: MediaTarget, languages: list[str], deadline: float) -> MediaProbe:
        self.calls.append((target, list(languages), deadline))
        if isinstance(self.answer, BaseException):
            raise self.answer
        if isinstance(self.answer, MediaProbe):
            return self.answer
        return await self.answer(target)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"auth_mode": "none", "outbound_min_delay_ms": 0}
    values.update(overrides)
    return Settings(**values)


type Handoff = Callable[[AsrNeeded, Envelope[ExtractResult]], Awaitable[Envelope[ExtractResult]]]


class Harness:
    def __init__(
        self,
        answers: dict[str, Answer] | None = None,
        media: Probe | None = None,
        *,
        fetcher: Fetcher | None = None,
        valkey: FakeAsyncRedis | None = None,
        **overrides: Any,
    ) -> None:
        self.registry = CollectorRegistry()
        self.fake = FakeFetcher(answers or {})
        self.valkey = valkey if valkey is not None else FakeAsyncRedis()
        self.settings = make_settings(**overrides)
        self.gate = RecordingGate(DomainGate(self.valkey, self.settings))
        if fetcher is None:
            self.fake.before_request = self.gate.before_request
            self.fake.on_request_sent = self.gate.mark_sent
        used = fetcher if fetcher is not None else cast(Fetcher, self.fake)
        self.media = FakeMediaFetcher(media) if media is not None else None
        self.pipeline = Pipeline(
            used,
            cast(DomainGate, self.gate),
            ComplianceChecker(used, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            self.registry,
            media_fetcher=cast(MediaFetcher | None, self.media),
        )

    async def arun(self, handoff: Handoff | None = None, **request: Any) -> Envelope[ExtractResult]:
        request.setdefault("url", YT)
        return await self.pipeline.run(ExtractRequest(**request), USER, asr_handoff=handoff)

    def run(self, handoff: Handoff | None = None, **request: Any) -> Envelope[ExtractResult]:
        return asyncio.run(self.arun(handoff, **request))

    def run_all(self, *requests: dict[str, Any]) -> list[Envelope[ExtractResult]]:
        async def all_of() -> list[Envelope[ExtractResult]]:
            return [await self.arun(**request) for request in requests]

        return asyncio.run(all_of())

    def probes(self) -> int:
        return len(self.media.calls) if self.media is not None else 0

    def fetched_urls(self) -> list[str]:
        return [url for url, _ in self.fake.calls]

    def keys(self, pattern: str) -> list[bytes]:
        async def scan() -> list[bytes]:
            return [key async for key in self.valkey.scan_iter(pattern)]

        return asyncio.run(scan())


def data_of(envelope: Envelope[ExtractResult]) -> ExtractResult:
    assert envelope.data is not None
    return envelope.data


def page_files(urls: list[str]) -> list[str]:
    """The fetched URLs that are not compliance files."""
    return [
        url
        for url in urls
        if not url.endswith(("/robots.txt", "/ai.txt", "/.well-known/tdmrep.json"))
    ]


# --- platform URLs ---------------------------------------------------------------------------


def test_youtube_with_manual_subtitles() -> None:
    harness = Harness(media=probe())

    envelope = harness.run(url=YT_SHORT)

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    assert data.url == YT_SHORT  # the request's
    assert data.final_url == YT  # the canonical URL
    assert data.mode_used == "fast"
    assert data.content_type == "text/vtt"
    assert data.content is not None
    assert data.content.startswith("# Photosynthesis for kids\n\nPlants use the light")
    assert data.title == "Photosynthesis for kids"
    assert data.language == "en"
    assert data.word_count > 50
    assert data.media is not None
    assert data.media.platform == "youtube"
    assert data.media.transcript_source == "manual_subtitles"
    assert data.media.transcript_language == "en"
    assert data.media.available_languages == ["en", "de"]
    assert data.media.duration_seconds == 212.0
    assert data.media.uploader == "Science Channel"
    assert data.metadata is not None
    assert data.metadata.license is not None
    assert data.metadata.license.id == "CC-BY-3.0"
    assert data.metadata.license.source == "platform"
    assert data.metadata.license.confidence == "high"
    assert data.metadata.description == "How plants eat light.\nA lesson for grade 5."
    assert data.metadata.authors == ["Science Channel"]
    assert data.metadata.published == "2024-01-31"
    assert data.metadata.keywords == ["biology", "plants"]
    assert data.metadata.canonical_url == YT
    assert data.fingerprint is not None
    assert data.attempts == 1
    assert data.compliance.decision == "allowed"
    # No page fetch: only the canonical origin's compliance files.
    assert page_files(harness.fetched_urls()) == []
    assert all(url.startswith("https://www.youtube.com/") for url in harness.fetched_urls())
    ((target, languages, _),) = harness.media.calls  # type: ignore[union-attr]
    assert target.canonical_url == YT
    assert languages == []


def test_formats_text_and_html_of_a_transcript() -> None:
    harness = Harness(media=probe(chapters=[Chapter(0.0, "Intro"), Chapter(9.0, "Leaves")]))

    text, html = harness.run_all({"format": "text"}, {"format": "html"})

    assert data_of(text).content is not None
    assert data_of(text).content.startswith("Photosynthesis for kids\nIntro\nPlants use")  # type: ignore[union-attr]
    assert "#" not in data_of(text).content  # type: ignore[operator]
    assert data_of(html).content is not None
    assert "<h1>Photosynthesis for kids</h1>" in data_of(html).content  # type: ignore[operator]
    assert "<h2>Leaves</h2>" in data_of(html).content  # type: ignore[operator]


def test_youtube_auto_captions_only() -> None:
    harness = Harness(media=probe(manual={}, track=track("auto_subtitles", "en-orig")))

    envelope = harness.run(languages=["en"])

    assert envelope.status == "success"
    media = data_of(envelope).media
    assert media is not None
    assert media.transcript_source == "auto_subtitles"
    assert media.transcript_language == "en"
    assert media.available_languages == []
    assert harness.media is not None
    assert harness.media.calls[0][1] == ["en"]


def test_no_subtitles_metadata_only() -> None:
    harness = Harness(media=probe(manual={}, automatic={}, track=None))

    first, second = harness.run_all({}, {})

    assert first.status == "partial"
    assert first.error is not None
    assert first.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    assert first.error.retryable is False
    assert "transcribe" not in first.error.message  # the service has no ASR
    data = data_of(first)
    assert (
        data.content
        == "# Photosynthesis for kids\n\nHow plants eat light.\n\nA lesson for grade 5."
    )
    assert data.content_type == "text/html"
    assert data.media is not None
    assert data.media.transcript_source == "metadata_only"
    assert data.media.transcript_language is None
    assert data.fingerprint is not None
    assert data_of(second).cache == "hit"
    assert harness.probes() == 1


def test_metadata_only_names_transcribe_when_the_service_has_asr() -> None:
    harness = Harness(media=probe(track=None), asr_enabled=True)

    envelope = harness.run()

    assert envelope.error is not None
    assert "transcribe: true" in envelope.error.message


LONG_DESCRIPTION = " ".join(f"Plants turn light into sugar, step {n}." for n in range(20))


@pytest.mark.parametrize(
    ("description", "asr_enabled"),
    [
        ("How plants eat light.\nA lesson for grade 5.", False),
        ("How plants eat light.\nA lesson for grade 5.", True),
        (LONG_DESCRIPTION, False),  # QUALITY_MIN_WORDS or more: no quality verdict either
    ],
    ids=["short", "transcribe-hint", "long-description"],
)
def test_a_cached_details_only_answer_is_the_fresh_one(description: str, asr_enabled: bool) -> None:
    """A details-only answer served from the cache has the media error of the fresh one
    (M6: partial with no_transcript_available), not a quality verdict."""
    harness = Harness(
        media=probe(manual={}, automatic={}, track=None, description=description),
        asr_enabled=asr_enabled,
    )

    first, second = harness.run_all({}, {})

    assert data_of(second).cache == "hit"
    assert first.error is not None
    assert first.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    assert (second.status, second.error) == (first.status, first.error)
    assert data_of(second).warnings == data_of(first).warnings


def test_blocked_gives_subtitles_blocked_warning() -> None:
    harness = Harness(
        media=probe(manual={}, automatic={}, track=None, blocked=True, error="blocked")
    )

    first, second = harness.run_all({}, {})

    assert first.status == "partial"
    assert first.error is not None
    assert first.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    data = data_of(first)
    assert "subtitles_blocked" in data.warnings
    assert data.content is not None
    assert "How plants eat light." in data.content
    # The block may lift: the answer is neither compared nor cached.
    assert data.fingerprint is None
    assert data_of(second).cache == "miss"
    assert harness.probes() == 2
    assert harness.keys("fp:*") == []


def test_blocked_without_any_metadata_fails_retryable() -> None:
    harness = Harness(media=MediaProbe(blocked=True, error="blocked"))

    envelope = harness.run()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    assert envelope.error.retryable is True
    assert "subtitles_blocked" in data_of(envelope).warnings
    # A block is no reason to fetch the watch page as well.
    assert page_files(harness.fetched_urls()) == []


def test_a_youtube_429_reaches_the_gates_backoff() -> None:
    """``rate_limited`` from the children is reported to the gate as the fast fetcher reports a
    429 (``Retry-After`` included): the host pauses, and the next request is refused by the gate
    without a child call."""
    limited = MediaProbe(blocked=True, error="rate_limited", rate_limited=True, retry_after="120")
    harness = Harness(media=limited)

    first, second = harness.run_all({}, {})

    assert first.status == "failed"
    assert first.error is not None
    assert first.error.code == ErrorCode.RATE_LIMITED
    assert first.error.retryable is True
    assert harness.keys("out:cooldown:*") == [b"out:cooldown:{www.youtube.com}"]
    assert ("record", "www.youtube.com") in harness.gate.events
    assert second.error is not None
    assert second.error.code == ErrorCode.CONNECTION_ERROR  # paused
    assert harness.probes() == 1
    assert page_files(harness.fetched_urls()) == []  # no watch page after a 429


def test_rate_limited_with_metadata_answers_it_with_subtitles_blocked() -> None:
    harness = Harness(
        media=probe(track=None, blocked=True, error="rate_limited", rate_limited=True)
    )

    envelope = harness.run()

    assert envelope.status == "partial"
    assert "subtitles_blocked" in data_of(envelope).warnings
    assert harness.keys("out:cooldown:*") == [b"out:cooldown:{www.youtube.com}"]


WATCH_PAGE = """<!doctype html><html><head><title>x - YouTube</title></head><body>
<script>var ytInitialPlayerResponse = {"videoDetails": {"videoId": "dQw4w9WgXcQ",
"title": "Leaves and light", "shortDescription": "Why leaves are green.\\nWith a quiz.",
"author": "Biology Class", "lengthSeconds": "95", "keywords": ["leaves"], "isLive": false},
"microformat": {"playerMicroformatRenderer": {"publishDate": "2023-05-04T01:02:03-07:00"}}};
var other = {"a": "};"};</script></body></html>"""


def test_youtube_metadata_from_watch_page_when_libraries_fail() -> None:
    """M2's last tier 2: both libraries failed without a block, so the watch page's
    ``ytInitialPlayerResponse`` gives title and description (fast fetcher, in the turn)."""
    harness = Harness(
        {YT: fetched(YT, WATCH_PAGE)},
        media=MediaProbe(error="extractor_error"),
    )

    envelope = harness.run()

    assert envelope.status == "partial"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    data = data_of(envelope)
    assert data.title == "Leaves and light"
    assert data.content == "# Leaves and light\n\nWhy leaves are green.\n\nWith a quiz."
    assert data.media is not None
    assert data.media.transcript_source == "metadata_only"
    assert data.media.duration_seconds == 95.0
    assert data.media.uploader == "Biology Class"
    assert data.metadata is not None
    assert data.metadata.published == "2023-05-04"
    assert data.attempts == 2  # the probe and the watch page
    assert page_files(harness.fetched_urls()) == [YT]
    assert harness.gate.held() == []


def test_a_watch_page_without_player_data_fails_without_transcript() -> None:
    harness = Harness({YT: fetched(YT, ARTICLE)}, media=MediaProbe(error="extractor_error"))

    envelope = harness.run()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE


def test_robots_disallow_on_platform_enforced() -> None:
    robots = fetched(YT_ROBOTS, "User-agent: *\nDisallow: /watch\n", content_type="text/plain")
    harness = Harness({YT_ROBOTS: robots}, media=probe())

    envelope = harness.run(url=YT_SHORT, compliance="enforce")

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.BLOCKED_BY_ROBOTS
    assert harness.probes() == 0


def test_platform_holds_turn_during_probe() -> None:
    harness: Harness

    async def while_held(target: MediaTarget) -> MediaProbe:
        assert harness.gate.held() == ["www.youtube.com"]
        return probe()

    harness = Harness(media=while_held)

    envelope = harness.run(url=YT_SHORT)

    assert envelope.status == "success"
    assert harness.gate.held() == []  # given back
    assert harness.probes() == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLabc_123",
        "https://www.youtube.com/embed/videoseries?list=PLabc_123",
    ],
)
def test_playlist_unsupported(url: str) -> None:
    harness = Harness(media=probe())

    envelope = harness.run(url=url)

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert "one by one" in envelope.error.message
    assert harness.probes() == 0
    assert harness.fetched_urls() == []


def test_live_unsupported() -> None:
    harness = Harness(media=probe(track=None, is_live=True))

    envelope = harness.run()

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert "live" in envelope.error.message


@pytest.mark.parametrize(
    "request_options",
    [{"mode": "browser"}, {"headers": {"X-Test": "1"}}, {"cookies": [{"name": "a", "value": "b"}]}],
)
def test_mode_browser_skips_media_path(request_options: dict[str, Any]) -> None:
    harness = Harness({YT: fetched(YT, ARTICLE)}, media=probe())

    envelope = harness.run(**request_options)

    assert harness.probes() == 0
    assert page_files(harness.fetched_urls()) == [YT]
    assert data_of(envelope).media is None


def test_without_media_a_platform_url_is_a_page() -> None:
    harness = Harness({YT: fetched(YT, ARTICLE)})

    envelope = harness.run()

    assert envelope.status == "success"
    assert data_of(envelope).media is None
    assert page_files(harness.fetched_urls()) == [YT]


def test_cache_key_has_media() -> None:
    valkey = FakeAsyncRedis()
    page = Harness({YT: fetched(YT, ARTICLE)}, valkey=valkey)
    assert data_of(page.run()).cache == "miss"  # a page result of the YouTube URL is stored

    media = Harness(media=probe(), valkey=valkey)
    envelope = media.run()

    assert data_of(envelope).cache == "miss"
    assert data_of(envelope).media is not None
    assert media.probes() == 1


def test_the_cache_and_the_fingerprint_use_the_canonical_url() -> None:
    harness = Harness(media=probe())

    _, second = harness.run_all({"url": YT}, {"url": YT_SHORT})

    assert data_of(second).cache == "hit"
    assert data_of(second).url == YT_SHORT
    assert harness.probes() == 1
    assert harness.keys("fp:*") == [_store_key(YT, "markdown|media").encode()]


def test_languages_are_part_of_the_cache_key() -> None:
    harness = Harness(media=probe())

    _, second, third = harness.run_all({}, {"languages": ["de"]}, {"languages": ["de"]})

    assert data_of(second).cache == "miss"
    assert data_of(third).cache == "hit"


def test_metadata_only_cache_not_hit_for_transcribe() -> None:
    handed: list[tuple[AsrNeeded, Envelope[ExtractResult]]] = []

    async def handoff(
        needed: AsrNeeded, stand_in: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append((needed, stand_in))
        return stand_in

    harness = Harness(media=probe(track=None))

    async def scenario() -> list[Envelope[ExtractResult]]:
        first = await harness.arun()
        second = await harness.arun(handoff, transcribe=True)
        third = await harness.arun()
        return [first, second, third]

    first, _, third = asyncio.run(scenario())

    assert data_of(first).cache == "miss"
    assert harness.probes() == 2  # the stored metadata_only result is no hit for transcribe
    ((needed, stand_in),) = handed
    assert needed.input == MediaInputMeta(
        source="platform",
        platform="youtube",
        url=YT,
        extractor="youtube",
        kind=None,
        file_name=None,
        duration=212.0,
        languages=[],
        compliance=data_of(first).compliance.model_copy(),
        attempts=1,
    )
    assert needed.draft is not None
    assert data_of(needed.draft).media is not None
    assert data_of(needed.draft).media.transcript_source == "metadata_only"  # type: ignore[union-attr]
    assert data_of(stand_in).fingerprint is None  # the stand-in is never stored
    assert data_of(third).cache == "hit"  # the plain request still gets the stored result


def test_transcribe_without_a_handoff_answers_the_draft_with_asr_unavailable() -> None:
    harness = Harness(media=probe(track=None))

    first, second = harness.run_all({"transcribe": True}, {})

    assert first.status == "partial"
    assert "asr_unavailable" in data_of(first).warnings
    assert data_of(first).fingerprint is None
    assert data_of(second).cache == "miss"


def test_transcribe_with_subtitles_needs_no_asr() -> None:
    handed: list[AsrNeeded] = []

    async def handoff(
        needed: AsrNeeded, stand_in: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        return stand_in

    harness = Harness(media=probe())

    envelope = harness.run(handoff, transcribe=True)

    assert envelope.status == "success"
    assert handed == []


def test_a_probe_past_the_deadline_is_a_retryable_timeout() -> None:
    harness = Harness(media=ConversionTimeout())

    envelope = harness.run()

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.TIMEOUT
    assert envelope.error.retryable is True


def test_the_probe_gets_the_extractions_deadline() -> None:
    harness = Harness(media=probe(), extract_timeout_seconds=12)

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        await harness.arun()
        return started

    started = asyncio.run(scenario())

    assert harness.media is not None
    deadline = harness.media.calls[0][2]
    assert started < deadline <= started + 12


def test_user_information_is_refused_and_a_port_is_dropped() -> None:
    """A platform URL with user information is ``invalid_url`` before anything happens; one
    with a port is probed and checked at the canonical URL, which carries neither."""
    harness = Harness(media=probe())

    refused = harness.run(url=f"https://user:secret@www.youtube.com/watch?v={YT_ID}")
    ported = harness.run(url=f"https://www.youtube.com:8443/watch?v={YT_ID}")

    assert refused.error is not None
    assert refused.error.code == ErrorCode.INVALID_URL
    assert harness.probes() == 1  # the second request only
    assert harness.media is not None
    assert harness.media.calls[0][0].canonical_url == YT
    assert data_of(ported).final_url == YT
    assert "secret" not in json.dumps(data_of(ported).model_dump(mode="json"))
    assert all(url.startswith("https://www.youtube.com/") for url in harness.fetched_urls())


def test_media_logs_carry_no_url_or_video_id() -> None:
    harness = Harness(media=probe(track=None))

    with capture_logs() as logs:
        harness.run(url=YT_SHORT)

    text = json.dumps(logs, default=str)
    assert YT_ID not in text
    assert "t=30" not in text
    assert "watch" not in text


def test_a_media_result_never_escalates() -> None:
    harness = Harness(media=probe(track=None, title="404 not found", description=None))

    envelope = harness.run(mode="auto")

    assert not needs_browser(envelope, unblock=True)


def test_media_metrics() -> None:
    harness = Harness(media=probe())

    harness.run()

    value = harness.registry.get_sample_value(
        "text_extraction_media_extractions_total",
        {"platform": "youtube", "source": "manual_subtitles", "outcome": "success"},
    )
    assert value == 1.0


# --- media files ------------------------------------------------------------------------------


def test_media_file_without_transcribe() -> None:
    harness = Harness({FILE: fetched(FILE, MP3, content_type="audio/mpeg")}, media=probe())

    envelope = harness.run(url=FILE)

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.NO_TRANSCRIPT_AVAILABLE
    assert envelope.error.retryable is False
    data = data_of(envelope)
    assert data.content_type == "audio/mpeg"
    assert data.media is not None
    assert data.media.platform == "file"
    assert harness.probes() == 0


def loopback_or_public(address: IPv4Address | IPv6Address) -> bool:
    return address.is_loopback or is_public_address(address)


def serve_compliance_files(httpserver: HTTPServer) -> None:
    for path in ("/robots.txt", "/ai.txt", "/.well-known/tdmrep.json"):
        httpserver.expect_request(path).respond_with_data("", status=404)


def test_media_file_with_transcribe_returns_asr_needed(httpserver: HTTPServer) -> None:
    """A large file declared as audio is read only to its head (64 KiB) and sniffed; with
    ``transcribe`` it goes to the handoff as an ``AsrNeeded`` without a draft."""
    serve_compliance_files(httpserver)
    body = MP3 + b"\x00" * 300_000
    httpserver.expect_request("/talk.mp3").respond_with_data(body, content_type="audio/mpeg")
    url = httpserver.url_for("/talk.mp3")
    handed: list[tuple[AsrNeeded, Envelope[ExtractResult]]] = []

    async def handoff(
        needed: AsrNeeded, stand_in: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append((needed, stand_in))
        return stand_in

    settings = make_settings()

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings, policy=loopback_or_public)
        try:
            harness = Harness(media=probe(), fetcher=fetcher)
            return await harness.arun(handoff, url=url, transcribe=True, languages=["de"])
        finally:
            await fetcher.aclose()

    envelope = asyncio.run(scenario())

    ((needed, stand_in),) = handed
    assert needed.draft is None
    assert needed.input.source == "file"
    assert needed.input.platform == "file"
    assert needed.input.kind == "mp3"
    assert needed.input.url == url
    assert needed.input.extractor is None
    assert needed.input.languages == ["de"]
    assert needed.input.attempts == 1
    assert needed.input.compliance is not None
    assert envelope is stand_in
    assert data_of(stand_in).content_type == "audio/mpeg"


def test_a_media_file_is_unsupported_while_media_is_off() -> None:
    harness = Harness({FILE: fetched(FILE, MP3, content_type="audio/mpeg")})

    envelope = harness.run(url=FILE)

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


def test_a_truncated_body_that_is_no_media_is_not_converted(httpserver: HTTPServer) -> None:
    serve_compliance_files(httpserver)
    body = ("<html><body><p>" + PARAGRAPH * 1000 + "</p></body></html>").encode()
    httpserver.expect_request("/fake.mp3").respond_with_data(body, content_type="audio/mpeg")
    settings = make_settings()

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings, policy=loopback_or_public)
        try:
            harness = Harness(media=probe(), fetcher=fetcher)
            return await harness.arun(url=httpserver.url_for("/fake.mp3"))
        finally:
            await fetcher.aclose()

    envelope = asyncio.run(scenario())

    assert envelope.error is not None
    assert envelope.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


# --- HTML5 tracks and embeds ---------------------------------------------------------------------


def lesson(players: str, *, head: str = "") -> str:
    return (
        f'<!doctype html><html lang="en"><head><title>Lesson</title>{head}</head><body>'
        f"<article><h1>Lesson</h1><p>{PARAGRAPH * 2}</p>{players}</article></body></html>"
    )


def track_vtt(words: str) -> str:
    return f"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n{words}\n"


def test_html5_track_appended(httpserver: HTTPServer) -> None:
    serve_compliance_files(httpserver)
    players = (
        '<video src="a.mp4"><track kind="subtitles" src="a.de.vtt" srclang="de" label="Deutsch">'
        '<track kind="subtitles" src="a.en.vtt" srclang="en" label="English"></video>'
        '<video src="b.mp4"><track src="b.vtt" srclang="en"></video>'
    )
    httpserver.expect_request("/lesson").respond_with_data(
        lesson(players), content_type="text/html"
    )
    httpserver.expect_request("/a.en.vtt").respond_with_data(
        track_vtt("Leaves are green because of chlorophyll."), content_type="text/vtt"
    )
    httpserver.expect_request("/b.vtt").respond_with_data(
        track_vtt("Roots take up water."), content_type="text/vtt"
    )
    settings = make_settings()

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings, policy=loopback_or_public)
        try:
            harness = Harness(media=probe(), fetcher=fetcher)
            return await harness.arun(url=httpserver.url_for("/lesson"))
        finally:
            await fetcher.aclose()

    envelope = asyncio.run(scenario())

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.content is not None
    page_part, first, second = data.content.split("\n\n## Transcript")
    assert "Photosynthesis is the process" in page_part
    assert first == " (English)\n\nLeaves are green because of chlorophyll."
    assert second == "\n\nRoots take up water."
    assert data.media is not None
    assert data.media.platform == "html5"
    assert data.media.transcript_source == "manual_subtitles"
    assert data.media.transcript_language == "en"
    assert data.media.available_languages == ["de", "en"]
    assert data.media.embeds == [
        MediaEmbed(url=httpserver.url_for("/a.mp4"), kind="file"),
        MediaEmbed(url=httpserver.url_for("/b.mp4"), kind="file"),
    ]
    assert data.quality is not None
    assert data.word_count == data.quality.word_count
    assert "chlorophyll" in data.content  # counted as well
    assert data.fingerprint is not None
    paths = [request.path for request, _ in httpserver.log]
    assert "/a.de.vtt" not in paths  # one track per player, by the page language


def test_track_to_private_address_skipped(httpserver: HTTPServer) -> None:
    serve_compliance_files(httpserver)
    players = (
        '<video><track src="http://169.254.169.254/latest/meta-data.vtt" srclang="en"></video>'
    )
    httpserver.expect_request("/lesson").respond_with_data(
        lesson(players), content_type="text/html"
    )
    settings = make_settings()

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings, policy=loopback_or_public)
        try:
            harness = Harness(media=probe(), fetcher=fetcher)
            return await harness.arun(url=httpserver.url_for("/lesson"))
        finally:
            await fetcher.aclose()

    envelope = asyncio.run(scenario())

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.content is not None
    assert "## Transcript" not in data.content
    assert "tracks_failed" in data.warnings
    assert data.media is not None
    assert data.media.transcript_source is None


def test_more_players_than_the_limit_are_truncated() -> None:
    players = "".join(
        f'<video><track src="/t{index}.vtt" srclang="en"></video>' for index in range(3)
    )
    answers: dict[str, Answer] = {PAGE: fetched(PAGE, lesson(players))}
    for index in range(3):
        url = f"https://example.org/t{index}.vtt"
        answers[url] = fetched(url, track_vtt(f"Track number {index}."), content_type="text/vtt")
    harness = Harness(answers, media=probe(), media_max_tracks=2)

    envelope = harness.run(url=PAGE)

    data = data_of(envelope)
    assert data.content is not None
    assert data.content.count("## Transcript") == 2
    assert "tracks_truncated" in data.warnings
    assert "https://example.org/t2.vtt" not in harness.fetched_urls()


def test_embeds_listed() -> None:
    ld = {
        "@context": "https://schema.org",
        "@type": "PodcastEpisode",
        "associatedMedia": {"@type": "AudioObject", "contentUrl": "https://cdn.example/ep1.mp3"},
    }
    players = (
        f'<iframe src="https://www.youtube.com/embed/{YT_ID}?rel=0"></iframe>'
        '<audio controls src="https://cdn.example/intro.mp3"></audio>'
    )
    head = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    harness = Harness({PAGE: fetched(PAGE, lesson(players, head=head))}, media=probe())

    envelope = harness.run(url=PAGE)

    assert envelope.status == "success"
    media = data_of(envelope).media
    assert media is not None
    assert media.platform == "html5"
    assert media.transcript_source is None
    assert media.embeds == [
        MediaEmbed(url=YT, kind="platform", platform="youtube"),
        MediaEmbed(url="https://cdn.example/intro.mp3", kind="file"),
        MediaEmbed(url="https://cdn.example/ep1.mp3", kind="file"),
    ]
    assert harness.probes() == 0  # embeds are listed, never extracted (D4 a)
    assert page_files(harness.fetched_urls()) == [PAGE]


def test_a_page_that_escalates_fetches_no_track() -> None:
    shell = (
        '<html><body><div id="root"></div><script src="/app.js"></script>'
        '<video><track src="/t.vtt" srclang="en"></video></body></html>'
    )
    harness = Harness({PAGE: fetched(PAGE, shell)}, media=probe(), browser_enabled=True)

    harness.run(url=PAGE, mode="auto")

    assert "https://example.org/t.vtt" not in harness.fetched_urls()


def test_page_answer_unchanged_without_media() -> None:
    """The package-4/5 answers of an HTML page and a text page are unchanged with media on
    (apart from the timings; ``media: null`` is in the recording since Task 2)."""
    golden = json.loads(GOLDEN.read_text())
    cases: dict[str, tuple[Answer, dict[str, Any]]] = {
        "article": (fetched(), {"links": True}),
        "plain": (
            fetched(body=PARAGRAPH * 5, content_type="text/plain"),
            {"format": "html", "links": True},
        ),
    }
    for name, (answer, request) in cases.items():
        harness = Harness({PAGE: answer}, media=probe())

        envelope = harness.run(url=PAGE, **request)

        dumped = envelope.model_dump(mode="json")
        dumped["data"].pop("timings")
        dumped.pop("trace_id")
        assert dumped == golden[name], name
        assert harness.probes() == 0


# --- the API ---------------------------------------------------------------------------------


def api_client(**overrides: Any) -> TestClient:
    from text_extraction.app import create_public_app
    from text_extraction.pipeline import create_pipeline

    settings = make_settings(**overrides)
    registry = CollectorRegistry()
    app = create_public_app(settings, FakeAsyncRedis(), registry)
    fake = FakeFetcher({PAGE: fetched(body=ARTICLE)})
    app.state.pipeline = create_pipeline(settings, app.state.valkey, cast(Fetcher, fake), registry)
    return TestClient(app)


@pytest.mark.parametrize(("asr", "jobs"), [(False, True), (True, False), (False, False)])
def test_transcribe_400_while_asr_off(asr: bool, jobs: bool) -> None:
    with api_client(asr_enabled=asr, jobs_enabled=jobs) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "transcribe": True})
        batch = client.post("/v1/batch", json={"urls": [PAGE], "transcribe": True})
        plain = client.post("/v1/extract", json={"url": PAGE, "transcribe": False})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"] == "Transcription is not enabled on this service."
    assert batch.status_code in (400, 503)  # 503: jobs disabled answers first
    if jobs:
        assert batch.status_code == 400
        assert batch.json()["error"]["message"] == body["error"]["message"]
    assert plain.status_code == 200


def test_transcribe_is_accepted_while_asr_is_on() -> None:
    with api_client(asr_enabled=True) as client:
        response = client.post("/v1/extract", json={"url": PAGE, "transcribe": True})

    assert response.status_code == 200


def test_transcribe_defaults_wait_seconds_to_20(monkeypatch: pytest.MonkeyPatch) -> None:
    from text_extraction import extract_api

    waited: list[float] = []
    wait_for_flow = extract_api.wait_for_flow

    async def recording(task: asyncio.Task[Any], seconds: float) -> None:
        waited.append(seconds)
        await wait_for_flow(task, seconds)

    monkeypatch.setattr(extract_api, "wait_for_flow", recording)
    with api_client(asr_enabled=True) as client:
        client.post("/v1/extract", json={"url": PAGE, "transcribe": True})
        client.post("/v1/extract", json={"url": PAGE, "transcribe": True, "wait_seconds": 5})
        client.post("/v1/extract", json={"url": PAGE})

    assert waited == [20, 5]


# --- fix round 1 -----------------------------------------------------------------------------

VIMEO = "https://player.vimeo.com/video/76979871"
NO_SUBTITLES = "has no subtitles"


# (probe error, with metadata) -> (status, code, retryable, warning)
PROBE_FAILURES: list[tuple[str | None, bool, str, ErrorCode, bool, str | None]] = [
    (None, True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, False, None),
    (None, False, "failed", ErrorCode.NO_TRANSCRIPT_AVAILABLE, False, None),
    (
        "extractor_error",
        True,
        "partial",
        ErrorCode.NO_TRANSCRIPT_AVAILABLE,
        True,
        "subtitles_failed",
    ),
    ("extractor_error", False, "failed", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, None),
    ("crashed", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "subtitles_failed"),
    ("crashed", False, "failed", ErrorCode.INTERNAL_ERROR, True, None),
    ("out_of_memory", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "subtitles_failed"),
    ("out_of_memory", False, "failed", ErrorCode.INTERNAL_ERROR, True, None),
    ("connect_error", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "subtitles_failed"),
    ("connect_error", False, "failed", ErrorCode.CONNECTION_ERROR, True, None),
    ("dns_error", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "subtitles_failed"),
    ("dns_error", False, "failed", ErrorCode.DNS_ERROR, True, None),
    ("track_failed", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "subtitles_failed"),
    ("track_failed", False, "failed", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, None),
    ("empty_track", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, "subtitles_failed"),
    ("empty_track", False, "failed", ErrorCode.NO_TRANSCRIPT_AVAILABLE, True, None),
    ("too_large", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, False, "subtitles_failed"),
    ("too_large", False, "failed", ErrorCode.TOO_LARGE, False, None),
    ("bytes_cap", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, False, "subtitles_failed"),
    ("bytes_cap", False, "failed", ErrorCode.TOO_LARGE, False, None),
    ("unavailable", True, "partial", ErrorCode.NO_TRANSCRIPT_AVAILABLE, False, None),
    ("unavailable", False, "failed", ErrorCode.NO_TRANSCRIPT_AVAILABLE, False, None),
    ("blocked_address", True, "failed", ErrorCode.BLOCKED_URL, False, None),
    ("blocked_address", False, "failed", ErrorCode.BLOCKED_URL, False, None),
]


@pytest.mark.parametrize(
    ("error", "with_metadata", "status", "code", "retryable", "warning"), PROBE_FAILURES
)
def test_a_failed_probe_is_reported_as_what_it_is(
    error: str | None,
    with_metadata: bool,
    status: str,
    code: ErrorCode,
    retryable: bool,
    warning: str | None,
) -> None:
    """Only a probe that succeeded and listed no usable track "has no subtitles", and only that
    answer is fingerprinted and cached; a failure of the children, the network or the track
    says so, and is extracted again next time."""
    answer = (
        probe(manual={}, automatic={}, track=None, error=error)
        if with_metadata
        else MediaProbe(error=error)
    )
    harness = Harness(media=answer)

    first, second = harness.run_all({"url": VIMEO}, {"url": VIMEO})

    assert first.status == status
    assert first.error is not None
    assert first.error.code == code
    assert first.error.retryable is retryable
    assert "refused" not in first.error.message
    assert (NO_SUBTITLES in first.error.message) is (error is None)
    data = data_of(first)
    assert "subtitles_blocked" not in data.warnings
    if warning is not None:
        assert warning in data.warnings
    cached = error is None and with_metadata
    assert (data.fingerprint is not None) is cached
    assert data_of(second).cache == ("hit" if cached else "miss")


def test_an_unreadable_subtitle_track_is_no_video_without_subtitles() -> None:
    harness = Harness(media=probe(track=track(body=b"\x00\x01 not a caption file", fmt="ttml")))

    first, second = harness.run_all({}, {})

    assert first.status == "partial"
    assert first.error is not None
    assert NO_SUBTITLES not in first.error.message
    assert "subtitles_failed" in data_of(first).warnings
    assert data_of(first).fingerprint is None
    assert data_of(second).cache == "miss"


def bilingual_lesson() -> dict[str, Answer]:
    players = (
        '<video src="/clip.mp4">'
        '<track src="/clip.en.vtt" srclang="en" label="English">'
        '<track src="/clip.de.vtt" srclang="de" label="Deutsch"></video>'
    )
    en = "https://example.org/clip.en.vtt"
    de = "https://example.org/clip.de.vtt"
    return {
        PAGE: fetched(PAGE, lesson(players)),
        en: fetched(en, track_vtt("Leaves are green."), content_type="text/vtt"),
        de: fetched(de, track_vtt("Blätter sind grün."), content_type="text/vtt"),
    }


def test_the_page_cache_key_has_the_languages_that_chose_a_track() -> None:
    harness = Harness(bilingual_lesson(), media=probe())

    english, german, again, plain = harness.run_all(
        {"url": PAGE, "languages": ["en"]},
        {"url": PAGE, "languages": ["de"]},
        {"url": PAGE, "languages": ["de"]},
        {"url": PAGE},
    )

    assert "Leaves are green." in (data_of(english).content or "")
    assert data_of(german).cache == "miss"
    assert "Blätter sind grün." in (data_of(german).content or "")
    assert data_of(again).cache == "hit"
    assert data_of(plain).cache == "miss"  # the default key is the one without languages


def test_switching_languages_on_a_page_with_tracks_is_no_content_change() -> None:
    harness = Harness(bilingual_lesson(), media=probe())

    _, german, plain, plain_again = harness.run_all(
        {"url": PAGE, "languages": ["en"], "cache": False},
        {"url": PAGE, "languages": ["de"], "cache": False},
        {"url": PAGE, "cache": False},
        {"url": PAGE, "cache": False},
    )

    assert data_of(german).fingerprint is not None
    assert data_of(german).fingerprint.changed is None  # type: ignore[union-attr]
    assert data_of(plain).fingerprint.changed is None  # type: ignore[union-attr]
    assert data_of(plain_again).fingerprint.changed is False  # type: ignore[union-attr]
    # A request without languages keeps the variant of package 4.
    assert _store_key(PAGE, "markdown") in {key.decode() for key in harness.keys("fp:*")}


def test_waiting_for_a_tracks_turn_never_outlives_the_deadline() -> None:
    """A busy track host is skipped once the time is up: the page still answers (M8)."""
    players = '<video><track src="https://media.example/t.vtt" srclang="en"></video>'
    harness = Harness(
        {PAGE: fetched(PAGE, lesson(players))}, media=probe(), extract_timeout_seconds=3
    )
    slot = harness.gate.slot

    @contextlib.asynccontextmanager
    async def busy(host: str, crawl_delay: float | None = None) -> AsyncIterator[None]:
        if host == "media.example":
            await asyncio.sleep(60)
        async with slot(host, crawl_delay):
            yield

    harness.gate.slot = busy  # type: ignore[method-assign]

    envelope = harness.run(url=PAGE)

    assert envelope.status == "success", envelope.error
    assert "tracks_failed" in data_of(envelope).warnings
    assert "https://media.example/t.vtt" not in harness.fetched_urls()


def test_a_track_on_the_page_host_keeps_its_crawl_delay() -> None:
    robots = "https://example.org/robots.txt"
    players = '<video><track src="/t.vtt" srclang="en"></video>'
    answers: dict[str, Answer] = {
        robots: fetched(robots, "User-agent: *\nCrawl-delay: 1\n", content_type="text/plain"),
        PAGE: fetched(PAGE, lesson(players)),
        "https://example.org/t.vtt": fetched(
            "https://example.org/t.vtt", track_vtt("Roots take up water."), content_type="text/vtt"
        ),
    }
    harness = Harness(answers, media=probe())

    envelope = harness.run(url=PAGE)

    assert "Roots take up water." in (data_of(envelope).content or "")
    assert harness.gate.crawl_delays[-1] == ("example.org", 1.0)


def test_a_track_that_redirects_to_another_host_is_skipped() -> None:
    players = '<video><track src="/t.vtt" srclang="en"></video>'
    elsewhere = "https://other.example/t.vtt"
    answers: dict[str, Answer] = {
        PAGE: fetched(PAGE, lesson(players)),
        "https://example.org/t.vtt": Redirect(elsewhere),
        elsewhere: fetched(elsewhere, track_vtt("Not ours."), content_type="text/vtt"),
    }
    harness = Harness(answers, media=probe())

    envelope = harness.run(url=PAGE)

    data = data_of(envelope)
    assert "Not ours." not in (data.content or "")
    assert "tracks_failed" in data.warnings


def test_a_track_over_the_subtitle_limit_is_skipped(httpserver: HTTPServer) -> None:
    serve_compliance_files(httpserver)
    players = '<video><track src="/big.vtt" srclang="en"></video>'
    httpserver.expect_request("/lesson").respond_with_data(
        lesson(players), content_type="text/html"
    )
    big = track_vtt("Word " * 400)
    assert len(big) > 1024
    httpserver.expect_request("/big.vtt").respond_with_data(big, content_type="text/vtt")
    settings = make_settings(media_subtitle_max_bytes=1024)

    async def scenario() -> Envelope[ExtractResult]:
        fetcher = Fetcher(settings, policy=loopback_or_public)
        try:
            harness = Harness(media=probe(), fetcher=fetcher, media_subtitle_max_bytes=1024)
            return await harness.arun(url=httpserver.url_for("/lesson"))
        finally:
            await fetcher.aclose()

    envelope = asyncio.run(scenario())

    assert envelope.status == "success"
    data = data_of(envelope)
    assert "## Transcript" not in (data.content or "")
    assert "tracks_failed" in data.warnings


def test_the_api_pods_media_calls_fit_its_pipelines_deadline() -> None:
    from text_extraction.app import create_public_app

    app = create_public_app(
        make_settings(extract_timeout_seconds=17), FakeAsyncRedis(), CollectorRegistry()
    )

    assert app.state.media_fetcher is not None
    assert app.state.media_fetcher.call_seconds == 17


# --- ZDF and 3sat articles without a video ---------------------------------------------------

ZDF_ARTICLE = "https://www.zdf.de/nachrichten/politik/photosynthese-100.html"


def comparable(envelope: Envelope[ExtractResult]) -> dict[str, Any]:
    dumped = envelope.model_dump(mode="json")
    dumped.pop("trace_id")
    dumped["data"].pop("timings")
    dumped["data"].pop("attempts")
    return dumped


@pytest.mark.parametrize("error", ["extractor_error", "unavailable", None])
def test_zdf_article_without_a_video_is_extracted_as_a_page(error: str | None) -> None:
    """A ZDF ``.html`` URL is a target, but the probe finds no video on a news article: the
    request goes on as a page, in the same request, and answers what the page path answers."""
    page_answer = Harness({ZDF_ARTICLE: fetched(ZDF_ARTICLE, ARTICLE)}).run(url=ZDF_ARTICLE)
    harness = Harness({ZDF_ARTICLE: fetched(ZDF_ARTICLE, ARTICLE)}, media=MediaProbe(error=error))

    first, second = harness.run_all({"url": ZDF_ARTICLE}, {"url": ZDF_ARTICLE})

    assert first.status == "success", first.error
    assert comparable(first) == comparable(page_answer)
    assert data_of(first).media is None
    assert data_of(first).attempts == 2  # the probe read the page, then the fetch
    assert page_files(harness.fetched_urls()) == [ZDF_ARTICLE]
    # The page is stored as a page; the next request is served from it without a probe.
    assert data_of(second).cache == "hit"
    assert data_of(second).media is None
    assert harness.probes() == 1
    assert harness.gate.held() == []


def test_a_3sat_article_with_transcribe_is_a_page_without_a_handoff() -> None:
    url = "https://www.3sat.de/wissen/nano/artikel-100.html"
    handed: list[AsrNeeded] = []

    async def handoff(
        needed: AsrNeeded, stand_in: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        return stand_in

    harness = Harness({url: fetched(url, ARTICLE)}, media=MediaProbe(error="extractor_error"))

    envelope = harness.run(handoff, url=url, transcribe=True)

    assert envelope.status == "success"
    assert data_of(envelope).media is None
    assert handed == []


@pytest.mark.parametrize(
    "answer",
    [
        MediaProbe(blocked=True, error="blocked"),
        MediaProbe(blocked=True, error="rate_limited", rate_limited=True),
        MediaProbe(error="blocked_address"),
        MediaProbe(error="crashed"),
        MediaProbe(error="dns_error"),
    ],
)
def test_a_refusal_or_a_failure_on_a_zdf_page_is_no_reason_to_fall_back(answer: MediaProbe) -> None:
    harness = Harness({ZDF_ARTICLE: fetched(ZDF_ARTICLE, ARTICLE)}, media=answer)

    envelope = harness.run(url=ZDF_ARTICLE)

    assert envelope.status == "failed"
    assert page_files(harness.fetched_urls()) == []


def test_a_zdf_video_page_still_answers_its_subtitles() -> None:
    harness = Harness({ZDF_ARTICLE: fetched(ZDF_ARTICLE, ARTICLE)}, media=probe())

    envelope = harness.run(url=ZDF_ARTICLE)

    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.media is not None
    assert data.media.platform == "zdf"
    assert data.media.transcript_source == "manual_subtitles"
    assert page_files(harness.fetched_urls()) == []


def test_vimeo_without_a_video_never_falls_back_to_the_page() -> None:
    harness = Harness({VIMEO: fetched(VIMEO, ARTICLE)}, media=MediaProbe(error="extractor_error"))

    envelope = harness.run(url=VIMEO)

    assert envelope.status == "failed"
    assert page_files(harness.fetched_urls()) == []


# --- transcriptions that cannot be, and cached ASR failures --------------------------------


def capture_handoff() -> tuple[list[AsrNeeded], Handoff]:
    handed: list[AsrNeeded] = []

    async def handoff(
        needed: AsrNeeded, stand_in: Envelope[ExtractResult]
    ) -> Envelope[ExtractResult]:
        handed.append(needed)
        return stand_in

    return handed, handoff


def test_a_video_over_the_duration_limit_answers_media_too_long_without_a_job() -> None:
    """With ``transcribe: true``, no usable track and a duration over
    ``MEDIA_MAX_DURATION_SECONDS``, the answer is ``media_too_long`` with the video's details at
    once; nothing is handed to the speech recognition."""
    handed, handoff = capture_handoff()
    harness = Harness(media=probe(track=None, duration=7300.0), asr_enabled=True)

    async def scenario() -> list[Envelope[ExtractResult]]:
        first = await harness.arun(handoff, transcribe=True)
        again = await harness.arun(handoff, transcribe=True)
        plain = await harness.arun()
        return [first, again, plain]

    first, again, plain = asyncio.run(scenario())

    assert handed == []
    assert first.status == "partial"
    assert first.error is not None
    assert first.error.code == ErrorCode.MEDIA_TOO_LONG
    assert first.error.retryable is False
    assert "7300 s" in first.error.message
    assert "How plants eat light." in (data_of(first).content or "")
    # Final: the next transcribe request is served from the cache, as is a plain one.
    assert harness.probes() == 1
    for later in (again, plain):
        assert data_of(later).cache == "hit"
        assert later.error is not None
        assert "could not be transcribed" in later.error.message


def test_a_video_within_the_duration_limit_is_still_handed_on() -> None:
    handed, handoff = capture_handoff()
    harness = Harness(media=probe(track=None, duration=7200.0), asr_enabled=True)

    harness.run(handoff, transcribe=True)

    assert len(handed) == 1


@pytest.mark.parametrize(("retryable", "probes"), [(False, 1), (True, 2)])
def test_a_cached_draft_of_a_failed_transcription_is_final_only_when_not_retryable(
    retryable: bool, probes: int
) -> None:
    """An ASR failure for good stores the details; a later ``transcribe: true`` request is
    served from them instead of probing and downloading again. A failure that may pass is not
    stored, so the next request tries again."""
    handed, handoff = capture_handoff()
    harness = Harness(media=probe(track=None), asr_enabled=True)
    failed = Envelope[ExtractResult](
        status="failed",
        error=ErrorInfo(
            code=ErrorCode.TRANSCRIPTION_FAILED, message="It failed.", retryable=retryable
        ),
    )

    async def scenario() -> Envelope[ExtractResult]:
        await harness.arun(handoff, transcribe=True)
        (needed,) = handed
        stored = await harness.pipeline.transcribed_result(
            ExtractRequest(url=YT, transcribe=True), USER, needed.input, needed.draft, failed
        )
        assert stored.error is not None
        assert stored.error.code == ErrorCode.TRANSCRIPTION_FAILED
        return await harness.arun(handoff, transcribe=True)

    later = asyncio.run(scenario())

    assert harness.probes() == probes
    if not retryable:
        assert data_of(later).cache == "hit"
        assert later.error is not None
        assert "could not be transcribed" in later.error.message
        assert len(handed) == 1
