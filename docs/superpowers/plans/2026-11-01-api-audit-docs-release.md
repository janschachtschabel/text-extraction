# API audit, docs review and v0.1.0 release page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A thorough audit of the released API (v0.1.0, b7c6737), a review of README and docs against the code, control files (compose, Helm, `.env.example`) that work as shipped, and a GitHub release page for `v0.1.0` with notes from `CHANGELOG.md`.

**Architecture:** Audit first, read-only, in four independent review areas plus a black-box probe run against a local stack; every confirmed finding becomes a failing test, then a fix (TDD). The release page comes from a new `release` workflow that turns the changelog section of a tag into the release notes, so every later tag gets its page without a hand step.

**Tech Stack:** Python 3.13, uv, pytest, ruff, mypy, GitHub Actions (`gh release` with `GITHUB_TOKEN`), docker compose, Helm.

**Spec:** Jan, 2026-11-01 09:39 UTC: "ok plane nochmal einen vollständigen gründlichen audit der api, die überprüfung von readme und docs und die fertigstellung der github release seite mit notizen. prüfe ob docker compose und helmchart sowie example env als muster oder steuerdateien beiliegen". Rules: `CLAUDE.md`, `docs/architecture.md`, the ops guide.

## Global Constraints

- English in code, comments, developer docs, commits; every change to an English user doc changes its German partner in the same PR (`tests/test_docs_i18n.py`, `scripts/check_translation_pairs.py`).
- The tag `v0.1.0` stays where it is (b7c6737). Code fixes from the audit do not move it; they go into `## [Unreleased]` in `CHANGELOG.md` and a later `v0.1.1` (decision D4).
- No team names in any text; the contact is Jan Schachtschabel (private project).
- No AGPL/GPL dependency; no IP/proxy rotation; result envelope and status-code rules of CLAUDE.md rule 4 stay.
- One branch (`claude/project-thread-qx15nh`), one PR, squash merge (Jan allows merging once done).

## Review Focus

- A request that reaches the network through a path other than `fetch.py` (sitemaps, media/yt-dlp, image URLs for vision, browser sub-requests, redirects to private addresses) must still hit the SSRF guard.
- A user key (not admin) must never get `compliance: ignore`, unblock, caller JavaScript or another caller's job, over REST, MCP and the UI alike.
- Every error answer, also 401/413/422/429 and the MCP tool errors, carries the documented envelope fields.
- A copied `.env.example` with only `API_KEYS` and `VALKEY_PASSWORD` set must start the compose stack, and every setting of `Settings` must appear in it.
- Every curl and JSON example in README and user guide must run against the local stack and return what the text says.

---

## Read-only check done with the plan (2026-11-01)

| File | Result |
|---|---|
| `docker-compose.yml` | Present, works as control file: `cp .env.example .env` + `VALKEY_PASSWORD` → `docker compose config` passes for the base stack and every profile (`docling`, `asr`, `nlp`, `llm`, all four). Without the password it stops with a clear message. CI job `compose` checks it on every push. Gap: it always builds locally, there is no way to run the published `ghcr.io/...:0.1.0` images (D2). |
| `deploy/helm/text-extraction/` | Present: `Chart.yaml` 0.1.0, `values.yaml` (template, 856 lines), `values-production.example.yaml` (template), README, `test.sh`. CI on main (run 41264549612) passed `helm lint --strict` in 19 variants and `test.sh`. Stale comment: `values-production.example.yaml:21` says "No version is tagged yet". |
| `.env.example` | Present, 393 lines, loads into `Settings` without error. Missing 3 of 164 settings: `LLM_GRACEFUL_SECONDS`, `UI_DEFAULT_LANGUAGE`, `UI_DOCS_BASE_URL` (documented in `docs/configuration.md`, not in the example). No test pins it. |
| GitHub release | No release exists; the tag `v0.1.0` and the five images (0.1.0, 0.1) do. |

---

### Task 1: Control files

**Files:**
- Modify: `.env.example`, `deploy/helm/text-extraction/values-production.example.yaml:21`
- Create: `tests/test_env_example.py`
- Create (D2 = a): `docker-compose.release.yml`; Modify: `docs/user-guide/en/installation.md`, `docs/user-guide/de/installation.md`, `README.md`, `README.de.md`

