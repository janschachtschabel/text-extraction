"""edu-sharing render pages through the repository's REST API (package 22, ``edusharing.py``,
``pipeline_edusharing.py``): a render URL is answered from its node's metadata and its file,
not from the Angular shell the page serves.

The metadata answers are synthetic, of the shape the repository's
``/rest/node/v1/nodes/-home-/<id>/metadata`` answers. One test converts an SVG in the
module's MarkItDown child; the others answer each conversion with a fake pool."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.fakes import PARAGRAPH, FakeRenderer, fetched, rendered
from tests.test_pipeline_documents import USER, FakePool, Harness, Loop, data_of, reply
from text_extraction.convert.attachments import Attachment
from text_extraction.convert.html import convert_html
from text_extraction.edusharing import metadata_url, node_page, parse_metadata, render_node
from text_extraction.envelope import Envelope
from text_extraction.escalation import needs_browser
from text_extraction.extract_models import ExtractRequest, ExtractResult
from text_extraction.fetch import FetchResult, Validators

NODE = "4aaee76c-ad4b-4540-b9ee-cb4789d38491"
RLP = "https://cloud.schulcampus-rlp.de"
BASE = f"{RLP}/edu-sharing"
RENDER = f"{BASE}/components/render/{NODE}"
METADATA = f"{BASE}/rest/node/v1/nodes/-home-/{NODE}/metadata?propertyFilter=-all-"
DOWNLOAD = f"{BASE}/eduservlet/download?nodeId={NODE}"
TWILLO_NODE = "0b5c7e3a-1f2d-4c6b-9a8e-7d6c5b4a3f21"
TWILLO = f"https://www.twillo.de/edu-sharing/components/render/{TWILLO_NODE}"
SVG_NAME = "Zusammenfassung Kupfer mit Iod.svg"
SVG_TYPE = "image/svg+xml"
DESCRIPTION = (
    "Die Grafik fasst die Reaktion von Kupfer mit Iod zusammen und zeigt die Teilchenebene "
    "nach dem Atommodell von Dalton."
)
FIRST = "Kupfer und Iod reagieren in einer chemischen Reaktion zu Kupferiodid."
SECOND = (
    "Nach Dalton bestehen alle Stoffe aus kleinsten Teilchen, die sich bei der Reaktion neu "
    "anordnen, aber nicht verloren gehen oder neu entstehen."
)
SHELL = (
    "<!doctype html><html><head><title>edu-sharing</title>"
    '<script src="/edu-sharing/main.js"></script></head>'
    "<body><es-app></es-app><noscript>Bitte JavaScript aktivieren.</noscript></body></html>"
)
RICH = (
    '<!doctype html><html lang="de"><head><title>Gerendert</title></head><body><main>'
    f"<h1>Gerendert</h1><p>{PARAGRAPH * 4}</p></main></body></html>"
)
SVG = (
    '<?xml version="1.0" encoding="UTF-8"?><svg xmlns="http://www.w3.org/2000/svg">'
    f'<path d="M0 0L9 9"/><text x="1" y="2">{FIRST}</text>'
    f'<g><text x="3" y="4">{SECOND}</text></g></svg>'
).encode()


def metadata(**node: Any) -> dict[str, Any]:
    """A metadata answer of the rlp node's shape; ``node`` replaces its fields."""
    values: dict[str, Any] = {
        "ref": {"repo": "local", "id": NODE},
        "title": SVG_NAME,
        "name": SVG_NAME,
        "mimetype": "image/svg+xml",
        "mediatype": "file-image",
        "size": "2097152",
        "downloadUrl": DOWNLOAD,
        "access": ["Read", "ReadAll", "Embed", "Feedback", "DownloadContent"],
        "properties": {
            "cclom:title": [SVG_NAME],
            "cclom:general_description": [DESCRIPTION],
            "cclom:general_keyword": ["Atommodell", "Dalton", "Chemische Reaktion"],
            "ccm:wwwurl": ["https://www.example.org/kupfer-iod"],
            "ccm:commonlicense_key": ["CC_BY_SA"],
        },
    }
    values.update(node)
    return {"node": values}


def json_answer(body: object, *, url: str = METADATA, **options: Any) -> FetchResult:
    raw = body if isinstance(body, str) else json.dumps(body)
    options.setdefault("content_type", "application/json")
    return fetched(url, raw, **options)


def svg_node() -> dict[str, Any]:
    """The answers of the rlp node: its metadata and its SVG file."""
    return {
        METADATA: json_answer(metadata()),
        DOWNLOAD: fetched(DOWNLOAD, SVG, content_type=SVG_TYPE),
    }


