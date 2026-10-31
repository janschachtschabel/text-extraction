"""The load profile (ruling P11-S4): what each scenario sends, how often and how many at once.

Every URL a scenario sends is a page of the local site farm (``tests/e2e/site_farm.py``) under
the farm base URL that ``body`` gets, or on the same farm host at one of its sitemap sites'
ports (``farm_site``): the load test sends no load to third-party sites. An
upload sends a file instead: a file of the repo's test corpus (``file``, relative to the repo
root) or one the generator builds (``GENERATED_DOCX``); ``upload_bytes`` reads either. The
other keys of an upload's body are the form fields. Every request turns the result cache off,
so each one is real work.

The site structure items of ruling P11-S7 are crawls (one request each): ``site-crawl`` at the
request's default caps, ``site-crawl-max-pages`` at ``SITE_MAX_PAGES``, ``sitemap-200k`` (one
sitemap of ``SITE_MAX_SITEMAP_URLS`` entries), ``sitemap-50m-1`` (one 50 MiB sitemap alone) and
``sitemaps-50m-3`` (three 50 MiB sitemaps of one crawl in flight at once). The sitemap crawls
start at depth 0 on the farm's sitemap sites, whose robots.txt lists those files
(``site_farm.SITEMAP_SITES``): robots.txt belongs to an origin, so the port picks the sitemaps.
The wall time of the three against the one alone is what the ``SITE_PARSE_CONCURRENCY`` ruling
compares: one parse thread per pod parses them one after another.

The scenarios run one after another, each alone (``run_load.py``), so ``docker stats``
attributes the load to one scenario. The models are the stack's: ``ASR_MODEL`` (``small`` or
``large-v3-turbo``) and ``PII_MODEL_SIZE`` (``md`` or ``lg``) are set per run, not per request.
"""

import functools
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from tests.e2e.site_farm import SITEMAP_SITES, article

REPO_ROOT = Path(__file__).resolve().parents[2]

ScenarioKind = Literal["sync", "batch", "upload", "site", "mcp"]
# The outcome of one request: its envelope's status (a job's: by its items). An ``error`` (a
# refusal, a client failure, a timeout) is never expected.
ExpectedOutcome = Literal["success", "partial", "failed"]

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
# The site structure items (P11-S7): the defaults of Settings and of the request.
SITE_MAX_PAGES = 1000  # SITE_MAX_PAGES
SITE_MAX_PAGES_DEPTH = 3  # ten links a farm page: 1,110 pages within depth 3
# The farm's sitemap sites by what their robots.txt lists (port offsets of SITEMAP_SITES).
SITEMAP_200K_SITE = next(k for k, names in SITEMAP_SITES.items() if names == ("200k",))
SITEMAP_50M_SITE = next(k for k, names in SITEMAP_SITES.items() if names == ("50m-1",))
SITEMAPS_50M_SITE = next(k for k, names in SITEMAP_SITES.items() if len(names) == 3)

# The test corpus files that the uploads send.
# A DOCX that the load generator builds when it first needs it (``upload_bytes``): the corpus
# DOCX files are too short for the quality gate (``content_too_short``), and a load scenario
# should succeed by design.
GENERATED_DOCX = "generated/report.docx"
REPORT_ARTICLES = 5  # farm articles in the generated DOCX, 600 words each
# MarkItDown: a 120-page PDF (100 pages converted) and a DOCX, taken in turns.
DOCUMENT_FILES = ("tests/documents/corpus/long.pdf", GENERATED_DOCX)
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
    "site-crawl-max-pages": 8_100,
    "sitemap-200k": 8_200,
    "sitemap-50m-1": 8_300,
    "sitemaps-50m-3": 8_400,
    "mcp-20": 9_000,
}

Body = Callable[[int, str], dict[str, object]]


@dataclass(frozen=True)
class Scenario:
    """One part of the profile: ``count`` requests of ``kind`` to ``endpoint``, at most
    ``concurrency`` at once, each with the body ``body(index, farm_base_url)``. Its window
    ends ``settle_seconds`` after the last answer. A request whose outcome is not in
    ``expected`` counts as unexpected and fails the run."""

    name: str
    kind: ScenarioKind
    endpoint: str
    count: int
    concurrency: int
    body: Body
    settle_seconds: int = 10
    expected: frozenset[ExpectedOutcome] = frozenset({"success"})


def _report_docx() -> bytes:
    """A DOCX of ``REPORT_ARTICLES`` farm articles: a heading and the paragraphs of each."""
    import docx  # python-docx, a dev dependency: only the load generator builds the file

    report = docx.Document()
    for number in range(1, REPORT_ARTICLES + 1):
        html = article(number).decode()
        heading = re.search(r"<h1>(.*?)</h1>", html)
        report.add_heading(heading.group(1) if heading else f"Report {number}", level=1)
        for paragraph in re.findall(r"<p>(.*?)</p>", html):
            report.add_paragraph(paragraph)
    buffer = io.BytesIO()
    report.save(buffer)
    return buffer.getvalue()


_GENERATED: dict[str, Callable[[], bytes]] = {GENERATED_DOCX: _report_docx}


@functools.cache
def upload_bytes(file: str) -> bytes:
    """The bytes of an upload's ``file``: a generated file, else a file of the repo."""
    make = _GENERATED.get(file)
    return make() if make is not None else (REPO_ROOT / file).read_bytes()


def page(farm: str, number: int) -> str:
    """The URL of a farm page: a 600-word article."""
    return f"{farm.rstrip('/')}/p/{number}.html"


def farm_site(farm: str, offset: int) -> str:
    """The base URL of the farm's site at ``offset`` (``SITEMAP_SITES``): the farm's host on
    its port + ``offset``."""
    parts = urlsplit(farm)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}:{port + offset}"


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


def _site(name: str, *, site: int = 0, **options: object) -> Body:
    """A body of ``POST /v1/site-structure`` from page ``offset + index`` of the farm's site
    at ``site``; without options the request's default caps (depth 2, 100 pages), and
    sitemaps as robots.txt lists them."""
    offset = _PAGE_OFFSETS[name]

    def body(index: int, farm: str) -> dict[str, object]:
        return {"url": page(farm_site(farm, site), offset + index), "cache": False, **options}

    return body


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
    # The farm's pages link to ten farm pages each (robots.txt and /sitemap.xml answer 404, the
    # guess's success): every item succeeds.
    Scenario("site-crawl", "site", "/v1/site-structure", 1, 1, _site("site-crawl")),
    Scenario(
        "site-crawl-max-pages",
        "site",
        "/v1/site-structure",
        1,
        1,
        _site("site-crawl-max-pages", depth=SITE_MAX_PAGES_DEPTH, max_pages=SITE_MAX_PAGES),
    ),
    Scenario(
        "sitemap-200k",
        "site",
        "/v1/site-structure",
        1,
        1,
        _site("sitemap-200k", site=SITEMAP_200K_SITE, depth=0),
    ),
    Scenario(
        "sitemap-50m-1",
        "site",
        "/v1/site-structure",
        1,
        1,
        _site("sitemap-50m-1", site=SITEMAP_50M_SITE, depth=0),
    ),
    Scenario(
        "sitemaps-50m-3",
        "site",
        "/v1/site-structure",
        1,
        1,
        _site("sitemaps-50m-3", site=SITEMAPS_50M_SITE, depth=0),
    ),
    Scenario("mcp-20", "mcp", "/mcp", MCP_COUNT, 5, _mcp),
)
