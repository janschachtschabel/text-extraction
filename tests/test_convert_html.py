import time
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from bs4 import BeautifulSoup

from tests.scaling import GROWTH_LIMIT, growth
from text_extraction.convert import OutputFormat, convert_html, convert_plain, page_links
from text_extraction.convert import html as html_module

URL = "https://www.example.org/lessons/fractions"

LONG = (
    "Fractions describe parts of a whole. A fraction has a numerator above the bar and a "
    "denominator below it, and the denominator says into how many equal parts the whole is "
    "divided. This paragraph is long enough for the extractor to treat it as real content. "
)

ARTICLE = f"""<html lang="en"><head><title>Fractions | Example School</title></head><body>
<nav><ul>
<li><a href="/home">Navigation Home Link</a></li>
<li><a href="/shop">Shop</a></li>
</ul></nav>
<header><div class="logo">Example School Logo Banner</div></header>
<main><article>
<h1>Fractions explained</h1>
<p>{LONG}</p>
<h2>Overview table</h2>
<table>
<tr><th>Fraction</th><th>Decimal</th></tr>
<tr><td>1/2</td><td>0.5</td></tr>
<tr><td>1/4</td><td>0.25</td></tr>
</table>
<p>{LONG} See the <a href="/lessons/decimals">lesson on decimals</a> next.</p>
<div style="display: none">Hidden by style marker</div>
<div hidden>Hidden by attribute marker</div>
<p aria-hidden="true">Hidden by aria marker</p>
<script>var trackingMarker = 1;</script>
</article></main>
<aside class="sidebar">Sidebar advert marker</aside>
<footer>Footer copyright marker</footer>
</body></html>"""


def test_markdown_keeps_heading_and_tables() -> None:
    result = convert_html(ARTICLE, URL, "markdown")

    assert result.converter == "trafilatura"
    assert result.content.startswith("# Fractions explained")
    assert "| Fraction | Decimal |" in result.content
    assert "| --- | --- |" in result.content.replace("|---|---|", "| --- | --- |")
    assert "| 1/4 | 0.25 |" in result.content
    assert result.title == "Fractions explained"
    assert result.warnings == []


def test_text_and_html_formats() -> None:
    text = convert_html(ARTICLE, URL, "text")
    markup = convert_html(ARTICLE, URL, "html")

    assert text.converter == markup.converter == "trafilatura"
    assert "Fractions explained" in text.content
    assert "Fractions describe parts of a whole" in text.content
    assert "<" not in text.content
    assert "](" not in text.content  # no Markdown link syntax in plain text
    assert "<h1>Fractions explained</h1>" in markup.content
    assert "<table>" in markup.content
    assert '<a href="https://www.example.org/lessons/decimals">' in markup.content


def test_hidden_and_nav_removed() -> None:
    formats: tuple[OutputFormat, ...] = ("markdown", "text", "html")
    for fmt in formats:
        content = convert_html(ARTICLE, URL, fmt).content

        assert "Fractions describe parts of a whole" in content
        for marker in (
            "Navigation Home Link",
            "Hidden by style marker",
            "Hidden by attribute marker",
            "Hidden by aria marker",
            "trackingMarker",
            "Footer copyright marker",
            "Sidebar advert marker",
        ):
            assert marker not in content, (fmt, marker)


def test_relative_links_made_absolute() -> None:
    page = f"""<html><head><base href="/lessons/"></head><body><article><h1>Links</h1>
    <p>{LONG} Read <a href="decimals">the decimals lesson</a>, <a href="../index.html">the index</a>
    and <a href="http://[broken">a broken link</a>.</p></article></body></html>"""

    content = convert_html(page, "https://www.example.org/x/y", "markdown").content

    assert "[the decimals lesson](https://www.example.org/lessons/decimals)" in content
    assert "[the index](https://www.example.org/index.html)" in content
    assert "a broken link" in content
    assert "[broken" not in content


def test_without_a_page_url_relative_links_stay_relative() -> None:
    """An upload has no URL: its relative links are kept as they are (ruling F17), the other
    rules still hold."""
    page = f"""<html><body><article><h1>Links</h1><p>{LONG} Read
    <a href="../lessons/decimals">the decimals lesson</a>,
    <a href="javascript:alert(1)">a script</a> and
    <a href="https://www.example.org/next">the next one</a>.</p></article></body></html>"""

    content = convert_html(page, "", "markdown").content

    assert "[the decimals lesson](../lessons/decimals)" in content
    assert "[the next one](https://www.example.org/next)" in content
    assert "javascript:" not in content
    assert "a script" in content


