import json

import pytest
from extruct.rdfa import RDFaExtractor
from extruct.w3cmicrodata import MicrodataExtractor

from text_extraction import metadata
from text_extraction.metadata import (
    License,
    PageMetadata,
    detect_language,
    extract_metadata,
    normalize_license,
)

URL = "https://www.example.org/de/lesson"

GERMAN_TEXT = (
    "Die Photosynthese ist ein biochemischer Vorgang, bei dem Pflanzen, Algen und manche Bakterien "
    "mit Hilfe von Licht aus Kohlenstoffdioxid und Wasser energiereiche Stoffe herstellen. "
    "Dabei entsteht als Nebenprodukt Sauerstoff, den fast alle Lebewesen zum Atmen brauchen. "
    "Im Unterricht untersuchen die Schülerinnen und Schüler, wie Blätter aufgebaut sind und "
    "welche Rolle das Chlorophyll bei diesem Vorgang spielt."
)
ENGLISH_TEXT = (
    "Photosynthesis is a process used by plants, algae and some bacteria to convert light energy "
    "into chemical energy that can later be released to fuel the organism's activities. "
    "During the process, oxygen is released as a by-product, which nearly all living things need "
    "to breathe. In class, students examine how leaves are built and which role chlorophyll "
    "plays in this process."
)


def page(head: str = "", body: str = "", lang: str = "") -> str:
    attribute = f' lang="{lang}"' if lang else ""
    return f"<html{attribute}><head>{head}</head><body>{body}</body></html>"


def meta(html: str, headers: dict[str, str] | None = None) -> PageMetadata:
    return extract_metadata(html, URL, headers or {})


def test_rel_license_cc_by_sa() -> None:
    result = meta(
        page(
            head='<link rel="license" href="https://creativecommons.org/licenses/by-sa/4.0/">',
            body='<a rel="license noopener" href="/lizenz">Eigene Lizenz</a>',
        )
    )

    assert result.license == License(
        id="CC-BY-SA-4.0",
        url="https://creativecommons.org/licenses/by-sa/4.0/",
        label=None,
        source="rel-license",
        confidence="high",
    )
    # The unrecognised licence page is kept, behind the recognised one.
    assert [(item.id, item.url) for item in result.licenses] == [
        ("CC-BY-SA-4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
        (None, "https://www.example.org/lizenz"),
    ]
    assert result.licenses[1].label == "Eigene Lizenz"


def test_rel_license_in_link_header() -> None:
    result = meta(
        page(),
        {"link": '<https://creativecommons.org/licenses/by/4.0/>; rel="license"; title="x"'},
    )

    assert result.license is not None
    assert (result.license.id, result.license.source) == ("CC-BY-4.0", "rel-license")


def test_jsonld_license_and_lrmi() -> None:
    data = {
        "@context": "https://schema.org",
        "@type": "LearningResource",
        "name": "Photosynthese",
        "license": "https://creativecommons.org/licenses/by-nc/4.0/deed.de",
        "educationalLevel": "Sekundarstufe I",
        "learningResourceType": ["Arbeitsblatt", "Übung"],
        "audience": {"@type": "Audience", "audienceType": "Lernende"},
        "typicalAgeRange": "12-14",
        "teaches": "Biologie",
        "educationalAlignment": {
            "@type": "AlignmentObject",
            "alignmentType": "educationalSubject",
            "targetName": "Biologie",
        },
        "inLanguage": "de",
        "isAccessibleForFree": True,
        "timeRequired": "PT45M",
        "interactivityType": "active",
        "author": {"@type": "Person", "name": "Erika Muster"},
    }
    html = page(head=f'<script type="application/ld+json">{json.dumps(data)}</script>')

    result = meta(html)

    assert result.license == License(
        id="CC-BY-NC-4.0",
        url="https://creativecommons.org/licenses/by-nc/4.0/deed.de",
        label=None,
        source="json-ld",
        confidence="high",
    )
    assert result.lrmi == {
        "educationalLevel": "Sekundarstufe I",
        "learningResourceType": ["Arbeitsblatt", "Übung"],
        "audience": {"@type": "Audience", "audienceType": "Lernende"},
        "typicalAgeRange": "12-14",
        "teaches": "Biologie",
        "educationalAlignment": data["educationalAlignment"],
        "inLanguage": "de",
        "isAccessibleForFree": True,
        "timeRequired": "PT45M",
        "interactivityType": "active",
    }
    assert result.authors == ["Erika Muster"]
    assert result.title == "Photosynthese"


def test_jsonld_graph_and_license_object() -> None:
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "Portal"},
            {
                "@type": "Article",
                "headline": "Bruchrechnung",
                "license": {"@id": "http://creativecommons.org/licenses/by/3.0/de/"},
                "datePublished": "2024-05-01",
                "dateModified": "2024-06-02",
            },
        ],
    }

    result = meta(page(head=f'<script type="application/ld+json">{json.dumps(data)}</script>'))

    assert result.license is not None
    assert (result.license.id, result.license.source) == ("CC-BY-3.0", "json-ld")
    assert (result.title, result.published, result.modified) == (
        "Bruchrechnung",
        "2024-05-01",
        "2024-06-02",
    )


