import json
from pathlib import Path

import pytest

from text_extraction.media.captions import CaptionError, Cue, parse_captions

CAPTIONS = Path(__file__).parent / "media" / "captions"


def load(name: str) -> bytes:
    return (CAPTIONS / name).read_bytes()


def texts(cues: list[Cue]) -> list[str]:
    return [cue.text for cue in cues]


def test_rolling_duplicates_removed() -> None:
    cues = parse_captions(load("youtube_auto.vtt"), "vtt")

    assert texts(cues) == [
        "[Musik]",
        "hallo und herzlich willkommen zu",
        "unserem Video über die Größe der",
        "Sonne >> Sie ist riesig & heiß",
        "(Applaus)",
    ]
    spoken = " ".join(texts(cues))
    for phrase in ("hallo und herzlich", "Video über die Größe", "Sonne"):
        assert spoken.count(phrase) == 1


def test_rolling_duplicates_keep_the_time_of_the_spoken_line() -> None:
    cues = parse_captions(load("youtube_auto.vtt"), "vtt")

    hello = cues[1]
    assert hello.start == pytest.approx(1.93)
    # The repeating cue (4.710 --> 4.720) lengthens the cue it repeats.
    assert hello.end == pytest.approx(4.72)
    assert cues[0].start == pytest.approx(0.0)
    assert cues[0].end == pytest.approx(1.93)


def test_a_line_repeated_by_a_later_cue_after_new_speech_is_kept() -> None:
    body = (
        b"WEBVTT\n\n"
        b"00:00:00.000 --> 00:00:01.000\nNein.\n\n"
        b"00:00:01.000 --> 00:00:02.000\nDoch.\n\n"
        b"00:00:02.000 --> 00:00:03.000\nNein.\n"
    )

    assert texts(parse_captions(body, "vtt")) == ["Nein.", "Doch.", "Nein."]


def test_vtt_blocks_and_spans_dropped() -> None:
    cues = parse_captions(load("manual.vtt"), "vtt")

    assert texts(cues) == [
        "Willkommen zur Vorlesung über Flüsse.",
        'Der Rhein ist 1.233 km lang und fließt durch "sechs" Länder.',
        "Rhein & Donau, 3 < 5 und 7 > 2.",
    ]
    assert (cues[0].start, cues[0].end) == (0.5, 3.0)
    assert cues[2].start == pytest.approx(67.25)
    assert cues[2].end == pytest.approx(69.0)


def test_vtt_cue_settings_and_identifiers() -> None:
    cues = parse_captions(load("zdf.vtt"), "vtt")

    assert texts(cues) == [
        "Heute beginnt die Woche mit viel Sonnenschein.",
        "Im Norden bleibt es aber wolkig.",
        "Weitere Informationen finden Sie im Videotext.",
    ]
    assert cues[0].start == pytest.approx(3.12)
    assert cues[0].end == pytest.approx(6.48)


def test_vtt_times_without_hours() -> None:
    cues = parse_captions(b"WEBVTT\n\n01:02.500 --> 01:04.000\nHallo\n", "vtt")

    assert [(c.start, c.end, c.text) for c in cues] == [(62.5, 64.0, "Hallo")]


def test_vtt_bom_and_crlf() -> None:
    body = "\ufeffWEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.000\r\nGrüße\r\n".encode()

    assert texts(parse_captions(body, "vtt")) == ["Grüße"]


def test_srt_latin1() -> None:
    cues = parse_captions(load("latin1.srt"), "srt")

    assert texts(cues) == [
        "Die Größe des Gebäudes überrascht",
        "Wir müssen früh aufstehen, sagte die Bäuerin.",
        "Äpfel, Öl und Brötchen für alle.",
    ]
    assert (cues[1].start, cues[1].end) == (4.0, 6.25)


def test_srt_declared_charset_is_used_first() -> None:
    body = "1\n00:00:01,000 --> 00:00:02,000\nPreis 5 €\n".encode("iso-8859-15")

    assert texts(parse_captions(body, "srt", charset="ISO-8859-15")) == ["Preis 5 €"]


def test_unknown_charset_falls_back_to_the_bytes() -> None:
    body = "1\n00:00:01,000 --> 00:00:02,000\nGrüße\n".encode()

    assert texts(parse_captions(body, "srt", charset="no-such-charset")) == ["Grüße"]


def test_srt_markup_and_ass_tags_are_removed() -> None:
    body = b"1\n00:00:01,000 --> 00:00:02,000\n{\\an8}<font color=red>Oben</font> <b>fett</b>\n"

    assert texts(parse_captions(body, "srt")) == ["Oben fett"]


def test_ttml_namespaces_and_br() -> None:
    cues = parse_captions(load("ard.ttml"), "ttml")

    assert texts(cues) == [
        "Guten Abend, meine Damen und Herren. Willkommen zur Sendung.",
        "Heute: Forschung & Lehre",
        "Verschachtelt und über mehrere Zeilen.",
    ]
    assert (cues[0].start, cues[0].end) == (1.2, 4.04)
    assert (cues[1].start, cues[1].end) == (4.5, 7.0)  # begin and dur


