"""The UI's single operations (rulings P10-S14, P10-S15, P10-S17): the forms' REST bodies, the
result view (content, downloads, metadata, structured values, images, links, the timeline), its
escaping, the shown REST answers and the curl line."""

import base64
import json
from datetime import UTC, datetime
from typing import Any, Final

import lxml.html
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from tests.fakes import ARTICLE, PAGE, FakeFetcher, fetched
from tests.test_extract_api import BrowserCase
from tests.ui.conftest import USER_KEY, run, running
from text_extraction.envelope import ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import ItemRun, JobItem
from text_extraction.structured.types import StructuredOptions
from text_extraction.ui import results
from text_extraction.ui.forms import (
    OPERATIONS,
    UI_METADATA_SCHEMA,
    OptionsError,
    build_body,
)
from text_extraction.ui.markdown import render_markdown
from text_extraction.ui.results import render_result
from text_extraction.ui.texts import WARNING_TEXTS, warning_text
from text_extraction.ui.timeline import TimelineStep, timeline

SCRIPT = "<script>alert(1)</script>"
BREAKOUT = '"><img src=x onerror=alert(1)>'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 120
LLM_KEY = "llm-key-raw-secret-0123456789"
TEXT = (
    "# Plants\n\n"
    + "Leaves turn light into sugar. " * 40
    + "\n\n## Roots\n\n"
    + ("Roots take up water from the soil. " * 40)
)
JSON_LD_PAGE = (
    """<!doctype html>
<html lang="en"><head><title>Plants</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Article", "headline": "How leaves work",
 "datePublished": "2026-01-02"}
</script></head>
<body><article><h1>How leaves work</h1><p>"""
    + ("Leaves turn light into sugar. " * 60)
    + """
</p></article></body></html>
"""
)


def fields(**values: str) -> dict[str, Any]:
    """An extract form as the page posts it by default (Markdown, metadata on, wait 25)."""
    form: dict[str, Any] = {"url": PAGE, "format": "markdown", "mode": "auto", "metadata": "on"}
    form["wait_seconds"] = "25"
    form.update(values)
    return form


def tree(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def texts(root: Any, xpath: str) -> list[str]:
    return [" ".join(node.text_content().split()) for node in root.xpath(xpath)]


def pairs(root: Any, dl_class: str) -> dict[str, str]:
    """The terms and descriptions of the definition list of ``dl_class``."""
    terms = texts(root, f"//dl[@class='{dl_class}']/dt")
    return dict(zip(terms, texts(root, f"//dl[@class='{dl_class}']/dd"), strict=True))


def raw_json(html: str) -> dict[str, Any]:
    """The REST answer the fragment shows in its "Raw JSON" block."""
    found = tree(html).xpath("//details[@class='raw']/pre")
    assert found, html
    parsed: dict[str, Any] = json.loads(found[0].text_content())
    return parsed


def sent_body(html: str) -> dict[str, Any]:
    """The JSON body of the fragment's curl line."""
    line = tree(html).xpath("//pre[@class='curl']")[0].text_content()
    start = line.index("-d '") + 4
    parsed: dict[str, Any] = json.loads(line[start : line.rindex("'")])
    return parsed


def result_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "url": PAGE,
        "final_url": PAGE,
        "upstream_status": 200,
        "content_type": "text/html",
        "mode_used": "fast",
        "attempts": 1,
        "language": "en",
        "word_count": 3,
        "title": "Title",
        "format": "markdown",
        "content": "Some text.",
        "quality": None,
        "timings": {"total_ms": 40, "compliance_ms": 3, "fetch_ms": 20, "convert_ms": 10},
        "compliance": {"mode": "enforce", "decision": "allowed", "robots": "allowed"},
        "metadata": None,
        "links": None,
        "fingerprint": None,
        "cache": "miss",
        "warnings": [],
    }
    data.update(overrides)
    return data


def rendered(data: dict[str, Any], *, status: str = "success", **kwargs: Any) -> str:
    envelope = {"status": status, "data": data, "trace_id": "t"}
    return render_result(op="extract", http_status=200, envelope=envelope, **kwargs)


# --- the forms' bodies ---------------------------------------------------------------------------


