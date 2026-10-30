"""The UI's job views (rulings P10-S18 to P10-S20): progress, batches from lists and files, the
site tree and its selection, an item's result, the cancel button, and the operations of every
public route."""

import gc
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import lxml.html
import pytest
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from tests.fakes import PAGE
from tests.ui.conftest import AUTH, HX, run, running
from tests.ui.site_items import page_item, result_data, sitemap_item
from text_extraction import jobs_api, ratelimit
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.job_models import JobItem, JobProgress, JobView
from text_extraction.jobs import JobStore
from text_extraction.ui.forms import OPERATIONS
from text_extraction.ui.forward import forward
from text_extraction.ui.results import render_result

HEADERS = {**HX, **AUTH}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 120
ORIGIN = "https://example.org"


def tree(html: str) -> Any:
    return lxml.html.fragment_fromstring(html, create_parent="div")


def texts(root: Any, xpath: str) -> list[str]:
    return [" ".join(node.text_content().split()) for node in root.xpath(xpath)]


def urls(count: int, prefix: str = "https://example.org/page") -> list[str]:
    return [f"{prefix}-{number}" for number in range(count)]


def portal(client: TestClient) -> BlockingPortal:
    """The portal of the running app's event loop (fakeredis is bound to it)."""
    assert client.portal is not None
    return client.portal


def job_store(client: TestClient) -> JobStore:
    store: JobStore = cast(FastAPI, client.app).state.job_store
    return store


def start_batch(client: TestClient, listed: list[str]) -> str:
    """A batch started over REST; its job id."""
    response = client.post("/v1/batch", json={"urls": listed}, headers=AUTH)
    assert response.status_code == 202, response.text
    job_id: str = response.json()["data"]["id"]
    return job_id


def store_result(
    client: TestClient, job_id: str, index: int, *, error: ErrorCode | None = None, **data: Any
) -> None:
    """Store the result of item ``index`` as a worker does."""
    envelope = Envelope[ExtractResult](
        status="failed" if error else "success",
        data=ExtractResult.model_validate(result_data(f"{ORIGIN}/{index}", **data)),
        error=ErrorInfo(code=error, message="Gone.", retryable=False) if error else None,
    )
    portal(client).call(job_store(client).write_result, job_id, index, envelope)


def claim(client: TestClient, job_id: str, index: int, pool: str, attempts: int) -> None:
    portal(client).call(job_store(client).record_claim, job_id, index, pool, attempts)


def progress(client: TestClient, *ids: str, offsets: tuple[int, ...] = (), **extra: str) -> str:
    params: list[tuple[str, str | int | float | bool | None]] = [("id", job_id) for job_id in ids]
    params += [("offset", str(o)) for o in offsets]
    params += list(extra.items())
    response = client.get("/ui/run/progress", params=params, headers=HEADERS)
    assert response.status_code == 200
    return response.text


def jobs_of(html: str) -> dict[str, Any]:
    """The job sections of a progress fragment, by job id."""
    return {
        section.get("data-job"): section for section in tree(html).xpath("//section[@class='job']")
    }


def bar(section: Any) -> tuple[str, str]:
    found = section.xpath(".//progress")[0]
    return found.get("value"), found.get("max")


def rows(section: Any) -> list[list[str]]:
    return [
        [" ".join(cell.text_content().split()) for cell in row.xpath("./td")]
        for row in section.xpath(".//table[@class='items']/tbody/tr")
    ]


def job_items(client: TestClient, job_id: str) -> list[str]:
    """The URLs of a job's items, over REST."""
    found: list[str] = []
    offset: int | None = 0
    while offset is not None:
        response = client.get(
            f"/v1/jobs/{job_id}", params={"offset": offset, "limit": 500}, headers=AUTH
        )
        data = response.json()["data"]
        found += [item["url"] for item in data["items"]]
        offset = data["next_offset"]
    return found


