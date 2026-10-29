"""The chunkers (ruling S10): ``fixed``, ``recursive``, ``markdown`` and ``sentence``.

Pure functions on a string, no I/O. Every chunk satisfies ``text == content[start:end]`` (code
points); the chunks are in order; none is empty or longer than ``size``; neighbours share at most
``overlap`` characters; and only whitespace between chunks is left out. The strategies work on
*atoms* (trimmed spans of at most ``size`` characters, whitespace-only gaps between them) that a
single packer joins, so the invariants hold in one place. Every step is linear in the content.

``semantic`` needs embeddings and lives in ``semantic.py``, on top of the helpers here.
"""

import re
import unicodedata
from bisect import bisect_right

from text_extraction.chunking.types import Chunk, ChunkingOptions

WARNING_TRUNCATED = "chunks_truncated"
WARNING_FALLBACK_RECURSIVE = "chunking_fallback_recursive"

Span = tuple[int, int]

# The separators of `recursive`, coarse to fine; the last resort is a cut into windows. A piece
# keeps the separator that ends it, so the pieces tile the text. Blank lines are matched with
# CRLF and trailing blanks in mind, sentence ends with the terminators and closers of the sentence
# splitter. The lookbehind makes a run of terminators one attempt, not one per character.
_SEPARATORS = (
    re.compile(r"\n(?:[ \t\r\f\v]*\n)+"),
    re.compile(r"\n"),
    re.compile(r"(?<![.!?…])[.!?…]+[\"'\u201d\u2019»)\]]*\s+"),
    re.compile(r"\s+"),
)

_MAX_RETREAT = 16
_ZERO_WIDTH_JOINER = "‍"


def _trim(content: str, start: int, end: int) -> Span | None:
    """``(start, end)`` without the whitespace at both ends; None when nothing is left."""
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _extends(previous: str, character: str) -> bool:
    """True when `character` belongs to the same user-perceived character as `previous`."""
    return (
        previous == _ZERO_WIDTH_JOINER
        or character == _ZERO_WIDTH_JOINER
        or unicodedata.category(character)[0] == "M"
        or "\U0001f3fb" <= character <= "\U0001f3ff"
    )


def _safe_cut(content: str, start: int, cut: int) -> int:
    """`cut` moved back, by a few characters at most, out of the middle of a combining sequence
    or an emoji joined with a zero-width joiner; `cut` itself when there is no such place."""
    candidate = cut
    for _ in range(_MAX_RETREAT):
        if candidate - 1 <= start:
            return cut
        if not _extends(content[candidate - 1], content[candidate]):
            return candidate
        candidate -= 1
    return cut


def _hard_cuts(content: str, start: int, end: int, limit: int) -> list[Span]:
    """Windows of at most `limit` characters over a run without separators."""
    atoms: list[Span] = []
    position = start
    while position < end:
        cut = position + limit
        cut = end if cut >= end else _safe_cut(content, position, cut)
        trimmed = _trim(content, position, cut)
        if trimmed is not None:
            atoms.append(trimmed)
        position = cut
    return atoms


def _split(content: str, start: int, end: int, level: int, limit: int) -> list[Span]:
    """Atoms of ``content[start:end]``, each at most `limit` characters: cut at the first
    separator from `level` on that occurs, pieces that are still too long recurse with the finer
    separators, and a piece without any separator is cut into windows."""
    trimmed = _trim(content, start, end)
    if trimmed is None:
        return []
    start, end = trimmed
    if end - start <= limit:
        return [trimmed]
    for current in range(level, len(_SEPARATORS)):
        pieces: list[Span] = []
        position = start
        for match in _SEPARATORS[current].finditer(content, start, end):
            if match.end() >= end:
                break
            pieces.append((position, match.end()))
            position = match.end()
        if pieces:
            pieces.append((position, end))
            atoms: list[Span] = []
            for piece_start, piece_end in pieces:
                atoms.extend(_split(content, piece_start, piece_end, current + 1, limit))
            return atoms
    return _hard_cuts(content, start, end, limit)


def _pack(atoms: list[Span], size: int, overlap: int) -> list[Span]:
    """Join neighbouring atoms into chunks of at most `size` characters. The next chunk starts
    at the earliest atom that is at most `overlap` characters before the end of the chunk before
    (and still fits with the atom that follows it), so it starts at a border between atoms."""
    chunks: list[Span] = []
    count = len(atoms)
    first = 0
    while first < count:
        start = atoms[first][0]
        last = first
        while last + 1 < count and atoms[last + 1][1] - start <= size:
            last += 1
        chunks.append((start, atoms[last][1]))
        if last + 1 >= count:
            break
        previous_end = atoms[last][1]
        next_end = atoms[last + 1][1]
        candidate = last + 1
        while (
            candidate - 1 > first
            and previous_end - atoms[candidate - 1][0] <= overlap
            and next_end - atoms[candidate - 1][0] <= size
        ):
            candidate -= 1
        first = candidate
    return chunks


