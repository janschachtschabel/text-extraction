"""Models of browser mode: page actions, cookies and headers, screenshots and their artifacts.

The request options (actions, cookies, headers) are defined in ``browser_options`` and
re-exported here, since ``extract_models`` needs them and this module needs ``extract_models``.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from text_extraction.browser_options import (
    ACTIONS_DESCRIPTION,
    COOKIES_DESCRIPTION,
    HEADERS_DESCRIPTION,
    MAX_ACTIONS,
    AcceptCookiesAction,
    Action,
    ActionList,
    ArtifactRef,
    BrowserCookie,
    BrowserSecrets,
    ClickAction,
    CookieList,
    EvaluateAction,
    ExpandAllAction,
    Headers,
    InfiniteScrollAction,
    PressAction,
    ScreenshotFormat,
    ScreenshotOptions,
    ScrollAction,
    SelectAction,
    TypeAction,
    Viewport,
    WaitForAction,
    WaitSeconds,
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


class ScreenshotRequest(BaseModel, extra="forbid", hide_input_in_errors=True, strict=True):
    """Body of ``POST /v1/screenshot``. The fields have the limits of ``ExtractRequest``."""

    url: str = Field(max_length=MAX_URL_LENGTH, description="The http(s) URL to capture.")
    full_page: bool = Field(
        default=True,
        description="Capture the whole page (cut at `SCREENSHOT_MAX_HEIGHT_PX`, with the warning "
        "`screenshot_clipped`); `false` the first viewport only.",
    )
    viewport: Viewport | None = Field(
        default=None,
        description="The browser window the page is rendered in (width 320 to 3840, height 240 "
        "to 2160); a narrow one gets the page's mobile layout. Default 1920x1080. A full-page "
        "capture holds at most 1920 x `SCREENSHOT_MAX_HEIGHT_PX` pixels, so a wider viewport "
        "gets a shorter capture (its height is cut at `SCREENSHOT_MAX_HEIGHT_PX` x 1920 / "
        "viewport width).",
    )
    format: ScreenshotFormat = Field(
        default="png",
        description="Image format: `png` (default, lossless), `jpeg` or `webp`. A `webp` image "
        "is at most 16,383 px high (the WebP limit): a taller one loses its bottom rows, with "
        "the warning `screenshot_clipped`.",
    )
    quality: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Quality of a `jpeg` or `webp` image, 1 to 100 (default 80). Not with "
        "`format: png` (422).",
    )
    width: int | None = Field(
        default=None,
        ge=16,
        le=3840,
        description="Scale the image down to this width in pixels (16 to 3840, at most the "
        "viewport's width, else 422; it is never scaled up), keeping the aspect ratio. "
        "Default: the viewport's width.",
    )
    actions: ActionList = Field(
        default=[], repr=False, description=f"{ACTIONS_DESCRIPTION} They run before the capture."
    )
    cookies: CookieList = Field(default=[], repr=False, description=COOKIES_DESCRIPTION)
    headers: Headers = Field(default={}, repr=False, description=HEADERS_DESCRIPTION)
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's. "
        "`ignore` also allows unblock mode where the operator enabled it: a browser that gets "
        "past bot protection, for sites that allowed extraction by contract.",
    )
    wait_seconds: WaitSeconds = Field(
        default=None,
        description="As for `/v1/extract`: after this many seconds a running capture becomes a "
        "job and the answer is 202 with it (kind `screenshot`).",
    )

    @model_validator(mode="after")
    def _cookie_domains(self) -> Self:
        check_cookie_domains(self.url, self.cookies)
        return self

    @model_validator(mode="after")
    def _screenshot_options_valid(self) -> Self:
        self.screenshot_options()  # the limits and combinations of ScreenshotOptions: else 422
        return self

    def screenshot_options(self) -> ScreenshotOptions | None:
        """The screenshot options these fields make; None while all four are at their defaults,
        so the extraction request stays the one it was before they existed."""
        if (self.viewport, self.format, self.quality, self.width) == (None, "png", None, None):
            return None
        fields: dict[str, Any] = {
            "format": self.format,
            "quality": self.quality,
            "width": self.width,
        }
        if self.viewport is not None:
            fields["viewport"] = self.viewport
        return ScreenshotOptions(**fields)

    def secrets(self) -> BrowserSecrets:
        return BrowserSecrets(actions=self.actions, cookies=self.cookies, headers=self.headers)


class ScreenshotResult(BaseModel):
    """What a screenshot request answers with: the extraction result without its text."""

    url: str
    final_url: str | None
    upstream_status: int | None
    mode_used: Literal["fast", "browser"] | None  # None: an own fault before any mode ran
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
    if (
        result.url is None or result.mode_used == "upload"
    ):  # pragma: no cover - uploads have no page
        raise ValueError("a screenshot needs a page, not an upload")
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
