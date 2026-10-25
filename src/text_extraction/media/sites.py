"""Recognise the media platforms we extract subtitles from, by host and path.

A page of a supported platform is not fetched: it goes to the media steps with its canonical
URL (the cache key, the fingerprint and the compliance check use that URL). The patterns are our
own and match the parsed host and path, never the URL as text: a host matches when it is the
registrable domain or one of its subdomains (``youtube.com.evil.example`` and ``evilyoutube.com``
do not).

``EXTRACTORS`` names, per platform, the yt-dlp extractor that is allowed for it (yt-dlp's
generic extractor, which follows anything, never runs). The names are yt-dlp's ``IE_NAME``s,
which its ``allowed_extractors`` option matches as case-insensitive full regular expressions
(not the class names: an unknown name silently loads no extractor at all); a unit test pins that
each matches exactly its extractor in the pinned yt-dlp. A change of a name is one line there.

Vimeo's canonical URL is the player's, ``https://player.vimeo.com/video/<id>``: yt-dlp needs a
login for ``vimeo.com/<id>`` but not for the player (spike, ruling M18). ZDF pages are targets
when they end in ``.html`` or are ``/video/<section>/<show>/...`` pages.

A YouTube playlist (``/playlist?list=``, ``/watch?list=`` without ``v``, and the playlist player
``/embed/videoseries?list=``, whose name has the shape of a video id) is a target without a
``video_id``. Only the URL's host and path count: user information and a port are dropped from
the canonical URL, which is what the media steps fetch.
"""

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qs, urlsplit

from text_extraction.media.types import Platform

__all__ = ["EXTRACTORS", "MediaTarget", "media_target"]

EXTRACTORS: Final[dict[Platform, str]] = {
    "youtube": "youtube",
    "vimeo": "vimeo",
    "ard": "ardmediathek",
    "zdf": "zdf",
    "arte": "artetv",
    "3sat": "3sat",
}

_YOUTUBE_DOMAINS: Final = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_YOUTUBE_ID: Final = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_LIST_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_YOUTUBE_ID_PREFIXES: Final = ("shorts", "embed", "live")
_ARTE_LANGUAGE: Final = re.compile(r"[a-z]{2}")
_DIGITS: Final = re.compile(r"[0-9]{1,20}")


@dataclass(frozen=True)
class MediaTarget:
    """A URL of a supported platform. ``video_id`` is ``None`` for a page of the platform that
    is not one video (a YouTube playlist): the pipeline answers it with its own message."""

    platform: Platform
    canonical_url: str
    extractor: str
    video_id: str | None


def media_target(url: str) -> MediaTarget | None:
    """The platform target of ``url``, or ``None`` when ``url`` is not a video page of a
    supported platform."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if _on_domain(host, "youtu.be") or _on_any(host, _YOUTUBE_DOMAINS):
        return _youtube(host, segments, parts.query)
    if _on_domain(host, "vimeo.com"):
        return _vimeo(host, segments)
    if _on_domain(host, "ardmediathek.de"):
        if segments[:1] == ["video"] and len(segments) > 1:
            return _mediathek("ard", host, parts.path, segments)
        return None
    if _on_domain(host, "zdf.de"):
        if segments[:1] == ["video"] and len(segments) >= 3:
            return _mediathek("zdf", host, parts.path, segments)
        return _html_page("zdf", host, parts.path, segments)
    if _on_domain(host, "3sat.de"):
        return _html_page("3sat", host, parts.path, segments)
    if _on_domain(host, "arte.tv"):
        if len(segments) >= 3 and _ARTE_LANGUAGE.fullmatch(segments[0]) and segments[1] == "videos":
            return _mediathek("arte", host, parts.path, segments, video_id=segments[2])
        return None
    return None


def _on_domain(host: str, domain: str) -> bool:
    host = host.lower()
    return host == domain or host.endswith("." + domain)


def _on_any(host: str, domains: tuple[str, ...]) -> bool:
    return any(_on_domain(host, domain) for domain in domains)


def _youtube(host: str, segments: list[str], query: str) -> MediaTarget | None:
    if _on_domain(host, "youtu.be"):
        return _youtube_video(segments[0]) if len(segments) == 1 else None
    nocookie = _on_domain(host, "youtube-nocookie.com")
    parameters = parse_qs(query)
    if segments == ["embed", "videoseries"]:  # the playlist player: no video id
        return _youtube_playlist(parameters)
    if segments[:1] and segments[0] in _YOUTUBE_ID_PREFIXES and len(segments) >= 2:
        if nocookie and segments[0] != "embed":
            return None
        return _youtube_video(segments[1])
    if nocookie:
        return None
    if segments == ["watch"] and "v" in parameters:
        return _youtube_video(parameters["v"][0])
    if segments in (["watch"], ["playlist"]):
        return _youtube_playlist(parameters)
    return None


def _youtube_playlist(parameters: dict[str, list[str]]) -> MediaTarget | None:
    list_id = parameters.get("list", [""])[0]
    if not _YOUTUBE_LIST_ID.fullmatch(list_id):
        return None
    return MediaTarget(
        "youtube",
        f"https://www.youtube.com/playlist?list={list_id}",
        EXTRACTORS["youtube"],
        None,
    )


def _youtube_video(video_id: str) -> MediaTarget | None:
    if not _YOUTUBE_ID.fullmatch(video_id):
        return None
    return MediaTarget(
        "youtube",
        f"https://www.youtube.com/watch?v={video_id}",
        EXTRACTORS["youtube"],
        video_id,
    )


def _vimeo(host: str, segments: list[str]) -> MediaTarget | None:
    if host.lower() == "player.vimeo.com":
        candidate = segments[1] if len(segments) >= 2 and segments[0] == "video" else ""
    elif host.lower() in ("vimeo.com", "www.vimeo.com"):
        candidate = segments[0] if segments else ""
    else:
        return None
    if not _DIGITS.fullmatch(candidate):
        return None
    return MediaTarget(
        "vimeo", f"https://player.vimeo.com/video/{candidate}", EXTRACTORS["vimeo"], candidate
    )


def _html_page(platform: Platform, host: str, path: str, segments: list[str]) -> MediaTarget | None:
    """ZDF and 3sat: any ``.html`` page is a target; the extractor decides whether it has a
    video."""
    if not segments or not segments[-1].lower().endswith(".html"):
        return None
    return _mediathek(platform, host, path, segments)


def _mediathek(
    platform: Platform,
    host: str,
    path: str,
    segments: list[str],
    *,
    video_id: str | None = None,
) -> MediaTarget:
    """The URL with scheme https, without query and fragment. The video id is the page's own
    name (the last path segment) unless the pattern gives one."""
    name = segments[-1]
    if video_id is None:
        video_id = name[: -len(".html")] if name.lower().endswith(".html") else name
    return MediaTarget(platform, f"https://{host.lower()}{path}", EXTRACTORS[platform], video_id)
