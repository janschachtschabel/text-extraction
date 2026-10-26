"""Which subtitle track of a video becomes its transcript (ruling M2, with the spike's findings
of M18). Pure: the media children import it to pick the track they download.

Tracks come keyed by the platform's language key: ``manual`` holds the tracks a person made,
``automatic`` the platform's automatic captions. Keys are compared by their primary subtag
after normalisation:

* ISO 639-2 codes (``deu``, ``ger``, ``eng``, ...), which the Mediatheken use, become ISO 639-1
  (``de``, ``en``); a code the table does not know stays as it is.
* Further subtags do not change the language: a named YouTube track (``de-XwLwiJMB_Xs``) and a
  regional one (``de-DE``) count as ``de``. A requested ``pt-BR`` prefers the track ``pt-BR``
  over another ``pt`` track, though.
* A track whose key is not a language never counts: ``und`` and the other special codes, a key
  whose primary subtag is not two or three letters (yt-dlp's ``live_chat``), and arte's
  ``-forced`` tracks, which only cover the foreign-language passages of a film.
* Of the automatic captions only the original-language speech recognition counts: YouTube marks
  it ``<lang>-orig``, and every other automatic language is a machine translation. A platform
  without ``-orig`` keys counts only the track in the video's original language (none when the
  original language is unknown).

The order: manual tracks in the order of ``languages``, then automatic captions in that order,
a manual track in the original language, any manual track (the platform's first), and last the
automatic captions in the original language.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from text_extraction.media.types import TranscriptSource

__all__ = ["TrackRef", "choose_track", "normalise_language", "track_language"]

ORIGINAL_SUFFIX: Final = "orig"
_FORCED: Final = "forced"
_PRIMARY: Final = re.compile(r"[a-z]{2,3}")
# Codes that name no language: undetermined, multiple, no linguistic content, uncoded.
_NOT_A_LANGUAGE: Final = frozenset({"und", "mul", "zxx", "mis"})
# ISO 639-2 (bibliographic and terminology codes) to ISO 639-1, for the languages a
# Mediathek or a teaching video is likely to carry.
_ISO_639_2: Final = {
    "ara": "ar",
    "bul": "bg",
    "ces": "cs",
    "cze": "cs",
    "chi": "zh",
    "zho": "zh",
    "dan": "da",
    "deu": "de",
    "ger": "de",
    "dut": "nl",
    "nld": "nl",
    "ell": "el",
    "gre": "el",
    "eng": "en",
    "est": "et",
    "fin": "fi",
    "fra": "fr",
    "fre": "fr",
    "hrv": "hr",
    "hun": "hu",
    "ita": "it",
    "jpn": "ja",
    "kor": "ko",
    "lav": "lv",
    "lit": "lt",
    "nob": "nb",
    "nor": "no",
    "per": "fa",
    "fas": "fa",
    "pol": "pl",
    "por": "pt",
    "ron": "ro",
    "rum": "ro",
    "rus": "ru",
    "slk": "sk",
    "slo": "sk",
    "slv": "sl",
    "spa": "es",
    "srp": "sr",
    "swe": "sv",
    "tur": "tr",
    "ukr": "uk",
}


@dataclass(frozen=True, slots=True)
class TrackRef:
    """One format of a subtitle track, as the platform offers it (``vtt``, ``ttml``, ...)."""

    ext: str
    name: str | None = None


def normalise_language(code: str) -> str:
    """The primary subtag of ``code`` in lower case, as ISO 639-1 where the table knows it."""
    primary = code.split("-", 1)[0].strip().lower()
    return _ISO_639_2.get(primary, primary)


def track_language(key: str) -> str | None:
    """The language a track key stands for, or None when the track never counts."""
    subtags = key.strip().lower().split("-")
    if _FORCED in subtags[1:] or not _PRIMARY.fullmatch(subtags[0]):
        return None
    language = normalise_language(subtags[0])
    return None if language in _NOT_A_LANGUAGE else language


def choose_track(
    manual: Mapping[str, Sequence[TrackRef]],
    automatic: Mapping[str, Sequence[TrackRef]],
    original_language: str | None,
    languages: Sequence[str],
) -> tuple[str, TranscriptSource] | None:
    """The key of the track to take and where it comes from, or None (see the module
    docstring). Machine-translated automatic captions are never chosen."""
    original = normalise_language(original_language) if original_language else None
    persons = [key for key in manual if track_language(key) is not None]
    machines = _original_automatic(list(automatic), original)
    wanted = list(dict.fromkeys(languages))
    for requested in wanted:
        if (key := _first_for(persons, requested)) is not None:
            return key, "manual_subtitles"
    for requested in wanted:
        if (key := _first_for(machines, requested)) is not None:
            return key, "auto_subtitles"
    if original is not None and (key := _first_for(persons, original)) is not None:
        return key, "manual_subtitles"
    if persons:
        return persons[0], "manual_subtitles"
    if machines:
        return machines[0], "auto_subtitles"
    return None


def _original_automatic(keys: list[str], original: str | None) -> list[str]:
    """The automatic tracks that are the original-language speech recognition."""
    marked = [
        key
        for key in keys
        if key.lower().split("-")[-1] == ORIGINAL_SUFFIX and track_language(key) is not None
    ]
    if marked:
        return marked
    if original is None:
        return []
    return [key for key in keys if track_language(key) == original]


def _first_for(keys: list[str], requested: str) -> str | None:
    """The first key with exactly the requested tag, else the first in its language."""
    language = normalise_language(requested)
    exact = _tag(requested)
    for key in keys:
        if _tag(key) == exact and track_language(key) == language:
            return key
    for key in keys:
        if track_language(key) == language:
            return key
    return None


def _tag(code: str) -> str:
    primary, _, rest = code.strip().lower().partition("-")
    return f"{normalise_language(primary)}-{rest}" if rest else normalise_language(primary)
