import json
from pathlib import Path
from typing import Any

import pytest

from text_extraction.media.captions import Cue
from text_extraction.media.panel import PanelError, parse_panel

FIXTURE = Path(__file__).parent / "media" / "panel" / "get_panel.json"
MAX_BYTES = 5_000_000


def answer() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_bytes())
    return loaded


def segments(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The segment view models of an answer, in panel order."""
    section = body["content"]["engagementPanelSectionListRenderer"]["content"]
    items = section["sectionListRenderer"]["contents"][0]["itemSectionRenderer"]["contents"]
    return [
        item["macroMarkersPanelItemViewModel"]["item"]["timelineItemViewModel"]["contentItems"][0][
            "transcriptSegmentViewModel"
        ]
        for item in items
    ]


def sync_list(body: dict[str, Any]) -> list[dict[str, Any]]:
    payload = body["frameworkUpdates"]["entityBatchUpdate"]["mutations"][0]["payload"]
    timed: list[dict[str, Any]] = payload["timedMarkersListSyncEntity"]["timedListData"][
        "sections"
    ][0]["timedSyncDataList"]
    return timed


def encode(body: object) -> bytes:
    return json.dumps(body).encode()


def test_segments_with_times_from_the_sync_list() -> None:
    cues = parse_panel(FIXTURE.read_bytes(), max_bytes=MAX_BYTES)

    assert len(cues) == 4
    assert cues[0] == Cue(
        start=18.0,
        end=23.98,
        text="Welcome to this short look at how rivers shape a valley.",
    )
    assert [cue.start for cue in cues] == [18.0, 24.0, 31.0, 37.0]
    assert [cue.end for cue in cues] == [23.98, 30.96, 36.5, 42.25]
    assert cues[2].text == "[Music]"  # a sound label stays a cue, as in a caption track


def test_times_fall_back_to_the_timestamp_when_lists_differ() -> None:
    body = answer()
    del sync_list(body)[1]
    segments(body)[3]["timestamp"] = "1:02:03"

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert [cue.start for cue in cues] == [18.0, 24.0, 31.0, 3723.0]
    assert [cue.end for cue in cues] == [24.0, 31.0, 3723.0, 3723.0]
    assert cues[0].text == "Welcome to this short look at how rivers shape a valley."


def test_times_fall_back_to_the_timestamp_without_a_sync_list() -> None:
    body = answer()
    del body["frameworkUpdates"]

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert [cue.start for cue in cues] == [18.0, 24.0, 31.0, 37.0]
    assert [cue.end for cue in cues] == [24.0, 31.0, 37.0, 37.0]


def test_times_fall_back_to_the_timestamp_when_a_sync_entry_is_not_a_number() -> None:
    body = answer()
    sync_list(body)[2]["videoTimeMs"] = "soon"

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert [cue.start for cue in cues] == [18.0, 24.0, 31.0, 37.0]
    assert cues[0].end == 24.0


def test_a_segment_with_an_unreadable_timestamp_is_skipped_in_the_fallback() -> None:
    body = answer()
    del body["frameworkUpdates"]
    segments(body)[1]["timestamp"] = "later"

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert [cue.start for cue in cues] == [18.0, 31.0, 37.0]
    assert [cue.end for cue in cues] == [31.0, 37.0, 37.0]


def test_segments_without_text_are_skipped() -> None:
    body = answer()
    segments(body)[1]["simpleText"] = "   "
    del segments(body)[3]["simpleText"]

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    # The sync list still pairs by the segment's index: the third cue keeps its own time.
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (18.0, 23.98, "Welcome to this short look at how rivers shape a valley."),
        (31.0, 36.5, "[Music]"),
    ]


def test_segment_text_whitespace_collapses() -> None:
    body = answer()
    segments(body)[0]["simpleText"] = "  Water\ncarries   sand \t "

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert cues[0].text == "Water carries sand"


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        "text",
        {"content": {}},
        {"content": {"engagementPanelSectionListRenderer": {"content": {}}}},
        {
            "content": {
                "engagementPanelSectionListRenderer": {
                    "content": {"sectionListRenderer": {"contents": "none"}}
                }
            }
        },
    ],
)
def test_no_segment_path_is_an_error(body: object) -> None:
    with pytest.raises(PanelError):
        parse_panel(encode(body), max_bytes=MAX_BYTES)


@pytest.mark.parametrize(
    "body", [b"", b"<html>not json</html>", b'{"content": ', b"\xff\xfe\xfa", b"[" * 100_000]
)
def test_invalid_json_is_an_error(body: bytes) -> None:
    with pytest.raises(PanelError):
        parse_panel(body, max_bytes=MAX_BYTES)


def test_body_over_the_cap_is_an_error() -> None:
    with pytest.raises(PanelError):
        parse_panel(FIXTURE.read_bytes(), max_bytes=10)


def test_an_empty_segment_list_is_no_cue() -> None:
    body = answer()
    section = body["content"]["engagementPanelSectionListRenderer"]["content"]
    section["sectionListRenderer"]["contents"][0]["itemSectionRenderer"]["contents"] = []

    assert parse_panel(encode(body), max_bytes=MAX_BYTES) == []


def test_items_that_are_not_segments_are_ignored() -> None:
    body = answer()
    section = body["content"]["engagementPanelSectionListRenderer"]["content"]
    contents = section["sectionListRenderer"]["contents"]
    contents[0]["itemSectionRenderer"]["contents"].insert(0, {"chapterHeader": {"title": "x"}})
    contents.append({"continuationItemRenderer": {}})

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert [cue.start for cue in cues] == [18.0, 24.0, 31.0, 37.0]
    assert cues[0].end == 23.98


def test_overlong_numbers_fall_back_or_skip_instead_of_failing() -> None:
    body = answer()
    sync_list(body)[0]["videoTimeMs"] = "9" * 5000
    segments(body)[0]["timestamp"] = "9" * 5000 + ":00"

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert [cue.start for cue in cues] == [24.0, 31.0, 37.0]


def test_an_end_before_its_start_ends_at_the_start() -> None:
    body = answer()
    sync_list(body)[0]["endTimeMs"] = "1000"

    cues = parse_panel(encode(body), max_bytes=MAX_BYTES)

    assert (cues[0].start, cues[0].end) == (18.0, 18.0)
