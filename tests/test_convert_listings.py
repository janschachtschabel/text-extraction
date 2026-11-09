import re
import time

import pytest

from text_extraction.convert.listings import (
    LinkSection,
    OutputFormat,
    listing_lists,
    render_link_sections,
)

BASE = "https://example.org/optik"


def _items(section: str, names: list[str]) -> str:
    return "".join(
        f'<div class="views-row pb-1"><a href="/optik/{section}/{name.lower()}">{name}</a></div>'
        for name in names
    )


def _more(section: str) -> str:
    return (
        f'<div><a class="read-more-link" href="/optik/{section}">Zum Themenbereich</a></div>'
        f'<div><a class="read-more-link" href="/optik/{section}">Zum Themenbereich</a></div>'
    )


HUB = f"""<html><body>
<header><a href="/login">Anmelden</a></header>
<nav><a href="/n1">NavEins</a><a href="/n2">NavZwei</a><a href="/n3">NavDrei</a>
<a href="/n4">NavVier</a><a href="/n5">NavFuenf</a></nav>
<div class="branch_header"><h1>Optik</h1>
<p>Eine Einfuehrung in die Optik, die mit einem <a href="/inline">Verweis im Text</a> endet
und lange genug ist, um keine Linkliste zu sein.</p></div>
<div class="branch_issues">
<h2 class="mb-3"><h3 id="degree-1">Sekundarstufe 1</h3></h2>
<h3 class="mb-0"><h3>Lichtausbreitung</h3></h3>
{_items("lichtausbreitung", ["Sehvorgang", "Schatten", "Lochkamera", "Lichtstrahl"])}
{_more("lichtausbreitung")}
<h3 class="mb-0"><h3>Lichtreflexion</h3></h3>
{_items("lichtreflexion", ["Spiegel", "Reflexionsgesetz", "Streuung"])}
{_more("lichtreflexion")}
<a href="#degree-1">Sekundarstufe 1</a>
</div>
<div class="region-pre-footer"><div><a href="/p1">Projekt Eins</a></div>
<div><a href="/p2">Projekt Zwei</a></div><div><a href="/p3">Projekt Drei</a></div></div>
<div id="feedback-form"><div><a href="/feedback">Feedback</a></div></div>
</body></html>"""


def test_hub_sections_become_headed_lists() -> None:
    out = listing_lists(HUB, BASE, "markdown", existing="# Optik\n\nIntro")

    assert "## Sekundarstufe 1" in out
    assert "### Lichtausbreitung" in out
    assert "- [Sehvorgang](https://example.org/optik/lichtausbreitung/sehvorgang)" in out
    assert out.index("Lichtausbreitung") < out.index("Lichtreflexion")
    assert out.count("(https://example.org/optik/lichtausbreitung)") == 1


def test_chrome_and_inline_links_are_left_out() -> None:
    out = listing_lists(HUB, BASE, "markdown", existing="")

    for left_out in ("Anmelden", "Nav", "Verweis im Text", "Projekt", "Feedback", "#degree-1"):
        assert left_out not in out


def test_known_urls_are_not_repeated_and_known_headings_keep_their_section() -> None:
    existing = (
        "[Sehvorgang](https://example.org/optik/lichtausbreitung/sehvorgang)\n\n### Lichtreflexion"
    )

    out = listing_lists(HUB, BASE, "markdown", existing=existing)

    assert "Sehvorgang" not in out
    assert "### Lichtreflexion\n\n- [Spiegel](" in out
    assert "Schatten" in out


def test_link_texts_the_content_holds_as_lines_are_not_repeated() -> None:
    existing = "Optik\n\nLichtreflexion\n- Sehvorgang\n  Spiegel"

    out = listing_lists(HUB, BASE, "markdown", existing=existing)

    assert "Sehvorgang" not in out
    assert "Spiegel" not in out
    assert "### Lichtreflexion\n\n- [Reflexionsgesetz](" in out
    assert "[Schatten](" in out
    assert "### Lichtausbreitung" in out


def test_too_few_links_give_nothing() -> None:
    page = "<h1>T</h1>" + "".join(f'<p><a href="/a{i}">Link {i}</a></p>' for i in range(4))

    assert listing_lists(page, BASE, "markdown", existing="") == ""


def test_long_items_are_not_standalone() -> None:
    long_items = "".join(f'<li><a href="/l{i}">Link {i}</a> {"x" * 200}</li>' for i in range(6))
    short_items = "".join(f'<li><a href="/s{i}">Kurz {i}</a></li>' for i in range(5))

    out = listing_lists(f"<ul>{long_items}{short_items}</ul>", BASE, "markdown", existing="")

    assert "Link 0" not in out
    assert "Kurz 4" in out


