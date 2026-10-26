import random
import struct
from typing import get_args

import pytest

from text_extraction.media.sniff import (
    MEDIA_HEAD_BYTES,
    is_declared_media,
    media_type_of,
    sniff_media,
)
from text_extraction.media.types import MediaKind

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64


def ftyp(brand: bytes) -> bytes:
    return (
        struct.pack(">I", 24) + b"ftyp" + brand + b"\x00\x00\x02\x00" + b"isomiso2" + b"\x00" * 32
    )


def ebml(doctype: bytes) -> bytes:
    body = b"\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82" + bytes([0x80 | len(doctype)]) + doctype
    body += b"\x42\x87\x81\x04\x42\x85\x81\x02"
    return (
        b"\x1a\x45\xdf\xa3" + bytes([0x80 | len(body)]) + body + b"\x18\x53\x80\x67" + b"\x00" * 40
    )


SAMPLES: list[tuple[str, bytes, MediaKind]] = [
    ("mp3 id3", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64, "mp3"),
    ("mp3 frame mpeg1 l3", b"\xff\xfb\x90\x64" + b"\x00" * 64, "mp3"),
    ("mp3 frame mpeg2 l3", b"\xff\xf3\x90\x64" + b"\x00" * 64, "mp3"),
    ("mp3 frame mpeg1 l2", b"\xff\xfd\x90\x64" + b"\x00" * 64, "mp3"),
    ("adts mpeg4", b"\xff\xf1\x50\x80\x02\x1f\xfc" + b"\x00" * 64, "aac"),
    ("adts mpeg2", b"\xff\xf9\x50\x80\x02\x1f\xfc" + b"\x00" * 64, "aac"),
    ("mp4 isom", ftyp(b"isom"), "mp4"),
    ("mp4 mp42", ftyp(b"mp42"), "mp4"),
    ("mp4 dash", ftyp(b"dash"), "mp4"),
    ("mp4 iso5", ftyp(b"iso5"), "mp4"),
    ("m4a", ftyp(b"M4A "), "m4a"),
    ("m4b", ftyp(b"M4B "), "m4a"),
    ("mov", ftyp(b"qt  "), "mov"),
    ("webm", ebml(b"webm"), "webm"),
    ("mkv", ebml(b"matroska"), "mkv"),
    ("ogg", b"OggS\x00\x02" + b"\x00" * 64, "ogg"),
    ("wav", b"RIFF" + struct.pack("<I", 1000) + b"WAVEfmt " + b"\x00" * 32, "wav"),
    ("flac", b"fLaC\x00\x00\x00\x22" + b"\x00" * 64, "flac"),
]


@pytest.mark.parametrize(("name", "head", "kind"), SAMPLES, ids=[s[0] for s in SAMPLES])
def test_sniff_kinds(name: str, head: bytes, kind: MediaKind) -> None:
    assert sniff_media(head, None) == kind


def test_every_media_kind_is_covered() -> None:
    assert {kind for _, _, kind in SAMPLES} == set(get_args(MediaKind))


@pytest.mark.parametrize("declared", [None, "audio/mpeg", "video/mp4", "application/pdf", ""])
def test_declared_type_does_not_override_the_bytes(declared: str | None) -> None:
    assert sniff_media(ftyp(b"M4A "), declared) == "m4a"
    assert sniff_media(PDF, declared) is None


def test_random_bytes_and_a_pdf_are_not_media() -> None:
    rng = random.Random(7)  # noqa: S311 - a fixed seed, not a secret
    assert sniff_media(PDF, "audio/mpeg") is None
    for _ in range(200):
        blob = bytes(rng.randrange(256) for _ in range(256))
        if blob[0] != 0xFF:  # a random blob can begin with a valid frame sync by chance
            assert sniff_media(blob, None) is None


def test_jpeg_is_not_an_mp3_frame() -> None:
    assert sniff_media(JPEG, None) is None
    assert sniff_media(JPEG, "audio/mpeg") is None


@pytest.mark.parametrize(
    "head",
    [
        b"",
        b"\xff",
        b"\xff\xfb",
        b"\xff\xe0\x00\x00",  # reserved version and layer bits
        b"\xff\xfb\xf0\x00",  # reserved bitrate index 15
        b"\xff\xfb\x9c\x00",  # reserved sample rate index 3
        b"\xff\xe9\x90\x00",  # reserved version 01
        b"\x00\x00\x00\x18ftyp",  # ftyp without a brand
        b"RIFF\x00\x00\x00\x00AVI LIST",  # RIFF, but not WAVE
        b"\x1a\x45\xdf",  # EBML signature cut short
    ],
)
def test_truncated_and_reserved_headers_are_not_media(head: bytes) -> None:
    assert sniff_media(head, None) is None


