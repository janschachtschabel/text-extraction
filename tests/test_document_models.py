from typing import Any, get_args

import pytest
from pydantic import ValidationError

from text_extraction.browser_dispatch import job_options
from text_extraction.compliance import ComplianceReport
from text_extraction.documents.types import DocumentInfo, DocumentKind, OcrOption
from text_extraction.extract_models import ExtractRequest, ExtractResult, Timings
from text_extraction.job_models import BatchRequest, JobItem, JobKind, JobOptions

# An ``ExtractResult`` as package 4 stored it: no ``document`` key.
PACKAGE_4_RESULT: dict[str, Any] = {
    "url": "https://www.example.org/page",
    "final_url": "https://www.example.org/page",
    "upstream_status": 200,
    "content_type": "text/html",
    "mode_used": "fast",
    "attempts": 1,
    "language": "en",
    "word_count": 3,
    "title": "A title",
    "format": "markdown",
    "content": "one two three",
    "quality": None,
    "timings": {"total_ms": 10, "compliance_ms": 1, "fetch_ms": 2, "convert_ms": 3},
    "compliance": {"mode": "enforce", "decision": "allowed"},
    "metadata": None,
    "links": None,
    "fingerprint": None,
    "cache": "miss",
    "warnings": [],
    "screenshot": None,
    "unblocked": False,
}


def upload_result(**fields: Any) -> ExtractResult:
    values: dict[str, Any] = {
        "url": None,
        "final_url": None,
        "upstream_status": None,
        "content_type": "application/pdf",
        "mode_used": "upload",
        "attempts": 0,
        "language": None,
        "word_count": 0,
        "title": None,
        "format": "markdown",
        "content": None,
        "quality": None,
        "timings": Timings(total_ms=1, compliance_ms=0, fetch_ms=0, convert_ms=1),
        "compliance": ComplianceReport(mode="enforce", decision="skipped"),
        "metadata": None,
        "links": None,
        "fingerprint": None,
        "cache": "bypass",
        "warnings": [],
    }
    values.update(fields)
    return ExtractResult(**values)


def test_cached_result_without_document_still_parses() -> None:
    result = ExtractResult.model_validate(PACKAGE_4_RESULT)

    assert result.document is None
    assert result.url == "https://www.example.org/page"


def test_ocr_option_defaults_to_auto() -> None:
    assert ExtractRequest(url="https://example.org/").ocr == "auto"
    assert BatchRequest(urls=["https://example.org/"]).ocr == "auto"
    assert JobOptions(compliance="enforce").ocr == "auto"


@pytest.mark.parametrize("model", [ExtractRequest, BatchRequest])
def test_ocr_option_rejects_unknown_values(model: type) -> None:
    body: dict[str, Any] = {"url": "https://example.org/", "urls": ["https://example.org/"]}
    if model is ExtractRequest:
        del body["urls"]
    else:
        del body["url"]

    with pytest.raises(ValidationError):
        model(**body, ocr="sometimes")


def test_ocr_option_values() -> None:
    assert get_args(OcrOption) == ("auto", "never", "always")


def test_ocr_travels_from_request_to_job_options_and_back() -> None:
    req = ExtractRequest(url="https://example.org/doc.pdf", ocr="always")

    options = job_options(req, "enforce")
    rebuilt = ExtractRequest(url=req.url, **options.model_dump())

    assert options.ocr == "always"
    assert rebuilt.ocr == "always"


def test_upload_result_has_no_url() -> None:
    result = upload_result()

    dumped = result.model_dump(mode="json")
    assert dumped["url"] is None
    assert dumped["final_url"] is None
    assert dumped["mode_used"] == "upload"
    assert dumped["document"] is None
    assert ExtractResult.model_validate(dumped) == result


def test_document_info_round_trips_in_a_result() -> None:
    info = DocumentInfo(
        kind="pdf",
        converter="docling",
        pages=12,
        pages_converted=10,
        ocr=True,
        file_name="report.pdf",
    )

    result = upload_result(document=info)

    dumped = result.model_dump(mode="json")
    assert dumped["document"] == {
        "kind": "pdf",
        "converter": "docling",
        "pages": 12,
        "pages_converted": 10,
        "ocr": True,
        "file_name": "report.pdf",
    }
    assert ExtractResult.model_validate(dumped).document == info


def test_document_info_file_name_defaults_to_none() -> None:
    info = DocumentInfo(
        kind="docx", converter="markitdown", pages=None, pages_converted=None, ocr=False
    )

    assert info.file_name is None


def test_document_kinds() -> None:
    assert get_args(DocumentKind) == (
        "pdf",
        "docx",
        "pptx",
        "xlsx",
        "xls",
        "epub",
        "csv",
        "odt",
        "ods",
        "odp",
        "png",
        "jpeg",
        "tiff",
        "webp",
        "bmp",
    )


@pytest.mark.parametrize(("kind", "converter"), [("doc", "markitdown"), ("pdf", "tika")])
def test_document_info_rejects_unknown_kind_and_converter(kind: str, converter: str) -> None:
    with pytest.raises(ValidationError):
        DocumentInfo.model_validate(
            {
                "kind": kind,
                "converter": converter,
                "pages": None,
                "pages_converted": None,
                "ocr": False,
            }
        )


def test_file_jobs_and_url_less_items() -> None:
    assert "file" in get_args(JobKind)

    item = JobItem(index=0, url=None, state="pending", result=None)

    assert item.model_dump(mode="json")["url"] is None
