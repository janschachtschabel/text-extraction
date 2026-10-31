"""URL lists of the UI's batches (ruling P10-S19), parsed on the server: a CSV or JSON file of
URLs (``parse_url_file``), typed lines or the tree's checked URLs (``parse_url_lines``), and the
split of a list into batches of ``BATCH_MAX_URLS`` (``split_batches``). Pure: no I/O.

Every value must be a plain http(s) URL (``netguard.validate_url``, without blanks or control
characters); anything else, such as ``=HYPERLINK(...)`` or ``@cmd``, is reported with its row and
never kept. The UI writes no CSV, so nothing a cell holds reaches a spreadsheet through it."""

import csv
import json
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from text_extraction.netguard import MAX_URL_LENGTH, InvalidUrlError, validate_url
from text_extraction.ui.i18n import Message, MessageError

__all__ = [
    "URL_PROBLEMS",
    "UrlList",
    "UrlListError",
    "parse_url_file",
    "parse_url_lines",
    "split_batches",
]

BOM: Final = b"\xef\xbb\xbf"
# A CSV field may be one character longer than a URL (so such a URL is reported as too long);
# a longer field refuses the file.
FIELD_MAX_CHARS: Final = MAX_URL_LENGTH + 1
# The delimiters a CSV may use, in the order they are tried, and the lines that decide.
DELIMITERS: Final = ("\t", ";", ",")
SNIFF_LINES: Final = 50
# A row of a CSV has at most this many fields; more refuses the file before the row is split.
MAX_COLUMNS: Final = 1000
# The delimiter of a file of one column: a control character it does not hold (one of these).
NO_DELIMITER: Final = ("\x1f", "\x1e", "\x1d", "\x1c", "\x00")

NOT_UTF8: Final = Message("urls.not_utf8")
NOT_JSON: Final = Message("urls.not_json")
NOT_URL_ARRAY: Final = Message("urls.not_url_array")
FIELD_TOO_LONG: Final = Message("urls.field_too_long")
TOO_MANY_COLUMNS: Final = Message("urls.too_many_columns")
BLANK_OR_CONTROL: Final = Message("urls.blank_or_control")
NO_URL: Final = Message("urls.no_url")
TOO_LONG: Final = Message("urls.too_long", {"max": MAX_URL_LENGTH})
# The text of each ``InvalidUrlError`` that ``netguard.validate_url`` raises (but the length,
# `TOO_LONG`, which is checked first); another one reads as an invalid URL.
URL_PROBLEMS: Final[Mapping[str, Message]] = MappingProxyType(
    {
        "URL cannot be parsed": Message("urls.unparsable"),
        "URL scheme must be http or https": Message("urls.scheme"),
        "URL has no host": Message("urls.no_host"),
        "URL must not contain user information": Message("urls.user_info"),
        "URL port must be between 1 and 65535": Message("urls.port"),
    }
)
INVALID_URL: Final = Message("error.invalid_url")


class UrlListError(MessageError):
    """A list or file that is refused as a whole; its ``message`` is shown."""


@dataclass
class UrlList:
    """The URLs of a list in their order (duplicates dropped), the rows that hold no URL with
    their reason (``(row, reason)``, rows counted from 1), how many duplicates were dropped and
    how many rows held a value."""

    urls: list[str] = field(default_factory=list)
    invalid: list[tuple[int, Message]] = field(default_factory=list)
    duplicates: int = 0
    rows: int = 0


def _problem(value: str) -> Message | None:
    """Why ``value`` (stripped) is no URL to extract, or None."""
    if not value:
        return NO_URL
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return BLANK_OR_CONTROL
    if len(value) > MAX_URL_LENGTH:
        return TOO_LONG
    try:
        validate_url(value)
    except InvalidUrlError as error:
        return URL_PROBLEMS.get(str(error), INVALID_URL)
    return None


class _Collector:
    """Collects the values of a list in order: valid, invalid or a duplicate."""

    def __init__(self) -> None:
        self.found = UrlList()
        self._seen: set[str] = set()

    def add(self, row: int, value: str) -> None:
        found = self.found
        value = value.strip()
        found.rows += 1
        problem = _problem(value)
        if problem is not None:
            found.invalid.append((row, problem))
        elif value in self._seen:
            found.duplicates += 1
        else:
            self._seen.add(value)
            found.urls.append(value)


def parse_url_lines(values: Iterable[str], *, max_rows: int) -> UrlList:
    """The URLs of ``values``, one per value; blank values are skipped, rows count every value
    from 1. More than ``max_rows`` values that are not blank is refused before any is checked."""
    lines = list(values)
    if sum(1 for value in lines if value.strip()) > max_rows:
        raise UrlListError(Message("urls.list_too_long", {"max": max_rows}))
    collector = _Collector()
    for row, value in enumerate(lines, start=1):
        if value.strip():
            collector.add(row, value)
    return collector.found


def _text(data: bytes) -> str:
    try:
        return data.removeprefix(BOM).decode("utf-8")
    except UnicodeDecodeError:
        raise UrlListError(NOT_UTF8) from None