def test_operations_name_their_rest_routes() -> None:
    assert {name: (op.method, op.path) for name, op in OPERATIONS.items()} == {
        "login": ("GET", "/v1/info"),
        "info": ("GET", "/v1/info"),
        "extract": ("POST", "/v1/extract"),
        "file": ("POST", "/v1/extract/file"),
        "screenshot": ("POST", "/v1/screenshot"),
        "structured": ("POST", "/v1/structured"),
        "chunk": ("POST", "/v1/chunk"),
        "batch": ("POST", "/v1/batch"),
        "site-structure": ("POST", "/v1/site-structure"),
        "job": ("GET", "/v1/jobs/{job_id}"),
        "artifact": ("GET", "/v1/jobs/{job_id}/artifacts/{name}"),
        "cancel": ("DELETE", "/v1/jobs/{job_id}"),
    }


@pytest.mark.parametrize(("choice", "suffix"), [("text", ".txt"), ("markdown", ".md")])
def test_format_choice_text_and_markdown(client: TestClient, choice: str, suffix: str) -> None:
    assert build_body("extract", fields(format=choice))["format"] == choice

    html = run(client, "extract", **fields(format=choice)).text

    assert sent_body(html)["format"] == choice
    assert raw_json(html)["data"]["format"] == choice
    links = tree(html).xpath("//a[@download]")
    assert [link.get("download") for link in links] == [f"result{suffix}"]
    href = links[0].get("href")
    media = "text/plain" if choice == "text" else "text/markdown"
    assert href.startswith(f"data:{media};charset=utf-8;base64,")
    content = raw_json(html)["data"]["content"]
    assert base64.b64decode(href.split(",", 1)[1]).decode() == content


def test_extract_body_from_the_form_fields() -> None:
    body = build_body("extract", fields(mode="fast", metadata=""))

    assert body == {
        "url": PAGE,
        "format": "markdown",
        "mode": "fast",
        "metadata": False,
        "wait_seconds": 25,
    }
    assert build_body("extract", {"url": PAGE})["metadata"] is False  # an unchecked box


def test_metadata_structured_checkbox() -> None:
    body = build_body("extract", fields(metadata_structured="on"))

    assert body["structured"] == {"strategy": "metadata", "schema": UI_METADATA_SCHEMA}
    StructuredOptions(**body["structured"])  # the REST model takes it
    assert set(UI_METADATA_SCHEMA["properties"]) == {
        "name",
        "headline",
        "description",
        "author",
        "datePublished",
        "dateModified",
        "license",
        "inLanguage",
        "keywords",
        "publisher",
        "educationalLevel",
        "learningResourceType",
        "isAccessibleForFree",
    }
    assert "structured" not in build_body("extract", fields())

    with running(FakeFetcher({PAGE: fetched(body=JSON_LD_PAGE)})) as client:
        html = run(client, "extract", **fields(metadata_structured="on")).text

    root = tree(html)
    values = dict(
        zip(
            texts(root, "//table[@class='values']//th[@scope='row']"),
            texts(root, "//table[@class='values']//td[@class='value']"),
            strict=True,
        )
    )
    assert values == {"headline": "How leaves work", "datePublished": "2026-01-02"}
    assert texts(root, "//table[@class='values']//td[@class='source']") == ["metadata"] * 2
    missing = set(texts(root, "//ul[@class='missing']/li"))
    assert missing == set(UI_METADATA_SCHEMA["properties"]) - {"headline", "datePublished"}


def test_describe_images_checkbox(client: TestClient) -> None:
    assert build_body("extract", fields(describe_images="on"))["describe_images"] == {
        "max_images": 5
    }
    assert build_body("extract", fields(describe_images="on", max_images="7"))[
        "describe_images"
    ] == {"max_images": 7}
    assert "describe_images" not in build_body("extract", fields(max_images="7"))
    with pytest.raises(OptionsError, match="max_images must be a whole number"):
        build_body("extract", fields(describe_images="on", max_images="many"))

    html = run(client, "extract", **fields(describe_images="on")).text

    root = tree(html)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 400"]
    assert texts(root, "//*[@class='error-code']") == ["invalid_request"]
    assert raw_json(html)["error"]["message"] in html