# --- render_node, metadata_url ---


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (RENDER, (BASE, NODE)),
        (TWILLO, ("https://www.twillo.de/edu-sharing", TWILLO_NODE)),
        (f"{RENDER}/1.0", (BASE, NODE)),
        (f"{RENDER}?action=view&closeOnBack=true", (BASE, NODE)),
        (f"{RENDER}/", (BASE, NODE)),
        (f"{BASE}/components/render/{NODE.upper()}", (BASE, NODE)),
        (
            f"https://host.example:8443/repo/edu-sharing/components/render/{NODE}#top",
            ("https://host.example:8443/repo/edu-sharing", NODE),
        ),
    ],
)
def test_render_node(url: str, expected: tuple[str, str]) -> None:
    assert render_node(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/components/search",
        f"{BASE}/components/search?query=kupfer",
        f"{BASE}/components/render/not-a-node-id",
        f"{BASE}/components/render/{NODE}/1.0/more",
        f"{BASE}/components/collections?id={NODE}",
        f"https://user:secret@cloud.example/edu-sharing/components/render/{NODE}",
        f"ftp://cloud.example/edu-sharing/components/render/{NODE}",
        f"https://cloud.example/components/render/{NODE}",
    ],
)
def test_not_a_render_node(url: str) -> None:
    assert render_node(url) is None


def test_metadata_url() -> None:
    assert metadata_url(BASE, NODE) == METADATA


# --- parse_metadata ---


def test_parse_metadata_reads_the_node() -> None:
    parsed = parse_metadata(json.dumps(metadata()).encode())

    assert parsed is not None
    assert parsed["node"]["name"] == SVG_NAME


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"\xff\xfe\x00broken",
        b"[1, 2, 3]",
        b'{"nodes": []}',
        b'{"node": "text"}',
        b"[" * 100_000 + b"]" * 100_000,
        b"",
    ],
)
def test_parse_metadata_refuses_what_is_no_node(body: bytes) -> None:
    assert parse_metadata(body) is None


def test_parse_metadata_survives_a_recursion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def deep(_body: bytes) -> Any:
        raise RecursionError("too deep")

    monkeypatch.setattr("text_extraction.edusharing.json.loads", deep)

    assert parse_metadata(b'{"node": {}}') is None


# --- node_page ---


def test_node_page_of_the_rlp_shape() -> None:
    html, attachment = node_page(metadata())

    assert f"<h1>{SVG_NAME}</h1>" in html
    assert f"<title>{SVG_NAME}</title>" in html
    assert DESCRIPTION in html
    assert "Atommodell, Dalton, Chemische Reaktion" in html
    assert "CC_BY_SA" in html
    assert '<a href="https://www.example.org/kupfer-iod">' in html
    assert f'<a href="{DOWNLOAD.replace("&", "&amp;")}">{SVG_NAME}</a>' in html
    assert attachment == Attachment(
        url=DOWNLOAD, label=SVG_NAME, mime="image/svg+xml", size=2097152
    )


@pytest.mark.parametrize(
    ("node", "heading"),
    [
        ({"title": "Titel"}, "Titel"),
        ({"title": None}, SVG_NAME),
        ({"title": "", "properties": {"cclom:title": ["Aus LOM"]}}, "Aus LOM"),
        ({"title": " ", "properties": {}}, SVG_NAME),
        ({"title": None, "name": None, "properties": {}}, None),
    ],
)
def test_node_page_heading(node: dict[str, Any], heading: str | None) -> None:
    html, _attachment = node_page(metadata(**node))

    if heading is None:
        assert "<h1>" not in html
    else:
        assert f"<h1>{heading}</h1>" in html


def test_node_page_escapes_every_value() -> None:
    evil = '<script>alert("x")</script>'
    meta = metadata(
        title=evil,
        name=f"{evil}.svg",
        mimetype=evil,
        properties={
            "cclom:general_description": [evil],
            "cclom:general_keyword": [evil],
            "ccm:commonlicense_key": [evil],
            "ccm:wwwurl": ['https://www.example.org/"><script>x</script>'],
        },
    )

    html, attachment = node_page(meta)

    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert '"><script>' not in html
    assert attachment is not None
    assert attachment.label == f"{evil}.svg"  # escaped where it is written, not here


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "ftp://files.example/x.svg",
        "https://user:secret@files.example/x.svg",
        "//files.example/x.svg",
        "https:///nohost",
        "",
    ],
)
def test_node_page_drops_unsafe_urls(url: str) -> None:
    meta = metadata(downloadUrl=url)
    meta["node"]["properties"]["ccm:wwwurl"] = [url]

    html, attachment = node_page(meta)

    assert attachment is None
    assert "href" not in html
    assert SVG_NAME in html  # the file is still named


