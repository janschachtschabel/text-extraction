"""Models of asynchronous jobs: stored options, progress and the views the job API returns."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from text_extraction.compliance import ComplianceMode
from text_extraction.convert import OutputFormat
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.netguard import MAX_URL_LENGTH

JobKind = Literal["extract", "batch", "screenshot"]
JobState = Literal["queued", "running", "completed"]
ItemState = Literal["pending", "done"]

# The most URLs a batch may ever have: the maximum of ``BATCH_MAX_URLS``. It bounds the model;
# the body itself is bounded in bytes before it is parsed, with or without a Content-Length
# (``body_limit.py``, ``jobs_api.batch_body_limit``).
MAX_BATCH_URLS = 100_000


class BatchRequest(BaseModel, extra="forbid"):
    """Body of ``POST /v1/batch``: the URLs and the options of ``ExtractRequest`` (same defaults).

    The most URLs a batch may have is ``BATCH_MAX_URLS``; the route checks it, since a model
    cannot see the settings."""

    urls: list[Annotated[str, Field(min_length=1, max_length=MAX_URL_LENGTH)]] = Field(
        min_length=1,
        max_length=MAX_BATCH_URLS,
        description="The http(s) URLs to extract, at most `BATCH_MAX_URLS`. A URL twice gets two "
        "items; an invalid URL gets an item that failed with `invalid_url`.",
    )
    mode: Literal["fast", "auto", "browser"] = Field(
        default="auto", description="As for `/v1/extract`."
    )
    format: OutputFormat = Field(default="markdown", description="Format of `content`.")
    metadata: bool = Field(default=True, description="Include the pages' DOM metadata.")
    links: bool = Field(default=False, description="Include the pages' links.")
    compliance: ComplianceMode | None = Field(
        default=None,
        description="`enforce`, `report` or `ignore` (admins only); default: the operator's.",
    )
    cache: bool = Field(
        default=True, description="Use the result cache; `false` neither reads nor stores."
    )


class JobOptions(BaseModel, extra="forbid"):
    """The extraction options of a job. Same as ``ExtractRequest``, except that the compliance
    mode is already resolved for the caller: it is never None."""

    mode: Literal["fast", "auto", "browser"] = "auto"
    format: OutputFormat = "markdown"
    metadata: bool = True
    links: bool = False
    compliance: ComplianceMode
    cache: bool = True
    screenshot: bool = False
    full_page: bool = True


class JobProgress(BaseModel):
    """How far a job is: finished items in total and by the status of their envelope."""

    total: int
    done: int
    success: int
    partial: int
    failed: int


class JobItem(BaseModel):
    """One URL of a job; ``result`` is set once its state is ``done``."""

    index: int
    url: str
    state: ItemState
    result: Envelope[ExtractResult] | None


class JobView(BaseModel):
    """Status of a job with one page of its items."""

    id: str
    kind: JobKind
    state: JobState
    created_at: datetime
    finished_at: datetime | None
    progress: JobProgress
    items: list[JobItem]
    next_offset: int | None


class JobAccepted(BaseModel):
    """Answer to the creation of a job."""

    id: str
    kind: JobKind
    state: JobState
    total: int
    status_url: str
