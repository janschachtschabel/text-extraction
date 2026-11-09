import time

from bs4 import BeautifulSoup

from text_extraction.convert import markup
from text_extraction.convert.markup import enhance_table_structure


def test_table_without_separator_gets_one() -> None:
    text = "Intro\n\n| A | B |\n| 1 | 2 |\n| 3 | 4 |\n\nOutro"

    assert enhance_table_structure(text) == (
        "Intro\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\nOutro"
    )


def test_table_with_separator_is_left_alone() -> None:
    text = "| A | B |\n|---|:---:|\n| 1 | 2 |"

    assert enhance_table_structure(text) == text


def test_fenced_code_that_looks_like_a_table_is_left_alone() -> None:
    text = "```\n| a | b |\n| c | d |\n```"

    assert enhance_table_structure(text) == text


def test_rows_of_different_width_and_single_rows_are_left_alone() -> None:
    assert enhance_table_structure("| a | b |\n| c |") == "| a | b |\n| c |"
    assert enhance_table_structure("| only |") == "| only |"


def test_revealing_many_formulas_under_one_parent_takes_linear_time() -> None:
    soup = BeautifulSoup("<html><body><p></p></body></html>", "lxml")
    parent = soup.p
    assert parent is not None
    replacements = []
    for _ in range(20_000):
        replacement = soup.new_tag("span")
        replacement.string = "$$x$$"
        parent.append(replacement)
        parent.append(" and ")
        replacements.append(replacement)

    started = time.perf_counter()
    for replacement in replacements:
        markup._reveal(replacement)

    assert time.perf_counter() - started < 5


def test_table_rows_are_padded_to_the_widest_row() -> None:
    text = (
        "| 7 Basisgrößen |\n|---|\n"
        "| Größe | Symbol | Zeichen | Einheit |\n| Länge | l | m | Meter |"
    )
    assert enhance_table_structure(text) == (
        "| 7 Basisgrößen |  |  |  |\n|---|---|---|---|\n"
        "| Größe | Symbol | Zeichen | Einheit |\n| Länge | l | m | Meter |"
    )


def test_padding_keeps_the_trailing_space_of_trafilatura_rows() -> None:
    text = "| 7 Basisgrößen | \n| --- | \n| Länge | l | m | Meter | \nafter"
    assert enhance_table_structure(text) == (
        "| 7 Basisgrößen |  |  |  | \n| --- |---|---|---| \n| Länge | l | m | Meter | \nafter"
    )


def test_padding_ignores_escaped_pipes_and_fenced_code() -> None:
    text = "| a \\| b |\n|---|\n| c | d |"
    assert enhance_table_structure(text) == "| a \\| b |  |\n|---|---|\n| c | d |"
    fenced = "```\n| a |\n| b | c |\n```"
    assert enhance_table_structure(fenced) == fenced


def test_table_with_equal_rows_stays_byte_for_byte() -> None:
    text = "| a | b | \n| --- | --- | \n| c | d | "
    assert enhance_table_structure(text) == text


def test_a_bare_pipe_line_is_no_separator() -> None:
    assert enhance_table_structure("text\n|\n| a | b |") == "text\n|\n| a | b |"
    assert enhance_table_structure("|\n|") == "|\n|"


def test_append_to_body_puts_the_block_inside_the_body() -> None:
    assert markup.append_to_body("<html><body><p>a</p></body>\n</html>", "<ul></ul>") == (
        "<html><body><p>a</p><ul></ul></body>\n</html>"
    )
    assert markup.append_to_body("<html><p>a</p></HTML>", "<ul></ul>") == (
        "<html><p>a</p><ul></ul></HTML>"
    )
    assert markup.append_to_body("<p>a</p>", "<ul></ul>") == "<p>a</p><ul></ul>"
