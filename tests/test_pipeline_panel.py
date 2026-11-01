"""A YouTube video whose subtitles could not be read, tried again in YouTube's transcript view
in the browser (package 13, rulings P13-S1, S2, S5, S6, S7).

The drafts come from a pipeline without a renderer (an API pod) and canned probes; the render
is a ``FakeRenderer`` whose ``transcript_panel`` is the recorded ``get_panel`` fixture. The
sandbox reaches no platform.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from tests.fakes import FakeFetcher, FakeRenderer, RecordingGate, fetched, rendered, response
from tests.test_pipeline_media import SENTENCES, YT, YT_ID, YT_SHORT, FakeMediaFetcher, Probe, probe
from text_extraction.auth import Principal
from text_extraction.compliance import ComplianceChecker, ComplianceReport
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.escalation import needs_transcript_panel
from text_extraction.extract_models import ExtractRequest, ExtractResult, Timings
from text_extraction.fetch import Fetcher, FetchError
from text_extraction.fingerprint import FingerprintStore
from text_extraction.media.captions import Cue
from text_extraction.media.panel import TRANSCRIPT_PANEL_UNAVAILABLE
from text_extraction.media.pool import MediaFetcher
from text_extraction.media.types import MediaInfo
from text_extraction.outbound import DomainGate
from text_extraction.pipeline import Pipeline
from text_extraction.pipeline_media import panel_result
from text_extraction.render import Renderer
from text_extraction.result_cache import ResultCache
from text_extraction.settings import Settings

USER = Principal(name="alice", role="user", rate_limit_per_minute=None)
ADMIN = Principal(name="root", role="admin", rate_limit_per_minute=None)
CANONICAL = f"https://www.youtube.com/watch?v={YT_ID}"
FIXTURE = Path(__file__).parent / "media" / "panel" / "get_panel.json"
PANEL = FIXTURE.read_bytes()
PANEL_TEXT = "Water carries sand and stones downstream."
EMPTY_PANEL = json.dumps(
    {
        "content": {
            "engagementPanelSectionListRenderer": {
                "content": {"sectionListRenderer": {"contents": []}}
            }
        }
    }
).encode()
# The watch page as the browser would read it: its text must never become content.
WATCH_PAGE = (
    "<html><head><title>Photosynthesis for kids - YouTube</title></head><body>"
    "<p>Recommended for you: Unrelated cooking video with many many words in it today.</p>"
    "</body></html>"
)
PAGE_TEXT = "Unrelated cooking video"


def blocked_probe() -> Probe:
    return probe(manual={}, automatic={}, track=None, blocked=True, error="blocked")


def failed_probe() -> Probe:
    return probe(track=None, error="track_failed")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "auth_mode": "none",
        "outbound_min_delay_ms": 0,
        "browser_enabled": True,
        # The fixture's transcript has some 25 words: a success with this minimum.
        "quality_min_words": 10,
    }
    values.update(overrides)
    return Settings(**values)


class Harness:
    """A pipeline with the media steps; ``renderer=None`` builds an API pod's (no renderer).
    Harnesses built on the same ``valkey`` share the cache and the fingerprints."""

    def __init__(
        self,
        media: Probe | None = None,
        *,
        renderer: FakeRenderer | None = None,
        unblock_renderer: FakeRenderer | None = None,
        valkey: FakeAsyncRedis | None = None,
        **overrides: Any,
    ) -> None:
        self.registry = CollectorRegistry()
        self.valkey = valkey if valkey is not None else FakeAsyncRedis()
        self.settings = make_settings(**overrides)
        self.gate = RecordingGate(DomainGate(self.valkey, self.settings))
        self.fake = FakeFetcher({})
        self.fake.before_request = self.gate.before_request
        self.fake.on_request_sent = self.gate.mark_sent
        self.media = FakeMediaFetcher(media if media is not None else blocked_probe())
        self.renderer = renderer
        fetcher = cast(Fetcher, self.fake)
        self.pipeline = Pipeline(
            fetcher,
            cast(DomainGate, self.gate),
            ComplianceChecker(fetcher, self.valkey, self.settings),
            ResultCache(self.valkey, self.settings),
            FingerprintStore(self.valkey, self.settings),
            self.settings,
            self.registry,
            media_fetcher=cast(MediaFetcher, self.media),
            renderer=cast(Renderer | None, renderer),
            unblock_renderer=cast(Renderer | None, unblock_renderer),
        )

    async def arun(
        self,
        *,
        principal: Principal = USER,
        draft: Envelope[ExtractResult] | None = None,
        **request: Any,
    ) -> Envelope[ExtractResult]:
        request.setdefault("url", YT)
        request.setdefault("mode", "auto")
        return await self.pipeline.run(ExtractRequest(**request), principal, draft=draft)

    def run(self, **request: Any) -> Envelope[ExtractResult]:
        return asyncio.run(self.arun(**request))

    def keys(self, pattern: str) -> list[bytes]:
        async def scan() -> list[bytes]:
            return [key async for key in self.valkey.scan_iter(pattern)]

        return asyncio.run(scan())

    def panel_count(self, outcome: str) -> float:
        value = self.registry.get_sample_value(
            "text_extraction_media_transcript_panel_total", {"outcome": outcome}
        )
        return value or 0.0


def data_of(envelope: Envelope[ExtractResult]) -> ExtractResult:
    assert envelope.data is not None
    return envelope.data


def watch(
    *, body: bytes | None = PANEL, warnings: tuple[str, ...] = (), answers: int = 1
) -> FakeRenderer:
    """A renderer that answers the watch page, with ``body`` as its get_panel answer."""
    if body is None and not warnings:
        warnings = (TRANSCRIPT_PANEL_UNAVAILABLE,)
    return FakeRenderer(
        rendered(
            WATCH_PAGE,
            url=CANONICAL,
            responses=[response(CANONICAL)] * answers,
            warnings=warnings,
            transcript_panel=body,
        )
    )


def draft_of(
    media: Probe | None = None, **request: Any
) -> tuple[Envelope[ExtractResult], FakeAsyncRedis]:
    """The draft an API pod makes, and the Valkey it used."""
    drafting = Harness(media)
    draft = drafting.run(**request)
    assert needs_transcript_panel(draft)
    return draft, drafting.valkey


# --- panel_result (pure) ----------------------------------------------------------------------


def draft_envelope() -> Envelope[ExtractResult]:
    result = ExtractResult(
        url=YT_SHORT,
        final_url=CANONICAL,
        upstream_status=None,
        content_type="text/html",
        mode_used="fast",
        attempts=1,
        language="en",
        word_count=8,
        title="Rivers and valleys",
        format="markdown",
        content="# Rivers and valleys\n\nHow rivers shape a valley.",
        quality=None,
        timings=Timings(total_ms=5, compliance_ms=1, fetch_ms=3, convert_ms=1),
        compliance=ComplianceReport(mode="enforce"),
        metadata=None,
        links=None,
        fingerprint=None,
        cache="miss",
        warnings=["subtitles_blocked", "other_note"],
        media=MediaInfo(
            platform="youtube",
            transcript_source="metadata_only",
            available_languages=["en"],
            uploader="River School",
        ),
    )
    error = ErrorInfo(code=ErrorCode.NO_TRANSCRIPT_AVAILABLE, message="details only")
    return Envelope(status="partial", data=result, error=error)


# Enough words for the language to be read from them (50).
CUES = [
    Cue(start=index * 3.0, end=index * 3.0 + 2.5, text=sentence)
    for index, sentence in enumerate(SENTENCES)
]


def test_panel_result_with_cues_is_the_transcript() -> None:
    draft = draft_envelope()

    answer = panel_result(draft, CUES, "markdown", warnings=["consent_overlay_removed"])

    assert answer.status == "success"
    assert answer.error is None
    data = data_of(answer)
    assert data.content is not None
    assert data.content.startswith("# Rivers and valleys\n")
    assert SENTENCES[0] in data.content
    assert "How rivers shape" not in data.content  # the description is not the transcript
    assert data.content_type == "text/vtt"
    assert data.mode_used == "browser"
    assert data.unblocked is False
    assert data.quality is not None
    assert data.word_count == data.quality.word_count > 0
    assert data.language == "en"
    assert data.media is not None
    assert data.media.transcript_source == "transcript_panel"
    assert data.media.transcript_language == "en"
    assert data.media.uploader == "River School"  # the draft's details stay
    assert data.warnings == ["other_note"]
    assert data.fingerprint is not None
    assert data.title == "Rivers and valleys"
    assert data.final_url == CANONICAL


def test_panel_result_fingerprints_the_cues_text() -> None:
    first = data_of(panel_result(draft_envelope(), CUES, "markdown", warnings=[]))
    again = data_of(panel_result(draft_envelope(), CUES, "text", warnings=[]))
    other = data_of(panel_result(draft_envelope(), CUES[:-1], "markdown", warnings=[]))

    assert first.fingerprint is not None
    assert again.fingerprint is not None
    assert other.fingerprint is not None
    assert first.fingerprint.raw_sha256 == again.fingerprint.raw_sha256
    assert first.fingerprint.raw_sha256 != other.fingerprint.raw_sha256


@pytest.mark.parametrize(
    ("cues", "warnings"),
    [
        (None, []),
        ([], []),
        ([Cue(start=0.0, end=1.0, text="  ")], []),
        (CUES, [TRANSCRIPT_PANEL_UNAVAILABLE]),
    ],
    ids=["no_cues", "empty", "no_text", "warning"],
)
def test_panel_result_without_text_is_the_draft_with_the_warning(
    cues: list[Cue] | None, warnings: list[str]
) -> None:
    draft = draft_envelope()

    answer = panel_result(draft, cues, "markdown", warnings=warnings)

    assert answer.status == draft.status
    assert answer.error == draft.error
    data = data_of(answer)
    assert data.warnings == ["subtitles_blocked", "other_note", TRANSCRIPT_PANEL_UNAVAILABLE]
    assert data.model_dump(exclude={"warnings"}) == data_of(draft).model_dump(exclude={"warnings"})


# --- the browser worker's render of a draft (P13-S2) ------------------------------------------


@pytest.mark.parametrize("media", [blocked_probe(), failed_probe()], ids=["blocked", "failed"])
def test_a_draft_whose_subtitles_failed_reads_the_transcript_view(media: Probe) -> None:
    draft, valkey = draft_of(media, url=YT_SHORT)
    renderer = watch()
    harness = Harness(media, renderer=renderer, valkey=valkey)

    envelope = harness.run(url=YT_SHORT, draft=draft)

    assert [call.url for call in renderer.calls] == [CANONICAL]
    options = renderer.calls[0].options
    assert options.transcript_panel is True
    assert options.unblock is False
    assert options.actions == ()
    assert envelope.status == "success"
    data = data_of(envelope)
    assert data.mode_used == "browser"
    assert data.media is not None
    assert data.media.transcript_source == "transcript_panel"
    assert data.content is not None
    assert data.content.startswith("# Photosynthesis for kids\n")
    assert PANEL_TEXT in data.content
    assert PAGE_TEXT not in data.content
    assert not [w for w in data.warnings if w.startswith("subtitles_")]
    assert data.fingerprint is not None
    assert data.attempts == data_of(draft).attempts + 1
    assert data.url == YT_SHORT
    assert harness.media.calls == []  # the browser worker does not probe again
    assert harness.panel_count("transcript") == 1


def test_the_transcript_is_stored_under_the_videos_key() -> None:
    draft, valkey = draft_of()
    Harness(renderer=watch(), valkey=valkey).run(draft=draft)

    later = Harness(valkey=valkey).run(url=YT_SHORT)  # an API pod, the short form

    assert later.status == "success"
    data = data_of(later)
    assert data.cache == "hit"
    assert data.media is not None
    assert data.media.transcript_source == "transcript_panel"


def test_the_render_and_the_steps_count_in_attempts_and_timings() -> None:
    draft, valkey = draft_of()
    renderer = watch(answers=2)

    async def steps() -> None:  # the transcript view steps run inside the render
        await asyncio.sleep(0.05)

    renderer.during = steps
    harness = Harness(renderer=renderer, valkey=valkey)

    data = data_of(harness.run(draft=draft))

    assert data.attempts == data_of(draft).attempts + 2
    assert data.timings.render_ms >= 40


@pytest.mark.parametrize(
    ("renderer", "outcome"),
    [
        (watch(body=None), "unavailable"),
        (watch(body=b"<html>not json</html>"), "unavailable"),
        (watch(body=EMPTY_PANEL), "empty"),
        (FakeRenderer(FetchError(ErrorCode.TIMEOUT, "slow", retryable=True)), "unavailable"),
    ],
    ids=["no_body", "not_json", "empty", "render_failed"],
)
def test_without_a_transcript_the_answer_is_the_draft_with_the_warning(
    renderer: FakeRenderer, outcome: str
) -> None:
    draft, valkey = draft_of()
    harness = Harness(renderer=renderer, valkey=valkey)

    envelope = harness.run(draft=draft)

    assert envelope.status == draft.status
    assert envelope.error == draft.error
    data = data_of(envelope)
    assert data.mode_used == "fast"
    assert data.warnings == [*data_of(draft).warnings, TRANSCRIPT_PANEL_UNAVAILABLE]
    assert data.content == data_of(draft).content
    assert PAGE_TEXT not in (data.content or "")
    assert data.fingerprint is None
    assert harness.keys("res:*") == []
    assert harness.keys("fp:*") == []
    assert harness.panel_count(outcome) == 1


BLOCKED_HOST = "https://blocked.example"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def disallow(harness: Harness, origin: str) -> None:
    """``origin``'s robots.txt forbids everything (compliance ``enforce`` blocks it)."""
    robots = f"{origin}/robots.txt"
    harness.fake.answers[robots] = fetched(robots, DISALLOW_ALL, content_type="text/plain")


