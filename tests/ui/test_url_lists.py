"""URL lists of the UI's batches (ruling P10-S19): CSV and JSON files, typed lines, their
bounds and messages, and the split into batches."""

import json
import re
import time
from pathlib import Path

import pytest

from text_extraction import netguard
from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.ui.i18n import Message
from text_extraction.ui.url_lists import (
    URL_PROBLEMS,
    UrlList,
    UrlListError,
    parse_url_file,
    parse_url_lines,
    split_batches,
)

A = "https://example.org/a"
B = "https://example.org/b"
C = "http://example.org/c?x=1;y=2"
# A URL of MAX_URL_LENGTH characters; one more is no URL (and still within the field limit).
LONGEST = "https://example.org/" + "x" * (MAX_URL_LENGTH - 20)


def parse(text: str | bytes, max_rows: int = 1000) -> UrlList:
    data = text.encode() if isinstance(text, str) else text
    return parse_url_file(data, max_rows=max_rows)


def english(invalid: list[tuple[int, Message]]) -> list[tuple[int, str]]:
    """The problems of a list with their English texts."""
    return [(row, reason.text("en")) for row, reason in invalid]


def refused(text: str | bytes, message: str, max_rows: int = 1000) -> None:
    with pytest.raises(UrlListError) as caught:
        parse(text, max_rows)
    assert str(caught.value) == message


def test_parse_url_file_bounds_and_errors() -> None:
    # A CSV with a URL header in the second column (any case); other columns are ignored.
    assert parse(f"title,URL\nFirst,{A}\nSecond, {B} \n") == UrlList(
        urls=[A, B], invalid=[], duplicates=0, rows=2
    )
    # A header-less CSV: the first column.
    assert parse(f"{A},first\n{B},second\n").urls == [A, B]
    # A first row that is not http(s) is a header.
    assert parse(f"address\n{A}\n").urls == [A]
    # A ";" CSV (a URL with a ";" stays whole) and a tab-separated one.
    assert parse(f'name;url\nx;"{C}"\ny;{A}\n').urls == [C, A]
    assert parse(f"url\tname\n{A}\tx\n").urls == [A]
    # JSON: an array of strings, and one of objects with url (other keys ignored).
    assert parse(json.dumps([A, B])).urls == [A, B]
    assert parse(json.dumps([{"url": A, "title": "x"}, {"url": B}])).urls == [A, B]
    refused(json.dumps([{"href": A}]), "Expected a JSON array of URLs or of objects with url.")
    refused(json.dumps([A, 3]), "Expected a JSON array of URLs or of objects with url.")
    refused(json.dumps({"urls": [A]}), "Expected a JSON array of URLs or of objects with url.")
    # A BOM is dropped; Latin-1 is not UTF-8.
    assert parse(b"\xef\xbb\xbf" + f"url\n{A}\n".encode()).urls == [A]
    refused("url\nhttps://example.org/caf\xe9\n".encode("latin-1"), "The file is not UTF-8.")
    # Values that are not http(s) URLs are reported with their row numbers (the header is row
    # 1), and never kept.
    found = parse(
        "url\n"
        f"{A}\n"
        "ftp://example.org/file\n"
        "javascript:alert(1)\n"
        '"=HYPERLINK(""http://evil"")"\n'
        "@cmd\n"
        "https://\n"
        "<svg onload=alert(1)>\n"
        f"{LONGEST}x\n"
        "https://example.org/a b\n"
        f"{B}\n"
    )
    assert found.urls == [A, B]
    assert [row for row, _ in found.invalid] == [3, 4, 5, 6, 7, 8, 9, 10]
    assert all("evil" not in reason.text("en") for _, reason in found.invalid)
    assert found.rows == 10
    # Duplicates after stripping are dropped and counted.
    assert parse(f"{A}\n {A}\n{B}\n{A}\n") == UrlList(urls=[A, B], invalid=[], duplicates=2, rows=4)
    # 100,000 rows are taken, 100,001 refused, each fast.
    rows = "".join(f"https://example.org/page-{number}\n" for number in range(100_000))
    started = time.monotonic()
    assert len(parse(rows, 100_000).urls) == 100_000
    assert time.monotonic() - started < 2
    started = time.monotonic()
    refused(rows + f"{A}\n", "The file has more than 100000 rows.", 100_000)
    assert time.monotonic() - started < 2
    many = json.dumps([f"https://example.org/{number}" for number in range(11)])
    refused(many, "The file has more than 10 rows.", 10)
    # JSON nested 100,000 levels deep, and a truncated JSON.
    refused("[" * 100_000 + "]" * 100_000, "The file is not valid JSON.")
    refused(json.dumps([A, B])[:-3], "The file is not valid JSON.")
    # A CSV field over MAX_URL_LENGTH + 1 characters, also a quoted one over the csv limit.
    refused(f"url,title\n{A},{'t' * 10_000}\n", "A field is too long.")
    refused(f'url\n"{"q" * 200_000}"\n', "A field is too long.")
    # A file of one column whose URLs hold commas or semicolons (not in every row alike) keeps
    # them whole.
    commas = "https://en.example.org/wiki/Washington,_D.C."
    maps = "https://maps.example.org/@52.5,13.4,15z"
    assert parse(f"{commas}\n{maps}\n{A};x=1\n").urls == [commas, maps, f"{A};x=1"]
    # Columns per row are bounded, also in a line the sniffing reads.
    refused(f"url\n{A}" + "\t" * 5000 + "\n", "A row has too many columns.")
    refused(f"url,title\n{A},x\n" + "," * 5000 + "\n", "A row has too many columns.")
    # A 10 KB JSON string is a row that is not a URL, not a refusal of the file.
    found = parse(json.dumps([A, "https://example.org/" + "x" * 10_000]))
    assert (found.urls, english(found.invalid)) == (
        [A],
        [(2, "URL is longer than 2048 characters")],
    )
    # An empty file has no URLs.
    assert parse("") == UrlList(urls=[], invalid=[], duplicates=0, rows=0)