def test_options_json_merged_and_invalid_json_message(client: TestClient) -> None:
    merged = build_body("extract", fields(options='{"links": true, "pii": "mask"}'))
    assert (merged["links"], merged["pii"], merged["url"]) == (True, "mask", PAGE)
    # A field left at its default yields to Options (html only through Options).
    assert build_body("extract", fields(options='{"format": "html"}'))["format"] == "html"
    assert build_body("extract", fields(options="  "))["url"] == PAGE

    for options, message in (
        ("{not json", "Options is not valid JSON."),
        ("[1, 2]", "Options must be a JSON object."),
        ('{"url": "https://other.example/"}', "url is given twice."),
        ('{"format": "html"}', "format is given twice."),  # with Text chosen
    ):
        form = fields(options=options, format="text")
        with pytest.raises(OptionsError) as raised:
            build_body("extract", form)
        assert str(raised.value) == message

        with capture_logs() as logs:
            html = run(client, "extract", **form).text
        assert texts(tree(html), "//*[@role='alert']") == [message]
        paths = [log["path"] for log in logs if log["event"] == "request"]
        assert paths == ["/ui/run/extract"]  # nothing forwarded


def test_inner_status_shown(client: TestClient) -> None:
    html = run(client, "extract", auth=False, **fields()).text
    root = tree(html)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 401"]
    assert texts(root, "//*[@class='error-code']") == ["unauthorized"]

    html = run(client, "extract", **fields(options='{"mode2": 1, "cache": "maybe"}')).text
    root = tree(html)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 422"]
    assert texts(root, "//*[@class='error-code']") == ["invalid_request"]
    details = texts(root, "//ul[@class='details']/li")
    assert len(details) == 2
    assert any(line.startswith("body.mode2:") for line in details)
    assert any(line.startswith("body.cache:") for line in details)


def test_file_upload_forwarded(client: TestClient) -> None:
    html = run(
        client,
        "file",
        files={"file": ("lesson.html", ARTICLE.encode(), "text/html")},
        format="text",
        metadata="on",
        wait_seconds="25",
        options='{"ocr": "never"}',
    ).text

    root = tree(html)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 200"]
    data = raw_json(html)["data"]
    assert (data["mode_used"], data["format"], data["title"]) == (
        "upload",
        "text",
        "Photosynthesis explained",
    )
    assert texts(root, "//*[@class='title']") == ["Photosynthesis explained"]
    curl = root.xpath("//pre[@class='curl']")[0].text_content()
    assert "-F 'file=@lesson.html'" in curl
    assert "-F 'format=text'" in curl
    assert "-F 'ocr=never'" in curl


def test_chunk_text(client: TestClient) -> None:
    html = run(
        client,
        "chunk",
        text=TEXT,
        text_type="markdown",
        format="markdown",
        chunking='{"strategy": "recursive", "size": 300}',
        wait_seconds="25",
    ).text

    chunks = raw_json(html)["data"]["chunks"]
    assert len(chunks) > 1
    root = tree(html)
    assert len(root.xpath("//ol[@class='chunks']/li")) == len(chunks)
    assert sent_body(html)["text"] == TEXT
    assert "url" not in sent_body(html)

    with pytest.raises(OptionsError, match="Chunking is not valid JSON"):
        build_body("chunk", {"text": TEXT, "chunking": "{"})


def test_structured(client: TestClient) -> None:
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    structured = json.dumps({"strategy": "metadata", "schema": schema})

    html = run(
        client, "structured", url=PAGE, format="markdown", mode="auto", structured=structured
    ).text

    root = tree(html)
    assert texts(root, "//table[@class='values']//th[@scope='row']") == ["title"]
    assert texts(root, "//table[@class='values']//td[@class='value']") == [
        "Photosynthesis explained"
    ]
    assert sent_body(html)["structured"] == {"strategy": "metadata", "schema": schema}


def test_info(client: TestClient) -> None:
    html = run(client, "info").text

    root = tree(html)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 200"]
    info = pairs(root, "info")
    assert info["Principal"] == "alice"
    assert info["Role"] == "user"
    curl = root.xpath("//pre[@class='curl']")[0].text_content()
    assert curl.startswith("curl -X GET 'http://testserver/v1/info'")


def test_screenshot_embeds_png() -> None:
    case = BrowserCase(ui_enabled=True)
    with TestClient(case.app) as client, case.worker(artifact=PNG):
        html = run(client, "screenshot", url=PAGE, full_page="on", wait_seconds="25").text

    root = tree(html)
    images = root.xpath("//img[@class='screenshot']")
    assert len(images) == 1
    expected = "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert images[0].get("src") == expected
    assert sent_body(html) == {"url": PAGE, "full_page": True, "wait_seconds": 25}


