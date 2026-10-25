import pytest

from text_extraction.media.captions import Cue
from text_extraction.media.transcript import Chapter, build_transcript


def cue(start: float, text: str, length: float = 1.0) -> Cue:
    return Cue(start=start, end=start + length, text=text)


def test_paragraph_breaks() -> None:
    cues = [cue(0, "Eins."), cue(1, "Zwei."), cue(4, "Drei nach der Pause."), cue(5, "Vier.")]

    assert build_transcript(cues, None, [], "markdown") == (
        "Eins. Zwei.\n\nDrei nach der Pause. Vier."
    )


def test_a_gap_of_exactly_two_seconds_breaks() -> None:
    cues = [cue(0, "Eins"), cue(2, "ohne Lücke"), cue(5, "Lücke von genau zwei Sekunden")]

    # 0-1 -> 2: gap 1 s; 3 -> 5: gap 2 s
    assert build_transcript(cues, None, [], "markdown") == (
        "Eins ohne Lücke\n\nLücke von genau zwei Sekunden"
    )


def test_seven_sentences_break_after_six() -> None:
    cues = [cue(i, f"Satz {n}.") for i, n in enumerate("ABCDEFG")]

    result = build_transcript(cues, None, [], "markdown")

    assert result == "Satz A. Satz B. Satz C. Satz D. Satz E. Satz F.\n\nSatz G."


def test_sentences_are_counted_inside_cues() -> None:
    cues = [
        cue(0, "Eins. Zwei! Drei?"),
        cue(1, "Vier... Fünf. Sechs."),
        cue(2, "Sieben."),
    ]

    assert build_transcript(cues, None, [], "markdown") == (
        "Eins. Zwei! Drei? Vier... Fünf. Sechs.\n\nSieben."
    )


def test_a_dot_inside_a_word_or_number_is_no_sentence_end() -> None:
    cues = [
        cue(i, "Der Rhein ist 1.233 km lang, siehe www.rhein.example bei Köln") for i in range(8)
    ]

    result = build_transcript(cues, None, [], "markdown")

    assert "\n\n" not in result


def test_cues_without_punctuation_stay_one_paragraph() -> None:
    cues = [cue(i, f"wort{i}") for i in range(20)]

    result = build_transcript(cues, None, [], "markdown")

    assert result == " ".join(f"wort{i}" for i in range(20))


def test_long_unpunctuated_text_breaks_after_120_words_at_cue_boundaries() -> None:
    words = [f"w{i}" for i in range(300)]
    cues = [cue(i, " ".join(words[i * 10 : i * 10 + 10]), 1.0) for i in range(30)]

    paragraphs = build_transcript(cues, None, [], "markdown").split("\n\n")

    assert [len(p.split()) for p in paragraphs] == [120, 120, 60]
    assert paragraphs[1].split()[0] == "w120"


def test_a_cue_is_never_cut_by_the_word_limit() -> None:
    cues = [cue(0, " ".join(["a"] * 100)), cue(1, " ".join(["b"] * 100)), cue(2, "c d")]

    paragraphs = build_transcript(cues, None, [], "markdown").split("\n\n")

    assert [len(p.split()) for p in paragraphs] == [200, 2]


def test_title_and_chapters_as_headings() -> None:
    cues = [cue(0.5, "Intro."), cue(10, "Erstes Thema."), cue(11, "Mehr."), cue(30, "Zweites.")]
    chapters = [Chapter(30, "Zweites Kapitel"), Chapter(0, "Einleitung"), Chapter(10, "Erstes")]

    result = build_transcript(cues, "Mein Video", chapters, "markdown")

    assert result == (
        "# Mein Video\n\n"
        "## Einleitung\n\nIntro.\n\n"
        "## Erstes\n\nErstes Thema. Mehr.\n\n"
        "## Zweites Kapitel\n\nZweites."
    )


def test_chapter_goes_before_the_first_cue_at_or_after_its_start() -> None:
    cues = [cue(0, "A."), cue(1, "B."), cue(9, "C.")]

    result = build_transcript(cues, None, [Chapter(5, "Mitte")], "markdown")

    assert result == "A. B.\n\n## Mitte\n\nC."