def test_node_page_resolves_a_relative_download_url() -> None:
    relative = f"/edu-sharing/eduservlet/download?nodeId={NODE}"

    _html, attachment = node_page(metadata(downloadUrl=relative), base=RENDER)

    assert attachment is not None
    assert attachment.url == DOWNLOAD


def test_node_page_without_download_right_has_no_attachment() -> None:
    _html, attachment = node_page(metadata(access=["Read"]))

    assert attachment is None


@pytest.mark.parametrize("size", [None, "", "x", -5, True, "1e9"])
def test_node_page_ignores_an_odd_size(size: object) -> None:
    _html, attachment = node_page(metadata(size=size))

    assert attachment is not None
    assert attachment.size is None


def test_node_page_tolerates_odd_shapes() -> None:
    meta = metadata(
        title=["not", "a", "string"],
        properties={
            "cclom:general_keyword": "not a list",
            "cclom:general_description": [None, 3, "Text"],
        },
    )

    html, _attachment = node_page(meta)

    assert "<h1>" in html
    assert "Text" in html
    assert node_page({"node": {}})[1] is None


# --- the pipeline ---


@pytest.fixture(scope="module")
def loop() -> Iterator[Loop]:
    instance = Loop()
    try:
        yield instance
    finally:
        instance.close()


def run(harness: Harness, **request: Any) -> Envelope[ExtractResult]:
    request.setdefault("url", RENDER)
    request.setdefault("cache", False)
    return harness.run(**request)


def fetched_urls(harness: Harness) -> list[str]:
    return [url for url, _options in harness.fetcher.calls]


def options_of(harness: Harness, url: str) -> dict[str, Any]:
    (options,) = [options for fetched_url, options in harness.fetcher.calls if fetched_url == url]
    return options


def browser_harness(answers: dict[str, Any], loop: Loop | None = None, **overrides: Any) -> Harness:
    """A harness whose ``auto`` requests can escalate to a renderer of a rich page."""
    pool = None if loop is not None else FakePool(reply(content="# Datei\n\n" + PARAGRAPH * 2))
    return Harness(
        loop,
        answers,
        pool=pool,
        renderer=FakeRenderer(rendered(RICH, url=RENDER)),
        browser_enabled=True,
        **overrides,
    )


def test_svg_node_answers_from_metadata_and_file(loop: Loop) -> None:
    harness = browser_harness({RENDER: fetched(RENDER, SHELL), **svg_node()}, loop)

    envelope = run(harness)

    assert envelope.status == "success", envelope.error
    data = data_of(envelope)
    content = data.content or ""
    assert data.mode_used == "fast"
    assert data.url == RENDER
    assert content.startswith(f"# {SVG_NAME}")
    assert DESCRIPTION in content
    assert "Atommodell" in content
    assert f"## {SVG_NAME}" in content
    assert FIRST in content
    assert SECOND in content
    assert data.warnings.count("attachment_read") == 1
    assert data.quality is not None
    assert data.word_count == data.quality.word_count >= harness.settings.quality_min_words
    assert data.fingerprint is not None
    assert data.title == SVG_NAME
    # Not the Angular shell, no render; the metadata as JSON within CONVERT_MAX_BYTES, the file
    # as a document within DOCUMENT_MAX_BYTES, every turn given back.
    assert RENDER not in fetched_urls(harness)
    assert harness.pipeline._renderer is not None
    renderer = harness.pipeline._renderer
    assert isinstance(renderer, FakeRenderer)
    assert renderer.calls == []
    assert options_of(harness, METADATA)["accept"] == "application/json"
    assert options_of(harness, METADATA)["max_bytes"] == harness.settings.convert_max_bytes
    assert options_of(harness, DOWNLOAD)["max_bytes"] == harness.settings.document_max_bytes
    assert harness.gate.held() == []
    assert not needs_browser(envelope, unblock=True)


def test_node_answer_is_cached_like_a_page() -> None:
    harness = browser_harness(svg_node())

    first = run(harness, cache=True)
    second = run(harness, cache=True)

    assert first.status == "success", first.error
    assert data_of(second).cache == "hit"
    assert data_of(second).content == data_of(first).content
    assert fetched_urls(harness).count(METADATA) == 1


