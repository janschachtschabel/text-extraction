"""The ``patterns`` strategy (ruling S6) and the tables of results that are no HTML page.

Patterns run in the post stage on the final content (after PII: masked content, D3 a), with the
``regex`` module, so that each pattern has a time limit: ``STRUCTURED_REGEX_TIMEOUT_SECONDS``
for all of its matches together. A pattern that runs into it leaves its field missing with the
warning ``pattern_timeout`` (no field name in it); a catastrophic caller pattern such as
``(a+)+$`` therefore costs at most that time (Review Focus 2). On top of that, all patterns of
one result share ``PATTERNS_TOTAL_BUDGET_SECONDS`` (2 s, ruling R10): each pattern gets what is
left of it, and the patterns after it is spent leave their fields missing with
``pattern_timeout``, so 50 such patterns cost 2 s, not 25. The post stage runs the patterns of
a result once and keeps their outcome on it (``post.run_post_patterns``). The search releases
the GIL (``concurrent``), so that it can run in a thread.

A rule's ``group`` (number or name) of each match is its value; ``all`` takes every match (the
default for an array field), else the first. Blank values are skipped. As for selector rules
(``dom.py``), a value is cut to ``MAX_VALUE_CHARS`` characters and a field takes at most
``MAX_FIELD_VALUES`` matches and ``MAX_FIELD_CHARS`` characters, then the warning
``structured_truncated``. The values are coerced to the field's type (``schema.coerce``); a
match that does not fit is no value, with the warning ``structured_value_invalid``.

``tables_from_markdown`` reads GitHub pipe tables (a header row, a delimiter row such as
``| --- | :-: |``, then the rows with a ``|``, up to a blank line) outside fenced code, within the
bounds of the DOM stage's tables. Nothing here logs a pattern, a value or the content.
"""

import re
import time
from collections.abc import Iterator, Sequence
from typing import Any, Final

import regex

from text_extraction.structured.dom import (
    MAX_FIELD_CHARS,
    MAX_FIELD_VALUES,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_TABLES,
    MAX_TABLES_CHARS,
    MAX_VALUE_CHARS,
    STRUCTURED_TRUNCATED_WARNING,
)
from text_extraction.structured.schema import CompiledSchema, coerce
from text_extraction.structured.types import PatternRule, StructuredOptions, Table

__all__ = [
    "PATTERNS_TOTAL_BUDGET_SECONDS",
    "PATTERN_TIMEOUT_WARNING",
    "VALUE_INVALID_WARNING",
    "markdown_tables",
    "run_patterns",
    "tables_from_markdown",
]

PATTERN_TIMEOUT_WARNING: Final = "pattern_timeout"
PATTERNS_TOTAL_BUDGET_SECONDS: Final = 2.0  # all patterns of one result together
VALUE_INVALID_WARNING: Final = "structured_value_invalid"
_FLAGS: Final = {"i": regex.IGNORECASE, "m": regex.MULTILINE, "s": regex.DOTALL}


