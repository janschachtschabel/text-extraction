"""Sitemap parsing within bounds (rulings P9-S8, P9-S9; Review Focus 2)."""

import gzip
import random
import struct
import time
import tracemalloc
import zlib
from itertools import pairwise
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from text_extraction.site.sitemaps import inflate_bounded, pack_parts, parse_sitemap
from text_extraction.site.types import SitemapEntry

FIXTURES = Path(__file__).parent / "sitemaps"
BASE = "https://lernen.example.org/sitemap.xml"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
_ENTRIES_JSON = TypeAdapter(list[SitemapEntry])


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def urlset(count: int, *, close: bool = True) -> bytes:
    urls = "".join(f"<url><loc>https://lernen.example.org/p{i}</loc></url>" for i in range(count))
    return f"<urlset {NS}>{urls}{'</urlset>' if close else ''}".encode()


def locs(entries: list[SitemapEntry]) -> list[str]:
    return [entry.loc for entry in entries]


def test_urlset_entries_and_fields() -> None:
    parsed = parse_sitemap(fixture("urlset.xml"), BASE, max_entries=100)

    assert parsed.format == "urlset"
    assert parsed.error is None
    assert parsed.truncated is False
    assert parsed.entry_count == 3
    assert parsed.children == []
    first, second, third = parsed.entries
    assert first == SitemapEntry(
        loc="https://lernen.example.org/",
        lastmod="2026-10-01",
        changefreq="weekly",
        priority=0.8,
    )
    assert second.loc == "https://lernen.example.org/mathe/bruchrechnung?seite=2&x=1"
    assert second.lastmod == "2026-09-15T10:00:00+00:00"
    assert second.changefreq is None  # "sometimes" is not in the protocol
    assert second.priority is None  # 1.5 is outside 0 to 1
    assert third == SitemapEntry(loc="https://lernen.example.org/physik/")


def test_index_children() -> None:
    parsed = parse_sitemap(fixture("index.xml"), BASE, max_entries=100)

    assert parsed.format == "index"
    assert parsed.error is None
    assert parsed.entries == []
    assert [(child.url, child.lastmod) for child in parsed.children] == [
        ("https://lernen.example.org/sitemap-pages.xml", "2026-10-01"),
        ("https://lernen.example.org/sitemap-posts.xml.gz", None),
    ]  # the ftp:// child is dropped
    assert {child.crawl for child in parsed.children} == {"listed"}  # the runner decides
    assert {child.target for child in parsed.children} == {None}


def test_index_child_lastmod_is_bounded() -> None:
    body = (
        f"<sitemapindex {NS}><sitemap><loc>/a.xml</loc><lastmod>{'9' * 1000}</lastmod>"
        "</sitemap></sitemapindex>"
    ).encode()

    parsed = parse_sitemap(body, BASE, max_entries=10)

    assert parsed.children[0].lastmod is None


def test_text_rss_atom_formats() -> None:
    text = parse_sitemap(fixture("text.txt"), BASE, max_entries=100)
    rss = parse_sitemap(fixture("rss.xml"), BASE, max_entries=100)
    atom = parse_sitemap(fixture("atom.xml"), BASE, max_entries=100)

    assert text.format == "text"
    assert locs(text.entries) == [
        "https://lernen.example.org/a",
        "https://lernen.example.org/b",
        "https://lernen.example.org/c",
    ]
    assert text.entry_count == 3
    assert rss.format == "rss"
    assert rss.entries == [
        SitemapEntry(
            loc="https://lernen.example.org/mathe/bruchrechnung",
            lastmod="Thu, 29 Oct 2026 10:00:00 GMT",
        ),
        SitemapEntry(loc="https://lernen.example.org/physik/optik"),
    ]  # the channel's own <link> is no entry
    assert atom.format == "atom"
    assert atom.entries == [
        SitemapEntry(
            loc="https://lernen.example.org/mathe/bruchrechnung", lastmod="2026-10-29T10:00:00Z"
        ),
        SitemapEntry(loc="https://lernen.example.org/physik/optik"),
    ]  # rel="edit" and the feed's rel="self" are no entries
    assert (text.error, rss.error, atom.error) == (None, None, None)


