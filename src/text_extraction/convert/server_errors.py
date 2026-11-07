"""PHP error output that a site printed into its own page, removed before extraction.

A PHP site that shows its errors writes them into the HTML it serves. With ``html_errors`` on
a message reads ``<br />\\n<b>Warning</b>:  message in <b>/var/www/file.php</b> on line
<b>128</b><br />``, with it off it is the same text (``PHP Warning: message in /file.php on
line 128``). Such a message is the server's, not the page's content.

The scan reads the text of each block, its inline children included (a message in the
``html_errors`` form spans ``<b>`` elements and the text between them), and anchors on the
label at the start of a line: every candidate costs a few binary searches, so the work stays
linear in the page. A message inside a line of prose is a quotation and stays, and so does text
inside ``pre``, ``code``, ``kbd``, ``samp``, ``var``, ``tt``, ``textarea`` and ``script``.

In a quoting context (``blockquote``, ``q``, ``cite``, a heading, ``li``, ``dd``, ``dt``,
``td``, ``th``, ``figcaption`` and anything inside them) a reader quotes a message as copied
from the browser, as plain text; there only the ``html_errors`` form counts, its label and its
path each the whole text of a ``<b>`` or ``<strong>`` (ruling AU3-R4). Xdebug writes a message as
a ``table.xdebug-error`` whose first ``th`` reads ``( ! ) Warning: ... on line 3``; such a table
goes as a whole.
"""

import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Final

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

from text_extraction.convert.markup import BLOCK_TAGS

SERVER_ERRORS_REMOVED_WARNING: Final = "server_errors_removed"
MAX_LISTED_ERRORS: Final = 50  # distinct messages returned; removal itself has no cap
MAX_MESSAGE_CHARS: Final = 300  # of a returned message

# PHP writes "\n" (or "<br />\n") before every label, so a label counts only at the start of a
# line of the block's text (a <br> and a block boundary read as "\n"); one inside a sentence
# is quoted. "^" holds only after a "\n", so each line's leading blanks are read once.
_LABEL = re.compile(
    r"(?m:^)[^\S\n]*(?P<label>(?:PHP )?(?P<level>Warning|Notice|Deprecated|Fatal error"
    r"|Parse error|Strict Standards|Recoverable fatal error|Catchable fatal error)):[^\S\n]+"
)
_ON_LINE = re.compile(r" on line (\d+)")
_IN_PATH = re.compile(r" in (?:/|[A-Za-z]:\\)")  # a path starts with "/" or a drive letter
_IN = len(" in ")
_NEWLINE = re.compile(r"\n")
_NON_SPACE = re.compile(r"\S")
_EXTENSIONS = (".php", ".inc", ".phtml", ".module", ".theme", ".install", ".engine")
_MAX_MESSAGE = 1000
_MAX_PATH = 500
_SKIPPED = frozenset({"pre", "code", "kbd", "samp", "script", "textarea", "tt", "var"})
_QUOTING = frozenset(
    {"blockquote", "q", "cite", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "td", "th"}
    | {"figcaption"}
)
_BOLD = frozenset({"b", "strong"})
_HEAD_CHARS = _MAX_MESSAGE + _MAX_PATH + 50  # of an Xdebug head cell: "( ! )", message, path
_XDEBUG_MARK = re.compile(r"\(\s*!\s*\)\s*")  # "( ! )" before an Xdebug message

# A block's text, piece by piece: a text node, a <br> or a boundary (a nested block or a
# skipped element), which reads as a line break.
_Piece = NavigableString | Tag | None
# Whether a piece is in a quoting context, and whether a <b> or <strong> inside its block
# holds it.
_QUOTED: Final = 1
_IN_BOLD: Final = 2
# Takes a candidate's label span and path span; False keeps the message in the page.
_Accept = Callable[[int, int, int, int], bool]


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
    # block), so each message is placed by the position of its first text node. The same
    # walk finds the tables, for Xdebug's.
    order: dict[int, int] = {}
    tables: list[Tag] = []
    for index, node in enumerate(soup.descendants):
        if isinstance(node, NavigableString):
            order[id(node)] = index
        elif isinstance(node, Tag) and node.name == "table":
            tables.append(node)
    removed = _strip_xdebug(tables, order)
    for block, pieces, flags in _blocks(soup):
        texts = [str(piece) if isinstance(piece, NavigableString) else "\n" for piece in pieces]
        quoted = any(flag & _QUOTED for flag in flags)
        found = _messages("".join(texts), _accepting(texts, flags) if quoted else None)
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


