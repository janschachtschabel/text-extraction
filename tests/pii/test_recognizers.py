"""The honorific recognizer (``pii/recognizers.py``, ruling N6 with R5): a name after a salutation
or a title is a ``PERSON`` of score 0.7, the span is the name only. It needs Presidio (marker
``nlp``) but no model."""

import importlib.util
import time

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
        ("Frau Dr. med. Weber", "Weber"),
        ("Prof. Dr. med. Klaus Meier", "Klaus Meier"),
        ("Herr Dr. med. dent. Xaver Huber", "Xaver Huber"),
        ("Prof. Dr. rer. nat. Müller", "Müller"),
        ("Prof. Dr. Dr. h. c. Anna Sommer", "Anna Sommer"),
        ("Dr. Ing. Meyer", "Meyer"),
        ("Dr. Phil. Meyer", "Meyer"),
        ("Herrn Dr. jur. habil. Peter Lang", "Peter Lang"),
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
        "Dr. med. dent.",
        "Prof. Dr. rer. nat.",
        "Frau Dr. med. und mehr",
        "Dr. Ing.",
        "Herr Dr. Phil.",
        "Dr.Anna",  # the title needs a blank before the name
        "Andr. Meyer",  # `Dr.` inside a word is no title
        "Mr Smith",  # English salutations are not German ones
    ],
)
def test_german_non_names(text: str) -> None:
    assert names(text, "de") == []


@pytest.mark.parametrize(
    "text",
    [
        "Mo.-Fr. Sprechstunde",
        "Mo.\u2013Fr. Unterricht",
        "Mo. bis Fr. Nachmittag",
        "Di.-Fr. Beratung",
        "Mi. - Fr. Sprechstunde",
        "Do.\u2013Fr. Unterricht",
        "Mo-Fr. Sprechstunde",
        # A line as the corpus has them: opening hours of a school office.
        "Sekretariat: Mo.-Fr. Sprechstunde 8-12 Uhr, Mo. bis Fr. Nachmittag nach Vereinbarung.",
    ],
)
def test_fr_closing_a_weekday_range_is_friday_not_frau(text: str) -> None:
    """The final review's M1: ``Fr.`` after ``Mo.``/``Di.``/``Mi.``/``Do.`` and a dash or ``bis``
    is Friday: the word after it is no name."""
    assert names(text, "de") == []


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Fr. Unterberger ist da.", "Unterberger"),  # Frau, not Friday
        ("Sprechstunde Fr. Nowak", "Nowak"),
        ("Mo.: Herr Lang, Fr. Kurz", "Kurz"),
    ],
)
def test_fr_outside_a_weekday_range_is_still_frau(text: str, name: str) -> None:
    assert names(text, "de")[-1] == name


@pytest.mark.parametrize(
    ("text", "name", "language"),
    [
        ("Mr. McDonald called.", "McDonald", "en"),
        ("Mrs O'Brien wrote.", "O'Brien", "en"),
        ("Mrs O\u2019Brien wrote.", "O\u2019Brien", "en"),
        ("Frau Nowak-Šimić unterrichtet.", "Nowak-Šimić", "de"),
        ("Herr Šimić unterrichtet.", "Šimić", "de"),
        ("Frau Łukasiewicz kommt.", "Łukasiewicz", "de"),
        ("Herr Dell'Orto kommt.", "Dell'Orto", "de"),
        ("Herr MacArthur Smith", "MacArthur Smith", "de"),
        ("Frau Ørsted", "Ørsted", "de"),
    ],
)
def test_a_name_takes_any_letter_internal_capitals_and_apostrophes(
    text: str, name: str, language: str
) -> None:
    """The final review's M2: a name part is any letter after an upper-case one, with internal
    capitals and an apostrophe; the match never ends inside a word."""
    assert names(text, language) == [name]


@pytest.mark.parametrize(
    "text",
    [
        "Herr MÜLLER",  # all capitals: no name part
        "Frau Müller2",  # a name never ends inside a word
        "Herr Mü_ller",
        "Herr ABC-Schulz",
        "Frau 'Anna",
    ],
)
def test_a_name_never_ends_inside_a_word(text: str) -> None:
    assert names(text, "de") == []


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Mr. Smith's car", "Smith"),
        ("Dr. Watson\u2019s notes", "Watson"),
        ("Mrs Jones' house", "Jones"),
    ],
)
def test_a_possessive_ends_the_name(text: str, name: str) -> None:
    """A possessive ``'s`` or a trailing apostrophe is no part of the word: the name before it is
    found, as before the end assertion."""
    assert names(text, "en") == [name]


def test_an_apostrophe_inside_a_word_is_no_name_end() -> None:
    assert names("Herr Dell'orto", "de") == []  # not "Dell"


def test_a_second_word_that_is_no_name_is_left_out() -> None:
    assert names("Herr Müller sagt", "de") == ["Müller"]
    assert names("Herr Müller Schmidt2 sagt", "de") == ["Müller"]


def test_a_hyphenated_part_is_no_title_either() -> None:
    assert names("Dr. Meier-Dr. Schulz", "de") == ["Meier", "Schulz"]  # not "Meier-Dr"


@pytest.mark.parametrize(
    "text",
    [
        "Dr. " * 8000 + "x",
        "Dr. " * 16000,
        "Frau " + "Dr. med. " * 4000,
        "Prof. Dr. med. " * 4000 + "x",
        "Herr A" + "a" * 32000 + "1",  # a long word that is no name at its end
        "Herr " + "AbC" * 10000 + "1",
        "Frau " + "O'" * 10000 + "x",
        "Mo.-Fr. " * 8000,
    ],
)
def test_a_long_title_chain_takes_linear_time(text: str) -> None:
    """Page text is untrusted: a chain of titles with no name must not backtrack quadratically
    (32 KB of ``Dr. `` took 4 s with an open-ended chain)."""
    started = time.perf_counter()
    found = names(text, "de")

    assert time.perf_counter() - started < 0.1
    assert found == []


@pytest.mark.parametrize(
    "text",
    ["Frau Ab-" * 8000 + "1", "Frau " + "Ab-" * 10000 + "1", "Herr " + "McD" * 10000 + "1"],
)
def test_long_name_like_text_takes_linear_time(text: str) -> None:
    started = time.perf_counter()
    names(text, "de")

    assert time.perf_counter() - started < 0.1


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


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Müller", True),
        ("Zoë", True),
        ("Ørsted", True),
        ("Herr Иванов", True),  # one Latin letter keeps the span
        ("2024", True),  # no letter at all: not ours to judge
        ("النطاق مُخصص", False),  # Arabic letters and a mark
        ("использования в", False),
        ("Σωκράτης", False),
        ("文档示例", False),
        ("«Петров»", False),
    ],
)
def test_a_span_without_latin_letters_is_dropped(text: str, kept: bool) -> None:
    from text_extraction.pii.recognizers import has_latin_letter_or_none

    assert has_latin_letter_or_none(text) is kept