def _note(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def _is_array(compiled: CompiledSchema, name: str) -> bool:
    return bool(compiled.properties[name].get("type") == "array")


def _matches(
    content: str, rule: PatternRule, take_all: bool, timeout: float
) -> tuple[list[str], bool]:
    """The rule's values in ``content`` and whether they were cut to the field's limits.
    Raises ``TimeoutError`` when the pattern runs over ``timeout`` seconds."""
    flags = 0
    for flag in rule.flags:
        flags |= _FLAGS[flag]
    pattern = regex.compile(rule.regex, flags)
    found: list[str] = []
    chars = 0
    for match in pattern.finditer(content, timeout=timeout, concurrent=True):
        value = match.group(rule.group)
        if not value or not value.strip():
            continue
        if len(found) >= MAX_FIELD_VALUES:
            return found, True
        value = value[:MAX_VALUE_CHARS]
        room = MAX_FIELD_CHARS - chars
        if len(value) > room:
            if value[:room].strip():
                found.append(value[:room])
            return found, True
        chars += len(value)
        found.append(value)
        if not take_all:
            break
    return found, False


def run_patterns(
    content: str,
    options: StructuredOptions,
    fields: Sequence[str],
    *,
    timeout: float,
    budget: float = PATTERNS_TOTAL_BUDGET_SECONDS,
) -> tuple[dict[str, Any], list[str]]:
    """The values the pattern rules of ``fields`` find in ``content`` (fields without a rule,
    or without a value, are left out) and the warnings (``pattern_timeout``,
    ``structured_truncated``, ``structured_value_invalid``). ``timeout`` is each pattern's
    limit in seconds, ``budget`` the limit of all of them together. Never raises for a
    pattern."""
    compiled = options.compiled
    values: dict[str, Any] = {}
    warnings: list[str] = []
    if compiled is None:
        return values, warnings
    deadline = time.monotonic() + budget
    for name in fields:
        rule = options.patterns.get(name)
        if rule is None or name not in compiled.fields:
            continue
        take_all = rule.all if rule.all is not None else _is_array(compiled, name)
        left = deadline - time.monotonic()
        if left <= 0:
            _note(warnings, PATTERN_TIMEOUT_WARNING)
            continue
        try:
            found, truncated = _matches(content, rule, take_all, min(timeout, left))
        except TimeoutError:
            _note(warnings, PATTERN_TIMEOUT_WARNING)
            continue
        if truncated:
            _note(warnings, STRUCTURED_TRUNCATED_WARNING)
        if not found:
            continue
        value = coerce(compiled, name, found if take_all else found[0])
        if value is None:
            _note(warnings, VALUE_INVALID_WARNING)
            continue
        values[name] = value
    return values, warnings


# --- tables from Markdown ----------------------------------------------------------------------

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DELIMITER_CELL = re.compile(r"^:?-+:?$")
_ESCAPED_PIPE = "\x00"


def _cells(line: str) -> list[str]:
    """The cells of a pipe-table row: split at unescaped ``|``, the outer pipes optional."""
    text = line.strip().replace("\\|", _ESCAPED_PIPE)
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip().replace(_ESCAPED_PIPE, "|") for cell in text.split("|")]


def _has_pipe(line: str) -> bool:
    return "|" in line.replace("\\|", "")


def _is_delimiter(line: str, columns: int) -> bool:
    if not _has_pipe(line) and columns > 1:
        return False
    cells = _cells(line)
    return len(cells) == columns and all(_DELIMITER_CELL.match(cell) for cell in cells)


def _unfenced(lines: list[str]) -> Iterator[tuple[int, bool]]:
    """Each line's number and whether it is outside fenced code."""
    fence: str | None = None
    for number, line in enumerate(lines):
        opened = _FENCE.match(line)
        if fence is None and opened:
            fence = opened.group(1)[0] * len(opened.group(1))
            yield number, False
        elif fence is not None:
            if line.strip().startswith(fence) and not line.strip().strip(fence[0]):
                fence = None
            yield number, False
        else:
            yield number, True


def markdown_tables(content: str) -> tuple[list[Table], bool]:
    """The pipe tables of ``content`` (see the module docstring) and whether they were cut at
    the bounds of ``dom.py`` (``MAX_TABLES``, ``MAX_TABLE_ROWS``, ``MAX_TABLE_COLUMNS``,
    ``MAX_TABLES_CHARS``). A table needs a row besides its header; a header of blank cells is
    none (``headers: []``)."""
    lines = content.splitlines()
    plain = {number for number, outside in _unfenced(lines) if outside}
    tables: list[Table] = []
    budget = MAX_TABLES_CHARS
    number = 0
    while number < len(lines) - 1:
        line = lines[number]
        if number not in plain or number + 1 not in plain or not _has_pipe(line):
            number += 1
            continue
        header = _cells(line)
        if not _is_delimiter(lines[number + 1], len(header)):
            number += 1
            continue
        columns = min(len(header), MAX_TABLE_COLUMNS)
        rows: list[list[str]] = []
        cut = False
        end = number + 2
        while end < len(lines) and end in plain and lines[end].strip() and _has_pipe(lines[end]):
            if len(rows) < MAX_TABLE_ROWS:
                cells = (_cells(lines[end]) + [""] * columns)[:columns]
                size = sum(map(len, cells))
                if size > budget:
                    cut = True
                    break
                budget -= size
                rows.append(cells)
            end += 1
        if rows:
            headers = header[:columns] if any(header) else []
            budget -= sum(map(len, headers))
            if budget < 0:
                return tables, True
            if len(tables) >= MAX_TABLES:
                return tables, True
            tables.append(Table(index=len(tables), caption=None, headers=headers, rows=rows))
        if cut:
            return tables, True
        number = end
    return tables, False


def tables_from_markdown(content: str) -> list[Table]:
    """The pipe tables of ``content``: the tables of a result that is no HTML page (S3)."""
    return markdown_tables(content)[0]
