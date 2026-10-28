import pytest

from text_extraction.pii.html import HtmlText
from text_extraction.pii.types import Span


def span(start: int, end: int, type_: str = "PERSON") -> Span:
    return Span(start=start, end=end, type=type_, score=0.9)  # type: ignore[arg-type]


def find(parsed: HtmlText, value: str, type_: str = "PERSON") -> Span:
    start = parsed.text.index(value)
    return span(start, start + len(value), type_)


def test_html_name_across_inline_nodes() -> None:
    html = "<p><b>Anna</b> Schmidt kam.</p>"
    parsed = HtmlText.parse(html)

    assert parsed.text == "Anna Schmidt kam."
    assert parsed.render([(find(parsed, "Anna Schmidt"), "[PERSON]")]) == (
        "<p><b>[PERSON]</b> kam.</p>"
    )


def test_html_name_across_inline_nodes_without_blank() -> None:
    parsed = HtmlText.parse("<p>Hi <b>An</b><i>na</i>!</p>")

    assert parsed.text == "Hi Anna!"
    assert (
        parsed.render([(find(parsed, "Anna"), "[PERSON]")]) == "<p>Hi <b>[PERSON]</b><i></i>!</p>"
    )


def test_html_alt_and_title_attributes_masked_href_kept() -> None:
    html = (
        '<p><a href="https://example.org/anna" title="Anna Schmidt">link</a>'
        '<img src="https://example.org/a.png" alt="Foto von Anna Schmidt"></p>'
    )
    parsed = HtmlText.parse(html)

    assert "Anna Schmidt" in parsed.text
    assert "example.org" not in parsed.text
    replacements = [
        (span(start, start + len("Anna Schmidt")), "[PERSON]")
        for start in (
            parsed.text.index("Anna Schmidt"),
            parsed.text.rindex("Anna Schmidt"),
        )
    ]

    assert parsed.render(replacements) == (
        '<p><a href="https://example.org/anna" title="[PERSON]">link</a>'
        '<img src="https://example.org/a.png" alt="Foto von [PERSON]"></p>'
    )


@pytest.mark.parametrize("href", ["mailto:anna@example.org", "tel:+4930123456", "MAILTO:anna@x.de"])
def test_html_contact_href_is_a_node_and_replaced(href: str) -> None:
    parsed = HtmlText.parse(f'<p>Write <a href="{href}">here</a>.</p>')

    assert href in parsed.text
    start = parsed.text.index(href)
    scheme = href.index(":") + 1

    rendered = parsed.render([(span(start + scheme, start + len(href), "EMAIL_ADDRESS"), "[X]")])

    assert f'href="{href[:scheme]}[X]"' in rendered
    assert href[scheme:] not in rendered


def test_html_replacement_escaped() -> None:
    parsed = HtmlText.parse("<p>Anna kam.</p>")

    rendered = parsed.render([(find(parsed, "Anna"), "<b>&</b>")])

    assert rendered == "<p>&lt;b&gt;&amp;&lt;/b&gt; kam.</p>"


def test_html_block_nodes_get_a_space() -> None:
    parsed = HtmlText.parse("<p>Anna</p><p>Schmidt</p>")

    assert parsed.text == "Anna Schmidt"
    assert parsed.render([(find(parsed, "Anna Schmidt"), "[PERSON]")]) == "<p>[PERSON]</p><p></p>"


def test_html_br_and_list_items_separate_words() -> None:
    assert HtmlText.parse("<p>Anna<br>Schmidt</p>").text == "Anna Schmidt"
    assert HtmlText.parse("<ul><li>Anna</li><li>Schmidt</li></ul>").text == "Anna Schmidt"
    assert HtmlText.parse("<table><tr><td>Anna</td><td>Schmidt</td></tr></table>").text == (
        "Anna Schmidt"
    )


def test_html_no_extra_space_next_to_whitespace() -> None:
    assert HtmlText.parse("<p>Anna </p>\n<p>Schmidt</p>").text == "Anna \nSchmidt"


def test_html_script_and_style_are_not_text() -> None:
    parsed = HtmlText.parse("<p>Anna</p><script>var a = 'Bob';</script><style>b {}</style>")

    assert parsed.text == "Anna"


def test_html_comment_tail_is_text_and_stays_without_drop() -> None:
    html = "<p>Anna<!-- note -->Schmidt</p>"
    parsed = HtmlText.parse(html)

    assert parsed.text == "AnnaSchmidt"
    assert parsed.has_comments
    assert parsed.render([(find(parsed, "AnnaSchmidt"), "[PERSON]")]) == (
        "<p>[PERSON]<!-- note --></p>"
    )
    assert parsed.render([]) == html


def test_html_drop_comments_with_a_replacement() -> None:
    parsed = HtmlText.parse("<p>Anna<!-- Bob Meier -->Schmidt kam.</p><!-- end -->")

    rendered = parsed.render([(find(parsed, "AnnaSchmidt"), "[PERSON]")], drop_comments=True)

    assert rendered == "<p>[PERSON] kam.</p>"


def test_html_drop_comments_without_a_replacement_keeps_the_tails() -> None:
    parsed = HtmlText.parse("<p>Anna<!-- Bob Meier -->Schmidt</p><!-- end -->tail")

    assert parsed.render([], drop_comments=True) == "<p>AnnaSchmidt</p>tail"


def test_html_drop_comments_takes_processing_instructions_too() -> None:
    parsed = HtmlText.parse("<p>a<?php echo 'Carl' ?>b</p>")

    assert parsed.has_comments
    assert parsed.render([], drop_comments=True) == "<p>ab</p>"