def test_mathml_to_latex() -> None:
    page = f"""<html><body><article><h1>Vectors</h1><p>{LONG}</p>
    <p>The length is
    <math><msqrt><msup><mi>a</mi><mn>2</mn></msup><mo>+</mo><msup><mi>b</mi><mn>2</mn></msup></msqrt></math>
    and the slope is <math><mfrac><mi>y</mi><mi>x</mi></mfrac></math>.</p>
    <p>Annotated: <math><mi>z</mi>
    <annotation encoding="application/x-tex">z_0 = \\alpha</annotation></math></p>
    <p>Hidden twin: <span style="display:none"><math><mi>q</mi></math></span></p>
    </article></body></html>"""

    content = convert_html(page, URL, "markdown").content

    assert r"$$\sqrt{a^{2} + b^{2}}$$" in content
    assert r"$$\frac{y}{x}$$" in content
    assert r"$$z_0 = \alpha$$" in content
    assert "$$q$$" in content  # the wrapper existed only to hide the formula; it is revealed


def test_data_tex_attributes_become_equations() -> None:
    page = f"""<html><body><article><h1>T</h1><p>{LONG}</p>
    <p>Formula <span data-tex="E = mc^2">E = mc2 rendered</span> here.</p>
    </article></body></html>"""

    content = convert_html(page, URL, "markdown").content

    assert "$$E = mc^2$$" in content
    assert "rendered" not in content


def test_linked_documents_kept() -> None:
    page = """<html><head><title>Worksheets</title></head><body>
    <nav><a href="/downloads/site-map.pdf">Site map</a></nav>
    <main><h1>Worksheets for grade 5</h1>
    <ul>
    <li><a href="/files/fractions.pdf">Fractions</a></li>
    <li><a href="/files/decimals%20basic.pdf">Decimals (basic)</a></li>
    <li><a href="/files/answers.docx" aria-label="Answer sheet"></a></li>
    </ul></main>
    <footer><a href="/files/privacy.pdf">Privacy</a></footer></body></html>"""

    markdown = convert_html(page, URL, "markdown")
    text = convert_html(page, URL, "text")
    markup = convert_html(page, URL, "html")

    for result in (markdown, text, markup):
        assert "Worksheets for grade 5" in result.content
        assert "https://www.example.org/files/fractions.pdf" in result.content
        assert "https://www.example.org/files/answers.docx" in result.content
        assert "site-map.pdf" not in result.content  # navigation belongs to the site
        assert "Site map" not in result.content
        assert "privacy.pdf" not in result.content
    assert "- [Fractions](https://www.example.org/files/fractions.pdf)" in markdown.content
    assert "- [Decimals (basic)](https://www.example.org/files/decimals%20basic.pdf)" in (
        markdown.content
    )
    assert "decimals%20basic.pdf" in markdown.content
    assert "[Answer sheet](https://www.example.org/files/answers.docx)" in markdown.content
    assert "Fractions: https://www.example.org/files/fractions.pdf" in text.content
    assert '<a href="https://www.example.org/files/fractions.pdf">Fractions</a>' in markup.content


def test_linked_documents_not_repeated_when_the_text_has_them() -> None:
    page = f"""<html><body><article><h1>Sheet</h1><p>{LONG}
    Download the <a href="/files/sheet.pdf">worksheet</a> now.</p></article></body></html>"""

    content = convert_html(page, URL, "markdown").content

    assert content.count("https://www.example.org/files/sheet.pdf") == 1


def test_fallback_to_bs4_when_trafilatura_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(html_module, "extract", lambda *args, **kwargs: None)
    page = ARTICLE

    result = convert_html(page, URL, "markdown")

    assert result.converter == "bs4"
    assert result.warnings == ["trafilatura found no main content; used the page text instead"]
    lines = result.content.splitlines()
    assert "Fractions explained" in lines  # one block per line
    assert any(line.startswith("Fractions describe parts of a whole") for line in lines)
    assert "Fraction Decimal" in lines  # table cells share a line, rows do not
    assert "1/4 0.25" in lines
    assert "Example School" not in result.content  # the <title> is not page text
    assert "Navigation Home Link" not in result.content
    assert "Hidden by style marker" not in result.content
    assert "trackingMarker" not in result.content
    assert "Footer copyright marker" not in result.content