def test_curl_line_hides_key(client: TestClient) -> None:
    html = run(client, "extract", headers={"X-LLM-Api-Key": LLM_KEY}, **fields()).text

    curl = tree(html).xpath("//pre[@class='curl']")[0].text_content()
    assert curl.startswith("curl -X POST 'http://testserver/v1/extract'")
    assert '-H "Authorization: Bearer $API_KEY"' in curl
    assert '-H "X-LLM-Api-Key: $LLM_API_KEY"' in curl
    assert USER_KEY not in html
    assert LLM_KEY not in html


# --- the result view -----------------------------------------------------------------------------


def test_result_shows_the_fields() -> None:
    data = result_data(
        title="Leaves",
        language="de",
        word_count=1234,
        metadata={
            "description": "About leaves.",
            "authors": ["Ada", "Ben"],
            "published": "2026-01-02",
            "canonical_url": "https://example.org/leaves",
            "license": {
                "id": "CC-BY-4.0",
                "url": "https://creativecommons.org/licenses/by/4.0/",
                "label": None,
                "source": "rel-license",
                "confidence": "high",
            },
            "lrmi": {"educationalLevel": ["primary"]},
            "images": [
                {"url": "https://example.org/a.png", "alt": "A leaf", "description": "Green"}
            ],
        },
        image_descriptions={"asked": 1, "described": 1, "skipped": 0, "failed": 0},
    )
    root = tree(rendered(data))

    assert texts(root, "//*[@class='title']") == ["Leaves"]
    facts = pairs(root, "facts")
    assert (facts["Language"], facts["Words"]) == ("de", "1234")
    meta = pairs(root, "meta")
    assert meta["Description"] == "About leaves."
    assert meta["Authors"] == "Ada, Ben"
    assert meta["Published"] == "2026-01-02"
    assert meta["Licence"].startswith("CC-BY-4.0")
    assert "educationalLevel" in meta["LRMI"]
    assert texts(root, "//table[@class='images']//td[@class='alt']") == ["A leaf"]
    assert texts(root, "//table[@class='images']//td[@class='description']") == ["Green"]
    report = texts(root, "//*[@class='image-report']")
    assert report == ["Descriptions: asked 1, described 1, skipped 0, failed 0"]
    form = root.xpath("//form[@class='previews']")[0]
    assert form.get("hx-post") == "/ui/run/images"
    assert [i.get("value") for i in form.xpath(".//input[@name='url']")] == [
        "https://example.org/a.png"
    ]
    assert [i.get("value") for i in form.xpath(".//input[@name='compliance']")] == ["enforce"]


def test_result_escapes_every_field() -> None:
    payloads = (SCRIPT, BREAKOUT)
    for payload in payloads:
        data = result_data(
            title=f"T {payload}",
            content=f"Text {payload}\n\n[x]({payload})",
            metadata={
                "description": f"D {payload}",
                "authors": [payload],
                "images": [
                    {
                        "url": f"https://example.org/a.png?{payload}",
                        "alt": f"A {payload}",
                        "description": f"V {payload}",
                    },
                ],
                "lrmi": {payload: payload},
            },
            structured={
                "values": {"headline": payload},
                "sources": {"headline": "metadata"},
                "missing": [payload],
            },
            warnings=[payload],
            links=[{"url": f"https://example.org/?{payload}", "text": payload, "kind": "internal"}],
        )
        html = rendered(data)
        assert payload not in html
        root = tree(html)
        assert root.xpath("//script") == []
        assert root.xpath("//*[@onerror]") == []
        assert [img for img in root.xpath("//img") if img.get("src") == "x"] == []
        # The text is still there, escaped.
        assert f"V {payload}" in texts(root, "//table[@class='images']//td[@class='description']")
        assert f"A {payload}" in texts(root, "//table[@class='images']//td[@class='alt']")
        assert texts(root, "//table[@class='values']//td[@class='value']") == [payload]


