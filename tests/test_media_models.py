from typing import Any, get_args

import pytest
from pydantic import ValidationError

from tests.test_document_models import PACKAGE_4_RESULT
from text_extraction.browser_dispatch import job_options
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.job_models import BatchRequest, JobOptions
from text_extraction.licenses import License
from text_extraction.media.types import MediaEmbed, MediaInfo, MediaKind, Platform, TranscriptSource


def request_body(model: type, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"url": "https://example.org/"}
    if model is BatchRequest:
        body = {"urls": ["https://example.org/"]}
    return {**body, **fields}


def test_cached_result_without_media_still_parses() -> None:
    """A result of package 5 (no ``media`` key) stays readable."""
    result = ExtractResult.model_validate(PACKAGE_4_RESULT)

    assert result.media is None
    assert result.url == "https://www.example.org/page"


@pytest.mark.parametrize("model", [ExtractRequest, BatchRequest])
def test_languages_validated(model: type) -> None:
    ok = model(**request_body(model, languages=["de", "DE", "en-GB", "EN-gb", "zh-Hans"]))

    assert ok.languages == ["de", "de", "en-GB", "en-gb", "zh-Hans"]
    for bad in (["deutsch"], ["d"], ["de_DE"], ["de-"], ["de-x"], [""], ["en-GB-oxendict"]):
        with pytest.raises(ValidationError):
            model(**request_body(model, languages=bad))
    with pytest.raises(ValidationError):
        model(**request_body(model, languages=["de", "en", "fr", "es", "it", "nl"]))
    assert (
        len(model(**request_body(model, languages=["de", "en", "fr", "es", "it"])).languages) == 5
    )


def test_languages_and_transcribe_default_to_off() -> None:
    assert ExtractRequest(url="https://example.org/").transcribe is False
    assert ExtractRequest(url="https://example.org/").languages == []
    assert BatchRequest(urls=["https://example.org/"]).transcribe is False
    assert BatchRequest(urls=["https://example.org/"]).languages == []
    assert JobOptions(compliance="enforce").transcribe is False
    assert JobOptions(compliance="enforce").languages == []


def test_job_options_omit_default_media_fields() -> None:
    """Pods of package 5 parse the options strictly: the defaults are not stored."""
    from text_extraction.jobs import _options_json

    stored = _options_json(JobOptions(compliance="enforce"))

    assert '"transcribe"' not in stored
    assert '"languages"' not in stored
    assert '"ocr"' not in stored


def test_job_options_store_a_media_choice() -> None:
    from text_extraction.jobs import _options_json

    options = JobOptions(compliance="enforce", transcribe=True, languages=["de"])

    stored = _options_json(options)

    assert JobOptions.model_validate_json(stored) == options
    assert '"transcribe":true' in stored
    assert '"languages":["de"]' in stored
    assert '"transcribe"' not in _options_json(JobOptions(compliance="enforce", languages=["de"]))
    assert '"languages"' not in _options_json(JobOptions(compliance="enforce", transcribe=True))


def test_media_options_travel_from_request_to_job_options_and_back() -> None:
    req = ExtractRequest(url="https://example.org/v.mp4", transcribe=True, languages=["de", "en"])

    options = job_options(req, "enforce")
    rebuilt = ExtractRequest(url=req.url, **options.model_dump())

    assert options.transcribe is True
    assert options.languages == ["de", "en"]
    assert rebuilt.transcribe is True
    assert rebuilt.languages == ["de", "en"]


def test_minimal_media_info_is_cheap_to_build() -> None:
    info = MediaInfo(platform="youtube", transcript_source="metadata_only")

    assert info.transcript_language is None
    assert info.available_languages == []
    assert info.duration_seconds is None
    assert info.uploader is None
    assert info.asr_model is None
    assert info.embeds == []


def test_media_info_round_trips_in_a_result() -> None:
    info = MediaInfo(
        platform="html5",
        transcript_source="manual_subtitles",
        transcript_language="de",
        available_languages=["de", "en"],
        duration_seconds=61.5,
        uploader="Someone",
        embeds=[
            MediaEmbed(
                url="https://www.youtube.com/watch?v=abc", kind="platform", platform="youtube"
            ),
            MediaEmbed(url="https://example.org/a.mp3", kind="file"),
        ],
    )
    values = {**PACKAGE_4_RESULT, "media": info.model_dump()}

    result = ExtractResult.model_validate(values)

    assert result.media == info
    assert result.media.embeds[1].platform is None


def test_media_info_has_at_most_ten_embeds() -> None:
    embed = MediaEmbed(url="https://example.org/a.mp3", kind="file")

    MediaInfo(platform="html5", transcript_source="manual_subtitles", embeds=[embed] * 10)
    with pytest.raises(ValidationError):
        MediaInfo(platform="html5", transcript_source="manual_subtitles", embeds=[embed] * 11)


def test_media_literals() -> None:
    assert get_args(MediaKind) == (
        "mp3", "mp4", "m4a", "mov", "webm", "mkv", "ogg", "wav", "flac", "aac"
    )  # fmt: skip
    assert get_args(Platform) == (
        "youtube", "vimeo", "ard", "zdf", "arte", "3sat", "file", "upload", "html5"
    )  # fmt: skip
    assert get_args(TranscriptSource) == (
        "manual_subtitles", "auto_subtitles", "asr", "metadata_only"
    )  # fmt: skip


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (MediaInfo, {"platform": "tiktok", "transcript_source": "asr"}),
        (MediaInfo, {"platform": "youtube", "transcript_source": "guess"}),
        (MediaEmbed, {"url": "https://example.org/", "kind": "stream"}),
    ],
)
def test_media_models_reject_unknown_values(model: type, fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model(**fields)


def test_platform_is_a_licence_source() -> None:
    licence = License(
        id="CC-BY-3.0", url=None, label="Creative Commons Attribution license", source="platform",
        confidence="high",
    )  # fmt: skip

    assert licence.source == "platform"
