# Package 20: Bot-Challenge Refusals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A 403, 429 or 503 answer that is a bot challenge (Cloudflare's managed challenge and
similar) is reported as `bot_protection` instead of `target_http_error`, in fast and in browser
mode, and the playground explains what can be done about it.

**Architecture:** One pure detector in `quality.py` looks at the answer's headers
(`cf-mitigated: challenge`) and the head of its body (challenge markup and phrases). The pipeline
calls it where a refused fast answer or a refused rendered document becomes an error. Escalation
in unblock mode treats a challenge refusal like any other refusal. The playground adds a hint to
the `bot_protection` error and tells an admin when the unblock browser is off on the server.

**Tech Stack:** Python 3.13, FastAPI, Jinja2 templates, pytest.

**Spec:** Jan's decision of 2026-11-09 (option "Test, dann Paket 20") on the diagnosis in
`/mnt/project-files/leifiphysik-403/plan-leifiphysik-403.md` (project files): leifiphysik.de sits
behind Cloudflare, which answers `HeadlessChrome` and curl with 403, `Cf-Mitigated: challenge`
and a "Just a moment..." page; unblock mode was off (`BROWSER_UNBLOCK_ENABLED=false`).

## Global Constraints

- CLAUDE.md rule 6 stays: the standard browser keeps its honest user agent (`HeadlessChrome` plus
  `text-extraction/<version>`); nothing here changes what is sent to a site.
- CLAUDE.md rule 4: the API still answers 200 with `status: failed`; `upstream_status` keeps the
  site's status (403, 429 or 503); `retryable` keeps today's rule (429, 503 and 5xx retryable).
- Outside unblock mode a challenge refusal never escalates to the browser (as today).
- English is the source; every UI and user-guide text change has its German partner.
- Version 0.1.9 (pyproject, Helm chart, compose defaults, README, docs), CHANGELOG entry.

## Review Focus

1. A 403 with `Cf-Mitigated: challenge` but a non-HTML or empty body: still `bot_protection`
   (the header alone is enough). Pinned in Task 1 and Task 2.
2. A plain 403 "Forbidden" page without challenge signals: stays `target_http_error`. Task 2.
3. A 404 or 500 whose body mentions "captcha": stays `target_http_error` (only 403, 429, 503
   count). Task 2.
4. Unblock `auto` with a challenge refusal still escalates to the unblock browser. Task 2.
5. A huge refused body: the detector reads only its first 64 KiB. Task 1.

---

### Task 1: The detector