def test_result_escapes_a_real_page() -> None:
    page = f"""<!doctype html><html lang="en"><head><title>T {SCRIPT}</title>
<meta name="description" content="D {BREAKOUT.replace('"', "&quot;")}">
<script type="application/ld+json">{json.dumps({"@type": "Article", "headline": BREAKOUT})}</script>
</head><body><article><h1>Heading</h1>
<p>{"Leaves turn light into sugar. " * 40} &lt;script&gt;alert(1)&lt;/script&gt;</p>
<p><img src="/a.png" alt="A {BREAKOUT.replace('"', "&quot;")}"></p>
</article></body></html>"""
    with running(FakeFetcher({PAGE: fetched(body=page)})) as client:
        html = run(client, "extract", **fields(metadata_structured="on")).text

    data = raw_json(html)["data"]
    assert SCRIPT in data["content"].replace("\\", "")  # Markdown escapes the "<"
    assert BREAKOUT in data["metadata"]["description"]
    assert SCRIPT not in html
    assert BREAKOUT not in html
    root = tree(html)
    assert root.xpath("//script") == []
    assert root.xpath("//*[@onerror]") == []


def test_rendered_markdown_is_safe() -> None:
    html = str(
        render_markdown(
            f"{SCRIPT}\n\n[x](javascript:alert(1))\n\n![a](http://img.example/a.png)\n\n"
            "[ok](https://example.org/ok) and [rel](/relative) <https://auto.example/>"
        )
    )

    root = tree(html)
    assert root.xpath("//script") == []
    assert "&lt;script&gt;" in html
    assert root.xpath("//img") == []
    assert "a" in texts(root, "//p")
    links = [(a.get("href"), a.get("rel"), a.text_content()) for a in root.xpath("//a")]
    assert links == [
        ("https://example.org/ok", "noopener noreferrer", "ok"),
        ("https://auto.example/", "noopener noreferrer", "https://auto.example/"),
    ]
    assert "javascript:" not in "".join(a.get("href") for a in root.xpath("//a"))


def test_markdown_shown_rendered_and_as_source(client: TestClient) -> None:
    html = run(client, "extract", **fields()).text
    root = tree(html)
    assert root.xpath("//pre[@class='content']")
    assert root.xpath("//div[@class='markdown']//h1")

    html = run(client, "extract", **fields(format="text")).text
    assert tree(html).xpath("//div[@class='markdown']") == []


def test_links_only_http() -> None:
    data = result_data(
        links=[
            {"url": "https://example.org/a", "text": "A", "kind": "internal"},
            {"url": "http://example.org/b", "text": "B", "kind": "internal"},
            {"url": "javascript:alert(1)", "text": "C", "kind": "external"},
            {"url": "ftp://example.org/d", "text": "D", "kind": "file"},
            {"url": "data:text/html,<b>x</b>", "text": "E", "kind": "external"},
        ]
    )
    root = tree(rendered(data))

    anchors = root.xpath("//ul[@class='links']//a")
    assert [(a.get("href"), a.get("rel")) for a in anchors] == [
        ("https://example.org/a", "noopener noreferrer"),
        ("http://example.org/b", "noopener noreferrer"),
    ]
    assert len(root.xpath("//ul[@class='links']/li")) == 5
    assert all(
        (a.get("href") or "").startswith(("http://", "https://", "data:text/"))
        for a in root.xpath("//a")
    )


def test_job_answer_is_shown() -> None:
    envelope = {
        "status": "success",
        "data": {
            "id": "job1",
            "kind": "extract",
            "state": "running",
            "total": 1,
            "status_url": "/v1/jobs/job1",
        },
    }
    root = tree(render_result(op="extract", http_status=202, envelope=envelope))

    assert texts(root, "//*[@class='http-status']") == ["HTTP 202"]
    assert texts(root, "//*[@class='job']") == ["Continues as job job1 (extract): /v1/jobs/job1"]


# --- the timeline --------------------------------------------------------------------------------


def labels(steps: list[TimelineStep]) -> list[str]:
    return [step.label for step in steps]


