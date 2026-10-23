import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest

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
    page = f"""<html><head><title>Worksheets</title></head><body>
    <nav><a href="/downloads/site-map.pdf">Site map</a></nav>
    <main><h1>Worksheets for grade 5</h1>
    <p>{LONG}</p>
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
