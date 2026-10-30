"""The ``semantic`` chunker (rulings S10 and D1): cut where the meaning shifts.

The sentences are embedded (the caller passes ``embed``, in the service the LLM client's
``/embeddings`` calls), the cosine distance of each sentence to the next is computed, and the text
is cut where that distance is above the ``breakpoint_percentile`` of all distances. A group over
``size`` is split by ``recursive``. No LLM client is imported here.

Memory (ruling R22): ``embed`` yields the vectors batch by batch as float32 arrays, and only the
distances are kept: each batch's are computed as it comes, with the last vector of the batch
before, so a result never holds all vectors (5,000 sentences at 3,072 dimensions would be 61 MB
as float32 and about 500 MB as Python floats). CPU work (sentences, distances, groups, chunks)
runs on ``threads``, the post pool (``POST_CONCURRENCY``, R21), never on the event loop.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from functools import partial
from typing import Any, Protocol

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

# The embeddings of the texts, in order, as float32 arrays of one or more rows each.
Embed = Callable[[Sequence[str]], AsyncGenerator[npt.NDArray[np.float32]]]


class Offload(Protocol):
    """Runs ``function(*args)`` off the event loop (``ConversionPool.run``)."""

    def __call__[T](self, function: Callable[..., T], /, *args: Any) -> Awaitable[T]: ...


def batch_distances(
    previous: npt.NDArray[np.float32] | None, batch: npt.NDArray[np.float32]
) -> npt.NDArray[np.float64]:
    """The cosine distance of each vector of ``batch`` to the one before it (``previous``, the
    last vector of the batch before, for the first; none without it). A zero vector is at
    distance 1."""
    rows = batch if previous is None else np.vstack([previous[np.newaxis, :], batch])
    matrix = rows.astype(np.float64)
    norms = np.linalg.norm(matrix, axis=1)
    dots = np.einsum("ij,ij->i", matrix[:-1], matrix[1:])
    denominators = norms[:-1] * norms[1:]
    similarities = np.divide(dots, denominators, out=np.zeros_like(dots), where=denominators > 0)
    distances: npt.NDArray[np.float64] = np.clip(1.0 - similarities, 0.0, 2.0)
    return distances


async def _distances(embed: Embed, texts: list[str], threads: Offload) -> npt.NDArray[np.float64]:
    """The distance of each text's embedding to the next one's, batch by batch."""
    parts: list[npt.NDArray[np.float64]] = []
    previous: npt.NDArray[np.float32] | None = None
    count = 0
    async with aclosing(embed(texts)) as batches:
        async for batch in batches:
            if batch.ndim != 2 or (previous is not None and batch.shape[1] != previous.shape[0]):
                raise ValueError("The embeddings must all have the same length.")
            if not len(batch):
                continue
            count += len(batch)
            parts.append(await threads(batch_distances, previous, batch))
            previous = batch[-1].copy()  # not a view: the batch itself is dropped
    if count != len(texts):
        raise ValueError("The number of embeddings does not match the number of sentences.")
    return np.concatenate(parts) if parts else np.zeros(0)


def _groups(
    sentences: list[Span], distances: npt.NDArray[np.float64], percentile: float
) -> list[Span]:
    """The sentences grouped: a cut after each distance above the percentile."""
    threshold = float(np.percentile(distances, percentile))
    groups: list[Span] = []
    group_start = sentences[0][0]
    for number, distance in enumerate(distances):
        if distance > threshold and distance > _EPSILON:
            groups.append((group_start, sentences[number][1]))
            group_start = sentences[number + 1][0]
    groups.append((group_start, sentences[-1][1]))
    return groups


async def semantic_chunks(
    content: str,
    options: ChunkingOptions,
    embed: Embed,
    *,
    markdown: bool,
    max_chunks: int,
    threads: Offload,
) -> tuple[list[Chunk], list[str]]:
    """Cut `content` where the embeddings of neighbouring sentences drift apart. Returns the
    chunks and the warnings (``chunking_fallback_recursive`` for more than 5,000 sentences,
    ``chunks_truncated`` at `max_chunks`). `embed` errors propagate; embeddings of another count
    or length than the sentences are a ``ValueError``. ``threads`` runs the CPU work."""
    size, overlap = options.size, options.overlap
    build = partial(build_chunks, markdown=markdown, max_chunks=max_chunks)
    sentences = await threads(split_sentences, content)
    if not sentences:
        return [], []
    if len(sentences) > MAX_SEMANTIC_SENTENCES:
        spans = await threads(recursive_spans, content, 0, len(content), size, overlap)
        chunks, warnings = await threads(build, content, spans)
        return chunks, [WARNING_FALLBACK_RECURSIVE, *warnings]
    groups: list[Span] = sentences
    if len(sentences) > 1:
        texts = [content[start:end] for start, end in sentences]
        distances = await _distances(embed, texts, threads)
        groups = await threads(_groups, sentences, distances, options.breakpoint_percentile)
    cuts: list[Span] = []
    for start, end in groups:
        if end - start <= size:
            cuts.append((start, end))
        else:
            cuts.extend(await threads(recursive_spans, content, start, end, size, overlap))
    return await threads(build, content, cuts)
