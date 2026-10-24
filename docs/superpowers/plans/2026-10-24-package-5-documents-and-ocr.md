# Package 5: Documents and OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Files become text:
- `POST /v1/extract/file` takes an upload (multipart) and answers with the usual result envelope.
- `POST /v1/extract`, `POST /v1/batch` and browser renders that end on a file convert PDFs, Office files and EPUBs instead of answering `unsupported_content_type`.
- PDFs with a text layer, DOCX, PPTX, XLSX, XLS, EPUB and CSV go through MarkItDown in the pod that fetched them.
- Scans, PDFs with a broken text layer, images and OpenDocument files go to a new Docling worker (OCR, layout, tables), whose models come from a PVC filled by an init container.

**Architecture:**
- **Detect, then route.** A new `documents` package sniffs the kind of a body (magic bytes first, then the declared type, then the file extension), probes a PDF's text layer with pypdfium2, and decides the converter (F1, F4, F11).
- **MarkItDown inline (D1, recommended).** API pods, extract workers and the browser worker convert MarkItDown kinds themselves, in a small pool of child processes (`DocumentPool`) that the parent kills at the deadline. The parent never imports MarkItDown, pdfminer or pandas.
- **Docling as a pool.** OCR work goes to pool `docling` of the package-3 fair queue, like browser work goes to `browser`. The fetched or uploaded bytes travel with the item in Valkey; the MarkItDown result (if any) is the item's draft, and the better of both is kept (B5's pattern).
- **One pipeline.** URL documents run through the existing `Pipeline` (fetch, compliance, gate, cache, fingerprints). The new document steps live in `pipeline_documents.py`, the way the render steps live in `pipeline_render.py`. The Docling worker runs the same `Pipeline` with a Docling converter, as the browser worker runs it with a renderer.

**Tech Stack:** existing, plus:
- API image (runtime dependencies): `markitdown[pdf,docx,pptx,xlsx,xls]` (MIT; pulls pdfminer.six, pdfplumber, mammoth, python-pptx, openpyxl, pandas, xlrd, magika/onnxruntime, all MIT/BSD/Apache), `pypdfium2` (Apache-2.0/BSD-3), `markdown-it-py` (MIT), `python-multipart` (Apache-2.0).
- Docling image only (extra `docling`): `docling-slim` (MIT) with the extras the conversion needs, `torch`/`torchvision` CPU wheels from PyTorch's CPU index (BSD), Tesseract (`tesseract-ocr` from Debian, Apache-2.0). Models: Docling's layout and TableFormer models and `tessdata_best` `deu` and `eng` (Apache-2.0), on the PVC, never in the image.
- Dev only: `reportlab` (BSD), `python-docx` (MIT), `pillow` (MIT-CMU), to regenerate the test corpus.

**Spec:**
- `docs/architecture.md`: Overview (docling-worker, model volume), Extraction pipeline step 5 (Office/EPUB and PDFs with a good text layer → MarkItDown; scans, table-heavy PDFs, images → Docling), Public API (`POST /v1/extract/file`: upload, sync, job for large files), Error codes (`unsupported_content_type`, `too_large`, `conversion_failed`), Feature note "Browser actions" (a download is `unsupported_content_type` until package 5 routes files), "Jobs and the fair queue" (docling pool reuses it).
- `docs/packages.md` row 5: done when the PDF, Office and image corpus converts and scans yield text.
- `CLAUDE.md` rules 2 (stateless, models from a PVC, Helm toggles, jobs for work over ~30 s), 4, 5 (byte and time limits), 7 (no GPL/AGPL; PyMuPDF, Marker, MinerU excluded).
- Jan's decisions D1-D3 below (open until he answers; the plan is written against the recommended options).

## Global Constraints

- English only. Python 3.13. No GPL or AGPL dependency anywhere (CLAUDE.md rule 7); Task 10 adds a CI licence gate.
- New dependencies are pinned to their newest release that is at least 14 days old at implementation time (supply-chain caution, as R16 did for Patchright): today `markitdown==0.1.8`, `docling-slim==2.137.0`. Torch comes from `https://download.pytorch.org/whl/cpu` (uv index, `explicit = true`), never the CUDA wheels.
- The API image must not contain Docling, torch or Tesseract. No module imported by `text_extraction.app`, `text_extraction.worker` or `text_extraction.browser_worker` may import `docling` or `torch`. The parent processes of API pods and workers must not import `markitdown`, `pdfminer`, `pdfplumber` or `pandas` (only `DocumentPool` children do).
- Stateless pods: uploads, Docling inputs, drafts and results live in Valkey with TTLs. Spooled multipart parts in `/tmp` (a tmpfs or emptyDir) are transient and deleted when the request ends. Models live on the PVC, mounted read-only in the Docling worker.
- Every new setting is an env var with a documented default in `docs/configuration.md` and `.env.example`, in the task that adds it; Helm sets it where the chart owns it.
- Result envelope everywhere: a document that cannot be converted is 200 with `status: failed`. 5xx only for our own faults.
- The answers for HTML and `text/plain` pages (fast, auto, browser) must not change, except that a `text/plain` or untyped body that starts with a document signature is now treated as that document (F1).
- Never log URLs, query strings or file names. Kind, size, pages, converter, host and `path_hash` at most.
- Not in this package: vision-LLM descriptions of images (package 8, with the LLM client), PII (package 7), S3 offload of inputs (package 11), legacy binary `.doc`/`.ppt`, RTF, e-mail files and archives (`unsupported_content_type`).

## Decisions for Jan (open)

- **D1, where MarkItDown runs.** The overview names an `nlp-worker (MarkItDown, Presidio)`.
  - (a, recommended) In the pod that has the bytes: API pods, extract workers, browser worker, each in `DOCUMENT_CONCURRENCY` child processes. No extra hop, no mandatory worker for PDFs, the P1 reasoning of package 3. The nlp-worker stays for Presidio (package 7). The API image grows by about 120 MB (pandas, pdfminer, onnxruntime via magika).
  - (b) A separate document worker for MarkItDown too: slim API image, but every PDF becomes a job hop and needs that worker.
