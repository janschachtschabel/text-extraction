"""The site tree's markup (package 18): a row of five cells (name, type, found, status, depth)
with a hidden column name each, the type pills with the "~" and the reason of a guess, a link's
title in quotes, the summary line, the type filter chips and a section's page count."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.ui.site_items import page_item, sitemap_item
from tests.ui.test_jobs_views import ORIGIN, fake_reads, show_tree, texts, tree

SITE = [
    page_item(
        0,
        f"{ORIGIN}/",
        title="Home",
        depth=0,
        page_type="home",
        links=(
            (f"{ORIGIN}/agb", "depth", "legal", "Terms"),
            (f"{ORIGIN}/p/12345", "depth", "content", "Mehr Infos"),
        ),
    ),
    page_item(1, f"{ORIGIN}/docs/a", title="Docs A", page_type="content"),
    sitemap_item(
        2,
        f"{ORIGIN}/sitemap.xml",
        [{"loc": f"{ORIGIN}/docs/learning-guide", "lastmod": "2024-05-01"}],
    ),
]


@pytest.fixture
def rendered(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_reads(monkeypatch, len(SITE), lambda offset, limit: SITE[offset : offset + limit])
    return lambda language="en": tree(show_tree(client, language=language))


def leaf(root: Any, path: str) -> Any:
    [item] = root.xpath(f"//li[./div/label/input[@value='{ORIGIN}{path}']]")
    return item


def test_a_url_row_has_five_cells_in_order(rendered: Any) -> None:
    row = leaf(rendered(), "/docs/a")

    classes = [
        name
        for element in row.iterdescendants()
        for name in element.get("class", "").split()
        if name.startswith("cell-")
    ]
    assert classes == ["cell-name", "cell-type", "cell-found", "cell-status", "cell-depth"]
    assert row.get("data-type") == "content"
    # example.org > docs/ > a; a data attribute, as the CSP forbids inline styles
    assert row.get("data-level") == "2"
    assert row.get("style") is None


@pytest.mark.parametrize(
    ("language", "names"),
    [
        ("en", ["Page:", "Type:", "Found:", "Status:", "Depth:"]),
        ("de", ["Seite:", "Typ:", "Gefunden:", "Status:", "Tiefe:"]),
    ],
)
def test_every_cell_starts_with_its_hidden_column_name(
    rendered: Any, language: str, names: list[str]
) -> None:
    root = rendered(language)

    row = leaf(root, "/docs/a")
    assert texts(row, ".//*[contains(@class, 'cell-')]/span[@class='visually-hidden'][1]") == names
    [head] = root.xpath("//div[@class='tree-head']")
    assert head.get("aria-hidden") == "true"
    assert [text.removesuffix(":") for text in names] == texts(head, "./span")


@pytest.mark.parametrize(
    ("language", "pill", "reason"),
    [
        ("en", "Legal ~, guessed, from the URL", "from the URL"),
        ("de", "Rechtliches ~, geschätzt, aus der URL", "aus der URL"),
    ],
)
def test_a_guessed_pill_says_so_with_its_reason(
    rendered: Any, language: str, pill: str, reason: str
) -> None:
    root = rendered(language)
    [element] = leaf(root, "/agb").xpath(".//span[contains(@class, 'page-type')]")

    classes = element.get("class").split()
    assert {"page-type", "type-legal", "guessed"} <= set(classes)
    assert " ".join(element.text_content().split()) == pill
    [mark] = element.xpath("./span[@aria-hidden='true']")
    assert mark.text == "~"
    assert reason in element.get("title")
    [fetched] = leaf(root, "/").xpath(".//span[contains(@class, 'page-type')]")
    assert "guessed" not in fetched.get("class").split()
    assert fetched.xpath("./span[@aria-hidden='true']") == []


@pytest.mark.parametrize(("language", "title"), [("en", "“Mehr Infos”"), ("de", "„Mehr Infos“")])
def test_a_title_from_a_link_is_in_quotes_and_in_italics(
    rendered: Any, language: str, title: str
) -> None:
    root = rendered(language)

    [linked] = leaf(root, "/p/12345").xpath(".//span[contains(@class, 'title')]")
    assert linked.text_content() == title
    assert linked.get("title") == title
    assert linked.get("class").split() == ["title", "title-link"]
    [plain] = leaf(root, "/docs/a").xpath(".//span[contains(@class, 'title')]")
    assert plain.text_content() == "Docs A"
    assert plain.get("class") == "title"


def test_the_found_cell_names_the_source_and_the_lastmod(rendered: Any) -> None:
    root = rendered()

    assert texts(leaf(root, "/docs/a"), ".//div[@class='cell-found']") == ["Found: crawled"]
    [found] = leaf(root, "/docs/learning-guide").xpath(".//div[@class='cell-found']")
    assert found.get("title") == "sitemap, changed 2024-05-01"
    assert root.xpath("//span[@class='lastmod']") == []


@pytest.mark.parametrize(
    ("language", "summary"),
    [
        ("en", "5 URLs · 2 crawled · 1 from the sitemap · 2 only linked · finished in 58 s"),
        ("de", "5 URLs · 2 gecrawlt · 1 aus der Sitemap · 2 nur verlinkt · fertig in 58 s"),
    ],
)
def test_the_summary_has_the_counts_and_the_duration(
    rendered: Any, language: str, summary: str
) -> None:
    assert texts(rendered(language), "//p[@class='tree-counts']") == [summary]


@pytest.mark.parametrize(
    ("language", "label", "all_", "event_free"),
    [
        ("en", "Filter by type", "All 5", "Type open 1"),
        ("de", "Nach Typ filtern", "Alle 5", "Typ offen 1"),
    ],
)
def test_the_chips_are_hidden_buttons_before_the_script_runs(
    rendered: Any, language: str, label: str, all_: str, event_free: str
) -> None:
    root = rendered(language)

    [group] = root.xpath("//div[@class='tree-filter']")
    assert group.get("role") == "group"
    assert group.get("aria-label") == label
    chips = group.xpath("./button")
    assert [chip.get("data-filter-type") for chip in chips] == [
        "all",
        "content",
        "home",
        "legal",
        "open",
    ]
    assert all(chip.get("hidden") is not None and chip.get("type") == "button" for chip in chips)
    assert [chip.get("aria-pressed") for chip in chips] == [
        "true",
        "false",
        "false",
        "false",
        "false",
    ]
    assert " ".join(chips[0].text_content().split()) == all_
    assert " ".join(chips[-1].text_content().split()) == event_free
    assert root.xpath("//p[@class='tree-types']")  # the text line stays without the script


def test_a_section_shows_its_pages_and_type(rendered: Any) -> None:
    root = rendered()

    [docs] = root.xpath("//li[contains(@class, 'node-section')][./div/label/span[text()='docs/']]")
    assert texts(docs, "./div[@class='cell-name']//span[@class='pages']") == ["2 pages"]
    [pill] = docs.xpath("./div//span[contains(@class, 'page-type')]")
    assert {"type-content", "guessed"} <= set(
        pill.get("class").split()
    )  # the sitemap page is guessed
    assert docs.get("data-type") is None
