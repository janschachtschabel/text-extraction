"""The load profile (ruling P11-S4): what each scenario sends, how often and how many at once.

Every URL a scenario sends is a page of the local site farm (``tests/e2e/site_farm.py``) under
the farm base URL that ``body`` gets: the load test sends no load to third-party sites. An
upload sends a file of the repo's test corpus instead (``file``, relative to the repo root;
the other keys of its body are the form fields). Every request turns the result cache off, so
each one is real work.

The scenarios run one after another, each alone (``run_load.py``), so ``docker stats``
attributes the load to one scenario. The models are the stack's: ``ASR_MODEL`` (``small`` or
``large-v3-turbo``) and ``PII_MODEL_SIZE`` (``md`` or ``lg``) are set per run, not per request.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]

ScenarioKind = Literal["sync", "batch", "upload", "site", "mcp"]

# The counts and concurrencies of P11-S4.
SYNC_FAST_C10_COUNT = 300
SYNC_FAST_C30_COUNT = 900
BATCH_URLS = 1000
BROWSER_COUNT = 100
DOCUMENT_UPLOADS = 50
OCR_UPLOADS = 10
PII_COUNT = 200
AUDIO_UPLOADS = 10
SELECTOR_COUNT = 200
LLM_COUNT = 50
SEMANTIC_CHUNK_COUNT = 50
MCP_COUNT = 20

# The test corpus files that the uploads send.
# MarkItDown: a 120-page PDF (100 pages converted) and a DOCX, taken in turns.
DOCUMENT_FILES = ("tests/documents/corpus/long.pdf", "tests/documents/corpus/lesson.docx")
SCAN_FILE = "tests/documents/corpus/scan.pdf"  # an image-only PDF: Docling's OCR
AUDIO_FILE = "tests/media/corpus/speech_de.ogg"  # 30 s of German speech

# Page numbers per scenario, so that no two scenarios ask for the same page.
_PAGE_OFFSETS = {
    "sync-fast-c10": 0,
    "sync-fast-c30": 1_000,
    "batch-1000": 2_000,
    "browser-100": 3_000,
    "pii-200": 4_000,
    "structured-selectors-200": 5_000,
    "structured-llm-50": 6_000,
    "chunk-semantic-50": 7_000,
    "site-crawl": 8_000,
    "mcp-20": 9_000,
}

Body = Callable[[int, str], dict[str, object]]


@dataclass(frozen=True)
class Scenario:
    """One part of the profile: ``count`` requests of ``kind`` to ``endpoint``, at most
    ``concurrency`` at once, each with the body ``body(index, farm_base_url)``. Its window
    ends ``settle_seconds`` after the last answer."""

    name: str
    kind: ScenarioKind
    endpoint: str
    count: int
    concurrency: int
    body: Body
    settle_seconds: int = 10


def page(farm: str, number: int) -> str:
    """The URL of a farm page: a 600-word article."""
    return f"{farm.rstrip('/')}/p/{number}.html"


def _extract(name: str, **options: object) -> Body:
    """A body of ``POST /v1/extract`` (and its siblings) for page ``offset + index``."""
    offset = _PAGE_OFFSETS[name]

    def body(index: int, farm: str) -> dict[str, object]:
        return {"url": page(farm, offset + index), "cache": False, **options}

    return body


def _batch(index: int, farm: str) -> dict[str, object]:
    offset = _PAGE_OFFSETS["batch-1000"]
    urls = [page(farm, offset + n) for n in range(BATCH_URLS)]
    return {"urls": urls, "mode": "fast", "cache": False}


def _upload(files: tuple[str, ...], **fields: str) -> Body:
    """A body of ``POST /v1/extract/file``: the files in turns, and the form fields."""

    def body(index: int, farm: str) -> dict[str, object]:
        return {"file": files[index % len(files)], **fields}

    return body


def _site(index: int, farm: str) -> dict[str, object]:
    # The request's default caps (depth 2, 100 pages); sitemaps as robots.txt lists them.
    return {"url": page(farm, _PAGE_OFFSETS["site-crawl"] + index), "cache": False}


def _mcp(index: int, farm: str) -> dict[str, object]:
    url = page(farm, _PAGE_OFFSETS["mcp-20"] + index)
    return {
        "jsonrpc": "2.0",
        "id": index,
        "method": "tools/call",
        "params": {
            "name": "extract_url",
            "arguments": {"url": url, "mode": "fast", "options": {"cache": False}},
        },
    }


# The fields of a page that the structured scenarios ask for.
_PAGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "The article's heading."},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
    },
}
_PAGE_SELECTORS = {"title": {"css": "article h1"}, "paragraphs": {"css": "article p"}}

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "sync-fast-c10",
        "sync",
        "/v1/extract",
        SYNC_FAST_C10_COUNT,
        10,
        _extract("sync-fast-c10", mode="fast"),
    ),
    Scenario(
        "sync-fast-c30",
        "sync",
        "/v1/extract",
        SYNC_FAST_C30_COUNT,
        30,
        _extract("sync-fast-c30", mode="fast"),
    ),
    Scenario("batch-1000", "batch", "/v1/batch", 1, 1, _batch),
    Scenario(
        "browser-100",
        "sync",
        "/v1/extract",
        BROWSER_COUNT,
        10,
        _extract("browser-100", mode="browser"),
    ),
    Scenario(
        "upload-docs-50",
        "upload",
        "/v1/extract/file",
        DOCUMENT_UPLOADS,
        5,
        _upload(DOCUMENT_FILES, format="markdown"),
    ),
    Scenario(
        "upload-ocr-10",
        "upload",
        "/v1/extract/file",
        OCR_UPLOADS,
        2,
        _upload((SCAN_FILE,), format="markdown", ocr="always"),
    ),
    Scenario(
        "pii-200",
        "sync",
        "/v1/extract",
        PII_COUNT,
        10,
        _extract("pii-200", mode="fast", pii="mask"),
    ),
    # Every audio upload is a job; at most ASR_MAX_OPEN_PER_CLIENT (3) may be open per key.
    Scenario(
        "asr-10",
        "upload",
        "/v1/extract/file",
        AUDIO_UPLOADS,
        2,
        _upload((AUDIO_FILE,), format="text"),
    ),
    Scenario(
        "structured-selectors-200",
        "sync",
        "/v1/structured",
        SELECTOR_COUNT,
        10,
        _extract(
            "structured-selectors-200",
            mode="fast",
            structured={
                "schema": _PAGE_SCHEMA,
                "strategy": "selectors",
                "selectors": _PAGE_SELECTORS,
            },
        ),
    ),
    Scenario(
        "structured-llm-50",
        "sync",
        "/v1/structured",
        LLM_COUNT,
        5,
        _extract(
            "structured-llm-50",
            mode="fast",
            structured={"schema": _PAGE_SCHEMA, "strategy": "llm"},
        ),
    ),
    Scenario(
        "chunk-semantic-50",
        "sync",
        "/v1/chunk",
        SEMANTIC_CHUNK_COUNT,
        5,
        _extract("chunk-semantic-50", mode="fast", chunking={"strategy": "semantic"}),
    ),
    Scenario("site-crawl", "site", "/v1/site-structure", 1, 1, _site),
    Scenario("mcp-20", "mcp", "/mcp", MCP_COUNT, 5, _mcp),
)