def test_microdata_license_and_lrmi() -> None:
    html = page(
        body="""
        <div itemscope itemtype="https://schema.org/LearningResource">
          <h1 itemprop="name">Bruchrechnung</h1>
          <a itemprop="license" href="https://creativecommons.org/publicdomain/zero/1.0/">CC0</a>
          <meta itemprop="educationalLevel" content="Klasse 6">
        </div>"""
    )

    result = meta(html)

    assert result.license is not None
    assert (result.license.id, result.license.source, result.license.confidence) == (
        "CC0-1.0",
        "microdata",
        "high",
    )
    assert result.lrmi == {"educationalLevel": "Klasse 6"}


def test_rdfa_license() -> None:
    html = """<html prefix="cc: http://creativecommons.org/ns#"><head></head><body>
      <div about="" ><a rel="cc:license" href="https://creativecommons.org/licenses/by-nd/4.0/">
      CC BY-ND</a></div></body></html>"""

    result = meta(html)

    assert result.license is not None
    assert (result.license.id, result.license.source) == ("CC-BY-ND-4.0", "rdfa")


def test_meta_license_is_medium() -> None:
    result = meta(
        page(
            head=(
                '<meta name="DC.rights" content="CC BY-SA 3.0 DE">'
                '<meta name="dcterms.license" '
                'content="https://creativecommons.org/licenses/by/2.5/">'
            )
        )
    )

    assert [(item.id, item.source, item.confidence) for item in result.licenses] == [
        ("CC-BY-SA-3.0", "meta", "medium"),
        ("CC-BY-2.5", "meta", "medium"),
    ]
    assert result.license is not None
    assert result.license.id == "CC-BY-SA-3.0"  # same confidence: the first one


def test_opengraph_license_is_medium() -> None:
    html = page(head='<meta property="og:license" content="CC0 1.0 Universal">')

    result = meta(html)

    assert result.license is not None
    assert (result.license.id, result.license.source, result.license.confidence) == (
        "CC0-1.0",
        "opengraph",
        "medium",
    )


def test_text_license_low_confidence() -> None:
    html = page(
        body=(
            "<main><p>Dieser Text steht unter CC BY 4.0, sagt der Autor.</p></main>"
            "<footer>Inhalte unter "
            '<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>'
            " International. Namensnennung - Nicht kommerziell 3.0 Deutschland.</footer>"
        )
    )

    result = meta(html)

    assert [(item.id, item.source, item.confidence) for item in result.licenses] == [
        ("CC-BY-SA-4.0", "text", "low"),
        ("CC-BY-NC-3.0", "text", "low"),
    ]
    assert result.license == result.licenses[0]


