"""Models of browser mode: page actions, cookies and headers, screenshots and their artifacts.

The request options (actions, cookies, headers) are defined in ``browser_options`` and
re-exported here, since ``extract_models`` needs them and this module needs ``extract_models``.
"""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from text_extraction.browser_options import (
    MAX_ACTIONS,
    MAX_COOKIES,
    AcceptCookiesAction,
    Action,
    ArtifactRef,
    BrowserCookie,
    BrowserSecrets,
    ClickAction,
    EvaluateAction,
    ExpandAllAction,
    Headers,
    InfiniteScrollAction,
    PressAction,
    ScrollAction,
    SelectAction,
    TypeAction,
    WaitForAction,
    check_cookie_domains,
    validate_headers,
)
from text_extraction.compliance import ComplianceMode, ComplianceReport
from text_extraction.extract_models import ExtractResult, Timings
from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.quality import Quality

__all__ = [
    "MAX_ACTIONS",
    "AcceptCookiesAction",
    "Action",
    "ArtifactRef",
    "BrowserCookie",
    "BrowserSecrets",
    "ClickAction",
    "EvaluateAction",
    "ExpandAllAction",
    "InfiniteScrollAction",
    "PressAction",
    "ScreenshotRequest",
    "ScreenshotResult",
    "ScrollAction",
    "SelectAction",
    "TypeAction",
    "WaitForAction",
    "screenshot_view",
    "validate_headers",
]


class ScreenshotRequest(BaseModel, extra="forbid", hide_input_in_errors=True):
    """Body of ``POST /v1/screenshot``. The fields have the limits of ``ExtractRequest``."""

    url: str = Field(max_length=MAX_URL_LENGTH, description="The http(s) URL to capture.")
    full_page: bool = Field(
        default=True, description="Capture the whole page; `false` the first viewport only."
    )
    actions: list[Action] = Field(
        default=[],
        max_length=MAX_ACTIONS,
        repr=False,
        description="Page actions to run before the capture, at most 20, in order.",
    )
    cookies: list[BrowserCookie] = Field(
        default=[],
        max_length=MAX_COOKIES,
        repr=False,
        description="Cookies to set before the page loads, at most 50. A `domain` must be the "
        "URL's host or a parent domain of it.",
    )
    headers: Headers = Field(
        default={},
        repr=False,
        description="Extra request headers, at most 20. They are sent only to the requested "
        "URL's host and port, never to other sites, also not across a redirect.",
    )
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's. "
        "`ignore` also allows unblock mode where the operator enabled it.",
    )
    wait_seconds: float | None = Field(
        default=None,
        ge=0,
        le=60,
        description="As for `/v1/extract`: after this many seconds a running capture becomes a "
        "job and the answer is 202.",
    )

    @model_validator(mode="after")
    def _cookie_domains(self) -> Self:
        check_cookie_domains(self.url, self.cookies)
        return self

    def secrets(self) -> BrowserSecrets:
        return BrowserSecrets(actions=self.actions, cookies=self.cookies, headers=self.headers)


class ScreenshotResult(BaseModel):
    """What a screenshot request answers with: the extraction result without its text."""

    url: str
    final_url: str | None
    upstream_status: int | None
    mode_used: Literal["fast", "browser"]
    unblocked: bool
    attempts: int
    title: str | None
    screenshot: ArtifactRef | None
    quality: Quality | None
    timings: Timings
    compliance: ComplianceReport
    warnings: list[str]


def screenshot_view(result: ExtractResult) -> ScreenshotResult:
    """The screenshot answer for an extraction result."""
    return ScreenshotResult(
        url=result.url,
        final_url=result.final_url,
        upstream_status=result.upstream_status,
        mode_used=result.mode_used,
        unblocked=result.unblocked,
        attempts=result.attempts,
        title=result.title,
        screenshot=result.screenshot,
        quality=result.quality,
        timings=result.timings,
        compliance=result.compliance,
        warnings=result.warnings,
    )
