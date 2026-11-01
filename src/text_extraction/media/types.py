"""Types shared by the media code, the request models and the result models.

Kept free of imports from the rest of the package, so that the models can use them.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

# What the bytes of an audio or video file say it is (``media.sniff``).
MediaKind = Literal["mp3", "mp4", "m4a", "mov", "webm", "mkv", "ogg", "wav", "flac", "aac"]

# Where the media came from: a supported platform, a media file fetched by URL, an upload, or an
# HTML5 player of a page.
Platform = Literal["youtube", "vimeo", "ard", "zdf", "arte", "3sat", "file", "upload", "html5"]

# Where the transcript comes from: a person's subtitles, the platform's automatic captions,
# speech recognition, YouTube's new transcript view read in the browser (which does not say
# whether the track is a person's or automatic), or nowhere (the result carries the metadata only).
TranscriptSource = Literal[
    "manual_subtitles", "auto_subtitles", "asr", "transcript_panel", "metadata_only"
]

MAX_EMBEDS = 10
MAX_LANGUAGES = 5


def _lower_primary_subtag(value: object) -> object:
    """``DE`` becomes ``de`` and ``EN-gb`` becomes ``en-gb``: only the primary subtag is
    lower-cased, so ``en-GB`` keeps its region."""
    if isinstance(value, str):
        primary, dash, rest = value.partition("-")
        return f"{primary.lower()}{dash}{rest}"
    return value


# An ISO 639 language code with an optional subtag (``de``, ``en-GB``, ``zh-Hans``).
LanguageCode = Annotated[
    str,
    BeforeValidator(_lower_primary_subtag),
    StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$"),
]


class MediaEmbed(BaseModel):
    """A player on a page: a platform's video (``kind`` ``platform``, ``platform`` set) or a
    media file (``kind`` ``file``). The caller can extract it with a request of its own."""

    url: str
    kind: Literal["platform", "file"]
    platform: Platform | None = None


class MediaInfo(BaseModel):
    """What a result says about the audio or video it was extracted from. ``transcript_source``
    is None where there is no transcript at all: a page whose players have no track we could
    read (only ``embeds``), or a media file that was not transcribed."""

    platform: Platform
    transcript_source: TranscriptSource | None = Field(
        description="Where the transcript comes from: `manual_subtitles`, `auto_subtitles` "
        "(the platform's automatic captions), `asr` (speech recognition), `transcript_panel` "
        "(YouTube's transcript view, read in the browser when the subtitles could not be: it "
        "does not say whether a person or a machine wrote it), `metadata_only` (no transcript: "
        "the video's details); null without any (a page's players without a readable track, "
        "an untranscribed media file)."
    )
    transcript_language: str | None = None
    available_languages: list[str] = []  # the manual subtitle tracks
    duration_seconds: float | None = None
    uploader: str | None = None
    asr_model: str | None = None  # speech recognition only
    embeds: list[MediaEmbed] = Field(default=[], max_length=MAX_EMBEDS)