def test_render_link_sections_in_each_format() -> None:
    sections = [LinkSection("Erklärungen", 2, (("A & B", "https://x.org/a?b=1&c=2"),))]

    assert render_link_sections(sections, "markdown") == (
        "## Erklärungen\n\n- [A & B](https://x.org/a?b=1&c=2)"
    )
    assert render_link_sections(sections, "text") == "Erklärungen\n\n- A & B"
    assert render_link_sections(sections, "html") == (
        '<h2>Erklärungen</h2><ul><li><a href="https://x.org/a?b=1&amp;c=2">A &amp; B</a></li></ul>'
    )
    headless = [LinkSection(None, 1, (("A", "https://x.org/a"),))]
    assert render_link_sections(headless, "markdown") == "- [A](https://x.org/a)"
    assert render_link_sections([], "markdown") == ""


def _topics(count: int = 6) -> str:
    return "".join(f'<li><a href="/t/{n}">Topic {n}</a></li>' for n in range(count))


def test_many_links_in_one_block_take_linear_time() -> None:
    links = "".join(f'<a href="/p/{n}">Page {n}</a><br>' for n in range(5_000))
    page = f"<html><body><h1>Hub</h1><div>{links}</div></body></html>"

    started = time.perf_counter()
    listing_lists(page, BASE, "markdown", existing="")

    assert time.perf_counter() - started < 5


def test_a_wrapper_with_the_h1_is_no_chrome_by_its_name() -> None:
    page = (
        '<html><body><div id="page" class="layout has-sidebar">'
        f"<h1>Hub</h1><ul>{_topics()}</ul></div></body></html>"
    )

    out = listing_lists(page, BASE, "markdown", existing="")

    assert "- [Topic 0](https://example.org/t/0)" in out
    assert "- [Topic 5](https://example.org/t/5)" in out


def test_a_landmark_wrapping_the_h1_still_is_chrome() -> None:
    page = f"<html><body><aside><h1>Hub</h1><ul>{_topics()}</ul></aside></body></html>"

    assert listing_lists(page, BASE, "markdown", existing="") == ""


def test_a_landmark_after_the_h1_is_left_out() -> None:
    nav = "".join(f'<li><a href="/nav/{n}">Nav {n}</a></li>' for n in range(6))
    footer = "".join(f'<div><a href="/foot/{n}">Foot {n}</a></div>' for n in range(6))
    page = (
        f"<html><body><h1>Hub</h1><nav><ul>{nav}</ul></nav><ul>{_topics()}</ul>"
        f"<footer>{footer}</footer></body></html>"
    )

    out = listing_lists(page, BASE, "markdown", existing="")

    assert "Topic 0" in out
    assert "Nav" not in out
    assert "Foot" not in out


def test_breadcrumbs_are_chrome() -> None:
    page = f'<html><body><h1>Hub</h1><ul class="breadcrumbs">{_topics()}</ul></body></html>'

    assert listing_lists(page, BASE, "markdown", existing="") == ""


def test_a_url_of_the_content_is_matched_whole() -> None:
    page = f"<html><body><h1>Hub</h1><ul>{_topics()}</ul></body></html>"

    out = listing_lists(
        page, BASE, "markdown", existing="See [deeper](https://example.org/t/1/deeper)."
    )

    assert "- [Topic 1](https://example.org/t/1)" in out


def test_a_url_with_an_ampersand_in_html_content_is_known() -> None:
    items = "".join(f'<li><a href="/q?a={n}&amp;b=1">Query {n}</a></li>' for n in range(6))
    page = f"<html><body><h1>Hub</h1><ul>{items}</ul></body></html>"
    existing = '<html><body><p><a href="https://example.org/q?a=0&amp;b=1">x</a></p></body></html>'

    out = listing_lists(page, BASE, "html", existing=existing)

    assert "Query 0" not in out
    assert "Query 1" in out


def test_a_link_wrapped_in_a_span_inside_running_text_is_not_standalone() -> None:
    prose = (
        "<p>Long prose about physics " + "word " * 40 + '<span class="x"><a href="/inline">'
        "inline link</a></span> and more prose " + "word " * 20 + "</p>"
    )
    page = f"<html><body><h1>Hub</h1>{prose}<ul>{_topics()}</ul></body></html>"

    out = listing_lists(page, BASE, "text", existing="")

    assert "Topic 0" in out
    assert "inline link" not in out