**Files:**
- Modify: `src/text_extraction/quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Produces: `is_challenge_answer(headers: Mapping[str, str], body: bytes | str) -> bool` and
  `CHALLENGE_SCAN_BYTES = 65536` in `text_extraction.quality`.

- [ ] **Step 1: Write the failing tests** `test_cf_mitigated_header_is_a_challenge` (header
  `cf-mitigated: challenge` in any letter case, empty body -> True; `cf-mitigated: block` -> False),
  `test_challenge_markup_or_phrase_is_a_challenge` (the Cloudflare "Just a moment..." title,
  `cf-chl` markup, "Checking your browser" -> True; a plain "Forbidden" page -> False),
  `test_only_the_head_of_the_body_is_read` (challenge markup after 64 KiB of padding -> False).
- [ ] **Step 2: Run** `uv run pytest tests/test_quality.py -k challenge -v`: FAIL (name missing).
- [ ] **Step 3: Implement** in `quality.py`: header names compared case-insensitively; the body
  head decoded as UTF-8 with `errors="replace"`, case-folded; true for `_CHALLENGE_MARKUP`, any of
  `_CHALLENGE_PHRASES`, or a `<title>` that starts with "just a moment".
- [ ] **Step 4: Run** the same command: PASS.
- [ ] **Step 5: Commit** `feat(quality): recognise a bot-challenge answer`.

### Task 2: The pipeline and escalation

**Files:**
- Modify: `src/text_extraction/pipeline.py` (the fast refusal near `_fetch_and_extract`'s
  status check, `_extract_rendered`'s status check), `src/text_extraction/escalation.py`
  (`_is_refusal`)
- Test: `tests/test_pipeline_render.py`, `tests/test_escalation.py`

**Interfaces:**
- Consumes: `is_challenge_answer` (Task 1), `REFUSAL_STATUSES` (escalation).
- Produces: `bot_protection` envelopes with message
  `"The target answered HTTP {status} with a bot challenge (Cloudflare)."` when the answer has a
  `cf-mitigated` header or `server: cloudflare`, else
  `"The target answered HTTP {status} with a bot challenge."`.

- [ ] **Step 1: Write the failing tests** in `tests/test_pipeline_render.py`:
  `test_fast_challenge_refusal_is_bot_protection` (parametrised 403/429/503; CHALLENGE_PAGE, and
  `Forbidden` with header `cf-mitigated: challenge`; `code == BOT_PROTECTION`,
  `upstream_status == status`, `retryable == (status != 403)`, `mode_used == "fast"`, no render in
  standard `auto`); `test_fast_plain_refusal_stays_target_http_error` (FORBIDDEN_PAGE 403; a 404
  with "captcha" in its body); `test_rendered_challenge_refusal_is_bot_protection` (browser mode,
  rendered CHALLENGE_PAGE with response 403 and header `cf-mitigated: challenge`, message names
  Cloudflare); `test_unblock_auto_escalates_a_challenge_refusal` (fast 403 challenge, unblock
  renderer renders ARTICLE -> `success`, `unblocked`). Update
  `test_challenge_answer_is_assessed_only_under_unblock` to expect `BOT_PROTECTION`. In
  `tests/test_escalation.py`: a `bot_protection` envelope with `upstream_status` 403 and quality
  set is a refusal under unblock.
- [ ] **Step 2: Run** `uv run pytest tests/test_pipeline_render.py tests/test_escalation.py -v`:
  the new tests FAIL.
- [ ] **Step 3: Implement**: both status checks call `is_challenge_answer` for a status in
  `REFUSAL_STATUSES` and fail with `ErrorCode.BOT_PROTECTION` and the message above; the retryable
  rule and the refusal assessment (`_assesses_refusal`, `_refusal_quality`) stay as they are.
  `_is_refusal` accepts `TARGET_HTTP_ERROR` and `BOT_PROTECTION`.
- [ ] **Step 4: Run** the whole suite `uv run pytest -q`: PASS.
- [ ] **Step 5: Commit** `feat(pipeline): report a challenge refusal as bot_protection`.

### Task 3: The playground

**Files:**
- Modify: `src/text_extraction/ui/forms.py` (`AccessOffer.unblock_off: bool = False`, set in
  `access_offer` for an admin while `BROWSER_ENABLED` and not `BROWSER_UNBLOCK_ENABLED`),
  `src/text_extraction/ui/templates/forms/access.html`, `src/text_extraction/ui/templates/result.html`,
  `src/text_extraction/ui/locales/en.json`, `de.json`
- Test: `tests/ui/test_access.py`, the result-view tests in `tests/ui/`

**Interfaces:**
- Produces: catalogue keys `hint.access.ignore_unblock_off` and `result.bot_protection_hint`.

- [ ] **Step 1: Write the failing tests**: an admin form with browser on and unblock off shows
  the `ignore_unblock_off` hint naming `BROWSER_UNBLOCK_ENABLED`; with unblock on it shows
  `maximum`; a user never sees `unblock_off`. A result with `error.code == "bot_protection"` shows
  the `result.bot_protection_hint` paragraph; a `target_http_error` result does not.
- [ ] **Step 2: Run** `uv run pytest tests/ui -q`: FAIL.
- [ ] **Step 3: Implement** the field, the template branches and both catalogue texts (EN source,
  DE partner).
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and the i18n checks: PASS.
- [ ] **Step 5: Commit** `feat(ui): explain bot protection and a switched-off unblock browser`.

### Task 4: Docs and version 0.1.9

**Files:** `docs/architecture.md` (B12, B16), the error-code tables of `docs/user-guide/en|de`,
`docs/packages.md` (row 20), `CHANGELOG.md`, version files (as in v0.1.8).

- [ ] **Step 1:** Document: a refusal with a challenge is `bot_protection`; how to get past a
  Cloudflare wall legitimately (allowlist by the operator, or unblock mode for contract partners
  with `BROWSER_UNBLOCK_ENABLED=true`).
- [ ] **Step 2:** Bump to 0.1.9; run `uv run pytest -q`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`, `uv run python scripts/check_translation_pairs.py`.
- [ ] **Step 3: Commit** `docs: bot_protection on challenge refusals; version 0.1.9`.
