"""Collapsed solution panels are part of the page: they are revealed before conversion."""

from bs4 import BeautifulSoup

from text_extraction.convert.html import convert_html
from text_extraction.convert.panels import reveal_solution_panels

SERLO_LIKE = """<html><body><main>
<p>Ein Werkstück hat die Masse m=657 g. Welche Dichte hat das Material?</p>
<nav><button><span>▾&nbsp;</span>Lösungsvorschlag</button></nav>
<div class="serlo-solution-box hidden"><div class="hidden">
<p>Mit diesem Ergebnis kann nun die Dichte bestimmt werden.</p></div></div>
</main></body></html>"""


def test_serlo_like_solution_survives_conversion() -> None:
    out = convert_html(SERLO_LIKE, "https://de.serlo.org/mathe/1/aufgabe", "markdown").content
    assert "Mit diesem Ergebnis kann nun die Dichte bestimmt werden." in out
    assert "Lösungsvorschlag" in out  # the label is kept as a heading before the panel


def test_details_solution_is_kept() -> None:
    html = "<main><p>Aufgabe.</p><details><summary>Lösung</summary><p>Zwölf.</p></details></main>"
    assert "Zwölf." in convert_html(html, "https://x.test/", "text").content


def test_non_solution_control_never_reveals_its_panel() -> None:
    html = (
        '<main><p>Text.</p><div><button aria-controls="m">Menü</button></div>'
        '<ul id="m" hidden><li>Impressum</li></ul></main>'
    )
    assert "Impressum" not in convert_html(html, "https://x.test/", "text").content


def test_responsive_hidden_class_content_is_kept() -> None:
    html = (
        '<main><p>Intro.</p><div class="hidden md:block"><p>Desktop sidebar text.</p></div></main>'
    )
    assert "Desktop sidebar text." in convert_html(html, "https://x.test/", "text").content


def test_footer_link_does_not_reveal_a_cookie_banner() -> None:
    html = (
        '<main><p>Text.</p><footer><a href="/hilfe">Hinweise</a>'
        "<div hidden><p>Cookie text.</p></div></footer></main>"
    )
    assert "Cookie text." not in convert_html(html, "https://x.test/", "text").content


def test_two_controls_for_one_panel_reveal_it_once() -> None:
    html = (
        '<main><p>Q?</p><button aria-controls="s">Lösung</button>'
        '<button aria-controls="s">Lösung</button><div id="s" hidden><p>Zwölf.</p></div></main>'
    )
    soup = BeautifulSoup(html, "lxml")
    assert reveal_solution_panels(soup) == 1
    assert len(soup.find_all("strong")) == 1


def test_aria_controls_solution_panel_is_revealed() -> None:
    html = (
        '<main><p>Frage?</p><button aria-controls="s1">Show solution</button>'
        '<div id="s1" hidden><p>Forty-two.</p></div></main>'
    )
    assert "Forty-two." in convert_html(html, "https://x.test/", "text").content


def test_reveal_counts_panels_and_clears_hiding() -> None:
    soup = BeautifulSoup(SERLO_LIKE, "lxml")
    assert reveal_solution_panels(soup) == 1
    box = soup.select_one(".serlo-solution-box")
    assert box is not None
    assert "hidden" not in str(box.get("class", ""))
    assert soup.select_one("div.hidden") is None


def test_inline_style_panel_is_revealed() -> None:
    html = (
        '<main><p>Q?</p><a role="button" aria-controls="a">Antwort</a>'
        '<div id="a" style="color:red; display:none"><p>Blau.</p></div></main>'
    )
    out = convert_html(html, "https://x.test/", "text").content
    assert "Blau." in out
