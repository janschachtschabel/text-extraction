import time

from text_extraction.convert.listings import LinkSection, listing_lists, render_link_sections

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


def test_known_urls_and_headings_are_not_repeated() -> None:
    existing = (
        "[Sehvorgang](https://example.org/optik/lichtausbreitung/sehvorgang)\n\n### Lichtreflexion"
    )

    out = listing_lists(HUB, BASE, "markdown", existing=existing)

    assert "Sehvorgang" not in out
    assert "### Lichtreflexion" not in out
    assert "Spiegel" in out
    assert "Schatten" in out


def test_texts_the_content_holds_as_lines_are_not_repeated() -> None:
    existing = "Optik\n\nLichtreflexion\n- Sehvorgang\n  Spiegel"

    out = listing_lists(HUB, BASE, "markdown", existing=existing)

    assert "Sehvorgang" not in out
    assert "Spiegel" not in out
    assert "Lichtreflexion" not in out
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