@pytest.mark.parametrize(
    "case", ["origin_blocked", "redirect_blocked", "refused_403", "no_document"]
)
def test_a_refused_panel_render_is_the_draft_with_the_warning(case: str) -> None:
    draft, valkey = draft_of()
    if case == "origin_blocked":  # the canonical URL's own origin forbids it
        renderer = watch()
    elif case == "redirect_blocked":  # the watch page redirected to a forbidden origin
        landed = f"{BLOCKED_HOST}/watch"
        renderer = FakeRenderer(
            rendered(
                WATCH_PAGE,
                url=landed,
                responses=[response(landed, redirects=[CANONICAL])],
                transcript_panel=PANEL,
            )
        )
    else:  # the watch page itself was refused (or not reported), whatever the panel answered
        answers = [response(CANONICAL, status=403)] if case == "refused_403" else []
        renderer = FakeRenderer(
            rendered(WATCH_PAGE, url=CANONICAL, responses=answers, transcript_panel=PANEL)
        )
    # The drafting pod cached YouTube's files as allowing: the origin's block needs its own
    # Valkey, where YouTube's files are loaded again.
    shared = FakeAsyncRedis() if case == "origin_blocked" else valkey
    harness = Harness(renderer=renderer, valkey=shared)
    disallow(harness, "https://www.youtube.com" if case == "origin_blocked" else BLOCKED_HOST)

    envelope = harness.run(draft=draft)

    assert envelope.status == draft.status
    assert envelope.error == draft.error
    data = data_of(envelope)
    assert data.mode_used == "fast"
    assert data.warnings == [*data_of(draft).warnings, TRANSCRIPT_PANEL_UNAVAILABLE]
    assert data.content == data_of(draft).content
    assert PAGE_TEXT not in (data.content or "")
    assert PANEL_TEXT not in (data.content or "")
    assert data.fingerprint is None
    assert harness.keys("res:*") == []
    assert harness.keys("fp:*") == []
    assert harness.panel_count("unavailable") == 1
    if case == "origin_blocked":
        assert renderer.calls == []  # never rendered


