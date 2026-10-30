"""URL lists of the UI's batches (ruling P10-S19): CSV and JSON files, typed lines, their
bounds and messages, and the split into batches."""

import json
import time

import pytest

from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.ui.url_lists import (
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
    assert all(reason and "evil" not in reason for _, reason in found.invalid)
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
    # An empty file has no URLs.
    assert parse("") == UrlList(urls=[], invalid=[], duplicates=0, rows=0)


def test_parse_url_lines() -> None:
    found = parse_url_lines([f" {A} ", "", "javascript:alert(1)", A, B], max_rows=10)

    assert found == UrlList(
        urls=[A, B], invalid=[(3, "URL scheme must be http or https")], duplicates=1, rows=4
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
