# Package 19: optional features on by default, chosen per request in the playground

Requested by Jan on 2026-11-07 (17:04): OCR (Docling), PII handling, the LLM features and
transcription are on by default in `.env`, `.env.example` and docker compose; the playground offers
each of them as an optional choice (not selected by default); env switches let the operator forbid
them. Package 19 builds on PR #50 (ASR on by default in compose, merged as dfc3dc7).

**Goal:** a fresh `docker compose up` runs every worker, and a playground user can pick OCR, PII,
transcription and the LLM features per request without writing JSON.

**Not in scope:** the Helm chart defaults stay off (Jan named env and compose only; in a cluster the
workers need RWX volumes, model downloads and several cores, which the operator switches on). The
service's own defaults in `settings.py` stay off (a bare `uv run` has no workers).

## Decisions

- **P19-D1, how to forbid a feature** (card in the thread, recommended b): (a) the existing
  `DOCLING_ENABLED`, `PII_ENABLED`, `LLM_ENABLED`, `ASR_ENABLED` switch a feature off for everyone;
  (b) also one switch per feature that keeps it for admin keys and refuses it for user keys:
  `OCR_ALLOW_USERS`, `PII_ALLOW_USERS`, `LLM_ALLOW_USERS`, `ASR_ALLOW_USERS` (default true), after
  the pattern of `COMPLIANCE_ALLOW_OVERRIDE`.
- **P19-R1, compose:** `docling-models`, `docling-worker`, `nlp-models`, `nlp-worker` and
  `llm-worker` lose their profiles, as `asr-*` did in #50; `DOCLING_ENABLED`, `PII_ENABLED` and
  `LLM_ENABLED` default to `true` in compose and in `.env.example`. Off: `*_ENABLED=false` in `.env`
  and `--scale <service>=0` for the feature's services. The profiles `docling`, `nlp`, `llm` go away;
  CI, the load workflow and the release compose follow.
- **P19-R2, LLM without a key:** `LLM_ENABLED=true` with no `LLM_API_KEY` is valid; a request that
  needs the LLM then needs `X-LLM-Api-Key` (the playground's key field). The playground says so next
  to the LLM choices, and an LLM request without either key answers a clear 400 (`invalid_request`,
  "The LLM needs a key: set LLM_API_KEY or send X-LLM-Api-Key."), checked before the job starts.
- **P19-R3, playground controls** (default unselected; disabled with a hint when the feature is off
  or not allowed for the signed-in key):
  - Extract and Upload: "OCR" select (`auto` default, `never`, `always`); "Personal data" select
    (off default, `mask`, `redact`, `hash`, `annotate`); Extract: "Transcribe audio and video"
    checkbox (uploads are always transcribed when ASR is on).
  - Structured: "Strategy" select (`auto` default, `selectors`, `llm`, `metadata`) that sets the
    strategy inside the JSON field's object unless the JSON names one.
  - Chunk: "Method" select whose `semantic` entry needs the LLM.
  - Batch options: OCR, Personal data, Transcribe, as for Extract.
  - The "Advanced" JSON field keeps working; a control set by the user and the same key in the JSON
    stay an error ("given twice"), as today.
- **P19-R4, enforcement of P19-D1 (b):** explicit requests of a forbidden feature by a user key
  answer 403 `forbidden` ("<feature> is for admin keys only on this service."): `ocr: always`,
  `pii`, `transcribe: true`, strategy `llm`, `learn_selectors`, `describe_images`, `semantic`
  chunking, an audio or video upload. Implicit uses behave as if the feature were off for that
  request: `ocr: auto` keeps the text layer (warning `ocr_unavailable`), strategy `auto` does not
  escalate to the LLM. The job options carry the allowed set, so workers apply the same rule.
- **P19-R5, te-ui-test:** Jan's `.env` needs the four switches set to `true` (the PC guard blocks
  Claude's edit unless Jan approves in the thread); then `docker compose up -d --wait --build`.

## Tasks

1. Compose, `.env.example`, CI compose job, release compose, load workflow and its test (P19-R1).
2. Settings `*_ALLOW_USERS` with validation and docs (only with D1 b), `effective features` helper
   per principal, checks in `extract_api`, `file_api`, `batch` and the MCP tools (P19-R4).
3. LLM key check before hand-off (P19-R2).
4. Playground controls and their form handling in `ui/forms.py`, templates, `ui.js` state,
   locales en/de, hints (P19-R3).
5. Docs: configuration.md, architecture.md, user guide en/de (installation, usage, parameters),
   README en/de, CHANGELOG.
6. Tests first for each task (TDD): settings, the 403s and implicit fallbacks, form bodies, the
   templates in both languages, compose config in CI; browser tests for the new controls at 1440
   and 390 px.
7. Review, fixes, PR; then te-ui-test with every worker on and Jan's PC check.

## Done when

A fresh `docker compose up -d --wait` from `.env.example` runs every worker; the playground offers
OCR, Personal data, Transcribe, Strategy and Method in both languages, disabled with a hint when a
feature is off or forbidden; with `*_ALLOW_USERS=false` a user key gets 403 for that feature and an
admin key does not; tests, linters and CI are green; Jan's check on his PC passes.
