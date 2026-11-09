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
