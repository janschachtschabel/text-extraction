"""Feeds and CMS APIs a start page names (package 26)."""

import pytest

from text_extraction.site.apis import MAX_FEEDS, detect_site_apis
from text_extraction.site.types import SiteApis

BASE = "https://ex.org/"


def detect(head: str = "", body: str = "", headers: dict[str, str] | None = None) -> SiteApis:
    from text_extraction.convert.markup import parse_html

    html = f"<html><head>{head}</head><body>{body}</body></html>"
    return detect_site_apis(parse_html(html), BASE, headers or {})


def test_wordpress_from_link_tag() -> None:
    found = detect('<link rel="https://api.w.org/" href="https://ex.org/wp-json/">')
    assert found.wordpress == "https://ex.org/wp-json/"


def test_wordpress_root_gets_a_trailing_slash() -> None:
    found = detect('<link rel="https://api.w.org/" href="/wp-json">')
    assert found.wordpress == "https://ex.org/wp-json/"


def test_wordpress_from_link_header() -> None:
    found = detect(headers={"link": '<https://ex.org/wp-json/>; rel="https://api.w.org/"'})
    assert found.wordpress == "https://ex.org/wp-json/"


def test_wordpress_root_on_other_host_ignored() -> None:
    found = detect('<link rel="https://api.w.org/" href="https://cdn.other.net/wp-json/">')
    assert found.wordpress is None


@pytest.mark.parametrize(
    ("head", "body"),
    [
        ("<script>var x = {drupalSettings: {}};</script>", ""),
        ("", '<div data-drupal-selector="x"></div>'),
        ('<meta name="generator" content="Drupal 10 (https://www.drupal.org)">', ""),
    ],
)
def test_drupal_markers(head: str, body: str) -> None:
    assert detect(head, body).drupal == "https://ex.org/jsonapi"


def test_plain_page_is_no_drupal() -> None:
    assert detect("<title>x</title>", "<p>hello</p>").drupal is None


def test_feeds_from_head() -> None:
    found = detect(
        '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        '<link rel="alternate" type="text/html" href="/de/">'
        '<link rel="alternate stylesheet" type="application/atom+xml" href="/s.xml">'
        '<link rel="alternate" type="application/rss+xml" href="https://other.net/feed">'
        '<link rel="alternate" type="application/atom+xml" href="https://EX.org/atom">'
    )
    assert found.feeds == ["https://ex.org/feed/", "https://ex.org/atom"]


def test_feeds_capped() -> None:
    head = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="/f{i}">' for i in range(5)
    )
    assert detect(head).feeds == [f"https://ex.org/f{i}" for i in range(MAX_FEEDS)]


def test_no_signals() -> None:
    assert detect("<title>x</title>") == SiteApis()


# --- parsers -------------------------------------------------------------------------------

import json  # noqa: E402

from text_extraction.site.apis import (  # noqa: E402
    api_type_hint,
    parse_drupal_index,
    parse_drupal_nodes,
    parse_wordpress_posts,
    parse_wordpress_types,
    path_matches,
    wordpress_list_url,
    wordpress_types_url,
)

ROOT = "https://ex.org/wp-json/"


def dump(value: object) -> bytes:
    return json.dumps(value).encode()


def wp_types() -> dict[str, object]:
    return {
        "post": {"slug": "post", "rest_base": "posts"},
        "page": {"slug": "page", "rest_base": "pages"},
        "tribe_events": {"slug": "tribe_events", "rest_base": "events"},
        "attachment": {"slug": "attachment", "rest_base": "media"},
        "wp_block": {"slug": "wp_block", "rest_base": "blocks"},
    }


def test_wordpress_types_children() -> None:
    parsed = parse_wordpress_types(dump(wp_types()), ROOT, path_prefix=None)
    assert parsed.format == "wordpress_types"
    assert [c.url for c in parsed.children] == [
        wordpress_list_url(ROOT, base) for base in ("posts", "pages", "events")
    ]
    assert {c.crawl for c in parsed.children} == {"listed"}
    assert wordpress_list_url(ROOT, "posts") == (
        "https://ex.org/wp-json/wp/v2/posts?per_page=100&_fields=link,title,modified_gmt"
    )
    assert wordpress_list_url(ROOT, "posts", 2).endswith("modified_gmt&page=2")


def test_wordpress_types_path_prefix() -> None:
    types = wp_types() | {"v": {"slug": "veranstaltung", "rest_base": "veranstaltung"}}
    parsed = parse_wordpress_types(dump(types), ROOT, path_prefix="/veranstaltungen/")
    assert [c.url for c in parsed.children] == [wordpress_list_url(ROOT, "veranstaltung")]


