# Package 7: PII Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A caller can ask for personal data in a result to be handled: `pii: {"mode": "mask" | "redact" | "hash" | "annotate"}` on `POST /v1/extract`, `POST /v1/extract/file` and `POST /v1/batch` finds names, e-mail addresses, phone numbers, IBANs, card numbers and IP addresses in German and English text (Presidio with spaCy models from a PVC) and masks, removes, hashes or lists them, in the content and in the result's free-text fields.

**Architecture:**
- **One last hop, pool `nlp`.** PII is the last step of every result that asked for it. Whoever produces the result (the API pod's fast pipeline, the browser, Docling or ASR worker, the extract worker) does not answer or store it: it stores the result as the item's draft and queues the item on pool `nlp` of the package-3 fair queue, as OCR goes to `docling`. The nlp worker applies PII to the draft and writes the item's result. A sync request waits for it like it waits for OCR (`PiiHandoff`, on `PoolDispatcher` and `Handover`); a batch item is moved by `Worker._finish`, the one place every pool writes results.
- **Presidio only in the nlp worker's children.** presidio-analyzer and spaCy run in child processes of the nlp worker (`DocumentPool(target="text_extraction.pii.child")`), which load the models once and are killed at the deadline. The child only finds spans; replacing them, the result fields, HTML text nodes and the report are our own pure code in the parent (`pii/spans.py`, `pii/replace.py`, `pii/fields.py`, `pii/html.py`), testable without models.
- **Fail closed.** A result that asked for PII never leaves the service unprocessed: when the nlp hop cannot run or does not finish, the answer is `failed` with the new code `pii_failed` and neither content nor metadata. One guard (`pii.fields.fail_closed`) sits in front of every answer and every result write.

**Tech Stack:** existing, plus:
- nlp image only (extra `nlp`): `presidio-analyzer` (MIT) with `spacy` (MIT), `phonenumbers` (Apache-2.0), `tldextract` (BSD-3), `regex` (Apache-2.0). Not `presidio-anonymizer`: replacement is our own (HTML text nodes, keyed hashes, overlaps).
- spaCy pipelines `de_core_news_<size>` and `en_core_web_<size>` 3.8.0 (MIT) from the spaCy model releases on GitHub, unpacked onto the PVC, never in the image.

**Spec:**
- `docs/packages.md` row 7: Presidio de/en with `mask`, `redact`, `hash`, `annotate`; spaCy models from PVC. Done when German and English test texts are handled per mode.
- `docs/architecture.md`: Overview (nlp-worker with Presidio, one model PVC per worker type, spaCy for the nlp-worker), pipeline step 6 (post-process: PII), `POST /v1/extract` option `pii`, "Jobs and the fair queue", "Documents and OCR" (F12 dispatch and inputs, F13 moves, F14 worker pattern, F15 models), "Audio and video" (PII applies to transcripts like to any content).
- `CLAUDE.md` rules 1, 2 (stateless, settings as env vars, jobs over ~30 s, models from a PVC, Helm toggles), 4 (result envelope), 5, 7 (no GPL/AGPL).
- Jan's decisions D1-D4 below (2026-10-28: Jan approved the plan and the implementation; the cards were unanswered, so the recommended options (a) apply).

## Global Constraints

- English only. Python 3.13. No GPL or AGPL dependency: the licence gate of package 5 covers the new extra `nlp`.
- New dependencies are pinned to their newest release at least 14 days old at implementation time: today `presidio-analyzer==2.2.364` (2.2.365 of 2026-10-25 is too new), `spacy==3.8.16`; transitive packages that `uv lock` would take newer than 14 days (`thinc` 8.3.14 of 2026-10-25) go into `exclude-newer-package`.
- The API image and the other worker images must not contain presidio, spaCy, thinc or models. The parent process of the nlp worker must not import `presidio_analyzer` or `spacy` (only its children do).
- Stateless pods: drafts and results live in Valkey with TTLs; models live on the PVC, mounted read-only.
- Every new setting is an env var with a documented default in `docs/configuration.md` and `.env.example`, in the task that adds it; Helm sets it where the chart owns it. `PII_HASH_KEY` is a secret.
- Answers without `pii` must not change by a byte (the result gains `pii: null` and `timings.pii_ms: 0` only).
- The result cache and fingerprints keep the unprocessed extraction (N9): `pii` is not part of the cache key.
- Never log, count by value, or put into a metric label any text, span or entity value; entity types and counts at most.
- Not in this package: PII in screenshots (refused, N4), PII inside `POST /v1/structured` and `/v1/chunk` (package 8 calls the same hop), custom entity lists or deny lists per caller, other languages' models, transformer models (`*_trf`, they need torch), S3.

## Decisions of Jan (2026-10-28: the recommended options)

- **D1, size of the spaCy models.** Measured in the sandbox on 2026-10-26 (both languages loaded, Presidio 2.2.364, our recognizers, 4 vCPU): `md` loads in 5.6 s with 0.7 GB RSS; `lg` in 7.0-7.8 s with 1.6 GB RSS, 2.2 GB peak on a 175,000-character German text. On a German test text `md` missed "Dr. Anna Schmidt", `lg` found her; both missed "Herr Müller" (N6 adds a rule for that). Speed is the same (about 6,500 words/s in 20,000-character segments, N7). Volume: `md` 115 MB, `lg` 1.03 GB.
  - (a, recommended, **chosen**) `lg`: better recall on German names, the point of the package; 1.1 GB volume, worker limit 3 GiB.
  - (b) `md`: a tenth of the volume and half the memory, more names missed.
  - Either way `PII_MODEL_SIZE` (`lg|md`) switches it; both are in the manifest.
- **D2, what is found by default** (when a request names no `entities`).
  - (a, recommended, **chosen**) Persons and contact or payment data: `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`, `IP_ADDRESS`. Places, organisations and URLs only when asked (`entities`). Teaching material is full of places and institutions ("Bremen", "Bundesregierung") that are not personal data; masking them would ruin the text.
  - (b) Everything we support, including `LOCATION`, `ORGANIZATION`, `URL`.
- **D3, the key of `hash`.** A plain SHA-256 of a phone number or e-mail can be reversed by trying all candidates, so `hash` uses HMAC-SHA-256.
  - (a, recommended, **chosen**) With `PII_HASH_KEY` (operator secret) set, the same value gives the same hash in every result of the service (`hash_scope: service`), so documents can be linked without the value; without it, a random key per result (`hash_scope: result`): equal values match only within one result.
  - (b) `hash` needs `PII_HASH_KEY`; without it, `mode: hash` answers 400.
- **D4, a sync request whose PII takes longer.** Our measurement: a page of 1,000 words takes well under a second, a 150,000-word PDF about 25 s.
  - (a, recommended, **chosen**) As `transcribe: true` (package 6, M9): a request with `pii` and without `wait_seconds` waits 20 s (`PII_SYNC_WAIT_SECONDS`), then answers 202 with its job, which goes on and stores the result. No work is lost.
  - (b) As OCR (package 5, D2): it waits 20 s, then answers `failed`/`pii_failed`, retryable, and the job is deleted; the caller has to send `wait_seconds` to get a job.

## Rulings in this plan

- **N1, the option.** `ExtractRequest`, `BatchRequest`, `JobOptions` and `FileForm` gain `pii: PiiOptions | None = None` (`pii/types.py`), stored in a job only when set:
  - `mode`: `mask | redact | hash | annotate` (required). A bare string (`"pii": "mask"`) is accepted as `{"mode": "mask"}`; the form field of `/v1/extract/file` takes the bare mode or the JSON object as its string.
  - `entities`: a non-empty list of `PERSON`, `LOCATION`, `ORGANIZATION`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD`, `IP_ADDRESS`, `URL` (no duplicates), default `PII_DEFAULT_ENTITIES` (D2).
  - `language`: `auto | de | en` (default `auto`, N8).
  - `score_threshold`: 0.0-1.0, default `PII_SCORE_THRESHOLD`.
- **N2, the modes.** Replacement text is plain brackets in every format, so Markdown does not read it as HTML:
  - `mask`: each entity becomes `[<TYPE>]`, e.g. `[PERSON]`, `[EMAIL_ADDRESS]`.
  - `redact`: each entity becomes `[REDACTED]`: neither value nor type is left.
  - `hash`: each entity becomes `[<TYPE>:<12 hex>]`, the first 12 hex digits of HMAC-SHA-256 over `<TYPE>:<value>` with the value case-folded and its whitespace collapsed (D3 for the key).
  - `annotate`: the fields stay as they are; `pii.items` lists every entity: `field`, `type`, `start`, `end` (Unicode code points in that field's string), `score`, `text`. At most `PII_MAX_ENTITIES` (1,000) items, then the warning `pii_entities_truncated` (the counts stay complete). `annotate` with `format: html` is 422 `invalid_request` ("annotate needs format markdown or text."), because HTML offsets of text nodes are not stable.
- **N3, the report.** `ExtractResult.pii: PiiReport | None = None`: `mode`, `language` (the model used, `de|en`), `entities` (count per type, only types found), `items` (annotate only, else null), `hash_scope` (`service|result`, hash only, else null). `Timings.pii_ms: int = 0`. `status`, `word_count`, `quality`, `language` and `fingerprint` stay those of the extraction. New warnings: `pii_language_unsupported` (N8), `pii_entities_truncated`.
- **N4, request checks** (`extract_api.check_pii_request`, also for files and batches). `pii` while the service has no PII (`PII_ENABLED` and `JOBS_ENABLED`) is 400 `invalid_request` ("PII handling is not enabled on this service."). `pii` with `screenshot: true` is 422 `invalid_request` ("A screenshot cannot be masked; ask for it without pii."). `/v1/screenshot` has no `pii`.
- **N5, which fields** (`pii/fields.py`). Processed: `title`, `content`, `metadata.title`, `metadata.description`, `metadata.authors[i]`, `metadata.keywords[i]`, `metadata.site_name`, `metadata.images[i].alt`, every string leaf of `metadata.lrmi` (field name `metadata.lrmi.<dotted path>`), `links[i].text`, `links[i].url` when it is `mailto:` or `tel:`, `media.uploader`, `document.file_name`. Unchanged: URLs (`url`, `final_url`, `canonical_url`, other link and image URLs), dates, language codes, licences, `compliance`, `quality`, `fingerprint`. `metadata.authors[i]` and `media.uploader` are a person as a whole when `PERSON` is asked for (the whole string is one `PERSON` span, without NER); the other fields go through the child. `content` in `format: html` is processed as its text nodes and the `alt` and `title` attributes (N10).
- **N6, recognizers** (`pii/recognizers.py`, child only). One `AnalyzerEngine` for `de` and `en`:
  - NER: Presidio's `SpacyRecognizer` with the mapping `PER`, `PERSON` → `PERSON`; `LOC`, `GPE` → `LOCATION`; `ORG` → `ORGANIZATION`; all other labels ignored (the German models have `MISC`, the English ones dates, money and more).
  - Patterns per language: `EmailRecognizer`, `PhoneRecognizer` (regions `DE`, `AT`, `CH`, `GB`, `US`; German context words `telefon`, `tel`, `telefonisch`, `fon`, `mobil`, `handy`, `fax`, `rufnummer`, `erreichbar`), `IbanRecognizer`, `CreditCardRecognizer`, `IpRecognizer`, `UrlRecognizer`.
  - Our `HonorificRecognizer`: a name after `Herr`, `Frau`, `Hr.`, `Fr.` (de) or `Mr`, `Mrs`, `Ms`, `Mx`, `Dr` with optional dot (en), with optional `Dr.`/`Prof.` between, is a `PERSON` of score 0.7; the span is the name only (`Herr Müller` → `Herr [PERSON]`).
  - tldextract never goes online: the child points Presidio's e-mail recognizer at `tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)` (the bundled suffix list). Without this the spike's first e-mail tried `publicsuffix.org` and logged a proxy error; in a cluster it would wait for its timeout.
  - Presidio's own loggers at `WARNING`; `log_decision_process` off.
- **N7, spans** (`pii/spans.py`, pure). The child returns spans `(start, end, type, score)` at or above the threshold for the asked types. Overlaps: a span inside another of equal or higher score is dropped; for a partial overlap the lower-score span is cut to its part outside the higher one, and dropped when that part has no letter or digit (the spike's NER span "IBAN DE89 3704" against the IBAN `DE89 3704 0044 0532 0130 00` keeps the IBAN whole). Long text is analysed in segments of at most 20,000 characters cut at a blank line, else a line break, else a space, with the offsets added back: in the spike the same 175,000 characters took 11.3 s in one piece and 3.8 s in segments (Presidio's context step grows faster than linear).
- **N8, language.** `auto` takes the result's `language`: `de` and `en` use their model; any other or none uses `PII_FALLBACK_LANGUAGE` (`en`) and adds the warning `pii_language_unsupported` (the patterns still work in any language; names are found less well). `de` or `en` in the option forces the model.
- **N9, cache and fingerprints.** PII runs after the cache and the fingerprint: a cache hit goes through the nlp hop like a fresh result; the cache stores the extraction, not the masked result, as it does for a request without `pii` (`cache: false` keeps the extraction out of the cache). Job results are the processed envelope.
- **N10, HTML** (`pii/html.py`). `HtmlText.parse(html)` (lxml fragment, as `documents.formats` builds it) collects the text nodes and the `alt` and `title` attribute values in document order into one string, a space between two nodes that are not adjacent inline text, with a map back to each node. `HtmlText.render(replacements)` writes each replacement into the node where its span starts and deletes the span's rest from the following nodes; markup and attributes other than `alt`/`title` (and `href` of `mailto:`/`tel:`, masked as N5's links) stay. lxml serialises, so a replacement is escaped.
- **N11, the hop** (`pii_dispatch.py`, as `docling_dispatch.py`). Pool `nlp`, queue `nlp_queue(valkey, settings)` without the per-domain lease cap (nothing is fetched); items `<job id>:<index>:<attempts>`; lease `pii_lease_seconds` = `2 * PII_TIMEOUT_SECONDS + 10`. The draft is the unprocessed envelope (`JobStore.put_draft`). No input bytes.
  - Sync (`PiiHandoff`, the `pii_handoff` of `extract_api._flow` and `file_api._flow`): once the flow has its envelope (fast, from a browser or Docling job, or an OCR or browser fallback), an envelope that is not processed yet (`data.pii is None`) and has something N5 processes goes to `nlp`: a one-item job of kind `extract` or `file` (or the request's P1 job, as `DoclingHandoff._on_p1_job`) with the envelope as draft. D4 (a): `pii` without `wait_seconds` sets `wait_seconds` to `PII_SYNC_WAIT_SECONDS` (as M9 does for `transcribe`); the route answers 202 with the nlp job once that passes. An envelope with nothing to process (a failure without content and metadata) answers as it is, with `pii` reporting zero entities.
  - Waits for other pools: a sync request with `pii` that waits for a browser or Docling job gets `PII_SYNC_WAIT_SECONDS` more on its `result_within` and `total_within`, because that worker's result moves on to `nlp` in the same job (below) before the job completes.
  - Moves (`PiiMove`, called by `Worker._finish` of every pool except `nlp`): when the job's options have `pii` and the envelope is not processed, store it as the item's draft and queue the item on `nlp` (same job, index and client), then complete it here without a result (outcome `escalated`). Draft, then queue, then completion: a crash in between reruns the item, which moves again. The ASR worker's transcripts and the Docling worker's OCR results take this path too.
  - Back-pressure: while Valkey's memory guard is high nothing is stored: the item or the request answers `failed`/`pii_failed`, retryable ("PII handling is busy; retry later."). Valkey failing: `pii_failed`, retryable, for a request too (never the unprocessed envelope).
  - `text_extraction_nlp_dispatch_total{outcome}` (`PII_OUTCOMES`): `answered`, `not_claimed`, `no_result`, `handed_over`, `backend_error`, `memory_guard`.
- **N12, fail closed** (`pii.fields.fail_closed(envelope, pii) -> Envelope`). When `pii` is set and the envelope is not processed but has content or N5 fields, it returns `failed`/`pii_failed` (retryable, "PII handling did not run; the content is withheld.") with `content`, `title`, `metadata`, `links`, `media.uploader`, `document.file_name` set to null and the other fields kept (status of the target, compliance, timings). It runs in `extract_api.respond`, the P1 result store, `file_api`, and `Worker._finish` of every pool (after `PiiMove` declined or failed, so a pool that forgot the move still withholds); an item of pool `nlp` that is exhausted or whose draft expired answers `pii_failed`, retryable, never its draft. `ErrorCode.PII_FAILED = "pii_failed"`; too much text (over `PII_MAX_CHARS` in all fields) is `failed`/`too_large` with content withheld; the child's deadline is `pii_failed`, retryable.
- **N13, the nlp worker** (`nlp_worker.py`, entry `text-extraction-nlp-worker`, pool `nlp`, internal port only). The package-3 `Worker` with a `PiiRunner`. `PII_CONCURRENCY` (1) slot loops, each with its child that loads the configured models once (`spacy.load(<PII_MODELS_PATH>/<model dir>)` through Presidio's `NlpEngineProvider`). Per item: read the draft (expired: `pii_failed`, retryable), collect N5's fields, choose the language (N8), call the child with the fields, the types, the threshold and the deadline (`PII_TIMEOUT_SECONDS`, 300), resolve the spans (N7), apply the mode (N2, N10) and build the report (N3, `timings.pii_ms`). `--download-models` (N14), `--check` (load both models, analyse a built-in German and English sentence, expect a `PERSON` and an `EMAIL_ADDRESS` in each; exit 1 otherwise). `/ready` after the models are present and the children started. `text_extraction_pii_entities_total{type}` counts found entities by type.
- **N14, models on the PVC** (`pii/models.toml`, `model_store.py`). The manifest names the four wheels (`de_core_news_lg`, `en_core_web_lg`, `de_core_news_md`, `en_core_web_md`, 3.8.0) by their GitHub release URL, size and SHA-256 (Task 1 records them). A manifest entry gains `unpack = "<member prefix>"`: the downloaded zip is checked as today, then the members under that prefix (`de_core_news_lg/de_core_news_lg-3.8.0/`) are extracted into the entry's `path` (`de_core_news_lg/`), refusing absolute paths, `..` and links; the zip is not kept. `missing_models` checks an unpacked entry by its `config.cfg` and `meta.json`. Only the models of `PII_MODEL_SIZE` are downloaded (`Manifest.only`, one top-level directory per model). Helm: init container with the PVC writable, the worker read-only, `downloadMode: init|job`, `existingClaim` (as M13/M17). Compose: one-shot `nlp-models` on the volume `nlp-models`.
- **N15, switches.** `PII_ENABLED` (default false, as D3 of package 5): Helm renders it into every pod from `workers.nlp.enabled` (default false); compose profile `nlp` with `PII_ENABLED=true` in `.env`. Start-up refuses `PII_ENABLED` without `JOBS_ENABLED`.

## Review Focus

1. **Unprocessed text slipping out:** an OCR fallback draft (`ocr_unavailable`), a browser fallback (`browser_unavailable`), a cache hit, an exhausted nlp item, an expired draft, a P1 job whose task raised, a batch item whose nlp move hit the memory guard. Each answers either the processed result or `pii_failed` without content; never the draft. Owner: Tasks 3 and 6 (one test per path).
2. **Overlaps and borders:** an e-mail that also matches `URL` twice inside it, an NER span that eats the word before an IBAN, a name across two HTML text nodes (`<b>Anna</b> Schmidt`), a name across a 20,000-character segment border, an entity at offset 0 and at the end, emoji and combining characters before an entity (code-point offsets). Clean replacements, no doubled or shifted text. Owner: Tasks 2 and 3.
3. **German specifics:** `Herr Müller`, `Frau Dr. Schmidt`, double names (`Anna-Lena Meyer-Schulz`), phone numbers in German notation (`0421 361-12345`, `+49 (0)30 1234567`), IBAN with and without spaces, capitalised nouns that are no names (`Energiewende`, `Photosynthese`). Found, or left alone, as the corpus says. Owner: Tasks 1 and 4.
4. **Huge or odd input:** a 2-million-character PDF text, a page that is one line without spaces, a result with 5,000 links, an empty content with metadata only, text in French. `too_large` or a result within `PII_TIMEOUT_SECONDS`; the child is killed at its deadline and the next item runs; French gets `pii_language_unsupported`. Owner: Tasks 4 and 5.
5. **No nlp worker:** scaled to zero, models missing, crashed child. Sync requests answer 202 after 20 s (D4 a) and the job waits; the backlog alert fires; `/ready` stays false while models are missing; a crashed child is restarted and its item runs again. Owner: Tasks 5, 6 and 7.

---

### Task 1: Corpus, calibration and model pins

Runs in the sandbox: GitHub releases and PyPI are reachable from it (checked on 2026-10-26).

**Files:**
- Create: `tests/pii/corpus/de_*.md` and `en_*.md` (at least 6 German and 4 English texts of 150-600 words, written for the purpose: a school contact page, a teacher's worksheet credits, a press release, a forum post, a CV-like bio, a transcript snippet; no real persons' data), `tests/pii/corpus/gold.json` (per file: spans with `type`, `start`, `end`, and `keep`: words that must not be found, e.g. `Energiewende`, `Bremen` under D2 a), `tests/pii/corpus/SOURCES.md`, `docs/superpowers/plans/2026-10-26-package-7-calibration.md` (results)
- Modify: none

- [ ] **Step 1:** in a scratch venv: `presidio-analyzer==2.2.364`, `spacy==3.8.16`, the four model wheels; record each wheel's URL, size and SHA-256 (sandbox values of 2026-10-26, to confirm: `de_core_news_lg` 567,843,151 B `36fda650e476b54d5e87803635e36dadd1e8e034c4b5962088586d684f4c9fed`, `en_core_web_lg` 400,658,291 B `293e9547a655b25499198ab15a525b05b9407a75f10255e405e8c3854329ab63`, `de_core_news_md` 44,398,316 B `b903f59220f1e76dd672acdaa7fa454d6703fe056c5ccd6457820e70874116d0`, `en_core_web_md` 33,480,380 B `5e6329fe3fecedb1d1a02c3ea2172ee0fede6cea6e4aefb6a02d832dba78a310`).
- [ ] **Step 2:** run the N6 recognizers over the corpus for `md` and `lg`: per type precision and recall against `gold.json`, the `keep` words found, time and peak RSS; thresholds 0.35, 0.4, 0.5. Choose `PII_SCORE_THRESHOLD` (expected 0.4: Presidio's phone score without context is 0.4, the spike's URL fragments inside e-mails 0.5 are resolved by N7) and confirm the German context words.
- [ ] **Step 3:** write the results file with the chosen threshold and the per-type numbers that Task 8's done-when test pins. If `lg` recall on German `PERSON` is below 0.85, stop and bring Jan the numbers before Task 4. Commit `docs: package 7 calibration and PII corpus`.

### Task 2: Options, report and spans

**Files:**
- Create: `src/text_extraction/pii/__init__.py`, `src/text_extraction/pii/types.py`, `src/text_extraction/pii/spans.py`, `src/text_extraction/pii/replace.py`
- Modify: `src/text_extraction/settings.py`, `src/text_extraction/extract_models.py`, `src/text_extraction/job_models.py`, `src/text_extraction/file_models.py`, `src/text_extraction/pool_dispatch.py` (`job_options`), `src/text_extraction/envelope.py` (`PII_FAILED`), `tests/test_errors.py`, `docs/configuration.md` (section "PII"), `.env.example`
- Test: `tests/pii/test_types.py`, `tests/pii/test_spans.py`, `tests/pii/test_replace.py`, `tests/test_settings.py` (extend)

**Interfaces:**
- Produces: settings `pii_enabled` (False), `pii_default_entities` (D2 list), `pii_score_threshold` (Task 1's value, 0-1), `pii_fallback_language` (`"en"`), `pii_sync_wait_seconds` (20, 1-60), `pii_timeout_seconds` (300, 10-3600), `pii_max_chars` (2_000_000, ≥ 10_000), `pii_max_entities` (1000, 1-100_000), `pii_concurrency` (1, ≥ 1), `pii_memory_limit_mb` (3072, ≥ 512), `pii_models_path` (`/models`), `pii_model_size` (`"lg" | "md"`), `pii_hash_key` (SecretStr | None, at least 32 bytes when set).
- Produces: `EntityType` (N1's nine names), `PiiMode`, `PiiOptions` (N1, with the bare-string form), `PiiEntity`, `PiiReport` (N3), `Span(start: int, end: int, type: EntityType, score: float)` (frozen dataclass); `ExtractResult.pii: PiiReport | None = None`; `Timings.pii_ms: int = 0`; `pii` on `ExtractRequest`, `BatchRequest`, `JobOptions`, `FileForm`.
- Produces: `resolve(spans: Iterable[Span], text: str) -> list[Span]` (N7, sorted, non-overlapping); `segments(text: str, max_chars: int = 20_000) -> list[tuple[int, str]]` (offset, piece); `Replacer(mode: PiiMode, key: bytes)` with `placeholder(span: Span, value: str) -> str` and `apply(text: str, spans: Sequence[Span]) -> str`; `hash_key(settings) -> tuple[bytes, Literal["service", "result"]]` (D3).

- [ ] **Step 1: Failing tests:** `test_cached_result_without_pii_still_parses` (a package-6 `ExtractResult` JSON validates, `pii is None`, `timings.pii_ms == 0`), `test_pii_bare_string_is_mode`, `test_pii_entities_unknown_or_duplicate_rejected`, `test_job_options_omit_pii_when_unset`, `test_pii_hash_key_not_in_repr_and_min_length`; `test_contained_span_dropped`, `test_partial_overlap_cut_to_outside_part` (IBAN case of N7), `test_cut_without_letter_or_digit_dropped`, `test_segments_cut_at_blank_line_then_line_then_space` and `test_segments_offsets_reassemble_text` (concatenating the pieces at their offsets gives the input, also for a text without any space); `test_mask_redact_hash_placeholders` (`[PERSON]`, `[REDACTED]`, `[PERSON:` + 12 hex + `]`), `test_hash_equal_values_equal_across_case_and_spaces`, `test_hash_differs_by_key`, `test_apply_keeps_text_between_spans_and_code_point_offsets` (an emoji before the span).
- [ ] **Step 2:** run, expect FAIL. **Step 3:** implement, with the `docs/configuration.md` rows (secret marked) and `.env.example`, and `pii_failed` in `docs/architecture.md`'s code list. **Step 4:** PASS, `uv run mypy` clean.
- [ ] **Step 5:** commit `feat: add PII options, report, spans and replacement`.

### Task 3: Result fields, HTML and the fail-closed guard

**Files:**
- Create: `src/text_extraction/pii/fields.py`, `src/text_extraction/pii/html.py`
- Test: `tests/pii/test_fields.py`, `tests/pii/test_html.py`

**Interfaces:**
- Consumes: Task 2's types, `resolve`, `Replacer`.
- Produces: `TextField(name: str, text: str, whole: EntityType | None)`; `text_fields(result: ExtractResult, entities: Collection[EntityType]) -> list[TextField]` (N5; for `format: html` the content field's text is `HtmlText.parse(content).text`); `apply_pii(result: ExtractResult, options: PiiOptions, spans: Mapping[str, list[Span]], *, language: Literal["de", "en"], key: bytes, hash_scope: Literal["service", "result"], max_entities: int) -> tuple[ExtractResult, list[str]]` (the processed result with `pii` set, and the warnings to add); `needs_pii(envelope: Envelope[ExtractResult], options: PiiOptions | None) -> bool` (set, unprocessed, and has a N5 field); `fail_closed(envelope: Envelope[ExtractResult], options: PiiOptions | None) -> Envelope[ExtractResult]` (N12); `HtmlText.parse(html: str) -> HtmlText`, `.text: str`, `.render(replacements: Sequence[tuple[Span, str]]) -> str` (N10).

- [ ] **Step 1: Failing tests:** `test_fields_cover_n5_and_skip_urls_dates_licences` (a full result fixture: exactly N5's names, no `canonical_url`, no image URL, a `mailto:` link's URL in, an `https:` link's URL out), `test_authors_and_uploader_whole_person`, `test_authors_untouched_without_person`, `test_lrmi_string_leaves_dotted_names`, `test_apply_mask_on_content_title_metadata_links`, `test_annotate_items_offsets_and_truncation` (1,001 spans with `max_entities` 1,000: 1,000 items, counts 1,001, warning `pii_entities_truncated`), `test_apply_keeps_status_quality_fingerprint_word_count`, `test_html_name_across_inline_nodes` (`<p><b>Anna</b> Schmidt kam.</p>` → `<p><b>[PERSON]</b> kam.</p>`), `test_html_alt_and_title_attributes_masked_href_kept`, `test_html_replacement_escaped`, `test_html_block_nodes_get_a_space` (`<p>Anna</p><p>Schmidt</p>` is two words, not `AnnaSchmidt`), `test_needs_pii_false_for_processed_or_empty_failure`, `test_fail_closed_withholds_content_and_metadata` (and keeps `upstream_status`, `compliance`, `timings`), `test_fail_closed_passes_processed_and_no_pii`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS, mypy clean.
- [ ] **Step 5:** commit `feat: apply PII to result fields and HTML, withhold unprocessed results`.

### Task 4: Recognizers, the child and the models

**Files:**
- Create: `src/text_extraction/pii/recognizers.py`, `src/text_extraction/pii/child.py`, `src/text_extraction/pii/models.toml`, `src/text_extraction/pii/check_de.txt` and `check_en.txt` (one sentence each with a name and an e-mail)
- Modify: `pyproject.toml` (extra `nlp`: `presidio-analyzer==2.2.364`, `spacy==3.8.16`; `exclude-newer-package` for too-new transitive releases; mypy overrides `presidio_analyzer.*`, `spacy.*`, `tldextract.*`; marker `nlp`), `uv.lock`, `src/text_extraction/model_store.py` (`unpack`, manifest `nlp`), `NOTICE` (spaCy model licences and their data sources)
- Test: `tests/pii/test_child.py` (default suite: the child protocol with a fake engine; marker `nlp`: real models), `tests/test_model_store.py` (extend)

**Interfaces:**
- Consumes: Task 2's `Span`, `segments`, `EntityType`.
- Produces: `build_analyzer(models: Mapping[Literal["de", "en"], Path]) -> AnalyzerEngine` (N6; child only); child functions `load(models_path: str, size: str) -> None` and `analyze(fields: list[tuple[str, str]], language: str, entities: list[str], threshold: float) -> dict[str, list[tuple[int, int, str, float]]]` (per field name, segments as N7, raw spans before `resolve`); manifest `nlp` loadable by `load_manifest("nlp")`; `ManifestFile.unpack: str | None`.

- [ ] **Step 1: Failing tests:** `test_unpack_extracts_member_prefix_only`, `test_unpack_refuses_traversal_absolute_and_links`, `test_missing_models_checks_unpacked_config` (model store, with a small zip fixture and `allow_http`); `test_parent_does_not_import_presidio_or_spacy` (import `nlp_worker` and `pii.*` except `child`/`recognizers` in a subprocess, check `sys.modules`); marked `nlp`: `test_german_persons_found` (`Dr. Anna Schmidt`, `Herr Müller` → `Herr [PERSON]` span on `Müller`, `Anna-Lena Meyer-Schulz`), `test_german_phone_iban_email`, `test_english_person_email_phone_card`, `test_capitalised_nouns_not_persons` (`Energiewende`, `Photosynthese`), `test_no_network_for_email` (sockets refused by a patched `socket.socket.connect` in the child, an e-mail still found), `test_segment_border_entity_found`.
- [ ] **Step 2:** FAIL. **Step 3:** implement; `uv lock` with the 14-day rule; the licence gate over the extra `nlp` passes. **Step 4:** PASS in the default suite; `uv run pytest -m nlp` PASS with the models downloaded by `--download-models` into a temp dir.
- [ ] **Step 5:** commit `feat: add Presidio recognizers, the PII child and the spaCy model manifest`.

### Task 5: The nlp worker

**Files:**
- Create: `src/text_extraction/nlp_worker.py`
- Modify: `pyproject.toml` (script `text-extraction-nlp-worker`), `src/text_extraction/jobs.py` (`pii_lease_seconds`), `src/text_extraction/queue_metrics.py` (pool `nlp`)
- Test: `tests/test_nlp_worker.py`

**Interfaces:**
- Consumes: Tasks 2-4; `Worker`, `ItemRunner`, `DocumentPool`, `JobStore.draft`, `missing_models`.
- Produces: `PiiRunner(settings, store, pool)` with `lease_seconds` and `run(job, item, url, *, gate_refusals) -> Envelope[ExtractResult]`; `build_nlp_worker(settings, valkey, registry) -> WorkerParts`; `main()` with `--download-models` and `--check`; metric `text_extraction_pii_entities_total{type}`.

- [ ] **Step 1: Failing tests** (fake child engine, fakeredis): `test_runner_masks_draft_and_reports`, `test_runner_draft_expired_is_pii_failed_retryable`, `test_runner_child_deadline_is_pii_failed_and_child_restarted`, `test_runner_too_much_text_is_too_large_withheld`, `test_runner_language_fallback_warning` (draft language `fr`), `test_exhausted_nlp_item_never_answers_draft`, `test_nlp_worker_drops_draft_after_result`, `test_ready_false_while_models_missing`, `test_check_exit_code` (fake engine finds nothing → 1).
- [ ] **Step 2:** FAIL. **Step 3:** implement (the `Worker` changes for pool `nlp` in `_fail_exhausted` and `_drop_input` sit here). **Step 4:** PASS; mypy clean.
- [ ] **Step 5:** commit `feat: add the nlp worker`.

### Task 6: The hop from requests, workers and batches

**Files:**
- Create: `src/text_extraction/pii_dispatch.py`
- Modify: `src/text_extraction/extract_api.py` (`check_pii_request`, `pii_handoff`, `wait_seconds` default, longer pool waits, `fail_closed` in `respond` and the P1 store), `src/text_extraction/file_api.py`, `src/text_extraction/jobs_api.py` (batch check), `src/text_extraction/worker.py` (`_finish` moves via `PiiMove`; docstring), `src/text_extraction/browser_worker.py`, `src/text_extraction/docling_worker.py`, `src/text_extraction/asr_worker.py` (wire `PiiMove`), `src/text_extraction/app.py` (dispatcher in state), `src/text_extraction/settings.py` (start-up check N15)
- Test: `tests/test_pii_dispatch.py`, `tests/test_extract_api.py`, `tests/test_file_api.py`, `tests/test_jobs_api.py`, `tests/test_worker.py` (extend)

**Interfaces:**
- Consumes: Tasks 2, 3, 5; `PoolDispatcher`, `Handover`, `JobStore.put_draft`, `FairQueue.enqueue_items`.
- Produces: `PII_POOL = "nlp"`, `PII_OUTCOMES`, `nlp_queue(valkey, settings) -> FairQueue`, `PiiDispatcher(PoolDispatcher)` with `submit(envelope, kind, options, principal, url, job_id=None) -> str` and `wait(...)`, `PiiHandoff.__call__(envelope, handover) -> Envelope[ExtractResult] | None`, `PiiMove.__call__(job, item, envelope) -> bool` (True when moved), `check_pii_request(pii: PiiOptions | None, req_format: OutputFormat, screenshot: bool, settings: Settings) -> None`.

- [ ] **Step 1: Failing tests:** `test_pii_disabled_is_400` (extract, file, batch), `test_pii_with_screenshot_is_422`, `test_annotate_with_html_is_422`; with a fake nlp worker loop on fakeredis: `test_sync_fast_result_goes_through_nlp`, `test_sync_cache_hit_goes_through_nlp`, `test_sync_slow_pii_answers_202_after_wait` (D4 a), `test_sync_memory_guard_is_pii_failed_without_content`, `test_sync_valkey_down_is_pii_failed_not_draft`, `test_ocr_fallback_draft_is_processed_or_withheld`, `test_browser_fallback_is_processed_or_withheld`, `test_p1_job_result_is_processed`, `test_p1_task_raised_is_withheld`, `test_worker_finish_moves_to_nlp_once` (a crash after the queue reruns and moves again; only the first result counts), `test_batch_items_processed_by_nlp`, `test_upload_with_pii_form_field_bare_and_json`, `test_request_without_pii_unchanged` (byte-equal answer to the package-6 golden fixture apart from `pii: null`, `pii_ms: 0`).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS; the whole default suite, `uv run mypy`, `uv run ruff check .`.
- [ ] **Step 5:** commit `feat: send results with pii through the nlp pool and fail closed`.

### Task 7: Image, compose, Helm and CI

**Files:**
- Modify: `Dockerfile` (target `nlp`, image `text-extraction-nlp`: venv with extra `nlp`, non-root, tini, `CMD text-extraction-nlp-worker`, no models), `docker-compose.yml` (services `nlp-models`, `nlp-worker` under profile `nlp`, volume `nlp-models`, `PII_ENABLED` from env), `deploy/helm/text-extraction/values.yaml` and templates (`nlp-pvc.yaml`; the nlp worker in `worker-deployment.yaml`, ScaledObject/HPA, internal service, `prometheusrule.yaml` backlog alert for queue `nlp`, `PII_ENABLED` in every pod, `PII_HASH_KEY` from a secret), chart `README.md`, `deploy/helm/test.sh`, `.github/workflows/ci.yml` (job `nlp`: `uv sync --extra nlp`, model cache by manifest hash, `--download-models` of `md` on every push and `lg` on the weekly schedule, `pytest -m nlp`; helm lint with nlp on; licence gate over `nlp`), `.github/workflows/image.yml` (variant `nlp`: build, `--check` with the `md` models, "API image has no presidio_analyzer, spacy, thinc", size printed)
- Test: `deploy/helm/test.sh` cases

**Interfaces:**
- Produces: Helm values `workers.nlp.{enabled: false, replicaCount: 1, concurrency: 1, modelSize: lg, image, resources (requests 1 CPU/2Gi, limits 2 CPU/3Gi; placeholders until package 11), terminationGracePeriodSeconds: 30, autoscaling.keda|hpa, hashKey.existingSecret}`, `workers.nlp.models.{download: true, downloadMode: init, persistence: {size: 2Gi, accessModes: [ReadWriteMany], storageClass: "", existingClaim: ""}}`.

- [ ] **Step 1: Failing tests:** helm cases `nlp off renders nothing and PII_ENABLED=false`, `nlp on renders deployment, PVC, init container writable and worker read-only, PII_ENABLED=true in every pod`, `modelSize md sets PII_MODEL_SIZE`, `downloadMode job renders a hook Job`, `existingClaim skips the PVC`, `hashKey secret renders PII_HASH_KEY from it`; licence gate over `nlp` passes.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `bash deploy/helm/test.sh`, the helm lint matrix and `docker compose --profile nlp config` pass; the image builds in GitHub Actions and `--check` passes there.
- [ ] **Step 5:** commit `feat: ship the nlp worker image, compose profile and Helm toggle`.

### Task 8: Done-when and docs

**Files:**
- Create: `tests/pii/test_corpus.py` (marker `nlp`: every corpus text through `POST /v1/extract/file` as Markdown and as HTML with each mode and a real nlp worker loop; per mode the expected form (`[TYPE]`, `[REDACTED]`, hashes equal within a result, annotate offsets point at the gold text); recall per type at least Task 1's pinned numbers, pattern types 1.0; no `keep` word touched)
- Modify: `docs/architecture.md` (new section "PII" with N1-N15 and D1-D4 as decided; Overview, pipeline step 6, `/v1/extract` row, warnings and error codes, "Known limits": names in other languages, PII in screenshots), `docs/packages.md` (status and note), `README.md` (examples: `pii: "mask"`, annotate, a batch with `pii`), `docs/configuration.md` (Helm values)

- [ ] **Step 1:** corpus test → PASS with `-m nlp` (CI job `nlp`, `md` and weekly `lg`; locally with `lg`).
- [ ] **Step 2:** `docker compose --profile nlp up` (in the sandbox if the image builds there from the full Python base, else on Jan's PC through Remote Control in a fresh folder `C:\Users\jan\te-pkg7-verify`): a German and an English page and an upload through each mode, the nlp child's peak memory, the image size. Done when the German and English test texts are handled per mode as the corpus test says.
- [ ] **Step 3:** label the PR `live`: `wlo_200` auto must stay ≥ 80 % (no request in the set has `pii`, so this checks that nothing else changed).
- [ ] **Step 4:** docs; commit `test: add the PII corpus test and document the package`.

### Final: review and hand-over

- [ ] `requesting-code-review` over the whole branch (Review Focus above as the brief), fixes via `receiving-code-review`.
- [ ] `verification-before-completion`: `uv run pytest`, `uv run pytest -m nlp` (CI job `nlp`), `uv run pytest -m browser` (CI), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, helm lint matrix, image workflow green, live result reported; done-when met.
- [ ] `finishing-a-development-branch`: PR, squash merge once green (Jan's standing permission), progress memory updated.