- **D2, a sync request that needs OCR.** OCR of a scan takes about 1-3 s per page on CPU, so many scans need longer than the ~30 s a sync answer may take.
  - (a, recommended) `POST /v1/extract` without `wait_seconds` waits up to `DOCLING_SYNC_WAIT_SECONDS` (20) for the Docling worker, then answers the text-layer result with the warning `ocr_unavailable` (an image, which has none, fails `timeout`, retryable). With `wait_seconds` it becomes a 202 job as today. `POST /v1/extract/file` is new, so it defaults to `wait_seconds: 25`: an upload that needs longer answers 202 with the job.
  - (b) Every request that needs OCR answers 202 with a job at once, even without `wait_seconds` (changes the sync contract of `/v1/extract`).
  - (c) Sync requests wait for OCR as long as it takes, up to `DOCLING_TIMEOUT_SECONDS` (breaks the 30 s rule).
- **D3, the Docling worker by default.** It needs an image of about 2 GB and a PVC of about 2 GiB for the models.
  - (a, recommended) Off by default: Helm `workers.docling.enabled: false`, compose profile `docling`. Without it scans keep their text layer (`ocr_unavailable`) and images answer `unsupported_content_type`.
  - (b) On by default, like the browser worker.

## Rulings in this plan

- **F1, formats and detection.** `documents/sniff.py` decides the `DocumentKind`. Magic bytes win; the declared type decides only where the bytes do not; the extension (URL path or upload name) only breaks ties (CSV). HTML, XHTML and `text/plain` keep their package-2 path unless the body starts with a document signature.

  | Kind | Detected by | Converter |
  | --- | --- | --- |
  | `pdf` | `%PDF-` in the first 1024 bytes | MarkItDown, or Docling per F4 |
  | `docx`, `pptx`, `xlsx` | ZIP with `[Content_Types].xml` naming the main part | MarkItDown |
  | `xls` | OLE2 signature with a `Workbook` or `Book` stream | MarkItDown |
  | `epub` | ZIP whose first entry `mimetype` is `application/epub+zip` | MarkItDown |
  | `csv` | `text/csv` or `.csv`, decodable text | MarkItDown |
  | `odt`, `ods`, `odp` | ZIP `mimetype` `application/vnd.oasis.opendocument.*` | Docling |
  | `png`, `jpeg`, `tiff`, `webp`, `bmp` | image signatures | Docling (OCR) |
  | anything else (`doc`, `ppt`, `rtf`, `msg`, `zip`, `gif`, …) | — | `unsupported_content_type` |

- **F2, where conversions run (D1 a).** MarkItDown kinds convert in the pod that holds the bytes, in a `DocumentPool` child. Docling kinds and OCR convert only in the Docling worker.
- **F3, process isolation.** `DocumentPool` keeps `DOCUMENT_CONCURRENCY` (2) child processes per pod (start method `spawn`, started before `/ready`). A child runs one conversion at a time. The caller passes its deadline; when it passes, the parent sends SIGKILL, starts a fresh child and the caller gets `timeout`. A child sets `RLIMIT_AS` to `DOCUMENT_MEMORY_LIMIT_MB` (2048) before its first import; a `MemoryError` there is `too_large`, a dead child `conversion_failed`. A child is replaced after 50 conversions. Calls wait for a free child within their deadline, like `ConversionPool`. MarkItDown's converter classes are called directly with our kind (`PdfConverter`, `DocxConverter`, …), so magika's guess is not on the path.
- **F4, PDF probe and OCR routing.** In the child, before MarkItDown: pypdfium2 opens the PDF (an encrypted one that needs a password fails `conversion_failed`, not retryable, "The PDF is password-protected."), counts the pages and the characters of each of the first 50 pages. The PDF `needs_ocr` when at least half of the probed pages have fewer than 50 characters, or when MarkItDown's text has `(cid:N)` tokens or U+FFFD for more than 5 % of its words. Pages after `DOCUMENT_MAX_PAGES` (100) are cut before conversion (a new pdfium document with the first pages), warning `pages_truncated`. "Table-heavy" is not guessed: MarkItDown's pdfplumber pass keeps simple tables, and `ocr: "always"` sends a PDF to Docling for layout and tables.
- **F5, container bombs.** Before a ZIP-based kind converts: at most 10,000 entries, at most `DOCUMENT_MAX_UNCOMPRESSED_BYTES` (200,000,000) uncompressed in total, and no entry with a ratio over 100 → `too_large`. `RLIMIT_AS` is the backstop.
- **F6, output formats.** MarkItDown gives Markdown. `markdown` is it, whitespace normalised. `html` is markdown-it-py (CommonMark plus tables, raw HTML escaped) rendering it. `text` is the text of that HTML with a newline after each block. Docling results use Docling's own `export_to_markdown`, `export_to_html` and `export_to_text`.
- **F7, the result.** `ExtractResult` gains `document: DocumentInfo | None = None` with `kind`, `converter` (`markitdown` or `docling`), `pages` (total, None for non-paged kinds), `pages_converted`, `ocr` (bool) and `file_name` (uploads only). `content_type` is the sniffed media type. URL documents have `mode_used: "fast"`, also after a render that ended on a file (F18). Uploads have `mode_used: "upload"`, `url: null`, `final_url: null`, `attempts: 0`. The new defaults keep cached results valid. `JobItem.url` becomes `str | None` (null for uploads).
- **F8, document metadata** (`metadata: true`): from the PDF info dictionary and XMP (`dc:title`, `dc:creator`, `dc:description`, `dc:rights`, `xmpRights:WebStatement`, `cc:license`, dates), OOXML `docProps/core.xml`, ODF `meta.xml` and the EPUB package document (`dc:*`). Licences go through `LicenseCollector`, so a CC licence in a PDF's XMP becomes `CC-BY-SA-4.0` with its source. Parsed with `defusedxml`. Language: `dc:language`, then `Content-Language`, then the text.
- **F9, quality.** A document is judged like `text/plain`: `assess(None, content)` and `QUALITY_MIN_WORDS`. It never escalates to the browser. A scan without OCR ends `empty_content` or `content_too_short` with the warning `ocr_unavailable`.
- **F10, URL documents.** Pipeline step 7 sends document kinds to `pipeline_documents.extract_document`. The byte limit is `DOCUMENT_MAX_BYTES` (20,000,000; at most `FETCH_MAX_BYTES`, checked at start-up) instead of `CONVERT_MAX_BYTES`. Compliance after the fetch checks the headers only (`X-Robots-Tag`, `tdm-reservation`), as for `text/plain`. Cache, revalidation and fingerprints work as for pages; the cache key gains `ocr`. A cached document result counts as final, also when it was cached with `ocr_unavailable`.
- **F11, the `ocr` option.** `ExtractRequest`, `BatchRequest` and the upload form take `ocr: "auto" | "never" | "always"` (default `auto`). `auto`: Docling for scans and broken text layers (F4), images and ODF. `never`: MarkItDown only (images and ODF answer `unsupported_content_type`). `always`: Docling for every PDF too. `needs_ocr(kind, probe, ocr, settings)` is the one rule; Docling is used only while `DOCLING_ENABLED` and `JOBS_ENABLED`. `ocr: "always"` while Docling is off answers 400 `invalid_request` ("OCR is not enabled on this service.").
- **F12, Docling dispatch (D2 a).** `BrowserDispatcher` becomes the general `PoolDispatcher` (`pool_dispatch.py`), configured per pool; `BrowserDispatcher` and its metric stay as they are, `DoclingDispatcher` adds `text_extraction_docling_dispatch_total{outcome}` with the same outcomes.
  - A sync request that needs OCR creates a one-item job (kind `extract` for a URL, `file` for an upload) and queues it on `docling` under the client `<key name>#live`.
  - The bytes go to `job:{<id>}:input:<index>`, a hash with `body` and `meta` (JSON: kind, content type, file name, validators, `Content-Language`, `raw_sha256`, probe verdict), TTL `DOCLING_INPUT_TTL_SECONDS` (3600). The worker deletes it once the item has its outcome. A job whose input expired before the claim fails `timeout`, retryable.
  - The MarkItDown result, when there is one, is the item's draft; the worker keeps the better of both (`escalation.better`).
  - Without `wait_seconds` the request waits `DOCLING_SYNC_WAIT_SECONDS` (20) in all for the result, then answers the draft with `ocr_unavailable` (no draft: `failed`/`timeout`, retryable) and deletes the job. With `wait_seconds` the `Handover` turns it into a 202 with the Docling job.
  - Back-pressure: while Valkey's memory guard is high, or the caller already has `DOCLING_MAX_OPEN_PER_CLIENT` (5) unfinished Docling jobs, nothing is dispatched: the draft is answered with `ocr_unavailable` (no draft: `failed`/`rate_limited`, retryable).
  - Graceful shutdown of API pods covers the fast phase plus `DOCLING_SYNC_WAIT_SECONDS` + 5 s while `DOCLING_ENABLED`.