def _rows_in(text: str) -> int:
    """The lines of ``text`` that are not blank (a CSV row per line; JSON counts its items)."""
    return sum(1 for line in text.splitlines() if line.strip())


def parse_url_file(data: bytes, *, max_rows: int) -> UrlList:
    """The URLs of a CSV or JSON file. The bytes must be UTF-8 (a BOM is dropped). A first
    character, after blanks, of ``[`` (or ``{``) means JSON: an array of strings or of objects
    with a string ``url``. Anything else is CSV, delimited by ``,``, ``;`` or a tab: a header
    with a ``url`` column (any case) selects it, else the first column, and a first row whose
    value is not http(s) is a header. More than ``max_rows`` rows is refused before anything
    else. Raises `UrlListError` with the message to show."""
    text = _text(data)
    start = text.lstrip()[:1]
    if start in ("[", "{"):
        return _parse_json(text, max_rows)
    if _rows_in(text) > max_rows:
        raise UrlListError(Message("urls.file_too_long", {"max": max_rows}))
    return _parse_csv(text)


def _parse_json(text: str, max_rows: int) -> UrlList:
    try:
        parsed: Any = json.loads(text)
    except (ValueError, RecursionError):  # RecursionError: nested too deep
        raise UrlListError(NOT_JSON) from None
    if not isinstance(parsed, list):
        raise UrlListError(NOT_URL_ARRAY)
    if len(parsed) > max_rows:
        raise UrlListError(Message("urls.file_too_long", {"max": max_rows}))
    values: list[str] = []
    for element in parsed:
        value = element.get("url") if isinstance(element, dict) else element
        if not isinstance(value, str):
            raise UrlListError(NOT_URL_ARRAY)
        values.append(value)
    collector = _Collector()
    for row, value in enumerate(values, start=1):
        collector.add(row, value)
    return collector.found


def _delimiter(lines: Sequence[str]) -> str | None:
    """The first of ``DELIMITERS`` that splits every one of ``lines`` into the same number (more
    than one) of fields; None when none does (a file of one column: each line is one value)."""
    for delimiter in DELIMITERS:
        counts = {len(row) for row in csv.reader(lines, delimiter=delimiter)}
        if len(counts) == 1 and counts.pop() > 1:
            return delimiter
    return None


def _no_delimiter(text: str) -> str:
    """A delimiter that ``text`` does not hold (an ASCII separator), so each line is one field
    and quotes still work."""
    return next((char for char in NO_DELIMITER if char not in text), NO_DELIMITER[0])


def _bounded(lines: Iterable[str], delimiters: Sequence[str]) -> Iterator[str]:
    """``lines``, refusing one with more than ``MAX_COLUMNS`` fields by any of ``delimiters``
    before the csv module splits it."""
    for line in lines:
        if any(line.count(delimiter) >= MAX_COLUMNS for delimiter in delimiters):
            raise UrlListError(TOO_MANY_COLUMNS)
        yield line


def _csv_rows(text: str) -> Iterator[tuple[int, list[str]]]:
    """The rows of ``text`` that are not blank, with their numbers (from 1). Raises
    `UrlListError` for a field over ``FIELD_MAX_CHARS`` or a row of more than ``MAX_COLUMNS``
    fields."""
    lines = text.splitlines()
    sample = [line for line in lines[: SNIFF_LINES * 4] if line.strip()][:SNIFF_LINES]
    sample = list(_bounded(sample, DELIMITERS))
    try:
        delimiter = _delimiter(sample) or _no_delimiter(text)
        reader = csv.reader(_bounded(lines, (delimiter,)), delimiter=delimiter)
        for cells in reader:
            if any(len(cell) > FIELD_MAX_CHARS for cell in cells):
                raise UrlListError(FIELD_TOO_LONG)
            if any(cell.strip() for cell in cells):
                yield reader.line_num, cells
    except csv.Error:  # a quoted field over the csv module's own limit, or a broken quote
        raise UrlListError(FIELD_TOO_LONG) from None


def _is_http(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def _parse_csv(text: str) -> UrlList:
    collector = _Collector()
    column = 0
    first = True
    for row, cells in _csv_rows(text):
        if first:
            first = False
            names = [cell.strip().casefold() for cell in cells]
            if "url" in names:
                column = names.index("url")
                continue
            if not _is_http(cells[0]):
                continue  # a header without a url column
        collector.add(row, cells[column] if column < len(cells) else "")
    return collector.found


def split_batches(urls: Sequence[str], *, per_batch: int, max_batches: int) -> list[list[str]]:
    """``urls`` in order, in ``ceil(len / per_batch)`` batches of at most ``per_batch``. More
    batches than ``max_batches`` is refused (`UrlListError`), so nothing is sent."""
    count = math.ceil(len(urls) / per_batch)
    if count > max_batches:
        raise UrlListError(Message("urls.select_at_most", {"max": max_batches * per_batch}))
    return [list(urls[start : start + per_batch]) for start in range(0, len(urls), per_batch)]