def _strip_xdebug(tables: list[Tag], order: dict[int, int]) -> list[tuple[int, int, ServerError]]:
    """Remove each Xdebug message table (``table.xdebug-error``) of ``tables``, and return its
    message with the position of its first text node. A table whose first ``th`` reads no
    message stays."""
    removed: list[tuple[int, int, ServerError]] = []
    for table in tables:
        # A table inside one removed before is decomposed, and bs4 blanks its name. Its
        # ``decomposed`` is not asked: on a live tag it searches the whole subtree for a child
        # called "_decomposed", which is quadratic in nested tables.
        if table.name != "table" or "xdebug-error" not in table.get_attribute_list("class"):
            continue
        head = _xdebug_head(table)
        if head is None:
            continue
        first, raw = _head_text(head)
        text = " ".join(raw.split())
        mark = _XDEBUG_MARK.match(text)
        found = _messages(text[mark.end() :]) if mark else []
        if not found or found[0].start:
            continue
        removed.append((order.get(id(first), 0), 0, found[0].error))
        table.decompose()
    return removed


def _xdebug_head(table: Tag) -> Tag | None:
    """The first ``th`` of the table's own first row (a child of the table or of its
    ``thead`` or ``tbody``). Only direct children are read, never a nested table's: bs4's
    ``find`` and ``descendants`` cost a whole subtree even when they stop early."""
    for child in table.children:
        if not isinstance(child, Tag):
            continue
        if child.name == "tr":
            row: Tag | None = child
        elif child.name in ("thead", "tbody"):
            row = _child(child, "tr")
            if row is None:
                continue
        else:
            continue
        return _child(row, "th") if row is not None else None
    return None


def _child(tag: Tag, name: str) -> Tag | None:
    """The tag's first child element called ``name``."""
    for child in tag.children:
        if isinstance(child, Tag) and child.name == name:
            return child
    return None


def _head_text(head: Tag) -> tuple[NavigableString | None, str]:
    """The head cell's first text node and its text, read in document order up to a nested
    table and at most ``_HEAD_CHARS`` characters, so a cell that holds further tables is not
    read in full."""
    first: NavigableString | None = None
    parts: list[str] = []
    length = 0
    stack = [iter(head.children)]
    while stack and length < _HEAD_CHARS:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
        elif isinstance(node, Tag):
            if node.name == "table":
                break
            stack.append(iter(node.children))
        elif type(node) is NavigableString:
            first = node if first is None else first
            parts.append(node)
            length += len(node)
    return first, "".join(parts)[:_HEAD_CHARS]


def _accepting(texts: list[str], flags: list[int]) -> _Accept:
    """Outside a quoting context every candidate counts; inside one only the ``html_errors``
    form, whose label and path are each the whole text of a bold piece. The pieces' starts are
    found on the first candidate, and a piece's bounds are read once, however many candidates
    it holds."""
    bounds: dict[int, tuple[int, int]] = {}
    starts: list[int] = []

    def alone(begin: int, end: int) -> int | None:
        """The bold piece whose whole text, without its blanks, is ``[begin, end)``."""
        index = bisect_right(starts, begin) - 1
        if not flags[index] & _IN_BOLD:
            return None
        if index not in bounds:
            piece = texts[index]
            stripped = piece.strip()
            lead = starts[index] + len(piece) - len(piece.lstrip())
            bounds[index] = (lead, lead + len(stripped))
        return index if bounds[index] == (begin, end) else None

    def accept(label: int, label_end: int, path: int, path_end: int) -> bool:
        if not starts:
            starts.extend(_starts(texts))
        if not flags[bisect_right(starts, label) - 1] & _QUOTED:
            return True
        label_piece, path_piece = alone(label, label_end), alone(path, path_end)
        return label_piece is not None and path_piece is not None and label_piece != path_piece

    return accept


def _starts(texts: list[str]) -> list[int]:
    """Where each piece's text starts in the block's text."""
    starts: list[int] = []
    position = 0
    for text in texts:
        starts.append(position)
        position += len(text)
    return starts


