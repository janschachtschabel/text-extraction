"""The UI's job views (rulings P10-S18 to P10-S20, Jan's requirements 2 to 4 and 7).

- ``POST /ui/run/batch-urls``: URLs (typed lines, a preview's list or the tree's checked boxes)
  checked again, split into batches of ``BATCH_MAX_URLS`` and forwarded one after another to
  ``POST /v1/batch``; the answer is the progress of the jobs started.
- ``POST /ui/run/batch-file``: a CSV or JSON file of URLs checked on the server and shown as a
  preview with a "Start batch" button (one rate-limit hit, nothing forwarded).
- ``POST /ui/run/site-structure``: forwards ``POST /v1/site-structure``.
- ``GET /ui/run/progress``: one or more jobs, read through the ``read_job`` core with one
  rate-limit hit per view; it refreshes itself every 3 s while any job runs.
- ``GET /ui/run/site-tree``: a site job read in pages of 20 items into its tree.
- ``GET /ui/run/job``, ``GET /ui/run/artifact`` and ``POST /ui/run/cancel``: an item's result
  and timeline, a screenshot, and the cancel button; each forwards one REST call.

Every outcome is answered with HTTP 200 and a fragment, so htmx swaps it."""

import asyncio
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, get_args
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter
from starlette.datastructures import FormData, State, UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from text_extraction import jobs_api
from text_extraction.auth import Principal, authenticate
from text_extraction.errors import ServiceError
from text_extraction.job_models import JobAccepted, JobItem, JobView
from text_extraction.ratelimit import count_used
from text_extraction.settings import Settings
from text_extraction.ui.answers import (
    artifact_of,
    envelope_error,
    options_error,
    raw_text,
    read_form,
    read_screenshot,
    refusal,
    screenshot_of,
    sent_keys,
    text_fields,
)
from text_extraction.ui.forms import OPERATIONS, OptionsError, access_choice, build_body
from text_extraction.ui.forward import Forwarded, forward
from text_extraction.ui.i18n import Code, Message
from text_extraction.ui.results import curl_line, render_result
from text_extraction.ui.security import RUN_PREFIX, form_part_limit, ui_principal
from text_extraction.ui.site_tree import FOUND_LABELS, SiteTree, TreeBuilder
from text_extraction.ui.templating import language_context, request_language, templates
from text_extraction.ui.texts import error_text
from text_extraction.ui.timeline import RequestedMode
from text_extraction.ui.url_lists import (
    UrlList,
    UrlListError,
    parse_url_file,
    parse_url_lines,
    split_batches,
)

__all__ = ["PAGE_ITEMS", "add_job_routes"]

_log = structlog.get_logger(__name__)

# Items per page of the progress view and per read of the site tree.
PAGE_ITEMS: Final = 20
# The items of a site tree that one rate-limit hit covers: a page of GET /v1/jobs/{id} (M4).
TREE_ITEMS_PER_HIT: Final = 500
# One progress view reads at most this many items in all (at least MIN_PAGE_ITEMS per job), and
# at most READS_AT_ONCE jobs at a time: an item may be up to JOB_ITEM_MAX_BYTES.
ITEMS_PER_VIEW: Final = 100
MIN_PAGE_ITEMS: Final = 5
READS_AT_ONCE: Final = 2
# The progress view refreshes itself after this many seconds while a job runs.
REFRESH_SECONDS: Final = 3
# The most jobs one progress view shows (at least BATCH_MAX_OPEN_PER_CLIENT).
MAX_JOBS_SHOWN: Final = 20
# What the preview of a batch file lists.
PREVIEW_URLS: Final = 50
PROBLEMS_SHOWN: Final = 20
FILE_PART: Final = "file"
# Fields of a batch form besides its URLs (format, mode, metadata, options and a few more).
FORM_OPTION_FIELDS: Final = 64

