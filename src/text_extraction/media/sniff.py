"""Decide whether a body is an audio or video file, by its first bytes.

Only the head of a file is needed (``MEDIA_HEAD_BYTES``): a fetched response declared as audio
or video is read no further than that, so a large file is recognised without being downloaded.
The bytes decide; the declared type never overrides them. Nothing here parses a container
beyond the few bytes that name its format.
"""

from typing import Final

from text_extraction.media.types import MediaKind

__all__ = ["MEDIA_HEAD_BYTES", "is_declared_media", "media_type_of", "sniff_media"]

# The part of a media file that is read for sniffing; a declared media type is fetched no further.
MEDIA_HEAD_BYTES: Final = 65_536

_MEDIA_TYPES: Final[dict[MediaKind, str]] = {
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
    "m4a": "audio/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "aac": "audio/aac",
}

# ``ftyp`` brands that are audio-only (iTunes) or QuickTime; every other brand (isom, mp41, mp42,
# avc1, dash, iso2 ...) is MP4.
_M4A_BRANDS: Final = frozenset({b"M4A ", b"M4B "})
_MOV_BRAND: Final = b"qt  "
_EBML_MAGIC: Final = b"\x1a\x45\xdf\xa3"
_EBML_DOCTYPE_ID: Final = b"\x42\x82"
_EBML_HEADER_LIMIT: Final = 1024  # the EBML header is a few dozen bytes
_ADTS_SYNCS: Final = (b"\xff\xf1", b"\xff\xf9")  # MPEG-4 and MPEG-2, no CRC


def media_type_of(kind: MediaKind) -> str:
    """The media type to report for a sniffed kind."""
    return _MEDIA_TYPES[kind]


def is_declared_media(content_type: str | None) -> bool:
    """True for a declared ``audio/*``, ``video/*`` or ``application/ogg`` (parameters ignored)."""
    if not content_type:
        return False
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type.startswith(("audio/", "video/")) or media_type == "application/ogg"


def sniff_media(head: bytes, declared: str | None) -> MediaKind | None:
    """The kind of audio or video file whose first bytes are ``head``, or ``None``.

    ``declared`` (the response's media type, if any) would break a tie where the bytes are
    ambiguous; no format sniffed here is, so today the bytes decide alone.
    """
    del declared
    if head.startswith(b"ID3") or _is_mpeg_audio_frame(head):
        return "mp3"
    if head.startswith(_ADTS_SYNCS):
        return "aac"
    if head[4:8] == b"ftyp" and len(head) >= 12:
        return _ftyp_kind(head[8:12])
    if head.startswith(_EBML_MAGIC):
        return _ebml_kind(head)
    if head.startswith(b"OggS"):
        return "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"fLaC"):
        return "flac"
    return None


def _is_mpeg_audio_frame(head: bytes) -> bool:
    """A frame header: 11 sync bits, then no reserved version (01), layer (00), bitrate index
    (1111) or sample rate index (11). The layer check also keeps ADTS AAC (layer 00) out, and
    the sync mask keeps out a JPEG (``FF D8``)."""
    if len(head) < 3 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return False
    version = (head[1] >> 3) & 0b11
    layer = (head[1] >> 1) & 0b11
    bitrate_index = head[2] >> 4
    sample_rate_index = (head[2] >> 2) & 0b11
    return (
        version != 0b01 and layer != 0b00 and bitrate_index != 0b1111 and sample_rate_index != 0b11
    )


def _ftyp_kind(brand: bytes) -> MediaKind:
    if brand in _M4A_BRANDS:
        return "m4a"
    if brand == _MOV_BRAND:
        return "mov"
    return "mp4"


def _ebml_kind(head: bytes) -> MediaKind:
    """``webm`` when the EBML DocType says so, else Matroska (also when the head is too short to
    show it)."""
    window = head[:_EBML_HEADER_LIMIT]
    at = window.find(_EBML_DOCTYPE_ID, len(_EBML_MAGIC))
    if at >= 0 and at + 2 < len(window):
        size = window[at + 2] & 0x7F  # a one-byte size: "webm" and "matroska" are short
        if window[at + 3 : at + 3 + size] == b"webm":
            return "webm"
    return "mkv"