def _blocks(soup: BeautifulSoup) -> Iterator[tuple[Tag, list[_Piece], list[int]]]:
    """Each block with its pieces and their flags (``_QUOTED``, ``_IN_BOLD``). A block's pieces
    are complete before the caller changes them; the blocks nested in it come later and are
    not touched by that change."""
    pending: list[tuple[Tag, int]] = [(soup, 0)]
    while pending:
        block, quoted = pending.pop()
        if block.name in _QUOTING:
            quoted = _QUOTED
        pieces: list[_Piece] = []
        flags: list[int] = []
        stack = [iter(list(block.children))]
        flag_stack = [quoted]
        flag = quoted
        while stack:
            node = next(stack[-1], None)
            if node is None:
                stack.pop()
                flag_stack.pop()
                flag = flag_stack[-1] if flag_stack else 0
            elif isinstance(node, Tag):
                if node.name == "br":
                    pieces.append(node)
                elif node.name in _SKIPPED:
                    pieces.append(None)
                elif node.name in BLOCK_TAGS:
                    pieces.append(None)
                    pending.append((node, flag & _QUOTED))
                else:  # inline: its text flows with the block's
                    if node.name in _QUOTING:
                        flag |= _QUOTED
                    if node.name in _BOLD:
                        flag |= _IN_BOLD
                    stack.append(iter(list(node.children)))
                    flag_stack.append(flag)
                    continue
                flags.append(flag)
            elif type(node) is NavigableString:  # comments and the like carry no text
                pieces.append(node)
                flags.append(flag)
        yield block, pieces, flags


def _messages(text: str, accept: _Accept | None = None) -> list[_Found]:
    """Each PHP error message in ``text`` that ``accept`` (by default every one) takes, in
    order and without overlap."""
    lines = list(_ON_LINE.finditer(text))
    line_starts = [line.start() for line in lines]
    newlines = [match.start() for match in _NEWLINE.finditer(text)]
    paths = [match.start() for match in _IN_PATH.finditer(text)]
    found: list[_Found] = []
    for label in _LABEL.finditer(text):
        start, message = label.start("label"), label.end()
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
        if accept and not accept(start, label.end("label"), k + _IN, path_end):
            continue
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
    """Cut the messages out of the block's text nodes and drop the ``<br>`` around them.

    Messages that only whitespace and ``<br>`` separate form a group. A group with content on
    both sides keeps one of its ``<br>`` (the last), so the lines around it stay apart; any
    other group loses them all. Every check reads only the text between a message and its
    neighbours, and each text node is rebuilt once: linear in the block."""
    text = "".join(texts)
    first_content, last_content = _content_bounds(text, messages)
    cuts: dict[int, list[tuple[int, int]]] = {}
    groups: list[_Group] = []
    previous_end = 0
    for number, (start, end) in enumerate(messages):
        next_start = messages[number + 1][0] if number + 1 < len(messages) else len(text)
        first = bisect_right(starts, start) - 1
        last = bisect_left(starts, end) - 1
        for index in range(first, last + 1):
            begin = max(start, starts[index]) - starts[index]
            stop = min(end, starts[index] + len(texts[index])) - starts[index]
            if begin < stop:
                cuts.setdefault(index, []).append((begin, stop))
        before = _break_before(pieces, texts, starts, text, first, previous_end, start)
        after = _break_after(pieces, texts, starts, text, last, end, next_start)
        previous_end = end
        if groups and before is not None and groups[-1].breaks[-1:] == [before]:
            group = groups[-1]  # only whitespace and the shared <br> since the last message
            group.end = end
        else:
            group = _Group(start, end, [] if before is None else [before])
            groups.append(group)
        if after is not None:
            group.breaks.append(after)
    breaks: list[int] = []
    for group in groups:
        breaks.extend(_dropped(group, first_content < group.start, last_content >= group.end))
    removed: list[PageElement] = []
    replaced: dict[int, tuple[PageElement, str]] = {}
    for index, ranges in cuts.items():
        piece = pieces[index]
        if piece is None:
            continue
        kept = _without(texts[index], ranges)
        if kept:
            replaced[id(piece)] = (piece, kept)
        else:
            removed.append(piece)
    for index in breaks:
        piece = pieces[index]
        if piece is not None:
            removed.append(piece)
    _apply(block, removed, replaced)


