"""The ``semantic`` chunker of ``chunking/semantic.py`` (rulings S10 and D1): cuts where the cosine
distance of neighbouring sentence embeddings peaks, with a fake `embed` in place of the endpoint."""

import asyncio
import hashlib
import random
from collections.abc import Sequence

import pytest

from tests.chunking.test_split import assert_invariants, documents
from text_extraction.chunking.semantic import MAX_SEMANTIC_SENTENCES, semantic_chunks
from text_extraction.chunking.split import chunk
from text_extraction.chunking.types import Chunk, ChunkingOptions


class FakeEmbed:
    """Cats are [1, 0, 0], dogs [0, 1, 0], birds [0, 0, 1]; anything else a hash-seeded vector."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            if "Cats" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "Dogs" in text:
                vectors.append([0.0, 1.0, 0.0])
            elif "Birds" in text:
                vectors.append([0.0, 0.0, 1.0])
            else:
                seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8])
                rng = random.Random(seed)  # noqa: S311
                vectors.append([rng.random() for _ in range(4)])
        return vectors


def run(
    content: str,
    embed: FakeEmbed,
    *,
    size: int = 1000,
    overlap: int = 0,
    percentile: int = 90,
    markdown: bool = False,
    max_chunks: int = 2000,
) -> tuple[list[Chunk], list[str]]:
    options = ChunkingOptions(
        strategy="semantic", size=size, overlap=overlap, breakpoint_percentile=percentile
    )
    return asyncio.run(
        semantic_chunks(content, options, embed, markdown=markdown, max_chunks=max_chunks)
    )


TWO_TOPICS = "Cats purr. Cats sleep. Cats hunt. Dogs bark. Dogs run. Dogs fetch."


def test_semantic_cuts_at_distance_peaks() -> None:
    embed = FakeEmbed()
    chunks, warnings = run(TWO_TOPICS, embed)
    assert warnings == []
    assert [item.text for item in chunks] == [
        "Cats purr. Cats sleep. Cats hunt.",
        "Dogs bark. Dogs run. Dogs fetch.",
    ]
    assert [(item.start, item.end) for item in chunks] == [(0, 33), (34, 66)]
    # One call, one text per sentence.
    assert embed.calls == [
        ["Cats purr.", "Cats sleep.", "Cats hunt.", "Dogs bark.", "Dogs run.", "Dogs fetch."]
    ]


def test_semantic_three_topics_and_the_percentile() -> None:
    content = "Cats a. Cats b. Dogs a. Dogs b. Birds a. Birds b."
    chunks, _ = run(content, FakeEmbed(), percentile=50)
    assert [item.text for item in chunks] == [
        "Cats a. Cats b.",
        "Dogs a. Dogs b.",
        "Birds a. Birds b.",
    ]
    # Distances 0, 1, 0, 1, 0: the 90th percentile is 1 and a cut needs a distance above it.
    chunks, _ = run(content, FakeEmbed(), percentile=90)
    assert [item.text for item in chunks] == [content]


def test_semantic_without_any_shift_is_one_chunk() -> None:
    chunks, _ = run("Cats a. Cats b. Cats c. Cats d.", FakeEmbed())
    assert [item.text for item in chunks] == ["Cats a. Cats b. Cats c. Cats d."]


def test_semantic_group_over_size_is_split_by_recursive() -> None:
    cats = " ".join(f"Cats sentence number {number} is here." for number in range(30))
    content = f"{cats} Dogs bark."
    chunks, warnings = run(content, FakeEmbed(), size=300, overlap=50)
    assert warnings == []
    assert chunks[-1].text == "Dogs bark."
    assert len(chunks) > 3
    assert_invariants(content, chunks, 300, 50)


def test_semantic_headings_follow_the_chunk_start() -> None:
    content = "# Cats\n\nCats purr. Cats sleep.\n\n## Dogs\n\nDogs bark. Dogs run."
    chunks, _ = run(content, FakeEmbed(), markdown=True)
    assert chunks[0].headings == ["Cats"]
    assert chunks[-1].headings == ["Cats", "Dogs"]


def test_semantic_over_5000_sentences_falls_back() -> None:
    content = " ".join(f"Cats item {number} done." for number in range(MAX_SEMANTIC_SENTENCES + 1))
    embed = FakeEmbed()
    options = ChunkingOptions(strategy="semantic", size=1000, overlap=100)
    chunks, warnings = asyncio.run(
        semantic_chunks(content, options, embed, markdown=False, max_chunks=2000)
    )
    assert warnings == ["chunking_fallback_recursive"]
    assert embed.calls == []
    expected, _ = chunk(
        content,
        ChunkingOptions(strategy="recursive", size=1000, overlap=100),
        markdown=False,
        max_chunks=2000,
    )
    assert chunks == expected


def test_semantic_exactly_5000_sentences_is_embedded() -> None:
    content = " ".join(f"Cats item {number} done." for number in range(MAX_SEMANTIC_SENTENCES))
    embed = FakeEmbed()
    _, warnings = run(content, embed, max_chunks=10_000)
    assert warnings == []
    assert len(embed.calls) == 1
    assert len(embed.calls[0]) == MAX_SEMANTIC_SENTENCES


def test_semantic_empty_and_single_sentence_need_no_embedding() -> None:
    embed = FakeEmbed()
    assert run("", embed) == ([], [])
    assert run("  \n ", embed) == ([], [])
    chunks, _ = run("Only one sentence", embed)
    assert [item.text for item in chunks] == ["Only one sentence"]
    assert embed.calls == []


def test_semantic_max_chunks_truncates_with_warning() -> None:
    content = " ".join(
        f"{topic} sentence {number} done."
        for topic in ("Cats", "Dogs", "Birds")
        for number in range(4)
    )
    everything, _ = run(content, FakeEmbed(), percentile=50)
    assert len(everything) == 3
    chunks, warnings = run(content, FakeEmbed(), percentile=50, max_chunks=2)
    assert chunks == everything[:2]
    assert len(chunks) == 2
    assert warnings == ["chunks_truncated"]


def test_semantic_rejects_a_wrong_number_of_embeddings() -> None:
    async def short(texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0]] * (len(texts) - 1)

    options = ChunkingOptions(strategy="semantic")
    with pytest.raises(ValueError, match="embeddings"):
        asyncio.run(semantic_chunks(TWO_TOPICS, options, short, markdown=False, max_chunks=10))


def test_semantic_zero_vectors_do_not_break_the_cosine() -> None:
    async def zeros(texts: Sequence[str]) -> list[list[float]]:
        return [[0.0, 0.0] for _ in texts]

    options = ChunkingOptions(strategy="semantic")
    chunks, _ = asyncio.run(
        semantic_chunks(TWO_TOPICS, options, zeros, markdown=False, max_chunks=10)
    )
    assert [item.text for item in chunks] == [TWO_TOPICS]


@pytest.mark.parametrize("size", [200, 1000])
@pytest.mark.parametrize("half_overlap", [False, True])
def test_semantic_keeps_the_invariants(size: int, half_overlap: bool) -> None:
    overlap = size // 2 if half_overlap else 0
    for content in documents():
        chunks, warnings = run(content, FakeEmbed(), size=size, overlap=overlap, markdown=True)
        assert_invariants(content, chunks, size, overlap)
        assert set(warnings) <= {"chunking_fallback_recursive"}
