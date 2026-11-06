"""PHP error output that a site printed into its own page, removed before extraction.

A PHP site that shows its errors writes them into the HTML it serves. With ``html_errors`` on
a message reads ``<br />\\n<b>Warning</b>:  message in <b>/var/www/file.php</b> on line
<b>128</b><br />``, with it off it is the same text (``PHP Warning: message in /file.php on
line 128``). Such a message is the server's, not the page's content.

The scan reads the text of each block, its inline children included (a message in the
``html_errors`` form spans ``<b>`` elements and the text between them), and anchors on the
label: every candidate costs a few binary searches, so the work stays linear in the page.
Text inside ``pre``, ``code``, ``textarea`` and ``script`` is never touched; an article that
quotes a PHP warning keeps it.
"""

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Final

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

from text_extraction.convert.markup import BLOCK_TAGS

SERVER_ERRORS_REMOVED_WARNING: Final = "server_errors_removed"
MAX_LISTED_ERRORS: Final = 50  # distinct messages returned; removal itself has no cap
MAX_MESSAGE_CHARS: Final = 300  # of a returned message

_LABEL = re.compile(
    r"(?<!\w)(?:PHP )?(?P<level>Warning|Notice|Deprecated|Fatal error|Parse error"
    r"|Strict Standards|Recoverable fatal error|Catchable fatal error):[^\S\n]+"
)
_ON_LINE = re.compile(r" on line (\d+)")
_IN_PATH = re.compile(r" in (?:/|[A-Za-z]:\\)")  # a path starts with "/" or a drive letter
_IN = len(" in ")
_NEWLINE = re.compile(r"\n")
_EXTENSIONS = (".php", ".inc", ".phtml")
_MAX_MESSAGE = 1000
_MAX_PATH = 500
_SKIPPED = frozenset({"pre", "code", "textarea", "script"})

# A block's text, piece by piece: a text node, a <br> or a boundary (a nested block or a
# skipped element), which reads as a line break.
_Piece = NavigableString | Tag | None


@dataclass(frozen=True)
class ServerError:
    """One PHP error message removed from the page; ``count`` identical ones were merged."""

    level: str  # the label as written, without a leading "PHP ": "Warning", "Fatal error"
    message: str  # whitespace collapsed, at most MAX_MESSAGE_CHARS
    file: str
    line: int
    count: int = 1


@dataclass(frozen=True)
class _Found:
    start: int
    end: int
    error: ServerError


def strip_server_errors(soup: BeautifulSoup) -> list[ServerError]:
    """Remove PHP error messages from the page, in place, with the ``<br>`` right before and
    right after each, and return what was removed: identical messages merged into one entry
    with its ``count``, in the order first seen, at most ``MAX_LISTED_ERRORS`` entries."""
    # Blocks are visited out of document order (a block's own text can follow a nested
    # block), so each message is placed by the position of its first text node.
    order = {
        id(node): index
        for index, node in enumerate(soup.descendants)
        if isinstance(node, NavigableString)
    }
    removed: list[tuple[int, int, ServerError]] = []
    for block, pieces in _blocks(soup):
        texts = [str(piece) if isinstance(piece, NavigableString) else "\n" for piece in pieces]
        found = _messages("".join(texts))
        if not found:
            continue
        starts = _starts(texts)
        for item in found:
            first = pieces[bisect_right(starts, item.start) - 1]
            removed.append((order.get(id(first), 0), item.start, item.error))
        _remove(block, pieces, texts, starts, [(item.start, item.end) for item in found])
    removed.sort(key=lambda entry: entry[:2])
    merged: dict[ServerError, ServerError] = {}
    for _, _, error in removed:
        seen = merged.get(error)
        merged[error] = error if seen is None else replace(seen, count=seen.count + 1)
    return list(merged.values())[:MAX_LISTED_ERRORS]


def _starts(texts: list[str]) -> list[int]:
    """Where each piece's text starts in the block's text."""
    starts: list[int] = []
    position = 0
    for text in texts:
        starts.append(position)
        position += len(text)
    return starts


def _blocks(soup: BeautifulSoup) -> Iterator[tuple[Tag, list[_Piece]]]:
    """Each block with its pieces. A block's pieces are complete before the caller changes
    them; the blocks nested in it come later and are not touched by that change."""
    pending: list[Tag] = [soup]
    while pending:
        block = pending.pop()
        pieces: list[_Piece] = []
        stack = [iter(list(block.children))]
        while stack:
            node = next(stack[-1], None)
            if node is None:
                stack.pop()
            elif isinstance(node, Tag):
                if node.name == "br":
                    pieces.append(node)
                elif node.name in _SKIPPED:
                    pieces.append(None)
                elif node.name in BLOCK_TAGS:
                    pieces.append(None)
                    pending.append(node)
                else:  # inline: its text flows with the block's
                    stack.append(iter(list(node.children)))
            elif type(node) is NavigableString:  # comments and the like carry no text
                pieces.append(node)
        yield block, pieces