NO_URLS: Final = Message("jobs.no_urls")
NOT_A_JOB: Final = Message("jobs.not_a_job")
# A job's id, as `jobs_api.visible_job` checks it: anything else is never forwarded.
JOB_ID: Final = re.compile(r"[0-9a-f]{32}", re.ASCII)
NO_JOB: Final = Message("jobs.no_job")
NOT_A_SITE: Final = Message("jobs.not_a_site")
NO_FILE: Final = Message("jobs.no_file")
NO_ITEM_INDEX: Final = Message("jobs.no_item_index")
NOT_AN_ARTIFACT: Final = Message("jobs.not_an_artifact")
QUEUED: Final = Message("job_state.queued")
SHOW_TREE: Final = Message("progress.show_tree")
SHOW_TREE_NOW: Final = Message("progress.show_tree_now")
_MODES: Final = frozenset(get_args(RequestedMode))


# --- the progress view ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemRow:
    """One item of a job's page: its state ("queued", "running in <pool>, claim <n>") until it
    is done, then its status and error code; ``link`` opens its result and timeline."""

    index: int
    url: str | None
    state: Message | None
    status: str | None
    error_code: str | None
    link: str | None


@dataclass
class JobPanel:
    """What the progress view shows of one job; ``error`` (the text of its code) and
    ``error_detail`` (the API's English message) when it could not be read."""

    id: str
    kind: str | None = None
    state: str | None = None
    done: int = 0
    total: int = 0
    success: int = 0
    partial: int = 0
    failed: int = 0
    elapsed: Message | None = None
    items: list[ItemRow] = field(default_factory=list)
    prev_url: str | None = None
    next_url: str | None = None
    tree_url: str | None = None
    tree_label: Message | None = None
    error: Message | None = None
    error_detail: str | None = None
    retryable: bool = False

    @property
    def running(self) -> bool:
        """Whether the view keeps refreshing for this job."""
        if self.error is not None:
            return self.retryable
        return self.state != "completed"


@dataclass(frozen=True)
class _Shown:
    """The jobs of a progress view, their page offsets and the mode they were asked with."""

    ids: list[str]
    offsets: list[int]
    mode: RequestedMode | None
    limit: int = PAGE_ITEMS  # items per job and page

    def url(self, *, offsets: Sequence[int] | None = None) -> str:
        """The live view's URL (with ``offsets`` instead of the view's own)."""
        query: list[tuple[str, str]] = [("id", job_id) for job_id in self.ids]
        query += [("offset", str(offset)) for offset in offsets or self.offsets]
        if self.mode is not None:
            query.append(("mode", self.mode))
        query.append(("live", "1"))
        return f"{RUN_PREFIX}/progress?{urlencode(query)}"

    def moved(self, position: int, offset: int) -> str:
        offsets = list(self.offsets)
        offsets[position] = offset
        return self.url(offsets=offsets)


