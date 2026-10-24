"""Request and result models of ``POST /v1/extract``."""

from typing import Literal

from pydantic import BaseModel, Field

from text_extraction.compliance import ComplianceMode, ComplianceReport
from text_extraction.convert import Link, OutputFormat
from text_extraction.fingerprint import Fingerprint
from text_extraction.metadata import PageMetadata
from text_extraction.netguard import MAX_URL_LENGTH
from text_extraction.quality import Quality

CacheState = Literal["miss", "hit", "revalidated", "bypass"]


class ExtractRequest(BaseModel, extra="forbid"):
    """Body of ``POST /v1/extract``."""

    url: str = Field(max_length=MAX_URL_LENGTH, description="The http(s) URL to extract.")
    mode: Literal["fast", "auto"] = Field(
        default="auto",
        description="`fast`: HTTP only. `auto`: will also use a browser when the page needs one "
        "(a later release). Both report `quality.browser_recommended`.",
    )
    format: OutputFormat = Field(default="markdown", description="Format of `content`.")
    metadata: bool = Field(default=True, description="Include the page's DOM metadata.")
    links: bool = Field(default=False, description="Include the page's links.")
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's.",
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


class Timings(BaseModel):
    """Milliseconds spent in this request. ``fetch_ms`` includes the wait for the host's turn;
    ``convert_ms`` covers decoding, conversion and the steps after it."""

    total_ms: int
    compliance_ms: int
    fetch_ms: int
    convert_ms: int


class ExtractResult(BaseModel):
    """What one extraction produced; on failure as much of it as was known."""

    url: str
    final_url: str | None
    upstream_status: int | None  # of the response the content came from
    content_type: str | None
    mode_used: Literal["fast"]
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
