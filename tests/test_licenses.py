import pytest

from text_extraction.licenses import (
    License,
    LicenseCollector,
    license_from_url,
    link_header_licenses,
    normalize_license,
)

BASE = "https://www.example.org/de/page"


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
        ("Namensnennung-NichtKommerziell-KeineBearbeitung 4.0", "CC-BY-NC-ND-4.0"),
        ("Namensnennung-WeitergabeUnterGleichenBedingungen 4.0", "CC-BY-SA-4.0"),
        ("Creative Commons Attribution-NoDerivs 3.0 Unported", "CC-BY-ND-3.0"),
        ("Attribution-NonCommercial-No Derivative Works 2.0", "CC-BY-NC-ND-2.0"),
        ("Creative Commons Zero v1.0 Universal", "CC0-1.0"),
        ("Creative Commons BY-SA 4.0", "CC-BY-SA-4.0"),
        ("Creative Commons BY 4.0 International", "CC-BY-4.0"),
        ("https://creativecommons.org/licenses/by-nd-nc/1.0/", "CC-BY-NC-ND-1.0"),
        ("https://creativecommons.org/licenses/by/2.1/jp/", "CC-BY-2.1"),
        ("CC BY 2.1 ES", "CC-BY-2.1"),
        ("https://spdx.org/licenses/CC-BY-4.0.html", "CC-BY-4.0"),
        ("CC 0,5 mg per dose", None),
        ("CC0.5", None),
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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://creativecommons.org/licenses/by-sa/4.0/", "CC-BY-SA-4.0"),
        ("http://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        ("https://spdx.org/licenses/CC-BY-4.0.html", "CC-BY-4.0"),
        ("https://example.org/tag/cc0", None),
        ("https://example.org/pages/cc-by-4.0", None),
        ("CC BY 4.0", None),
    ],
)
def test_license_from_url_reads_only_urls(url: str, expected: str | None) -> None:
    assert license_from_url(url) == expected


def test_link_header_licenses() -> None:
    header = (
        '<https://creativecommons.org/licenses/by/4.0/>; rel="license"; title="x", '
        '</next>; rel=next, </terms>; rel="author license", </style.css>; rel=stylesheet'
    )

    assert list(link_header_licenses(header)) == [
        "https://creativecommons.org/licenses/by/4.0/",
        "/terms",
    ]
    assert list(link_header_licenses("")) == []


def test_collector_ranks_by_confidence_then_known_id_then_discovery() -> None:
    collector = LicenseCollector(BASE)
    collector.add("Alle Rechte vorbehalten", "meta", "medium")
    collector.add("CC BY 2.0", "meta", "medium")
    collector.add("/terms", "rel-license", "high", is_url=True)
    collector.add("/cc", "rel-license", "high", is_url=True)
    collector.add("https://creativecommons.org/licenses/by-sa/4.0/", "rel-license", "high")
    collector.add("CC BY-NC 4.0", "text", "low")

    assert [(item.id, item.confidence) for item in collector.result()] == [
        ("CC-BY-SA-4.0", "high"),
        (None, "high"),
        (None, "high"),
        ("CC-BY-2.0", "medium"),
        (None, "medium"),
        ("CC-BY-NC-4.0", "low"),
    ]


def test_collector_lists_a_licence_once_with_its_best_confidence() -> None:
    collector = LicenseCollector(BASE)
    collector.add("CC BY 4.0", "text", "low")
    collector.add("https://creativecommons.org/licenses/by/4.0/", "meta", "medium")
    collector.add("http://creativecommons.org/licenses/by/4.0/deed.en", "rel-license", "high")

    assert collector.result() == [
        License(
            id="CC-BY-4.0",
            url="http://creativecommons.org/licenses/by/4.0/deed.en",
            label=None,
            source="rel-license",
            confidence="high",
        )
    ]


def test_collector_url_values_are_not_text_scanned() -> None:
    collector = LicenseCollector(BASE)
    collector.add("/tag/cc0", "rel-license", "high", is_url=True)
    collector.add("javascript:void(0)", "rel-license", "high", is_url=True)

    assert [(item.id, item.url) for item in collector.result()] == [
        (None, "https://www.example.org/tag/cc0")
    ]


def test_collector_properties() -> None:
    collector = LicenseCollector(BASE)
    collector.add_property(
        [
            {"@id": "https://creativecommons.org/licenses/by/4.0/"},
            {"@type": "CreativeWork", "url": "https://example.org/own", "name": "Own terms"},
            {"@value": "CC0"},
            {"name": "CC BY-SA 3.0"},
            7,
            None,
        ],
        "json-ld",
        "high",
    )

    assert [(item.id, item.url, item.label) for item in collector.result()] == [
        ("CC-BY-4.0", "https://creativecommons.org/licenses/by/4.0/", None),
        ("CC0-1.0", None, "CC0"),
        ("CC-BY-SA-3.0", None, "CC BY-SA 3.0"),
        (None, "https://example.org/own", "Own terms"),
    ]


def test_collector_footer_uses_urls_for_anchors_and_text_for_prose() -> None:
    collector = LicenseCollector(BASE)
    collector.add_footer(
        "Inhalte unter CC BY-SA 4.0. Dosis: CC 0,5 mg.",
        [
            ("https://creativecommons.org/licenses/by-sa/4.0/", "Lizenz"),
            ("https://example.org/tag/cc0", "Tag"),
        ],
    )

    assert [(item.id, item.source, item.confidence) for item in collector.result()] == [
        ("CC-BY-SA-4.0", "text", "low")
    ]
    assert collector.result()[0].url == "https://creativecommons.org/licenses/by-sa/4.0/"


def test_collector_bounds_the_number_of_candidates() -> None:
    collector = LicenseCollector(BASE)
    for number in range(5000):
        collector.add(f"/terms/{number}", "rel-license", "high", is_url=True)

    assert len(collector.result()) == 10
