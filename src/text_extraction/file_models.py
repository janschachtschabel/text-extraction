"""Form model of ``POST /v1/extract/file`` (ruling F17)."""

import json
from typing import Annotated, Any

from pydantic import BaseModel, Field, WithJsonSchema, field_validator, model_validator

from text_extraction.browser_options import WaitSeconds
from text_extraction.compliance import ComplianceMode
from text_extraction.convert import OutputFormat
from text_extraction.documents.types import OcrOption
from text_extraction.job_models import JobOptions
from text_extraction.pii.types import PiiOptions

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
    # The part is text, so the schema says string; the validator below makes the options of it.
    pii: Annotated[PiiOptions | None, WithJsonSchema({"type": "string"})] = Field(
        default=None,
        description="Find personal data in the result and handle it. Either a mode (`mask`, "
        "`redact`, `hash` or `annotate`) or a JSON object as a string, e.g. "
        '`{"mode": "annotate", "entities": ["PERSON"], "score_threshold": 0.6}`; see '
        "`POST /v1/extract` for its fields.",
    )

    @model_validator(mode="before")
    @classmethod
    def _without_the_file(cls, data: Any) -> Any:
        # FastAPI hands a form model every part of the form, the file part included.
        if isinstance(data, dict) and FILE_PART in data:
            return {name: value for name, value in data.items() if name != FILE_PART}
        return data

    @field_validator("pii", mode="before")
    @classmethod
    def _pii_from_its_form_string(cls, value: Any) -> Any:
        """The part is text: a bare mode (``PiiOptions`` reads it as the mode) or a JSON
        object."""
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                return json.loads(value)
            except ValueError:
                raise ValueError("pii is not valid JSON") from None
        return value


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
        pii=form.pii,
    )


def form_of(options: JobOptions) -> FileForm:
    """The form of an upload, rebuilt from its job's options (``wait_seconds`` does not
    matter any more)."""
    return FileForm(
        format=options.format, metadata=options.metadata, ocr=options.ocr, pii=options.pii
    )
