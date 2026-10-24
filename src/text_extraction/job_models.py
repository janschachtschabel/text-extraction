"""Models of asynchronous jobs: stored options, progress and the views the job API returns."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from text_extraction.compliance import ComplianceMode
from text_extraction.convert import OutputFormat
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult

JobKind = Literal["extract", "batch"]
JobState = Literal["queued", "running", "completed"]
ItemState = Literal["pending", "done"]


class JobOptions(BaseModel, extra="forbid"):
    """The extraction options of a job. Same as ``ExtractRequest``, except that the compliance
    mode is already resolved for the caller: it is never None."""

    mode: Literal["fast", "auto"] = "auto"
    format: OutputFormat = "markdown"
    metadata: bool = True
    links: bool = False
    compliance: ComplianceMode
    cache: bool = True


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
