"""A media ``DocumentPool`` target for the tests: fakes of the media child's functions.

What a fake does depends on the target's video id (``payload["video_id"]``), so one pool serves
every scenario: ``sleep`` hangs, ``blocked`` raises the libraries' refusals (which one by the
environment variable ``FAKE_REFUSAL``), anything else answers a small video with a track.
"""

import os
import time
from collections.abc import Callable
from typing import Any

WEBVTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nHallo aus dem Test.\n"


def _reply(**overrides: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "title": "A video",
        "description": None,
        "uploader": None,
        "upload_date": None,
        "duration": 60.0,
        "license": None,
        "chapters": [],
        "tags": [],
        "original_language": "de",
        "manual": {"de": [{"ext": "vtt", "name": "Deutsch"}]},
        "automatic": {},
        "track": {
            "key": "de",
            "language": "de",
            "source": "manual_subtitles",
            "format": "vtt",
            "body": WEBVTT,
            "charset": None,
        },
        "is_live": False,
        "error": None,
    }
    reply.update(overrides)
    return reply


def _behave(payload: dict[str, Any]) -> None:
    if payload["video_id"] == "sleep":
        while True:
            time.sleep(1)


def youtube_transcript(payload: dict[str, Any]) -> dict[str, Any]:
    _behave(payload)
    if payload["video_id"] == "blocked":
        import youtube_transcript_api

        refusal = getattr(youtube_transcript_api, os.environ.get("FAKE_REFUSAL", "RequestBlocked"))
        raise refusal(payload["video_id"])
    return _reply(title=None, duration=None)


def ytdlp_info(payload: dict[str, Any]) -> dict[str, Any]:
    _behave(payload)
    if payload["video_id"] == "blocked":
        from yt_dlp.utils import DownloadError

        raise DownloadError(
            "ERROR: [youtube] blocked: Sign in to confirm you're not a bot. Use --cookies."
        )
    if not payload.get("fetch_track", True):
        return _reply(track=None)
    return _reply()


FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "youtube_transcript": youtube_transcript,
    "ytdlp_info": ytdlp_info,
}
