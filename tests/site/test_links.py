"""Typed links of a page (ruling P9-S5)."""

import time
from pathlib import Path

import pytest

from text_extraction.convert.links import page_links
from text_extraction.site.links import PageLinks, site_links
from text_extraction.site.types import SiteLink

PAGE = Path(__file__).parent / "pages" / "typed_links.html"
BASE = "https://lernen.example.org/start"
ORIGIN = "https://lernen.example.org"


def fixture_links() -> PageLinks:
    return site_links(PAGE.read_text(encoding="utf-8"), BASE, {}, max_links=1000)


def by_url(links: list[SiteLink], source: str = "a") -> dict[str, SiteLink]:
    return {link.url: link for link in links if link.source == source}


def links_of(body: str) -> list[SiteLink]:
    html = f"<html><head></head><body>{body}</body></html>"
    return site_links(html, BASE, {}, max_links=1000).links


@pytest.mark.parametrize(
    ("url", "region"),
    [
        (f"{ORIGIN}/", "header"),
        (f"{ORIGIN}/suche", "header"),
        (f"{ORIGIN}/mathe/", "nav"),
        (f"{ORIGIN}/physik/", "nav"),
        (f"{ORIGIN}/start", "breadcrumb"),
        (f"{ORIGIN}/mathe/klasse-6/", "breadcrumb"),
        (f"{ORIGIN}/autoren/meier", "main"),  # a header inside an article
        (f"{ORIGIN}/kurs/einheit-1", "main"),
        (f"{ORIGIN}/material/arbeitsblatt.pdf", "main"),
        ("https://forum.example.com/t/42", "main"),
        (f"{ORIGIN}/lizenz", "main"),  # a footer inside an article
        (f"{ORIGIN}/kurs/bruchrechnung?seite=2", "pagination"),
        (f"{ORIGIN}/mathe/dezimalzahlen", "aside"),
        (f"{ORIGIN}/impressum", "footer"),
        ("https://partner.example.com/", "footer"),
    ],
)
def test_regions_per_link(url: str, region: str) -> None:
    assert by_url(fixture_links().links)[url].region == region


@pytest.mark.parametrize(
    ("body", "region"),
    [
        ('<div role="navigation"><a href="/x">x</a></div>', "nav"),
        ('<div role="banner"><a href="/x">x</a></div>', "header"),
        ('<div role="contentinfo"><a href="/x">x</a></div>', "footer"),
        ('<div role="complementary"><a href="/x">x</a></div>', "aside"),
        ('<div role="main"><a href="/x">x</a></div>', "main"),
        ("<main><header><a href='/x'>x</a></header></main>", "main"),
        ("<main><footer><a href='/x'>x</a></footer></main>", "main"),
        ('<div class="Site-Breadcrumbs"><a href="/x">x</a></div>', "breadcrumb"),
        ('<nav aria-label="Breadcrumb"><a href="/x">x</a></nav>', "breadcrumb"),
        ('<div id="breadCrumb"><a href="/x">x</a></div>', "breadcrumb"),
        ('<nav class="pager"><a href="/x">x</a></nav>', "pagination"),
        ('<div id="list-pagination"><a href="/x">x</a></div>', "pagination"),
        ('<main><a href="/x" rel="next">weiter</a></main>', "pagination"),
        ('<nav><div class="breadcrumb"><a href="/x">x</a></div></nav>', "breadcrumb"),
        ('<div class="breadcrumb"><nav><a href="/x">x</a></nav></div>', "nav"),  # nearest wins
        ('<div><a href="/x">x</a></div>', None),
    ],
)
def test_region_rules(body: str, region: str | None) -> None:
    (link,) = links_of(body)
    assert link.region == region


def test_sources_and_head_rels() -> None:
    links = fixture_links().links
    heads = by_url(links, "link")

    assert heads[f"{ORIGIN}/kurs/bruchrechnung"].rel == ["canonical"]
    alternate = heads[f"{ORIGIN}/en/fractions"]
    assert (alternate.rel, alternate.hreflang) == (["alternate"], "en")
    assert heads[f"{ORIGIN}/fr/fractions"].hreflang == "fr"
    assert heads[f"{ORIGIN}/kurs/bruchrechnung?seite=2"].region == "pagination"
    assert set(heads) == {
        f"{ORIGIN}/kurs/bruchrechnung",
        f"{ORIGIN}/en/fractions",
        f"{ORIGIN}/fr/fractions",
        f"{ORIGIN}/kurs/bruchrechnung?seite=2",
    }  # stylesheet, alternate stylesheet and icon skipped
    (iframe,) = (link for link in links if link.source == "iframe")
    assert (iframe.url, iframe.kind, iframe.text) == (
        "https://h5p.example.net/h5p/embed/1234",
        "external",
        "Übung",
    )
    (area,) = (link for link in links if link.source == "area")
    assert (area.url, area.text, area.region) == (f"{ORIGIN}/orte/berlin", "Berlin", "main")
    assert all(link.crawl == "listed" and link.target is None for link in links)


def test_document_order_and_first_occurrence_per_source() -> None:
    links = fixture_links().links
    urls = [(link.source, link.url) for link in links]

    assert urls[0] == ("link", f"{ORIGIN}/kurs/bruchrechnung")
    assert urls.index(("a", f"{ORIGIN}/")) < urls.index(("a", f"{ORIGIN}/mathe/"))
    assert len(urls) == len(set(urls))
    # Both /kurs/bruchrechnung?seite=2 links: one per source.
    assert ("link", f"{ORIGIN}/kurs/bruchrechnung?seite=2") in urls
    assert ("a", f"{ORIGIN}/kurs/bruchrechnung?seite=2") in urls
    # /mathe/ again at the end: the nav link (the first) wins.
    assert by_url(links)[f"{ORIGIN}/mathe/"].text == "Mathe"


