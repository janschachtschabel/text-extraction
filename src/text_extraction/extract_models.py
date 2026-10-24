"""Request and result models of ``POST /v1/extract``."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from text_extraction.browser_options import (
    MAX_ACTIONS,
    MAX_COOKIES,
    Action,
    ArtifactRef,
    BrowserCookie,
    BrowserSecrets,
    Headers,
    check_cookie_domains,
)
from text_extraction.compliance import ComplianceMode, ComplianceReport
from text_extraction.convert import Link, OutputFormat
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.fingerprint import Fingerprint
from text_extraction.metadata import PageMetadata
from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.quality import Quality
from text_extraction.tracing import current_trace_id

CacheState = Literal["miss", "hit", "revalidated", "bypass"]


class ExtractRequest(BaseModel, extra="forbid", hide_input_in_errors=True):
    """Body of ``POST /v1/extract``."""

    url: str = Field(max_length=MAX_URL_LENGTH, description="The http(s) URL to extract.")
    mode: Literal["fast", "auto", "browser"] = Field(
        default="auto",
        description="`fast`: HTTP only. `auto`: uses a browser when the page needs one and "
        "browser mode is enabled. `browser`: always renders the page in a browser.",
    )
    format: OutputFormat = Field(default="markdown", description="Format of `content`.")
    metadata: bool = Field(default=True, description="Include the page's DOM metadata.")
    links: bool = Field(default=False, description="Include the page's links.")
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's. "
        "`ignore` also allows unblock mode where the operator enabled it.",
    )
    cache: bool = Field(
        default=True, description="Use the result cache; `false` neither reads nor stores."
    )
    wait_seconds: float | None = Field(
        default=None,
        ge=0,
        le=60,
        description="Without it, the request answers when the extraction ends (the unchanged "
        "default). With it, an extraction that is still running after this many seconds becomes "
        "a job: the answer is then 202 with the job's id and `status_url`, and "
        "`GET /v1/jobs/{id}` has the result. 0 hands over at once. Ignored when jobs are "
        "disabled (`JOBS_ENABLED=false`).",
    )
    actions: list[Action] = Field(
        default=[],
        max_length=MAX_ACTIONS,
        repr=False,
        description="Page actions to run in the browser before the text is read, at most 20, in "
        "order. Not with `mode: fast`.",
    )
    cookies: list[BrowserCookie] = Field(
        default=[],
        max_length=MAX_COOKIES,
        repr=False,
        description="Cookies to set before the page loads, at most 50. A `domain` must be the "
        "URL's host or a parent domain of it. Not with `mode: fast`.",
    )
    headers: Headers = Field(
        default={},
        repr=False,
        description="Extra request headers for the browser, at most 20. They are sent only to "
        "the requested URL's host and port, never to other sites, also not across a redirect. "
        "Not with `mode: fast`.",
    )
    screenshot: bool = Field(
        default=False, description="Also capture a screenshot. Not with `mode: fast`."
    )
    full_page: bool = Field(
        default=True,
        description="Screenshot of the whole page; `false`: the first viewport only. Not with "
        "`mode: fast`.",
    )

    @model_validator(mode="after")
    def _browser_options(self) -> Self:
        if self.mode == "fast" and (
            self.actions or self.cookies or self.headers or self.screenshot or not self.full_page
        ):
            raise ValueError(
                "mode fast cannot take actions, cookies, headers, screenshot or full_page: "
                "they need the browser (use mode auto or browser)"
            )
        check_cookie_domains(self.url, self.cookies)
        return self

    def secrets(self) -> BrowserSecrets:
        """The options that may carry secrets."""
        return BrowserSecrets(actions=self.actions, cookies=self.cookies, headers=self.headers)


class Timings(BaseModel):
    """Milliseconds spent in this request. ``fetch_ms`` includes the wait for the host's turn;
    ``convert_ms`` covers decoding, conversion and the steps after it; ``render_ms`` is the time
    in the browser (0 in fast mode)."""

    total_ms: int
    compliance_ms: int
    fetch_ms: int
    convert_ms: int
    render_ms: int = 0  # browser: navigation, actions and settling; 0 in fast mode


class ExtractResult(BaseModel):
    """What one extraction produced; on failure as much of it as was known."""

    url: str
    final_url: str | None
    upstream_status: int | None  # of the response the content came from
    content_type: str | None
    mode_used: Literal["fast", "browser"]
    attempts: int  # fetches of the target in this request
    language: str | None
    word_count: int
    title: str | None
    format: OutputFormat
    content: str | None
    quality: Quality | None
    timings: Timings
    compliance: ComplianceReport
    metadata: PageMetadata | None
    links: list[Link] | None
    fingerprint: Fingerprint | None
    cache: CacheState
    warnings: list[str]
    # Browser mode only; the defaults keep results cached before browser mode valid.
    screenshot: ArtifactRef | None = None
    unblocked: bool = False  # the page was loaded in unblock mode


def failed_extract(code: ErrorCode, message: str, *, retryable: bool) -> Envelope[ExtractResult]:
    """A failed extraction envelope without data, with the trace id bound to the context (the
    request's, or in a worker the trace id of the job's creator)."""
    error = ErrorInfo(code=code, message=message, retryable=retryable)
    return Envelope[ExtractResult](status="failed", error=error, trace_id=current_trace_id())