def recursive_spans(content: str, start: int, end: int, size: int, overlap: int) -> list[Span]:
    """The ``recursive`` chunks of ``content[start:end]`` as spans."""
    return _pack(_split(content, start, end, 0, size), size, overlap)


def _fixed_spans(content: str, size: int, overlap: int) -> list[Span]:
    step = size - overlap
    spans: list[Span] = []
    start = 0
    length = len(content)
    while True:
        end = min(start + size, length)
        if not content[start:end].isspace():
            spans.append((start, end))
        if end >= length:
            return spans
        start += step


# --- sentences ---------------------------------------------------------------------------------

_ABBREVIATIONS = (
    "z. B.",
    "z.B.",
    "bzw.",
    "ca.",
    "d. h.",
    "d.h.",
    "u. a.",
    "u.a.",
    "usw.",
    "vgl.",
    "Nr.",
    "S.",
    "Abs.",
    "Jh.",
    "Dr.",
    "Prof.",
    "Hr.",
    "Fr.",
    "St.",
    "e.g.",
    "i.e.",
    "etc.",
    "vs.",
    "Mr.",
    "Mrs.",
    "Ms.",
    "No.",
    "Fig.",
)
_ABBREVIATION = re.compile(
    r"(?<!\w)(?:"
    + "|".join(dict.fromkeys(re.escape(item).replace("\\ ", r"\s?") for item in _ABBREVIATIONS))
    + ")"
)
_TERMINATOR = re.compile(r"(?<![.!?…])(?P<run>[.!?…]+)[\"'\u201d\u2019»)\]]*(?=\s)")


def split_sentences(text: str) -> list[tuple[int, int]]:
    """The sentences of `text` as ``(start, end)`` spans without the whitespace around them.

    A sentence ends after ``.``, ``!``, ``?`` or ``…`` (and closing quotes or brackets after
    them) that is followed by whitespace, except after an abbreviation of the list above, after a
    single capital letter (``J. Smith``) and after digits (``3. Oktober``)."""
    protected: set[int] = set()
    for abbreviation in _ABBREVIATION.finditer(text):
        protected.update(
            position
            for position in range(abbreviation.start(), abbreviation.end())
            if text[position] == "."
        )
    spans: list[Span] = []
    start = 0
    for match in _TERMINATOR.finditer(text):
        if match.group("run") == ".":
            dot = match.start("run")
            if dot in protected or (dot > 0 and text[dot - 1].isdigit()):
                continue
            if dot > 0 and text[dot - 1].isupper() and (dot == 1 or not text[dot - 2].isalpha()):
                continue
        sentence = _trim(text, start, match.end())
        if sentence is not None:
            spans.append(sentence)
        start = match.end()
    last = _trim(text, start, len(text))
    if last is not None:
        spans.append(last)
    return spans


def sentence_atoms(content: str, size: int) -> list[Span]:
    """The sentences of `content` as atoms: a sentence over `size` is split by ``recursive``."""
    atoms: list[Span] = []
    for start, end in split_sentences(content):
        if end - start <= size:
            atoms.append((start, end))
        else:
            atoms.extend(_split(content, start, end, 0, size))
    return atoms


# --- Markdown ----------------------------------------------------------------------------------

_BLOCK_START = re.compile(
    r"^ {0,3}(?:(?P<hash>#{1,6})(?=[ \t\r]|$)|(?P<fence>`{3,}|~{3,}))", re.MULTILINE
)
_CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+$")


def _headings(content: str) -> list[tuple[int, int, str]]:
    """``(offset of the #, level, text)`` of every ATX heading outside fenced code."""
    found: list[tuple[int, int, str]] = []
    fence_character = ""
    fence_length = 0
    for match in _BLOCK_START.finditer(content):
        line_end = content.find("\n", match.end())
        if line_end == -1:
            line_end = len(content)
        fence = match.group("fence")
        if fence:
            rest = content[match.end() : line_end]
            if fence_character:
                if fence[0] == fence_character and len(fence) >= fence_length and not rest.strip():
                    fence_character = ""
            elif fence[0] != "`" or "`" not in rest:
                fence_character = fence[0]
                fence_length = len(fence)
        elif not fence_character:
            text = _CLOSING_HASHES.sub("", content[match.end() : line_end].strip())
            found.append((match.start("hash"), len(match.group("hash")), text))
    return found


def heading_paths(content: str) -> list[tuple[int, list[str]]]:
    """``(offset, path)`` for every ATX heading outside fenced code, in order: the offset of its
    ``#`` and the heading path in force from there (``["Topic", "Subtopic"]``, text without
    ``#``). A deeper heading extends the path, a heading of the same or a higher level replaces
    the entries from its level on."""
    stack: list[tuple[int, str]] = []
    paths: list[tuple[int, list[str]]] = []
    for offset, level, text in _headings(content):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
        paths.append((offset, [entry for _, entry in stack if entry]))
    return paths