- **F13, batches.** The extract worker moves an item that needs OCR to pool `docling` (same job, index and client): input, then draft, then the queue, then the completion here, as B5 moves items to `browser`. While the memory guard is high it keeps the draft with `ocr_unavailable` instead.
- **F14, the Docling worker.** `text-extraction-docling-worker`, pool `docling`, the package-3 `Worker` with a `DoclingRunner`; internal port only.
  - Lease `2 × DOCLING_TIMEOUT_SECONDS + 10`. `DOCLING_CONCURRENCY` (1) slot loops, each converting in a child process of its own (F3 rules; the child loads the models once; killed at `DOCLING_TIMEOUT_SECONDS`, 300; `DOCLING_MEMORY_LIMIT_MB` 6144).
  - Docling options: PDF pipeline with OCR (`TesseractCliOcrOptions`, languages from `DOCLING_OCR_LANGUAGES`, default `deu,eng`), full-page OCR when the probe said "scan", table structure in fast mode, `artifacts_path` = `DOCLING_MODELS_PATH`, CPU only with `DOCLING_THREADS` (4) threads, `document_timeout` = `DOCLING_TIMEOUT_SECONDS`. Pages after `DOCLING_MAX_PAGES` (100) are cut (`pages_truncated`).
  - It runs `Pipeline.convert_queued_document`, which builds the result like the inline path (F7-F9), compares with the draft and stores the kept one in the result cache and fingerprints for URL items.
  - On SIGTERM it stops claiming and gives items in flight 30 s, then kills its children; their leases expire and the items run again.
  - Before `/ready` it loads the models and converts a built-in one-page scan; missing models end the process with exit code 1 and a log line naming `DOCLING_MODELS_PATH`.
- **F15, models on the PVC.** `text-extraction-docling-worker --download-models` downloads Docling's layout and TableFormer models (Docling's own downloader, its pinned revisions) and `deu.traineddata`, `eng.traineddata` and `osd.traineddata` from `tesseract-ocr/tessdata_best` at a pinned commit, each checked against a SHA-256 in `documents/models.toml`. It writes into a temporary directory under `DOCLING_MODELS_PATH` and renames it into place, then writes `.complete-<manifest hash>`; with that marker present it does nothing. `--check` converts the built-in scan and exits 0 or 1. Helm: an init container runs `--download-models` with the PVC mounted writable; the worker mounts it read-only. `models.download: false` with `models.persistence.existingClaim` serves an air-gapped, pre-filled volume. Compose: a one-shot `docling-models` service on a named volume. `TESSDATA_PREFIX` points at the PVC.
- **F16, switches (D3 a).** `DOCLING_ENABLED` (default false). Helm renders it into every pod from `workers.docling.enabled` (default false); compose reads it from `.env` (`docker compose --profile docling up` with `DOCLING_ENABLED=true`).
- **F17, `POST /v1/extract/file`.** `multipart/form-data` with one file part `file` and the form fields `format`, `metadata`, `ocr`, `wait_seconds` (default 25, D2 a).
  - The body limit is `DOCUMENT_MAX_BYTES` + 64 KiB (`BodyLimitMiddleware.path_limits`); at most 1 file and 8 fields.
  - The part's file name is reduced to its base name, control characters removed, at most 255 characters, and reported as `document.file_name`; it is never logged.
  - HTML and plain-text uploads use the package-2 converters (links made absolute against nothing: relative links stay as they are).
  - No compliance check (`compliance.decision: "skipped"`), no result cache (`cache: "bypass"`), no stored fingerprint (computed, `changed: null`).
  - `wait_seconds` hands an inline conversion over to a P1 job like `/v1/extract`, and an OCR upload to its Docling job (F12). The job kind is `file`.
  - The usual auth (user role) and inbound rate limit apply. OpenAPI documents the multipart body.
