"""The UI's routes (rulings P10-S12 to P10-S20): the page ``GET /ui``, the login
``POST /ui/run/login``, the single operations ``POST /ui/run/<op>`` (each forwards one REST
call and shows its answer), the previews ``POST /ui/run/images``, the job views
(``job_views.py``) and the guard of every ``/ui/run`` route. None is in the OpenAPI spec."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast, get_args

from fastapi import APIRouter, Depends
from prometheus_client import Counter
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from text_extraction.auth import authenticate, principal_for
from text_extraction.compliance import ComplianceMode, resolve_mode
from text_extraction.errors import ServiceError
from text_extraction.file_models import FILE_PART
from text_extraction.ratelimit import check_rate_left
from text_extraction.settings import Settings
from text_extraction.ui.answers import (
    envelope_message,
    raw_text,
    read_form,
    refusal,
    screenshot_of,
    sent_keys,
    text_fields,
)
from text_extraction.ui.forms import (
    OPERATIONS,
    SINGLE_OPERATIONS,
    OptionsError,
    access_choice,
    build_body,
    multipart_fields,
)
from text_extraction.ui.forward import Forwarded, forward
from text_extraction.ui.i18n import STATE_KEY, Language, page_language, run_language
from text_extraction.ui.job_views import add_job_routes
from text_extraction.ui.previews import SHOWN, load_previews
from text_extraction.ui.results import curl_line, render_result
from text_extraction.ui.security import (
    PREFIX,
    RUN_PREFIX,
    form_part_limit,
    require_htmx,
    ui_principal,
)
from text_extraction.ui.templating import request_language, templates
from text_extraction.ui.timeline import RequestedMode
from text_extraction.vision.fetch import ImageFetcher

STATIC_DIR: Final = Path(__file__).parent / "static"

BANNER: Final = (
    "This service runs without authentication: anyone who can reach it can use the API and this UI."
)
NOT_VALID: Final = "This key is not valid."
# Sent with a refused key: ui.js drops the stored keys (P10-S13).
LOGIN_FAILED_EVENT: Final = "ui-login-failed"


@dataclass(frozen=True)
class Tab:
    """One tab of the application: its name, its label and the template of its form."""

    name: str
    label: str
    template: str


# The application's tabs, in order.
TABS: Final[tuple[Tab, ...]] = (
    Tab("extract", "Extract", OPERATIONS["extract"].form),
    Tab("file", "File", OPERATIONS["file"].form),
    Tab("screenshot", "Screenshot", OPERATIONS["screenshot"].form),
    Tab("structured", "Structured", OPERATIONS["structured"].form),
    Tab("chunk", "Chunk", OPERATIONS["chunk"].form),
    Tab("batch", "Batch", OPERATIONS["batch"].form),
    Tab("batch-file", "Batch file", "forms/batch_file.html"),
    Tab("site-structure", "Site structure", OPERATIONS["site-structure"].form),
    Tab("info", "Info", OPERATIONS["info"].form),
)
NO_FILE: Final = "Choose a file to upload."
_MODES: Final = frozenset(get_args(RequestedMode))
_COMPLIANCE_MODES: Final = frozenset(get_args(ComplianceMode))


def _app_context(settings: Settings, name: str, role: str) -> dict[str, Any]:
    return {
        "principal_name": name,
        "role": role,
        "auth_none": settings.auth_mode == "none",
        "banner": BANNER,
        "tabs": TABS,
        "access_choice": access_choice(role, settings),
        "browser_enabled": settings.browser_enabled,
    }


async def _forward_single(
    request: Request, op: str, body: dict[str, Any], form: FormData
) -> tuple[Forwarded, str] | str:
    """Forward the body of ``op`` and return the answer with its curl line, or the message of a
    form that cannot be sent."""
    operation = OPERATIONS[op]
    keys = sent_keys(request)
    base = str(request.base_url)
    if op == "file":
        upload = form.get(FILE_PART)
        if not isinstance(upload, UploadFile):
            return NO_FILE
        fields = multipart_fields(body)
        name = upload.filename or "file"
        files = {FILE_PART: (name, upload.file, upload.content_type or "application/octet-stream")}
        answer = await forward(request, "POST", operation.path, data=fields, files=files)
        curl = curl_line(base, "POST", operation.path, headers=keys, form=fields, file_name=name)
    elif operation.method == "GET":
        answer = await forward(request, "GET", operation.path)
        curl = curl_line(base, "GET", operation.path, headers=keys)
    else:
        answer = await forward(request, "POST", operation.path, json=body)
        curl = curl_line(base, "POST", operation.path, headers=keys, json_body=body)
    return answer, curl


def _single_view(op: str, settings: Settings) -> Callable[[Request], Awaitable[Response]]:
    part_limit = form_part_limit(settings, f"{RUN_PREFIX}/{op}")
    max_files = 1 if op == "file" else 0

    async def view(request: Request) -> Response:
        """Build the REST body of the form, forward it and show the answer (always 200, so htmx
        swaps it). A form that cannot become a body is answered with its message, and nothing
        is forwarded.

        The key and the rate limit are checked first, as REST checks them (R16), so the form of
        a caller without a valid key, or over its limit, is never read. The rate check does not
        count: the forwarded call counts the view's one hit."""
        try:
            principal = await authenticate(request)
            await check_rate_left(request.app.state, principal)
        except ServiceError as error:
            return refusal(error)
        form = await read_form(request, max_part_size=part_limit, max_files=max_files)
        if isinstance(form, str):
            return templates.TemplateResponse(request, "form_error.html", {"message": form})
        try:
            body = build_body(op, text_fields(form))
        except OptionsError as error:
            await form.close()
            return templates.TemplateResponse(request, "form_error.html", {"message": str(error)})
        try:
            forwarded = await _forward_single(request, op, body, form)
        finally:
            await form.close()  # an upload's spooled file
        if isinstance(forwarded, str):
            return templates.TemplateResponse(request, "form_error.html", {"message": forwarded})
        answer, curl = forwarded
        mode = body.get("mode")
        screenshot, screenshot_note = await screenshot_of(request, answer.body)
        # In a thread: a long content's Markdown takes a while to render.
        html = await asyncio.to_thread(
            render_result,
            op=op,
            http_status=answer.status,
            envelope=answer.body,
            raw=raw_text(answer),
            curl=curl,
            requested_mode=mode if mode in _MODES else None,
            screenshot=screenshot,
            screenshot_note=screenshot_note,
            language=request_language(request),
        )
        return HTMLResponse(html)

    view.__name__ = f"run_{op}"
    return view