def test_ttml_default_namespace_and_clock_time_without_fraction() -> None:
    body = (
        b'<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
        b'<p begin="0:00:02" end="0:00:03">Zeile<br/>zwei</p>'
        b'<p begin="1.5s" end="2500ms">kurz</p>'
        b"</div></body></tt>"
    )

    cues = parse_captions(body, "ttml")

    assert [(c.start, c.end, c.text) for c in cues] == [
        (2.0, 3.0, "Zeile zwei"),
        (1.5, 2.5, "kurz"),
    ]


def test_ttml_declared_encoding_is_not_applied_twice() -> None:
    body = (
        '<?xml version="1.0" encoding="iso-8859-1"?><tt xmlns="http://www.w3.org/ns/ttml">'
        '<body><div><p begin="0s" end="1s">Grüße</p></div></body></tt>'
    ).encode()

    assert texts(parse_captions(body, "ttml")) == ["Grüße"]


def test_json3_segments() -> None:
    cues = parse_captions(load("youtube.json3"), "json3")

    assert texts(cues) == [
        "hallo und willkommen",
        "zu unserem Video",
        "über die Größe der Sonne",
    ]
    assert cues[0].start == pytest.approx(0.16)
    assert cues[0].end == pytest.approx(4.71)


def test_empty_track_gives_no_cues() -> None:
    assert parse_captions(load("empty.vtt"), "vtt") == []
    assert parse_captions(b"", "vtt") == []
    assert parse_captions(b"  \n", "srt") == []
    assert parse_captions(b"", "ttml") == []
    assert parse_captions(b"", "json3") == []
    assert parse_captions(b'{"events": []}', "json3") == []
    assert parse_captions(b"1\nnot a timing line\ntext\n", "srt") == []
    tt = b'<tt xmlns="http://www.w3.org/ns/ttml"><body><div/></body></tt>'
    assert parse_captions(tt, "ttml") == []


def test_entities_and_tags_removed() -> None:
    body = (
        b"WEBVTT\n\n"
        b"00:00:01.000 --> 00:00:02.000\n"
        b"<v Anna><i>Tom</i> &amp; <b>Jerry</b>&nbsp;&#39;heute&#39; <00:00:01.500>&lt;ok&gt;</v>\n"
        b"<ruby>\xe6\xbc\xa2<rt>kan</rt></ruby> <lang en>hello</lang>   <u>x</u>\n"
    )

    assert texts(parse_captions(body, "vtt")) == ["Tom & Jerry 'heute' <ok> 漢kan hello x"]


def test_cue_that_is_only_markup_is_dropped() -> None:
    body = (
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c> </c>\n&nbsp;\n\n"
        b"00:00:02.000 --> 00:00:03.000\nSo.\n"
    )

    assert texts(parse_captions(body, "vtt")) == ["So."]


def test_sound_labels_are_kept() -> None:
    body = (
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n[Musik]\n\n"
        b"00:00:03.000 --> 00:00:04.000\n(Applaus)\n"
    )

    assert texts(parse_captions(body, "vtt")) == ["[Musik]", "(Applaus)"]


def test_ttml_external_entity_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    body = (
        f'<?xml version="1.0"?><!DOCTYPE tt [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
        '<p begin="0s" end="1s">&xxe;</p></div></body></tt>'
    ).encode()

    with pytest.raises(CaptionError):
        parse_captions(body, "ttml")


def test_ttml_entity_expansion_refused() -> None:
    body = (
        b'<?xml version="1.0"?><!DOCTYPE tt [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        b'<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p begin="0s" end="1s">&b;</p></div>'
        b"</body></tt>"
    )

    with pytest.raises(CaptionError):
        parse_captions(body, "ttml")


@pytest.mark.parametrize(
    ("body", "fmt"),
    [
        (b"<tt><body><p begin='0s'>unclosed", "ttml"),
        (b"<html><body>Not found</body></html>", "ttml"),
        (b"{not json", "json3"),
        (b'["a", "list"]', "json3"),
        (b'{"events": "nope"}', "json3"),
        (b"<html><body>Not found</body></html>", "vtt"),
    ],
)
def test_malformed_track_raises(body: bytes, fmt: str) -> None:
    with pytest.raises(CaptionError):
        parse_captions(body, fmt)  # type: ignore[arg-type]


def test_body_over_the_cap_is_refused() -> None:
    body = b"WEBVTT\n\n" + b"x" * 100

    with pytest.raises(CaptionError, match="too large"):
        parse_captions(body, "vtt", max_bytes=50)
    assert parse_captions(body, "vtt", max_bytes=len(body)) == []


def test_json3_odd_events_are_tolerated() -> None:
    body = json.dumps(
        {
            "events": [
                "not an object",
                {"tStartMs": "bad", "segs": [{"utf8": "x"}]},
                {"tStartMs": 1000, "segs": [{"utf8": "ok"}, {"nope": 1}, "str"]},
            ]
        }
    ).encode()

    cues = parse_captions(body, "json3")

    assert [(c.start, c.text) for c in cues] == [(1.0, "ok")]