- [ ] **Step 1: failing test `test_env_example_lists_every_setting`**: every `Settings.model_fields` name (alias, upper case) appears in `.env.example` as `NAME=` or `# NAME=`; the only extra name allowed is `VALKEY_PASSWORD`.
- [ ] **Step 2: failing test `test_env_example_loads`**: the active lines of `.env.example` as environment build `Settings()` without error.
- [ ] **Step 3:** add the three missing settings with their defaults and a comment each, in their sections (LLM, UI); fix the stale comment in the production values.
- [ ] **Step 4 (D2 = a):** `docker-compose.release.yml`, an override that sets `image: ghcr.io/janschachtschabel/text-extraction{,-browser,-docling,-asr,-nlp}:${TEXT_EXTRACTION_VERSION:-0.1.0}` and `pull_policy: always` on every app service, so `docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait` runs the release without a build. CI job `compose` gets one `config -q` step for it. Installation docs en/de get the command.
- [ ] **Step 5:** `uv run pytest tests/test_env_example.py tests/test_configuration_doc.py tests/test_docs_i18n.py -q` green; `docker compose -f docker-compose.yml -f docker-compose.release.yml config -q` passes; commit.

### Task 2: Release page

**Files:**
- Create: `scripts/release_notes.py`, `tests/test_release_notes.py`, `.github/workflows/release.yml`

**Interfaces:**
- Produces: `release_notes(changelog: str, version: str) -> str` and a CLI `python scripts/release_notes.py <version>` that prints the notes.

- [ ] **Step 1: failing tests**: `test_notes_hold_the_section_of_the_version` (the text between `## [0.1.0]` and the next `## [`, without the heading), `test_unknown_version_raises` (`SystemExit` with code 1), `test_notes_end_with_the_images_block` (the five `ghcr.io/janschachtschabel/text-extraction*:0.1.0` names and a link to the installation guide).
- [ ] **Step 2:** implement; the images block is fixed copy built from the version.
- [ ] **Step 3:** `release.yml`: on `push` of tags `v*.*.*` and on `workflow_dispatch` with input `tag`; `permissions: contents: write`; checks out `main`, runs the script into `notes.md`, then `gh release view "$TAG"` → `gh release edit "$TAG" --notes-file notes.md`, else `gh release create "$TAG" --verify-tag --title "$TAG" --notes-file notes.md` (`--prerelease` while the major is 0, D1). Idempotent: a second run only updates the text.
- [ ] **Step 4:** `uv run pytest tests/test_release_notes.py tests/test_release.py -q` green, `actionlint` if available; commit.
- [ ] **Step 5 (after the merge):** start the workflow by `workflow_dispatch` with `tag=v0.1.0` (GitHub MCP `actions_run_trigger`); check the page with `get_release_by_tag`. Fallback if the dispatch is refused: `gh release create` through Remote Control in `C:\Users\jan\te-p13-check`.

### Task 3: API audit (read-only reviews)

Four reviewers in parallel, each read-only, each returns findings with file:line, a failure scenario and a severity (Critical / Important / Minor). Every finding is then verified by reproducing it (test or probe); unverified findings are dropped.