def test_timeline_from_fields() -> None:
    fast = ExtractResult.model_validate(
        result_data(attempts=2, final_url=f"{PAGE}#moved", cache="hit", upstream_status=200)
    )
    steps = timeline(None, fast, status="success")
    assert labels(steps) == ["Compliance", "Fetch", "Convert", "Outcome"]
    assert steps[0] == TimelineStep("Compliance", "allowed (mode enforce, robots allowed)", 3)
    assert steps[1].ms == 20
    for part in ("2 attempts", "HTTP 200", "cache hit", f"final URL {PAGE}#moved"):
        assert part in steps[1].detail
    assert steps[2] == TimelineStep("Convert", "text/html", 10)
    assert steps[3] == TimelineStep("Outcome", "success", 40)

    browser = ExtractResult.model_validate(
        result_data(
            mode_used="browser",
            unblocked=True,
            timings={
                "total_ms": 90,
                "compliance_ms": 1,
                "fetch_ms": 5,
                "convert_ms": 4,
                "render_ms": 70,
                "pii_ms": 3,
                "llm_ms": 6,
            },
            pii={"mode": "mask", "language": "en", "entities": {"PERSON": 2, "EMAIL_ADDRESS": 1}},
            document={
                "kind": "pdf",
                "converter": "docling",
                "pages": 9,
                "pages_converted": 9,
                "ocr": True,
            },
        )
    )
    error = ErrorInfo(code=ErrorCode.TIMEOUT, message="Too slow.", retryable=True)
    steps = timeline(None, browser, status="partial", error=error, requested_mode="auto")
    assert labels(steps) == ["Compliance", "Fetch", "Render", "Convert", "PII", "LLM", "Outcome"]
    assert steps[2] == TimelineStep("Render", "escalated from fast, unblock mode", 70)
    assert steps[3] == TimelineStep("Convert", "text/html, docling, 9 pages, OCR", 4)
    assert steps[4] == TimelineStep("PII", "3 entities (mask)", 3)
    assert steps[5] == TimelineStep("LLM", "", 6)
    assert steps[6] == TimelineStep("Outcome", "partial, timeout, retryable", 90)
    assert timeline(None, browser, requested_mode="browser")[2].detail == "unblock mode"

    run = ItemRun(
        pool="browser", attempts=2, claimed_at=datetime(2026, 10, 30, 12, 0, 5, tzinfo=UTC)
    )
    item = JobItem(index=0, url=PAGE, state="done", result=None, run=run)
    steps = timeline(item, fast, status="success")
    assert steps[0] == TimelineStep("Claim", "browser pool, claim 2, 2026-10-30 12:00:05 UTC", None)

    html = rendered(result_data(warnings=["browser_unavailable", "no_such_warning_x"]))
    root = tree(html)
    warnings = texts(root, "//ul[@class='warnings']/li")
    assert warnings == [
        f"browser_unavailable: {WARNING_TEXTS['browser_unavailable']}",
        "no_such_warning_x",
    ]
    assert "browser" in WARNING_TEXTS["browser_unavailable"]
    assert warning_text("action_3_failed") == "Browser action 3 failed."
    steps_html = texts(root, "//ol[@class='timeline']/li")
    assert steps_html[0].startswith("Compliance")
    assert steps_html[-1].startswith("Outcome")


# --- review fixes ---------------------------------------------------------------------------


def test_html_download_is_plain_text() -> None:
    root = tree(rendered(result_data(format="html", content=f"<p>{SCRIPT}</p>")))

    links = root.xpath("//a[@download]")
    assert [link.get("download") for link in links] == ["result.html"]
    assert links[0].get("href").startswith("data:text/plain;charset=utf-8;base64,")
    assert "data:text/html" not in rendered(result_data(format="html", content="<p>x</p>"))


def test_large_content_copies_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(results, "SHOWN_MAX_CHARS", 1_000)
    monkeypatch.setattr(results, "DOWNLOAD_MAX_CHARS", 5_000)
    content = "".join(f"line {index:05d}\n" for index in range(400))  # 4,400 characters
    chunks = [
        {
            "index": i,
            "text": content[i * 400 : (i + 1) * 400],
            "start": i * 400,
            "end": (i + 1) * 400,
            "headings": [],
        }
        for i in range(11)
    ]
    html = rendered(result_data(content=content, chunks=chunks))
    root = tree(html)

    shown = root.xpath("//pre[@class='content']")[0].text_content()
    assert shown == content[:1_000]
    notes = texts(root, "//*[@class='cut-note']")
    assert "Showing the first 1000 of 4400 characters; the download has all of them." in notes
    raw = root.xpath("//details[@class='raw']/pre")[0].text_content()
    assert len(raw) <= 1_000
    assert any(note.startswith("The raw JSON is cut at 1000 of ") for note in notes)
    href = root.xpath("//a[@download]")[0].get("href")
    assert base64.b64decode(href.split(",", 1)[1]).decode() == content  # the full copy
    assert len(root.xpath("//ol[@class='chunks']/li")) == 2  # 800 of the 1,000 characters
    assert "9 more chunks not shown; see the download." in notes
    assert root.xpath("//div[@class='markdown']") == []  # not rendered over the cap

    big = "x" * 6_000
    root = tree(rendered(result_data(content=big)))
    href = root.xpath("//a[@download]")[0].get("href")
    assert base64.b64decode(href.split(",", 1)[1]).decode() == big[:5_000]
    assert (
        "The download is cut at 5000 of 6000 characters; the curl command reads all of it."
        in texts(root, "//*[@class='cut-note']")
    )


