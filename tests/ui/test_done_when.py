"""Package 10's done-when test for the UI: every entry of ``OPERATIONS`` and the views
``progress``, ``site-tree``, ``batch-file``, ``batch-urls`` and ``images``, posted to
``/ui/run`` as the page's htmx posts them, against the setup of the MCP done-when test
(``tests/mcp_server/test_done_when.py``: the app under uvicorn, a real extract worker, the
sample site on pytest-httpserver). The worker completes the batches and the site job; the API
pod completes a P1 job. With the browser off, ``screenshot`` shows the REST 400, and
``artifact`` shows an artifact stored for a job. Every REST call the views forward is recorded
with its answer: the tests prove which entries of ``OPERATIONS`` they drove, and check the REST
status and envelope of each, in the fragment where it shows them (the single operations, the
job item, a refusal) and at the forward otherwise (the job views: their fragments show the jobs
started, the image or the "cancelled" line).

Package 12 runs both tests in English and German (``X-UI-Language``, ruling P12-S6): every
fragment answers in the language asked for, and the texts the tests read come from that
language's catalogue. An admin drives the "Site rules" choice (ruling P12-S3) in both
languages."""

import json
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final, cast

import httpx
import lxml.html
import pytest
from fakeredis import FakeAsyncRedis
from pytest_httpserver import HTTPServer

from tests.mcp_server.test_done_when import (
    ADMIN_KEY,
    PDF,
    PNG,
    STRUCTURED,
    SUBPAGES,
    TITLE,
    USER_KEY,
    Live,
    serving,
)
from text_extraction.jobs import JobStore
from text_extraction.ui import answers, job_views, routes
from text_extraction.ui.forms import ACCESS_FIELD, OPERATIONS
from text_extraction.ui.forward import Forwarded
from text_extraction.ui.forward import forward as real_forward
from text_extraction.ui.i18n import LANGUAGE_HEADER, LANGUAGES, Language, translate

HEADERS = {"HX-Request": "true", "Authorization": f"Bearer {USER_KEY}"}
ADMIN_HEADERS = {**HEADERS, "Authorization": f"Bearer {ADMIN_KEY}"}
LICENSE = "https://creativecommons.org/licenses/by-sa/4.0/"
DONE_WITHIN_SECONDS = 60


# The entries of OPERATIONS each test below drives.
SINGLE_DRIVEN: Final = frozenset(
    {"login", "info", "extract", "file", "screenshot", "structured", "chunk", "job"}
)
JOBS_DRIVEN: Final = frozenset({"batch", "site-structure", "job", "artifact", "cancel"})


@dataclass(frozen=True)
class Call:
    """One REST call a ``/ui/run`` view forwarded, with its answer."""

    view: str
    method: str
    path: str
    status: int
    body: dict[str, Any] | None
    content_type: str


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Every REST call the views forward while the test runs (the server runs in this
    process)."""
    calls: list[Call] = []

    async def recording(request: Any, method: Any, path: str, **kwargs: Any) -> Forwarded:
        answer = await real_forward(request, method, path, **kwargs)
        calls.append(
            Call(request.url.path, method, path, answer.status, answer.body, answer.content_type)
        )
        return answer

    for module in (answers, job_views, routes):
        monkeypatch.setattr(module, "forward", recording)
    return calls


def _route(path: str) -> re.Pattern[str]:
    return re.compile(re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(path)))


def driven(calls: Iterable[Call]) -> set[str]:
    """The entries of ``OPERATIONS`` whose REST call a view forwarded. Entries that share a
    route (login and info) count only from their own view, ``/ui/run/<name>``."""
    shared = {
        name
        for name, op in OPERATIONS.items()
        for other, theirs in OPERATIONS.items()
        if other != name and (op.method, op.path) == (theirs.method, theirs.path)
    }
    return {
        name
        for name, op in OPERATIONS.items()
        for call in calls
        if call.method == op.method
        and _route(op.path).fullmatch(call.path)
        and (name not in shared or call.view == f"/ui/run/{name}")
    }


def calls_of(calls: Iterable[Call], name: str) -> list[Call]:
    op = OPERATIONS[name]
    return [c for c in calls if c.method == op.method and _route(op.path).fullmatch(c.path)]


@pytest.fixture
def live(httpserver: HTTPServer) -> Iterator[Live]:
    with serving(httpserver) as running:
        yield running


@pytest.fixture(params=LANGUAGES)
def lang(request: pytest.FixtureRequest) -> Language:
    """The UI language a test runs in: each test runs once per language."""
    language: Language = request.param
    return language


def client_of(live: Live, lang: Language, headers: dict[str, str]) -> httpx.Client:
    """A client of the running service that sends what the page's htmx sends in ``lang``."""
    return httpx.Client(
        base_url=live.base_url, headers={**headers, LANGUAGE_HEADER: lang}, timeout=60
    )


