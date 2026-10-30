"""The chunkers of ``chunking/split.py`` (ruling S10): the invariants every strategy keeps (checked
over a fixed-seed generator of awkward Markdown), the strategies one by one, the sentence splitter,
the heading paths and the cap on the number of chunks."""

import random
import time
from itertools import pairwise

import pytest

from text_extraction.chunking.split import chunk, heading_paths, split_sentences
from text_extraction.chunking.types import Chunk, ChunkingOptions, ChunkStrategy

MAX = 2000


def options(
    strategy: ChunkStrategy = "recursive", size: int = 200, overlap: int = 0
) -> ChunkingOptions:
    return ChunkingOptions(strategy=strategy, size=size, overlap=overlap)


def run(
    content: str,
    strategy: ChunkStrategy = "recursive",
    *,
    size: int = 200,
    overlap: int = 0,
    markdown: bool = False,
    max_chunks: int = MAX,
) -> tuple[list[Chunk], list[str]]:
    return chunk(
        content, options(strategy, size, overlap), markdown=markdown, max_chunks=max_chunks
    )


def assert_invariants(content: str, chunks: list[Chunk], size: int, overlap: int) -> None:
    """Review Focus 5: the text is the slice, no chunk is empty or longer than `size`, the chunks
    are in order, neighbours share at most `overlap` characters and only whitespace is left out."""
    covered = bytearray(len(content))
    previous: Chunk | None = None
    for position, item in enumerate(chunks):
        assert item.index == position
        assert item.text == content[item.start : item.end]
        assert item.text.strip()
        assert len(item.text) <= size
        if previous is not None:
            assert item.start > previous.start
            assert item.end > previous.end
            assert previous.end - item.start <= overlap
        for offset in range(item.start, item.end):
            covered[offset] = 1
        previous = item
    for offset, character in enumerate(content):
        assert covered[offset] or character.isspace(), (offset, character)


# --- the generator -----------------------------------------------------------------------------

GERMAN = "Der schnelle braune Fuchs springt über den faulen Hund und läuft durch den Wald".split()  # noqa: SIM905
ENGLISH = "The quick brown fox jumps over the lazy dog while the river keeps running".split()  # noqa: SIM905
SPECIAL = [
    "\U0001f600",  # emoji
    "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # family: emoji joined with zero-width joiners
    "e\u0301",  # combining acute
    "a\u0308\u0323",  # two combining marks
    "\U0001f1e9\U0001f1ea",  # flag: two regional indicators
    "\U0001f44d\U0001f3fd",  # emoji with a skin tone
    "…",
    "„Zitat“",
    "(Klammer)",
]
ABBREVIATIONS = ["z. B.", "Dr. Meier", "e.g.", "Mr. Smith", "Nr. 5", "3. Oktober", "ca.", "usw."]


def sentence(rng: random.Random) -> str:
    words = [
        rng.choice(rng.choice((GERMAN, ENGLISH, SPECIAL, ABBREVIATIONS)))
        for _ in range(rng.randint(3, 25))
    ]
    return " ".join(words) + rng.choice([".", ".", "!", "?", "…", ".)", '."'])


def paragraph(rng: random.Random, newline: str) -> str:
    sentences = [sentence(rng) for _ in range(rng.randint(1, 8))]
    if rng.random() < 0.3:
        return newline.join(sentences)
    return " ".join(sentences)


def fenced(rng: random.Random, newline: str) -> str:
    fence = rng.choice(["```", "~~~~"])
    lines = [
        rng.choice(["# not a heading", "x = 1", "", "    indented", "def f(): pass"])
        for _ in range(rng.randint(1, 12))
    ]
    return newline.join([fence + "python", *lines, fence])


def document(rng: random.Random) -> str:
    newline = rng.choice(["\n", "\r\n"])
    blocks: list[str] = []
    for _ in range(rng.randint(1, 14)):
        kind = rng.random()
        if kind < 0.55:
            blocks.append(paragraph(rng, newline))
        elif kind < 0.7:
            blocks.append("#" * rng.randint(1, 4) + " " + " ".join(rng.sample(GERMAN, 3)))
        elif kind < 0.82:
            blocks.append(fenced(rng, newline))
        elif kind < 0.88:
            blocks.append("x" * rng.randint(1100, 1700))  # whitespace-free, longer than every size
        elif kind < 0.94:
            blocks.append(rng.choice(SPECIAL) * rng.randint(100, 400))
        else:
            blocks.append("  \t ")
    separator = rng.choice([newline * 2, newline * 2, newline * 3, newline])
    return rng.choice(["", newline, "  "]) + separator.join(blocks) + rng.choice(["", newline])