- [ ] **3a Security:** SSRF guard on every outbound path (`fetch.py`, redirects, `site/` sitemaps, `media/` and yt-dlp, vision image URLs, browser route interception and the egress proxy, Docling/MarkItDown remote references); admin-only features (`compliance: ignore`, unblock, `actions` with JS) over REST, MCP and UI; job ownership (one key cannot read or cancel another key's job); byte and time limits (bodies, uploads, gzip, sitemap size, redirect loops); secrets in logs; LLM key precedence (header over env). Also `security-review` skill on the tree, `uvx pip-audit` on the lock, `scripts/check_licenses.py`.
- [ ] **3b Contract:** OpenAPI (`/openapi.json`): every public route present with request and response models and examples, internal routes absent, the security scheme set; the envelope (`status`, `error.code`, `error.retryable`, `upstream_status`, `mode_used`, timings) on every route and every error path (401, 403, 404, 413, 422, 429, 503); 200 + `failed` for target failures, 5xx only for own faults; error codes in the docs equal to the code's enum.
- [ ] **3c Jobs, state and ops:** statelessness (no local file as state), job lifecycle (create, status, paging, cancel, TTL, the sync-to-job fallback at ~30 s), fairness and leases under worker loss, Valkey memory guard, `/ready` during drain, metrics names in docs vs code, B3 and `x-client-trace-id` pass-through, settings validation (every bound in `configuration.md` equal to the code).
- [ ] **3d MCP and UI:** the 11 tools against their REST twins (same limits, same auth, same envelope), Origin check, size caps; the UI's auth, CSRF-free design, escaping of extracted content (XSS from a hostile page), language switch.

### Task 4: Black-box probe run

**Files:** Create `tests/e2e/audit_probes.py` (kept as a reusable probe set, run by hand like `run_batch`).

- [ ] **Step 1:** local stack: `redis-server`, API + workers with `uv run`, real Chromium, `FETCH_ALLOW_PRIVATE_NETWORKS=false`, a local hostile site farm (the one of `tests/load/`) plus public pages.
- [ ] **Step 2:** probes, each with the expected answer: private and metadata IPs directly and by redirect and DNS name, redirect loop, 30 MB body, gzip bomb, slow drip, non-HTML types, broken encodings, IDN host, `javascript:` and `file:` URLs, user key with `compliance: ignore`, foreign job id, bad JSON, oversized batch, every endpoint without key and with a wrong key, rate limit, MCP tool calls with the same inputs.
- [ ] **Step 3:** every probe whose answer differs from the expectation becomes a finding for Task 5.

### Task 5: Fixes

- [ ] Per confirmed Critical or Important finding: a failing test in the owning test file, the fix, the test green, a commit `fix: <finding>`. Minor findings: fixed when the fix is small, else listed in the PR as parked.
- [ ] `CHANGELOG.md`: `## [Unreleased]` with `### Fixed` per fix (D4).

### Task 6: Docs review

- [ ] **Step 1: link check:** a test `tests/test_doc_links.py` that resolves every relative link and `#anchor` in `README*.md`, `docs/**/*.md` (without `docs/superpowers/`) and the Helm README.
- [ ] **Step 2: parameter check:** every field of the request models (`extract_models`, `file_models`, `browser_models`, `job_models`, `site`, `structured`, `chunking`) appears in `docs/user-guide/en/parameters.md` and the German partner, with the same default; a test pins it.
- [ ] **Step 3: examples:** every curl/JSON example of README and user guide (en and de) run against the stack of Task 4; wrong examples fixed in both languages.
- [ ] **Step 4: read-through** of README en/de, user guide, `configuration.md`, Helm README, `handover.md`, `CHANGELOG.md` for statements the code contradicts or that went stale with the tag (e.g. "no version is tagged yet", status lines, counts like "11 tools", "164 settings").
- [ ] **Step 5:** `uv run pytest tests/test_docs_i18n.py tests/test_configuration_doc.py tests/test_handover_doc.py -q` and `uv run python scripts/check_translation_pairs.py` green; commit.

### Task 7: Finish

- [ ] `requesting-code-review` on the whole branch; findings handled with `receiving-code-review`.
- [ ] `verification-before-completion`: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, CI (helm lint, `test.sh`, compose), the `live` label run (wlo_200 auto ≥ 81 %, the band of the last runs).
- [ ] PR (draft → ready), squash merge, then Task 2 Step 5 (release page), then report to Jan: findings table, what was fixed, what is parked, the release link; v0.1.1 tag only if Jan wants it (D4).

## Decisions for Jan

- **D1 Release page:** (a, recommended) a `release` workflow in the repo creates it from the changelog, marked as pre-release (0.x prototype); (b) the same, as a full release; (c) Jan creates it by hand from prepared text.
- **D2 Compose with released images:** (a, recommended) an override file `docker-compose.release.yml` for the ghcr images, no build needed; (b) compose stays build-only.
- **D3 Audit depth:** (a, recommended) four parallel read-only reviewers plus the probe run and a final reviewer; (b) one reviewer for the whole repo (cheaper, less thorough).
- **D4 Fixes and version:** (a, recommended) the tag `v0.1.0` stays, its page shows the 0.1.0 notes; fixes go to `[Unreleased]` and a `v0.1.1` once Jan says so; (b) no new version, fixes only on main.