@pytest.fixture
def ui(live: Live, lang: Language) -> Iterator[httpx.Client]:
    with client_of(live, lang, HEADERS) as client:
        yield client


def tree(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def texts(root: Any, xpath: str) -> list[str]:
    return [" ".join(node.text_content().split()) for node in root.xpath(xpath)]


def language_of(ui: httpx.Client) -> Language:
    return cast(Language, ui.headers[LANGUAGE_HEADER])


def fragment_of(ui: httpx.Client, response: httpx.Response) -> Any:
    """The parsed fragment of an answer, which is in the language the client asked for."""
    assert response.status_code == 200, response.text
    assert response.headers["content-language"] == ui.headers[LANGUAGE_HEADER]
    return tree(response.text)


def post(ui: httpx.Client, op: str, files: Any = None, **fields: Any) -> Any:
    """POST a form to ``/ui/run/<op>``; the parsed fragment (always HTTP 200, so htmx swaps
    it)."""
    return fragment_of(ui, ui.post(f"/ui/run/{op}", data=fields, files=files))


def get(ui: httpx.Client, path: str, **params: Any) -> Any:
    """GET a view; ``path`` may carry its query (a link of a fragment), else ``params`` do."""
    return fragment_of(ui, ui.get(path, params=params or None))


def shown_answer(fragment: Any) -> tuple[str, dict[str, Any]]:
    """The REST answer a result fragment shows: its HTTP status line and its envelope (the raw
    JSON block)."""
    [status] = texts(fragment, "//*[@class='http-status']")
    raw = fragment.xpath("//details[@class='raw']/pre")
    assert raw, lxml.html.tostring(fragment)
    envelope: dict[str, Any] = json.loads(raw[0].text_content())
    return status, envelope


def job_ids(fragment: Any) -> list[str]:
    return [str(job_id) for job_id in fragment.xpath("//section[@class='job']/@data-job")]


def wait_completed(ui: httpx.Client, ids: list[str]) -> Any:
    """Refresh the progress view of ``ids`` as the page does until every job is completed; the
    last fragment. The state badges read as the client's language says."""
    assert ids
    completed = translate(language_of(ui), "job_state.completed")
    deadline = time.monotonic() + DONE_WITHIN_SECONDS
    while True:
        fragment = get(ui, "/ui/run/progress", id=ids, live="1")
        states = texts(fragment, "//section[@class='job']//span[contains(@class, 'state')]")
        if states == [completed] * len(ids):
            # Refreshing stops once every job shown is completed.
            assert fragment.xpath("//div[@class='progress-live']")[0].get("hx-trigger") is None
            return fragment
        assert time.monotonic() < deadline, states
        time.sleep(0.2)


def rows(fragment: Any, job_id: str) -> list[list[str]]:
    return [
        [" ".join(cell.text_content().split()) for cell in row.xpath("./td")]
        for row in fragment.xpath(
            f"//section[@data-job='{job_id}']//table[@class='items']/tbody/tr"
        )
    ]


def page_urls(live: Live) -> list[str]:
    return [f"{live.origin}/{name}.html" for name in SUBPAGES]


def extract_fields(url: str, **values: str) -> dict[str, str]:
    """The extract form as the page posts it by default (Markdown, metadata on, wait 25)."""
    fields = {"url": url, "format": "markdown", "mode": "auto", "metadata": "on"}
    fields["wait_seconds"] = "25"
    fields.update(values)
    return fields


def batch_options() -> dict[str, str]:
    return {"format": "markdown", "mode": "auto", "metadata": "on"}


def test_operations_cover_every_rest_route() -> None:
    """The two tests below together drive every entry of ``OPERATIONS``: each proves that it
    drove its set (from the calls it forwarded), so an entry added to ``OPERATIONS`` fails here
    until a test drives it."""
    assert set(OPERATIONS) == SINGLE_DRIVEN | JOBS_DRIVEN


def test_driven_tells_login_from_info() -> None:
    def call(view: str, method: str, path: str) -> Call:
        return Call(view, method, path, 200, None, "application/json")

    assert driven([call("/ui/run/login", "GET", "/v1/info")]) == {"login"}
    assert driven([call("/ui/run/job", "GET", "/v1/jobs/" + "a" * 32)]) == {"job"}
    assert driven([call("/ui/run/cancel", "DELETE", "/v1/jobs/" + "a" * 32)]) == {"cancel"}
    assert driven([call("/ui/run/x", "GET", "/v1/jobs/a/b")]) == set()


def test_single_operations_and_previews(
    live: Live, ui: httpx.Client, forwarded: list[Call], lang: Language
) -> None:
    origin = live.origin

    # login and info: GET /v1/info with the browser's key.
    shell = post(ui, "login")
    assert shell.xpath("//*[@id='app-shell']")
    assert "alice" in texts(shell, "//*[@class='who']")[0]
    refused = tree(
        httpx.post(
            f"{live.base_url}/ui/run/login",
            headers={
                "HX-Request": "true",
                "Authorization": "Bearer wrong-key-0123456789",
                LANGUAGE_HEADER: lang,
            },
        ).text
    )
    assert texts(refused, "//*[contains(@class, 'error')]") == [translate(lang, "login.not_valid")]
    assert not refused.xpath("//*[@id='app-shell']")
    status, envelope = shown_answer(post(ui, "info"))
    assert (status, envelope["status"]) == ("HTTP 200", "success")
    assert (envelope["data"]["principal"], envelope["data"]["role"]) == ("alice", "user")

    # extract, as Markdown with structured metadata and as Text.
    markdown = post(ui, "extract", **extract_fields(f"{origin}/", metadata_structured="on"))
    status, envelope = shown_answer(markdown)
    assert (status, envelope["status"]) == ("HTTP 200", "success")
    assert texts(markdown, "//h3[@class='title']") == [TITLE]
    assert TITLE in texts(markdown, "//pre[@class='content']")[0]
    assert markdown.xpath("//a[@class='download']")[0].get("download").endswith(".md")
    values = dict(
        zip(
            texts(markdown, "//table[@class='values']/tbody/tr/th"),
            texts(markdown, "//table[@class='values']/tbody/tr/td[@class='value']"),
            strict=True,
        )
    )
    assert (values["name"], values["license"]) == (TITLE, LICENSE)
    assert texts(markdown, "//table[@class='images']//td[@class='alt']") == ["Ein Bruch als Kreis"]
    steps = texts(markdown, "//ol[@class='timeline']/li")
    assert steps
    assert steps[-1].startswith(translate(lang, "timeline.outcome"))
    text = post(ui, "extract", **extract_fields(f"{origin}/", format="text"))
    status, envelope = shown_answer(text)
    assert (status, envelope["data"]["format"]) == ("HTTP 200", "text")
    assert text.xpath("//a[@class='download']")[0].get("download").endswith(".txt")

    # images: "Load previews" posts the result's image URLs; the server fetches and inlines them.
    form = markdown.xpath("//form[@class='previews']")[0]
    preview_fields: dict[str, list[str]] = {}
    for field in form.xpath(".//input[@type='hidden']"):
        preview_fields.setdefault(field.get("name"), []).append(field.get("value"))
    assert preview_fields["url"] == [f"{origin}/bild.png"]
    previews = post(ui, "images", **preview_fields)
    [image] = previews.xpath("//figure[@class='preview']/img")
    assert image.get("src").startswith("data:image/png;base64,")
    assert image.get("alt") == "Ein Bruch als Kreis"

    # file: a PDF upload.
    uploaded = post(
        ui,
        "file",
        files={"file": ("arbeitsblatt.pdf", PDF, "application/pdf")},
        format="markdown",
        metadata="on",
        wait_seconds="25",
    )
    status, envelope = shown_answer(uploaded)
    assert (status, envelope["status"]) == ("HTTP 200", "success")
    assert envelope["data"]["document"]["kind"] == "pdf"

    # screenshot: the browser is off, so the REST 400.
    shot = post(ui, "screenshot", url=f"{origin}/", full_page="on", wait_seconds="25")
    status, envelope = shown_answer(shot)
    assert (status, envelope["status"], envelope["error"]["code"]) == (
        "HTTP 400",
        "failed",
        "invalid_request",
    )
    assert texts(shot, "//span[@class='error-code']") == ["invalid_request"]

    # structured and chunk.
    structured = post(
        ui,
        "structured",
        url=f"{origin}/",
        format="markdown",
        mode="auto",
        structured=json.dumps(STRUCTURED),
        wait_seconds="25",
    )
    status, envelope = shown_answer(structured)
    assert (status, envelope["data"]["structured"]["values"]["name"]) == ("HTTP 200", TITLE)
    chunked = post(
        ui,
        "chunk",
        url=f"{origin}/a.html",
        format="markdown",
        chunking=json.dumps({"strategy": "recursive", "size": 200}),
        wait_seconds="25",
    )
    status, envelope = shown_answer(chunked)
    assert (status, envelope["status"]) == ("HTTP 200", "success")
    assert len(chunked.xpath("//ol[@class='chunks']/li")) == len(envelope["data"]["chunks"]) > 1

    # A P1 job: wait_seconds 0 hands the extraction over to a job in the API pod.
    handed = post(ui, "extract", **extract_fields(f"{origin}/langsam.html", wait_seconds="0"))
    status, envelope = shown_answer(handed)
    assert (status, envelope["data"]["kind"]) == ("HTTP 202", "extract")
    job_id = envelope["data"]["id"]
    progress_url = handed.xpath("//button[contains(@class, 'progress-button')]")[0].get("hx-get")
    assert get(ui, progress_url).xpath(f"//section[@data-job='{job_id}']")
    done = wait_completed(ui, [job_id])
    assert rows(done, job_id)[0][:3] == [
        "0",
        f"{origin}/langsam.html",
        translate(lang, "status.success"),
    ]
    item = get(ui, "/ui/run/job", id=job_id, index=0)
    assert texts(item, "//h3[@class='title']") == ["Einheit langsam"]
    status, envelope = shown_answer(item)  # the item's result
    assert (status, envelope["status"], envelope["data"]["url"]) == (
        "HTTP 200",
        "success",
        f"{origin}/langsam.html",
    )
    steps = texts(item, "//ol[@class='timeline']/li")
    assert not [step for step in steps if "claim" in step]  # it ran in the API pod

    assert driven(forwarded) >= SINGLE_DRIVEN


def test_batches_site_tree_and_cancel(
    live: Live, ui: httpx.Client, forwarded: list[Call], lang: Language
) -> None:
    origin = live.origin
    success = translate(lang, "status.success")
    urls = page_urls(live)

    # batch: the batch form posts its lines to batch-urls, which forwards POST /v1/batch.
    started = post(ui, "batch-urls", urls="\n".join(urls), **batch_options())
    [batch_id] = job_ids(started)
    [batch_call] = calls_of(forwarded, "batch")
    assert (batch_call.view, batch_call.status) == ("/ui/run/batch-urls", 202)
    assert batch_call.body is not None
    assert (batch_call.body["status"], batch_call.body["data"]["id"]) == ("success", batch_id)
    done = wait_completed(ui, [batch_id])
    assert [row[:3] for row in rows(done, batch_id)] == [
        [str(index), url, success] for index, url in enumerate(urls)
    ]
    item = get(ui, "/ui/run/job", id=batch_id, index=1, mode="auto")
    status, envelope = shown_answer(item)  # the item's result
    assert (status, envelope["status"], envelope["data"]["url"]) == ("HTTP 200", "success", urls[1])
    claimed = translate(lang, "timeline.claim_detail", pool="extract", attempt=1, when="\0")
    assert "extract" in claimed.split("\0")[0]  # the prefix names the pool, so it says something
    assert claimed.split("\0")[0] in " ".join(texts(item, "//ol[@class='timeline']/li"))
    assert texts(item, "//h3[@class='title']") == ["Einheit b"]

    # artifact: a screenshot stored for the job, read from the artifact route.
    async def store_artifact(valkey: FakeAsyncRedis) -> None:
        await JobStore(valkey, live.settings).put_artifact(batch_id, "screenshot.png", PNG)

    live.run(store_artifact)
    artifact = get(ui, "/ui/run/artifact", id=batch_id, name="screenshot.png")
    assert (
        artifact.xpath("//img[@class='screenshot']")[0]
        .get("src")
        .startswith("data:image/png;base64,")
    )
    [artifact_call] = calls_of(forwarded, "artifact")
    assert (artifact_call.view, artifact_call.status) == ("/ui/run/artifact", 200)
    assert (artifact_call.content_type, artifact_call.body) == ("image/png", None)

    # site-structure, its progress and its tree; "Extract selected" starts a batch.
    site = post(
        ui,
        "site-structure",
        url=f"{origin}/",
        depth="1",
        max_pages="100",
        scope="host",
        sitemaps="on",
    )
    [site_id] = job_ids(site)
    [site_call] = calls_of(forwarded, "site-structure")
    assert (site_call.view, site_call.status) == ("/ui/run/site-structure", 202)
    assert site_call.body is not None
    assert (site_call.body["status"], site_call.body["data"]["id"]) == ("success", site_id)
    assert site_call.body["data"]["kind"] == "site"
    crawled = wait_completed(ui, [site_id])
    assert texts(crawled, f"//section[@data-job='{site_id}']//span[@class='kind']") == ["site"]
    tree_url = crawled.xpath("//button[contains(@class, 'tree-button')]")[0].get("hx-get")
    site_tree = get(ui, tree_url)
    checkable = set(site_tree.xpath("//ul[@class='tree']//input[@name='url']/@value"))
    assert {f"{origin}/", *urls} <= checkable
    assert translate(lang, "tree.snapshot") not in texts(site_tree, "//*[@class='hint']")
    selected = urls[:2]
    from_tree = post(ui, "batch-urls", url=selected, **batch_options())  # the checked boxes
    [tree_batch] = job_ids(from_tree)
    done = wait_completed(ui, [tree_batch])
    assert [row[1] for row in rows(done, tree_batch)] == selected

    # batch-file: a CSV with a bad row and a duplicate, previewed, then started.
    csv = "url\n" + "\n".join(urls) + f'\n{urls[0]}\n=HYPERLINK("http://evil.example")\n'
    preview = post(ui, "batch-file", files={"file": ("urls.csv", csv.encode(), "text/csv")})
    counts = {
        name: texts(preview, f"//dd[@class='{name}']")[0]
        for name in ("rows", "urls", "invalid", "duplicates", "parts")
    }
    assert counts == {"rows": "5", "urls": "3", "invalid": "1", "duplicates": "1", "parts": "1"}
    listed = preview.xpath("//textarea[@name='urls']")[0].text_content()
    from_file = post(ui, "batch-urls", urls=listed, **batch_options())
    [file_batch] = job_ids(from_file)
    done = wait_completed(ui, [file_batch])
    assert [row[1] for row in rows(done, file_batch)] == urls

    # One progress view of several jobs.
    together = wait_completed(ui, [batch_id, site_id, tree_batch, file_batch])
    assert job_ids(together) == [batch_id, site_id, tree_batch, file_batch]

    # cancel: DELETE /v1/jobs/{id}; the job is gone afterwards.
    cancelled = post(ui, "cancel", id=file_batch)
    assert file_batch in texts(cancelled, "//p[@class='cancelled']")[0]
    [cancel_call] = calls_of(forwarded, "cancel")
    assert (cancel_call.view, cancel_call.status) == ("/ui/run/cancel", 200)
    assert cancel_call.body is not None
    assert (cancel_call.body["status"], cancel_call.body["data"]["id"]) == ("success", file_batch)
    gone = get(ui, "/ui/run/job", id=file_batch, index=0)
    status, envelope = shown_answer(gone)
    assert (status, envelope["error"]["code"]) == ("HTTP 404", "not_found")

    assert driven(forwarded) >= JOBS_DRIVEN


@pytest.fixture
def unblock_live(httpserver: HTTPServer) -> Iterator[Live]:
    """The setup above with browser mode and unblock mode on, so an admin's "Site rules" choice
    is enabled. No browser worker runs: the sample site's pages need no browser."""
    with serving(httpserver, browser_enabled=True, browser_unblock_enabled=True) as running:
        yield running


def facts(fragment: Any) -> dict[str, str]:
    """The result view's facts (``dl.facts``): each term with its value."""
    return dict(
        zip(
            texts(fragment, "//dl[@class='facts']/dt"),
            texts(fragment, "//dl[@class='facts']/dd"),
            strict=True,
        )
    )


def test_access_choice_as_an_admin(unblock_live: Live, lang: Language) -> None:
    origin = unblock_live.origin
    with (
        client_of(unblock_live, lang, ADMIN_HEADERS) as admin,
        client_of(unblock_live, lang, HEADERS) as user,
    ):
        # The admin sees the choice, enabled, respect first; a user does not see it.
        shell = post(admin, "login")
        [group] = shell.xpath(
            "//section[@data-panel='extract']//fieldset[contains(@class, 'access')]"
        )
        assert group.get("disabled") is None
        assert texts(group, "./legend") == [translate(lang, "access.legend")]
        radios = group.xpath(f".//input[@name='{ACCESS_FIELD}']")
        assert [radio.get("value") for radio in radios] == ["respect", "maximum"]
        assert [radio.get("disabled") for radio in radios] == [None, None]
        assert radios[0].get("checked") is not None
        assert not post(user, "login").xpath(f"//input[@name='{ACCESS_FIELD}']")

        # respect sends no compliance (the default applies), maximum sends "ignore"; the result
        # view shows the mode. The pages need no browser, so nothing is unblocked.
        for access, mode in (("respect", "enforce"), ("maximum", "ignore")):
            shown = post(admin, "extract", **extract_fields(f"{origin}/", access=access))
            status, envelope = shown_answer(shown)
            assert (status, envelope["status"]) == ("HTTP 200", "success"), access
            assert envelope["data"]["compliance"]["mode"] == mode
            assert facts(shown)[translate(lang, "access.compliance")] == mode
            assert envelope["data"]["unblocked"] is False
            assert not shown.xpath("//*[contains(@class, 'badge-unblocked')]")

        # A user who forges maximum gets REST's 403.
        forged = post(user, "extract", **extract_fields(f"{origin}/", access="maximum"))
        assert texts(forged, "//*[@class='http-status']") == ["HTTP 403"]
        assert texts(forged, "//*[@class='error-code']") == ["forbidden"]