def _messages(text: str) -> list[_Found]:
    """Each PHP error message in ``text``, in order and without overlap."""
    lines = list(_ON_LINE.finditer(text))
    line_starts = [line.start() for line in lines]
    newlines = [match.start() for match in _NEWLINE.finditer(text)]
    paths = [match.start() for match in _IN_PATH.finditer(text)]
    found: list[_Found] = []
    for label in _LABEL.finditer(text):
        start, message = label.start(), label.end()
        if found and start < found[-1].end:
            continue
        index = bisect_left(line_starts, message)
        if index == len(lines):
            break  # no " on line" after this label, so none after any later one
        line = lines[index]
        path_end = line.start()
        newline = bisect_left(newlines, message)
        if newline < len(newlines) and newlines[newline] < path_end:
            continue
        if not text.endswith(_EXTENSIONS, message, path_end):
            continue
        # " in " at k: the message is text[message:k], the path text[k + 4:path_end]. The
        # last such " in " is taken: a message may say "in", a path seldom does.
        earliest = max(message, path_end - _IN - _MAX_PATH)
        latest = min(message + _MAX_MESSAGE, path_end - _IN - 1)
        candidate = bisect_right(paths, latest) - 1
        if candidate < 0 or paths[candidate] < earliest:
            continue
        k = paths[candidate]
        error = ServerError(
            level=label.group("level"),
            message=" ".join(text[message:k].split())[:MAX_MESSAGE_CHARS],
            file=text[k + _IN : path_end],
            line=int(line.group(1)),
        )
        found.append(_Found(start, line.end(), error))
    return found


def _remove(
    block: Tag,
    pieces: list[_Piece],
    texts: list[str],
    starts: list[int],
    messages: list[tuple[int, int]],
) -> None:
    cuts: dict[int, list[tuple[int, int]]] = {}
    breaks: set[int] = set()
    for start, end in messages:
        first = bisect_right(starts, start) - 1
        last = bisect_left(starts, end) - 1
        for index in range(first, last + 1):
            begin = max(start, starts[index]) - starts[index]
            stop = min(end, starts[index] + len(texts[index])) - starts[index]
            if begin < stop:
                cuts.setdefault(index, []).append((begin, stop))
        before = _adjacent_break(pieces, texts, first, -1, texts[first][: start - starts[first]])
        after = _adjacent_break(pieces, texts, last, 1, texts[last][end - starts[last] :])
        breaks.update(index for index in (before, after) if index is not None)
    emptied: list[Tag] = []
    for index, ranges in cuts.items():
        piece = pieces[index]
        if not isinstance(piece, NavigableString):  # cuts are only made in text pieces
            continue
        kept = _without(texts[index], ranges)
        parent = piece.parent
        if kept:
            piece.replace_with(kept)
        else:
            piece.extract()
            if parent is not None:
                emptied.append(parent)
    for index in breaks:
        br = pieces[index]
        if not isinstance(br, Tag):  # breaks are only <br> pieces
            continue
        parent = br.parent
        br.decompose()
        if parent is not None:
            emptied.append(parent)
    for tag in emptied:
        _drop_if_empty(tag, block)


def _adjacent_break(
    pieces: list[_Piece], texts: list[str], index: int, step: int, rest: str
) -> int | None:
    """The index of the ``<br>`` next to a message (``step`` -1: before it, 1: after it), with
    only whitespace between; ``rest`` is the text of the message's own piece on that side."""
    if rest.strip():
        return None
    index += step
    while 0 <= index < len(pieces):
        piece = pieces[index]
        if isinstance(piece, NavigableString):
            if texts[index].strip():
                return None
        elif isinstance(piece, Tag):
            return index
        else:
            return None  # a block boundary
        index += step
    return None


def _without(text: str, ranges: list[tuple[int, int]]) -> str:
    kept: list[str] = []
    position = 0
    for begin, stop in sorted(ranges):
        kept.append(text[position:begin])
        position = max(position, stop)
    kept.append(text[position:])
    return "".join(kept)


def _drop_if_empty(tag: PageElement, block: Tag) -> None:
    """Remove ``tag`` and then each inline ancestor below ``block`` that the removal left
    without any content (``<b>Warning</b>`` once its text has gone)."""
    while isinstance(tag, Tag) and tag is not block and not tag.decomposed and not tag.contents:
        parent = tag.parent
        tag.decompose()
        if parent is None:
            return
        tag = parent