def documents() -> list[str]:
    rng = random.Random(20261029)  # noqa: S311
    handwritten = [
        "",
        "   \n\r\n\t ",
        "short",
        "# Heading only",
        "x" * 5000,
        "😀" * 1500,
        "é" * 900,
        "a. " * 700,
        "line\r\n" * 400,
        "# A\r\n## B\r\n### C\r\ntext",
        "```\n# a\n" + "code line\n" * 80 + "```\n# real\ntext",
    ]
    return handwritten + [document(rng) for _ in range(60)]


STRATEGIES: tuple[ChunkStrategy, ...] = ("fixed", "recursive", "markdown", "sentence")


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("markdown", [False, True])
@pytest.mark.parametrize("size", [200, 1000])
@pytest.mark.parametrize("half_overlap", [False, True])
def test_invariants_hold_for_every_strategy(
    strategy: ChunkStrategy, markdown: bool, size: int, half_overlap: bool
) -> None:
    overlap = size // 2 if half_overlap else 0
    for content in documents():
        chunks, warnings = run(content, strategy, size=size, overlap=overlap, markdown=markdown)
        assert_invariants(content, chunks, size, overlap)
        if not content.strip():
            assert chunks == []
        assert set(warnings) <= {"chunking_fallback_recursive"}
        if markdown:
            continue
        assert all(item.headings == [] for item in chunks)


def test_invariants_with_the_default_overlap_of_a_hundred() -> None:
    for content in documents():
        chunks, _ = run(content, "recursive", size=1000, overlap=100, markdown=True)
        assert_invariants(content, chunks, 1000, 100)


def test_fixed_windows_and_step() -> None:
    content = "".join(chr(97 + number % 26) for number in range(1000))
    chunks, warnings = run(content, "fixed", size=200, overlap=50)
    assert warnings == []
    assert [(item.start, item.end) for item in chunks] == [
        (0, 200),
        (150, 350),
        (300, 500),
        (450, 650),
        (600, 800),
        (750, 950),
        (900, 1000),
    ]
    assert [item.text for item in chunks] == [
        content[s:e]
        for s, e in [
            (0, 200),
            (150, 350),
            (300, 500),
            (450, 650),
            (600, 800),
            (750, 950),
            (900, 1000),
        ]
    ]
    assert [item.index for item in chunks] == list(range(7))


def test_fixed_without_overlap_tiles_the_content() -> None:
    content = "ab" * 250
    chunks, _ = run(content, "fixed", size=200, overlap=0)
    assert [(item.start, item.end) for item in chunks] == [(0, 200), (200, 400), (400, 500)]


def test_content_shorter_than_size_is_one_chunk_for_every_strategy() -> None:
    content = "Ein kurzer Text. Mit zwei Sätzen."
    for strategy in STRATEGIES:
        chunks, warnings = run(content, strategy, size=1000, overlap=100, markdown=True)
        assert [(item.start, item.end) for item in chunks] == [(0, len(content))]
        assert warnings == []


def test_empty_content_gives_no_chunks() -> None:
    for strategy in STRATEGIES:
        for markdown in (False, True):
            assert run("", strategy, markdown=markdown) == ([], [])
            assert run(" \n\r\n\t ", strategy, markdown=markdown) == ([], [])


def test_semantic_is_not_served_by_chunk() -> None:
    with pytest.raises(ValueError, match="semantic_chunks"):
        run("text", "semantic")