def test_an_unblock_admin_still_gets_the_standard_browser() -> None:
    draft, valkey = draft_of(principal=ADMIN, compliance="ignore")
    renderer, unblock = watch(), watch()
    harness = Harness(
        renderer=renderer,
        unblock_renderer=unblock,
        valkey=valkey,
        browser_unblock_enabled=True,
    )

    envelope = harness.run(principal=ADMIN, compliance="ignore", draft=draft)

    assert unblock.calls == []
    assert renderer.calls[0].options.unblock is False
    assert data_of(envelope).unblocked is False
    assert data_of(envelope).media is not None


def test_a_panel_escalation_of_a_page_result_is_never_a_page_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A page result that looks like a panel draft (no such page result is known today) goes to
    # the transcript view: never the whole-page render, never the unblock renderer.
    renderer, unblock = watch(), watch()
    harness = Harness(renderer=renderer, unblock_renderer=unblock, browser_unblock_enabled=True)

    async def no_video(*args: Any) -> None:
        return None

    async def page_result(*args: Any) -> Envelope[ExtractResult]:
        return draft_envelope()

    monkeypatch.setattr(harness.pipeline, "_platform", no_video)
    monkeypatch.setattr(harness.pipeline, "_fetch_and_extract", page_result)

    envelope = harness.run(principal=ADMIN, compliance="ignore")

    assert unblock.calls == []
    assert [call.url for call in renderer.calls] == [CANONICAL]
    assert renderer.calls[0].options.transcript_panel is True
    assert renderer.calls[0].options.unblock is False
    data = data_of(envelope)
    assert data.unblocked is False
    assert data.media is not None
    assert data.media.transcript_source == "transcript_panel"
    assert PAGE_TEXT not in (data.content or "")


