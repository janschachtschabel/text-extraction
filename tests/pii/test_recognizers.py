"""The honorific recognizer (``pii/recognizers.py``, ruling N6 with R5): a name after a salutation
or a title is a ``PERSON`` of score 0.7, the span is the name only. It needs Presidio (marker
``nlp``) but no model."""

import importlib.util

import pytest

pytestmark = [
    pytest.mark.nlp,
    pytest.mark.skipif(
        importlib.util.find_spec("presidio_analyzer") is None,
        reason="the extra nlp is not installed (uv sync --extra nlp)",
    ),
]


NBSP = "\N{NO-BREAK SPACE}"


def names(text: str, language: str) -> list[str]:
    from text_extraction.pii.recognizers import HonorificRecognizer

    results = HonorificRecognizer(language).analyze(text, ["PERSON"])  # type: ignore[arg-type]
    assert all(result.entity_type == "PERSON" and result.score == 0.7 for result in results)
    return [text[result.start : result.end] for result in results]


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Herr Müller", "Müller"),
        ("Bitte fragen Sie Herrn Schmidt.", "Schmidt"),
        ("Frau Winter kommt später.", "Winter"),
        ("Hr. Meyer-Schulz ist da.", "Meyer-Schulz"),
        ("Fr. Anna Neumann ist da.", "Anna Neumann"),
        ("Frau Dr. Anna Schmidt leitet es.", "Anna Schmidt"),
        ("Herrn Prof. Dr. Henrik Sommer wurde gedankt.", "Henrik Sommer"),
        ("Herr Doktor Weber bittet.", "Weber"),
        ("Doktor Weber bittet.", "Weber"),
        ("Dr. Anna Schmidt leitet das Projekt.", "Anna Schmidt"),
        ("Prof. Dr. Henrik Sommer hielt den Vortrag.", "Henrik Sommer"),
        ("Prof. Neumann spricht.", "Neumann"),
        ("Frau Anna-Lena Meyer-Schulz spricht.", "Anna-Lena Meyer-Schulz"),
        (f"Herr Müller{NBSP}Schmidt", f"Müller{NBSP}Schmidt"),
        ("Frau Zoë Meier", "Zoë Meier"),
    ],
)
def test_german_salutations_and_titles(text: str, name: str) -> None:
    assert names(text, "de") == [name]


@pytest.mark.parametrize(
    "text",
    [
        "herr müller",  # lower case is no name
        "Herr der Ringe",
        "Frau und Mann",
        "Frau",
        "Dr. med.",
        "Der Doktor kommt.",
        "Mehrere Herren Müller",  # `Herren` is no salutation
        "Doktorand Meyer",
        "Dr.Anna",  # the title needs a blank before the name
        "Andr. Meyer",  # `Dr.` inside a word is no title
        "Mr Smith",  # English salutations are not German ones
    ],
)
def test_german_non_names(text: str) -> None:
    assert names(text, "de") == []


def test_a_name_does_not_run_over_a_line_break() -> None:
    assert names("Herr Müller\nSchmidt", "de") == ["Müller"]


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Mr Smith", "Smith"),
        ("Mrs. Jones will call.", "Jones"),
        ("Ms Anna-Maria Lopez", "Anna-Maria Lopez"),
        ("Mx Taylor", "Taylor"),
        ("Dr Watson", "Watson"),
        ("Dr. Sarah Connor", "Sarah Connor"),
        ("Mr Dr. Smith", "Smith"),
        ("Prof. Dr. Alan Turing", "Alan Turing"),
        ("Prof. Turing spoke.", "Turing"),
    ],
)
def test_english_salutations_and_titles(text: str, name: str) -> None:
    assert names(text, "en") == [name]


@pytest.mark.parametrize("text", ["Dr smith", "Amr Smith", "the mr Smith", "Mr", "Herr Müller"])
def test_english_non_names(text: str) -> None:
    assert names(text, "en") == []


def test_only_when_persons_are_asked_for() -> None:
    from text_extraction.pii.recognizers import HonorificRecognizer

    assert HonorificRecognizer("de").analyze("Herr Müller", ["EMAIL_ADDRESS"]) == []


def test_other_languages_have_no_honorifics() -> None:
    from text_extraction.pii.recognizers import HonorificRecognizer

    with pytest.raises(ValueError, match="de and en"):
        HonorificRecognizer("fr")  # type: ignore[arg-type]
