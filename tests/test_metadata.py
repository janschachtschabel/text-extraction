import json
import time

import pytest
from extruct.rdfa import RDFaExtractor
from extruct.w3cmicrodata import MicrodataExtractor

from text_extraction import metadata
from text_extraction.licenses import License
from text_extraction.metadata import PageMetadata, detect_language, extract_metadata

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


def _jsonld(*nodes: object) -> str:
    return "".join(
        f'<script type="application/ld+json">{json.dumps(node)}</script>' for node in nodes
    )


def test_keywords_are_deduplicated_and_capped_in_linear_time() -> None:
    words = ",".join(f"word{number}" for number in range(100_000))
    html = page(
        head=f'<meta name="keywords" content="{words}">'
        + _jsonld({"@type": "Article", "keywords": words.split(",")})
    )

    started = time.perf_counter()
    result = meta(html)
    elapsed = time.perf_counter() - started

    assert result.keywords == [f"word{number}" for number in range(50)]
    assert elapsed < 3


def test_licence_of_a_list_page_child_is_not_the_page_licence() -> None:
    listing = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "item": {
                    "@type": "LearningResource",
                    "license": "https://creativecommons.org/licenses/by/4.0/",
                    "educationalLevel": "Klasse 9",
                },
            }
        ],
    }

    result = meta(page(head=_jsonld(listing)))

    assert result.license is None
    assert result.lrmi == {}


def test_licence_of_a_source_work_or_part_or_media_is_not_the_page_licence() -> None:
    article = {
        "@type": "Article",
        "license": "https://creativecommons.org/licenses/by-sa/4.0/",
        "isBasedOn": {
            "@type": "CreativeWork",
            "license": "https://creativecommons.org/licenses/by/2.0/",
        },
        "hasPart": [{"@type": "WebPageElement", "license": "CC0"}],
        "associatedMedia": {"@type": "ImageObject", "license": "CC BY-NC 3.0"},
        "author": {"@type": "Person", "license": "CC BY-ND 4.0"},
    }

    result = meta(page(head=_jsonld(article)))

    assert [item.id for item in result.licenses] == ["CC-BY-SA-4.0"]


def test_licence_and_lrmi_of_main_entity_and_graph_members_are_the_pages() -> None:
    page_node = {
        "@type": "WebPage",
        "mainEntity": {
            "@type": "LearningResource",
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "educationalLevel": "Klasse 7",
        },
    }
    graph = {
        "@context": "https://schema.org",
        "@graph": [{"@type": "Course", "teaches": "Algebra", "license": "CC0"}],
    }

    result = meta(page(head=_jsonld(page_node, graph)))

    assert [item.id for item in result.licenses] == ["CC-BY-4.0", "CC0-1.0"]
    assert result.lrmi == {"educationalLevel": "Klasse 7", "teaches": "Algebra"}


def test_microdata_list_items_do_not_leak_into_the_page() -> None:
    html = page(
        body="""
        <div itemscope itemtype="https://schema.org/ItemList">
          <div itemprop="itemListElement" itemscope itemtype="https://schema.org/LearningResource">
            <a itemprop="license" href="https://creativecommons.org/licenses/by/4.0/">CC</a>
            <meta itemprop="educationalLevel" content="Klasse 9">
          </div>
        </div>"""
    )

    result = meta(html)

    assert result.license is None
    assert result.lrmi == {}


def test_microdata_image_licence_is_not_the_page_licence() -> None:
    html = page(
        body="""
        <div itemscope itemtype="https://schema.org/Article">
          <a itemprop="license" href="https://creativecommons.org/licenses/by/4.0/">CC</a>
          <div itemprop="image" itemscope itemtype="https://schema.org/ImageObject">
            <a itemprop="license" href="https://creativecommons.org/licenses/by-nd/4.0/">CC</a>
          </div>
        </div>"""
    )

    assert [item.id for item in meta(html).licenses] == ["CC-BY-4.0"]


def test_rel_license_in_a_figure_is_an_image_credit() -> None:
    html = page(
        body=(
            '<figure><img src="/a.png"><figcaption>Foto: '
            '<a rel="license" href="https://creativecommons.org/licenses/by-nd/2.0/">CC BY-ND</a>'
            "</figcaption></figure>"
        )
    )

    result = meta(html)

    assert result.license is not None
    assert (result.license.id, result.license.source, result.license.confidence) == (
        "CC-BY-ND-2.0",
        "rel-license",
        "medium",
    )


def test_site_footer_wins_over_early_footers() -> None:
    articles = "".join(f"<article><footer>Autor {n}</footer></article>" for n in range(30))
    html = page(body=articles + "<footer>Alle Texte unter CC BY-SA 4.0</footer>")

    result = meta(html)

    assert result.license is not None
    assert result.license.id == "CC-BY-SA-4.0"


def test_footer_anchor_without_licence_url_is_not_a_licence() -> None:
    html = page(body='<footer><a href="/tag/cc0">Schlagwort</a> Dosis CC 0,5 mg</footer>')

    assert meta(html).license is None


