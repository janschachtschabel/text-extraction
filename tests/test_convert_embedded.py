import json

import pytest
from bs4 import BeautifulSoup

from text_extraction.convert import convert_html
from text_extraction.convert.embedded import embedded_content
from text_extraction.convert.html import OutputFormat
from text_extraction.convert.listings import LinkSection
from text_extraction.pii.html import HtmlText

URL = "https://www.example.org/topic/1"
BODY = "<p>" + "The lesson text is carried as data and not shown in the page shell. " * 4 + "</p>"


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def ld_json(payload: object, extra_body: str = "") -> str:
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(payload)}</script></head>'
        f"<body>{extra_body}</body></html>"
    )


def test_article_body_with_headline_is_returned() -> None:
    page = ld_json(
        {"@graph": [{"@type": "Article", "headline": "Data & more", "articleBody": BODY}]}
    )

    embedded = embedded_content(soup(page), URL)

    assert embedded is not None
    assert embedded.html.startswith("<h1>Data &amp; more</h1>")
    assert "carried as data" in embedded.html


def test_visible_article_wins_over_the_data_block() -> None:
    visible = "<article>" + "A visible article with plenty of text. " * 10 + "</article>"

    assert embedded_content(soup(ld_json({"articleBody": BODY}, visible)), URL) is None


def test_generic_description_is_not_a_document() -> None:
    assert embedded_content(soup(ld_json({"description": BODY})), URL) is None
    assert (
        embedded_content(soup('<script type="application/ld+json">{broken</script>'), URL) is None
    )


def test_historic_topic_payload_resolves_inline_attachments() -> None:
    payload = {
        "topic": "Lesson",
        "description": '<p>Read <a href="inline:a.pdf">the sheet</a>.</p>',
        "attachments": [
            {"file": "a.pdf", "name": "Sheet", "href": "files/a.pdf"},
            {"file": "b.pdf", "name": "Extra idea", "href": "files/b.pdf", "tag": "idea"},
        ],
    }
    script = f'<script id="embedded-topic" type="json">var x = {json.dumps(payload)};</script>'
    page = f"<html><body>{script}</body></html>"

    embedded = embedded_content(soup(page), URL)

    assert embedded is not None
    assert 'href="https://www.example.org/topic/files/a.pdf"' in embedded.html
    assert [(s.heading, s.links) for s in embedded.sections] == [
        ("Ideas", (("Extra idea", "https://www.example.org/topic/files/b.pdf"),))
    ]  # the sheet is used in the body, so not listed again


def kmap_page(lang: str = ' lang="de"') -> str:
    payload = {
        "topic": "Physikalische Größen",
        "description": '<p>Eine Größe hat Wert und Einheit.</p><img src="inline:Basisgrößen.png"/>',
        "attachments": [
            {
                "tag": "explanation",
                "name": "LEIFIphysik",
                "type": "link",
                "mime": "text/html",
                "href": "https://www.leifiphysik.de/uebergreifend/allgemeines-und-hilfsmittel/grundwissen/si-basisgroessen-und-einheiten",
            },
            {
                "tag": "explanation",
                "name": "Physikunterricht Online",
                "type": "link",
                "mime": "text/html",
                "href": "https://physikunterricht-online.de/hilfsmittel/physikalische-groessen-und-einheiten/",
            },
            {
                "type": "file",
                "name": "Basisgrößen.png",
                "file": "Basisgrößen.png",
                "mime": "image/png",
                "href": "data/Physik/Basisgr%C3%B6%C3%9Fen.png",
            },
        ],
    }
    script = (
        '<script id="embedded-topic" type="json">//<![CDATA[\n'
        f"{json.dumps(payload)}\n//]]></script>"
    )
    return f'<html{lang}><head><base href="/app/"></head><body>{script}</body></html>'


def test_link_attachments_keep_their_own_urls() -> None:
    payload = {
        "topic": "Lesson",
        "description": "<p>Text.</p>",
        "attachments": [
            {"tag": "explanation", "name": "One", "type": "link", "href": "https://a.example/1"},
            {"tag": "explanation", "name": "Two", "type": "link", "href": "https://b.example/2"},
            {"type": "file", "name": "Sheet", "file": "s.pdf", "href": "files/s.pdf"},
        ],
    }
    script = f'<script id="embedded-topic" type="json">{json.dumps(payload)}</script>'

    embedded = embedded_content(soup(f"<html><body>{script}</body></html>"), URL)

    assert embedded is not None
    assert embedded.sections == (
        LinkSection(
            "Explanations", 2, (("One", "https://a.example/1"), ("Two", "https://b.example/2"))
        ),
        LinkSection("Attachments", 2, (("Sheet", "https://www.example.org/topic/files/s.pdf"),)),
    )


def test_attachment_headings_follow_the_page_language() -> None:
    german = embedded_content(soup(kmap_page()), URL)
    default = embedded_content(soup(kmap_page(lang="")), URL)

    assert german is not None
    assert default is not None
    assert [s.heading for s in german.sections] == ["Erklärungen"]
    assert [s.heading for s in default.sections] == ["Explanations"]


def test_convert_html_keeps_kmap_attachment_links() -> None:
    result = convert_html(kmap_page(), "https://kmap.eu/app/browser/Physik", "markdown")

    assert result.content.endswith(
        "## Erklärungen\n\n"
        "- [LEIFIphysik](https://www.leifiphysik.de/uebergreifend/allgemeines-und-hilfsmittel/"
        "grundwissen/si-basisgroessen-und-einheiten)\n"
        "- [Physikunterricht Online](https://physikunterricht-online.de/hilfsmittel/"
        "physikalische-groessen-und-einheiten/)"
    )


