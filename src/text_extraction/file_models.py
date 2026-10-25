"""Form model of ``POST /v1/extract/file`` (ruling F17)."""

from typing import Any

from pydantic import BaseModel, Field, model_validator

from text_extraction.browser_options import WaitSeconds
from text_extraction.compliance import ComplianceMode
from text_extraction.convert import OutputFormat
from text_extraction.documents.types import OcrOption
from text_extraction.job_models import JobOptions

# The form part that carries the file; the route takes it as a parameter of its own.
FILE_PART = "file"


class FileForm(BaseModel, extra="forbid", hide_input_in_errors=True):
    """The form fields of an upload, next to its file part. Unknown fields are refused, as in
    the JSON bodies."""

    format: OutputFormat = Field(default="markdown", description="Format of `content`.")
    metadata: bool = Field(
        default=True,
        description="Include the document's metadata (title, authors, licence, dates).",
    )
    ocr: OcrOption = Field(
        default="auto",
        description="OCR of documents. `auto`: scans, broken text layers and images are read "
        "with OCR where the service has it. `never`: no OCR. `always`: every PDF is read with "
        "OCR (400 while the service has no OCR). HTML and plain-text files ignore it.",
    )
    wait_seconds: WaitSeconds = Field(
        default=25,
        description="An upload that is still converting after this many seconds becomes a "
        "job: the answer is then 202 with the job's id and `status_url`, and "
        "`GET /v1/jobs/{id}` has the result. Ignored when jobs are disabled "
        "(`JOBS_ENABLED=false`).",
    )

    @model_validator(mode="before")
    @classmethod
    def _without_the_file(cls, data: Any) -> Any:
        # FastAPI hands a form model every part of the form, the file part included.
        if isinstance(data, dict) and FILE_PART in data:
            return {name: value for name, value in data.items() if name != FILE_PART}
        return data


class UploadInputMeta(BaseModel):
    """What a job keeps next to an upload's bytes (``JobStore.put_input``) until its item is
    extracted: the part's sanitised file name and its declared content type."""

    file_name: str | None = None
    content_type: str | None = None  # as the part declared it, parameters included


def upload_options(form: FileForm, compliance: ComplianceMode) -> JobOptions:
    """The stored options of an upload's job (kind ``file``): no mode of its own, no result
    cache."""
    return JobOptions(
        mode="fast",
        format=form.format,
        metadata=form.metadata,
        links=False,
        compliance=compliance,
        cache=False,
        ocr=form.ocr,
    )


def form_of(options: JobOptions) -> FileForm:
    """The form of an upload, rebuilt from its job's options (``wait_seconds`` does not
    matter any more)."""
    return FileForm(format=options.format, metadata=options.metadata, ocr=options.ocr)
