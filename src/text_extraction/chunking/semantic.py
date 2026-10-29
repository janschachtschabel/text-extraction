"""The ``semantic`` chunker (rulings S10 and D1): cut where the meaning shifts.

The sentences are embedded (the caller passes ``embed``, in the service the LLM client's
``/embeddings`` call), the cosine distance of each sentence to the next is computed, and the text
is cut where that distance is above the ``breakpoint_percentile`` of all distances. A group over
``size`` is split by ``recursive``. No LLM client is imported here.
"""

from collections.abc import Awaitable, Callable, Sequence

import numpy as np
import numpy.typing as npt

from text_extraction.chunking.split import (
    WARNING_FALLBACK_RECURSIVE,
    Span,
    build_chunks,
    recursive_spans,
    split_sentences,
)
from text_extraction.chunking.types import Chunk, ChunkingOptions

# More sentences than this are not embedded (the cost of the calls): `recursive` instead.
MAX_SEMANTIC_SENTENCES = 5000

# Distances closer to the threshold than float noise are no shift in meaning.
_EPSILON = 1e-9

Embed = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


def _distances(vectors: list[list[float]]) -> npt.NDArray[np.float64]:
    """The cosine distance of each vector to the next (a zero vector is at distance 1)."""
    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2:
        raise ValueError("The embeddings must all have the same length.")
    norms = np.linalg.norm(matrix, axis=1)
    dots = np.einsum("ij,ij->i", matrix[:-1], matrix[1:])
    denominators = norms[:-1] * norms[1:]
    similarities = np.divide(dots, denominators, out=np.zeros_like(dots), where=denominators > 0)
    distances: npt.NDArray[np.float64] = np.clip(1.0 - similarities, 0.0, 2.0)
    return distances


async def semantic_chunks(
    content: str,
    options: ChunkingOptions,
    embed: Embed,
    *,
    markdown: bool,
    max_chunks: int,
) -> tuple[list[Chunk], list[str]]:
    """Cut `content` where the embeddings of neighbouring sentences drift apart. Returns the
    chunks and the warnings (``chunking_fallback_recursive`` for more than 5,000 sentences,
    ``chunks_truncated`` at `max_chunks`). `embed` errors propagate."""
    size, overlap = options.size, options.overlap
    sentences = split_sentences(content)
    if not sentences:
        return [], []
    if len(sentences) > MAX_SEMANTIC_SENTENCES:
        spans = recursive_spans(content, 0, len(content), size, overlap)
        chunks, warnings = build_chunks(content, spans, markdown=markdown, max_chunks=max_chunks)
        return chunks, [WARNING_FALLBACK_RECURSIVE, *warnings]
    groups: list[Span] = []
    if len(sentences) == 1:
        groups = sentences
    else:
        vectors = await embed([content[start:end] for start, end in sentences])
        if len(vectors) != len(sentences):
            raise ValueError("The number of embeddings does not match the number of sentences.")
        distances = _distances(vectors)
        threshold = float(np.percentile(distances, options.breakpoint_percentile))
        group_start = sentences[0][0]
        for number, distance in enumerate(distances):
            if distance > threshold and distance > _EPSILON:
                groups.append((group_start, sentences[number][1]))
                group_start = sentences[number + 1][0]
        groups.append((group_start, sentences[-1][1]))
    cuts: list[Span] = []
    for start, end in groups:
        if end - start <= size:
            cuts.append((start, end))
        else:
            cuts.extend(recursive_spans(content, start, end, size, overlap))
    return build_chunks(content, cuts, markdown=markdown, max_chunks=max_chunks)
