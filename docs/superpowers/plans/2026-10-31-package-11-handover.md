# Package 11: Hand-over Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the hand-over to team T6. This means:

- a full WLO regression run on 4,000 URLs;
- a reproducible load test with measured resource baselines (`docker stats`), which also sizes `STRUCTURED_SELECTOR_PROCESSES` (issue #19) and the package 9 load-test items;
- Helm requests and limits taken from those measurements;
- a hand-over document that answers every item of the ops guide's checklist (section 6) and its maintenance section (section 7);
- a first tagged release.

**Architecture:** No new service code paths. The package adds three things:

- **Harnesses.** `live.yml` gets a mode input for the 4,000-URL run. A new `load.yml` workflow builds the five images, starts the compose stack with every profile next to a local site farm, drives scenarios with an own async httpx load generator, and samples `docker stats`.
- **Documents.** `docs/handover.md` (checklist answers and a compact architecture overview), cost notes, an example production values file, and a CHANGELOG.
- **Maintenance.** Dependabot.

Measurements feed back into `values.yaml`, `docs/configuration.md` and `docs/architecture.md`. Only measured numbers go in. If a measurement shows a setting needs a change, record that as a ruling.

**Tech Stack:** Python 3.13, uv, httpx (load generator), Docker Compose v2 and `docker stats` on the GitHub runner (ubuntu-latest: 4 vCPU, 16 GB), GitHub Actions, Helm 3.19, Dependabot.

**Spec:**

- The ops guide: Jan's first message in the project, 2026-10-23. Its sections 5.3, 5.5, 6 and 7 are quoted below.
- `docs/packages.md` row 11: "Full WLO regression run, load test (it also sizes the default `STRUCTURED_SELECTOR_PROCESSES`, 1 today, issue #19), resource baselines (`docker stats`), env reference, example `values.yaml`, secrets list, cost notes (LLM, ASR), final architecture doc". Done when: "Ops-guide hand-over checklist complete".
- The package 9 load-test notes in `docs/packages.md`.
- `CLAUDE.md`.

## Global Constraints

- English only in code, comments, docs and commit messages.
- No AGPL/GPL dependencies. k6 (AGPL-3.0) is excluded. The load generator is our own httpx script, like `tests/live/run_live.py` and `tests/e2e/run_batch.py`.
- The load test sends no load to third-party sites. Every load target is the local site farm (`tests/e2e/site_farm.py` or its container), with `FETCH_ALLOW_PRIVATE_NETWORKS=true` only inside the load stack. Only the WLO regression touches real sites, at the normal per-host outbound limits.
- No secrets in the repo or in workflow logs. An LLM key, if one is used for cost measurement, comes from a repository secret and is never echoed.
- Measured values only. Every number in a doc or in `values.yaml` names its source run (workflow run URL or date and commit).
- The ops guide, section 5.3, verbatim: "Die Werte unter realistischer Last messen, z. B. mit docker stats während eines Testlaufs, statt sie zu schätzen. Großen Speicherbedarf, etwa durch Modelle, die in den RAM geladen werden, gesondert ausweisen."
- Helm sizing rule (P11-S3): request = the measured steady-state p95 under the load profile, rounded up to the next 64Mi or 50m; limit = 1.5 × the measured peak, rounded up to the next 256Mi or 250m. A component that loads a model states the model's share separately.
- Never skip, disable or quarantine a test to get green.

## Rulings (P11-S*)

- **P11-S1.** The load generator lives in `tests/load/` (a package next to `tests/live` and `tests/e2e`). It is not part of the default pytest run. Its pure parts (scenario plan, stats parsing, percentiles, report) have unit tests in `tests/test_load_*.py`, which are.
- **P11-S2.** `docker stats` is sampled with `docker stats --no-stream --format '{{json .}}'` every 2 s into a JSONL file. The report gives, per container and scenario, CPU % (mean, p95, max) and memory (p95, max) in MiB. Memory comes from `MemUsage`, which is cgroup usage without page cache.
- **P11-S3.** The sizing rule in Global Constraints.
- **P11-S4.** The load profile is "realistic" in the guide's sense. Its parts:
  - sync fast extracts at 10 and 30 concurrent clients;
  - a batch of 1,000 URLs;
  - 100 browser renders;
  - 50 document uploads (MarkItDown) and 10 OCR PDFs (Docling);
  - 200 PII calls (`md` and `lg` models);
  - 10 audio files of 1 minute (ASR `small` and `large-v3-turbo`);
  - 200 structured calls with selectors and 50 with the LLM (against a fake OpenAI-compatible server that answers with a fixed delay);
  - one site crawl at the default caps, plus one sitemap of 200,000 entries;
  - 20 MCP tool calls.

  Each scenario runs alone, so `docker stats` attributes the load. Durations and counts are constants in `tests/load/scenarios.py`.
- **P11-S5.** The `STRUCTURED_SELECTOR_PROCESSES` sweep runs the selector scenario at 1, 2 and 4 children per pod. It records throughput, p95 latency and the extra memory per child. The default becomes the smallest value whose throughput is within 10 % of the best. Issue #19 is closed with the numbers.
- **P11-S6.** Cost notes give the formula and the measured inputs, not a fixed euro sum:
  - **LLM:** prompt and completion tokens per structured call and per semantic-chunk call, counted by the fake server from the request bodies. The tokens are approximated as UTF-8 bytes / 4, with the approximation stated. Then requests per page × price per 1M tokens, with the price as a parameter. A real-model run is optional (D4 decides).
  - **ASR:** CPU seconds per audio minute for `small` and `large-v3-turbo`, from the load test.
  - **External APIs:** none by default; the optional external ASR backend and the LLM are listed with their env vars.
- **P11-S7.** The site structure load items from package 9:
  - the event-loop time of packing and encoding 200,000 sitemap entries;
  - crawl duration at `SITE_MAX_PAGES` against `JOB_TTL_SECONDS`;
  - Valkey bytes per open crawl (via `MEMORY USAGE` over the job's keys);
  - extract worker peak memory with three 50 MiB sitemaps in flight.

  All four are measured in the load test. The split of `site/runner.py` into `runner.py` and `sitemap_items.py` is a pure move, done in Task 4 with no behaviour change. `SITE_PARSE_CONCURRENCY` is added only if the measurement shows the one-thread pool as the bottleneck: the sitemap scenario's wall time > 2 × the parse time of one sitemap. Otherwise the result is recorded as "not needed" with the numbers.
- **P11-S8.** The full WLO regression uses `live.yml` with a new `mode` input (`auto`, `fast`, `browser`; default `auto`). Only a manual run with `url_set: wlo_4000` uses it. The PR gate (`wlo_200`, auto, ≥ 80 %) does not change. Results are compared with the 2026-09-28 baselines (fast 68 %, browser 80 % on 4,000 URLs). They are reported, not gated.
- **P11-S9.** The hand-over document is `docs/handover.md`. It has one section per checklist item of the ops guide's section 6, in the guide's order and with the guide's German item text as the heading's subtitle, plus section 7 (maintenance and lifecycle). Each answer names the file, setting or workflow that delivers it. A test (`tests/test_handover_doc.py`) checks that every checklist item has a section and that every relative link in the file resolves.
- **P11-S10.** The env reference completeness:
  - A test (`tests/test_configuration_doc.py`) checks that every field of `Settings` has a row in `docs/configuration.md` with its default, and that every secret named in the `Settings` docstring is in the "Secrets" table.
  - If such a test already exists, extend it instead.
- **P11-S11.** The example production values file is `deploy/helm/text-extraction/values-production.example.yaml`. It sets the measured requests and limits, external Valkey (`valkey.enabled: false` plus `externalValkey`), `secrets.existingSecret`, a pinned SemVer image tag, the PrometheusRule and ServiceMonitor on, and MCP and UI off. `deploy/helm/test.sh` renders it with `--strict` and checks those keys.

## Decisions for Jan (recommended = a)

- **D1. Where the load test and the `docker stats` baselines run.**
  - a) In a GitHub Actions workflow (`load.yml`, started by hand) on the standard runner, with the stack from the repo's compose file. It is reproducible, needs no PC, and the numbers carry the run URL.
  - b) On Jan's PC through Remote Control (Docker Desktop).
  - c) Both: the runner for the baselines, Jan's PC as a cross-check.