def test_text_outside_footer_is_not_a_licence() -> None:
    assert meta(page(body="<main><p>Lizenz: CC BY 4.0</p></main>")).license is None


def test_higher_confidence_wins_over_earlier_candidate() -> None:
    html = page(
        head='<meta name="license" content="CC BY 2.0">',
        body=(
            '<footer>CC BY-NC 4.0</footer><a rel="license" '
            'href="https://creativecommons.org/licenses/by-sa/4.0/">x</a>'
        ),
    )

    result = meta(html)

    assert [(item.id, item.confidence) for item in result.licenses] == [
        ("CC-BY-SA-4.0", "high"),
        ("CC-BY-2.0", "medium"),
        ("CC-BY-NC-4.0", "low"),
    ]
    assert result.license == result.licenses[0]


def test_same_licence_from_two_sources_is_listed_once_with_the_best_confidence() -> None:
    html = page(
        head='<meta name="license" content="https://creativecommons.org/licenses/by/4.0/">',
        body='<link rel="license" href="http://creativecommons.org/licenses/by/4.0/deed.en">',
    )

    result = meta(html)

    assert [(item.id, item.source) for item in result.licenses] == [("CC-BY-4.0", "rel-license")]


def test_licence_without_known_id_is_kept_and_ranked_behind_a_known_one() -> None:
    html = page(
        head='<meta name="license" content="Alle Rechte vorbehalten">',
        body='<a rel="license" href="/terms">Terms</a><a rel="license" href="/cc.html">CC</a>',
    )

    result = meta(html)

    assert result.license is not None
    assert result.license.id is None
    assert result.license.url == "https://www.example.org/terms"  # first high one
    assert [item.id for item in result.licenses] == [None, None, None]
    assert result.licenses[2].label == "Alle Rechte vorbehalten"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        ("http://creativecommons.org/licenses/by-sa/3.0/", "CC-BY-SA-3.0"),
        ("https://creativecommons.org/licenses/by-nc-nd/2.0/deed.de", "CC-BY-NC-ND-2.0"),
        ("http://creativecommons.org/licenses/by-nc-sa/3.0/de/legalcode", "CC-BY-NC-SA-3.0"),
        ("https://www.creativecommons.org/licenses/by-nc/2.5/ch/", "CC-BY-NC-2.5"),
        ("https://creativecommons.org/licenses/by-sa/1.0", "CC-BY-SA-1.0"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        ("http://creativecommons.org/publicdomain/zero/1.0/deed.en", "CC0-1.0"),
        ("https://creativecommons.org/publicdomain/mark/1.0/", "PDM-1.0"),
        ("CC BY-SA 4.0", "CC-BY-SA-4.0"),
        ("cc-by-nc-nd-3.0", "CC-BY-NC-ND-3.0"),
        ("CC BY 4.0 International", "CC-BY-4.0"),
        ("CC-BY-SA-4.0", "CC-BY-SA-4.0"),
        ("CC BY-NC-SA 4.0", "CC-BY-NC-SA-4.0"),
        ("CC0", "CC0-1.0"),
        ("CC0 1.0 Universal", "CC0-1.0"),
        ("CC0-1.0", "CC0-1.0"),
        ("Public Domain Mark 1.0", "PDM-1.0"),
        ("PDM-1.0", "PDM-1.0"),
        ("Creative Commons Attribution 4.0 International", "CC-BY-4.0"),
        ("Creative Commons Attribution-ShareAlike 3.0 Unported", "CC-BY-SA-3.0"),
        (
            "Creative Commons Attribution-NonCommercial-NoDerivatives 4.0",
            "CC-BY-NC-ND-4.0",
        ),
        ("Namensnennung - Weitergabe unter gleichen Bedingungen 4.0", "CC-BY-SA-4.0"),
        ("Namensnennung 4.0 International", "CC-BY-4.0"),
        (
            "Namensnennung - Nicht kommerziell - Keine Bearbeitungen 3.0 Deutschland",
            "CC-BY-NC-ND-3.0",
        ),
        (
            "Namensnennung-Nicht kommerziell-Weitergabe unter gleichen Bedingungen 2.0",
            "CC-BY-NC-SA-2.0",
        ),
        ("  cc by 4.0  ", "CC-BY-4.0"),
        ("All rights reserved", None),
        ("", None),
        ("CC BY-SA", None),  # no version: not an SPDX id
        ("https://creativecommons.org/licenses/by/", None),
        ("https://creativecommons.org/licenses/by-sa/9.9/", None),
        ("https://example.org/licenses/by/4.0/", None),
        ("CC BY-SA-ND 4.0", None),  # SA and ND exclude each other
        ("Attribution 2024", None),
    ],
)
def test_normalize_license_variants(value: str, expected: str | None) -> None:
    assert normalize_license(value) == expected


