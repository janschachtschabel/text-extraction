"""The UI's routes (rulings P10-S12 to P10-S14): the page ``GET /ui``, the login
``POST /ui/run/login`` and the guard of every ``/ui/run`` route. None is in the OpenAPI spec."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Depends
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request
from starlette.responses import Response

from text_extraction.auth import principal_for
from text_extraction.settings import Settings
from text_extraction.ui.forward import forward
from text_extraction.ui.security import PREFIX, RUN_PREFIX, require_htmx

TEMPLATES_DIR: Final = Path(__file__).parent / "templates"
STATIC_DIR: Final = Path(__file__).parent / "static"

# Autoescape on for every template, whatever its name (P10-S12).
templates: Final = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
)

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


# The application's tabs, in order; the forms of the operations (Tasks 8 and 9) add theirs.
TABS: Final[tuple[Tab, ...]] = ()


def _app_context(settings: Settings, name: str, role: str) -> dict[str, Any]:
    return {
        "principal_name": name,
        "role": role,
        "auth_none": settings.auth_mode == "none",
        "banner": BANNER,
        "tabs": TABS,
    }


def _envelope_message(body: dict[str, Any] | None, status: int) -> str:
    error = (body or {}).get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else f"The service answered HTTP {status}."


def create_ui_router(settings: Settings) -> APIRouter:
    """The UI's routes: ``GET /ui`` (no key needed: the page holds nothing but forms) and the
    ``/ui/run`` routes behind `require_htmx`."""
    router = APIRouter(include_in_schema=False)

    @router.get(PREFIX)
    async def page(request: Request) -> Response:
        """Under ``AUTH_MODE=api_key`` the login box only; under ``none`` the application, under
        the banner."""
        context: dict[str, Any] = {"login": settings.auth_mode == "api_key"}
        if not context["login"]:
            anonymous = principal_for(settings, None)
            context.update(_app_context(settings, anonymous.name, anonymous.role))
        return templates.TemplateResponse(request, "index.html", context)

    run = APIRouter(prefix=RUN_PREFIX, dependencies=[Depends(require_htmx)])

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
        message = _envelope_message(answer.body, answer.status)
        return templates.TemplateResponse(request, "login.html", {"error": message})

    router.include_router(run)
    return router
