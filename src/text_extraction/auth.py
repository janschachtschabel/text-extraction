"""API key authentication and roles.

Keys arrive as ``Authorization: Bearer <key>``. Only their SHA-256 is configured, and the
raw key is never stored, logged or echoed.
"""

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

import structlog
from fastapi import Depends, Request

from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.settings import Settings

Role = Literal["user", "admin"]

_BEARER_PREFIX = "bearer "
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class Principal:
    """Who is calling: the configured key name, its role and its own rate limit."""

    name: str
    role: Role
    rate_limit_per_minute: int | None


_ANONYMOUS = Principal(name="anonymous", role="admin", rate_limit_per_minute=None)


def _unauthorized() -> ServiceError:
    # One message for every failure, so a caller learns nothing about why the key was refused.
    return ServiceError(
        ErrorCode.UNAUTHORIZED,
        "A valid API key is required.",
        401,
        headers=_UNAUTHORIZED_HEADERS,
    )


def _bearer_token(header: str | None) -> str | None:
    if header is None or header[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    return header[len(_BEARER_PREFIX) :].strip() or None


def _find_principal(settings: Settings, token: str) -> Principal | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    found: Principal | None = None
    # Compare against every configured hash, so timing does not reveal which one matched.
    for key in settings.api_keys:
        if hmac.compare_digest(digest, key.sha256):
            found = Principal(key.name, key.role, key.rate_limit_per_minute)
    return found


def _record_principal(request: Request, principal: Principal) -> None:
    """Make the principal's name visible in the log context and on the access line."""
    structlog.contextvars.bind_contextvars(principal=principal.name)
    # `TraceMiddleware` writes the access line after the request context may have changed,
    # so it reads the name from the ASGI scope state.
    request.scope.setdefault("state", {})["principal"] = principal.name


def principal_for(settings: Settings, authorization: str | None) -> Principal:
    """The caller of a request whose ``Authorization`` header is ``authorization`` (None
    without one), or a 401 `ServiceError`. Under ``AUTH_MODE=none`` every caller is the
    anonymous admin.

    It checks the key only: the caller checks the role a door needs (every key has ``user``;
    ``admin`` routes need ``principal.role == "admin"``), counts the call with ``check_rate``,
    and binds the name to the log context and the access line (as ``authenticate`` does)."""
    if settings.auth_mode == "none":
        # Behind the B-API gateway, which authorises the caller.
        return _ANONYMOUS
    token = _bearer_token(authorization)
    if token is None:
        raise _unauthorized()
    found = _find_principal(settings, token)
    if found is None:
        raise _unauthorized()
    return found


async def authenticate(request: Request) -> Principal:
    """FastAPI dependency: the caller's `Principal`, or a 401 `ServiceError`."""
    settings: Settings = request.app.state.settings
    principal = principal_for(settings, request.headers.get("authorization"))
    _record_principal(request, principal)
    return principal


def require_role(role: Role) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: admin passes everywhere, a user only on user routes."""

    async def dependency(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if role == "admin" and principal.role != "admin":
            raise ServiceError(ErrorCode.FORBIDDEN, "This endpoint requires the admin role.", 403)
        return principal

    return dependency