def _sections(content: str, offsets: list[int]) -> list[Span]:
    """Sections cut at the headings at `offsets`. A section that is only heading lines goes with
    the section that follows, so that a heading never ends a chunk."""
    end = len(content)
    sections: list[tuple[int, int, bool]] = []
    if content[: offsets[0]].strip():
        sections.append((0, offsets[0], True))
    for number, offset in enumerate(offsets):
        section_end = offsets[number + 1] if number + 1 < len(offsets) else end
        line_end = content.find("\n", offset, section_end)
        line_end = section_end if line_end == -1 else line_end
        sections.append((offset, section_end, bool(content[line_end:section_end].strip())))
    merged: list[Span] = []
    current: tuple[int, int, bool] | None = None
    for start, section_end, has_body in sections:
        if current is None:
            current = (start, section_end, has_body)
        elif not current[2]:
            current = (current[0], section_end, has_body)
        else:
            merged.append((current[0], current[1]))
            current = (start, section_end, has_body)
    if current is not None:
        merged.append((current[0], current[1]))
    return merged


def _glue_headings(
    content: str, atoms: list[Span], heading_offsets: set[int], size: int
) -> list[Span]:
    """Join every atom that is a heading line with the atom after it, so that no chunk ends at a
    heading; an atom that does not fit next to the heading is cut so that its first piece does."""

    def is_heading(atom: Span) -> bool:
        return atom[0] in heading_offsets and content.find("\n", atom[0], atom[1]) == -1

    glued: list[Span] = []
    waiting: Span | None = None
    for atom in atoms:
        if waiting is None:
            if is_heading(atom):
                waiting = atom
            else:
                glued.append(atom)
        elif atom[1] - waiting[0] <= size:
            joined = (waiting[0], atom[1])
            if is_heading(atom):
                waiting = joined
            else:
                glued.append(joined)
                waiting = None
        elif not is_heading(atom) and atom[0] - waiting[0] <= size // 2:
            pieces = _split(content, atom[0], atom[1], 0, size - (atom[0] - waiting[0]))
            glued.append((waiting[0], pieces[0][1]))
            glued.extend(pieces[1:])
            waiting = None
        else:
            glued.append(waiting)
            waiting = None
            if is_heading(atom):
                waiting = atom
            else:
                glued.append(atom)
    if waiting is not None:
        glued.append(waiting)
    return glued


def _markdown_spans(content: str, size: int, overlap: int) -> list[Span]:
    offsets = [offset for offset, _, _ in _headings(content)]
    if not offsets:
        return recursive_spans(content, 0, len(content), size, overlap)
    heading_offsets = set(offsets)
    spans: list[Span] = []
    for start, end in _sections(content, offsets):
        atoms = _split(content, start, end, 0, size)
        if len(atoms) > 1:
            atoms = _glue_headings(content, atoms, heading_offsets, size)
        spans.extend(_pack(atoms, size, overlap))
    return spans


# --- the chunks --------------------------------------------------------------------------------


def build_chunks(
    content: str, spans: list[Span], *, markdown: bool, max_chunks: int
) -> tuple[list[Chunk], list[str]]:
    """The `Chunk`s of `spans`, with their heading paths for Markdown, at most `max_chunks` of
    them (then the warning ``chunks_truncated``)."""
    warnings: list[str] = []
    if len(spans) > max_chunks:
        spans = spans[:max_chunks]
        warnings.append(WARNING_TRUNCATED)
    paths = heading_paths(content) if markdown else []
    offsets = [offset for offset, _ in paths]
    chunks: list[Chunk] = []
    for index, (start, end) in enumerate(spans):
        found = bisect_right(offsets, start) - 1
        chunks.append(
            Chunk(
                index=index,
                text=content[start:end],
                start=start,
                end=end,
                headings=list(paths[found][1]) if found >= 0 else [],
            )
        )
    return chunks, warnings


def chunk(
    content: str, options: ChunkingOptions, *, markdown: bool, max_chunks: int
) -> tuple[list[Chunk], list[str]]:
    """Cut `content` into chunks with ``options.strategy`` (all but ``semantic``, which needs
    embeddings: ``semantic_chunks``). `markdown` says that `content` is Markdown: it gives the
    chunks their heading paths and lets ``markdown`` cut at headings; ``markdown`` on text falls
    back to ``recursive`` with the warning ``chunking_fallback_recursive``. Returns the chunks and
    the warnings."""
    size, overlap = options.size, options.overlap
    warnings: list[str] = []
    if not content.strip():
        return [], warnings
    strategy = options.strategy
    if strategy == "semantic":
        raise ValueError("The semantic strategy needs embeddings: use semantic_chunks.")
    if strategy == "markdown" and not markdown:
        strategy = "recursive"
        warnings.append(WARNING_FALLBACK_RECURSIVE)
    if strategy == "fixed":
        spans = _fixed_spans(content, size, overlap)
    elif strategy == "markdown":
        spans = _markdown_spans(content, size, overlap)
    elif strategy == "sentence":
        spans = _pack(sentence_atoms(content, size), size, overlap)
    else:
        spans = recursive_spans(content, 0, len(content), size, overlap)
    chunks, more = build_chunks(content, spans, markdown=markdown, max_chunks=max_chunks)
    return chunks, [*warnings, *more]