- **D2. Size of the full WLO regression.**
  - a) `wlo_4000` in all three modes (auto, fast, browser), one run after another. That is about 5 h of runner time, free on the public repo. It is the only like-for-like comparison with the 2026-09-28 baselines.
  - b) Auto only (about 45 min).
  - c) Auto and fast.
- **D3. First release.**
  - a) Tag `v1.0.0` on main after this package is merged. The image workflow then publishes `1.0.0` and `1.0` for all five images, and the chart gets version and appVersion `1.0.0`.
  - b) Tag `v0.1.0` and call it a prototype; the guide's section 7 then records when a decision on permanent operation is due.
  - c) No tag yet.
- **D4. Contact and maintenance.**
  - a) Jan is the T4 contact for questions and updates. Dependabot opens weekly grouped PRs (uv, GitHub Actions, Docker base images), and Jan, or Claude in this project, merges them after green CI. Cost notes use measured token counts and a price parameter, with no real-model run.
  - b) Like a), plus one real-model cost run with Jan's LLM key as a repository secret.
  - c) Another contact person (Jan names them).
- **D5. The open package 10 checks** (WLO pages in the UI, `docker compose up`, Claude Desktop through `mcp-remote`).
  - a) At the end of this package, on Jan's PC through Remote Control in a fresh clone, about 20 minutes. Jan only needs to approve the session.
  - b) Jan runs them himself from a short checklist in `docs/handover.md`.
  - c) Skip them.

