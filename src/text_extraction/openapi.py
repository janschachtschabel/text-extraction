"""The public OpenAPI document as the service answers: the bearer key, and the error envelope.

FastAPI derives the document from the routes, but the routes read the key by hand
(``auth.py``) and every error answer is the result envelope (``errors.py``), not FastAPI's
``HTTPValidationError``. ``install_openapi`` corrects both once, when the document is first
built: a ``bearerAuth`` security scheme (``AUTH_MODE=api_key``), an ``ErrorEnvelope`` schema,
the 422 of every operation pointing at it, the error statuses every ``/v1`` operation can
answer, and a tag per area.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from text_extraction.settings import Settings

ERROR_ENVELOPE = "ErrorEnvelope"
_REF = {"$ref": f"#/components/schemas/{ERROR_ENVELOPE}"}
_ERROR_SCHEMA: dict[str, Any] = {
    "title": ERROR_ENVELOPE,
    "type": "object",
    "description": "Every error answer: `status` is `failed`, `error` says why.",
    "required": ["status", "error"],
    "properties": {
        "status": {"const": "failed", "title": "Status"},
        "data": {"type": "null", "title": "Data"},
        "error": {"$ref": "#/components/schemas/ErrorInfo"},
        "trace_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Trace Id"},
    },
}
_DESCRIPTIONS = {
    "400": "Bad request: an option or route needs a part of the service that is switched off "
    "(jobs too), or a header is invalid (`invalid_request`).",
    "401": "No API key, or an unknown one (`unauthorized`).",
    "403": "The option is for administrators only (`forbidden`).",
    "404": "No such job or artifact for this key (`not_found`).",
    "413": "The request body is too large (`invalid_request`).",
    "415": "The body is not JSON (`invalid_request`); send `Content-Type: application/json`.",
    "422": "The request is invalid (`invalid_request`, `error.details` names the fields).",
    "429": "Too many requests or open jobs for this key (`rate_limited`), see `Retry-After`.",
    "503": "The service is busy or its state store is unavailable (`internal_error`, "
    "usually retryable; a retryable 503 carries `Retry-After`).",
}
_TAGS = (
    ("/v1/jobs", "jobs"),
    ("/v1/batch", "jobs"),
    ("/v1/site-structure", "site structure"),
    ("/v1/structured", "structured output"),
    ("/v1/chunk", "structured output"),
    ("/v1/screenshot", "extraction"),
    ("/v1/extract", "extraction"),
    ("/v1/info", "service"),
)


def _statuses(path: str, method: str, settings: Settings) -> list[str]:
    """The error statuses an operation can answer, besides its own."""
    statuses = ["422", "503"]
    if settings.auth_mode == "api_key":
        statuses.append("401")
    if settings.rate_limit_enabled or path in ("/v1/batch", "/v1/site-structure"):
        statuses.append("429")
    if method == "post":
        statuses += ["400", "403", "413"]
        if path != "/v1/extract/file":  # the upload is multipart
            statuses.append("415")
    if "{job_id}" in path:
        statuses += ["400", "404"]  # 400 while jobs are off
    return statuses


def _tag(path: str) -> str | None:
    return next((tag for prefix, tag in _TAGS if path.startswith(prefix)), None)


def build_openapi(app: FastAPI, settings: Settings) -> dict[str, Any]:
    """The document of ``app`` with the corrections of this module."""
    spec = get_openapi(
        title=app.title, version=app.version, description=app.description, routes=app.routes
    )
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    schemas[ERROR_ENVELOPE] = _ERROR_SCHEMA
    if settings.auth_mode == "api_key":
        components["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "An API key of the service: `Authorization: Bearer <key>`.",
            }
        }
        spec["security"] = [{"bearerAuth": []}]
    for path, operations in spec.get("paths", {}).items():
        for method, operation in operations.items():
            responses = operation.setdefault("responses", {})
            for status in _statuses(path, method, settings):
                response = responses.setdefault(status, {"description": _DESCRIPTIONS[status]})
                response["content"] = {"application/json": {"schema": _REF}}
                if status == "422":
                    response["description"] = _DESCRIPTIONS["422"]
            if (tag := _tag(path)) is not None:
                operation["tags"] = [tag]
    return spec


def install_openapi(app: FastAPI, settings: Settings) -> None:
    """Serve ``build_openapi`` at ``/openapi.json`` (built once, on the first request)."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app, settings)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
