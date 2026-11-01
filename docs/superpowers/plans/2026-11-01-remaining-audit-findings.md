# Remaining audit findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every finding of the v0.1.0 audit that was parked (ledger: `/mnt/project-files/audit-v0.1.0/findings.md`), so that the next release (`v0.1.1`) carries no known open finding.

**Architecture:** One task per finding or tight group, each test first (TDD), in the module that owns the behaviour. No new component; two new settings (`ALLOWED_HOSTS`, `MCP_MAX_ANSWER_CHARS`) and two new warnings. Pillow 13 waits for the 14-day rule (ruling R16) and comes last.

**Tech Stack:** Python 3.13, FastAPI, pydantic, redis-py 8.1, FastMCP, Pillow, pytest, ruff, mypy.

**Spec:** Jan, 2026-11-01 15:10 UTC: "behebe alle befunde" (after the v0.1.0 audit report). This reverses ruling P10-S6 (MCP cut only `content`).

## Global Constraints

- CLAUDE.md rules: English code and docs, English and German user docs in the same PR, result envelope (rule 4), SSRF guard, ops guide, no GPL, 14-day rule for new dependency versions (R16).
- Every change goes into `CHANGELOG.md` `## [Unreleased]`.
- One branch (`claude/project-thread-qx15nh`), one PR, squash merge.
- A behaviour change visible to callers (status codes, strict types, error codes) is listed under `### Changed` in the changelog and in the user guide.

## Decisions (defaults; Jan may override)

- **D1 (O6, `/ready` and Valkey):** asked on a card. Default (a): `/ready` no longer fails on a Valkey outage; the routes keep answering their 503 envelope, and the alert `TextExtractionValkeyDown` (new, from the metrics' last-success timestamp) reports the outage. (b): keep it as it is and document it.
- **D2 (C9, types):** JSON bodies are parsed strictly (`"5"` is not an int, `"yes"` is not a bool). Multipart form fields of `/v1/extract/file` stay lax, since a form carries only strings.
- **D3 (C11, own pause):** the service's own outbound pause of a host answers `rate_limited` (retryable), with the wait in `error.details.retry_after_seconds`, instead of `connection_error`.
- **D4 (C12, jobs off):** `JOBS_ENABLED=false` answers 400 `invalid_request` like every other switch that is off; retryable 503s carry `Retry-After: 5`.
- **D5 (M3/S4):** under `AUTH_MODE=none` the API and UI answer only requests whose `Host` is in `ALLOWED_HOSTS` (default `localhost,127.0.0.1,[::1],api`); `*` switches the check off. Under `api_key` the setting is unused.
- **D6 (M2):** `MCP_MAX_ANSWER_CHARS` (default 200,000) bounds a whole tool answer: after `content`, the lists `chunks`, `links`, `structured.tables` and `items` are cut to fit, with the warning `mcp_answer_truncated` naming what was cut; `get_job` and the REST routes return the rest.

## Tasks

### Contract
- [ ] **C8** `/v1/screenshot` with an invalid URL answers 200 `failed`/`invalid_url` at once, like `/v1/extract` (today 202 and a job). Rule in `parameters.md` en/de: sync routes answer `invalid_url` in the envelope, a site job answers 422, a batch fails the item.
- [ ] **C9** strict JSON request models (D2). Test: `"wait_seconds":"5"` and `"cache":"yes"` answer 422.
- [ ] **C10** a JSON route with another `Content-Type` answers 415 `invalid_request` (envelope) instead of a misleading 422.
- [ ] **C11** own host pause → `rate_limited` (D3).
- [ ] **C12** jobs off → 400; `Retry-After: 5` on retryable 503s (D4).
- [ ] **C13** operationIds without the path noise (`extract`, `extract_file`, `get_job`, ...), request examples for `/v1/extract`, `/v1/extract/file`, `/v1/screenshot`, `/v1/batch`; `ErrorCode` description lists the codes.
- [ ] **C14** a failed item or result for an own fault carries `data` with `mode_used: null`, `upstream_status: null` and `timings`.

### MCP and UI
- [ ] **M2** answer budget (D6).
- [ ] **M3/S4** Host allowlist under `AUTH_MODE=none` (D5), REST and UI; MCP keeps its origin list too.
- [ ] **M4** one counted call does at most one unit of work: `get_job` with `wait_seconds` counts one hit per 10 s waited; `/ui/run/images` counts one hit per image; `/ui/run/site-tree` one per page of 500 items; MCP `initialize` and `tools/list` count one hit.
- [ ] **M5** the UI reads a screenshot artifact uncounted (the forwarded `POST` counted already).

### Ops
- [ ] **O4** a caller's B3 headers are stored with the job and bound in worker logs; `x-b3-traceid` is the trace id when there is no `x-client-trace-id`; `x-client-trace-id` and B3 go to the LLM and the external ASR endpoint. CLAUDE.md stack: OpenTelemetry is not used (B3 is passed by hand); the line is corrected.
- [ ] **O5** stale pooled connections: a health check (PING) on a connection idle for more than 5 s, which reconnects once; commands themselves are never retried (a retry could run a Lua script or MULTI twice).
- [ ] **O6** `/ready` (D1).
- [ ] **O11** a job deleted between `put_input` and `put_draft` leaves no input or budget reservation behind, for every pool.

### Probes
- [ ] **B1** a `text/html` body that is binary (NUL bytes, a known magic number) is `unsupported_content_type`, not garbage `partial`.
- [ ] **B2** a body declared UTF-8 that is not (many U+FFFD) is decoded as Windows-1252 with the warning `charset_mismatch`.
- [ ] **B3** a gzip bomb in browser mode answers `too_large` (the egress proxy's byte cap names it), not `timeout`.
- [ ] **R7** the hostile probe farm becomes `tests/e2e/audit_probes.py` (run by hand; SSRF forms, bombs, drip, loops).

### Dependencies
- [ ] **S5** fastmcp 4.1.2 once it is 14 days old.
- [ ] **S3 follow-up** Pillow 13 with docling-slim ≥ 2.142.0, from 2026-11-05 (14 days after 2026-10-22).

### Finish
- [ ] Docs en/de, `configuration.md`, Helm values for the new settings, CHANGELOG.
- [ ] requesting-code-review, full checks, CI, `live` run, merge.
- [ ] Tag `v0.1.1` per Jan's answer on the card.