def test_screenshot_read_failure_is_noted() -> None:
    # One call a minute: the screenshot call takes it, the read of the artifact gets a 429.
    case = BrowserCase(ui_enabled=True, rate_limit_per_minute=1)
    with TestClient(case.app) as client, case.worker(artifact=PNG):
        html = run(client, "screenshot", url=PAGE, full_page="on", wait_seconds="25").text

    root = tree(html)
    assert root.xpath("//img[@class='screenshot']") == []
    notes = texts(root, "//*[@class='screenshot-note']")
    assert len(notes) == 1
    assert notes[0].startswith("The screenshot could not be read: HTTP 429")


def test_refusal_shows_when_to_retry() -> None:
    with running(rate_limit_per_minute=1) as client:
        run(client, "images", url=["https://example.org/a.png"], alt=["a"])
        html = run(client, "images", url=["https://example.org/a.png"], alt=["a"]).text

    root = tree(html)
    assert texts(root, "//*[@class='error-code']") == ["rate_limited"]
    assert texts(root, "//*[@class='retry-after']")[0].startswith("Retry after ")


# A Markdown text of about 1.5 MB: over Starlette's default field size of 1 MiB.
LONG_TEXT: Final = "\n\n".join(
    f"## Part {n}\n\n" + "Leaves turn light into sugar. " * 50 for n in range(1000)
)


@pytest.mark.parametrize("encoding", ["multipart", "urlencoded"])
def test_chunk_text_over_one_mib(client: TestClient, encoding: str) -> None:
    """A text up to the REST limit of ``POST /v1/chunk`` goes through the Chunk tab, posted as
    the tab posts it (multipart) and urlencoded (ruling R-P10-F2)."""
    assert len(LONG_TEXT.encode()) > 1_500_000
    form: dict[str, Any] = {"text_type": "markdown", "format": "markdown"}
    form["chunking"] = '{"strategy": "recursive", "size": 8000}'
    if encoding == "multipart":  # a field part without a file name, as the browser sends it
        answer = run(client, "chunk", files={"text": (None, LONG_TEXT.encode())}, **form)
    else:
        answer = run(client, "chunk", text=LONG_TEXT, **form)

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/html")
    root = tree(answer.text)
    assert texts(root, "//*[@class='http-status']") == ["HTTP 200"]
    # 200 chunks of about 7,600 characters: the fragment shows the first ones.
    assert "174 more chunks not shown; see the download." in texts(root, "//p[@class='cut-note']")


def test_chunk_tab_posts_multipart() -> None:
    """The Chunk tab posts its text unencoded, so a text takes no more bytes than it has."""
    with running(auth_mode="none") as open_client:
        root = lxml.html.fromstring(open_client.get("/ui").text)
    (form,) = root.xpath("//form[@hx-post='/ui/run/chunk']")
    assert form.get("hx-encoding") == "multipart/form-data"


@pytest.mark.parametrize(
    ("op", "content_type", "body", "cause"),
    [
        ("chunk", "multipart/form-data", b"text=x", "Missing boundary in multipart."),
        (
            "file",
            "multipart/form-data; boundary=b",
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\nx\r\n'
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="b.txt"\r\n\r\ny\r\n'
            b"--b--\r\n",
            "Too many files. Maximum number of files is 1.",
        ),
    ],
    ids=["malformed", "files"],
)
def test_a_form_that_cannot_be_read_is_a_fragment(
    client: TestClient, op: str, content_type: str, body: bytes, cause: str
) -> None:
    answer = client.post(
        f"/ui/run/{op}",
        content=body,
        headers={
            "HX-Request": "true",
            "Authorization": f"Bearer {USER_KEY}",
            "Content-Type": content_type,
        },
    )

    assert answer.status_code == 200
    assert texts(tree(answer.text), "//div[@class='error']/p") == [
        f"The form could not be read: {cause}"
    ]