- **F18, a render that ends on a file.** When a render's main frame ends on a download or a document type (B2 step 5) and the request carries no caller secrets, the pipeline fetches that URL with the fast fetcher in a turn of its own (compliance as in fast mode) and converts it per F10, with the warning `document_fetched_directly` and `mode_used: "fast"`. `Rendered` gains `download_url`, the URL of a download the main frame started (also by a clicked link). With secrets it stays `unsupported_content_type`: the fetcher must not send the caller's cookies or headers.
- **F19, metrics.** `text_extraction_document_conversions_total{converter, kind, outcome}`, `text_extraction_document_conversion_seconds{converter}`, `text_extraction_document_pages_total{converter}`, `text_extraction_docling_dispatch_total{outcome}`, the queue gauges for pool `docling`. Helm alert rules: Docling backlog (queue gauge), Docling workers down.

## Review Focus

1. **Hostile documents:** a ZIP bomb as DOCX, a PDF with 10,000 pages, a PDF that makes pdfminer spin for minutes, an XLSX with a million rows, an image of 30,000 × 30,000 pixels. The child is killed or refuses (`timeout`, `too_large`), the parent's memory stays flat, and the next request converts normally. Owner: Tasks 3 and 5 (and 9 for Docling).
2. **Wrong or missing content types:** a PDF served as `application/octet-stream`, `text/plain` or without a type; an upload named `report.pdf` that is HTML; a DOCX whose URL ends in `.zip`. Detection follows the bytes, an HTML page served as HTML keeps its package-2 answer. Owner: Tasks 2 and 6.
3. **No Docling worker in time** (scaled to zero, crashed, models missing): a sync scan answers its text layer with `ocr_unavailable` after `DOCLING_SYNC_WAIT_SECONDS` and its job and input are gone; an image fails `timeout`, retryable; a batch item waits in the queue and the backlog alert fires. Owner: Tasks 8 and 9.
4. **Valkey memory with large inputs:** many 20 MB scans at once. The guard and `DOCLING_MAX_OPEN_PER_CLIENT` stop new inputs, inputs are deleted with the outcome and expire after `DOCLING_INPUT_TTL_SECONDS`, and no input bytes appear in `GET /v1/jobs` or the logs. Owner: Task 8.
5. **Odd but valid files:** a password-protected PDF, a PDF with 0 pages, a scan with a hidden OCR layer of garbage, an upload without a file name or with `../../etc/passwd` or a non-UTF-8 name, a CSV in Latin-1. Clean answers: `conversion_failed` (not retryable), `empty_content`, OCR through the garbage rule, a sanitised `file_name`, decoded text. Owner: Tasks 2, 4 and 7.

---

### Task 1: Settings, models and codes

**Files:**
- Modify: `src/text_extraction/settings.py`, `src/text_extraction/extract_models.py`, `src/text_extraction/job_models.py`, `docs/configuration.md`, `.env.example`
- Create: `src/text_extraction/documents/__init__.py`, `src/text_extraction/documents/models.py`
- Test: `tests/test_document_models.py`, `tests/test_settings.py` (extend)

**Interfaces:**
- Produces: settings `document_max_bytes` (20_000_000, ≥ 1024, ≤ `fetch_max_bytes`), `document_max_pages` (100, ≥ 1), `document_max_uncompressed_bytes` (200_000_000), `document_concurrency` (2, ≥ 1), `document_memory_limit_mb` (2048, ≥ 256), `docling_enabled` (False), `docling_timeout_seconds` (300, 30-1800), `docling_sync_wait_seconds` (20, 0-25), `docling_input_ttl_seconds` (3600, ≥ 600), `docling_max_open_per_client` (5, ≥ 1), `docling_concurrency` (1), `docling_threads` (4), `docling_max_pages` (100), `docling_memory_limit_mb` (6144), `docling_ocr_languages` ("deu,eng", Tesseract codes, validated `^[a-z_]{3,}(,[a-z_]{3,})*$`), `docling_models_path` (`/models`; Docling models in `docling/`, Tesseract data in `tessdata/`).
- Produces: `DocumentKind = Literal["pdf","docx","pptx","xlsx","xls","epub","csv","odt","ods","odp","png","jpeg","tiff","webp","bmp"]`; `OcrOption = Literal["auto","never","always"]`; `DocumentInfo(kind, converter: Literal["markitdown","docling"], pages: int | None, pages_converted: int | None, ocr: bool, file_name: str | None = None)`; `ExtractResult.document: DocumentInfo | None = None`; `ExtractResult.url: str | None`; `mode_used: Literal["fast","browser","upload"]`; `ExtractRequest.ocr` and `BatchRequest.ocr` and `JobOptions.ocr` (default `"auto"`); `JobKind` gains `"file"`; `JobItem.url: str | None`.

- [ ] **Step 1: Failing tests:** `test_document_max_bytes_cannot_exceed_fetch_max_bytes` (Settings with `DOCUMENT_MAX_BYTES=30000000`, `FETCH_MAX_BYTES=20000000` → `ConfigError`), `test_docling_ocr_languages_validated` (`"deu,eng"` ok, `"deu;rm -rf"` → `ConfigError` without echoing the value), `test_cached_result_without_document_still_parses` (an `ExtractResult` JSON from package 4 validates, `document is None`), `test_ocr_option_defaults_to_auto` (ExtractRequest, BatchRequest), `test_upload_result_has_no_url` (`ExtractResult(url=None, mode_used="upload", …)` dumps `url: null`).
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3:** implement the settings with comments in the style of `settings.py`, the models, and the rows in `docs/configuration.md` ("Documents and OCR" section) and `.env.example`.
- [ ] **Step 4:** `uv run pytest tests/test_document_models.py tests/test_settings.py -q` → PASS; `uv run mypy` clean.
- [ ] **Step 5:** commit `feat: add document and OCR settings and result models`.