@pytest.fixture
def hits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The ``check_rate`` hits of the UI's own views (by principal)."""
    counted: list[str] = []
    real = ratelimit.check_rate

    async def counting(state: Any, principal: Any) -> None:
        counted.append(principal.name)
        await real(state, principal)

    monkeypatch.setattr("text_extraction.ui.security.check_rate", counting)
    return counted


# --- progress ------------------------------------------------------------------------------------


def test_progress_view(client: TestClient) -> None:
    small = start_batch(client, urls(3))
    large = start_batch(client, urls(25, "https://example.org/large"))
    store_result(client, small, 0, title="First")
    store_result(client, small, 1, error=ErrorCode.TARGET_HTTP_ERROR)
    claim(client, small, 2, "browser", 2)

    html = progress(client, small, large, mode="auto")

    jobs = jobs_of(html)
    assert list(jobs) == [small, large]
    first = jobs[small]
    assert bar(first) == ("2", "3")
    assert texts(first, ".//span[@class='done']") == ["2/3"]
    assert texts(first, ".//span[@class='success']") == ["1"]
    assert texts(first, ".//span[@class='failed']") == ["1"]
    assert texts(first, ".//span[contains(@class, 'state')]") == ["queued"]  # no worker yet
    assert texts(first, ".//p[@class='elapsed']")[0].startswith("Elapsed: ")
    assert rows(first) == [
        ["0", urls(3)[0], "success", "", "Show"],
        ["1", urls(3)[1], "failed", "target_http_error", "Show"],
        ["2", urls(3)[2], "running in browser, claim 2", "", ""],
    ]
    link = first.xpath(".//td[@class='open']/button")[0].get("hx-get")
    assert link == f"/ui/run/job?id={small}&index=0&mode=auto"
    # The item's result and timeline.
    claim(client, small, 0, "extract", 1)
    item = client.get(link, headers=HEADERS).text
    assert texts(tree(item), "//h3[@class='title']") == ["First"]
    assert "extract pool, claim 1" in " ".join(texts(tree(item), "//ol[@class='timeline']/li"))
    # Pages of 20 items with previous and next buttons.
    second = jobs[large]
    assert [row[2] for row in rows(second)] == ["queued"] * 20
    assert not second.xpath(".//button[contains(@class, 'prev')]")
    following = second.xpath(".//button[contains(@class, 'next')]")[0].get("hx-get")
    assert following == (
        f"/ui/run/progress?id={small}&id={large}&offset=0&offset=20&mode=auto&live=1"
    )
    paged = jobs_of(client.get(following, headers=HEADERS).text)[large]
    assert [row[0] for row in rows(paged)] == [str(n) for n in range(20, 25)]
    previous = paged.xpath(".//button[contains(@class, 'prev')]")[0].get("hx-get")
    assert "offset=0&offset=0" in previous
    assert not paged.xpath(".//button[contains(@class, 'next')]")
    # An unknown job is shown as such, beside the others.
    missing = jobs_of(progress(client, small, "f" * 32))["f" * 32]
    assert texts(missing, ".//p[contains(@class, 'job-error')]") == ["Job not found."]


def test_progress_refresh_one_hit_and_stops(client: TestClient, hits: list[str]) -> None:
    ids = [start_batch(client, urls(2, f"https://example.org/{n}")) for n in range(5)]

    live = tree(progress(client, *ids, live="1"))

    assert hits == ["alice"]
    holder = live.xpath("//div[@class='progress-live']")[0]
    assert holder.get("hx-trigger") == "load delay:3s"
    assert holder.get("hx-swap") == "outerHTML"
    assert holder.get("hx-get").endswith("&live=1")
    assert len(live.xpath("//section[@class='job']")) == 5

    for job_id in ids:
        store_result(client, job_id, 0)
    still = tree(progress(client, *ids, live="1"))
    assert still.xpath("//div[@class='progress-live']")[0].get("hx-trigger")
    for job_id in ids:
        store_result(client, job_id, 1)
    done = tree(progress(client, *ids, live="1"))

    assert hits == ["alice"] * 3
    assert done.xpath("//div[@class='progress-live']")[0].get("hx-trigger") is None
    assert texts(done, "//span[contains(@class, 'state')]") == ["completed"] * 5