def items(count: int) -> list[dict[str, str]]:
    return [
        {
            "link": f"https://ex.org/p{i}/",
            "title": {"rendered": "<b>Fortbildung</b> &#8211; Mathe"},  # type: ignore[dict-item]
            "modified_gmt": "2026-01-01T00:00:00",
        }
        for i in range(count)
    ]


def posts(body: object, url: str = wordpress_list_url(ROOT, "posts"), **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {"site_host": "ex.org", "total_pages": None, "max_entries": 1000}
    args.update(kw)
    return parse_wordpress_posts(dump(body), url, **args)  # type: ignore[arg-type]


def test_wordpress_posts_entries_and_next() -> None:
    parsed = posts(items(100), total_pages=3)
    assert parsed.format == "wordpress_posts"
    assert len(parsed.entries) == parsed.entry_count == 100
    assert parsed.entries[0].title == "Fortbildung \u2013 Mathe"
    assert parsed.entries[0].lastmod == "2026-01-01T00:00:00"
    assert [c.url for c in parsed.children] == [wordpress_list_url(ROOT, "posts", 2)]
    page3 = posts(items(100), url=wordpress_list_url(ROOT, "posts", 3), total_pages=3)
    assert page3.children == []
    assert posts(items(40)).children == []
    assert len(posts(items(100)).children) == 1


def test_wordpress_posts_max_entries() -> None:
    parsed = posts(items(10), max_entries=4)
    assert (len(parsed.entries), parsed.entry_count, parsed.truncated) == (4, 10, True)


def test_wordpress_posts_drops_bad_links() -> None:
    bad = [
        {"link": "javascript:alert(1)", "title": {"rendered": "x"}},
        {"link": 42, "title": {"rendered": "x"}},
        {"link": "https://other.org/a/", "title": {"rendered": "x"}},
        {"link": "https://ex.org/ok/", "title": "nope"},
    ]
    parsed = posts(bad)
    assert [e.loc for e in parsed.entries] == ["https://ex.org/ok/"]
    assert parsed.entry_count == 1


def test_wordpress_posts_hostile() -> None:
    big = [{"link": "https://ex.org/a/", "title": {"rendered": "x" * 1_000_000}}]
    assert len(posts(big).entries[0].title or "") == 200
    nested = parse_wordpress_posts(
        b"[" * 100_000, "https://ex.org/x", site_host="ex.org", total_pages=None, max_entries=5
    )
    assert nested.error == "invalid"
    huge = posts(items(1), total_pages=999_999_999)
    assert [c.url for c in huge.children] == [wordpress_list_url(ROOT, "posts", 2)]


def test_drupal_index_and_nodes() -> None:
    origin = "https://ex.org"
    index = {
        "links": {
            "self": {"href": "https://ex.org/jsonapi"},
            "node--article": {"href": "https://ex.org/jsonapi/node/article"},
            "node--event": {"href": "https://ex.org/jsonapi/node/event"},
            "file--file": {"href": "https://ex.org/jsonapi/file/file"},
        }
    }
    parsed = parse_drupal_index(dump(index), origin, path_prefix=None)
    assert parsed.format == "drupal_index"
    assert [c.url for c in parsed.children] == [
        "https://ex.org/jsonapi/node/article?page%5Blimit%5D=50"
        "&fields%5Bnode--article%5D=title%2Cpath%2Cchanged%2Cdrupal_internal__nid",
        "https://ex.org/jsonapi/node/event?page%5Blimit%5D=50"
        "&fields%5Bnode--event%5D=title%2Cpath%2Cchanged%2Cdrupal_internal__nid",
    ]
    only = parse_drupal_index(dump(index), origin, path_prefix="/events/")
    assert len(only.children) == 1
    nodes = {
        "data": [
            {"attributes": {"title": "A", "path": {"alias": "/a"}, "changed": "2026-01-01"}},
            {"attributes": {"title": "B", "path": {"alias": None}, "drupal_internal__nid": 7}},
        ],
        "links": {"next": {"href": "https://ex.org/jsonapi/node/article?page=2"}},
    }
    got = parse_drupal_nodes(dump(nodes), origin, site_host="ex.org", max_entries=10)
    assert [e.loc for e in got.entries] == ["https://ex.org/a", "https://ex.org/node/7"]
    assert got.entries[0].lastmod == "2026-01-01"
    assert [c.url for c in got.children] == ["https://ex.org/jsonapi/node/article?page=2"]
    nodes["links"] = {"next": {"href": "https://evil.org/x"}}
    other = parse_drupal_nodes(dump(nodes), origin, site_host="ex.org", max_entries=10)
    assert other.children == []


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'"x"'])
def test_invalid_body(body: bytes) -> None:
    assert parse_wordpress_types(body, ROOT, path_prefix=None).error == "invalid"
    assert parse_drupal_index(body, "https://ex.org", path_prefix=None).error == "invalid"
    nodes = parse_drupal_nodes(body, "https://ex.org", site_host="ex.org", max_entries=5)
    assert nodes.error == "invalid"


