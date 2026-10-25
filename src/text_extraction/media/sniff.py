"""Decide whether a body is an audio or video file, by its first bytes.

Only the head of a file is needed (``MEDIA_HEAD_BYTES``): a fetched response declared as audio
or video is read no further than that, so a large file is recognised without being downloaded.
The bytes decide; the declared type never overrides them. Nothing here parses a container
beyond the few bytes that name its format.
"""

from typing import Final, Literal

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

# ``ftyp`` major brands. Only video and audio brands are media: HEIC, HEIF and AVIF images carry
# ``ftyp`` too (heic, heix, hevc, mif1, msf1, avif, avis), so an unknown brand is not media.
_M4A_BRANDS: Final = frozenset({b"M4A ", b"M4B "})
_MOV_BRANDS: Final = frozenset({b"qt  "})
_MP4_BRANDS: Final = frozenset(
    {
        *(f"iso{digit}".encode() for digit in range(2, 10)),
        b"isom",
        b"mp41",
        b"mp42",
        b"mp4v",
        b"avc1",
        b"dash",
        b"M4V ",
        b"3gp4",
        b"3gp5",
        b"3gp6",
        b"3g2a",
        b"f4v ",
    }
)
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
    if _is_id3v2(head) or _is_mpeg_audio_frame(head):
        return "mp3"
    if head.startswith(_ADTS_SYNCS):
        return "aac"
    if head[4:8] == b"ftyp":
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


def _is_id3v2(head: bytes) -> bool:
    """An ID3v2 tag header: ``ID3``, major version 2 to 4, a revision other than 0xFF and four
    syncsafe size bytes (each below 0x80). Text that merely starts with ``ID3`` fails this."""
    return (
        len(head) >= 10
        and head.startswith(b"ID3")
        and 2 <= head[3] <= 4
        and head[4] != 0xFF
        and all(byte < 0x80 for byte in head[6:10])
    )


# MPEG audio bitrates (kbit/s) by bitrate index 1 to 14, for Layer II and Layer III; free format
# (index 0) has no fixed frame length. Layer I is not accepted at all: it is practically unused
# and the UTF-16 byte order marks (FF FE, FF FF) would pass as its headers.
_BITRATES_V1_L2: Final = (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384)
_BITRATES_V1_L3: Final = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BITRATES_V2: Final = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_SAMPLE_RATES: Final = {0b11: (44100, 48000, 32000), 0b10: (22050, 24000, 16000)}
_SAMPLE_RATES_V25: Final = (11025, 12000, 8000)


def _frame_length(header: bytes) -> int | Literal[False] | None:
    """Length in bytes of the Layer II or III frame that ``header`` opens, ``None`` for free
    format (no fixed length), ``False`` when ``header`` is not a valid frame header."""
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return False
    version = (header[1] >> 3) & 0b11  # 11 MPEG-1, 10 MPEG-2, 00 MPEG-2.5, 01 reserved
    layer = (header[1] >> 1) & 0b11  # 10 Layer II, 01 Layer III; 11 Layer I and 00 refused
    bitrate_index = header[2] >> 4
    sample_rate_index = (header[2] >> 2) & 0b11
    if (
        version == 0b01
        or layer in (0b00, 0b11)
        or bitrate_index == 0b1111
        or sample_rate_index == 3
    ):
        return False
    sample_rates = _SAMPLE_RATES_V25 if version == 0b00 else _SAMPLE_RATES[version]
    if bitrate_index == 0:
        return None
    if version == 0b11:
        bitrate = (_BITRATES_V1_L2 if layer == 0b10 else _BITRATES_V1_L3)[bitrate_index - 1]
    else:
        bitrate = _BITRATES_V2[bitrate_index - 1]
    samples_per_frame_div8 = 72 if version != 0b11 and layer == 0b01 else 144
    padding = (header[2] >> 1) & 1
    return samples_per_frame_div8 * bitrate * 1000 // sample_rates[sample_rate_index] + padding


def _is_mpeg_audio_frame(head: bytes) -> bool:
    """A Layer II or III frame header at offset 0 (see ``_frame_length``); when the head reaches
    past the frame, the next frame must open with a header of the same version, layer and sample
    rate. A JPEG (``FF D8``) fails the sync mask, ADTS AAC (layer 00) and UTF-16 text with a
    byte order mark fail the header checks."""
    length = _frame_length(head[:4])
    if length is False:
        return False
    if length is None or len(head) < length + 4:
        return True
    following = head[length : length + 4]
    if _frame_length(following) is False:
        return False
    return (following[1] & 0b00011110, following[2] & 0b00001100) == (
        head[1] & 0b00011110,
        head[2] & 0b00001100,
    )


def _ftyp_kind(brand: bytes) -> MediaKind | None:
    if brand in _M4A_BRANDS:
        return "m4a"
    if brand in _MOV_BRANDS:
        return "mov"
    if brand in _MP4_BRANDS:
        return "mp4"
    return None


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
