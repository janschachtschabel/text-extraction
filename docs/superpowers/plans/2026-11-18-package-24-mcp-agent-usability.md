# Package 24: MCP agent usability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four gaps an MCP agent meets today: it cannot see which `options` exist, cannot
learn which features and limits the service has, gets no image from `extract_url` with a
screenshot, and must read a site crawl item by item with all links and content.

**Architecture:** All four reuse the REST cores (ruling P10-S1). (1) Each tool's `options`
description lists the REST body's further fields with a short hint, generated from the REST model
and a hint table. (2) `GET /v1/info` (and `service_info`) gains `features` (what the caller's key
can use) and `limits`. (3) Every extraction tool attaches a `data.screenshot` it can read, as
`screenshot_url` does now; `get_job` hints at `get_job_screenshot`. (4) `GET /v1/jobs/{id}` gains
the opt-in query `view=summary`, which drops the heavy result fields; `get_job` gets `view` too.

**Tech Stack:** Python 3.13, FastAPI, pydantic, FastMCP, pytest.

**Spec:** Jan's request of 2026-11-18 ("MCP Lücken beheben") and the findings in
`/mnt/project-files/mcp-websuche/befund-mcp-websuche.md` (gaps 1-4).

## Global Constraints

- English in code, comments, developer docs; user docs `docs/user-guide/en|de/` change in pairs.
- `GET /v1/jobs/{id}` without `view` answers exactly as before (the UI's live tree polls it).
- Each extraction tool's input schema stays under 4096 bytes (`test_tools_list_lean_schemas`).
- No new dependency. `ruff check`, `ruff format --check`, `mypy` and `pytest` green.

## Review Focus

1. A REST field added later without a hint: a test fails (every field reachable through `options`
   has a hint), so the description never silently drops a field.
2. A user key whose role may not use OCR/PII/LLM/ASR: `features` says false for it while an
   admin key on the same service sees true (the UI's `feature_offer` logic, not a copy of it).
3. `view=summary` on a `site` job: `data.site.links` and `data.site.sitemap` are null, `role`,
   `depth`, `parent` and `page_type` stay.
4. `get_job` with `view: full` and `limit` over 50 is refused; with `summary` up to 500 is fine.
5. `extract_url` with `screenshot: true` whose image is over `MCP_IMAGE_MAX_BYTES`: the same
   download hint as `screenshot_url`, no image.

---

### Task 1: Hints for `options`

**Files:**
- Create: `src/text_extraction/mcp_server/option_hints.py`
- Modify: `src/text_extraction/mcp_server/tools_extract.py` (`options_argument`, `ExtractionTool`)
- Modify: `src/text_extraction/mcp_server/tools_jobs.py` (`BatchExtractTool`, `SiteStructureTool`)
- Test: `tests/mcp_server/test_option_hints.py`

**Interfaces:**
- Produces: `OPTION_HINTS: Final[dict[str, str]]`; `option_fields(rest_model: type[BaseModel],
  args_model: type[BaseModel]) -> list[str]` (REST fields that are not a tool argument, in the
  REST model's order); `options_description(route: str, fields: list[str]) -> str`.
- `ExtractionTool` gains `rest_model: ClassVar[type[BaseModel]]`; its `__init__` sets
  `parameters["properties"]["options"]["description"]`.

- [ ] **Step 1: Failing tests** — `test_every_option_field_has_a_hint` (for each of the 7 tools
  with `options`, every name of `option_fields` is in `OPTION_HINTS`);
  `test_options_description_lists_the_fields` (extract_url's description contains
  `pii (`, `ocr (auto|never|always)`, `compliance (`, `screenshot (` and still `/docs`, and does
  not contain `url (` or `mode (`); `test_site_options_description` (site_structure lists
  `sitemaps (`, `metadata (`, `compliance (`, `cache (` and nothing else).
- [ ] **Step 2: Run** `uv run pytest tests/mcp_server/test_option_hints.py -q` → FAIL (import).
- [ ] **Step 3: Implement.** Description text:
  `"Further fields of the REST body of {route} (details: /docs): {name (hint)}, ...; e.g. {\"pii\": {\"mode\": \"mask\"}}."`
  Hints at most ~60 characters; enumerations written `a|b|c`. `rest_model` per tool:
  ExtractRequest, FileForm, ScreenshotRequest, StructuredRequest, ChunkRequest, BatchRequest,
  SiteStructureRequest.
- [ ] **Step 4: Run** the new tests and `tests/mcp_server/test_tools_extract.py::test_tools_list_lean_schemas` → PASS.
- [ ] **Step 5: Commit** `feat(mcp): list the option fields in each tool's options description`.

### Task 2: Features and limits in `GET /v1/info`

**Files:**
- Modify: `src/text_extraction/info.py`, `src/text_extraction/app.py` (route),
  `src/text_extraction/mcp_server/tools_info.py`, `src/text_extraction/feature_access.py`,
  `src/text_extraction/ui/forms.py` (`feature_offer` uses the shared function)
- Test: `tests/test_info.py` (new), `tests/mcp_server/test_server.py` (service_info answer)

**Interfaces:**
- Produces: `offered_features(role: Role | None, settings: Settings) -> tuple[dict[str, bool],
  frozenset[str]]` in `feature_access.py` (keys `ocr`, `pii`, `transcribe`, `llm`; the second
  value the admin-only ones), used by `ui.forms.feature_offer` and `info`.
- `InfoFeatures` (bool fields): `jobs`, `browser`, `screenshots`, `unblock`, `media`, `ocr`,
  `pii`, `transcribe`, `llm`, `llm_needs_key`, `semantic_chunking`, `describe_images`,
  `site_structure`, `compliance_choice`; plus `compliance_default: ComplianceMode`.
- `InfoLimits` (int | None): `batch_max_urls`, `site_max_depth`, `site_max_pages`,
  `upload_max_bytes` (`upload_file_limit`), `request_max_body_bytes`, `rate_limit_per_minute`
  (the key's own, else the service's, None while `RATE_LIMIT_ENABLED` is off).
- `InfoData` gains `features: InfoFeatures`, `limits: InfoLimits`; `service_info(principal,
  settings)`.

- [ ] **Step 1: Failing tests** — `test_info_lists_features_for_the_role` (OCR on with
  `OCR_ALLOW_USERS=false`: user key `ocr` false, admin key true); `test_info_unblock_admin_only`;
  `test_info_limits` (values from settings, key rate limit wins, None when rate limit off);
  `test_service_info_tool_matches_rest`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** as above; `screenshots` = browser and jobs; `unblock` = browser,
  `BROWSER_UNBLOCK_ENABLED` and admin; `compliance_choice` = admin or `COMPLIANCE_ALLOW_OVERRIDE`;
  `site_structure` = jobs.
- [ ] **Step 4: Run** the new tests, `tests/ui/` and `tests/test_app.py` → PASS.
- [ ] **Step 5: Commit** `feat(api): /v1/info lists the caller's features and the limits`.

### Task 3: Screenshots of every extraction tool

**Files:**
- Modify: `src/text_extraction/mcp_server/tools_extract.py` (attach moves from
  `ScreenshotUrlTool` to `ExtractionTool`), `src/text_extraction/mcp_server/tools_jobs.py`
  (`get_job` hint, `get_job_screenshot` description)
- Test: `tests/mcp_server/test_tools_extract.py`, `tests/mcp_server/test_tools_jobs.py`

- [ ] **Step 1: Failing tests** — `test_extract_url_attaches_its_screenshot` (BrowserCase,
  `options: {"screenshot": true}` → one image block, PNG bytes); `test_extract_url_screenshot_over_the_cap_is_linked`;
  `test_get_job_hints_at_the_screenshot` (a page with an item that has `data.screenshot` gets the
  hint `"Item {i} has a screenshot: get_job_screenshot with job_id {id}."`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; `test_screenshot_hint_without_a_size` keeps passing through the base class.
- [ ] **Step 4: Run** `tests/mcp_server/` → PASS.
- [ ] **Step 5: Commit** `feat(mcp): attach the screenshot of extract_url, extract_structured and chunk`.

### Task 4: `view=summary` for jobs

**Files:**
- Modify: `src/text_extraction/jobs_api.py` (`get_job`, `read_job`, new `summary_item`),
  `src/text_extraction/mcp_server/tools_jobs.py` (`GetJobArguments.view`, limit check,
  `site_structure` description)
- Test: `tests/test_jobs_api.py`, `tests/test_site_api.py`, `tests/mcp_server/test_tools_jobs.py`

**Interfaces:**
- Produces: `JobViewMode = Literal["full", "summary"]`; `read_job(state, principal, job_id,
  offset, limit, view: JobViewMode = "full")`; `summary_item(item: JobItem) -> JobItem`.
- Summary drops (sets to None): `content`, `metadata`, `links`, `fingerprint`, `structured`,
  `chunks`, `image_descriptions`, `server_errors`, and in `site`: `links`, `sitemap`. Everything
  else stays.

- [ ] **Step 1: Failing tests** — `test_job_summary_view_drops_the_heavy_fields` (batch);
  `test_site_job_summary_keeps_role_depth_and_type`; `test_job_view_default_is_unchanged`;
  MCP `test_get_job_summary_allows_500_items` and `test_get_job_full_refuses_over_50`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; the full view's MCP cap stays `MAX_PAGE_ITEMS` (50), summary up to
  `MAX_PAGE_SIZE` (500).
- [ ] **Step 4: Run** the job tests → PASS.
- [ ] **Step 5: Commit** `feat(jobs): view=summary for GET /v1/jobs/{id} and get_job`.

### Task 5: Docs, changelog, package row

**Files:** `docs/user-guide/en/mcp.md` + `de/mcp.md`, `docs/user-guide/en/usage.md` + `de` (info
and jobs, where they describe them), `docs/packages.md` (row 24), `CHANGELOG.md` ([Unreleased]
Added), `docs/architecture.md` (MCP section: the four rulings).

- [ ] Update, run `uv run pytest tests/test_docs_i18n.py tests/test_check_translation_pairs.py -q`
  and `uv run python scripts/check_translation_pairs.py` → PASS; commit `docs: package 24`.

### Task 6: Whole-package checks

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`.
- [ ] Code review of the branch (requesting-code-review), fix findings, open the draft PR.