def test_with_the_switch_off_the_draft_is_not_rendered() -> None:
    draft, valkey = draft_of()
    renderer = watch()
    harness = Harness(renderer=renderer, valkey=valkey, media_transcript_panel_enabled=False)

    envelope = harness.run(draft=draft)

    assert renderer.calls == []
    assert data_of(envelope).warnings == [*data_of(draft).warnings, TRANSCRIPT_PANEL_UNAVAILABLE]
    assert harness.panel_count("unavailable") == 0


def test_panel_logs_carry_no_url_or_video_id() -> None:
    draft, valkey = draft_of(url=YT_SHORT)
    harness = Harness(renderer=watch(body=b"not json"), valkey=valkey)

    with capture_logs() as logs:
        harness.run(url=YT_SHORT, draft=draft)

    text = json.dumps(logs, default=str)
    assert YT_ID not in text
    assert "watch" not in text


# --- a browser worker's own ``auto`` request (P13-S1 in the pipeline) -------------------------


def test_an_auto_request_in_the_browser_worker_escalates_to_the_panel() -> None:
    renderer = watch()
    harness = Harness(renderer=renderer)

    envelope = harness.run()

    assert [call.url for call in renderer.calls] == [CANONICAL]
    assert renderer.calls[0].options.transcript_panel is True
    data = data_of(envelope)
    assert data.media is not None
    assert data.media.transcript_source == "transcript_panel"
    assert data.attempts == 2  # the probe and the rendered document


@pytest.mark.parametrize(
    ("request_options", "overrides"),
    [
        ({"mode": "fast"}, {}),
        ({"transcribe": True}, {}),
        ({}, {"media_transcript_panel_enabled": False}),
        ({}, {"browser_enabled": False}),
    ],
    ids=["fast", "transcribe", "switch_off", "browser_off"],
)
def test_no_panel_render_without_its_conditions(
    request_options: dict[str, Any], overrides: dict[str, Any]
) -> None:
    renderer = watch()
    harness = Harness(renderer=renderer, **overrides)

    envelope = harness.run(**request_options)

    assert renderer.calls == []
    data = data_of(envelope)
    assert data.media is not None
    assert data.media.transcript_source == "metadata_only"
    assert TRANSCRIPT_PANEL_UNAVAILABLE not in data.warnings


def test_a_video_without_subtitles_is_not_rendered() -> None:
    renderer = watch()
    harness = Harness(probe(manual={}, automatic={}, track=None), renderer=renderer)

    envelope = harness.run()

    assert renderer.calls == []
    assert data_of(envelope).media is not None