def test_recursive_prefers_paragraphs_then_lines() -> None:
    paragraphs = [
        "".join(
            f"{letter} line {number} of this paragraph is rather wordy. " for number in range(2)
        ).strip()
        for letter in "ABCDE"
    ]
    content = "\n\n".join(paragraphs)
    chunks, _ = run(content, "recursive", size=260)
    texts = [item.text for item in chunks]
    # Every chunk is a whole number of paragraphs: it starts and ends at a paragraph border.
    assert all(
        text.split("\n\n")[0] in paragraphs and text.split("\n\n")[-1] in paragraphs
        for text in texts
    )
    assert texts[0] == paragraphs[0] + "\n\n" + paragraphs[1]
    assert sum(len(text.split("\n\n")) for text in texts) == 5

    # One paragraph of lines that are too long to share a chunk: cut at the line borders.
    lines = [f"Line {number} " + "word " * 30 for number in range(6)]
    lines = [line.strip() for line in lines]
    chunks, _ = run("\n".join(lines), "recursive", size=200)
    assert [item.text for item in chunks] == lines


def test_recursive_starts_the_next_chunk_overlap_characters_back_at_a_border() -> None:
    sentences = [f"Sentence number {number} is about forty chars." for number in range(30)]
    content = " ".join(sentences)
    chunks, _ = run(content, "recursive", size=200, overlap=100)
    assert len(chunks) > 3
    for before, after in pairwise(chunks):
        shared = before.end - after.start
        assert 0 < shared <= 100
        assert content[after.start - 1] == " " or after.start == 0  # a border between two pieces
    assert_invariants(content, chunks, 200, 100)


def test_recursive_cuts_a_whitespace_free_run_without_losing_a_character() -> None:
    content = "start " + "z" * 700 + " end"
    chunks, _ = run(content, "recursive", size=200, overlap=0)
    assert "".join(item.text for item in chunks).replace(" ", "") == content.replace(" ", "")
    assert max(len(item.text) for item in chunks) <= 200


def test_hard_cuts_do_not_separate_a_combining_mark_from_its_letter() -> None:
    content = "x" + "e\u0301" * 600  # the window border of 200 falls on a combining mark
    chunks, _ = run(content, "recursive", size=200, overlap=0)
    assert_invariants(content, chunks, 200, 0)
    assert all(not item.text.startswith("\u0301") for item in chunks)

    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # a joined emoji of five code points
    content = "x" + family * 100
    chunks, _ = run(content, "recursive", size=200, overlap=0)
    assert_invariants(content, chunks, 200, 0)
    assert all(item.text.startswith("x") or item.text.startswith(family[0]) for item in chunks)
    assert all(not item.text.endswith("\u200d") for item in chunks)


# --- markdown ---------------------------------------------------------------------------------


MARKDOWN = (
    "# Intro\n\n" + "Intro text. " * 8 + "\n\n"
    "## Code\n\nSome code:\n\n```python\n# a comment, not a heading\nx = 1\n## nor this\n```\n\n"
    "# Second\n\n" + "Second text. " * 8
)


def test_markdown_cuts_at_headings_not_inside_code_fence() -> None:
    chunks, warnings = run(MARKDOWN, "markdown", size=400, markdown=True)
    assert warnings == []
    assert [item.text.splitlines()[0] for item in chunks] == ["# Intro", "## Code", "# Second"]
    assert "```python" in chunks[1].text
    assert "# a comment, not a heading" in chunks[1].text
    assert "## nor this" in chunks[1].text
    assert_invariants(MARKDOWN, chunks, 400, 0)


def test_markdown_heading_never_ends_a_chunk() -> None:
    # A heading without text of its own goes with the section that follows.
    content = "# Title\n## Part\nBody text of the part.\n\n# Next\n\nMore."
    chunks, _ = run(content, "markdown", size=400, markdown=True)
    assert [item.text for item in chunks] == [
        "# Title\n## Part\nBody text of the part.",
        "# Next\n\nMore.",
    ]
    assert chunks[0].headings == ["Title"]

    # In a section over `size` the heading stays with the first piece of its text.
    body = " ".join(f"word{number}" for number in range(40))  # 238 characters
    content = f"# Title\n\n{body}\n\n{body}"
    chunks, _ = run(content, "markdown", size=300, markdown=True)
    assert len(chunks) == 2
    assert chunks[0].text.startswith("# Title")
    assert chunks[0].text.endswith(body)
    assert not chunks[1].text.startswith("#")
    assert_invariants(content, chunks, 300, 0)

    # The first piece is so long that the heading and it do not fit together: the piece is cut.
    body = "x" * 190 + " tail"
    content = f"# Title\n{body}\n{body}"
    chunks, _ = run(content, "markdown", size=200, markdown=True)
    assert_invariants(content, chunks, 200, 0)
    for item in chunks:
        assert item.text.rstrip().splitlines()[-1].lstrip("#").strip() != "Title"