def test_blank_fields_do_not_hide_later_ones() -> None:
    """An item's empty ``atom:link`` (local name ``link``) does not stand for its real link."""
    body = (
        b'<rss xmlns:atom="http://www.w3.org/2005/Atom"><channel><item>'
        b'<atom:link href="https://a.example/feed" rel="self"/>'
        b"<link>https://a.example/post</link><pubDate> </pubDate><pubDate>Fri</pubDate>"
        b"</item></channel></rss>"
    )

    parsed = parse_sitemap(body, BASE, max_entries=10)

    assert parsed.entries == [SitemapEntry(loc="https://a.example/post", lastmod="Fri")]


def test_text_sitemap_with_crlf_and_blanks() -> None:
    body = b"\xef\xbb\xbf  https://a.example/x  \r\n\r\n\thttps://a.example/y\r\n\xff\xfe/bad\n"

    parsed = parse_sitemap(body, BASE, max_entries=10)

    assert parsed.format == "text"
    assert locs(parsed.entries) == ["https://a.example/x", "https://a.example/y"]


def test_relative_and_invalid_locs() -> None:
    long_loc = "https://lernen.example.org/" + "a" * 2100
    body = (
        f"<urlset {NS}>"
        "<url><loc>/absolute-path</loc></url>"
        "<url><loc>  relative/page.html  </loc></url>"
        "<url><loc>mailto:info@lernen.example.org</loc></url>"
        "<url><loc>javascript:void(0)</loc></url>"
        "<url><loc>ftp://lernen.example.org/f</loc></url>"
        "<url><loc></loc></url>"
        "<url><lastmod>2026-10-01</lastmod></url>"
        f"<url><loc>{long_loc}</loc></url>"
        "<url><loc>http://[::1/</loc></url>"
        "<url><loc>HTTPS://Other.Example:443/x#frag</loc></url>"
        "</urlset>"
    ).encode()

    parsed = parse_sitemap(body, "https://lernen.example.org/maps/sitemap.xml", max_entries=100)

    assert locs(parsed.entries) == [
        "https://lernen.example.org/absolute-path",
        "https://lernen.example.org/maps/relative/page.html",
        "https://other.example/x",
    ]
    assert parsed.entry_count == 3
    assert parsed.error is None


def test_bom_and_namespace_prefixes() -> None:
    parsed = parse_sitemap(fixture("bom_urlset.xml"), BASE, max_entries=100)

    assert parsed.format == "urlset"
    assert parsed.error is None
    assert parsed.entries == [
        SitemapEntry(loc="https://lernen.example.org/", lastmod="2026-10-01"),
        SitemapEntry(loc="https://lernen.example.org/b"),
    ]


def test_no_namespace_is_read_too() -> None:
    body = b"<urlset><url><loc>https://a.example/</loc></url></urlset>"

    assert locs(parse_sitemap(body, BASE, max_entries=10).entries) == ["https://a.example/"]


def test_gzip_detected_by_magic_bytes() -> None:
    """Served as ``application/octet-stream`` and named ``.xml``: only the bytes tell."""
    xml = fixture("urlset.xml")
    body, cut = inflate_bounded(gzip.compress(xml), 1_000_000)

    assert (body, cut) == (xml, False)
    assert parse_sitemap(body, BASE, max_entries=100).entry_count == 3


def test_plain_body_is_not_inflated() -> None:
    xml = fixture("urlset.xml")

    assert inflate_bounded(xml, 10) == (xml, False)  # the fetch cap bounds a plain body


def test_inflate_exactly_at_the_limit_is_not_cut() -> None:
    assert inflate_bounded(gzip.compress(b"x" * 1000), 1000) == (b"x" * 1000, False)
    assert inflate_bounded(gzip.compress(b"x" * 1001), 1000) == (b"x" * 1000, True)