def test_opengraph_and_canonical() -> None:
    html = page(
        head="""
        <title>Fallback title</title>
        <link rel="canonical" href="/de/lesson?ref=1">
        <meta property="og:title" content="Photosynthese">
        <meta property="og:description" content="Wie Pflanzen Licht nutzen">
        <meta property="og:site_name" content="Lernportal">
        <meta property="og:locale" content="de_DE">
        <meta property="article:published_time" content="2024-05-01T08:00:00+02:00">
        <meta property="article:modified_time" content="2024-06-02T09:30:00+02:00">
        <meta property="article:tag" content="Biologie">
        <meta name="author" content="Erika Muster">
        <meta name="keywords" content="Pflanzen, Licht ,, Pflanzen">
        """
    )

    result = meta(html)

    assert result.title == "Photosynthese"
    assert result.description == "Wie Pflanzen Licht nutzen"
    assert result.site_name == "Lernportal"
    assert result.canonical_url == "https://www.example.org/de/lesson?ref=1"
    assert result.language == "de"
    assert result.published == "2024-05-01T08:00:00+02:00"
    assert result.modified == "2024-06-02T09:30:00+02:00"
    assert result.authors == ["Erika Muster"]
    assert result.keywords == ["Pflanzen", "Licht", "Biologie"]


def test_plain_page_falls_back_to_title_and_meta_description() -> None:
    result = meta(
        page(
            head='<title> Brüche \n kürzen </title><meta name="description" content="Kurz">',
            lang="de-DE",
        )
    )

    assert (result.title, result.description, result.language) == ("Brüche kürzen", "Kurz", "de")
    assert result.canonical_url is None
    assert result.license is None
    assert result.lrmi == {}
    assert result.images == []


def test_javascript_canonical_is_ignored() -> None:
    assert (
        meta(page(head='<link rel="canonical" href="javascript:alert(1)">')).canonical_url is None
    )


def test_images_with_alt_capped_at_20() -> None:
    images = "".join(f'<img src="/img/{number}.png" alt="Bild {number}">' for number in range(30))
    html = page(
        head=(
            '<meta property="og:image" content="/cover.jpg">'
            '<meta property="og:image:alt" content="Titelbild">'
        ),
        body=(
            '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="pixel">'
            '<img src="/cover.jpg" alt="Duplicate">'
            '<img data-src="/lazy.png">'
            '<img alt="no source">'
        )
        + images,
    )

    result = meta(html)

    assert len(result.images) == 20
    assert result.images[0].model_dump() == {
        "url": "https://www.example.org/cover.jpg",
        "alt": "Titelbild",
    }
    assert result.images[1].model_dump() == {"url": "https://www.example.org/lazy.png", "alt": ""}
    assert result.images[2].model_dump() == {
        "url": "https://www.example.org/img/0.png",
        "alt": "Bild 0",
    }
    assert all(not image.url.startswith("data:") for image in result.images)


def test_images_resolve_against_base_href() -> None:
    html = page(head='<base href="https://cdn.example.net/x/">', body='<img src="a.png" alt="A">')

    assert meta(html).images[0].url == "https://cdn.example.net/x/a.png"


