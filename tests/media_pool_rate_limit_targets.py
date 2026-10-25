"""A media ``DocumentPool`` target for the rate-limit tests: the real ``youtube_transcript`` of
the media child, with youtube-transcript-api's watch page moved to a local test server
(``FAKE_WATCH_URL``, a template with ``{video_id}``). yt-dlp must not be asked at all: its fake
raises, so a call would show as ``extractor_error``."""

import os
from collections.abc import Callable
from typing import Any

from youtube_transcript_api import _transcripts

from text_extraction.media import child

_transcripts.WATCH_URL = os.environ["FAKE_WATCH_URL"]  # type: ignore[attr-defined]


def ytdlp_info(payload: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("yt-dlp must not be asked after YouTube answered 429")


FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "youtube_transcript": child.youtube_transcript,
    "ytdlp_info": ytdlp_info,
}