## Review Focus

1. **A load-test number that does not measure what the doc claims.** Two examples: memory sampled while the container was idle, or a scenario overlapping another so `docker stats` mixes them. Each scenario must run alone, the sampler must cover its whole window, and the report names the window.
2. **A load target that leaks to the internet.** For example, a redirect in a farm page, or a DNS name that resolves outside the compose network. Every scenario URL must resolve to the farm container, and the load stack sets no proxy.
3. **Secrets in logs or artifacts.** An API key in the load generator's printed config, the LLM key in a workflow log, or `docker inspect` output in an artifact. The workflow masks keys, and the artifacts hold only stats, results and service logs.
4. **Helm values that no longer render.** These come from sizing changes or the example production file (`--strict`, every toggle combination in `test.sh`), or a request above its limit.
5. **The hand-over doc drifting from the code.** For example, a setting renamed in configuration.md but not in handover.md, or a dead link. `tests/test_handover_doc.py` and `tests/test_configuration_doc.py` pin this.

---

### Task 1: Full WLO regression mode in `live.yml`

**Files:**
- Modify: `.github/workflows/live.yml` (inputs, the gate step, `timeout-minutes`)
- Modify: `tests/live/README.md`
- Test: `tests/test_live_workflow.py` (create; parses the workflow YAML)

**Interfaces:**
- Produces: the `workflow_dispatch` input `mode` (choice `auto|fast|browser`, default `auto`). The run step uses `--mode "$MODE"` for `URL_SET`. The report-only steps (smoke, browser on wlo_200, media, sites) are skipped when `url_set` is `wlo_4000`. The job timeout is 330 minutes for `wlo_4000` and 110 otherwise, set via an expression.

- [ ] **Step 1: Write the failing tests** in `tests/test_live_workflow.py`, using `yaml.safe_load` on the workflow:
  - `test_manual_run_offers_a_mode_input`: `inputs.mode.options == ["auto", "fast", "browser"]` and the default is `auto`.
  - `test_pull_request_gate_stays_auto_on_wlo_200`: the env defaults still resolve to `wlo_200` and `80`, and the run step contains `--mode "$MODE"` with `MODE: ${{ inputs.mode || 'auto' }}`.
  - `test_full_run_skips_the_report_only_steps`: each report-only step has `if:` containing `env.URL_SET != 'wlo_4000'`.
  - `test_full_run_has_room_for_four_hours`: `timeout-minutes` is an expression yielding 330 for `wlo_4000`.