def test_kind_matches_page_links() -> None:
    html = PAGE.read_text(encoding="utf-8")
    ours = {(link.url, link.kind) for link in fixture_links().links if link.source == "a"}

    assert ours == {(link.url, link.kind) for link in page_links(html, BASE)}


def test_fragments_and_other_schemes() -> None:
    links = by_url(fixture_links().links)

    assert links[f"{ORIGIN}/kurs/einheit-1"].text == "Einheit 1"  # the first; #aufgaben dropped
    assert not any(url.startswith(("mailto:", "javascript:")) or "#" in url for url in links)
    assert links[f"{ORIGIN}/material/arbeitsblatt.pdf"].kind == "file"
    assert links["https://www.lernen.example.org/glossar"].kind == "internal"
    assert links["https://forum.example.com/t/42"].kind == "external"


def test_rel_tokens_lowercase_max_8() -> None:
    (link,) = links_of('<a href="/x" rel=" NoFollow  UGC nofollow a b c d e f g h">x</a>')
    assert link.rel == ["nofollow", "ugc", "a", "b", "c", "d", "e", "f"]

    forum = by_url(fixture_links().links)["https://forum.example.com/t/42"]
    assert forum.rel == ["nofollow", "ugc"]
    assert by_url(fixture_links().links)[f"{ORIGIN}/impressum"].rel == []


def test_rel_and_hreflang_are_bounded() -> None:
    long = "x" * 65
    (link,) = links_of(f'<a href="/x" rel="{long} next" hreflang="{long}">x</a>')
    assert (link.rel, link.hreflang) == (["next"], None)
    (link,) = links_of('<a href="/x" hreflang=" de-AT ">x</a>')
    assert link.hreflang == "de-AT"


@pytest.mark.parametrize(
    ("head", "headers", "nofollow"),
    [
        ('<meta name="robots" content="noindex, nofollow">', {}, True),
        ('<meta name="ROBOTS" content="NONE">', {}, True),
        ('<meta name="text-extraction" content="nofollow">', {}, True),
        ('<meta name="googlebot" content="nofollow">', {}, False),
        ('<meta name="robots" content="noindex">', {}, False),
        ("", {"x-robots-tag": "text-extraction: none"}, True),
        ("", {"X-Robots-Tag": "noindex, nofollow"}, True),
        ("", {"x-robots-tag": "otherbot: nofollow"}, False),
        ("", {"x-robots-tag": "otherbot: noindex, nofollow"}, False),
        ("", {"x-robots-tag": "otherbot: noindex, text-extraction: nofollow"}, True),
        ("", {"x-robots-tag": "unavailable_after: 2030-01-01, nofollow"}, True),
        ("", {}, False),
    ],
)
def test_meta_and_header_nofollow(head: str, headers: dict[str, str], nofollow: bool) -> None:
    html = f"<html><head>{head}</head><body><a href='/x'>x</a></body></html>"
    assert site_links(html, BASE, headers, max_links=10).nofollow is nofollow


def test_link_caps_and_cuts() -> None:
    """Review Focus 5: a huge page stays bounded and fast."""
    many = "".join(f'<a href="/p{i}">Seite {i}</a>' for i in range(50_000))
    huge_text = "Lernen " * 150_000  # 1 MB
    long_href = "/" + "a" * 10_000
    html = (
        f'<html><body><a href="{long_href}">lang</a><a href="/big">{huge_text}</a>'
        f"{many}</body></html>"
    )

    started = time.perf_counter()
    result = site_links(html, BASE, {}, max_links=1000)
    elapsed = time.perf_counter() - started

    assert len(result.links) == 1000
    assert result.truncated is True
    assert result.links[0].url == f"{ORIGIN}/big"  # the 10,000-character href is dropped
    assert len(result.links[0].text) == 200
    assert result.links[0].text == ("Lernen " * 30)[:200]  # cut inside a word
    assert result.links[-1].url == f"{ORIGIN}/p998"
    assert elapsed < 2.0


def test_exactly_max_links_is_not_truncated() -> None:
    html = "".join(f'<a href="/p{i}">{i}</a>' for i in range(10))
    result = site_links(html, BASE, {}, max_links=10)
    assert (len(result.links), result.truncated) == (10, False)


def test_base_href_and_icon_label() -> None:
    links = by_url(fixture_links().links)

    assert f"{ORIGIN}/kurs/einheit-1" in links  # relative to <base href="/kurs/">
    assert links[f"{ORIGIN}/suche"].text == "Suche"  # an icon link: its aria-label
    assert links[f"{ORIGIN}/autoren/meier"].text == "Frau Meier"  # whitespace collapsed
    (titled,) = links_of('<a href="/x" title="  Mehr  dazu "><img src="i.png"></a>')
    assert titled.text == "Mehr dazu"


def test_not_html_and_empty() -> None:
    assert site_links("", BASE, {}, max_links=10) == PageLinks(
        links=[], nofollow=False, truncated=False
    )
    assert site_links("just text, no links", BASE, {}, max_links=10).links == []