def test_markdown_section_over_size_is_split_by_recursive() -> None:
    section = "\n\n".join(f"Paragraph {number} " + "text " * 20 for number in range(8))
    content = f"# Big\n\n{section}\n\n# Small\n\nTiny."
    chunks, _ = run(content, "markdown", size=300, overlap=50, markdown=True)
    assert len(chunks) > 3
    assert chunks[-1].text == "# Small\n\nTiny."
    assert all(item.headings == ["Big"] for item in chunks[:-1])
    assert_invariants(content, chunks, 300, 50)


def test_markdown_on_text_falls_back_with_warning() -> None:
    content = "# Not a heading\n\n" + "plain words " * 60
    chunks, warnings = run(content, "markdown", markdown=False)
    expected, _ = run(content, "recursive", markdown=False)
    assert warnings == ["chunking_fallback_recursive"]
    assert chunks == expected
    assert all(item.headings == [] for item in chunks)


def test_headings_path_per_chunk() -> None:
    content = (
        "Preface.\n\n# Topic\n\ntext a\n\n## Subtopic\n\ntext b\n\n### Deep\n\ntext c\n\n"
        "## Other\n\ntext d\n\n# Next\n\ntext e"
    )
    chunks, _ = run(content, "markdown", markdown=True)
    assert [item.headings for item in chunks] == [
        [],
        ["Topic"],
        ["Topic", "Subtopic"],
        ["Topic", "Subtopic", "Deep"],
        ["Topic", "Other"],
        ["Next"],
    ]
    as_text, _ = run(content, "markdown", markdown=False)
    assert all(item.headings == [] for item in as_text)
    long = content.replace("text", "text " * 30)
    recursive, _ = run(long, "recursive", size=200, markdown=True)
    assert recursive[0].headings == []
    assert recursive[-1].headings == ["Next"]
    assert ["Topic", "Subtopic"] in [item.headings for item in recursive]


def test_heading_paths() -> None:
    content = (
        "intro\n# One #\n```\n# fenced\n```\n  ## Two  \n#hashtag\n####### seven\n"
        "~~~\n# tilde\n~~~\n# Three\r\n## \n"
    )
    paths = heading_paths(content)
    assert paths == [
        (content.index("# One"), ["One"]),
        (content.index("## Two"), ["One", "Two"]),
        (content.index("# Three"), ["Three"]),
        (content.index("## \n"), ["Three"]),
    ]
    assert heading_paths("no headings\nat all") == []
    assert heading_paths("") == []


def test_unclosed_fence_swallows_the_rest() -> None:
    assert heading_paths("# A\n```\n# B\n## C\n") == [(0, ["A"])]


# --- sentences --------------------------------------------------------------------------------


def sentences_of(text: str) -> list[str]:
    return [text[start:end] for start, end in split_sentences(text)]


def test_sentence_splitter_keeps_abbreviations_and_ordinals() -> None:
    assert sentences_of("Das ist z. B. ein Test. Am 3. Oktober kam Dr. Meier. Er ging.") == [
        "Das ist z. B. ein Test.",
        "Am 3. Oktober kam Dr. Meier.",
        "Er ging.",
    ]
    assert sentences_of("e.g. this. Mr. Smith left.") == ["e.g. this.", "Mr. Smith left."]


def test_sentence_splitter_cuts_at_the_other_terminators_and_closers() -> None:
    assert sentences_of('Wirklich? Ja! "Genau." (Siehe oben.) Und so weiter… Ende') == [
        "Wirklich?",
        "Ja!",
        '"Genau."',
        "(Siehe oben.)",
        "Und so weiter…",
        "Ende",
    ]
    assert sentences_of("Hallo J. Smith kam. Siehe Nr. 5 und S. 7 usw. Dann weiter.") == [
        "Hallo J. Smith kam.",
        "Siehe Nr. 5 und S. 7 usw. Dann weiter.",
    ]
    assert sentences_of("No dot at all") == ["No dot at all"]
    assert sentences_of("3.14 is pi. Yes.") == ["3.14 is pi.", "Yes."]
    assert split_sentences("") == []
    assert split_sentences(" \n ") == []