def test_markdown_link_text_and_url_are_escaped() -> None:
    items = "".join(f'<li><a href="/w/{n}_(x) y">A [b] {n}</a></li>' for n in range(6))
    page = f"<html><body><h1>Hub</h1><ul>{items}</ul></body></html>"

    out = listing_lists(page, BASE, "markdown", existing="")

    assert out.splitlines()[0] == r"- [A \[b\] 0](https://example.org/w/0_%28x%29%20y)"


def test_a_card_heading_inside_its_link_stays_the_link_label() -> None:
    cards = "".join(
        f'<div class="col"><a class="card" href="/t/{n}"><h3>Thema {n}</h3>'
        f"<p>Kurzer Teaser {n}.</p></a></div>"
        for n in range(6)
    )
    page = f'<html><body><main><h1>Themen</h1><div class="grid">{cards}</div></main></body></html>'

    out = listing_lists(page, BASE, "markdown", existing="# Themen")

    assert "###" not in out
    for n in range(6):
        assert f"- [Thema {n} Kurzer Teaser {n}.](https://example.org/t/{n})" in out


def test_a_parent_link_of_a_nested_list_is_listed_with_its_children() -> None:
    def children(topic: str) -> str:
        return "".join(f'<li><a href="/{topic}/{n}">{topic} {n}</a></li>' for n in range(3))

    page = (
        "<html><body><main><h1>Physik</h1><ul>"
        f'<li><a href="/mechanik">Mechanik</a><ul>{children("mech")}</ul></li>'
        f'<li><a href="/akustik">Akustik</a><ul>{children("aku")}</ul></li>'
        "</ul></main></body></html>"
    )

    out = listing_lists(page, BASE, "markdown", existing="# Physik")

    assert "- [Mechanik](https://example.org/mechanik)" in out
    assert "- [Akustik](https://example.org/akustik)" in out
    assert "- [mech 2](https://example.org/mech/2)" in out
    assert "- [aku 0](https://example.org/aku/0)" in out
    assert out.index("Mechanik") < out.index("mech 0") < out.index("Akustik")


def test_a_heading_the_content_holds_keeps_its_section() -> None:
    def links(prefix: str) -> str:
        return "".join(f'<li><a href="/{prefix}/{n}">{prefix}{n}</a></li>' for n in range(3))

    page = (
        f"<html><body><h1>Optik</h1><h2>A</h2><ul>{links('A')}</ul>"
        f"<h2>B</h2><ul>{links('B')}</ul></body></html>"
    )

    out = listing_lists(page, BASE, "markdown", existing="# Optik\n\n## B\n\nIntro")

    assert out == (
        "## A\n\n- [A0](https://example.org/A/0)\n- [A1](https://example.org/A/1)\n"
        "- [A2](https://example.org/A/2)\n\n"
        "## B\n\n- [B0](https://example.org/B/0)\n- [B1](https://example.org/B/1)\n"
        "- [B2](https://example.org/B/2)"
    )


def _structure(out: str, fmt: str) -> list[tuple[str, str]]:
    """The headings and link labels of a rendered result, in order."""
    if fmt == "html":
        pattern = r"<h\d>([^<]+)</h\d>|<a href=\"[^\"]+\">([^<]+)</a>"
    else:
        pattern = r"^#+ (.+)$|^- \[(.+)\]\(.+\)$"
    return [
        ("heading", match[0]) if match[0] else ("link", match[1])
        for match in re.findall(pattern, out, flags=re.MULTILINE)
    ]


def test_markdown_and_html_give_the_same_sections() -> None:
    items = "".join(f'<li><a href="/b/{n}">Bio {n}</a></li>' for n in range(8))
    page = f"<html><body><main><h1>Physik</h1><h2>Biology</h2><ul>{items}</ul></main></body></html>"
    markdown = "# Physik\n\n## Biology\n\nBio 0\n\n[Bio & 1](https://example.org/b/1)\n\nIntro"
    html = (
        "<html><body><h1>Physik</h1>\n<h2>Biology</h2><p>Bio 0</p>"
        '<p><a href="https://example.org/b/1">Bio &amp; 1</a></p><p>Intro</p></body></html>'
    )

    from_markdown = listing_lists(page, BASE, "markdown", existing=markdown)
    from_html = listing_lists(page, BASE, "html", existing=html)

    expected = [("heading", "Biology")] + [("link", f"Bio {n}") for n in range(2, 8)]
    assert _structure(from_markdown, "markdown") == expected
    assert _structure(from_html, "html") == expected