def test_jsonld_script_type_is_matched_case_insensitively_with_parameters() -> None:
    node = json.dumps({"@type": "Article", "headline": "Titel", "license": "CC0"})
    html = page(
        head=(
            f'<script type="Application/LD+JSON; charset=utf-8">{node}</script>'
            '<script type="application/json">{"headline": "Wrong"}</script>'
        )
    )

    result = meta(html)

    assert result.title == "Titel"
    assert result.license is not None
    assert result.license.id == "CC0-1.0"


def test_jsonld_wrapped_in_comment_markers_is_read() -> None:
    node = json.dumps({"@type": "Article", "headline": "Kommentiert"})
    html = page(head=f'<script type="application/ld+json"><!-- {node} --></script>')

    assert meta(html).title == "Kommentiert"


def test_a_big_page_is_processed_in_a_few_seconds() -> None:
    link = '<link rel="license" href="https://creativecommons.org/licenses/by/4.0/">'
    html = page(head=link, body="<p>x</p>" * 700_000 + "<footer>CC0</footer>")
    assert len(html) > 5_000_000

    started = time.perf_counter()
    result = meta(html)
    elapsed = time.perf_counter() - started

    assert result.license is not None
    assert result.license.id == "CC-BY-4.0"
    assert elapsed < 6  # about 1 s on a laptop; the bound leaves room for a slow CI runner


def test_language_detection_only_looks_at_a_bounded_prefix() -> None:
    padding = " " * 20_000
    # Fifty words, but only after a long stretch of blanks: outside the scanned prefix.
    assert detect_language(None, None, padding + GERMAN_TEXT) is None


def test_deeply_nested_page_still_yields_footer_licence_and_images() -> None:
    html = page(
        body=(
            "<div>" * 300
            + '<img src="/deep.png" alt="Deep">'
            + '<footer><a href="https://creativecommons.org/licenses/by/4.0/">CC</a></footer>'
            + "</div>" * 300
        )
    )

    result = meta(html)

    assert result.license is not None
    assert result.license.id == "CC-BY-4.0"
    assert [image.url for image in result.images] == ["https://www.example.org/deep.png"]


def test_microdata_and_jsonld_still_work_on_the_big_tree_parser() -> None:
    html = page(
        head=_jsonld({"@type": "Article", "headline": "Titel"}),
        body='<div itemscope itemtype="https://schema.org/Article">'
        '<meta itemprop="educationalLevel" content="Klasse 5"></div>',
    )

    result = meta(html)

    assert result.title == "Titel"
    assert result.lrmi == {"educationalLevel": "Klasse 5"}


def test_jsonld_with_trailing_commas_and_comments_is_read() -> None:
    lenient = """{
        // the headline
        "@type": "Article",
        "headline": "Lenient",
        "license": "CC0",
    }"""
    html = page(head=f'<script type="application/ld+json">{lenient}</script>')

    result = meta(html)

    assert result.title == "Lenient"
    assert result.license is not None
    assert result.license.id == "CC0-1.0"


def test_author_list_is_deduplicated_then_capped_at_fifty() -> None:
    names = "".join(f'<meta name="author" content="Autor {n % 120}">' for n in range(300))

    authors = meta(page(head=names)).authors

    assert authors == [f"Autor {n}" for n in range(50)]


def test_deeply_nested_itemscopes_do_not_blow_up_microdata_extraction() -> None:
    nested = (
        '<div itemscope itemtype="https://schema.org/CreativeWork"><div itemprop="x" itemscope>'
        * 300
        + "</div></div>" * 300
    )
    link = '<link rel="license" href="https://creativecommons.org/licenses/by/4.0/">'

    started = time.perf_counter()
    result = meta(page(head=link, body=nested))
    elapsed = time.perf_counter() - started

    assert result.license is not None  # the other readers are not affected
    assert elapsed < 2


def test_ordinary_nested_microdata_is_still_read() -> None:
    html = page(
        body="""
        <div itemscope itemtype="https://schema.org/LearningResource">
          <meta itemprop="teaches" content="Algebra">
          <div itemprop="author" itemscope itemtype="https://schema.org/Person">
            <span itemprop="name">Erika Muster</span>
            <div itemprop="affiliation" itemscope itemtype="https://schema.org/Organization">
              <span itemprop="name">Schule</span>
            </div>
          </div>
          <a itemprop="license" href="https://creativecommons.org/licenses/by-sa/4.0/">CC</a>
        </div>"""
    )

    result = meta(html)

    assert result.lrmi == {"teaches": "Algebra"}
    assert [item.id for item in result.licenses] == ["CC-BY-SA-4.0"]


def test_lenient_jsonld_fallback_is_skipped_for_huge_scripts() -> None:
    padding = "a" * 1_500_000
    lenient = f'{{"@type": "Article", "headline": "Huge", "pad": "{padding}",}}'
    small = '{"@type": "Article", "headline": "Small",}'

    assert meta(page(head=_jsonld_raw(lenient))).title is None
    assert meta(page(head=_jsonld_raw(small))).title == "Small"


def _jsonld_raw(text: str) -> str:
    return f'<script type="application/ld+json">{text}</script>'