def test_ebml_without_a_doctype_in_the_head_is_matroska() -> None:
    assert sniff_media(b"\x1a\x45\xdf\xa3" + b"\x00" * 100, None) == "mkv"


def test_media_types() -> None:
    expected: dict[MediaKind, str] = {
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
    assert {kind: media_type_of(kind) for kind in expected} == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("audio/mpeg", True),
        ("audio/x-wav", True),
        ("video/mp4", True),
        ("Video/WebM; codecs=vp9", True),
        ("application/ogg", True),
        (" application/ogg ;x=y", True),
        ("application/octet-stream", False),
        ("application/pdf", False),
        ("text/html; charset=utf-8", False),
        ("audioish/x", False),
        ("", False),
        (None, False),
    ],
)
def test_is_declared_media(value: str | None, expected: bool) -> None:
    assert is_declared_media(value) is expected


def test_head_is_64_kib() -> None:
    assert MEDIA_HEAD_BYTES == 65_536


def mp3_frames(count: int, *, header: bytes = b"\xff\xfb\x90\x00") -> bytes:
    """``count`` MPEG-1 Layer III frames at 128 kbit/s and 44.1 kHz (417 bytes each)."""
    return (header + b"\x00" * (417 - len(header))) * count


def test_a_real_two_frame_mp3_head_is_mp3() -> None:
    assert sniff_media(mp3_frames(2), None) == "mp3"
    assert sniff_media(mp3_frames(8) + b"junk", "audio/mpeg") == "mp3"


def test_an_mp3_whose_second_frame_does_not_follow_is_not_mp3() -> None:
    head = mp3_frames(1) + b"\x00" * 200  # no frame header where the next frame must start
    assert sniff_media(head, None) is None
    other_rate = mp3_frames(1) + b"\xff\xfb\x94\x00" + b"\x00" * 200  # 48 kHz: not consistent
    assert sniff_media(other_rate, None) is None


@pytest.mark.parametrize(
    "text",
    ["hello world", "Größe und Ähnlichkeit, ein längerer Text\n" * 20, "ID3 is a tag"],
    ids=["short", "long", "id3"],
)
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_utf16_text_is_not_mp3(text: str, encoding: str) -> None:
    encoded = text.encode(encoding)
    if encoding in ("utf-16-le", "utf-16-be"):
        encoded = ("﻿" + text).encode(encoding)  # with the byte order mark spelled out
    assert sniff_media(encoded, None) is None
    assert sniff_media(b"\xff\xfe" + b"\x00" * 64, None) is None
    assert sniff_media(b"\xfe\xff" + b"\x00" * 64, None) is None
    assert sniff_media(b"\xff\xff" + b"\x00" * 64, None) is None


@pytest.mark.parametrize(
    "head",
    [
        b"ID3,Name,Value\n1,a,b\n",
        b"ID3 plain text that merely starts like a tag",
        b"ID3\x05\x00\x00\x00\x00\x00\x00",  # major version 5 does not exist
        b"ID3\x01\x00\x00\x00\x00\x00\x00",
        b"ID3\x04\xff\x00\x00\x00\x00\x00",  # revision 0xFF
        b"ID3\x04\x00\x00\x00\x00\x80\x00",  # a size byte with the high bit set
        b"ID3\x04\x00\x00",  # header cut short
    ],
)
def test_text_starting_with_id3_is_not_mp3(head: bytes) -> None:
    assert sniff_media(head, None) is None


@pytest.mark.parametrize("version", [2, 3, 4])
def test_id3v2_versions_are_mp3(version: int) -> None:
    assert (
        sniff_media(b"ID3" + bytes([version, 0, 0, 0, 0, 0x7F, 0x7F]) + b"\x00" * 32, None) == "mp3"
    )


@pytest.mark.parametrize(
    "brand", [b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"avif", b"avis", b"XXXX", b"    "]
)
def test_image_and_unknown_ftyp_brands_are_not_media(brand: bytes) -> None:
    assert sniff_media(ftyp(brand), None) is None


VIDEO_BRANDS: tuple[bytes, ...] = (
    *(b"isom", b"iso2", b"iso9", b"mp41", b"mp42", b"mp4v", b"avc1", b"dash"),
    *(b"M4V ", b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"f4v "),
)


@pytest.mark.parametrize("brand", VIDEO_BRANDS)
def test_video_ftyp_brands_are_mp4(brand: bytes) -> None:
    assert sniff_media(ftyp(brand), None) == "mp4"