def test_language_from_html_then_header_then_detection() -> None:
    assert detect_language("de-DE", "en", ENGLISH_TEXT) == "de"
    assert detect_language("  EN_gb ", None, GERMAN_TEXT) == "en"
    assert detect_language(None, "fr-CA, en;q=0.5", GERMAN_TEXT) == "fr"
    assert detect_language("", "de-DE, en", "") == "de"
    assert detect_language(None, None, GERMAN_TEXT) == "de"
    assert detect_language(None, None, ENGLISH_TEXT) == "en"
    assert detect_language("x-klingon", None, GERMAN_TEXT) == "de"
    assert detect_language("*", "*", GERMAN_TEXT) == "de"


def test_language_detection_needs_fifty_words() -> None:
    assert detect_language(None, None, " ".join(GERMAN_TEXT.split()[:49])) is None
    assert detect_language(None, None, " ".join(GERMAN_TEXT.split()[:50])) == "de"
    assert detect_language(None, None, "") is None


def test_header_language_reaches_the_metadata() -> None:
    result = meta(page(), {"content-language": "de-AT, en"})

    assert result.language == "de"


def test_broken_jsonld_is_ignored() -> None:
    good = {"@type": "Article", "license": "https://creativecommons.org/licenses/by/4.0/"}
    html = page(
        head=(
            '<script type="application/ld+json">{"license": </script>'
            '<script type="application/ld+json"></script>'
            '<script type="application/ld+json">[1, "x", null]</script>'
            f'<script type="application/ld+json">{json.dumps(good)}</script>'
            '<meta property="og:title" content="Still here">'
        )
    )

    result = meta(html)

    assert result.title == "Still here"
    assert result.license is not None
    assert result.license.id == "CC-BY-4.0"


def test_broken_rdfa_and_microdata_do_not_stop_the_other_syntaxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> list[object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(RDFaExtractor, "extract_items", explode)
    monkeypatch.setattr(MicrodataExtractor, "extract_items", explode)
    html = page(
        head=(
            '<link rel="license" href="https://creativecommons.org/licenses/by/4.0/">'
            '<link rel="cc:license" href="https://creativecommons.org/licenses/by-sa/4.0/">'
        ),
        body='<span itemprop="license" content="CC0"></span>',
    )

    result = meta(html)

    assert result.license is not None
    assert result.license.id == "CC-BY-4.0"


def test_unparseable_input_yields_empty_metadata() -> None:
    for html in ("", "   ", "plain text, no markup", "<<<>>>"):
        result = meta(html)
        assert result.license is None
        assert result.images == []


def test_deeply_nested_markup_does_not_crash() -> None:
    html = page(body="<div>" * 5000 + "text" + "</div>" * 5000)

    assert meta(html).title is None


def test_rdfa_runs_only_for_pages_that_use_it_and_are_not_huge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def spy(self: RDFaExtractor, *_args: object, **_kwargs: object) -> list[object]:
        calls.append("rdfa")
        return []

    monkeypatch.setattr(RDFaExtractor, "extract_items", spy)
    opengraph_only = page(head='<meta property="og:title" content="T">')
    with_rdfa = page(
        body='<a rel="cc:license" href="https://creativecommons.org/licenses/by/4.0/">'
    )
    monkeypatch.setattr(metadata, "_RDFA_MAX_CHARS", 1000)
    huge = page(body='<p vocab="https://schema.org/"></p>' + "<p>x</p>" * 500)

    meta(opengraph_only)
    assert calls == []
    meta(with_rdfa)
    assert calls == ["rdfa"]
    meta(huge)
    assert calls == ["rdfa"]


def test_lrmi_values_are_clipped() -> None:
    data = {"educationalLevel": "x" * 5000, "teaches": ["a"] * 200}
    html = page(head=f'<script type="application/ld+json">{json.dumps(data)}</script>')

    lrmi = meta(html).lrmi

    assert len(lrmi["educationalLevel"]) == 1000
    assert len(lrmi["teaches"]) == 50