def _apply(
    block: Tag, removed: list[PageElement], replaced: dict[int, tuple[PageElement, str]]
) -> None:
    """Remove the ``removed`` nodes and each inline ancestor below ``block`` that this leaves
    without any content (``<b>Warning</b>`` once its text has gone), and give each node in
    ``replaced`` (by ``id``) its new text.

    bs4 finds a node's position in its parent by a linear search and closes the gap in the
    parent's list, so removing one node at a time is quadratic in a parent with many children
    (thousands of messages in one block). Each affected parent is rebuilt once instead: every
    child is taken out from the end (with its position given, which costs nothing), and the
    kept ones are appended again in order."""
    removed = list({id(node): node for node in removed}.values())  # each node counts once
    gone = {id(node) for node in removed}
    emptied: list[PageElement] = []
    left: dict[int, int] = {}  # the children an ancestor keeps
    for node in removed:
        parent = node.parent
        while parent is not None and parent is not block:
            count = left.get(id(parent), len(parent.contents)) - 1
            left[id(parent)] = count
            if count:
                break
            gone.add(id(parent))  # its last child went
            emptied.append(parent)
            parent = parent.parent
    parents: dict[int, Tag] = {}
    for node in [*removed, *emptied, *(node for node, _ in replaced.values())]:
        parent = node.parent
        if parent is not None and id(parent) not in gone:
            parents[id(parent)] = parent
    for parent in parents.values():
        children = list(parent.contents)
        for index in range(len(children) - 1, -1, -1):
            children[index].extract(_self_index=index)
        for child in children:
            if id(child) in gone:
                if isinstance(child, Tag):
                    child.decompose()
            elif id(child) in replaced:
                parent.append(NavigableString(replaced[id(child)][1]))
            else:
                parent.append(child)


@dataclass
class _Group:
    """Messages that only whitespace and ``<br>`` separate, and the ``<br>`` around them."""

    start: int
    end: int
    breaks: list[int]


def _dropped(group: _Group, content_before: bool, content_after: bool) -> list[int]:
    """The group's ``<br>`` to remove: all of them, unless content on both sides needs one."""
    if content_before and content_after:
        return group.breaks[:-1]
    return group.breaks


def _content_bounds(text: str, messages: list[tuple[int, int]]) -> tuple[int, int]:
    """The first and last position of non-whitespace text outside the messages (``len(text)``
    and -1 when there is none). Each gap between messages is read at most twice."""
    edges = [0, *(position for message in messages for position in message), len(text)]
    gaps = list(zip(edges[::2], edges[1::2], strict=True))
    first = len(text)
    for begin, stop in gaps:
        found = _NON_SPACE.search(text, begin, stop)
        if found:
            first = found.start()
            break
    last = -1
    for begin, stop in reversed(gaps):
        kept = text[begin:stop].rstrip()
        if kept:
            last = begin + len(kept) - 1
            break
    return first, last


def _break_before(
    pieces: list[_Piece],
    texts: list[str],
    starts: list[int],
    text: str,
    first: int,
    previous_end: int,
    start: int,
) -> int | None:
    """The ``<br>`` piece right before the message at ``start`` in piece ``first``, with only
    whitespace between; the scan stops at the previous message."""
    if previous_end > starts[first] or _NON_SPACE.search(text, starts[first], start):
        return None  # the previous message or text shares the piece
    for index in range(first - 1, -1, -1):
        found = _neighbour(pieces[index], text, starts[index], len(texts[index]))
        if found != "space":
            return index if found == "break" else None
    return None


def _break_after(
    pieces: list[_Piece],
    texts: list[str],
    starts: list[int],
    text: str,
    last: int,
    end: int,
    next_start: int,
) -> int | None:
    """The ``<br>`` piece right after the message that ends at ``end`` in piece ``last``, with
    only whitespace between; the scan stops at the next message."""
    piece_end = starts[last] + len(texts[last])
    if next_start < piece_end or _NON_SPACE.search(text, end, piece_end):
        return None  # the next message or text shares the piece
    for index in range(last + 1, len(pieces)):
        found = _neighbour(pieces[index], text, starts[index], len(texts[index]))
        if found != "space":
            return index if found == "break" else None
    return None


def _neighbour(piece: _Piece, text: str, start: int, length: int) -> str:
    """What a piece next to a message is: "space", "break" (a <br>) or "other" (text or a
    block boundary)."""
    if isinstance(piece, NavigableString):
        return "other" if _NON_SPACE.search(text, start, start + length) else "space"
    return "break" if isinstance(piece, Tag) else "other"


def _without(text: str, ranges: list[tuple[int, int]]) -> str:
    kept: list[str] = []
    position = 0
    for begin, stop in sorted(ranges):
        kept.append(text[position:begin])
        position = max(position, stop)
    kept.append(text[position:])
    return "".join(kept)