- [ ] **Step 2: Run** `uv run pytest tests/test_live_workflow.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the input, the `MODE` env, the `if:` conditions and the timeout expression. In `tests/live/README.md`, describe the full regression ("run three manual dispatches with `url_set: wlo_4000` and mode auto, fast, browser") and the baselines it compares with.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then `uv run pytest -q tests/test_live*.py`.
- [ ] **Step 5: Commit** `ci: add a mode input for the full WLO regression run`.

### Task 2: Load-test harness (pure parts)

**Files:**
- Create: `tests/load/__init__.py`, `tests/load/scenarios.py`, `tests/load/stats.py`, `tests/load/report.py`, `tests/load/run_load.py`, `tests/load/fake_llm.py`
- Test: `tests/test_load_stats.py`, `tests/test_load_report.py`, `tests/test_load_scenarios.py`, `tests/test_load_fake_llm.py`

**Interfaces:**
- Produces:
  - `scenarios.SCENARIOS: tuple[Scenario, ...]`. `Scenario` is a frozen dataclass with `name: str`, `kind: Literal["sync", "batch", "upload", "site", "mcp"]`, `endpoint: str`, `count: int`, `concurrency: int`, `body: Callable[[int, str], dict[str, object]]` (index, farm base URL → request JSON) and `settle_seconds: int = 10`. It holds the P11-S4 profile.
  - `stats.parse_line(line: str) -> StatSample`, where `StatSample` holds `t: float`, `container: str`, `cpu_percent: float` and `mem_mib: float`. It parses `docker stats --format '{{json .}}'` output, with units KiB/MiB/GiB and `CPUPerc` like `"153.27%"`.
  - `stats.Sampler`: an async context manager that runs the sampling subprocess every 2 s and writes JSONL.
  - `report.summarise(samples: Sequence[StatSample], windows: Mapping[str, tuple[float, float]]) -> list[Row]`. A `Row` holds `scenario`, `container`, `cpu_mean`, `cpu_p95`, `cpu_max`, `mem_p95` and `mem_max`.
  - `report.markdown(rows, results) -> str`.
  - `report.sizing(rows) -> dict[str, Resources]`, which applies P11-S3.
  - `run_load.main(argv)`. Its arguments are `--base-url`, `--internal-urls`, `--farm-url`, `--api-key-env API_KEY`, `--only NAME` (repeatable), `--out-dir` and `--selector-processes N` (a label only).
  - `fake_llm`: a tiny ASGI app (Starlette, already a dependency). `POST /v1/chat/completions` and `/v1/embeddings` answer valid OpenAI-shaped JSON after `FAKE_LLM_DELAY_MS` (default 800) and count request bytes per route. `GET /stats` returns the counts.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_line_reads_cpu_and_memory`: two fixture lines with `"MemUsage": "1.2GiB / 15.6GiB"` and `"512MiB / 15.6GiB"`, giving 1228.8 and 512.0; `"CPUPerc": "153.27%"` gives 153.27.
  - `test_summarise_uses_only_samples_inside_the_window`.
  - `test_percentile_p95_of_known_series`.
  - `test_sizing_rounds_request_and_limit` (P11-S3: p95 300 MiB → request 320Mi; peak 900 MiB → limit 1536Mi; CPU p95 0.37 cores → request 400m; peak 1.6 cores → 2.4 → limit 2500m).
  - `test_every_scenario_targets_the_farm`: each body's URLs start with the farm base URL passed in.
  - `test_fake_llm_counts_request_bytes`: Starlette TestClient against `fake_llm`.
  - `test_markdown_names_each_window`.
