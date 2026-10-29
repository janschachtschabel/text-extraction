"""The ``patterns`` strategy (ruling S6): regular expressions on the final content with a time
limit per pattern, and tables from Markdown pipe tables for results that are no HTML page."""

import time
from typing import Any

from text_extraction.structured.dom import (
    MAX_FIELD_VALUES,
    MAX_TABLES,
    STRUCTURED_TRUNCATED_WARNING,
)
from text_extraction.structured.patterns import (
    PATTERN_TIMEOUT_WARNING,
    markdown_tables,
    run_patterns,
    tables_from_markdown,
)
from text_extraction.structured.types import StructuredOptions, Table

from .post_helpers import NUMBER, STRING, STRINGS, schema


def options(properties: dict[str, Any], patterns: dict[str, Any]) -> StructuredOptions:
    return StructuredOptions.model_validate(
        {"schema": schema(**properties), "strategy": "patterns", "patterns": patterns}
    )


CONTENT = """Order number: AB-1234
Price: 1.234,50 EUR
TAGS: red, green
Tag: blue
tag: yellow
Contact: info@example.org"""


def test_patterns_group_all_flags() -> None:
    opts = options(
        {"order": STRING, "price": NUMBER, "tags": STRINGS, "first_tag": STRING, "mail": STRING},
        {
            # a named group
            "order": {"regex": r"Order number: (?P<id>[A-Z]+-\d+)", "group": "id"},
            # a numbered group, coerced to the field's type
            "price": {"regex": r"Price: ([\d.,]+)", "group": 1},
            # an array field takes every match by default; `i` ignores case, `m` anchors lines
            "tags": {"regex": r"^tag: (\w+)$", "group": 1, "flags": ["i", "m"]},
            # `all: false` on ... a string field takes the first match only
            "first_tag": {"regex": r"tag: (\w+)", "group": 1, "flags": ["i"]},
            # group 0 by default: the whole match
            "mail": {"regex": r"\w+@[\w.]+"},
        },
    )

    values, warnings = run_patterns(CONTENT, opts, opts.compiled.fields, timeout=0.5)  # type: ignore[union-attr]

    assert values == {
        "order": "AB-1234",
        "price": 1234.5,
        "tags": ["blue", "yellow"],
        "first_tag": "blue",
        "mail": "info@example.org",
    }
    assert warnings == []


def test_patterns_all_flag_and_dotall_and_only_the_asked_fields() -> None:
    opts = options(
        {"tag": STRING, "block": STRING, "words": STRINGS},
        {
            "tag": {"regex": r"(?i)tag: (\w+)", "group": 1, "all": True},
            "block": {"regex": r"Order.*Price", "flags": ["s"]},
            "words": {"regex": r"(\w+)@", "group": 1, "all": False},
        },
    )

    values, _ = run_patterns(CONTENT, opts, ["tag", "words"], timeout=0.5)

    # `all` on a string field joins the matches; `all: false` on an array takes one
    assert values == {"tag": "blue, yellow", "words": ["info"]}
    values, _ = run_patterns(CONTENT, opts, ["block"], timeout=0.5)
    assert values == {"block": "Order number: AB-1234\nPrice"}


def test_pattern_without_a_match_or_with_an_unfit_value_is_missing() -> None:
    opts = options(
        {"price": NUMBER, "none": STRING},
        {"price": {"regex": r"Contact: (\S+)", "group": 1}, "none": {"regex": "zzz"}},
    )

    values, warnings = run_patterns(CONTENT, opts, ["price", "none"], timeout=0.5)

    assert values == {}
    assert warnings == ["structured_value_invalid"]


def test_catastrophic_pattern_times_out() -> None:
    """Review Focus 2: ``(a+)+$`` on 50,000 ``a`` and a ``b`` backtracks for ever."""
    opts = options(
        {"evil": STRING, "fine": STRING},
        {"evil": {"regex": r"(a+)+$"}, "fine": {"regex": r"b"}},
    )
    content = "a" * 50_000 + "b"

    started = time.monotonic()
    values, warnings = run_patterns(content, opts, ["evil", "fine"], timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert values == {"fine": "b"}
    assert warnings == [PATTERN_TIMEOUT_WARNING]  # the field's name is not in it


def test_pattern_matches_capped_per_field() -> None:
    opts = options({"numbers": STRINGS}, {"numbers": {"regex": r"\d+"}})
    content = " ".join(str(number) for number in range(MAX_FIELD_VALUES + 50))

    values, warnings = run_patterns(content, opts, ["numbers"], timeout=0.5)

    assert len(values["numbers"]) == MAX_FIELD_VALUES
    assert values["numbers"][:3] == ["0", "1", "2"]
    assert warnings == [STRUCTURED_TRUNCATED_WARNING]


def test_empty_matches_are_skipped() -> None:
    opts = options({"letters": STRINGS}, {"letters": {"regex": r"x*"}})

    values, warnings = run_patterns("ab xx c x", opts, ["letters"], timeout=0.5)

    assert values == {"letters": ["xx", "x"]}
    assert warnings == []


MARKDOWN = """# Prices

Some text | with a pipe that is no table.

| Item | Price |
| :--- | ----: |
| Tea  | 2,50  |
| Cake \\| pie | 3 |

```
| not | a table |
| --- | --- |
| in  | code    |
```

|   |   |
|---|---|
| a | b |
| c |

Header | Only
--- | ---
x | y | z
"""


def test_markdown_pipe_tables() -> None:
    tables = tables_from_markdown(MARKDOWN)

    assert tables == [
        Table(
            index=0,
            caption=None,
            headers=["Item", "Price"],
            rows=[["Tea", "2,50"], ["Cake | pie", "3"]],
        ),
        # an empty header row (a converter's table without headers) is no header
        Table(index=1, caption=None, headers=[], rows=[["a", "b"], ["c", ""]]),
        # pipes at the ends are optional; a longer row is cut to the header's cells
        Table(index=2, caption=None, headers=["Header", "Only"], rows=[["x", "y"]]),
    ]


def test_markdown_tables_are_capped() -> None:
    one = "| a | b |\n| - | - |\n| 1 | 2 |\n\n"

    tables, truncated = markdown_tables(one * (MAX_TABLES + 10))

    assert len(tables) == MAX_TABLES
    assert truncated is True
    assert tables_from_markdown("no tables here") == []
    assert markdown_tables("| a |\n| - |\n")[0] == []  # a header without rows is no table