def test_html_keeps_kmap_attachment_links_inside_the_body() -> None:
    content = convert_html(kmap_page(), "https://kmap.eu/app/browser/Physik", "html").content

    body_end = content.rindex("</body>")
    assert content.index("<h2>Erklärungen</h2>") < body_end
    assert content.index("LEIFIphysik") < body_end
    assert content.rstrip().endswith("</html>")
    analysed = HtmlText.parse(content)
    assert not analysed.has_unread
    assert "LEIFIphysik" in analysed.text
    assert "LEIFIphysik" in analysed.render([], drop_unread=True)


def test_an_attachment_without_a_label_is_left_out() -> None:
    payload = {
        "topic": "Lesson",
        "description": "<p>Text.</p>",
        "attachments": [
            {"tag": "idea", "type": "link", "href": "https://a.example/1"},
            {"tag": "idea", "name": "Named", "type": "link", "href": "https://b.example/2"},
        ],
    }
    script = f'<script id="embedded-topic" type="json">{json.dumps(payload)}</script>'

    embedded = embedded_content(soup(f"<html><body>{script}</body></html>"), URL)

    assert embedded is not None
    assert embedded.sections == (LinkSection("Ideas", 2, (("Named", "https://b.example/2"),)),)


def test_an_attachment_with_an_unknown_tag_is_an_attachment() -> None:
    payload = {
        "topic": "Lesson",
        "description": "<p>Text.</p>",
        "attachments": [
            {"tag": "quiz", "name": "Quiz", "type": "link", "href": "https://a.example/q"},
        ],
    }
    script = f'<script id="embedded-topic" type="json">{json.dumps(payload)}</script>'

    embedded = embedded_content(soup(f"<html><body>{script}</body></html>"), URL)

    assert embedded is not None
    assert embedded.sections == (LinkSection("Attachments", 2, (("Quiz", "https://a.example/q"),)),)


def test_youtube_watch_page_yields_video_details() -> None:
    player = {
        "videoDetails": {
            "title": "Fractions <1>",
            "author": "Math Channel",
            "shortDescription": "Line one\n\nLine two",
        }
    }
    script = f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
    page = f"<html><body>{script}</body></html>"

    result = embedded_content(soup(page), "https://www.youtube.com/watch?v=abc")

    assert result is not None
    assert result.html == (
        "<h1>Fractions &lt;1&gt;</h1><p>Math Channel</p><p>Line one</p><p>Line two</p>"
    )
    assert embedded_content(soup(page), "https://example.org/watch") is None


def test_convert_html_uses_the_embedded_content() -> None:
    page = ld_json({"@type": "Article", "headline": "Data lesson", "articleBody": BODY})

    result = convert_html(page, URL, "markdown")

    assert result.content.startswith("# Data lesson")
    assert "carried as data" in result.content
    assert result.title == "Data lesson"


def lesson_page(description: str, *attachments: dict[str, str]) -> str:
    payload = {"topic": "Lesson", "description": description, "attachments": list(attachments)}
    script = f'<script id="embedded-topic" type="json">{json.dumps(payload)}</script>'
    return f'<html lang="de"><body>{script}</body></html>'


KMAP_URL = "https://kmap.eu/app/browser/Physik"
UNSAFE = ("javascript:alert(1)", "data:text/html,<b>x</b>", "mailto:someone@example.org")


@pytest.mark.parametrize("fmt", ["markdown", "html", "text"])
def test_only_http_attachment_targets_are_listed(fmt: OutputFormat) -> None:
    unsafe = [
        {"tag": "explanation", "name": f"Bad {n}", "type": "link", "href": href}
        for n, href in enumerate(UNSAFE)
    ]
    good = {"tag": "explanation", "name": "Good", "type": "link", "href": "https://a.example/1"}
    page = lesson_page(BODY, *unsafe, good)

    content = convert_html(page, KMAP_URL, fmt).content

    assert "https://a.example/1" in content
    for scheme in ("javascript", "data:", "mailto"):
        assert scheme not in content
    assert "Bad" not in content


def test_text_format_lists_attachments_with_their_urls() -> None:
    page = lesson_page(
        BODY,
        {"file": "b.pdf", "name": "Extra worksheet", "href": "files/b.pdf", "tag": "idea"},
        {"name": "Site", "type": "link", "href": "https://other.org/x", "tag": "explanation"},
    )

    content = convert_html(page, KMAP_URL, "text").content

    assert content.endswith(
        "Ideen\n\n- Extra worksheet: https://kmap.eu/app/browser/files/b.pdf\n\n"
        "Erklärungen\n\n- Site: https://other.org/x"
    )
    assert content.count("https://kmap.eu/app/browser/files/b.pdf") == 1


def test_an_attachment_the_lesson_text_links_to_is_not_listed_again() -> None:
    description = (
        '<p>See <a href="https://a.example/1#part">the first site</a> and '
        '<a href="files/s.pdf">the sheet</a>.</p>'
    )
    page = lesson_page(
        description,
        {"tag": "explanation", "name": "One", "type": "link", "href": "https://a.example/1"},
        {"tag": "explanation", "name": "Two", "type": "link", "href": "https://b.example/2"},
        {"type": "file", "name": "Sheet", "file": "s.pdf", "href": "files/s.pdf"},
    )

    embedded = embedded_content(soup(page), URL)

    assert embedded is not None
    assert embedded.sections == (LinkSection("Erklärungen", 2, (("Two", "https://b.example/2"),)),)