def test_fallback_when_trafilatura_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(html_module, "extract", boom)

    result = convert_html(ARTICLE, URL, "text")

    assert result.converter == "bs4"
    assert result.warnings == ["trafilatura failed (RuntimeError); used the page text instead"]
    assert "Fractions describe parts of a whole" in result.content


def test_fallback_in_html_format_wraps_lines_in_paragraphs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(html_module, "extract", lambda *args, **kwargs: None)
    page = "<html><body><h1>Title &amp; more</h1><p>One <b>bold</b> thing</p></body></html>"

    result = convert_html(page, URL, "html")

    assert result.content == (
        "<html><body><p>Title &amp; more</p><p>One bold thing</p></body></html>"
    )


def test_page_without_text_gives_empty_content() -> None:
    result = convert_html(
        "<html><body><script>x()</script><p hidden>x</p></body></html>", URL, "text"
    )

    assert result.content == ""
    assert result.warnings == ["The page contains no text"]


def test_title_falls_back_to_the_title_element() -> None:
    page = f"<html><head><title> My  Page </title></head><body><p>{LONG}</p></body></html>"

    assert convert_html(page, URL, "text").title == "My Page"
    assert convert_html("<p>no title</p>", URL, "text").title is None


def test_html_with_xml_declaration_and_entities() -> None:
    page = (
        '<?xml version="1.0" encoding="utf-8"?><html><body><article><h1>Caf&eacute;</h1>'
        f"<p>{LONG}</p></article></body></html>"
    )

    assert convert_html(page, URL, "markdown").content.startswith("# Café")


def test_convert_html_gives_the_same_result_in_concurrent_threads() -> None:
    expected = convert_html(ARTICLE, URL, "markdown")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: convert_html(ARTICLE, URL, "markdown"), range(16)))

    assert results == [expected] * 16


def test_equations_survive_every_format_unescaped() -> None:
    page = f"""<html><body><article><h1>Inequality</h1><p>{LONG}</p>
    <p>Claim: <math><mi>a</mi><mo>&lt;</mo><msub><mi>b</mi><mn>1</mn></msub></math> and
    <math><mi>x</mi>
    <annotation encoding="application/x-tex">x_1 * y_2 \\le [z]</annotation></math>.</p>
    </article></body></html>"""

    markdown = convert_html(page, URL, "markdown").content
    text = convert_html(page, URL, "text").content
    markup = convert_html(page, URL, "html").content

    assert "$$a < b_{1}$$" in markdown
    assert "$$x_1 * y_2 \\le [z]$$" in markdown
    assert "$$x_1 * y_2 \\le [z]$$" in text
    assert "$$a &lt; b_{1}$$" in markup
    assert "EQUATIONTOKEN" not in markdown + text + markup


def test_plain_text_passthrough() -> None:
    result = convert_plain("  First line\r\nSecond line\rThird line\n\n\nLast  line  \n")

    assert result.converter == "plain"
    assert result.content == "First line\nSecond line\nThird line\n\n\nLast  line"
    assert result.title is None
    assert result.warnings == []
    assert convert_plain("").content == ""


def test_xhtml_with_xml_declaration_is_converted_without_warnings() -> None:
    page = '<?xml version="1.0" encoding="utf-8"?><html><body><p>Only text</p></body></html>'

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = convert_html(page, URL, "text")
        page_links(page, URL)

    assert result.content == "Only text"


def test_linked_document_is_not_mistaken_for_a_longer_url_in_the_text() -> None:
    page = f"""<html><body><main><h1>Downloads</h1><p>{LONG} Get the
    <a href="/files/a.pdf.zip">archive</a>.</p>
    <ul><li><a href="/files/a.pdf">Sheet A</a></li></ul></main></body></html>"""

    content = convert_html(page, URL, "markdown").content

    assert "(https://www.example.org/files/a.pdf.zip)" in content
    assert "[Sheet A](https://www.example.org/files/a.pdf)" in content