def test_html_drop_comments_without_any_returns_the_original() -> None:
    html = "<p>Anna <br/> kam  &nbsp; </p>"
    parsed = HtmlText.parse(html)

    assert not parsed.has_comments
    assert parsed.render([], drop_comments=True) == html


def test_html_drop_comments_beside_the_root_and_in_a_head() -> None:
    html = (
        "<!-- a --><!DOCTYPE html><html><head><!-- b --><title>T</title></head>"
        "<body>x</body></html><!-- z -->"
    )
    parsed = HtmlText.parse(html)

    assert parsed.has_comments
    rendered = parsed.render([], drop_comments=True)

    assert "<!--" not in rendered
    assert rendered == "<!DOCTYPE html>\n<html><head><title>T</title></head><body>x</body></html>"


def test_html_drop_comments_is_repeatable() -> None:
    parsed = HtmlText.parse("<p>Anna<!-- c -->Schmidt</p>")
    replacements = [(find(parsed, "Anna"), "[PERSON]")]

    first = parsed.render(replacements, drop_comments=True)

    assert first == parsed.render(replacements, drop_comments=True) == "<p>[PERSON]Schmidt</p>"
    # The tree of a render without dropping is not touched by the one that dropped.
    assert parsed.render(replacements) == "<p>[PERSON]<!-- c -->Schmidt</p>"


def test_html_entities_decoded_in_text_and_reescaped() -> None:
    html = "<p>Müller &amp; Söhne &lt;GmbH&gt; Anna</p>"
    parsed = HtmlText.parse(html)

    assert parsed.text == "Müller & Söhne <GmbH> Anna"
    assert parsed.render([(find(parsed, "Anna"), "[PERSON]")]) == (
        "<p>Müller &amp; Söhne &lt;GmbH&gt; [PERSON]</p>"
    )


def test_html_without_replacements_returns_the_original() -> None:
    html = "<p>Anna <br/> kam  &nbsp; <b >x</b></p>"

    assert HtmlText.parse(html).render([]) == html


def test_html_full_document_keeps_its_shell() -> None:
    html = (
        "<!DOCTYPE html><html><head><title>Anna</title></head><body><p>Anna kam.</p></body></html>"
    )
    parsed = HtmlText.parse(html)

    assert parsed.text == "Anna Anna kam."
    spans = [
        (span(0, 4), "[PERSON]"),
        (span(5, 9), "[PERSON]"),
    ]
    assert parsed.render(spans) == (
        "<!DOCTYPE html>\n<html><head><title>[PERSON]</title></head>"
        "<body><p>[PERSON] kam.</p></body></html>"
    )


def test_html_page_without_doctype_gets_none() -> None:
    parsed = HtmlText.parse("<html><body><p>Anna</p></body></html>")

    assert parsed.render([(find(parsed, "Anna"), "[PERSON]")]) == (
        "<html><body><p>[PERSON]</p></body></html>"
    )


def test_html_body_without_html_element_renders_the_body() -> None:
    parsed = HtmlText.parse("<body><p>Anna</p></body>")

    assert parsed.render([(find(parsed, "Anna"), "[PERSON]")]) == "<body><p>[PERSON]</p></body>"


def test_html_text_before_the_first_element_and_after_the_last() -> None:
    parsed = HtmlText.parse("Anna <b>kam</b> heim")

    assert parsed.text == "Anna kam heim"
    assert parsed.render([(find(parsed, "Anna"), "[PERSON]"), (find(parsed, "heim"), "[X]")]) == (
        "[PERSON] <b>kam</b> [X]"
    )


def test_html_span_starting_in_a_separator_begins_at_the_next_node() -> None:
    parsed = HtmlText.parse("<p>Anna</p><p>Schmidt</p>")

    assert parsed.render([(span(4, 12), "[PERSON]")]) == "<p>Anna</p><p>[PERSON]</p>"


def test_html_render_twice_gives_the_same() -> None:
    parsed = HtmlText.parse("<p><b>Anna</b> Schmidt kam.</p>")
    replacements = [(find(parsed, "Anna Schmidt"), "[PERSON]")]

    assert parsed.render(replacements) == parsed.render(replacements)


def test_html_several_replacements_in_one_node_and_across() -> None:
    parsed = HtmlText.parse("<p>Anna und Bob und <i>Carl</i>.</p>")

    rendered = parsed.render(
        [
            (find(parsed, "Anna"), "[A]"),
            (find(parsed, "Bob"), "[B]"),
            (find(parsed, "Carl"), "[C]"),
        ]
    )

    assert rendered == "<p>[A] und [B] und <i>[C]</i>.</p>"


@pytest.mark.parametrize("html", ["", "   ", "\n"])
def test_html_empty_has_no_text(html: str) -> None:
    parsed = HtmlText.parse(html)

    assert parsed.text == ""
    assert parsed.render([]) == html


def test_html_invalid_control_characters_do_not_break_parsing() -> None:
    parsed = HtmlText.parse("<p>An\x00na</p>")

    assert parsed.text == "Anna"


def test_html_drop_comments_of_a_source_that_is_only_a_comment() -> None:
    parsed = HtmlText.parse("<!-- Bob Meier -->")

    assert parsed.text == ""
    assert parsed.has_comments
    assert parsed.render([], drop_comments=True) == ""
    assert parsed.render([]) == "<!-- Bob Meier -->"
