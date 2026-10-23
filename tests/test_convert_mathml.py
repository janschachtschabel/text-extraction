# ruff: noqa: RUF001 - the tests use the characters whose handling they check
import pytest
from bs4 import BeautifulSoup, Tag

from text_extraction.convert.mathml import to_latex


def latex(mathml: str) -> str:
    math = BeautifulSoup(f"<math>{mathml}</math>", "lxml").find("math")
    assert isinstance(math, Tag)
    return to_latex(math)


@pytest.mark.parametrize(
    ("mathml", "expected"),
    [
        ("<mfrac><mi>a</mi><mi>b</mi></mfrac>", r"\frac{a}{b}"),
        ("<msqrt><mi>x</mi></msqrt>", r"\sqrt{x}"),
        ("<mroot><mi>x</mi><mn>3</mn></mroot>", r"\sqrt[3]{x}"),
        ("<msup><mi>x</mi><mn>2</mn></msup>", "x^{2}"),
        ("<msub><mi>a</mi><mn>1</mn></msub>", "a_{1}"),
        ("<msubsup><mo>∑</mo><mn>0</mn><mi>n</mi></msubsup>", r"\sum_{0}^{n}"),
        ("<mover><mi>v</mi><mo>→</mo></mover>", r"\vec{v}"),
        ("<mover><mi>x</mi><mo>=</mo></mover>", r"\overset{=}{x}"),
        ("<mi>x</mi><mo>≤</mo><mn>5</mn><mo>°</mo>", r"x \leq 5 \circ"),
        ("<mi>ℝ</mi>", r"\mathbb{R}"),
        ("<mtext>if x</mtext>", r"\text{if x}"),
        ("<mi>$</mi><mi>a_b</mi>", r"\$ a\_b"),
        ("<mi>x</mi><mspace></mspace><annotation>ignored</annotation>", "x"),
        ('<mfenced open="[" close="]"><mi>a</mi></mfenced>', "[a]"),
        ("<mtable><mtr><mtd><mn>1</mn></mtd><mtd><mn>2</mn></mtd></mtr></mtable>", None),
    ],
)
def test_presentation_mathml_conversion(mathml: str, expected: str | None) -> None:
    result = latex(mathml)

    if expected is None:
        assert result.startswith(r"\begin{matrix}")
        assert "1 & 2" in result
    else:
        assert result == expected


def test_deeply_nested_mathml_falls_back_to_text_instead_of_recursing() -> None:
    mathml = "<mrow>" * 200 + "<mi>x</mi>" + "</mrow>" * 200

    assert latex(mathml) == "x"