@pytest.mark.parametrize("depth", [300, 2000])
def test_content_nested_deeper_than_255_elements_is_kept(depth: int) -> None:
    deep = "Deep paragraph about leaves and their many green cells. " * 8
    page = (
        f"<html><body><article><h1>Leaves</h1><p>{LONG}</p><p>{LONG}</p>"
        + "<div>" * depth
        + f"<p>{deep}</p>"
        + "</div>" * depth
        + "</article></body></html>"
    )

    started = time.perf_counter()
    result = convert_html(page, URL, "markdown")

    assert time.perf_counter() - started < 20
    assert result.converter == "trafilatura"
    assert "Deep paragraph about leaves" in result.content


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_links_with_a_script_or_data_scheme_lose_their_target(fmt: OutputFormat) -> None:
    page = f"""<html><body><article><h1>Links</h1><p>{LONG}
    <a href="javascript:alert(1)">Script link</a>, <a href="JaVaScRiPt:alert(2)">Mixed case</a>,
    <a href="data:text/html;base64,PHNjcmlwdD4=">Data link</a>,
    <a href="vbscript:msgbox(1)">VB link</a> and <a href="/lessons/next">next lesson</a>.</p>
    </article></body></html>"""

    content = convert_html(page, URL, fmt).content.lower()

    for scheme in ("javascript:", "data:", "vbscript:"):
        assert scheme not in content
    assert "script link" in content
    assert "data link" in content
    assert "https://www.example.org/lessons/next" in content


@pytest.mark.parametrize(
    "text",
    ["<" * 80_000, "](" * 80_000, "<a" * 40_000 + "](x" * 40_000],
    ids=["open-tags", "open-link-targets", "both"],
)
def test_the_heading_check_takes_linear_time(text: str) -> None:
    started = time.perf_counter()

    html_module._compact(text)

    assert time.perf_counter() - started < 5


def _nested_tables(depth: int) -> str:
    intro = "<p>" + "intro text " * 200 + "</p>"
    return (
        f"<html><head><title>t</title></head><body><main>{intro}"
        + "<table>" * depth
        + "deep"
        + "</table>" * depth
        + "</main></body></html>"
    )


def test_deeply_nested_tables_convert_in_bounded_time() -> None:
    # Trafilatura's table handling is quadratic in the nesting: 2000 nested tables (30 KB) held
    # a conversion thread for about 35 s, past every request's deadline.
    started = time.perf_counter()

    result = convert_html(_nested_tables(2000), "https://example.org/", "markdown")

    assert time.perf_counter() - started < 10
    assert "intro text" in result.content


def test_only_tables_nested_deeper_than_the_cap_lose_their_table_tag() -> None:
    cap = html_module.MAX_TABLE_NESTING
    soup = BeautifulSoup(_nested_tables(cap + 5), "html.parser")

    html_module.flatten_deep_tables(soup)

    assert len(soup.find_all("table")) == cap
    assert "deep" in soup.get_text()
    shallow = BeautifulSoup(_nested_tables(cap), "html.parser")
    html_module.flatten_deep_tables(shallow)
    assert len(shallow.find_all("table")) == cap


def _page(content: str) -> str:
    return f"<html><body><main><h1>Inline runs</h1>{content}<p>{LONG}</p></main></body></html>"


def test_inline_runs_after_br_stay_one_paragraph() -> None:
    result = convert_html(_page("<div><br/>Start <b>b</b> danach</div>"), URL, "markdown")

    assert "Start **b** danach" in result.content


