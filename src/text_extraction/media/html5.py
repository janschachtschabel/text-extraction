"""The HTML5 players of a page: their subtitle tracks and the media they embed (ruling M8).

``page_media(html, url, languages, max_tracks)`` reads, from the page's markup only (nothing is
fetched here):

- **Tracks.** Each ``<video>`` and ``<audio>`` element with a ``<track>`` of kind
  ``subtitles``, ``captions`` or none (the HTML default, subtitles) gives one track, chosen by
  the requested ``languages`` (primary subtag, in their order), then the page's own language
  (``<html lang>``), then the ``default`` attribute, then the first; a track whose ``src`` is not
  a plain http(s) URL (``javascript:``, ``data:``, user information) is never chosen. The first
  ``max_tracks`` elements count; with more, ``truncated`` is set. ``track_languages`` are the
  languages of all transcript tracks of the page.
- **Embeds** (D4 a): iframes of a supported platform (``media/sites.py``; normalised to the
  canonical URL, a playlist player left out: it cannot be extracted as one video), ``<video>``,
  ``<audio>`` and their ``<source>`` with an http(s) ``src`` (a platform URL counts as the
  platform's), then what JSON-LD and OpenGraph declare (``metadata.declared_media``): a player
  URL only when it is a platform's, a file URL as a platform's or a file. At most ten, without
  repeats, in that order.

Every URL is resolved against ``<base href>`` or the page URL and must pass ``validate_url``
(http or https, a host, no user information), so nothing listed or fetched carries
credentials. CPU-bound: the caller runs it on the conversion pool.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final

from lxml.html import HtmlElement

from text_extraction.convert.links import absolute_url
from text_extraction.media.captions import CaptionFormat
from text_extraction.media.select import track_language
from text_extraction.media.sites import media_target
from text_extraction.media.types import MAX_EMBEDS, MediaEmbed
from text_extraction.metadata import base_url, declared_media, page_tree
from text_extraction.netguard import InvalidUrlError, validate_url

__all__ = ["PageMedia", "PageTrack", "page_media", "track_format"]

_TRANSCRIPT_KINDS: Final = frozenset({"", "subtitles", "captions"})
_MAX_ELEMENTS: Final = 200  # media elements and iframes looked at
_MAX_TRACKS_PER_ELEMENT: Final = 50
_MAX_LABEL: Final = 200


@dataclass(frozen=True, slots=True)
class PageTrack:
    """The track chosen for one player: its absolute URL, its label and its language (the
    primary subtag of ``srclang``)."""

    url: str
    label: str | None
    language: str | None


@dataclass(frozen=True, slots=True)
class PageMedia:
    tracks: list[PageTrack] = field(default_factory=list)
    track_languages: list[str] = field(default_factory=list)
    truncated: bool = False
    embeds: list[MediaEmbed] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.tracks and not self.truncated and not self.embeds


@dataclass(frozen=True, slots=True)
class _Candidate:
    url: str | None  # None when its src is unusable
    label: str | None
    language: str | None
    default: bool


def page_media(html: str, url: str, languages: Sequence[str], max_tracks: int) -> PageMedia:
    """The tracks and embeds of the page at ``url`` (see the module docstring)."""
    tree = page_tree(html)
    base = base_url(tree, url)
    page_language = track_language(tree.get("lang") or "") if tree.tag == "html" else None
    wanted = [track_language(code) for code in languages]

    tracks: list[PageTrack] = []
    track_languages: dict[str, None] = {}
    truncated = False
    embeds = _Embeds()
    for element in _limited(tree.iter("video", "audio", "iframe")):
        if element.tag == "iframe":
            embeds.add_player(_resolve(base, element.get("src")))
            continue
        for source in (element, *element.iter("source")):
            embeds.add_file(_resolve(base, source.get("src")))
        candidates = list(_candidates(element, base))
        for candidate in candidates:
            if candidate.language:
                track_languages.setdefault(candidate.language)
        chosen = _choose(candidates, wanted, page_language)
        if chosen is None:
            continue
        if len(tracks) >= max_tracks:
            truncated = True
            continue
        tracks.append(chosen)
    for reference, role in declared_media(tree, base):
        if role == "player":
            embeds.add_player(reference)
        else:
            embeds.add_file(reference)
    return PageMedia(tracks, list(track_languages), truncated, embeds.found)


def track_format(body: bytes) -> CaptionFormat:
    """The caption format of a page's track, by its first bytes: WebVTT starts with ``WEBVTT``,
    TTML with ``<``; anything else is read as SRT."""
    head = body[:64].lstrip(b"\xef\xbb\xbf").lstrip()
    if head.startswith(b"WEBVTT"):
        return "vtt"
    if head.startswith(b"<"):
        return "ttml"
    return "srt"


class _Embeds:
    def __init__(self) -> None:
        self._found: dict[str, MediaEmbed] = {}

    @property
    def found(self) -> list[MediaEmbed]:
        return list(self._found.values())

    def add_player(self, url: str | None) -> None:
        """An iframe or a declared player: listed only when it is a platform's video."""
        target = media_target(url) if url else None
        if target is not None and target.video_id is not None:
            self._add(
                MediaEmbed(url=target.canonical_url, kind="platform", platform=target.platform)
            )

    def add_file(self, url: str | None) -> None:
        """A media file: a platform's video when its URL is one, else a file."""
        if url is None:
            return
        target = media_target(url)
        if target is not None:
            if target.video_id is not None:
                self._add(
                    MediaEmbed(url=target.canonical_url, kind="platform", platform=target.platform)
                )
            return
        self._add(MediaEmbed(url=url, kind="file"))

    def _add(self, embed: MediaEmbed) -> None:
        if len(self._found) < MAX_EMBEDS:
            self._found.setdefault(embed.url, embed)


def _limited(elements: Iterator[HtmlElement]) -> Iterator[HtmlElement]:
    for count, element in enumerate(elements):
        if count >= _MAX_ELEMENTS:
            return
        yield element


def _candidates(element: HtmlElement, base: str) -> Iterator[_Candidate]:
    count = 0
    for track in element.iter("track"):
        if count >= _MAX_TRACKS_PER_ELEMENT:
            return
        count += 1
        if (track.get("kind") or "").strip().lower() not in _TRANSCRIPT_KINDS:
            continue
        src = (track.get("src") or "").strip()
        if not src:
            continue
        label = " ".join((track.get("label") or "").split())[:_MAX_LABEL] or None
        yield _Candidate(
            url=_resolve(base, src),
            label=label,
            language=track_language((track.get("srclang") or "").strip()),
            default=track.get("default") is not None,
        )


def _choose(
    candidates: list[_Candidate], wanted: list[str | None], page_language: str | None
) -> PageTrack | None:
    usable = [candidate for candidate in candidates if candidate.url is not None]
    picked: _Candidate | None = None
    for language in [*wanted, page_language]:
        if language is None:
            continue
        picked = next((c for c in usable if c.language == language), None)
        if picked is not None:
            break
    if picked is None:
        picked = next((c for c in usable if c.default), usable[0] if usable else None)
    if picked is None or picked.url is None:
        return None
    return PageTrack(url=picked.url, label=picked.label, language=picked.language)


def _resolve(base: str, value: str | None) -> str | None:
    """``value`` as an absolute URL that passes ``validate_url``, or None."""
    value = (value or "").strip()
    if not value:
        return None
    url = absolute_url(base, value)
    if url is None:
        return None
    try:
        validate_url(url)
    except InvalidUrlError:
        return None
    return url
