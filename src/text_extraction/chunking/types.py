"""The ``chunking`` option of a request and the chunks of a result (ruling S1, S10).

Kept free of imports from the rest of the package, so that the request, job and result models
can use them.
"""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

ChunkStrategy = Literal["fixed", "recursive", "markdown", "sentence", "semantic"]

MIN_SIZE = 200
MAX_SIZE = 20_000


class ChunkingOptions(BaseModel, extra="forbid", hide_input_in_errors=True, strict=True):
    """The ``chunking`` option of a request. Sizes are characters (code points)."""

    strategy: ChunkStrategy = Field(
        default="recursive",
        description="`fixed`: windows of `size` characters. `recursive`: split at paragraphs, "
        "then lines, sentences and words, and pack the pieces up to `size`. `markdown`: split at "
        "headings first (needs `format: markdown`; `format: text` falls back to `recursive` with "
        "the warning `chunking_fallback_recursive`). `sentence`: whole sentences up to `size`. "
        "`semantic`: cut where the meaning shifts, measured with embeddings (400 while the "
        "service has no embedding model).",
    )
    size: int = Field(
        default=1000,
        ge=MIN_SIZE,
        le=MAX_SIZE,
        description="The most characters in a chunk, 200 to 20,000.",
    )
    overlap: int = Field(
        default=100,
        ge=0,
        description="Characters that consecutive chunks share, at most half of `size`.",
    )
    breakpoint_percentile: int = Field(
        default=90,
        ge=50,
        le=99,
        description="`semantic` only: a cut is made where the distance between neighbouring "
        "sentences is above this percentile of all distances.",
    )

    @model_validator(mode="after")
    def _overlap_within_half_the_size(self) -> Self:
        if self.overlap > self.size // 2:
            raise ValueError(f"overlap must be at most half of size ({self.size // 2})")
        return self


class Chunk(BaseModel):
    """One chunk of ``content``: ``content[start:end] == text`` (code point offsets)."""

    index: int = Field(description="Position in the list, from 0.")
    text: str
    start: int = Field(description="Offset of the first character in `content`.")
    end: int = Field(description="Offset after the last character in `content`.")
    headings: list[str] = Field(
        description="The Markdown heading path at the chunk's start (text without `#`); empty "
        "for `format: text`."
    )