def test_progress_refusals(hits: list[str]) -> None:
    with running(rate_limit_per_minute=1) as client:
        job_id = start_batch(client, urls(1))  # the REST call takes the minute's one hit
        live = tree(progress(client, job_id, live="1"))
        # A refresh refused for the rate limit tries again after Retry-After.
        holder = live.xpath("//div[@class='progress-live']")[0]
        assert holder.get("hx-trigger").startswith("load delay:")
        assert texts(live, "//p[@class='error']")[0].startswith("Rate limit exceeded.")
        whole = tree(progress(client, job_id))
        assert texts(whole, "//*[@class='http-status']") == ["HTTP 429"]
        no_key = client.get("/ui/run/progress", params={"id": job_id}, headers=HX)
        assert texts(tree(no_key.text), "//*[@class='http-status']") == ["HTTP 401"]
        many = progress(client, *(["0" * 32] * 21))
        assert "Show at most 20 jobs at once." in many


def test_cancel_button(client: TestClient) -> None:
    job_id = start_batch(client, urls(2))
    section = jobs_of(progress(client, job_id))[job_id]
    form = section.xpath(".//form[@class='cancel']")[0]
    assert form.get("hx-post") == "/ui/run/cancel"
    assert form.get("hx-confirm")
    assert [(field.get("name"), field.get("value")) for field in form.xpath(".//input")] == [
        ("id", job_id)
    ]

    cancelled = run(client, "cancel", id=job_id).text

    assert job_id in texts(tree(cancelled), "//p[@class='cancelled']")[0]
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH).status_code == 404
    gone = tree(progress(client, job_id, live="1"))
    assert texts(gone, "//p[contains(@class, 'job-error')]") == ["Job not found."]
    assert gone.xpath("//div[@class='progress-live']")[0].get("hx-trigger") is None
    again = tree(run(client, "cancel", id=job_id).text)
    assert texts(again, "//*[@class='http-status']") == ["HTTP 404"]
    assert texts(tree(run(client, "cancel", id="../info").text), "//p") == ["This is not a job id."]


def test_job_item_view_and_artifact(client: TestClient) -> None:
    job_id = start_batch(client, urls(2))
    portal(client).call(job_store(client).put_artifact, job_id, "screenshot.png", PNG)
    screenshot = {
        "name": "screenshot.png",
        "url": f"/v1/jobs/{job_id}/artifacts/screenshot.png",
        "content_type": "image/png",
        "bytes": len(PNG),
        "width": 1920,
        "height": 1080,
        "expires_at": "2030-01-01T00:00:00Z",
    }
    store_result(
        client,
        job_id,
        0,
        mode_used="browser",
        screenshot=screenshot,
        timings={
            "total_ms": 900,
            "compliance_ms": 1,
            "fetch_ms": 0,
            "convert_ms": 9,
            "render_ms": 800,
        },
    )

    item = tree(
        client.get(
            "/ui/run/job", params={"id": job_id, "index": 0, "mode": "auto"}, headers=HEADERS
        ).text
    )

    steps = texts(item, "//ol[@class='timeline']/li")
    assert any(step.startswith("Render escalated from fast") for step in steps), steps
    assert (
        item.xpath("//img[@class='screenshot']")[0].get("src").startswith("data:image/png;base64,")
    )
    curl = texts(item, "//pre[@class='curl']")[0]
    assert f"/v1/jobs/{job_id}?offset=0&limit=1" in curl
    waiting = client.get("/ui/run/job", params={"id": job_id, "index": 1}, headers=HEADERS)
    assert texts(tree(waiting.text), "//p") == ["Item 1: queued."]
    shot = tree(
        client.get(
            "/ui/run/artifact", params={"id": job_id, "name": "screenshot.png"}, headers=HEADERS
        ).text
    )
    assert shot.xpath("//img")[0].get("src").startswith("data:image/png;base64,")
    other = client.get("/ui/run/artifact", params={"id": job_id, "name": "../x"}, headers=HEADERS)
    assert "This is not an artifact of a job." in other.text