@pytest.mark.parametrize(
    ("fmt", "heading", "description"),
    [
        ("text", SVG_NAME, DESCRIPTION),
        ("html", f"<h2>{SVG_NAME}</h2>", "<h1>"),
    ],
)
def test_node_answer_in_each_format(fmt: str, heading: str, description: str) -> None:
    harness = browser_harness(svg_node())

    content = data_of(run(harness, format=fmt)).content or ""

    assert heading in content
    assert description in content
    assert "##" not in content


def shell_answers(answer: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {RENDER: fetched(RENDER, SHELL)}
    if answer is not None:
        answers[METADATA] = answer
    return answers


FALLBACKS = {
    "unauthorized": json_answer({"error": "auth"}, status=401),
    "not_found": json_answer({"error": "missing"}, status=404),
    "html": fetched(METADATA, "<html><body>Login</body></html>"),
    "broken_json": json_answer('{"node": {"title": '),
    "not_a_node": json_answer([1, 2]),
    "server_error": json_answer({"error": "boom"}, status=500),
}


@pytest.mark.parametrize("bad", sorted(FALLBACKS))
def test_metadata_failure_falls_back_to_the_page(bad: str) -> None:
    fallback = browser_harness(shell_answers(FALLBACKS[bad]))

    envelope = run(fallback)

    data = data_of(envelope)
    assert METADATA in fetched_urls(fallback)
    assert RENDER in fetched_urls(fallback)  # today's path: the shell ...
    renderer = fallback.pipeline._renderer
    assert isinstance(renderer, FakeRenderer)
    assert [call.url for call in renderer.calls] == [RENDER]  # ... which escalates
    assert data.mode_used == "browser"
    assert envelope.status == "success", envelope.error
    assert DOWNLOAD not in fetched_urls(fallback)
    assert not any(code.startswith("attachment_") for code in data.warnings)
    assert envelope.error is None
    assert fallback.gate.held() == []


@pytest.mark.parametrize("bad", ["not_found", "broken_json"])
def test_fast_fallback_answers_as_today(bad: str) -> None:
    fallback = browser_harness(shell_answers(FALLBACKS[bad]))

    envelope = run(fallback, mode="fast")

    data = data_of(envelope)
    assert data.final_url == RENDER
    assert data.content_type == "text/html"
    assert data.upstream_status == 200
    assert data.mode_used == "fast"
    assert envelope.error is not None
    assert envelope.error.code.value in {"content_too_short", "empty_content"}


def test_metadata_blocked_by_robots_falls_back() -> None:
    robots = fetched(f"{RLP}/robots.txt", "User-agent: *\nDisallow: /edu-sharing/rest/")
    answers = shell_answers(json_answer(metadata()))
    answers[f"{RLP}/robots.txt"] = robots
    harness = browser_harness(answers)

    data = data_of(run(harness, mode="fast", compliance="enforce"))

    assert METADATA not in fetched_urls(harness)
    assert RENDER in fetched_urls(harness)
    assert data.final_url == RENDER


def test_render_url_blocked_by_robots_is_blocked_as_today() -> None:
    robots = fetched(f"{RLP}/robots.txt", "User-agent: *\nDisallow: /edu-sharing/components/")
    answers = shell_answers(json_answer(metadata()))
    answers[f"{RLP}/robots.txt"] = robots
    harness = browser_harness(answers)

    envelope = run(harness, mode="fast", compliance="enforce")

    assert envelope.status == "failed"
    assert envelope.error is not None
    assert envelope.error.code.value == "blocked_by_robots"
    assert METADATA not in fetched_urls(harness)
    assert RENDER not in fetched_urls(harness)


def test_mp3_node_lists_its_file() -> None:
    meta = metadata(
        title="Podcast Folge 3",
        name="folge-3.mp3",
        mimetype="audio/mpeg",
        mediatype="file-audio",
        size="17825792",
    )
    harness = browser_harness({RENDER: fetched(RENDER, SHELL), METADATA: json_answer(meta)})

    envelope = run(harness, links=True)

    data = data_of(envelope)
    content = data.content or ""
    assert data.mode_used == "fast"
    assert content.startswith("# Podcast Folge 3")
    assert "folge-3.mp3" in content
    assert DOWNLOAD not in fetched_urls(harness)  # not a document kind: listed, not fetched
    assert RENDER not in fetched_urls(harness)
    assert not any(code.startswith("attachment_") for code in data.warnings)
    assert data.links is not None
    assert DOWNLOAD in [link.url for link in data.links]
    renderer = harness.pipeline._renderer
    assert isinstance(renderer, FakeRenderer)
    assert renderer.calls == []  # a thin node answer never goes to the browser
    assert envelope.status == "partial"
    assert not needs_browser(envelope, unblock=True)


def test_download_on_another_host_is_listed_not_fetched() -> None:
    other = f"https://files.example.net/download?nodeId={NODE}"
    harness = browser_harness({METADATA: json_answer(metadata(downloadUrl=other))})

    data = data_of(run(harness, links=True))

    assert other not in fetched_urls(harness)
    assert data.links is not None
    assert other in [link.url for link in data.links]
    assert not any(code.startswith("attachment_") for code in data.warnings)


def test_failed_file_keeps_the_node_page_with_attachment_failed() -> None:
    harness = browser_harness(
        {METADATA: json_answer(metadata()), DOWNLOAD: fetched(DOWNLOAD, "Gone", status=404)}
    )

    envelope = run(harness)

    data = data_of(envelope)
    assert data.mode_used == "fast"
    assert DESCRIPTION in (data.content or "")
    assert data.warnings.count("attachment_failed") == 1


def test_transient_file_failure_is_not_cached() -> None:
    harness = browser_harness(
        {METADATA: json_answer(metadata()), DOWNLOAD: fetched(DOWNLOAD, "Busy", status=503)}
    )

    envelope = run(harness, cache=True)

    assert data_of(envelope).warnings.count("attachment_failed") == 1
    assert data_of(envelope).fingerprint is not None
    assert harness.keys("res:*") == []


def test_permanent_file_failure_is_cached() -> None:
    harness = browser_harness(
        {METADATA: json_answer(metadata()), DOWNLOAD: fetched(DOWNLOAD, "Gone", status=404)}
    )

    run(harness, cache=True)

    assert harness.keys("res:*") != []


def test_browser_request_renders_as_today() -> None:
    harness = browser_harness({METADATA: json_answer(metadata())})

    data = data_of(run(harness, mode="browser"))

    assert data.mode_used == "browser"
    assert METADATA not in fetched_urls(harness)


def test_hanging_metadata_leaves_today_its_time() -> None:
    async def hanging(_validators: Validators | None) -> FetchResult:
        await asyncio.sleep(30)
        return json_answer(metadata())

    harness = browser_harness(shell_answers(hanging), extract_timeout_seconds=8)

    envelope = run(harness, mode="fast")

    data = data_of(envelope)
    assert data.final_url == RENDER  # the shell answered, not a timeout
    assert envelope.error is None or envelope.error.code.value != "timeout"
    assert harness.gate.held() == []


def test_site_crawl_page_is_not_read_through_the_api() -> None:
    harness = browser_harness({**shell_answers(json_answer(metadata())), **svg_node()})
    request = ExtractRequest(url=RENDER, mode="fast", cache=False)

    envelope = harness._run(harness.pipeline.run(request, USER, site_links=True))

    assert METADATA not in fetched_urls(harness)
    assert data_of(envelope).final_url == RENDER


def test_request_with_browser_options_without_a_browser_takes_the_page() -> None:
    harness = Harness(None, {**shell_answers(json_answer(metadata())), **svg_node()})

    data = data_of(run(harness, mode="auto", headers={"X-Test": "1"}))

    assert METADATA not in fetched_urls(harness)
    assert data.final_url == RENDER


def test_a_bug_in_reading_the_node_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_meta: dict[str, Any], *, base: str | None = None) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr("text_extraction.pipeline_edusharing.node_page", broken)
    harness = browser_harness(shell_answers(json_answer(metadata())))

    data = data_of(run(harness, mode="fast"))

    assert RENDER in fetched_urls(harness)
    assert data.final_url == RENDER


def test_a_node_page_that_breaks_the_converter_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = convert_html

    def picky(html: str, *args: Any, **kwargs: Any) -> Any:
        if "<article>" in html:
            raise RuntimeError("bug")
        return real(html, *args, **kwargs)

    monkeypatch.setattr("text_extraction.pipeline.convert_html", picky)
    harness = browser_harness(shell_answers(json_answer(metadata())))

    envelope = run(harness, mode="fast")

    data = data_of(envelope)
    assert RENDER in fetched_urls(harness)
    assert data.final_url == RENDER
    assert data.content_type == "text/html"
