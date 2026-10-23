import json

from bs4 import BeautifulSoup

from text_extraction.convert import convert_html
from text_extraction.convert.embedded import embedded_html

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

    result = embedded_html(soup(page), URL)

    assert result is not None
    assert result.startswith("<h1>Data &amp; more</h1>")
    assert "carried as data" in result


def test_visible_article_wins_over_the_data_block() -> None:
    visible = "<article>" + "A visible article with plenty of text. " * 10 + "</article>"

    assert embedded_html(soup(ld_json({"articleBody": BODY}, visible)), URL) is None


def test_generic_description_is_not_a_document() -> None:
    assert embedded_html(soup(ld_json({"description": BODY})), URL) is None
    assert embedded_html(soup('<script type="application/ld+json">{broken</script>'), URL) is None


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

    result = embedded_html(soup(page), URL)

    assert result is not None
    assert 'href="https://www.example.org/topic/files/a.pdf"' in result
    assert "<h2>Ideas</h2>" in result
    assert "Extra idea" in result
    assert "Sheet</a></li>" not in result  # used in the body, so not listed again


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

    result = embedded_html(soup(page), "https://www.youtube.com/watch?v=abc")

    assert result == (
        "<h1>Fractions &lt;1&gt;</h1><p>Math Channel</p><p>Line one</p><p>Line two</p>"
    )
    assert embedded_html(soup(page), "https://example.org/watch") is None


def test_convert_html_uses_the_embedded_content() -> None:
    page = ld_json({"@type": "Article", "headline": "Data lesson", "articleBody": BODY})

    result = convert_html(page, URL, "markdown")

    assert result.content.startswith("# Data lesson")
    assert "carried as data" in result.content
    assert result.title == "Data lesson"