def test_leading_inline_element_keeps_spaces() -> None:
    page = _page('<div><a href="https://e.org/z">W</a> danach text <b>x</b> mehr</div>')

    result = convert_html(page, URL, "markdown")

    assert "[W](https://e.org/z) danach text **x** mehr" in result.content


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<div><b>Fach:</b> Mathematik</div>", "**Fach:** Mathematik"),
        (
            "<div><i>Hinweis:</i> Das Material ist frei.</div>",
            "*Hinweis:* Das Material ist frei.",
        ),
    ],
)
def test_a_leading_label_stays_on_the_line_of_its_text(
    content: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(content)

    assert expected in convert_html(page, URL, "markdown").content
    assert expected not in _without_wrapping(page, "markdown", monkeypatch)


def test_mixed_block_and_inline_children() -> None:
    page = _page("<div><b>W</b>: msg<div>Inner <b>z</b> end</div>tail <i>t</i> after</div>")

    content = convert_html(page, URL, "markdown").content

    paragraphs = content.split("\n\n")
    assert "**W**: msg" in paragraphs
    assert "Inner **z** end" in paragraphs
    assert "tail *t* after" in paragraphs
    first = paragraphs.index("**W**: msg")
    assert paragraphs.index("Inner **z** end") == first + 1
    assert paragraphs.index("tail *t* after") == first + 2


def _without_wrapping(page: str, fmt: OutputFormat, monkeypatch: pytest.MonkeyPatch) -> str:
    with monkeypatch.context() as patched:
        patched.setattr(html_module, "wrap_inline_runs", lambda soup: None)
        return convert_html(page, URL, fmt).content


@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
@pytest.mark.parametrize(
    "content",
    [
        # Ordinary prose in a <div>: no leading inline element, so nothing moves.
        '<div>Text with a <a href="/l">link</a> and <b>bold</b> words.<br/>Second '
        "<i>line</i> here.</div><div>Another <strong>block</strong> of text.</div>",
        # Table cells keep their structure, even when a cell starts with an inline element.
        "<table><tr><th><b>Name</b> of it</th><th>Value</th></tr>"
        "<tr><td><a href='/x'>x</a> one</td><td><br/>two <b>2</b></td></tr></table>",
        # Nothing inside <pre> moves.
        "<pre><b>W</b>: code <i>x</i>\n<div><b>y</b> z</div></pre>",
        # A <p> keeps its own structure.
        "<p><b>W</b>: already <i>a</i> paragraph</p>",
        # Lists and quotes, which Trafilatura does not split, are not wrapped.
        "<ul><li><b>Term</b>: definition <i>x</i> more</li><li><br/>Start <b>b</b> on</li></ul>"
        "<blockquote><i>Quote</i> said <b>someone</b> wise</blockquote>",
    ],
    ids=["div-prose", "table-cells", "pre", "paragraph", "list-and-quote"],
)
def test_content_without_a_leading_inline_run_is_unchanged(
    content: str, fmt: OutputFormat, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(content)

    assert convert_html(page, URL, fmt).content == _without_wrapping(page, fmt, monkeypatch)


TEASERS = (
    "<html><body>"
    + "".join(
        f'<div><a href="/m{index}">Material {index} zur Bruchrechnung</a>'
        "<span>Ein kurzer Text über Brüche.</span></div>"
        for index in range(5)
    )
    + "</body></html>"
)
CARDS = _page(
    "".join(
        f"<div class=card><span>12.05.2024</span><span>Mathematik</span><h3>Karte {index}</h3>"
        f"<p>{LONG}</p></div>"
        for index in range(4)
    )
)
KEY_VALUES = (
    "<html><body><h1>Daten</h1>"
    + "".join(
        f"<div><span>Feld {index}</span><span>Wert {index}</span></div>" for index in range(5)
    )
    + f"<p>{LONG}</p></body></html>"
)
SENTENCES = (
    "<html><body><div><span>Ein erster Satz, der hier steht und gelesen wird.</span>"
    "<span>Ein zweiter Satz, der danach kommt.</span></div>"
    "<p>Noch ein Absatz mit etwas Text.</p></body></html>"
)


@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
@pytest.mark.parametrize(
    "page",
    [TEASERS, CARDS, KEY_VALUES, SENTENCES],
    ids=["teasers", "card-meta", "key-values", "sentences-in-spans"],
)
def test_a_run_of_inline_elements_without_own_text_is_not_wrapped(
    page: str, fmt: OutputFormat, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adjacent inline elements with no text of the container between them (teaser links,
    # card meta, key-value rows) are no prose run: wrapping them glued them together.
    assert convert_html(page, URL, fmt).content == _without_wrapping(page, fmt, monkeypatch)


def test_wrap_inline_runs_leaves_cells_pre_paragraphs_lists_and_quotes_alone() -> None:
    markup = (
        "<table><tr><td><b>a</b> b</td><th><br/>c</th></tr></table>"
        "<pre><b>d</b> e<div><b>f</b> g</div></pre><p><b>h</b> i</p>"
        "<ul><li><b>j</b> k</li></ul><blockquote><br/><i>l</i> m</blockquote>"
    )
    soup = BeautifulSoup(f"<html><body>{markup}</body></html>", "lxml")
    before = str(soup)

    html_module.wrap_inline_runs(soup)

    assert str(soup) == before


def test_wrap_inline_runs_wraps_only_runs_with_own_text() -> None:
    soup = BeautifulSoup(
        "<html><body><section><br/> <b>a</b> b<br/>c<div>d</div> <br/> <div>e</div>"
        "<i>f</i><span>g</span></section></body></html>",
        "lxml",
    )

    html_module.wrap_inline_runs(soup)

    section = soup.find("section")
    assert section is not None
    children = [child.name for child in section.find_all(recursive=False)]
    # The <br>-only run and the run of elements without text of the section stay as they are.
    assert children == ["p", "div", "br", "div", "i", "span"]
    first = section.find("p")
    assert first is not None
    assert str(first) == "<p><b>a</b> b<br/>c</p>"  # the leading <br> and space are gone


def _wrapping(body: Callable[[int], str]) -> Callable[[int], Callable[[], object]]:
    """For ``growth``: a fresh page of the given size, and the wrapping to time on it."""

    def prepare(size: int) -> Callable[[], object]:
        soup = BeautifulSoup(f"<html><body>{body(size)}</body></html>", "lxml")
        return lambda: html_module.wrap_inline_runs(soup)

    return prepare


def _nested(depth: int) -> str:
    return "<div><b>x</b> y" * depth + "</div>" * depth


def test_wrap_inline_runs_handles_deep_nesting() -> None:
    soup = BeautifulSoup(f"<html><body>{_nested(3000)}</body></html>", "lxml")

    html_module.wrap_inline_runs(soup)  # no recursion: no RecursionError

    assert len(soup.find_all("p")) == 3000
    assert growth(_wrapping(_nested), 500) < GROWTH_LIMIT


PHP_PAGE = _page(
    "<div class='x'>"
    + "".join(
        "<br />\n<b>Warning</b>:  Undefined array key "
        f'"http://w3id.org/openeduhub/vocabs/discipline/{subject}" in '
        "<b>/var/www/prod.210317/wp-content/themes/wir-lernen-online/front-page.php</b> on line "
        "<b>128</b><br />\n"
        for subject in ("Sport", "Kunst")
    )
    + "Mechatronik Germanistik</div>"
)


@pytest.mark.parametrize("fmt", ["markdown", "text", "html"])
def test_php_warnings_removed_and_reported(fmt: OutputFormat) -> None:
    result = convert_html(PHP_PAGE, URL, fmt)

    assert "Warning" not in result.content
    assert "front-page.php" not in result.content
    assert "Mechatronik Germanistik" in result.content
    assert result.warnings.count("server_errors_removed") == 1
    assert len(result.server_errors) == 2
    assert all(error.level == "Warning" for error in result.server_errors)
    assert result.server_errors[0].message.startswith("Undefined array key ")


def test_php_warnings_removed_in_the_page_text_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(html_module, "extract", lambda *args, **kwargs: None)

    result = convert_html(PHP_PAGE, URL, "markdown")

    assert result.converter == "bs4"
    assert "Warning" not in result.content
    assert "Mechatronik Germanistik" in result.content
    assert "server_errors_removed" in result.warnings


def test_page_without_server_errors_has_no_warning() -> None:
    result = convert_html(ARTICLE, URL, "markdown")

    assert "server_errors_removed" not in result.warnings
    assert result.server_errors == []


def _runs(size: int) -> str:
    return "<div>" + "<b>x</b> y<hr/>" * size + "</div>"


def test_wrap_inline_runs_takes_linear_time_in_a_wide_container() -> None:
    soup = BeautifulSoup(f"<html><body>{_runs(1_000)}</body></html>", "lxml")

    html_module.wrap_inline_runs(soup)

    div = soup.find("div")
    assert div is not None
    assert [child.name for child in div.find_all(recursive=False)] == ["p", "hr"] * 1_000
    # The quadratic version (bs4 finding each moved node's position) grew about 15-fold.
    assert growth(_wrapping(_runs), 1_000) < GROWTH_LIMIT


# Final review of package 22, finding 2: a MediaWiki figure links its image to the file's
# description page (``/wiki/Datei:Foo.svg``); it is no document of the article.
WIKI_URL = "https://de.wikipedia.org/wiki/Wasserkreislauf"
WIKI_ARTICLE = f"""<html lang="de"><head><title>Wasserkreislauf</title></head><body>
<main><h1>Wasserkreislauf</h1><p>{LONG}</p>
<figure typeof="mw:File/Thumb"><a href="/wiki/Datei:Foo.svg" class="mw-file-description"><img
src="//upload.wikimedia.org/wikipedia/commons/thumb/a/a1/Foo.svg/220px-Foo.svg.png" alt=""
width="220" height="165"></a><figcaption>Der Kreislauf des Wassers</figcaption></figure>
<p>{LONG}</p></main></body></html>"""


def test_a_wiki_figure_link_to_an_svg_is_no_linked_document() -> None:
    content = convert_html(WIKI_ARTICLE, WIKI_URL, "markdown").content

    assert "Datei:Foo.svg" not in content
    assert "\n---\n" not in content