- [ ] **Step 2: Run** `uv run pytest tests/test_load_*.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the modules. `run_load` runs the scenarios one after another and records each window (start, end + `settle_seconds`). It gets latencies with httpx (`asyncio.Semaphore(concurrency)`) and polls jobs for batch and site scenarios until done. It writes `results.jsonl`, `stats.jsonl`, `summary.md` and `sizing.json`. It reads the API key from the env var named by `--api-key-env` and never prints it.
- [ ] **Step 4: Run** the tests. Expected: PASS. Run ruff and mypy on `tests/load`.
- [ ] **Step 5: Commit** `test: add the load-test harness with docker stats sampling and a sizing report`.

### Task 3: Load workflow and compose override

**Files:**
- Create: `.github/workflows/load.yml`
- Create: `deploy/load/compose.load.yml` (an override: services `farm`, which runs `python -m tests.e2e.site_farm --bind 0.0.0.0` from the API image with `tests/` mounted read-only, and `fake-llm`; env `FETCH_ALLOW_PRIVATE_NETWORKS=true`, `LLM_BASE_URL=http://fake-llm:8000/v1`, `ASR_MODEL` from the workflow input)
- Modify: `tests/e2e/site_farm.py` (bind address and the large sitemap fixtures, if they are missing)
- Test: `tests/test_load_workflow.py`

**Interfaces:**
- Consumes: Task 2's `run_load.main`.
- Produces: the workflow `load` with `workflow_dispatch` inputs `selector_processes` (string, default `"1,2,4"`) and `asr_model` (choice `small|large-v3-turbo`, default `large-v3-turbo`). The artifact `load-results` holds `summary.md`, `sizing.json`, `stats.jsonl`, `results.jsonl` and the service logs. The job summary is `summary.md`.

- [ ] **Step 1: Write the failing tests** in `tests/test_load_workflow.py`:
  - `test_load_runs_only_by_hand` (`on` holds only `workflow_dispatch`).
  - `test_load_stack_uses_the_override_and_all_profiles` (the run step has `-f docker-compose.yml -f deploy/load/compose.load.yml --profile docling --profile asr --profile nlp --profile llm`).
  - `test_load_masks_the_api_key` (`::add-mask::` before use).
  - `test_load_uploads_no_inspect_output` (the artifact paths are only `load-results/**`).
  - `test_farm_sets_no_proxy` (the override sets no `HTTP_PROXY` or `HTTPS_PROXY`).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the workflow:
  1. Check out the repo.
  2. Create `.env` with a random `VALKEY_PASSWORD` and an API key, both masked.
  3. `docker compose ... build`, then `up -d --wait`.
  4. Run `uv run python -m tests.load.run_load` once per value in `selector_processes`, restarting the stack with `STRUCTURED_SELECTOR_PROCESSES` set. Only the selector scenario repeats.
  5. Upload the artifact and write the job summary.
  6. Run `docker compose down -v` (always).

  Set `timeout-minutes: 120`.
- [ ] **Step 4: Run** the tests. Expected: PASS. Run `docker compose -f docker-compose.yml -f deploy/load/compose.load.yml config -q` if Docker is available. If it is not, note that CI's `compose` job checks it, and add the override to that job's `config` call.
- [ ] **Step 5: Commit** `ci: add a manual load-test workflow on the compose stack with a local site farm`.
- [ ] **Step 6 (controller):** Push, dispatch `load.yml` on the branch, and read the summary through `get_job_logs`. Record the run URL in the ledger. A failing scenario is fixed in this task before Task 5 uses the numbers.

### Task 4: Site structure load items and the runner split

**Files:**
- Modify: `src/text_extraction/site/runner.py`; create `src/text_extraction/site/sitemap_items.py` (pure move: robots, sitemaps, parsing)
- Modify: `tests/load/scenarios.py` (P11-S7 measurements: one sitemap of 200,000 entries, three 50 MiB sitemaps at once, a crawl at `SITE_MAX_PAGES`)
- Modify: `tests/load/run_load.py`. It reads the Valkey `MEMORY USAGE` of a crawl's keys (`job:{id}*`) through `redis-cli` in the valkey container, and the event-loop lag from the worker's existing metrics. If no such metric exists, add `text_extraction_event_loop_lag_seconds` (a histogram sampled every 0.5 s) to the worker's internal metrics, test-first.
- Test: the existing `tests/site/` suite unchanged (the move must keep it green); `tests/test_load_scenarios.py` gets the site scenarios.