def test_chapter_without_a_cue_after_it_is_dropped() -> None:
    cues = [cue(0, "A."), cue(1, "B.")]
    chapters = [Chapter(0, "Anfang"), Chapter(100, "Nie erreicht")]

    result = build_transcript(cues, "T", chapters, "markdown")

    assert result == "# T\n\n## Anfang\n\nA. B."


def test_chapter_starts_a_new_paragraph_even_without_a_gap() -> None:
    cues = [cue(0, "A.", 5), cue(5, "B.")]

    assert build_transcript(cues, None, [Chapter(5, "Neu")], "markdown") == "A.\n\n## Neu\n\nB."


def test_no_cues_gives_the_title_or_nothing() -> None:
    assert build_transcript([], None, [Chapter(0, "X")], "markdown") == ""
    assert build_transcript([], "Titel", [Chapter(0, "X")], "markdown") == "# Titel"
    assert build_transcript([], None, [], "html") == ""


def test_text_and_html_formats() -> None:
    cues = [cue(0, "Hallo <script>alert(1)</script> Welt."), cue(5, "Nach der Pause.")]
    chapters = [Chapter(0, "Anfang")]

    text = build_transcript(cues, "Titel", chapters, "text")
    html = build_transcript(cues, "Titel", chapters, "html")

    assert "#" not in text
    assert text == "Titel\nAnfang\nHallo <script>alert(1)</script> Welt.\nNach der Pause."
    assert "<h1>Titel</h1>" in html
    assert "<h2>Anfang</h2>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.count("<p>") == 2


@pytest.mark.parametrize(
    "line",
    [
        "# Kein Titel",
        "## Kein Kapitel",
        "- kein Aufzählungspunkt",
        "+ auch nicht",
        "> kein Zitat",
        "| a | b |",
        "1. Nicht nummeriert",
        "12) auch nicht",
        "---",
        "***",
        "___",
        "~~~",
        "```python",
        "    eingerückt",
        "<b>fett</b> und <https://example.com>",
        "[Link](https://example.com) und ![Bild](x.png)",
        "*Stern* und _Strich_ und **doppelt** und __doppelt__",
        "&copy; &amp; &#35;",
        "Zeile mit \\ Backslash \\* und `Code`",
    ],
)
def test_cue_text_does_not_create_markup(line: str) -> None:
    html = build_transcript([cue(0, line)], None, [], "html")
    text = build_transcript([cue(0, line)], None, [], "text")

    body = html.removeprefix("<html><body>").removesuffix("</body></html>")
    assert body.startswith("<p>")
    assert body.count("<p>") == 1
    for tag in (
        "<h1",
        "<h2",
        "<ul",
        "<ol",
        "<li",
        "<blockquote",
        "<pre",
        "<code",
        "<em",
        "<strong",
        "<a ",
        "<img",
        "<hr",
        "<table",
        "<b>",
    ):
        assert tag not in body
    assert text == " ".join(line.split())


def test_markdown_format_escapes_so_the_text_survives_a_round_trip() -> None:
    assert build_transcript([cue(0, "# Hallo")], None, [], "markdown") == "\\# Hallo"
    assert build_transcript([cue(0, "snake_case und 2*3")], None, [], "markdown") == (
        "snake\\_case und 2\\*3"
    )


def test_title_and_chapter_titles_cannot_break_out_of_their_heading() -> None:
    cues = [cue(0, "A.")]

    html = build_transcript(cues, "Lerne C #\nzweite Zeile", [Chapter(0, "# <b>x</b> *y*")], "html")

    assert "<h1>Lerne C # zweite Zeile</h1>" in html
    assert "<h2># &lt;b&gt;x&lt;/b&gt; *y*</h2>" in html
    assert html.count("<h1>") == 1
    assert html.count("<h2>") == 1


def test_blank_title_and_chapter_titles_are_skipped() -> None:
    result = build_transcript([cue(0, "A.")], "  ", [Chapter(0, " \n ")], "markdown")

    assert result == "A."