def test_an_offcanvas_menu_after_the_h1_is_chrome() -> None:
    menu = "".join(
        f'<li class="nav-item"><a href="/m/{n}">Menu item {n}</a></li>' for n in range(6)
    )
    page = (
        f"<html><body><main><h1>Hub</h1><ul>{_topics()}</ul></main>"
        '<div class="offcanvas offcanvas-end" id="offcanvasNavbar">'
        f'<div class="offcanvas-body"><ul class="navbar-nav">{menu}</ul></div></div>'
        "</body></html>"
    )

    out = listing_lists(page, BASE, "markdown", existing="")

    assert "Topic 0" in out
    assert "Menu item" not in out


@pytest.mark.parametrize("word", ["nav", "navbar", "navigation", "offcanvas"])
def test_navigation_words_are_chrome(word: str) -> None:
    page = (
        f'<html><body><h1>Hub</h1><div class="site-{word}"><ul>{_topics()}</ul></div></body></html>'
    )

    assert listing_lists(page, BASE, "markdown", existing="") == ""


@pytest.mark.parametrize(
    ("page_url", "href"),
    [
        ("https://example.org/themen/", "/themen"),
        ("https://example.org/themen", "/themen/"),
        ("https://example.org/themen/#top", "https://example.org/themen#list"),
    ],
)
def test_a_link_to_the_page_itself_is_left_out_whatever_its_trailing_slash(
    page_url: str, href: str
) -> None:
    page = (
        f'<html><body><h1>T</h1><ul><li><a href="{href}">Themen</a></li>'
        '<li><a href="/themen/?page=2">Weiter</a></li>'
        f"{_topics()}</ul></body></html>"
    )

    out = listing_lists(page, page_url, "markdown", existing="")

    assert "[Themen]" not in out
    assert "- [Weiter](https://example.org/themen/?page=2)" in out


WIKI = "https://de.wikipedia.org/wiki/Licht_(Physik)"


@pytest.mark.parametrize(
    ("fmt", "existing", "listed"),
    [
        ("markdown", f"See [Licht (Physik)](<{WIKI}>) for more.", WIKI),
        ("markdown", f"See [Licht (Physik)]({WIKI}) for more.", WIKI),
        ("markdown", "See [Licht](https://de.wikipedia.org/wiki/Licht_%28Physik%29).", WIKI),
        (
            "markdown",
            "See [Licht](<https://de.wikipedia.org/wiki/Licht (Physik)>).",
            WIKI.replace("_", " "),
        ),
        (
            "html",
            f'<html><body><p>See <a href="{WIKI}">Licht (Physik)</a>.</p></body></html>',
            WIKI,
        ),
        (
            "html",
            '<p><a href="https://de.wikipedia.org/wiki/Licht_%28Physik%29">Licht</a></p>',
            WIKI,
        ),
        (
            "html",
            "<p><a href='https://de.wikipedia.org/wiki/Licht_(Physik'>Licht</a></p>",
            "https://de.wikipedia.org/wiki/Licht_(Physik",
        ),
    ],
)
def test_a_url_with_parentheses_in_the_content_is_known(
    fmt: OutputFormat, existing: str, listed: str
) -> None:
    items = f'{_topics()}<li><a href="{listed}">Wiki Licht</a></li>'
    page = f"<html><body><h1>Hub</h1><ul>{items}</ul></body></html>"

    out = listing_lists(page, BASE, fmt, existing=existing)

    assert "Topic 0" in out
    assert "Wiki Licht" not in out


def test_a_tag_list_of_span_wrapped_links_is_listed() -> None:
    spans = "".join(f'<span class="tag"><a href="/t/{n}">Tag {n}</a></span> ' for n in range(8))
    page = f"<html><body><h1>Hub</h1><div class='tags'>{spans}</div></body></html>"

    out = listing_lists(page, BASE, "markdown", existing="")

    assert "- [Tag 0](https://example.org/t/0)" in out
    assert "- [Tag 7](https://example.org/t/7)" in out


def test_span_wrapped_links_inside_running_text_are_not_listed() -> None:
    links = " und ".join(f'<span><a href="/s/{n}">Begriff {n}</a></span>' for n in range(6))
    prose = f"<p>Die Optik erklaert viele Dinge, etwa {links}, und noch {'mehr ' * 30}.</p>"
    page = f"<html><body><h1>Hub</h1>{prose}<ul>{_topics()}</ul></body></html>"

    out = listing_lists(page, BASE, "markdown", existing="")

    assert "Topic 0" in out
    assert "Begriff" not in out


def test_render_link_sections_in_text_with_urls() -> None:
    sections = [LinkSection("Ideen", 2, (("Blatt", "https://x.org/b.pdf"),))]

    assert render_link_sections(sections, "text", text_urls=True) == (
        "Ideen\n\n- Blatt: https://x.org/b.pdf"
    )
