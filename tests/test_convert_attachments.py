"""Attachment candidates of a page: linked files of the content and Drupal file entities."""

import json

from text_extraction.convert.attachments import Attachment, attachment_candidates

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PAGE_URL = "https://www.zebis.ch/unterrichtsmaterial/wasser"


def _entity(url: str = "/sites/default/files/2024-09/wasserkreislauf.docx", **over: object) -> str:
    entity = {
        "type": "file--file",
        "filename": "wasserkreislauf.docx",
        "uri": {"value": "public://x", "url": url},
        "filemime": DOCX,
        "filesize": 162627,
    }
    entity.update(over)
    return json.dumps(entity, separators=(",", ":"))


def _flight(payload: str) -> str:
    chunk = json.dumps(f'1a:{{"data":[{payload}]}}')
    return f"<script>self.__next_f.push([1,{chunk}])</script>"


def test_links_of_content_only() -> None:
    html = (
        '<main><a href="/files/blatt.pdf">Arbeitsblatt</a></main>'
        '<footer><a href="/files/impressum.pdf">Impressum</a></footer>'
    )
    assert attachment_candidates(html, PAGE_URL) == [
        Attachment("https://www.zebis.ch/files/blatt.pdf", "Arbeitsblatt")
    ]


def test_base_href_wins() -> None:
    html = '<base href="https://cdn.example/x/"><main><a href="a.pdf">A</a></main>'
    assert attachment_candidates(html, PAGE_URL)[0].url == "https://cdn.example/x/a.pdf"


def test_next_flight_entity() -> None:
    found = attachment_candidates(_flight(_entity()), PAGE_URL)
    assert found == [
        Attachment(
            "https://www.zebis.ch/sites/default/files/2024-09/wasserkreislauf.docx",
            "wasserkreislauf.docx",
            DOCX,
            162627,
        )
    ]


def test_entity_in_json_script() -> None:
    html = f'<script type="application/json">{{"included":[{_entity()}]}}</script>'
    assert [a.label for a in attachment_candidates(html, PAGE_URL)] == ["wasserkreislauf.docx"]


def test_links_first_then_entities_deduplicated() -> None:
    link = "/sites/default/files/2024-09/wasserkreislauf.docx"
    html = f'<main><a href="{link}">Blatt</a></main>' + _flight(_entity())
    found = attachment_candidates(html, PAGE_URL)
    assert [a.label for a in found] == ["Blatt"]


def test_unsafe_urls_and_images_skipped() -> None:
    payload = ",".join(
        [
            _entity("javascript:alert(1)"),
            _entity("https://user:pw@evil.example/a.docx"),
            _entity("/img/p.png", filemime="image/png"),
            _entity("ftp://host/a.docx"),
        ]
    )
    assert attachment_candidates(_flight(payload), PAGE_URL) == []


def test_malformed_and_huge_scripts_do_not_break() -> None:
    html = (
        '<script>"type":"file--file" {{{</script>'
        '<script type="application/json">{"type":"file--file","filename":</script>'
        "<script>" + '"type":"file--file",' * 5000 + "</script>"
        "<script>" + "x" * 3_000_000 + _entity() + "</script>" + _flight(_entity())
    )
    found = attachment_candidates(html, PAGE_URL)
    assert [a.label for a in found] == ["wasserkreislauf.docx"]


def test_at_most_20_candidates() -> None:
    payload = ",".join(_entity(f"/f/{i}.docx") for i in range(30))
    assert len(attachment_candidates(_flight(payload), PAGE_URL)) == 20


def test_deeply_nested_script_is_skipped() -> None:
    html = "<script>" + '{"type":"file--file","x":' + '{"a":' * 200000 + "</script>"
    assert attachment_candidates(html, "https://x.test/") == []


def test_pretty_printed_entity_found() -> None:
    pretty = json.dumps(json.loads(_entity()), indent=2).replace('"type":', '"type" :')
    html = f'<script type="application/json">{pretty}</script>'
    assert [a.label for a in attachment_candidates(html, PAGE_URL)] == ["wasserkreislauf.docx"]