def test_sentence_spans_cover_everything_but_whitespace() -> None:
    text = "  Eins. Zwei!\r\n\r\nDrei? Vier…   Fünf  "
    spans = split_sentences(text)
    assert [text[s:e] for s, e in spans] == ["Eins.", "Zwei!", "Drei?", "Vier…", "Fünf"]


def test_sentence_chunks_pack_whole_sentences() -> None:
    sentences = [f"This is sentence number {number} of the text." for number in range(12)]
    content = " ".join(sentences)
    chunks, _ = run(content, "sentence", size=200)
    for item in chunks:
        assert item.text.endswith(".")
        assert item.text.startswith("This")
        assert len(item.text) <= 200
    assert len(chunks) == 3
    assert_invariants(content, chunks, 200, 0)


def test_long_sentence_split_by_recursive() -> None:
    long_sentence = " ".join(f"word{number}" for number in range(300)) + "."
    content = f"Short one. {long_sentence} Another short one."
    chunks, _ = run(content, "sentence", size=200)
    assert chunks[0].text.startswith("Short one.")
    assert chunks[-1].text.endswith("Another short one.")
    assert len(chunks) > 5
    assert_invariants(content, chunks, 200, 0)


def test_sentence_overlap_starts_at_a_sentence_border() -> None:
    content = " ".join(
        f"Sentence {number:02d} has exactly thirty-eight chars." for number in range(30)
    )
    chunks, _ = run(content, "sentence", size=200, overlap=100)
    for before, after in pairwise(chunks):
        assert content[after.start - 1] == " "
        assert 0 < before.end - after.start <= 100
    assert_invariants(content, chunks, 200, 100)


# --- the cap and the speed --------------------------------------------------------------------


def test_max_chunks_truncates_with_warning() -> None:
    content = "word " * 400
    everything, warnings = run(content, "recursive", max_chunks=2000)
    assert len(everything) > 5
    assert warnings == []

    chunks, warnings = run(content, "recursive", max_chunks=3)
    assert warnings == ["chunks_truncated"]
    assert chunks == everything[:3]

    exact, warnings = run(content, "recursive", max_chunks=len(everything))
    assert warnings == []
    assert exact == everything


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "markdown", "sentence"])
def test_max_chunks_applies_to_every_strategy(strategy: ChunkStrategy) -> None:
    content = "# Head\n\n" + "A sentence of some words. " * 200
    chunks, warnings = run(content, strategy, max_chunks=2, markdown=True, overlap=50)
    assert len(chunks) == 2
    assert warnings == ["chunks_truncated"]


def test_a_megabyte_of_text_chunks_quickly() -> None:
    rng = random.Random(7)  # noqa: S311
    text = " ".join(sentence(rng) if number % 5 else "\n\n" for number in range(14_000))
    content = (text * 2)[:1_000_000]
    assert len(content) == 1_000_000
    for strategy in ("recursive", "sentence"):
        started = time.perf_counter()
        chunks, warnings = run(content, strategy, size=1000, overlap=100, max_chunks=100_000)
        elapsed = time.perf_counter() - started
        assert elapsed < 5, (strategy, elapsed)
        assert len(chunks) > 900
        assert warnings == []
        assert_invariants(content, chunks, 1000, 100)


@pytest.mark.parametrize("pattern", [".", ".)", "x", " ", "\n", "# a\n", "A. b. ", "\n \n"])
@pytest.mark.parametrize("strategy", ["recursive", "markdown", "sentence"])
def test_degenerate_runs_chunk_in_linear_time(strategy: ChunkStrategy, pattern: str) -> None:
    content = "a" + pattern * (300_000 // len(pattern)) + "b"
    started = time.perf_counter()
    chunks, _ = run(content, strategy, size=1000, overlap=500, markdown=True, max_chunks=10**6)
    elapsed = time.perf_counter() - started
    assert elapsed < 5, (strategy, pattern, elapsed)
    assert_invariants(content, chunks, 1000, 500)
