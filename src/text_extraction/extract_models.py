"""Request and result models of ``POST /v1/extract``."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from text_extraction.browser_options import (
    ACTIONS_DESCRIPTION,
    COOKIES_DESCRIPTION,
    HEADERS_DESCRIPTION,
    ActionList,
    ArtifactRef,
    BrowserSecrets,
    CookieList,
    Headers,
    WaitSeconds,
    check_cookie_domains,
)
from text_extraction.compliance import ComplianceMode, ComplianceReport
from text_extraction.convert import Link, OutputFormat
from text_extraction.documents.types import DocumentInfo, OcrOption
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.fingerprint import Fingerprint
from text_extraction.media.types import MAX_LANGUAGES, LanguageCode, MediaInfo
from text_extraction.metadata import PageMetadata
from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.pii.types import BareModeOrObject, PiiOptions, PiiReport
from text_extraction.quality import Quality
from text_extraction.tracing import current_trace_id

CacheState = Literal["miss", "hit", "revalidated", "bypass"]


class ExtractRequest(BaseModel, extra="forbid", hide_input_in_errors=True):
    """Body of ``POST /v1/extract``."""

    url: str = Field(max_length=MAX_URL_LENGTH, description="The http(s) URL to extract.")
    mode: Literal["fast", "auto", "browser"] = Field(
        default="auto",
        description="`fast`: HTTP only. `auto`: HTTP first, then a browser when the page needs "
        "one (a JavaScript shell, a challenge, a soft 404) and browser mode is enabled; the "
        "better of both results is kept. Without a browser worker in time `auto` keeps the HTTP "
        "result, with the warning `browser_unavailable`; while browser mode is off it never "
        "uses a browser. `auto` with `actions`, `cookies`, `headers` or `screenshot` always "
        "renders. `browser`: always renders the page in a browser; without a browser worker in "
        "time it fails with "
        "`timeout` (retryable). While browser mode is off, `browser` and the browser options "
        "answer 400.",
    )
    format: OutputFormat = Field(default="markdown", description="Format of `content`.")
    metadata: bool = Field(default=True, description="Include the page's DOM metadata.")
    links: bool = Field(default=False, description="Include the page's links.")
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's. "
        "`ignore` also allows unblock mode where the operator enabled it: a browser that gets "
        "past bot protection, for sites that allowed extraction by contract.",
    )
    cache: bool = Field(
        default=True, description="Use the result cache; `false` neither reads nor stores."
    )
    wait_seconds: WaitSeconds = Field(
        default=None,
        description="Without it, the request answers when the extraction ends (the unchanged "
        "default). With it, an extraction that is still running after this many seconds becomes "
        "a job: the answer is then 202 with the job's id and `status_url`, and "
        "`GET /v1/jobs/{id}` has the result. 0 hands over at once. Ignored when jobs are "
        "disabled (`JOBS_ENABLED=false`). A request that waits for the browser when the time "
        "is up answers 202 with the browser's job. With `transcribe: true` the default is 20.",
    )
    actions: ActionList = Field(
        default=[], repr=False, description=f"{ACTIONS_DESCRIPTION} Not with `mode: fast`."
    )
    cookies: CookieList = Field(
        default=[], repr=False, description=f"{COOKIES_DESCRIPTION} Not with `mode: fast`."
    )
    headers: Headers = Field(
        default={}, repr=False, description=f"{HEADERS_DESCRIPTION} Not with `mode: fast`."
    )
    screenshot: bool = Field(
        default=False,
        description="Also capture a PNG screenshot (1920 px wide): `data.screenshot` then "
        "references it for `GET /v1/jobs/{id}/artifacts/screenshot.png` (`ARTIFACT_TTL_SECONDS`). "
        "Not with `mode: fast`.",
    )
    full_page: bool = Field(
        default=True,
        description="Screenshot of the whole page; `false`: the first viewport only. Not with "
        "`mode: fast`.",
    )
    ocr: OcrOption = Field(
        default="auto",
        description="OCR of documents. `auto`: scans, broken text layers and images are read "
        "with OCR where the service has it. `never`: no OCR. `always`: every PDF is read with "
        "OCR. Pages that are not documents ignore it.",
    )
    transcribe: bool = Field(
        default=False,
        description="Transcribe audio and video that have no subtitles with speech recognition; "
        "always a job: the answer is 202 with the job when transcription is needed. 400 while "
        "the service has no transcription.",
    )
    languages: list[LanguageCode] = Field(
        default=[],
        max_length=MAX_LANGUAGES,
        description="Preferred subtitle and transcription languages, ISO 639 codes, at most 5; "
        "default: the video's original language.",
    )
    pii: Annotated[PiiOptions | None, BareModeOrObject()] = Field(
        default=None,
        description="Find personal data in the result and handle it: `mask`, `redact`, `hash` or "
        "`annotate` (a bare string is the mode; see the object's fields). Applies to the "
        "content, the title, the metadata, the links, the media uploader and the uploaded "
        "file's name (`document.file_name`); the report is "
        "`data.pii` (null when nothing was processed: a failure without content). Always a "
        "job when the service has to wait for it: a request without "
        "`wait_seconds` waits `PII_SYNC_WAIT_SECONDS`, then answers 202. 400 while the service "
        "has no PII handling. Not with `screenshot`.",
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
    pii_ms: int = 0  # the PII step; 0 without ``pii``


class ExtractResult(BaseModel):
    """What one extraction produced; on failure as much of it as was known."""

    url: str | None  # None for an upload
    final_url: str | None
    upstream_status: int | None  # of the response the content came from
    content_type: str | None
    mode_used: Literal["fast", "browser", "upload"]
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
    warnings: list[str] = Field(
        description="What did not go as asked, while the result still answers: "
        "`browser_unavailable` (no browser worker took the page: the fast result), "
        "`action_<index>_failed` (a browser action, counted from 0), "
        "`navigation_refused_non_get`, `consent_overlay_removed`, `screenshot_clipped`, "
        "`browser_bytes_capped`, `challenge_checkbox_clicked`, `challenge_not_cleared`, "
        "`browser_profile_left` (browser mode); `ocr_unavailable` (a document that needed OCR "
        "answers its text layer, or nothing), `pages_truncated` (pages after the page limit "
        "were cut), `pages_partial` (OCR stopped at its time limit or skipped a page: the pages "
        "it read), `document_fetched_directly` (a render ended on a file, which was fetched "
        "and converted in fast mode) (documents); `subtitles_blocked` (the platform refused the "
        "subtitles: the metadata answers), `subtitles_failed` (the subtitles could not be "
        "had or read this time: the metadata answers), `asr_unavailable` (no transcription in "
        "time, or it failed: the metadata answers), `tracks_truncated` (more tracks than the "
        "limit: the first ones), `tracks_failed` (no track of the page could be read) (media); "
        "`content_not_stored_too_large` (a job item stored without its content). Converter "
        "notes may follow."
    )
    # Browser mode only; the defaults keep results cached before browser mode valid.
    screenshot: ArtifactRef | None = None
    unblocked: bool = False  # the page was loaded in unblock mode
    # Documents only; the default keeps results cached before documents valid.
    document: DocumentInfo | None = None
    # Audio and video only (and pages with players); the default keeps older results valid.
    media: MediaInfo | None = None
    # With a ``pii`` request only; the default keeps results cached before PII valid.
    pii: PiiReport | None = None


def failed_extract(code: ErrorCode, message: str, *, retryable: bool) -> Envelope[ExtractResult]:
    """A failed extraction envelope without data, with the trace id bound to the context (the
    request's, or in a worker the trace id of the job's creator)."""
    error = ErrorInfo(code=code, message=message, retryable=retryable)
    return Envelope[ExtractResult](status="failed", error=error, trace_id=current_trace_id())