def test_parse_url_lines() -> None:
    found = parse_url_lines([f" {A} ", "", "javascript:alert(1)", A, B], max_rows=10)

    assert (found.urls, english(found.invalid), found.duplicates, found.rows) == (
        [A, B],
        [(3, "URL scheme must be http or https")],
        1,
        4,
    )
    with pytest.raises(UrlListError, match=r"^The list has more than 3 URLs\.$"):
        parse_url_lines([A, B, C, "https://example.org/d"], max_rows=3)


def test_split_batches() -> None:
    urls = [f"https://example.org/{number}" for number in range(2500)]

    parts = split_batches(urls, per_batch=1000, max_batches=5)

    assert [len(part) for part in parts] == [1000, 1000, 500]
    assert [url for part in parts for url in part] == urls
    assert split_batches(urls[:1000], per_batch=1000, max_batches=1) == [urls[:1000]]
    assert split_batches([], per_batch=10, max_batches=1) == []
    with pytest.raises(UrlListError, match=r"^Select at most 5000 URLs at once\.$"):
        split_batches(urls * 3, per_batch=1000, max_batches=5)


def test_url_list_problems_in_german() -> None:
    found = parse_url_lines(
        ["javascript:alert(1)", "https://", A, "https://example.org/a b", LONGEST + "x"],
        max_rows=10,
    )

    assert [(row, reason.text("de")) for row, reason in found.invalid] == [
        (1, "Das Schema der URL muss http oder https sein"),
        (2, "Die URL hat keinen Host"),
        (4, "Die URL enthält ein Leerzeichen oder ein Steuerzeichen"),
        (5, f"Die URL ist länger als {MAX_URL_LENGTH} Zeichen"),
    ]
    for data, german in (
        (
            "url\nhttps://example.org/caf\xe9\n".encode("latin-1"),
            "Die Datei ist nicht UTF-8-kodiert.",
        ),
        (b"[1", "Die Datei ist kein gültiges JSON."),
        (f"url,title\n{A},{'t' * 10_000}\n".encode(), "Ein Feld ist zu lang."),
        (f"{A}\n{B}\n{C}\n".encode(), "Die Datei hat mehr als 2 Zeilen."),
    ):
        with pytest.raises(UrlListError) as caught:
            parse(data, max_rows=2)
        assert caught.value.message.text("de") == german
    with pytest.raises(UrlListError) as caught:
        split_batches([A, B, C], per_batch=1, max_batches=2)
    assert caught.value.message.text("de") == "Wählen Sie höchstens 2 URLs auf einmal."
    assert str(caught.value) == "Select at most 2 URLs at once."  # English for logs and tests


def test_every_url_problem_of_netguard_has_a_text() -> None:
    source = Path(netguard.__file__).read_text(encoding="utf-8")
    raised = set(re.findall(r'InvalidUrlError\("([^"]+)"\)', source))

    assert raised
    assert raised == set(URL_PROBLEMS)
