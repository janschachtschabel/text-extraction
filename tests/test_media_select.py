import pytest

from text_extraction.media.select import (
    TrackRef,
    choose_track,
    normalise_language,
    track_language,
)
from text_extraction.media.types import TranscriptSource

VTT = [TrackRef("vtt")]

# YouTube's automatic captions: the original-language ASR (`-orig`) and machine translations.
YOUTUBE_AUTO = {"de-orig": VTT, "de": VTT, "en": VTT, "fr": VTT}


@pytest.mark.parametrize(
    ("manual", "automatic", "original", "languages", "expected"),
    [
        # 1. A manual track in a requested language, in the order of `languages`.
        ({"en": VTT, "de": VTT}, YOUTUBE_AUTO, "de", ["fr", "de"], ("de", "manual_subtitles")),
        ({"en": VTT, "de": VTT}, YOUTUBE_AUTO, "de", ["en", "de"], ("en", "manual_subtitles")),
        # 2. Automatic captions in a requested language, before a manual track of another one.
        ({"en": VTT}, YOUTUBE_AUTO, "de", ["de"], ("de-orig", "auto_subtitles")),
        # 3. A manual track in the video's original language.
        ({"en": VTT, "de": VTT}, YOUTUBE_AUTO, "de", ["fr"], ("de", "manual_subtitles")),
        ({"en": VTT, "de": VTT}, YOUTUBE_AUTO, "de", [], ("de", "manual_subtitles")),
        # 4. Any manual track: the platform's first.
        ({"es": VTT, "en": VTT}, YOUTUBE_AUTO, "de", [], ("es", "manual_subtitles")),
        ({"es": VTT, "en": VTT}, {}, None, ["fr"], ("es", "manual_subtitles")),
        # 5. Automatic captions in the original language.
        ({}, YOUTUBE_AUTO, "de", [], ("de-orig", "auto_subtitles")),
        ({}, YOUTUBE_AUTO, None, ["fr"], ("de-orig", "auto_subtitles")),
        # Without a `-orig` track, the original language's own automatic track counts.
        ({}, {"de": VTT, "en": VTT}, "de", ["en"], ("de", "auto_subtitles")),
        ({}, {"de": VTT, "en": VTT}, "deu", [], ("de", "auto_subtitles")),
    ],
)
def test_choose_track_order(
    manual: dict[str, list[TrackRef]],
    automatic: dict[str, list[TrackRef]],
    original: str | None,
    languages: list[str],
    expected: tuple[str, TranscriptSource],
) -> None:
    assert choose_track(manual, automatic, original, languages) == expected


@pytest.mark.parametrize(
    ("automatic", "original", "languages"),
    [
        # `de` machine-translated from an English original: never.
        ({"en-orig": VTT, "de": VTT}, "en", ["de"]),
        ({"en": VTT, "de": VTT}, "en", ["de"]),
        # Without an original language and without `-orig` every automatic track may be a
        # translation.
        ({"de": VTT, "en": VTT}, None, ["de"]),
    ],
)
def test_translated_automatic_tracks_never(
    automatic: dict[str, list[TrackRef]], original: str | None, languages: list[str]
) -> None:
    choice = choose_track({}, automatic, original, languages)

    assert choice is None or choice[0] in ("en-orig", "en")
    assert choice is None or choice[0] != "de"


def test_english_original_gives_english_auto_not_translated_german() -> None:
    assert choose_track({}, {"en-orig": VTT, "de": VTT}, "en", ["de"]) == (
        "en-orig",
        "auto_subtitles",
    )


@pytest.mark.parametrize(
    ("key", "language"),
    [
        ("de", "de"),
        ("DE", "de"),
        ("deu", "de"),
        ("ger", "de"),
        ("eng", "en"),
        ("fra", "fr"),
        ("fre", "fr"),
        ("de-DE", "de"),
        ("en-GB", "en"),
        ("de-XwLwiJMB_Xs", "de"),  # a named YouTube track
        ("de-orig", "de"),
        ("pt-BR", "pt"),
        ("de-forced", None),  # arte: only the foreign-language passages
        ("deu-forced", None),
        ("und", None),
        ("mul", None),
        ("zxx", None),
        ("live_chat", None),  # yt-dlp's chat replay
        ("", None),
        ("x", None),
        ("1234", None),
    ],
)
def test_track_language(key: str, language: str | None) -> None:
    assert track_language(key) == language


@pytest.mark.parametrize(
    ("code", "expected"),
    [("de", "de"), ("deu", "de"), ("ger", "de"), ("EN-gb", "en"), ("nld", "nl"), ("xyz", "xyz")],
)
def test_normalise_language(code: str, expected: str) -> None:
    assert normalise_language(code) == expected


def test_mediathek_iso_639_2_keys_match_requested_639_1() -> None:
    assert choose_track({"deu": VTT}, {}, None, ["de"]) == ("deu", "manual_subtitles")
    assert choose_track({"eng": VTT, "deu": VTT}, {}, None, ["de"]) == ("deu", "manual_subtitles")


def test_named_youtube_track_counts_as_its_language() -> None:
    manual = {"en": VTT, "de-XwLwiJMB_Xs": VTT}

    assert choose_track(manual, YOUTUBE_AUTO, "de", ["de"]) == (
        "de-XwLwiJMB_Xs",
        "manual_subtitles",
    )


def test_forced_and_und_tracks_never_count() -> None:
    assert choose_track({"de-forced": VTT, "und": VTT}, {}, "de", ["de"]) is None
    assert choose_track({"de-forced": VTT, "en": VTT}, {}, "de", ["de"]) == (
        "en",
        "manual_subtitles",
    )


def test_exact_tag_wins_over_primary_subtag() -> None:
    manual = {"pt-PT": VTT, "pt-BR": VTT}

    assert choose_track(manual, {}, None, ["pt-BR"]) == ("pt-BR", "manual_subtitles")
    assert choose_track(manual, {}, None, ["pt"]) == ("pt-PT", "manual_subtitles")


def test_requested_three_letter_code_matches_two_letter_track() -> None:
    assert choose_track({"en": VTT, "de": VTT}, {}, None, ["deu"]) == ("de", "manual_subtitles")


def test_no_tracks_no_choice() -> None:
    assert choose_track({}, {}, "de", ["de"]) is None
