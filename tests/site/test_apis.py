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