async def _preview_view(request: Request, settings: Settings) -> Response:
    """Fetch the posted image URLs (``url``, with their ``alt`` texts) through the UI's
    ``ImageFetcher`` with the caller's resolved compliance mode (``compliance``) and show them
    as ``data:`` images (P10-S16). One rate-limit hit, whatever the number of images."""
    try:
        principal = await ui_principal(request)
        form = await read_form(request, max_files=0)
        if isinstance(form, str):
            return templates.TemplateResponse(request, "form_error.html", {"message": form})
        requested = form.get("compliance")
        chosen = cast(ComplianceMode, requested) if requested in _COMPLIANCE_MODES else None
        mode = resolve_mode(chosen, principal, settings)
    except ServiceError as error:
        return refusal(error)
    urls = [value for value in form.getlist("url") if isinstance(value, str)]
    alts = [value for value in form.getlist("alt") if isinstance(value, str)]
    alt_of: dict[str, str] = {}
    for url, alt in zip(urls, alts, strict=False):
        alt_of.setdefault(url, alt)
    fetcher: ImageFetcher = request.app.state.ui_images
    counter: Counter = request.app.state.ui_image_previews
    previews = await load_previews(
        urls, fetcher=fetcher, mode=mode, max_bytes=settings.ui_image_max_bytes
    )
    entries = []
    for preview in previews:
        counter.labels(preview.skipped or SHOWN).inc()
        entries.append(
            {
                "url": preview.url,
                "data_uri": preview.data_uri,
                "skipped": preview.skipped,
                "alt": alt_of.get(preview.url, ""),
            }
        )
    return templates.TemplateResponse(request, "images.html", {"previews": entries})


def _language_dependency(
    choose: Callable[[Request, Settings], Language], settings: Settings
) -> Callable[[Request], Awaitable[None]]:
    """A dependency that sets ``request.state.ui_language`` to ``choose``'s language: the
    templates render in it, and `UiHeadersMiddleware` sends it as ``Content-Language``."""

    async def set_language(request: Request) -> None:
        setattr(request.state, STATE_KEY, choose(request, settings))

    return set_language


def create_ui_router(settings: Settings) -> APIRouter:
    """The UI's routes: ``GET /ui`` (no key needed: the page holds nothing but forms) and the
    ``/ui/run`` routes behind `require_htmx`. The page takes its language from `page_language`,
    the ``/ui/run`` fragments from `run_language` (P12-S6)."""
    router = APIRouter(include_in_schema=False)

    @router.get(PREFIX, dependencies=[Depends(_language_dependency(page_language, settings))])
    async def page(request: Request) -> Response:
        """Under ``AUTH_MODE=api_key`` the login box only; under ``none`` the application, under
        the banner."""
        context: dict[str, Any] = {"login": settings.auth_mode == "api_key"}
        if not context["login"]:
            anonymous = principal_for(settings, None)
            context.update(_app_context(settings, anonymous.name, anonymous.role))
        return templates.TemplateResponse(request, "index.html", context)

    run = APIRouter(
        prefix=RUN_PREFIX,
        dependencies=[
            Depends(require_htmx),
            Depends(_language_dependency(run_language, settings)),
        ],
    )

    @run.post("/login")
    async def login(request: Request) -> Response:
        """Forward ``GET /v1/info`` with the browser's key: 200 renders the application, 401 the
        login box with "This key is not valid." and nothing else, any other answer the login box
        with its message (the stored key is kept: a 429 or 503 says nothing about it)."""
        answer = await forward(request, "GET", "/v1/info")
        data = (answer.body or {}).get("data")
        if answer.status == 200 and isinstance(data, dict):
            context = _app_context(settings, str(data.get("principal")), str(data.get("role")))
            return templates.TemplateResponse(request, "app.html", context)
        if answer.status == 401:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": NOT_VALID},
                headers={"HX-Trigger": LOGIN_FAILED_EVENT},
            )
        message = envelope_message(answer.body, answer.status)
        return templates.TemplateResponse(request, "login.html", {"error": message})

    for op in SINGLE_OPERATIONS:
        run.add_api_route(f"/{op}", _single_view(op, settings), methods=["POST"])

    @run.post("/images")
    async def images(request: Request) -> Response:
        """Previews of a result's images (P10-S16, D7 a)."""
        return await _preview_view(request, settings)

    add_job_routes(run, settings)

    router.include_router(run)
    return router