# --- batches -------------------------------------------------------------------------------------


@pytest.fixture
def batch_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The number of URLs of each ``POST /v1/batch`` the UI forwards."""
    counted: list[int] = []
    real = forward

    async def counting(request: Any, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/v1/batch":
            counted.append(len(kwargs["json"]["urls"]))
        return await real(request, method, path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("text_extraction.ui.job_views.forward", counting)
    return counted


def test_batch_split_and_refusal(batch_calls: list[int]) -> None:
    listed = urls(2500)
    with running(batch_max_urls=1000) as client, capture_logs() as logs:
        html = run(client, "batch-urls", urls="\n".join(listed), format="text").text
        assert batch_calls == [1000, 1000, 500]
        jobs = jobs_of(html)
        assert [bar(section) for section in jobs.values()] == [
            ("0", "1000"),
            ("0", "1000"),
            ("0", "500"),
        ]
        assert [url for job_id in jobs for url in job_items(client, job_id)] == listed
        assert {"event": "ui_batch_split", "parts": 3, "urls": 2500} in [
            {key: log[key] for key in ("event", "parts", "urls") if key in log} for log in logs
        ]
        live = tree(html).xpath("//div[@class='progress-live']")[0]
        assert live.get("hx-trigger") == "load delay:3s"

    batch_calls.clear()
    with running(batch_max_urls=1000, batch_max_open_per_client=5) as client:
        html = run(client, "batch-urls", urls="\n".join(urls(5001))).text
        assert texts(tree(html), "//div[@class='error']/p") == ["Select at most 5000 URLs at once."]
        assert batch_calls == []

    with running(batch_max_urls=1000, batch_max_open_per_client=5) as client:
        for number in range(3):
            start_batch(client, urls(1, f"https://example.org/open-{number}"))
        batch_calls.clear()
        html = run(client, "batch-urls", urls="\n".join(listed)).text
        assert batch_calls == [1000, 1000, 500]
        assert len(jobs_of(html)) == 2
        refused = " ".join(texts(tree(html), "//section[@class='jobs-view']/div[@class='error']"))
        assert "Part 3 of 3 was refused (HTTP 429)" in refused
        assert "At most 5 batches and site jobs may be unfinished at a time." in refused


def test_batch_urls_refusals(client: TestClient, batch_calls: list[int]) -> None:
    html = run(client, "batch-urls", urls=f"{PAGE}\nftp://example.org/x\n@cmd").text
    assert texts(tree(html), "//ul[@class='problems']/li") == [
        "Row 2: URL scheme must be http or https",
        "Row 3: URL scheme must be http or https",
    ]
    assert texts(tree(run(client, "batch-urls", urls=" \n").text), "//p") == [
        "Give at least one URL."
    ]
    twice = run(client, "batch-urls", urls=PAGE, options='{"urls": ["x"]}').text
    assert "urls is given twice." in twice
    assert batch_calls == []
    # The REST refusal of a first part is shown as its answer.
    refused = tree(run(client, "batch-urls", urls=PAGE, options='{"compliance": "ignore"}').text)
    assert texts(refused, "//*[@class='http-status']") == ["HTTP 403"]
    assert batch_calls == [1]


def test_batch_file_preview_then_start(hits: list[str], batch_calls: list[int]) -> None:
    csv_file = (
        "title;url\n"
        f"one;{ORIGIN}/a\n"
        f"two;{ORIGIN}/b\n"
        'evil;=HYPERLINK("http://evil")\n'
        f"three;{ORIGIN}/c\n"
        f"again;{ORIGIN}/a\n"
        "svg;<svg onload=alert(1)>\n"
    ).encode()
    with running() as client, capture_logs() as logs:
        response = run(client, "batch-file", files={"file": ("urls.csv", csv_file, "text/csv")})

        preview = tree(response.text)
        assert hits == ["alice"]
        assert batch_calls == []
        counts = dict(
            zip(
                texts(preview, "//dl[contains(@class, 'counts')]/dt"),
                texts(preview, "//dl[contains(@class, 'counts')]/dd"),
                strict=True,
            )
        )
        assert counts == {
            "Rows": "6",
            "URLs": "3",
            "Not a URL": "2",
            "Duplicates dropped": "1",
            "Batches": "1",
        }
        assert texts(preview, "//ul[@class='problems']/li") == [
            "Row 4: URL scheme must be http or https",
            "Row 7: URL contains a blank or a control character",
        ]
        assert "<svg" not in response.text
        assert texts(preview, "//ol[@class='first-urls']/li") == [
            f"{ORIGIN}/a",
            f"{ORIGIN}/b",
            f"{ORIGIN}/c",
        ]
        form = preview.xpath("//form")[0]
        assert form.get("hx-post") == "/ui/run/batch-urls"
        area = form.xpath(".//textarea[@name='urls']")[0]
        assert area.get("readonly") is not None
        assert {
            field.get("name") for field in form.xpath(".//input | .//select | .//textarea")
        } >= {
            "urls",
            "format",
            "mode",
            "metadata",
            "options",
        }
        assert texts(form, ".//button[@type='submit']") == ["Start batch"]
        assert {"event": "ui_batch_file", "rows": 6, "urls": 3, "invalid": 2, "duplicates": 1} in [
            {key: log.get(key) for key in ("event", "rows", "urls", "invalid", "duplicates")}
            for log in logs
        ]

        started = run(client, "batch-urls", urls=area.text_content(), format="text").text
        (job_id,) = jobs_of(started)
        assert batch_calls == [3]
        assert job_items(client, job_id) == [f"{ORIGIN}/a", f"{ORIGIN}/b", f"{ORIGIN}/c"]


def test_batch_file_bounds(hits: list[str]) -> None:
    with running(ui_batch_file_max_bytes=1024, ui_batch_file_max_rows=10) as client:
        large = run(client, "batch-file", files={"file": ("a.csv", b"x" * 1025, "text/csv")})
        assert texts(tree(large.text), "//p") == ["The file is larger than 1024 bytes."]
        rows_file = "".join(f"{ORIGIN}/{n}\n" for n in range(11)).encode()
        many = run(client, "batch-file", files={"file": ("a.csv", rows_file, "text/csv")})
        assert texts(tree(many.text), "//p") == ["The file has more than 10 rows."]
        broken = run(client, "batch-file", files={"file": ("a.json", b'["x",', "application/json")})
        assert texts(tree(broken.text), "//p") == ["The file is not valid JSON."]
        assert texts(tree(run(client, "batch-file").text), "//p") == ["Choose a CSV or JSON file."]
        no_key = run(client, "batch-file", auth=False, files={"file": ("a.csv", b"x", "text/csv")})
        assert texts(tree(no_key.text), "//*[@class='http-status']") == ["HTTP 401"]
    over = "".join(f"{ORIGIN}/{n}\n" for n in range(11)).encode()
    with running(batch_max_urls=5, batch_max_open_per_client=2) as client:
        preview = tree(run(client, "batch-file", files={"file": ("a.csv", over, "text/csv")}).text)
        assert texts(preview, "//p[@class='error']") == ["Select at most 10 URLs at once."]
        assert not preview.xpath("//form")


def test_site_structure_form_starts_a_job(client: TestClient) -> None:
    html = run(
        client, "site-structure", url=PAGE, depth="1", max_pages="5", scope="host", sitemaps="on"
    ).text

    (section,) = jobs_of(html).values()
    assert texts(section, ".//span[@class='kind']") == ["site"]
    assert "The total grows while it crawls." in texts(section, ".//p[@class='hint']")
    button = section.xpath(".//button[contains(@class, 'tree-button')]")[0]
    assert button.text_content() == "Show tree now"
    assert button.get("hx-get") == f"/ui/run/site-tree?id={section.get('data-job')}"
    refused = tree(run(client, "site-structure", url=PAGE, depth="99").text)
    assert texts(refused, "//*[@class='http-status']") == ["HTTP 422"]


# --- the site tree -------------------------------------------------------------------------------


Pages = Callable[[int, int], list[JobItem]]


def fake_reads(
    monkeypatch: pytest.MonkeyPatch, total: int, items_of: Pages, *, state: str = "completed"
) -> list[tuple[int, int]]:
    """Replace the ``read_job`` core by one that answers a ``site`` job of ``total`` items made
    by ``items_of(offset, limit)``; records each read's (offset, limit) and checks that the items
    of the read before are gone (the view holds one page at a time)."""
    reads: list[tuple[int, int]] = []
    held: list[weakref.ref[JobItem]] = []

    async def read_job(state_: Any, principal: Any, job_id: str, offset: int, limit: int) -> Any:
        gc.collect()
        assert [ref for ref in held if ref() is not None] == [], "the page before is still held"
        reads.append((offset, limit))
        items = items_of(offset, min(limit, total - offset))
        held[:] = [weakref.ref(item) for item in items]
        view = JobView(
            id=job_id,
            kind="site",
            state=state,  # type: ignore[arg-type]
            created_at=datetime(2026, 10, 30, tzinfo=UTC),
            finished_at=None,
            progress=JobProgress(total=total, done=total, success=total, partial=0, failed=0),
            items=items,
            next_offset=offset + limit if offset + limit < total else None,
        )
        return Envelope[JobView](status="success", data=view)

    monkeypatch.setattr(jobs_api, "read_job", read_job)
    return reads


def show_tree(client: TestClient, job_id: str = "a" * 32) -> str:
    response = client.get("/ui/run/site-tree", params={"id": job_id}, headers=HEADERS)
    assert response.status_code == 200
    return response.text


SMALL_SITE = [
    page_item(0, f"{ORIGIN}/", title="Home", depth=0, links=((f'{ORIGIN}/q"uote', "depth"),)),
    page_item(1, f"{ORIGIN}/docs/intro", title="<script>alert(1)</script>"),
    sitemap_item(2, f"{ORIGIN}/sitemap.xml", [{"loc": f"{ORIGIN}/docs/more"}]),
]


def test_tree_checkboxes_named_only_for_urls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_reads(
        monkeypatch, len(SMALL_SITE), lambda offset, limit: SMALL_SITE[offset : offset + limit]
    )

    html = show_tree(client)

    root = tree(html)
    boxes = root.xpath("//ul[@class='tree']//input[@type='checkbox']")
    named = {box.get("value") for box in boxes if box.get("name") == "url"}
    assert named == {
        f"{ORIGIN}/",
        f'{ORIGIN}/q"uote',
        f"{ORIGIN}/docs/intro",
        f"{ORIGIN}/docs/more",
    }
    sections = root.xpath("//li[contains(@class, 'node-section')]/label/input")
    assert len(sections) == 2  # example.org and docs/
    assert all(box.get("name") is None and box.get("value") is None for box in sections)
    assert "<script>alert" not in html
    assert texts(root, "//li[contains(@class, 'node-linked')]/span[contains(@class, 'found')]") == [
        "linked only"
    ]
    assert texts(root, "//p[@class='tree-counts']") == ["4 URLs"]
    assert texts(root, "//span[@class='tree-counter']") == ["0 selected"]
    assert {button.get("data-action") for button in root.xpath("//button[@data-action]")} == {
        "tree-all",
        "tree-none",
    }


def test_tree_caps_and_reads_in_pages(monkeypatch: pytest.MonkeyPatch, hits: list[str]) -> None:
    pages, parts, per_part = 300, 20, 10_000

    def items_of(offset: int, limit: int) -> list[JobItem]:
        made: list[JobItem] = []
        for index in range(offset, offset + limit):
            if index < pages:
                made.append(page_item(index, f"{ORIGIN}/p/{index}"))
                continue
            part = index - pages
            entries = [{"loc": f"{ORIGIN}/s/{part}/{number}"} for number in range(per_part)]
            made.append(sitemap_item(index, f"{ORIGIN}/sitemap.xml", entries, part=part + 1))
        return made

    reads = fake_reads(monkeypatch, pages + parts, items_of)
    with running(ui_tree_max_urls=5000) as client:
        html = show_tree(client)

    assert reads == [(offset, 20) for offset in range(0, pages + parts, 20)]
    assert hits == ["alice"]
    root = tree(html)
    assert len(root.xpath("//input[@name='url']")) == 5000
    hidden = pages + parts * per_part - 5000
    assert texts(root, "//span[@class='hidden-note']") == [
        f"{hidden} more URLs not shown (UI_TREE_MAX_URLS)"
    ]
    assert len(root.xpath("//input[starts-with(@value, 'https://example.org/p/')]")) == pages


def test_tree_refusals(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    unknown = tree(show_tree(client, "f" * 32))
    assert texts(unknown, "//*[@class='http-status']") == ["HTTP 404"]
    job_id = start_batch(client, urls(1))
    assert "This job is not a site structure job." in show_tree(client, job_id)
    fake_reads(monkeypatch, 1, lambda offset, limit: SMALL_SITE[:1], state="running")
    assert "A snapshot: the job is still running." in show_tree(client)


def test_extract_selected_starts_batch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, batch_calls: list[int]
) -> None:
    fake_reads(
        monkeypatch, len(SMALL_SITE), lambda offset, limit: SMALL_SITE[offset : offset + limit]
    )
    form = tree(show_tree(client)).xpath("//form[@class='tree-form']")[0]
    assert form.get("hx-post") == "/ui/run/batch-urls"
    assert form.get("hx-target") == "next .tree-jobs"
    assert {field.get("name") for field in form.xpath(".//input | .//textarea")} >= {
        "url",
        "format",
        "metadata",
        "options",
    }
    assert texts(form, ".//button[@type='submit']") == ["Extract selected"]
    monkeypatch.undo()  # the REST reads below are real

    selected = [f"{ORIGIN}/docs/intro", f'{ORIGIN}/q"uote']
    sent = {**HX, **AUTH}
    response = client.post(
        "/ui/run/batch-urls",
        data={"url": selected, "format": "markdown", "mode": "auto", "metadata": "on"},
        headers=sent,
    )

    (job_id,) = jobs_of(response.text)
    assert job_items(client, job_id) == selected


# --- operations ----------------------------------------------------------------------------------


def test_every_public_route_has_an_operation(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    routes = {
        (method.upper(), path) for path, methods in spec["paths"].items() for method in methods
    }

    operations = {(op.method, op.path) for op in OPERATIONS.values()}

    assert routes == operations


def test_the_job_tabs_are_on_the_page(client: TestClient) -> None:
    html = run(client, "login").text
    root = tree(html)
    tabs = texts(root, "//nav[@class='tabs']/button")
    assert tabs[-4:] == ["Batch", "Batch file", "Site structure", "Info"]
    assert root.xpath("//form[@hx-post='/ui/run/batch-urls']//textarea[@name='urls']")
    assert root.xpath("//form[@hx-post='/ui/run/batch-file']//input[@type='file'][@name='file']")
    assert root.xpath("//form[@hx-post='/ui/run/site-structure']//input[@name='url']")


def test_single_job_answer_links_its_progress() -> None:
    accepted = {"id": "a" * 32, "kind": "extract", "state": "running", "total": 1}
    envelope = {"status": "success", "data": {**accepted, "status_url": f"/v1/jobs/{'a' * 32}"}}

    html = render_result(op="extract", http_status=202, envelope=envelope, requested_mode="auto")

    button = tree(html).xpath("//button[contains(@class, 'progress-button')]")[0]
    assert button.get("hx-get") == f"/ui/run/progress?id={'a' * 32}&mode=auto"
    assert button.get("hx-target") == "closest .result-view"
    assert button.get("hx-swap") == "outerHTML"