def _elapsed(start: datetime, end: datetime | None) -> Message:
    seconds = max(0, int(((end or datetime.now(UTC)) - start).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return Message("progress.hours", {"hours": hours, "minutes": minutes})
    if minutes:
        return Message("progress.minutes", {"minutes": minutes, "seconds": seconds})
    return Message("progress.seconds", {"seconds": seconds})


def _item_state(item: JobItem) -> Message:
    if item.run is None:
        return QUEUED
    return Message("progress.running", {"pool": Code(item.run.pool), "attempt": item.run.attempts})


def _item_link(job_id: str, index: int, mode: RequestedMode | None) -> str:
    query = [("id", job_id), ("index", str(index))] + ([("mode", mode)] if mode else [])
    return f"{RUN_PREFIX}/job?{urlencode(query)}"


def _row(job_id: str, item: JobItem, mode: RequestedMode | None) -> ItemRow:
    if item.result is None:
        return ItemRow(item.index, item.url, _item_state(item), None, None, None)
    error = item.result.error
    return ItemRow(
        index=item.index,
        url=item.url,
        state=None,
        status=item.result.status,
        error_code=str(error.code) if error is not None else None,
        link=_item_link(job_id, item.index, mode),
    )


def _panel(view: JobView, shown: _Shown, position: int) -> JobPanel:
    offset = shown.offsets[position]
    progress = view.progress
    panel = JobPanel(
        id=view.id,
        kind=view.kind,
        state=view.state,
        done=progress.done,
        total=progress.total,
        success=progress.success,
        partial=progress.partial,
        failed=progress.failed,
        elapsed=_elapsed(view.created_at, view.finished_at),
        items=[_row(view.id, item, shown.mode) for item in view.items],
    )
    if offset > 0:
        panel.prev_url = shown.moved(position, max(0, offset - shown.limit))
    if view.next_offset is not None:
        panel.next_url = shown.moved(position, view.next_offset)
    if view.kind == "site":
        panel.tree_url = f"{RUN_PREFIX}/site-tree?{urlencode({'id': view.id})}"
        panel.tree_label = SHOW_TREE if view.state == "completed" else SHOW_TREE_NOW
    return panel


def _accepted_panel(job: JobAccepted) -> JobPanel:
    """A job as its 202 answer describes it, before it is read."""
    panel = JobPanel(
        id=job.id,
        kind=job.kind,
        state=job.state,
        total=job.total,
        elapsed=Message("progress.seconds", {"seconds": 0}),
    )
    if job.kind == "site":
        panel.tree_url = f"{RUN_PREFIX}/site-tree?{urlencode({'id': job.id})}"
        panel.tree_label = SHOW_TREE_NOW
    return panel


async def _read_panel(
    state: State, principal: Principal, shown: _Shown, position: int, gate: asyncio.Semaphore
) -> JobPanel:
    """One job of the view, read under ``gate``; only the panel's fields outlive the read."""
    job_id = shown.ids[position]
    try:
        async with gate:
            envelope = await jobs_api.read_job(
                state, principal, job_id, shown.offsets[position], shown.limit
            )
            assert envelope.data is not None  # noqa: S101 - read_job always answers its view
            return _panel(envelope.data, shown, position)
    except ServiceError as error:
        return JobPanel(
            id=job_id,
            error=error_text(error.code, error.http_status),
            error_detail=error.message,
            retryable=error.retryable,
        )


def _live_context(
    panels: Sequence[JobPanel],
    shown: _Shown,
    *,
    refused: ServiceError | None = None,
    delay: int = 0,
) -> dict[str, Any]:
    """The context of ``progress_live.html``; ``refused`` is the refusal of a refresh (a 429 or
    503), shown as the text of its code with the API's English message."""
    running = any(panel.running for panel in panels) or refused is not None
    return {
        "jobs": panels,
        "refresh_url": shown.url() if running else None,
        "delay": max(delay, REFRESH_SECONDS),
        "refusal": error_text(refused.code, refused.http_status) if refused else None,
        "refusal_detail": refused.message if refused else None,
        "page_items": shown.limit,
    }


def _offset(value: str) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def _mode(value: str | None) -> RequestedMode | None:
    return value if value in _MODES else None  # type: ignore[return-value]


def _max_jobs(settings: Settings) -> int:
    return max(MAX_JOBS_SHOWN, settings.batch_max_open_per_client)


def _progress_fragment(
    request: Request,
    panels: Sequence[JobPanel],
    shown: _Shown,
    *,
    live: bool,
    refused: Mapping[str, Any] | None = None,
) -> Response:
    context = _live_context(panels, shown)
    context["refused"] = refused
    name = "progress_live.html" if live else "progress.html"
    return templates.TemplateResponse(request, name, context)


async def progress_view(request: Request, settings: Settings) -> Response:
    """``GET /ui/run/progress?id=…[&id=…][&offset=…][&mode=…][&live=1]`` (P10-S18): the jobs
    (each once) with a page of 20 items each, read through ``read_job`` after one rate-limit
    hit, however many jobs it shows. One view reads at most ``ITEMS_PER_VIEW`` items (fewer per
    job when it shows more than five), ``READS_AT_ONCE`` jobs at a time, and keeps only the
    panels' fields of each. ``live=1`` answers the refreshing part only (the refresh and the
    pager); without it the whole view."""
    params = request.query_params
    given = params.getlist("id")
    offsets = [_offset(value) for value in params.getlist("offset")]
    offsets = (offsets + [0] * len(given))[: len(given)]
    first: dict[str, int] = {}
    for job_id, offset in zip(given, offsets, strict=True):
        first.setdefault(job_id, offset)  # a job given twice is shown once
    ids = list(first)
    limit = min(PAGE_ITEMS, max(MIN_PAGE_ITEMS, ITEMS_PER_VIEW // max(1, len(ids))))
    shown = _Shown(
        ids=ids, offsets=list(first.values()), mode=_mode(params.get("mode")), limit=limit
    )
    live = params.get("live") == "1"
    if not ids:
        return templates.TemplateResponse(request, "form_error.html", {"message": NO_JOB})
    if len(ids) > _max_jobs(settings):
        message = Message("jobs.too_many_jobs", {"max": _max_jobs(settings)})
        return templates.TemplateResponse(request, "form_error.html", {"message": message})
    try:
        principal = await ui_principal(request)
        jobs_api.check_jobs_enabled(settings)
    except ServiceError as error:
        if live and error.retryable:  # a 429 or 503: the view tries again later
            retry_after = (error.headers or {}).get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else REFRESH_SECONDS
            context = _live_context([], shown, refused=error, delay=delay)
            return templates.TemplateResponse(request, "progress_live.html", context)
        return refusal(error, request_language(request))
    state: State = request.app.state
    gate = asyncio.Semaphore(READS_AT_ONCE)
    panels = await asyncio.gather(
        *(_read_panel(state, principal, shown, position, gate) for position in range(len(ids)))
    )
    return _progress_fragment(request, panels, shown, live=live)


# --- batches -------------------------------------------------------------------------------------


def _problems(found: Sequence[tuple[int, Message]]) -> dict[str, Any]:
    shown = list(found[:PROBLEMS_SHOWN])
    return {"problems": shown, "more_problems": max(0, len(found) - len(shown))}


def _form_error(request: Request, message: Message, **extra: Any) -> Response:
    return templates.TemplateResponse(request, "form_error.html", {"message": message, **extra})


def _lines(form: FormData) -> list[str]:
    """The URLs of a form: each ``urls`` field split into lines, then each ``url`` field."""
    lines = [
        line
        for value in form.getlist("urls")
        if isinstance(value, str)
        for line in value.splitlines()
    ]
    lines.extend(value for value in form.getlist("url") if isinstance(value, str))
    return lines


def _max_listed(settings: Settings) -> int:
    """The most lines a batch form may list: a batch file's rows or every URL of the batches a
    caller may have open (more is refused by the split anyway)."""
    return max(
        settings.ui_batch_file_max_rows,
        settings.batch_max_open_per_client * settings.batch_max_urls,
    )


def _max_fields(settings: Settings) -> int:
    """The most fields of a batch form: one ``url`` field per checked box of a tree (at most
    ``UI_TREE_MAX_URLS``), or the URLs of the batches a caller may have open, besides the batch
    options. Typed lines and a preview's list are one ``urls`` field."""
    open_urls = settings.batch_max_open_per_client * settings.batch_max_urls
    return max(settings.ui_tree_max_urls, open_urls) + FORM_OPTION_FIELDS


@dataclass(frozen=True)
class _Refused:
    """A refused part of a batch: the text of its error code and the API's English message."""

    part: int
    parts: int
    http_status: int
    message: Message
    detail: str | None


async def batch_urls_view(request: Request, settings: Settings) -> Response:
    """``POST /ui/run/batch-urls`` (P10-S19): the URLs are checked again (`parse_url_lines`) and
    split into ``ceil(n / BATCH_MAX_URLS)`` batches. More batches than
    ``BATCH_MAX_OPEN_PER_CLIENT`` sends nothing; otherwise each batch is forwarded to
    ``POST /v1/batch`` in turn. A refused part stops the rest and is shown beside the jobs
    already started, which keep running. A list with rows that are not URLs is answered with the
    batch preview of its valid URLs instead, and nothing is sent.

    The key is checked first (no rate-limit hit: each forwarded batch counts), so no form of a
    caller without a valid key is parsed. The form may have a field per URL (the tree's checked
    boxes), at most `_max_fields`; a larger one is answered with a message. Its bytes are bounded
    by the route's body limit."""
    try:
        principal = await authenticate(request)
    except ServiceError as error:
        return refusal(error, request_language(request))
    form = await read_form(
        request,
        max_part_size=form_part_limit(settings, f"{RUN_PREFIX}/batch-urls"),
        max_fields=_max_fields(settings),
        max_files=0,
    )
    if isinstance(form, Message):  # more fields than allowed, or malformed
        return _form_error(request, form)
    try:
        lines = _lines(form)
        fields = text_fields(form)
    finally:
        await form.close()
    try:
        listed = parse_url_lines(lines, max_rows=_max_listed(settings))
        body = build_body("batch", fields)
        if listed.invalid:
            return _preview(
                request, listed, settings, role=principal.role, source="list", chosen=fields
            )
        if not listed.urls:
            return _form_error(request, NO_URLS)
        parts = split_batches(
            listed.urls,
            per_batch=settings.batch_max_urls,
            max_batches=settings.batch_max_open_per_client,
        )
    except UrlListError as error:
        return _form_error(request, error.message)
    except OptionsError as error:
        return templates.TemplateResponse(request, "form_error.html", options_error(error, request))
    _log.info("ui_batch_split", parts=len(parts), urls=len(listed.urls))
    path = OPERATIONS["batch"].path
    started: list[JobAccepted] = []
    refused: _Refused | None = None
    first: tuple[Forwarded, dict[str, Any]] | None = None
    for number, part in enumerate(parts, start=1):
        part_body = {**body, "urls": part}
        answer = await forward(request, "POST", path, json=part_body)
        data = (answer.body or {}).get("data")
        if answer.status == 202 and isinstance(data, dict):
            started.append(JobAccepted.model_validate(data))
            continue
        if not started:
            first = (answer, part_body)
        else:
            message, detail = envelope_error(answer.body, answer.status)
            refused = _Refused(number, len(parts), answer.status, message, detail)
        break
    if first is not None:
        answer, part_body = first
        curl = curl_line(
            str(request.base_url), "POST", path, headers=sent_keys(request), json_body=part_body
        )
        html = render_result(
            op="batch",
            http_status=answer.status,
            envelope=answer.body,
            raw=raw_text(answer),
            curl=curl,
            language=request_language(request),
        )
        return HTMLResponse(html)
    mode = _mode(body.get("mode"))
    return _started(request, started, mode, refused=refused)


def _started(
    request: Request,
    jobs: Sequence[JobAccepted],
    mode: RequestedMode | None,
    *,
    refused: _Refused | None = None,
) -> Response:
    """The progress view of jobs just started, from their 202 answers (no read: the view
    refreshes itself and reads them then)."""
    shown = _Shown(ids=[job.id for job in jobs], offsets=[0] * len(jobs), mode=mode)
    panels = [_accepted_panel(job) for job in jobs]
    refused_context = refused.__dict__ if refused is not None else None
    return _progress_fragment(request, panels, shown, live=False, refused=refused_context)


def _preview(
    request: Request,
    listed: UrlList,
    settings: Settings,
    *,
    role: str,
    source: str,
    chosen: Mapping[str, str] | None = None,
) -> Response:
    """The batch preview of a parsed list (P10-S19): the counts, the first URLs and problems,
    and the valid URLs in a read-only box with the batch options (``chosen``: the fields the
    user had set; "Site rules" as `access_choice` offers it to ``role``) and "Start batch",
    unless there are none or too many."""
    parts = math.ceil(len(listed.urls) / settings.batch_max_urls)
    most = settings.batch_max_open_per_client * settings.batch_max_urls
    context: dict[str, Any] = {
        "source": source,
        "rows": listed.rows,
        "urls": len(listed.urls),
        "invalid": len(listed.invalid),
        "duplicates": listed.duplicates,
        "parts": parts,
        "first": listed.urls[:PREVIEW_URLS],
        "text": "\n".join(listed.urls),
        "too_many": (
            Message("urls.select_at_most", {"max": most})
            if parts > settings.batch_max_open_per_client
            else None
        ),
        "chosen": chosen,
        "access_choice": access_choice(role, settings),
        "browser_enabled": settings.browser_enabled,
        **_problems(listed.invalid),
    }
    return templates.TemplateResponse(request, "batch_preview.html", context)


async def batch_file_view(request: Request, settings: Settings) -> Response:
    """``POST /ui/run/batch-file`` (P10-S19): one rate-limit hit, then the file (at most
    ``UI_BATCH_FILE_MAX_BYTES``) is parsed on the server and shown as a preview whose "Start
    batch" posts the URLs to ``/ui/run/batch-urls``. Nothing is forwarded here."""
    try:
        principal = await ui_principal(request)
    except ServiceError as error:
        return refusal(error, request_language(request))
    limit = settings.ui_batch_file_max_bytes
    form = await read_form(
        request,
        max_part_size=form_part_limit(settings, f"{RUN_PREFIX}/batch-file"),
        max_files=1,
    )
    if isinstance(form, Message):
        return _form_error(request, form)
    too_large = Message("jobs.file_too_large", {"limit": limit})
    try:
        upload = form.get(FILE_PART)
        if not isinstance(upload, UploadFile):
            return _form_error(request, NO_FILE)
        if upload.size is not None and upload.size > limit:
            return _form_error(request, too_large)
        data = await upload.read(limit + 1)
    finally:
        await form.close()
    if len(data) > limit:
        return _form_error(request, too_large)
    try:
        listed = await asyncio.to_thread(
            parse_url_file, data, max_rows=settings.ui_batch_file_max_rows
        )
    except UrlListError as error:
        return _form_error(request, error.message)
    _log.info(
        "ui_batch_file",
        rows=listed.rows,
        urls=len(listed.urls),
        invalid=len(listed.invalid),
        duplicates=listed.duplicates,
    )
    return _preview(request, listed, settings, role=principal.role, source="file")


async def site_structure_view(request: Request) -> Response:
    """``POST /ui/run/site-structure``: forwards the form to ``POST /v1/site-structure``; a 202
    shows the job's progress, anything else the REST answer."""
    try:
        await authenticate(request)  # before the form is parsed; the forward checks it again
    except ServiceError as error:
        return refusal(error, request_language(request))
    form = await read_form(request, max_files=0)
    if isinstance(form, Message):
        return _form_error(request, form)
    try:
        fields = text_fields(form)
    finally:
        await form.close()
    try:
        body = build_body("site-structure", fields)
    except OptionsError as error:
        return templates.TemplateResponse(request, "form_error.html", options_error(error, request))
    path = OPERATIONS["site-structure"].path
    answer = await forward(request, "POST", path, json=body)
    data = (answer.body or {}).get("data")
    if answer.status == 202 and isinstance(data, dict):
        return _started(request, [JobAccepted.model_validate(data)], None)
    curl = curl_line(
        str(request.base_url), "POST", path, headers=sent_keys(request), json_body=body
    )
    html = render_result(
        op="site-structure",
        http_status=answer.status,
        envelope=answer.body,
        raw=raw_text(answer),
        curl=curl,
        language=request_language(request),
    )
    return HTMLResponse(html)


# --- the site tree -------------------------------------------------------------------------------


@dataclass
class _TreeRead:
    """What the reads of a site job found besides its items, and how many items they read."""

    kind: str | None = None
    state: str | None = None
    items: int = 0


async def _read_tree_page(
    state: State,
    principal: Principal,
    job_id: str,
    offset: int,
    builder: TreeBuilder,
    read: _TreeRead,
) -> int | None:
    """Read one page of the job's items into ``builder``; the next offset. The page is dropped
    when this returns, so at most one page of items is held at a time."""
    envelope = await jobs_api.read_job(state, principal, job_id, offset, PAGE_ITEMS)
    view = envelope.data
    assert view is not None  # noqa: S101 - read_job always answers its view
    read.kind, read.state = view.kind, view.state
    read.items += len(view.items)
    if view.kind != "site":
        return None
    await asyncio.to_thread(_add_all, builder, view.items)
    return view.next_offset


def _add_all(builder: TreeBuilder, items: Iterable[JobItem]) -> None:
    for item in items:
        builder.add(item)


async def site_tree_view(request: Request, settings: Settings) -> Response:
    """``GET /ui/run/site-tree?id=`` (P10-S20): the site job read through ``read_job`` in
    pages of 20 items until ``next_offset`` is null, kept as the tree's fields only, and shown
    as a tree of checkboxes; while the job runs, as a snapshot. One rate-limit hit per 500
    items read (``TREE_ITEMS_PER_HIT``, a REST page, M4)."""
    job_id = request.query_params.get("id") or ""
    if not job_id:
        return _form_error(request, NO_JOB)
    try:
        principal = await ui_principal(request)
        jobs_api.check_jobs_enabled(settings)
        builder = TreeBuilder(settings.ui_tree_max_urls)
        read = _TreeRead()
        offset: int | None = 0
        while offset is not None:
            offset = await _read_tree_page(
                request.app.state, principal, job_id, offset, builder, read
            )
    except ServiceError as error:
        return refusal(error, request_language(request))
    pages = math.ceil(read.items / TREE_ITEMS_PER_HIT)
    await count_used(request.app.state, principal, pages - 1)  # the view's own hit is the first
    if read.kind != "site":
        return _form_error(request, NOT_A_SITE)
    tree: SiteTree = await asyncio.to_thread(builder.tree)
    context = {
        **language_context(request_language(request)),
        "job_id": job_id,
        "tree": tree,
        "snapshot": read.state != "completed",
        "found_labels": FOUND_LABELS,
        "access_choice": access_choice(principal.role, settings),
        "browser_enabled": settings.browser_enabled,
    }
    html = await asyncio.to_thread(templates.get_template("tree.html").render, context)
    return HTMLResponse(html)


# --- one item, a screenshot, cancel --------------------------------------------------------------


def _index(value: str | None) -> int | None:
    try:
        index = int(value or "")
    except ValueError:
        return None
    return index if index >= 0 else None


async def job_item_view(request: Request) -> Response:
    """``GET /ui/run/job?id=&index=[&mode=]``: forwards ``GET /v1/jobs/{id}?offset=<index>&
    limit=1`` and shows the item's result with its timeline (``mode``, the request's mode,
    tells a render after ``auto``)."""
    params = request.query_params
    job_id = params.get("id") or ""
    index = _index(params.get("index"))
    if not job_id or index is None:
        return _form_error(request, NO_ITEM_INDEX)
    if not JOB_ID.fullmatch(job_id):
        return _form_error(request, NOT_A_JOB)
    path = OPERATIONS["job"].path.format(job_id=job_id)
    query = {"offset": index, "limit": 1}
    try:
        answer = await forward(request, "GET", path, params=query)
    except ValueError:  # not a plain path under /v1/
        return _form_error(request, NOT_A_JOB)
    curl = curl_line(
        str(request.base_url), "GET", f"{path}?{urlencode(query)}", headers=sent_keys(request)
    )
    data = (answer.body or {}).get("data")
    items = data.get("items") if answer.status == 200 and isinstance(data, dict) else None
    if not isinstance(items, list):
        html = render_result(
            op="job",
            http_status=answer.status,
            envelope=answer.body,
            raw=raw_text(answer),
            language=request_language(request),
        )
        return HTMLResponse(html)
    if not items:
        return _form_error(request, Message("jobs.no_item", {"index": index}))
    item = JobItem.model_validate(items[0])
    if item.result is None:
        context = {"index": item.index, "state": _item_state(item)}
        return templates.TemplateResponse(request, "item_state.html", context)
    envelope = item.result.model_dump(mode="json")
    screenshot, screenshot_note, screenshot_detail = await screenshot_of(request, envelope)
    html = await asyncio.to_thread(
        render_result,
        op="job",
        http_status=answer.status,
        envelope=envelope,
        raw=raw_text(answer),
        curl=curl,
        requested_mode=_mode(params.get("mode")),
        screenshot=screenshot,
        screenshot_note=screenshot_note,
        screenshot_detail=screenshot_detail,
        item=item,
        language=request_language(request),
    )
    return HTMLResponse(html)


async def artifact_view(request: Request) -> Response:
    """``GET /ui/run/artifact?id=&name=``: a job's PNG artifact read from the artifact route,
    as a ``data:`` image."""
    params = request.query_params
    job_id, name = params.get("id") or "", params.get("name") or ""
    url = OPERATIONS["artifact"].path.format(job_id=job_id, name=name)
    found = artifact_of({"data": {"screenshot": {"url": url}}})
    if found is None or not JOB_ID.fullmatch(job_id):
        return _form_error(request, NOT_AN_ARTIFACT)
    screenshot, note, detail = await read_screenshot(request, *found)
    context = {"screenshot": screenshot, "note": note, "detail": detail}
    return templates.TemplateResponse(request, "artifact.html", context)


async def cancel_view(request: Request) -> Response:
    """``POST /ui/run/cancel`` (D4 a): forwards ``DELETE /v1/jobs/{id}`` for the posted ``id``."""
    try:
        await authenticate(request)  # before the form is parsed; the forward checks it again
    except ServiceError as error:
        return refusal(error, request_language(request))
    form = await read_form(request, max_files=0)
    if isinstance(form, Message):
        return _form_error(request, form)
    try:
        job_id = text_fields(form).get("id", "")
    finally:
        await form.close()
    if not JOB_ID.fullmatch(job_id):
        return _form_error(request, NOT_A_JOB)
    path = OPERATIONS["cancel"].path.format(job_id=job_id)
    try:
        answer = await forward(request, "DELETE", path)
    except ValueError:  # not a plain path under /v1/
        return _form_error(request, NOT_A_JOB)
    data = (answer.body or {}).get("data")
    if answer.status == 200 and isinstance(data, dict):
        context = {"job_id": data.get("id"), "kind": data.get("kind")}
        return templates.TemplateResponse(request, "cancel.html", context)
    html = render_result(
        op="cancel",
        http_status=answer.status,
        envelope=answer.body,
        raw=raw_text(answer),
        language=request_language(request),
    )
    return HTMLResponse(html)


def add_job_routes(run: APIRouter, settings: Settings) -> None:
    """Add the job views to the ``/ui/run`` router."""

    @run.post("/batch-urls")
    async def batch_urls(request: Request) -> Response:
        return await batch_urls_view(request, settings)

    @run.post("/batch-file")
    async def batch_file(request: Request) -> Response:
        return await batch_file_view(request, settings)

    @run.post("/site-structure")
    async def site_structure(request: Request) -> Response:
        return await site_structure_view(request)

    @run.get("/progress")
    async def progress(request: Request) -> Response:
        return await progress_view(request, settings)

    @run.get("/site-tree")
    async def site_tree(request: Request) -> Response:
        return await site_tree_view(request, settings)

    @run.get("/job")
    async def job(request: Request) -> Response:
        return await job_item_view(request)

    @run.get("/artifact")
    async def artifact(request: Request) -> Response:
        return await artifact_view(request)

    @run.post("/cancel")
    async def cancel(request: Request) -> Response:
        return await cancel_view(request)