def test_inflate_concatenated_members() -> None:
    body = gzip.compress(b"<urlset>") + gzip.compress(b"</urlset>")

    assert inflate_bounded(body, 1000) == (b"<urlset></urlset>", False)


def test_inflate_broken_stream_keeps_what_it_read() -> None:
    whole = gzip.compress(urlset(50))
    body, cut = inflate_bounded(whole[: len(whole) // 2], 1_000_000)

    assert cut is True
    parsed = parse_sitemap(body, BASE, max_entries=100)
    assert parsed.truncated is True
    assert 0 < parsed.entry_count < 50

    garbage, cut = inflate_bounded(b"\x1f\x8b" + b"\x00" * 50, 1000)
    assert (garbage, cut) == (b"", True)


@pytest.fixture(scope="module")
def gzip_bomb() -> bytes:
    """A valid gzip file of 1 GiB of zeros, about 1 MB compressed. Built from one deflate block
    of 1 MiB of zeros repeated (after a full flush the window is all zeros, so every further
    block compresses the same), which takes a fraction of compressing 1 GiB."""
    zeros = bytes(1 << 20)
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    first = compressor.compress(zeros) + compressor.flush(zlib.Z_FULL_FLUSH)
    block = compressor.compress(zeros) + compressor.flush(zlib.Z_FULL_FLUSH)
    end = compressor.flush()
    crc = 0
    for _ in range(1024):
        crc = zlib.crc32(zeros, crc)
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
    trailer = struct.pack("<II", crc, (1 << 30) & 0xFFFFFFFF)
    return header + first + block * 1023 + end + trailer


def test_gzip_bomb_cut_at_limit(gzip_bomb: bytes) -> None:
    assert len(gzip_bomb) < 2_000_000
    tracemalloc.start()
    try:
        body, cut = inflate_bounded(gzip_bomb, 1_000_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(body) == 1_000_000
    assert cut is True
    assert peak < 50_000_000


@pytest.mark.parametrize("name", ["billion_laughs.xml", "external_entity.xml"])
def test_entities_refused(name: str) -> None:
    started = time.monotonic()
    parsed = parse_sitemap(fixture(name), BASE, max_entries=100)

    assert parsed.error == "invalid"
    assert parsed.entries == []
    assert parsed.entry_count == 0
    assert time.monotonic() - started < 1.0  # nothing was expanded


def test_entity_without_doctype_is_invalid() -> None:
    body = f"<urlset {NS}><url><loc>https://a.example/&undefined;</loc></url></urlset>".encode()

    assert parse_sitemap(body, BASE, max_entries=10).error == "invalid"


def test_html_page_is_unsupported() -> None:
    parsed = parse_sitemap(fixture("html_error.html"), BASE, max_entries=100)

    assert parsed.error == "unsupported"
    assert parsed.entries == []


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body><p>Not found</p></body></html>",
        b"<?xml version='1.0'?><foo><url><loc>https://a.example/</loc></url></foo>",
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"></rdf:RDF>',
    ],
)
def test_other_roots_are_unsupported(body: bytes) -> None:
    parsed = parse_sitemap(body, BASE, max_entries=100)

    assert parsed.error == "unsupported"
    assert parsed.entries == []


@pytest.mark.parametrize("body", [b"<urlset><url><loc>", b"<<<", b"<urlset", b"<"])
def test_broken_xml_before_any_entry_is_invalid(body: bytes) -> None:
    parsed = parse_sitemap(body, BASE, max_entries=100)

    assert parsed.error == "invalid"
    assert parsed.entries == []


def test_empty_body_is_an_empty_text_sitemap() -> None:
    parsed = parse_sitemap(b" \n\t", BASE, max_entries=100)

    assert (parsed.format, parsed.entries, parsed.error) == ("text", [], None)


def test_parse_error_after_entries_keeps_them_truncated() -> None:
    body = urlset(2, close=False) + b"<url><loc>https://lernen.example.org/cut</lo"

    parsed = parse_sitemap(body, BASE, max_entries=100)

    assert parsed.error is None
    assert parsed.truncated is True
    assert locs(parsed.entries) == [
        "https://lernen.example.org/p0",
        "https://lernen.example.org/p1",
    ]
    assert parsed.entry_count == 2


def test_max_entries_counts_all() -> None:
    parsed = parse_sitemap(urlset(25), BASE, max_entries=10)

    assert len(parsed.entries) == 10
    assert locs(parsed.entries) == [f"https://lernen.example.org/p{i}" for i in range(10)]
    assert parsed.entry_count == 25
    assert parsed.truncated is True


def test_max_entries_zero_counts_only() -> None:
    parsed = parse_sitemap(urlset(5), BASE, max_entries=0)

    assert (parsed.entries, parsed.entry_count, parsed.truncated) == ([], 5, True)


def test_entries_at_max_are_not_truncated() -> None:
    parsed = parse_sitemap(urlset(10), BASE, max_entries=10)

    assert (len(parsed.entries), parsed.truncated) == (10, False)


def test_closed_elements_are_not_kept() -> None:
    """Counting 10,000 entries without keeping them stays far below the ~3 MB the whole tree
    would take: each element is dropped once it is read."""
    body = urlset(10_000)
    tracemalloc.start()
    try:
        counted = parse_sitemap(body, BASE, max_entries=0)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert counted.entry_count == 10_000
    assert peak < 1_000_000


def test_large_urlset_streams() -> None:
    parsed = parse_sitemap(urlset(60_000), BASE, max_entries=60_000)

    assert parsed.entry_count == len(parsed.entries) == 60_000
    assert parsed.entries[-1].loc == "https://lernen.example.org/p59999"


def test_pack_parts_under_limit() -> None:
    entries = [
        SitemapEntry(
            loc=f"https://lernen.example.org/kategorie/{i}/seite-mit-einem-langen-namen",
            lastmod="2026-10-30T12:00:00+00:00",
            changefreq="weekly",
            priority=0.5,
        )
        for i in range(60_000)
    ]

    parts = pack_parts(entries, 1_000_000)

    assert len(parts) > 1
    for part in parts:
        assert len(_ENTRIES_JSON.dump_json(part)) <= 1_000_000
    assert [entry for part in parts for entry in part] == entries
    # greedy: every part but the last is full enough that the next entry would not fit
    for part, following in pairwise(parts):
        assert len(_ENTRIES_JSON.dump_json([*part, following[0]])) > 1_000_000


def test_pack_parts_small_and_empty() -> None:
    entries = [SitemapEntry(loc="https://a.example/1"), SitemapEntry(loc="https://a.example/2")]

    assert pack_parts(entries, 1_000_000) == [entries]
    assert pack_parts([], 1_000_000) == [[]]


def test_pack_parts_entry_larger_than_the_limit_gets_its_own_part() -> None:
    big = SitemapEntry(loc="https://a.example/" + "x" * 500)
    small = SitemapEntry(loc="https://a.example/s")

    assert pack_parts([small, big, small], 100) == [[small], [big], [small]]


def test_inflate_copies_the_input_only_in_slices() -> None:
    """A large, very compressible body is fed to the inflater in slices: no step copies the
    whole rest of the input (which would make inflating it quadratic)."""
    to_ab = bytes.maketrans(bytes(range(256)), bytes(b"ab"[i & 1] for i in range(256)))
    rng = random.Random(9)  # noqa: S311 - a fixed seed, not a secret
    xml = urlset(1).replace(b"</urlset>", b"") + rng.randbytes(2_000_000).translate(to_ab)
    compressed = gzip.compress(xml * 20, compresslevel=1)
    assert len(compressed) > 4_000_000

    tracemalloc.start()
    try:
        body, cut = inflate_bounded(compressed, 30_000_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (len(body), cut) == (30_000_000, True)
    # the output and its copy, plus at most a slice of input at a time
    assert peak < 2 * 30_000_000 + 1_000_000


IMAGE_URL = (
    "<url><loc>https://a.example/1</loc>"
    "<image:image><image:loc>https://a.example/1.png</image:loc></image:image></url>"
)


@pytest.mark.parametrize(
    "body",
    [
        f"<urlset {NS}>{IMAGE_URL}<url><loc>https://a.example/2</loc></url></urlset>",
        f"<urlset {NS}><url><loc>https://a.example/1</loc></url>"
        "<url><loc>https://a.example/2</loc><video:video><video:title>T</video:title>"
        "</video:video></url></urlset>",
        "<sm:urlset><sm:url><sm:loc>https://a.example/1</sm:loc></sm:url>"
        "<sm:url><sm:loc>https://a.example/2</sm:loc></sm:url></sm:urlset>",
    ],
)
def test_undeclared_prefixes_are_ignored(body: str) -> None:
    """Extensions used without their ``xmlns``: elements still match by local name, the
    extension's ``loc`` is no entry, and nothing is counted twice."""
    parsed = parse_sitemap(body.encode(), BASE, max_entries=10)

    assert parsed.error is None
    assert parsed.truncated is False
    assert locs(parsed.entries) == ["https://a.example/1", "https://a.example/2"]
    assert parsed.entry_count == 2


def test_undeclared_prefix_retry_keeps_entities_refused() -> None:
    body = fixture("external_entity.xml").replace(b"<url>", b"<url><image:image/>")

    assert parse_sitemap(body, BASE, max_entries=10).error == "invalid"


def test_undeclared_prefix_retry_keeps_caps() -> None:
    body = urlset(25).replace(b"</urlset>", IMAGE_URL.encode() + b"</urlset>")

    parsed = parse_sitemap(body, BASE, max_entries=10)

    assert (len(parsed.entries), parsed.entry_count, parsed.truncated) == (10, 26, True)


def test_deep_nesting_is_invalid_and_bounded() -> None:
    """A gzip bomb of a million nested elements: refused early, memory stays small."""
    nested = b"<urlset>" + b"<a>" * 1_000_000
    body, cut = inflate_bounded(gzip.compress(nested), 50_000_000)
    assert (len(body), cut) == (len(nested), False)

    tracemalloc.start()
    try:
        parsed = parse_sitemap(body, BASE, max_entries=10)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert parsed.error == "invalid"
    assert peak < 5_000_000


def test_deep_nesting_after_entries_is_truncated() -> None:
    body = urlset(2, close=False) + b"<a>" * 100

    parsed = parse_sitemap(body, BASE, max_entries=10)

    assert (parsed.error, parsed.entry_count, parsed.truncated) == (None, 2, True)


def test_legit_depth_is_read() -> None:
    body = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="https://a.example/x"/>'
        b'<content type="xhtml"><div><div><div><p><b>x</b></p></div></div></div></content>'
        b"</entry></feed>"
    )

    assert locs(parse_sitemap(body, BASE, max_entries=10).entries) == ["https://a.example/x"]


@pytest.mark.parametrize("encoding", ["bogus", "shift_jis", "big5", "utf-32", "rot13"])
def test_unsupported_encoding_is_invalid(encoding: str) -> None:
    body = f'<?xml version="1.0" encoding="{encoding}"?><urlset {NS}></urlset>'.encode()

    parsed = parse_sitemap(body, BASE, max_entries=10)

    assert (parsed.format, parsed.error) == (None, "invalid")


def test_trailing_junk_after_the_root_is_not_truncated() -> None:
    body = urlset(2) + b"<stray/>"

    parsed = parse_sitemap(body, BASE, max_entries=10)

    assert (parsed.error, parsed.entry_count, parsed.truncated) == (None, 2, False)


@pytest.mark.parametrize("bom", [b"\xff\xfe", b"\xfe\xff"])
def test_utf16_is_invalid_not_an_empty_text_sitemap(bom: bytes) -> None:
    codec = "utf-16-le" if bom == b"\xff\xfe" else "utf-16-be"
    body = bom + f"<urlset {NS}><url><loc>https://a.example/</loc></url></urlset>".encode(codec)

    assert parse_sitemap(body, BASE, max_entries=10).error == "invalid"
