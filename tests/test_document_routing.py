"""Which converter a document goes to: ``documents/routing.py`` (ruling F11)."""

from typing import Any, get_args

import pytest

from text_extraction.documents.routing import (
    DOCLING_KINDS,
    MARKITDOWN_KINDS,
    needs_ocr,
    ocr_available,
    wants_ocr,
)
from text_extraction.documents.types import DocumentConversion, DocumentKind, OcrOption
from text_extraction.settings import Settings

KINDS: tuple[DocumentKind, ...] = get_args(DocumentKind)
OPTIONS: tuple[OcrOption, ...] = get_args(OcrOption)


def make_settings(**overrides: Any) -> Settings:
    return Settings(**{"auth_mode": "none", **overrides})


def conversion(*, needs_ocr: bool) -> DocumentConversion:
    return DocumentConversion(
        content="",
        title=None,
        warnings=[],
        pages=1,
        pages_converted=1,
        needs_ocr=needs_ocr,
        metadata=None,
    )


def expected(kind: DocumentKind, scan: bool | None, ocr: OcrOption, available: bool) -> bool:
    """The table of ruling F11, written out."""
    if ocr == "never" or not available:
        return False
    if kind in ("odt", "ods", "odp", "png", "jpeg", "tiff", "webp", "bmp"):
        return True
    if kind != "pdf":
        return False
    return ocr == "always" or bool(scan)


def test_every_kind_has_exactly_one_converter() -> None:
    assert frozenset(KINDS) == MARKITDOWN_KINDS | DOCLING_KINDS
    assert not MARKITDOWN_KINDS & DOCLING_KINDS


@pytest.mark.parametrize(
    ("docling", "jobs", "available"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_ocr_needs_docling_and_jobs(docling: bool, jobs: bool, available: bool) -> None:
    assert ocr_available(make_settings(docling_enabled=docling, jobs_enabled=jobs)) is available


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("ocr", OPTIONS)
@pytest.mark.parametrize("scan", [None, False, True])
@pytest.mark.parametrize(("docling", "jobs"), [(True, True), (True, False), (False, True)])
def test_routing_table(
    kind: DocumentKind, ocr: OcrOption, scan: bool | None, docling: bool, jobs: bool
) -> None:
    settings = make_settings(docling_enabled=docling, jobs_enabled=jobs)
    converted = None if scan is None else conversion(needs_ocr=scan)

    assert needs_ocr(kind, converted, ocr, settings) is expected(kind, scan, ocr, docling and jobs)
    # What the request asks for, whatever the service has.
    assert wants_ocr(kind, converted, ocr) is expected(kind, scan, ocr, True)