**Interfaces:**
- Produces: `site/sitemap_items.py` with the moved functions under their current names. `runner.py` imports them, and no public name changes. Optionally `text_extraction_event_loop_lag_seconds`.

- [ ] **Step 1:** Move the code. Run `uv run pytest -q tests/site` (must pass unchanged) and `uv run mypy`. Commit `refactor: move the sitemap items out of the site runner`.
- [ ] **Step 2:** Write the failing scenario test (the site scenarios target the farm's `/sitemap-200k.xml` and `/sitemap-50m-{1..3}.xml`), add the farm fixtures and the scenarios, and run the tests to PASS. Commit.
- [ ] **Step 3 (only if the metric is missing):** Test-first, add the lag histogram, with a test asserting it is exported on the internal port and absent from OpenAPI. Commit.
- [ ] **Step 4 (controller):** Re-run `load.yml` with `--only` set to the site scenarios. Record the P11-S7 numbers and the `SITE_PARSE_CONCURRENCY` ruling in the ledger.

### Task 5: Apply the measurements

**Files:**
- Modify: `deploy/helm/text-extraction/values.yaml` (requests and limits per component), `deploy/helm/test.sh`
- Modify: `src/text_extraction/settings.py` (`structured_selector_processes` default, per P11-S5), `docs/configuration.md` (its row and a new "Resource baselines" section), `docs/architecture.md` ("Observability" and "Lessons" tables)
- Modify: `docker-compose.yml` (`deploy.resources` hints in comments only, if the numbers are useful there)
- Test: `tests/test_settings.py` (the default), `deploy/helm/test.sh` (the rendered requests and limits)

**Interfaces:**
- Consumes: `sizing.json` from the Task 3 and 4 runs (the controller passes the run URL and the file in the task brief).
- Produces: a "Resource baselines" table in `docs/configuration.md`. Each row has: component, image, scenario, CPU p95/max, memory p95/max, model share, request and limit, and the source run.

- [ ] **Step 1:** Write the failing tests. In `tests/test_settings.py`, `test_selector_processes_default_is_measured` expects the value from P11-S5. In `test.sh`, per component, `expect_contains` on the new requests and limits.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Apply the values, then write the doc table and the issue #19 note.
- [ ] **Step 4:** Run `uv run pytest -q tests/test_settings.py`, `bash deploy/helm/test.sh` (with a helm built from source if there is none) and `helm lint --strict`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: size Helm resources and the selector pool from the load test`.

### Task 6: Hand-over document, cost notes, example values, env reference

**Files:**
- Create: `docs/handover.md`
- Create: `deploy/helm/text-extraction/values-production.example.yaml`
- Modify: `deploy/helm/test.sh` (render the example with `--strict`), `deploy/helm/text-extraction/README.md` (link it)
- Modify: `README.md` (a "Hand-over" link under Documentation), `docs/configuration.md` (cost notes section, if not in handover.md)
- Test: `tests/test_handover_doc.py`, `tests/test_configuration_doc.py` (create or extend, per P11-S10)

**Interfaces:**
- Consumes: Task 5's tables; the WLO regression results (the controller passes them); Jan's D3 and D4 answers.
- Produces: `docs/handover.md` with these sections:
  1. A compact architecture overview: components, dependencies and data flows, one Mermaid diagram, and at most one page of text.
  2. One section per checklist item 1–14.
  3. "Maintenance and lifecycle".
  4. "Open checks on a workstation" (only if D5 = b).

- [ ] **Step 1:** Write the failing tests.
  - `tests/test_handover_doc.py`:
    - `CHECKLIST` holds the 14 German item texts verbatim from the guide's section 6, from "Repo-Link und CI-Pipeline, die getaggte, schlanke Images ohne Daten baut" to "Ansprechperson in T4 für Rückfragen und Updates".
    - `test_every_checklist_item_has_a_section` checks each text appears under a `##` heading.
    - `test_relative_links_resolve` checks every `](path)` and `](path#anchor)` resolves to a file in the repo.
    - `test_maintenance_section_exists`.
  - `tests/test_configuration_doc.py`:
    - `test_every_setting_is_documented_with_its_default`, which iterates `Settings.model_fields` against the env-name table rows.
    - `test_every_secret_is_in_the_secrets_table`.
  - In `test.sh`, the example file renders, with `valkey.enabled=false`, an `existingSecret`, a pinned tag and the rules on.
- [ ] **Step 2:** Run them. Expected: FAIL. If the configuration test reveals undocumented settings, document them: that is the point of the test.
- [ ] **Step 3:** Write the documents. The checklist answers point at existing material and do not copy it:
  - OpenAPI path and texts;
  - the ports;
  - headers and content types, with expected response times from the load test;
  - the job endpoints;
  - auth and LLM env vars, with the B-API readiness of `AUTH_MODE` and the header key;
  - metrics and alert rules;
  - the log format and trace headers;
  - state and scaling;
  - resources;
  - Valkey and the switchable components;
  - model volumes (sources and sizes);
  - env, values and secrets;
  - costs (P11-S6);
  - the contact person (D4).
- [ ] **Step 4:** Run the tests and `test.sh`. Expected: PASS.
- [ ] **Step 5:** Commit `docs: add the hand-over document, cost notes and an example production values file`.

### Task 7: Maintenance and release preparation

**Files:**
- Create: `.github/dependabot.yml` (ecosystems `uv`, `github-actions` and `docker`, at `/`; weekly; grouped minor and patch updates; at most 5 open PRs)
- Create: `CHANGELOG.md` (Keep a Changelog format; `## [1.0.0] - unreleased` with a summary of packages 0–11, or the D3 version)
- Modify: `pyproject.toml` `version`, `deploy/helm/text-extraction/Chart.yaml` `version` and `appVersion`, `src/text_extraction/__init__.py` `__version__` (the D3 version)
- Modify: `docs/handover.md` (the release process: tag `vX.Y.Z` on main, then the image workflow publishes; how to roll back)
- Test: `tests/test_release.py` (the three versions are equal; the CHANGELOG has a section for that version; `dependabot.yml` names the three ecosystems)

- [ ] **Step 1:** Write the failing tests. Run them. Expected: FAIL.
- [ ] **Step 2:** Implement. Run the tests, `uv lock --check` (the version change touches `uv.lock`; edit it by hand if the pytorch index is blocked, and CI's `uv sync --locked` validates it) and `bash deploy/helm/test.sh`.
- [ ] **Step 3:** Commit `chore: add Dependabot, a changelog and the release version`.

### Task 8: Done-when and live checks

**Files:**
- Modify: `docs/packages.md` (row 11 and its note, after the runs), `docs/architecture.md` ("Lessons" table: the 4,000-URL results next to the 2026-09-28 baselines)
- Test: everything (`uv run pytest -q`, ruff, format, mypy, `test.sh`)

- [ ] **Step 1 (controller):** Dispatch `live.yml` with `url_set: wlo_4000` for each mode D2 chose, one after another. Read each "Ok rate:" line, record the results in the ledger, then in `docs/architecture.md` and `docs/packages.md`.
- [ ] **Step 2 (controller, D5 = a):** Start a Remote Control session on Jan's PC in a fresh clone (`C:\Users\jan\te-pkg11-verify`), never in the staging folder. In the session:
  1. Run `docker compose up -d --wait`.
  2. Run the UI against three WLO pages and one WLO-type site.
  3. Connect Claude Desktop through `mcp-remote` and call `service_info` and `extract_url`.
  4. Run `docker compose down -v`.

  Record the results.
- [ ] **Step 3:** Check every checklist item in `docs/handover.md` against the merged state, and update the `docs/packages.md` row 11 done-when evidence.
- [ ] **Step 4:** Run the full verification. Commit `docs: record the package 11 runs and the hand-over evidence`.
- [ ] **Step 5 (controller, after the squash merge, D3 = a or b):** Tag the merge commit with `vX.Y.Z` and push the tag. Check that the image workflow published the SemVer tags for all five images.
