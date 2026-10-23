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