### Task 2: Kind detection and container limits

**Files:**
- Create: `src/text_extraction/documents/sniff.py`
- Test: `tests/test_document_sniff.py`, fixtures in `tests/documents/corpus/` (Task 11 adds the full corpus; this task commits the minimal headers it needs, built in the test with `zipfile` and byte strings)

**Interfaces:**
- Produces: `sniff(body: bytes, declared: str | None, name: str | None) -> DocumentKind | Literal["html","text"] | None` (None: unsupported); `media_type(kind) -> str`; `check_container(body: bytes, kind: DocumentKind, max_uncompressed: int) -> None` raising `DocumentTooLarge`.

- [ ] **Step 1: Failing tests:** `test_pdf_signature_wins_over_declared_type` (`%PDF-1.7` with `text/plain`, `application/octet-stream`, None → `"pdf"`), `test_html_declared_stays_html` (`text/html` body `<html>` → `"html"`; `text/html` body `%PDF-` → `"html"`, the package-2 path is unchanged), `test_ooxml_main_part` (ZIP with `[Content_Types].xml` naming `word/document.xml` → `docx`, `ppt/presentation.xml` → `pptx`, `xl/workbook.xml` → `xlsx`), `test_epub_and_odf_mimetype_entry`, `test_xls_ole_workbook` and `test_doc_ole_is_unsupported`, `test_images` (PNG, JPEG, TIFF II/MM, WebP, BMP → kinds; GIF → None), `test_csv_by_type_or_extension` (`text/csv` → csv; `text/plain` + `data.csv` → csv; `text/plain` + no name → `"text"`), `test_plain_zip_is_unsupported`, `test_zip_bomb_refused` (an entry of 300 MB of zeros, ratio > 100 → `DocumentTooLarge`), `test_too_many_entries_refused` (10,001 entries).
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3:** implement with `zipfile` (central directory only, nothing decompressed) and `olefile`-free OLE parsing (read the directory sector for stream names; `olefile` is BSD and comes with MarkItDown's `xls` extra, so it may be used instead).
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: detect document kinds and refuse container bombs`.

### Task 3: DocumentPool, child processes with a kill switch

**Files:**
- Create: `src/text_extraction/documents/pool.py`
- Modify: `src/text_extraction/app.py`, `src/text_extraction/worker.py`, `src/text_extraction/browser_worker.py` (create, start before `/ready`, stop at shutdown)
- Test: `tests/test_document_pool.py`

**Interfaces:**
- Produces: `DocumentPool(processes: int, memory_limit_mb: int, *, max_tasks: int = 50, target: str)`; `await pool.start()`; `await pool.run(function_name: str, payload: bytes | dict, deadline: float) -> Any` (deadline as `loop.time()`); exceptions `ConversionTimeout`, `ConversionCrashed`, `ConversionOutOfMemory`; `await pool.shutdown()`. `target` is a module path whose functions the child may call (an allowlist dict in the child module, no pickled callables from the parent).

- [ ] **Step 1: Failing tests** (target `tests.document_pool_targets`, a test module with `echo`, `sleep_forever`, `allocate(mb)`, `crash` (os._exit(3)), `count_calls`): `test_runs_in_child_and_returns`, `test_deadline_kills_child_and_next_call_works` (sleep_forever with a 0.5 s deadline → `ConversionTimeout` within 1.5 s; then `echo` succeeds), `test_memory_limit` (allocate 4096 with a 512 MB limit → `ConversionOutOfMemory`), `test_crash_is_reported_and_replaced`, `test_child_recycled_after_max_tasks` (pids differ after `max_tasks` calls), `test_waiting_for_a_free_child_counts_against_the_deadline` (1 child busy, second call with a 0.3 s deadline → `ConversionTimeout`), `test_parent_never_imports_markitdown` (after `start()` and a call, `"markitdown" not in sys.modules` and `"pandas" not in sys.modules`), `test_shutdown_kills_children`.
- [ ] **Step 2:** run, expect FAIL.
- [ ] **Step 3:** implement with `multiprocessing.get_context("spawn")`, one duplex `Pipe` per child, `asyncio` waiting via `loop.add_reader` on the connection (or a thread that polls with timeout), `resource.setrlimit(RLIMIT_AS)` in the child entry before imports, SIGKILL plus `join` on timeout. Children ignore SIGINT/SIGTERM; the parent stops them.
- [ ] **Step 4:** PASS (`-p no:randomly` not needed; each test under 5 s).
- [ ] **Step 5:** commit `feat: add document pool with child processes, deadlines and memory limits`.

### Task 4: PDF probe, truncation and document metadata

**Files:**
- Create: `src/text_extraction/documents/pdf.py`, `src/text_extraction/documents/meta.py`
- Test: `tests/test_document_pdf.py`, `tests/test_document_meta.py`

**Interfaces:**
- Produces: `PdfProbe(pages: int, probed: int, thin_pages: int, encrypted: bool)`; `probe_pdf(body: bytes) -> PdfProbe` (raises `PasswordProtected`); `truncate_pdf(body: bytes, max_pages: int) -> bytes`; `scan_verdict(probe: PdfProbe) -> bool` (≥ half thin); `broken_text(markdown: str) -> bool` (`(cid:N)` or U+FFFD over 5 % of words); `document_metadata(body: bytes, kind: DocumentKind, base: str | None, content_language: str | None, text: str) -> PageMetadata`.

- [ ] **Step 1: Failing tests** with corpus files (Task 11 lists them; this task adds `text.pdf`, `scan.pdf`, `encrypted.pdf`, `empty.pdf`, `xmp-cc-by-sa.pdf`, `core.docx`): `test_probe_counts_pages_and_thin_pages`, `test_scan_verdict` (scan.pdf → True, text.pdf → False), `test_password_protected`, `test_zero_pages` (probe pages 0, verdict False, conversion later gives `empty_content`), `test_truncate_keeps_first_pages` (12 pages, max 5 → 5), `test_broken_text_rule` (`"(cid:12)(cid:13) word"` → True; ordinary German text → False), `test_xmp_cc_license` (metadata.license.id == `CC-BY-SA-4.0`, source names XMP), `test_pdf_info_title_author_dates`, `test_docx_core_properties`, `test_metadata_never_resolves_entities` (core.xml with an external entity → no network, no expansion, defusedxml refuses and the metadata is empty).
- [ ] **Step 2:** FAIL. **Step 3:** implement with pypdfium2 (`PdfDocument`, `get_textpage().count_chars()`, `new_pdf().import_pages`) and defusedxml; the XMP stream via pypdfium2's raw metadata API (verify the name in the pinned version). **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: probe PDF text layers and read document metadata`.

### Task 5: MarkItDown conversion in the child

**Files:**
- Create: `src/text_extraction/documents/markitdown_convert.py` (child side, allowlisted in `documents/child.py`), `src/text_extraction/documents/child.py`, `src/text_extraction/documents/formats.py`
- Modify: `pyproject.toml`, `uv.lock` (markitdown extras, pypdfium2, markdown-it-py)
- Test: `tests/test_document_markitdown.py`

**Interfaces:**
- Consumes: `DocumentPool` (Task 3), `sniff.check_container` (Task 2), `pdf.*` and `meta.document_metadata` (Task 4).
- Produces: child function `"markitdown"` taking `{"body", "kind", "format", "metadata", "base", "content_language", "max_pages", "max_uncompressed"}` and returning `DocumentConversion(content: str, title: str | None, warnings: list[str], pages: int | None, pages_converted: int | None, needs_ocr: bool, metadata: dict | None)`; `formats.from_markdown(markdown: str, fmt: OutputFormat) -> str` (F6).

- [ ] **Step 1: Failing tests** (through a real `DocumentPool`): `test_text_pdf_to_markdown` (contains "Photosynthese" and a Markdown table row from text.pdf), `test_docx_headings_and_table`, `test_pptx_slides_and_notes`, `test_xlsx_sheets_as_tables`, `test_xls`, `test_epub_chapters`, `test_csv_latin1` (decoded "Größe"), `test_scan_pdf_reports_needs_ocr`, `test_pages_truncated_warning`, `test_formats` (`text` has no `#` or `|`; `html` has `<h1>` and `<table>`, `<script>` from the source is escaped), `test_xlsx_million_rows_hits_deadline` (generated in the test; 25 s deadline → `ConversionTimeout`, pool still works).
- [ ] **Step 2:** FAIL. **Step 3:** implement; call the converter classes directly per kind. **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: convert PDF, Office, EPUB and CSV with MarkItDown in child processes`.

### Task 6: Documents in the URL pipeline

**Files:**
- Create: `src/text_extraction/pipeline_documents.py`, `src/text_extraction/documents/routing.py`
- Modify: `src/text_extraction/pipeline.py` (step 7 dispatch, `_cache_options` gains `ocr`, module docstring), `src/text_extraction/extract_api.py` (400 for `ocr: "always"` while Docling is off), `src/text_extraction/jobs_api.py` (same for batches)
- Test: `tests/test_pipeline_documents.py`, `tests/test_document_routing.py`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: `needs_ocr(kind: DocumentKind, conversion: DocumentConversion | None, ocr: OcrOption, settings: Settings) -> bool` (F11); `pipeline_documents.extract_document(...)` returning the envelope, or an `OcrNeeded(draft: Envelope | None, input: DocumentInput)` marker that the callers of Tasks 8 and 9 act on; `DocumentInput(body: bytes, meta: DocumentInputMeta)`.

- [ ] **Step 1: Failing tests** (pytest-httpserver as in `test_pipeline.py`): `test_pdf_url_converts` (status success, `document.converter == "markitdown"`, `content_type == "application/pdf"`, `mode_used == "fast"`), `test_octet_stream_pdf_converts`, `test_text_plain_pdf_converts`, `test_html_page_answer_unchanged` (a package-2 HTML fixture gives byte-identical JSON apart from timings), `test_document_over_document_max_bytes_too_large`, `test_x_robots_tag_noai_blocks_pdf` (`X-Robots-Tag: noai` under `enforce` → `failed`/`tdm_reserved`, as for `text/plain`), `test_pdf_cached_and_revalidated` (second request `hit`; after TTL a 304 → `revalidated`), `test_ocr_option_in_cache_key`, `test_scan_without_docling_answers_text_layer` (`empty_content`, warning `ocr_unavailable`), `test_image_without_docling_unsupported`, `test_scan_with_docling_returns_ocr_needed` (Docling enabled → `OcrNeeded` with the draft and the input meta), `test_routing_table` (parametrised over kind × ocr option × enabled flags).
- [ ] **Step 2:** FAIL. **Step 3:** implement; the turn, compliance and cache code stays in `pipeline.py`, only the conversion branch is new. **Step 4:** PASS, and the full suite still passes.
- [ ] **Step 5:** commit `feat: convert documents fetched by URL`.

### Task 7: `POST /v1/extract/file`

**Files:**
- Create: `src/text_extraction/file_api.py`, `src/text_extraction/file_models.py`
- Modify: `src/text_extraction/app.py` (router, `path_limits["/v1/extract/file"]`), `pyproject.toml` (`python-multipart`), `docs/architecture.md` (Public API row)
- Test: `tests/test_file_api.py`

**Interfaces:**
- Consumes: Tasks 1-6; `Handover` and the P1 path of `extract_api.py` (reuse, do not copy).
- Produces: `FileForm` (Pydantic model used with `Annotated[FileForm, Form()]`: `format`, `metadata`, `ocr`, `wait_seconds: WaitSeconds = 25`); `sanitize_file_name(name: str | None) -> str | None`; `Pipeline.extract_upload(body: bytes, name: str | None, form: FileForm, principal) -> Envelope | OcrNeeded`.

- [ ] **Step 1: Failing tests** (TestClient with fakeredis): `test_upload_pdf` (200, `mode_used == "upload"`, `url is None`, `document.file_name == "report.pdf"`, `cache == "bypass"`, `compliance.decision == "skipped"`), `test_upload_requires_auth` (401), `test_upload_rate_limited`, `test_upload_over_limit_413_before_parse` (Content-Length over `DOCUMENT_MAX_BYTES` + 64 KiB → 413 `invalid_request`, nothing read), `test_two_files_refused` (422), `test_file_name_sanitised` (`../../etc/passwd` → `passwd`; `"a\x00b.pdf"` → `ab.pdf`; 300 chars → 255; no name → None), `test_html_upload_uses_html_converter`, `test_unsupported_upload` (GIF → 200 `failed`/`unsupported_content_type`), `test_ocr_always_while_docling_off_400`, `test_openapi_documents_multipart` (the schema has `multipart/form-data` with `file` of format binary), `test_file_name_not_logged` (capture logs; the name appears nowhere).
- [ ] **Step 2:** FAIL. **Step 3:** implement; read the part into memory up to the limit and close it (deletes the spool file). **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: add POST /v1/extract/file`.

### Task 8: Docling dispatch, inputs and back-pressure

**Files:**
- Create: `src/text_extraction/pool_dispatch.py`
- Modify: `src/text_extraction/browser_dispatch.py` (built on `PoolDispatcher`, behaviour and metric unchanged), `src/text_extraction/jobs.py` (input hash, `open_docling_jobs(client)` counter), `src/text_extraction/extract_api.py`, `src/text_extraction/file_api.py`, `src/text_extraction/worker.py` (move to `docling`, F13), `src/text_extraction/server.py` (graceful seconds while `DOCLING_ENABLED`), `src/text_extraction/queue_metrics.py` (pool `docling`)
- Test: `tests/test_pool_dispatch.py`, `tests/test_docling_dispatch.py`, extend `tests/test_browser_dispatch.py` (still green unchanged), `tests/test_worker.py`

**Interfaces:**
- Consumes: `OcrNeeded` (Task 6), `JobStore`, `FairQueue`, `MemoryGuard`.
- Produces: `PoolDispatcher(pool: str, store, queue, settings, registry, *, metric: str, deadline: Callable[[JobOptions], float])` with `submit`, `wait`, `abandon`, `answered` as `BrowserDispatcher` has today; `JobStore.put_input(job_id, index, body, meta, ttl)`, `get_input`, `delete_input`; `DoclingDispatcher = PoolDispatcher("docling", …)`.

- [ ] **Step 1: Failing tests:** `test_sync_scan_answers_ocr_result` (a fake Docling runner in the test completes the item → the answer is the OCR result, the job and input are deleted), `test_sync_scan_falls_back_after_wait` (no worker; after `DOCLING_SYNC_WAIT_SECONDS` = 1 → draft with `ocr_unavailable`, job gone, `docling_dispatch_total{outcome="not_claimed"}` 1), `test_image_without_worker_times_out` (`failed`/`timeout`, retryable), `test_wait_seconds_gives_202_docling_job`, `test_upload_default_wait_25_gives_202` (fake slow worker), `test_memory_guard_high_skips_dispatch` (draft + `ocr_unavailable`), `test_open_job_limit` (5 open → sixth answers draft + `ocr_unavailable`; image → `rate_limited`), `test_input_never_in_job_view` (`GET /v1/jobs/{id}` JSON has no `body`/input bytes), `test_input_ttl_set`, `test_batch_item_moves_to_docling` (input, draft, queue, completion order; a crash after the draft leaves it leased and the rerun moves it once), `test_browser_dispatch_unchanged` (existing tests pass untouched), `test_graceful_seconds_cover_docling_wait`.
- [ ] **Step 2:** FAIL. **Step 3:** implement; refactor `BrowserDispatcher` onto `PoolDispatcher` first with its tests green, then add Docling. **Step 4:** PASS.
- [ ] **Step 5:** commit `feat: dispatch OCR work to the docling pool`.

### Task 9: The Docling worker

**Files:**
- Create: `src/text_extraction/docling_worker.py`, `src/text_extraction/documents/docling_convert.py` (child side), `src/text_extraction/documents/models.toml`, `src/text_extraction/documents/models.py` (download, verify, marker), `src/text_extraction/documents/scan_check.png` (built-in one-page scan)
- Modify: `pyproject.toml` (extra `docling`, script `text-extraction-docling-worker`, uv index `pytorch-cpu`), `uv.lock`, `src/text_extraction/pipeline.py` (`convert_queued_document`)
- Test: `tests/test_docling_worker.py` (fakes, default suite), `tests/docling/test_docling_convert.py` (marker `docling`, real models)

**Interfaces:**
- Consumes: `Worker`, `ItemRunner` (package 3), `DocumentPool` (Task 3), `PoolDispatcher` inputs (Task 8), `meta.document_metadata`, `formats` (Tasks 4-5).
- Produces: `DoclingRunner(ItemRunner)`; child function `"docling"` taking `{"body", "kind", "format", "full_page_ocr", "languages", "threads", "max_pages", "models_path", "timeout"}` and returning `DocumentConversion`; `Pipeline.convert_queued_document(req, principal, input: DocumentInput, draft: Envelope | None, convert: Callable) -> Envelope`; CLI `--download-models` and `--check`.

- [ ] **Step 1: Failing tests (fakes):** `test_runner_keeps_better_of_draft_and_ocr`, `test_runner_writes_cache_for_url_items` (and not for uploads), `test_expired_input_fails_timeout_retryable`, `test_input_deleted_after_outcome` (also after a failed conversion), `test_child_timeout_fails_item` (`failed`/`timeout`, not retryable: the same file would time out again; message "OCR did not finish within DOCLING_TIMEOUT_SECONDS."), `test_missing_models_exit_1` (empty models dir → exit code 1, log names the path), `test_download_models_is_idempotent` (fake HTTP server serving the manifest files; second run downloads nothing; a wrong SHA-256 fails and leaves no marker), `test_worker_does_not_import_docling_in_parent`.
- [ ] **Step 2: Failing tests (marker `docling`, CI job of Task 10):** `test_scan_pdf_german_ocr` (word recall ≥ 0.9 against `scan.expected.txt`, umlauts present), `test_png_and_jpeg_ocr`, `test_odt_converts`, `test_text_pdf_always_mode_keeps_table` (Markdown table with the expected header row), `test_page_cap_warning`.
- [ ] **Step 3:** FAIL. **Step 4:** implement. Verify the Docling API names (`DocumentConverter`, `PdfPipelineOptions`, `TesseractCliOcrOptions`, `AcceleratorOptions`, `TableFormerMode`, the model downloader) against the pinned version and note them in the module docstring; record the model licences in `NOTICE`. **Step 5:** PASS (`uv run pytest -m docling` locally after `--download-models` into a temp dir).
- [ ] **Step 6:** commit `feat: add the docling worker with OCR, models download and self-check`.

### Task 10: Images, compose, Helm, CI and the licence gate

**Files:**
- Modify: `Dockerfile` (target `docling`: venv with extra `docling`, `apt-get install --no-install-recommends tesseract-ocr` without language packs except what Debian forces, `TESSDATA_PREFIX=/models/tessdata`, non-root, `CMD text-extraction-docling-worker`), `docker-compose.yml` (services `docling-models` and `docling-worker` under profile `docling`, volume `docling-models`, `DOCLING_ENABLED` from env), `deploy/helm/text-extraction/values.yaml`, templates `docling-deployment.yaml`, `docling-pvc.yaml`, `docling-scaledobject.yaml`, `docling-hpa.yaml`, `docling-service-internal.yaml`, `prometheusrule.yaml`, `networkpolicy.yaml`, `configmap.yaml`/env rendering of `DOCLING_ENABLED`, chart `README.md`; `.github/workflows/ci.yml` (helm lint with Docling on; licence gate; job `documents`: `uv sync --extra docling`, cache the models by the manifest hash, `--download-models`, `pytest -m docling`), `.github/workflows/image.yml` (variant `docling`: build, `--check` with the models mounted from the cache, "API image has no docling or torch", size printed)
- Create: `scripts/check_licenses.py`
- Test: `tests/test_check_licenses.py`, `deploy/helm/test.sh` cases

**Interfaces:**
- Produces: Helm values `workers.docling.{enabled: false, replicaCount: 1, concurrency: 1, image, resources (requests 1 CPU/3Gi, limits 4 CPU/8Gi, placeholders until package 11), terminationGracePeriodSeconds: 60, autoscaling.keda|hpa, nodeSelector}`, `workers.docling.models.{download: true, persistence: {size: 5Gi, accessModes: [ReadWriteMany], storageClass: "", existingClaim: ""}}`.

- [ ] **Step 1: Failing tests:** `test_check_licenses_fails_on_gpl` (fake distribution metadata with `License-Expression: GPL-3.0-only` → exit 1, names it; `LGPL-2.1` → warning only; MIT → 0), helm test cases `docling off renders nothing`, `docling on renders deployment, PVC, init container with writable mount, worker with read-only mount, DOCLING_ENABLED=true in every pod`, `existingClaim skips the PVC`, `download false skips the init container`, `keda and hpa both on fails`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_check_licenses.py`, `bash deploy/helm/test.sh`, the helm lint matrix (helm binary as in package 4), `docker compose --profile docling config` → all pass. The Docling image build and `--check` run in GitHub Actions (the sandbox cannot build images); Jan's PC verifies `docker compose --profile docling up` via Remote Control in a fresh clone folder, as in package 4.
- [ ] **Step 5:** commit `feat: ship the docling worker image, compose profile, Helm toggle and licence gate`.

### Task 11: Corpus, done-when and docs

**Files:**
- Create: `tests/documents/make_corpus.py` (dev-only generator; the generated files are committed), `tests/documents/corpus/` (`text.pdf` German with a table, `scan.pdf` two pages of rendered German text with umlauts at 200 dpi plus `scan.expected.txt`, `photo.jpeg`, `page.png`, `encrypted.pdf`, `empty.pdf`, `xmp-cc-by-sa.pdf`, `long.pdf` 120 pages, `core.docx`, `slides.pptx`, `sheets.xlsx`, `legacy.xls`, `book.epub`, `latin1.csv`, `text.odt`, `bomb.docx`), `tests/documents/test_corpus.py` (default suite: MarkItDown kinds; marker `docling`: the OCR kinds)
- Modify: `docs/architecture.md` (new section "Documents and OCR" with F1-F19 and D1-D3 as decided; Overview and pipeline step 5 updated; "Known limits"), `docs/packages.md` (row 5 unchanged; note the D2 choice), `README.md` (upload example), `pyproject.toml` (dev group: reportlab, python-docx, pillow; marker `docling`)

- [ ] **Step 1:** generate the corpus (`uv run python tests/documents/make_corpus.py`), commit the files (each under 300 KB except `long.pdf`, under 1 MB).
- [ ] **Step 2: Corpus test:** `test_corpus_converts` parametrised over every supported file: status `success` (with `QUALITY_MIN_WORDS=0`), the expected phrases from `corpus/expected.json` present, `document.kind` as named; `test_scan_yields_text` (marker `docling`, word recall ≥ 0.9).
- [ ] **Step 3:** run both suites → PASS. Then the live check: label the PR `live`; `wlo_200` auto must stay ≥ 80 % (PDF URLs in the set should now count as success); report the change in the PR.
- [ ] **Step 4:** docs; commit `test: add the document corpus and document the package`.

### Final: review and hand-over

- [ ] `requesting-code-review` over the whole branch (Review Focus above as the brief), fixes via `receiving-code-review`.
- [ ] `verification-before-completion`: `uv run pytest`, `uv run pytest -m docling` (CI job), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, helm lint matrix, image workflow green, live run reported; done-when met (corpus converts, scans yield text).
- [ ] `finishing-a-development-branch`: PR, squash merge once green (Jan's standing permission), progress memory updated.