@pytest.mark.parametrize(
    ("name", "prefix", "expected"),
    [
        ("veranstaltung", "/veranstaltungen/", True),
        ("events", "/event/", True),
        ("event", "/eventlocations/", True),
        ("kurs", "/veranstaltungen/", False),
        ("post", "/posts/", False),
    ],
)
def test_path_matches(name: str, prefix: str, expected: bool) -> None:
    assert path_matches(name, prefix) is expected


def test_api_type_hint() -> None:
    assert api_type_hint("wordpress_posts", wordpress_list_url(ROOT, "events")) == "event"
    assert api_type_hint("drupal_nodes", "https://ex.org/jsonapi/node/event?x=1") == "event"
    assert api_type_hint("drupal_index", "https://ex.org/jsonapi") is None


# --- plain permalinks: a ``?rest_route=/`` root ----------------------------------------------

PLAIN = "https://ex.org/?rest_route=/"


def test_detect_plain_permalink_root() -> None:
    found = detect('<link rel="https://api.w.org/" href="https://ex.org/?rest_route=/">')
    assert found.wordpress == PLAIN


def test_rest_route_list_and_types_urls() -> None:
    assert wordpress_types_url(PLAIN) == "https://ex.org/?rest_route=/wp/v2/types"
    assert wordpress_types_url(ROOT) == "https://ex.org/wp-json/wp/v2/types"
    assert wordpress_list_url(PLAIN, "posts") == (
        "https://ex.org/?rest_route=/wp/v2/posts&per_page=100&_fields=link,title,modified_gmt"
    )
    assert wordpress_list_url(PLAIN, "posts", 2) == (
        "https://ex.org/?rest_route=/wp/v2/posts&per_page=100&_fields=link,title,modified_gmt"
        "&page=2"
    )
    types = parse_wordpress_types(dump(wp_types()), wordpress_types_url(PLAIN), path_prefix=None)
    assert [c.url for c in types.children] == [
        wordpress_list_url(PLAIN, base) for base in ("posts", "pages", "events")
    ]


def test_rest_route_next_page() -> None:
    first = posts(items(100), url=wordpress_list_url(PLAIN, "posts"), total_pages=3)
    assert [c.url for c in first.children] == [wordpress_list_url(PLAIN, "posts", 2)]
    second = posts(items(100), url=wordpress_list_url(PLAIN, "posts", 2), total_pages=3)
    assert [c.url for c in second.children] == [wordpress_list_url(PLAIN, "posts", 3)]


def test_rest_route_type_hint() -> None:
    assert api_type_hint("wordpress_posts", wordpress_list_url(PLAIN, "events")) == "event"
    assert api_type_hint("wordpress_posts", wordpress_list_url(PLAIN, "events", 2)) == "event"


def test_wordpress_types_use_their_rest_namespace() -> None:
    types = {
        "post": {"slug": "post", "rest_base": "posts", "rest_namespace": "wp/v2"},
        "kurs": {"slug": "kurs", "rest_base": "kurse", "rest_namespace": "lms/v1"},
        "odd": {"slug": "odd", "rest_base": "odds", "rest_namespace": 7},
    }
    parsed = parse_wordpress_types(dump(types), ROOT, path_prefix=None)
    assert [c.url for c in parsed.children] == [
        wordpress_list_url(ROOT, "posts"),
        "https://ex.org/wp-json/lms/v1/kurse?per_page=100&_fields=link,title,modified_gmt",
        wordpress_list_url(ROOT, "odds"),
    ]
    plain = parse_wordpress_types(dump(types), PLAIN, path_prefix=None)
    assert plain.children[1].url == (
        "https://ex.org/?rest_route=/lms/v1/kurse&per_page=100&_fields=link,title,modified_gmt"
    )
    assert api_type_hint("wordpress_posts", plain.children[1].url) == api_type_hint(
        "wordpress_posts", parsed.children[1].url
    )
