# Package 3: Jobs and Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Work that does not fit one request runs as a job:
- `POST /v1/batch` takes many URLs, and `GET /v1/jobs/{id}` reports status, progress and paged results.
- Extract workers process batches across pods with fair per-domain and per-client scheduling, never pushing a host over its outbound limit.
- `POST /v1/extract` hands over to a job when the caller's `wait_seconds` runs out.
- The Helm chart ships the worker Deployment, autoscaling on queue length (KEDA or HPA), alert rules, a Valkey password and a NetworkPolicy.

**Architecture:**
- **Two record types in Valkey:**
  - *Job records* (`jobs.py`): owner, options, URLs and item results, one hash tag per job.
  - *The fair queue* (`scheduler.py`): one hash tag per worker pool (`extract`).
- **How a worker claims an item:** it calls one Lua script. The script rotates clients, then the domains of that client, skips domains that are deferred or at their in-flight cap, and leases the next item with an expiry. An expired lease goes back to the front of its domain's list, so a killed worker never strands a batch.
- **The worker process** (`worker.py`, entry point `text-extraction-worker`) runs the existing fast `Pipeline` for each item. It defers an item when the outbound gate refuses the host. It serves `/health`, `/ready` and `/metrics` on 9090 like the API.
- **The sync-to-job fallback** keeps the extraction running in the API pod after `wait_seconds`. It answers 202 with a job and stores the result in that job when done.

**Tech Stack:** existing (FastAPI, redis-py asyncio against Valkey, Lua scripts, prometheus-client, structlog); dev: fakeredis[lua]. No new runtime dependency.

**Spec:**
- `docs/architecture.md`: Overview (worker pools through Valkey), Public API (`POST /v1/batch`, `GET /v1/jobs/{id}`, `wait_seconds`), Feature notes "Batch", Observability (queue length, job duration, alert rules), Auth (users see own jobs).
- `docs/packages.md` row 3.
- `CLAUDE.md` rules 2 and 4.

**Pending decision (Jan):** taskiq-redis 1.2.4 queues are plain FIFO. Its stream broker reclaims stale messages only when new ones arrive (`listen()` skips the reclaim when `xreadgroup` returns nothing). This plan therefore implements the fair queue itself and does **not** use Taskiq. Task 8 replaces "Taskiq on Valkey" in `CLAUDE.md`, `docs/architecture.md` and `docs/packages.md`. If Jan chooses Taskiq, Tasks 2 and 3 change and the rest stays.

## Global Constraints

- English only. Python 3.13. No new runtime dependency; anything added must be checked for licence (no AGPL/GPL).
- Ports: API public 8080 + internal 9090. The worker serves only 9090 (`/health`, `/ready`, `/metrics`) and never appears in the OpenAPI spec.
- Stateless pods: jobs, queue, leases and results live in Valkey with TTLs; nothing on local disk.
- Every new setting is an env var with a documented default in `docs/configuration.md` and `.env.example`, in the task that adds it (Helm `config` where the chart sets it).
- Every Valkey key a single Lua script touches shares one hash tag:
  - queue keys `q:{<pool>}:…`;
  - job keys `job:{<id>}:…`.
  No script touches both.
- Result envelope everywhere: job endpoints answer `Envelope[...]`. HTTP codes:
  - 202 for an accepted job;
  - 404 `not_found` for an unknown, expired or foreign job (never 403, so job ids leak nothing);
  - 429 `rate_limited` for too many open batches;
  - 503 when Valkey is down or jobs are disabled.
- Users see their own jobs only; admins see all (architecture "Auth").
- Never log URLs or query strings: host and `path_hash` at most (as in package 2). Job ids may be logged.
- The fast pipeline's behaviour for a plain `POST /v1/extract` without `wait_seconds` must not change (same answers, same cache, same metrics).
- Not in this package: browser, documents, ASR and NLP workers (packages 4-7 reuse the queue with their own pool names), job cancellation, job listing, S3 offload, webhooks (architecture: not needed).

## Rulings in this plan

- P1 (sync-to-job fallback): the extraction keeps running in the API pod after `wait_seconds`, and the job only stores its result. The fast pipeline ends within `EXTRACT_TIMEOUT_SECONDS` (25 s) anyway. A pod that dies mid-run leaves the job's lease to expire, and `GET /v1/jobs/{id}` then reports the item as `failed`/`internal_error`, retryable. Routing every extract through a worker would add a hop to every request and make workers mandatory. Package 4 sends browser work to its own pool.
- P2 (domain = host): the queue keys domains by `normalize_host(urlsplit(url).hostname)`, the same key the `DomainGate` uses. A URL without a valid host goes to the domain `-`.
- P3 (client = principal name): fair shares are per API key name; under `AUTH_MODE=none` everyone is `anonymous`, so batches then share by domain only.
- P4 (domain cap in the scheduler): a domain is skipped while its leased items reach `OUTBOUND_MAX_CONCURRENCY_PER_HOST`. That stops workers from claiming items they would only wait for. The `DomainGate` stays the authority on limits; the scheduler only avoids pointless waits.
- P5 (Valkey memory): Valkey is started with `--maxmemory` (`valkey.maxmemory`, default `768mb`) and `--maxmemory-policy noeviction`, and its memory limit rises to 1Gi. Eviction would drop gate, lease or job keys silently; noeviction fails writes loudly (503). Package 11 measures real sizes.
- P6 (result size): an item result whose JSON exceeds `JOB_ITEM_MAX_BYTES` is stored without `content`, with the warning `content_not_stored_too_large`. A later `POST /v1/extract` of the URL is a cache hit for `RESULT_CACHE_TTL_SECONDS`.

## Review Focus

1. A worker killed in the middle of an item (SIGKILL, OOM): the lease expires, another worker re-runs the item, and the job counts it once. After `JOB_ITEM_MAX_ATTEMPTS` lost leases the item ends `failed`/`internal_error`, retryable, and the job still completes. Owner: Tasks 2 and 3.
2. Client A submits 900 URLs of one host, then client B submits 10 URLs: B's items are claimed within the next few claims and are not queued behind A's. The same holds for a small domain inside a batch dominated by one host. Owner: Task 2.
3. A host that is paused (circuit open, 429 cooldown) or busy: its items are deferred until the retry time and do not hold worker slots. Other domains keep flowing, and an item deferred more than `JOB_ITEM_MAX_ATTEMPTS` times ends with the gate's error code. Owner: Tasks 2 and 3.
4. Valkey restarts or is unreachable mid-batch: workers back off and resume without crashing, `POST /v1/batch` answers 503, and `GET /v1/jobs/{id}` answers 503 (not 500). Owner: Tasks 3 and 4.
5. The same URL twice in one batch, an invalid URL in a batch, and a job read after its TTL: every index gets its own result (an invalid URL gives `failed`/`invalid_url`, not a rejected batch), and an expired job is 404. Owner: Task 4.

---

### Task 1: Job records

**Files:**
- Create: `src/text_extraction/jobs.py`, `src/text_extraction/job_models.py`, `tests/test_jobs.py`
- Modify: `settings.py`, `docs/configuration.md`, `.env.example`

**Interfaces:**
- Consumes: `Envelope`, `ExtractResult` and `ExtractRequest` (package 2); `Principal`.
- Produces:
  - `job_models.py`:
    - `JobKind = Literal["extract", "batch"]` and `JobState = Literal["queued", "running", "completed"]`.
    - `ItemState = Literal["pending", "done"]`.
    - `JobOptions(BaseModel, extra="forbid")`: `mode`, `format`, `metadata`, `links`, `cache` with the same types and defaults as `ExtractRequest`, plus `compliance: ComplianceMode` (already resolved, never None).
    - `JobProgress(BaseModel)`: `total`, `done`, `success`, `partial`, `failed`: int.
    - `JobItem(BaseModel)`: `index: int`, `url: str`, `state: ItemState`, `result: Envelope[ExtractResult] | None`.
    - `JobView(BaseModel)`: `id`, `kind`, `state`, `created_at: datetime`, `finished_at: datetime | None`, `progress`, `items: list[JobItem]`, `next_offset: int | None`.
    - `JobAccepted(BaseModel)`: `id`, `kind`, `state`, `total: int`, `status_url: str`.
  - `jobs.py`:
    - `class JobStore` with `__init__(valkey: Redis, settings: Settings, *, clock=time.time)`.
    - `async create(kind: JobKind, owner: Principal, options: JobOptions, urls: list[str]) -> str`: returns the id, `uuid4().hex`.
    - `async get(job_id: str) -> JobRecord | None`. `JobRecord` is a frozen dataclass: `id`, `kind`, `owner`, `owner_role`, `options`, `state`, `created_at`, `finished_at`, `progress`, `lease_until: float | None`.
    - `async url(job_id: str, index: int) -> str | None`.
    - `async items(job_id: str, offset: int, limit: int) -> list[JobItem]`.
    - `async store_result(job_id: str, index: int, envelope: Envelope[ExtractResult]) -> bool`: True when this call finished the index (first write); a repeated write of a finished index changes nothing and returns False.
    - `async mark_running(job_id: str, *, lease_seconds: float | None = None) -> None`.
    - `class JobLost(Exception)` for a job whose keys expired while it was being written.

Keys: `job:{<id>}:meta` (hash), `job:{<id>}:urls` (list), `job:{<id>}:results` (hash index→JSON) and `job:{<id>}:done` (bitmap). All get `EXPIRE JOB_TTL_SECONDS` at creation and again when the job completes.

`store_result` is one Lua script. It does `SETBIT done index 1`, and only when the old bit was 0 it:
- writes the result;
- increments `done` and the counter of the envelope's status;
- sets `state=completed`, `finished_at` and the TTL refresh when `done == total`.
So a re-run after a lost lease counts once.

- [ ] **Step 1: Write the failing tests** in `tests/test_jobs.py` (fakeredis with Lua):
  - `test_create_and_get_round_trip`: kind, owner, owner_role, options and `progress == JobProgress(total=3, done=0, success=0, partial=0, failed=0)`; `state == "queued"`.
  - `test_store_result_counts_once`: storing index 1 twice returns `True` then `False`, `progress.done == 1`, and the second envelope does not replace the first.
  - `test_last_result_completes_the_job`: `state == "completed"`, `finished_at` set, the keys' TTL reset to `JOB_TTL_SECONDS`.
  - `test_items_pages_in_index_order`: `offset=2, limit=2` of 5 gives indexes 2, 3 with `state` `pending` or `done` and `result` only when done.
  - `test_oversized_result_is_stored_without_content`: an envelope over `JOB_ITEM_MAX_BYTES` is stored with `data.content is None`, and `"content_not_stored_too_large"` is in `data.warnings` (P6).
  - `test_unknown_or_expired_job_is_none`.
  - `test_running_job_with_expired_lease` (P1): after `mark_running(id, lease_seconds=10)`, a store with a clock 11 s later shows `lease_until` in the past. `get` reports it unchanged; the API decides (Task 5).
  - `test_all_job_keys_share_one_hash_tag`.
- [ ] **Step 2:** Run `uv run pytest tests/test_jobs.py -q`; expect failures (module missing).
- [ ] **Step 3:** Implement `job_models.py` and `jobs.py`. Add the settings with `Field` bounds:
  - `JOB_TTL_SECONDS` (default 86400, ≥ 60);
  - `JOB_ITEM_MAX_BYTES` (default 2_000_000, ≥ 10_000).
  Document both.
- [ ] **Step 4:** Run the tests until green, then the full gates (`uv run pytest -q`, `ruff check .`, `ruff format --check .`, `mypy`).
- [ ] **Step 5:** Commit `feat: add job records in Valkey`.

### Task 2: Fair queue with leases

**Files:**
- Create: `src/text_extraction/scheduler.py`, `tests/test_scheduler.py`
- Modify: `settings.py`, `docs/configuration.md`, `.env.example`

**Interfaces:**
- Consumes: `normalize_host` (outbound.py); setting `outbound_max_concurrency_per_host`.
- Produces:
  - `@dataclass(frozen=True) class QueueItem`: `job_id: str`, `index: int`, `client: str`, `domain: str`, `attempts: int`.
  - `@dataclass(frozen=True) class Claim`: `item: QueueItem | None`, `exhausted: list[QueueItem]` (leases lost more than `job_item_max_attempts` times; the caller fails them), `retry_in: float | None` (seconds until the earliest deferred domain frees, when nothing was claimable).
  - `def domain_of(url: str) -> str` (P2).
  - `class FairQueue` with `__init__(valkey: Redis, settings: Settings, pool: str = "extract", *, clock=time.time)` and these methods:
    - `async enqueue(job_id: str, client: str, urls: list[str]) -> None`: in index order, in chunks of 500 per script call.
    - `async claim(lease_seconds: float) -> Claim`: one Lua call.
    - `async complete(item: QueueItem) -> bool`: False when the lease had already expired and was re-queued; the caller still stores the result, and Task 1's `store_result` dedupes.
    - `async defer(item: QueueItem, until: float) -> None`: back to the front of its domain list, `attempts + 1`, domain not-before = max(existing, until), lease removed.
    - `async counts() -> QueueCounts`: `pending`, `leased`, `clients`, `domains`: int.

Keys, all under `q:{<pool>}:`:
- `clients` (list, rotation);
- `c:<client>:domains` (list, rotation);
- `c:<client>:d:<domain>` (list of `"<job_id>:<index>:<attempts>"`);
- `notbefore` (hash domain → epoch seconds);
- `running` (hash domain → leased count);
- `leases` (zset member → lease expiry);
- `leased` (hash member → `client\tdomain`);
- `pending` (counter).

The `claim` script works in this order:
1. It reclaims up to 20 expired leases. Each goes back to the front of its list with `attempts + 1`, re-entering the rotations when missing. A lease past `job_item_max_attempts` goes into `exhausted` instead.
2. It rotates `clients` once per client (`LMOVE … LEFT RIGHT`). For each client it rotates that client's `domains` once per domain, skipping a domain whose `notbefore` is in the future or whose `running` count is at the per-host cap (P4).
3. It pops the first claimable item and leases it. A domain list that becomes empty leaves the client's rotation, and a client without domains leaves `clients`.
4. It returns at most one item.

- [ ] **Step 1: Write the failing tests** in `tests/test_scheduler.py` (fakeredis with Lua, an injected clock):
  - `test_claims_rotate_between_clients`: A has 100 items of one host, B then gets 10. The first 20 claims alternate A, B, A, B … until B is empty (Review Focus 2).
  - `test_claims_rotate_between_domains_of_one_client`: 6 items on `a.example` and 2 on `b.example` give the order a, b, a, b, a, a …
  - `test_domain_at_its_cap_is_skipped`: with cap 2 and two leases on `a.example`, the next claim returns the `b.example` item; with nothing else queued it returns `item=None`.
  - `test_deferred_domain_waits_until_not_before`: `defer(until=now+30)` hides the domain. Claims return other domains, then `item=None` with `retry_in == 30`. At `now+30` the item comes back with `attempts == 1`.
  - `test_expired_lease_is_reclaimed_first_in_line`: after `lease_seconds` pass without `complete`, the next claim returns the same item with `attempts == 1`.
  - `test_lost_leases_beyond_max_attempts_are_exhausted`: the item appears in `Claim.exhausted` and is not claimable again (Review Focus 1).
  - `test_complete_after_expiry_returns_false`.
  - `test_counts`.
  - `test_invalid_url_goes_to_the_dash_domain`.
  - `test_all_queue_keys_share_one_hash_tag` (Global Constraints).
- [ ] **Step 2:** Run `uv run pytest tests/test_scheduler.py -q`; expect failures.
- [ ] **Step 3:** Implement `scheduler.py`. The Lua scripts are module constants registered once per `FairQueue`, following the style of `outbound.py`. Add the setting `JOB_ITEM_MAX_ATTEMPTS` (default 5, ≥ 1) for lost leases plus deferrals, and document it.
- [ ] **Step 4:** Run the tests until green, then the full gates.
- [ ] **Step 5:** Commit `feat: add fair job queue with leases`.

### Task 3: Extract worker

**Files:**
- Create: `src/text_extraction/worker.py`, `tests/test_worker.py`
- Modify: `pipeline.py` (gate refusal option), `pyproject.toml` (`[project.scripts] text-extraction-worker = "text_extraction.worker:main"`), `settings.py`, `docs/configuration.md`, `.env.example`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `JobStore` (Task 1), `FairQueue`/`QueueItem`/`Claim` (Task 2), `Pipeline`, `create_pipeline`, `Fetcher`, `ConversionPool`, `create_internal_app`/`Readiness`, `_server`/`graceful_shutdown_seconds` patterns from `server.py`.
- Produces:
  - `Pipeline.run(req, principal, *, gate_refusals: Literal["fail", "raise"] = "fail")`. With `"raise"`, `OutboundBusy` and `CircuitOpen` propagate unchanged instead of becoming `rate_limited` / `connection_error` envelopes. The default keeps package 2's behaviour exactly.
  - `class Worker` with `__init__(settings, valkey, pipeline, store: JobStore, queue: FairQueue, registry)`.
  - `Worker.async run(stop: asyncio.Event) -> None`: runs `WORKER_CONCURRENCY` slot loops until `stop` is set and in-flight items have finished.
  - `Worker.async process(item: QueueItem) -> None`: one item, used by tests.
  - `def main() -> None`: the worker process. It parses settings like `server.main`, serves the internal app on `INTERNAL_PORT` with `/ready` = Valkey answers and not draining, and handles SIGTERM in three steps: stop claiming, let in-flight items finish within `graceful_shutdown_seconds`, then exit. A Readiness without a public listener is marked listening once the loops start.
  - Metrics, registered in the worker's registry:
    - `text_extraction_job_items_total{outcome}`: outcome = envelope `error.code` or `success`, plus `deferred` and `lost`;
    - `text_extraction_job_item_seconds` (histogram);
    - `text_extraction_worker_busy_slots` (gauge);
    - `text_extraction_job_duration_seconds{kind}` (histogram), observed by the worker whose `store_result` completed the job.

A slot loop works like this:
1. It calls `claim(lease_seconds = EXTRACT_TIMEOUT_SECONDS * 2 + 10)` and fails each `exhausted` item with `failed`/`internal_error`, retryable, message "The item was lost by a worker too often."
2. With no item, it sleeps `min(retry_in or 1.0, 1.0)` with ±50 % jitter.
3. With an item, it rebuilds the request: `ExtractRequest(url=…, **options)` with `compliance=options.compliance` and `Principal(owner, owner_role, None)`. It calls `pipeline.run(…, gate_refusals="raise")`.
4. On `OutboundBusy`/`CircuitOpen` it calls `defer(item, now + retry_after)`. An item already at `job_item_max_attempts` is instead stored as the envelope the pipeline would have given (`rate_limited` or `connection_error`, retryable) (Review Focus 3).
5. Otherwise it calls `store_result`, then `complete`.
6. On `RedisError`/`OSError` around claim, store or complete, it logs `worker_valkey_unavailable` and backs off for 1, 2, 4 … up to 30 s, without crashing (Review Focus 4).
7. When a job's URL cannot be read (the job expired), it calls `complete` and drops the item.

- [ ] **Step 1: Write the failing tests:**
  - `tests/test_pipeline.py::test_gate_refusals_raise_when_asked`: with `RefusingGate`, `gate_refusals="raise"` raises `OutboundBusy`; the default still answers `rate_limited`.
  - `tests/test_worker.py`, with `FakeFetcher` from `tests/fakes.py`, fakeredis and an injected clock:
    - `test_worker_processes_a_batch_and_completes_the_job`: 5 URLs, all `success`, job `completed`.
    - `test_gate_refusal_defers_the_item`: the item is re-queued with `attempts == 1`, the domain is not-before `now + retry_after`, and no result is stored.
    - `test_item_deferred_too_often_fails_with_the_gate_code`.
    - `test_exhausted_items_fail_with_internal_error`.
    - `test_rerun_after_lost_lease_counts_once` (Review Focus 1).
    - `test_valkey_outage_backs_off_and_recovers`: a fake raises `ConnectionError` for 3 calls, then works; the batch completes.
    - `test_stop_finishes_in_flight_items_and_claims_no_more`.
    - `test_worker_ready_endpoint` (503 before the loops start, 200 while they run, 503 after stop).
    - `test_metrics_count_outcomes`.
- [ ] **Step 2:** Run `uv run pytest tests/test_worker.py tests/test_pipeline.py -q`; expect failures.
- [ ] **Step 3:** Implement the pipeline option and `worker.py`. Add the setting `WORKER_CONCURRENCY` (default 8, ≥ 1; items processed at once per worker pod; conversions are still capped by `CONVERT_CONCURRENCY`) and document it.
- [ ] **Step 4:** Run the tests until green, then the full gates.
- [ ] **Step 5:** Commit `feat: add extract worker process`.

### Task 4: Batch and job API

**Files:**
- Create: `src/text_extraction/jobs_api.py`, `tests/test_jobs_api.py`
- Modify: `app.py` (router, `JobStore`/`FairQueue` on `app.state`), `job_models.py` (`BatchRequest`), `settings.py`, `docs/configuration.md`, `.env.example`, `docs/architecture.md` (Public API rows)

**Interfaces:**
- Consumes: Tasks 1 and 2; `resolve_mode`; `require_role("user")`; `rate_limited`.
- Produces:
  - `BatchRequest(BaseModel, extra="forbid")`: `urls: list[str]`, 1 to `BATCH_MAX_URLS` items, each 1 to `MAX_URL_LENGTH` characters, checked in a validator so the limit follows the setting. It also takes the `ExtractRequest` options `mode`, `format`, `metadata`, `links`, `compliance`, `cache` with the same defaults.
  - `POST /v1/batch` → 202 `Envelope[JobAccepted]`. Steps:
    1. Resolve the compliance mode (a 403 as in `/v1/extract`).
    2. Refuse a client with `BATCH_MAX_OPEN_PER_CLIENT` unfinished batches with 429 `rate_limited`, retryable. Count via a per-owner set `jobs:{owner:<name>}:open` of job ids, cleaned of finished or expired ids on each check.
    3. Create the job, then enqueue it. A Valkey failure in either answers 503 `internal_error`, retryable; a job created but not enqueued is deleted.
    4. `status_url = "/v1/jobs/<id>"`.
  - `GET /v1/jobs/{job_id}?offset=0&limit=100` → 200 `Envelope[JobView]`. `limit` is 1 to 500 and `offset` at least 0. `next_offset` is set while `offset + limit < total`.
    - Envelope status: `success` while queued or running and when all items succeeded; `partial` when the job completed with some failed or partial items; `failed` when every item failed.
    - Unknown id, expired job, or another user's job (admins see all) → 404 `not_found`.
    - A malformed id (not 32 lowercase hex) → 404 as well.
  - `JOBS_ENABLED` (default `true`): `false` makes both routes answer 503 `internal_error` with the message "Jobs are disabled on this deployment.", for API pods without workers.

- [ ] **Step 1: Write the failing tests** in `tests/test_jobs_api.py` (TestClient, fakeredis, `AUTH_MODE=api_key` with a user key A, a user key B and an admin key):
  - `test_batch_returns_202_and_job_reports_progress`.
  - `test_duplicate_and_invalid_urls_get_their_own_items`: enqueued as separate indexes; run one worker pass (Task 3 `Worker.process`) and expect `failed`/`invalid_url` at the invalid index (Review Focus 5).
  - `test_batch_size_limit_is_422`.
  - `test_foreign_job_is_404_admin_sees_it`.
  - `test_expired_job_is_404`.
  - `test_paging_and_next_offset`.
  - `test_open_batch_limit_is_429`.
  - `test_ignore_compliance_needs_admin_403`.
  - `test_valkey_down_is_503_for_create_and_get` (Review Focus 4).
  - `test_jobs_disabled_is_503`.
  - `test_job_routes_in_openapi_internal_routes_not`.
- [ ] **Step 2:** Run `uv run pytest tests/test_jobs_api.py -q`; expect failures.
- [ ] **Step 3:** Implement `jobs_api.py`. Add the settings and document them:
  - `BATCH_MAX_URLS` (default 1000, 1 to 100000);
  - `BATCH_MAX_OPEN_PER_CLIENT` (default 5, ≥ 1);
  - `JOBS_ENABLED`.
- [ ] **Step 4:** Run the tests until green, then the full gates.
- [ ] **Step 5:** Commit `feat: add POST /v1/batch and GET /v1/jobs/{id}`.

### Task 5: Sync-to-job fallback for `POST /v1/extract`

**Files:**
- Modify: `extract_models.py` (`wait_seconds`), `extract_api.py`, `app.py` (lifespan waits for background extractions), `jobs_api.py` (lease-expired view), `tests/test_extract_api.py`, `tests/test_jobs_api.py`, `docs/architecture.md` (P1)

**Interfaces:**
- Consumes: `JobStore.create`/`mark_running`/`store_result` (Task 1), the job routes (Task 4).
- Produces:
  - `ExtractRequest.wait_seconds: float | None = Field(default=None, ge=0, le=60)`. `None` keeps package 2's path unchanged; the route never creates a job then.
  - With a value, the route runs `pipeline.run` as a task and waits `wait_seconds` (shielded):
    - Done in time: the usual 200 answer.
    - Otherwise:
      1. It creates an `extract` job with one URL and `mark_running(lease_seconds = EXTRACT_TIMEOUT_SECONDS + 10)`.
      2. It answers 202 `Envelope[JobAccepted]`.
      3. A done-callback stores the envelope as item 0. An exception in the task is stored as a `failed`/`internal_error` envelope.
  - `app.state.background: set[asyncio.Task]`. The lifespan shutdown awaits them up to `EXTRACT_TIMEOUT_SECONDS + 5`.
  - `GET /v1/jobs/{id}` for a running job whose `lease_until` has passed shows item 0 as `done` with a synthetic `failed`/`internal_error`, retryable, "The pod running this extraction stopped." This is not stored (P1).
  - With `JOBS_ENABLED=false`, `wait_seconds` is ignored and the request runs inline to its end.

- [ ] **Step 1: Write the failing tests:**
  - `test_extract_without_wait_seconds_is_unchanged` (same body as before; no job keys in Valkey).
  - `test_extract_finishing_within_wait_is_200`.
  - `test_slow_extract_hands_over_to_a_job` (a pipeline stub that sleeps 0.5 s with `wait_seconds=0.1` → 202; the job then completes with the envelope).
  - `test_wait_seconds_zero_is_immediate_202`.
  - `test_lost_running_job_reports_internal_error`.
  - `test_shutdown_waits_for_background_extractions`.
  - `test_jobs_disabled_ignores_wait_seconds`.
- [ ] **Step 2:** Run `uv run pytest tests/test_extract_api.py tests/test_jobs_api.py -q`; expect failures.
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run the tests until green, then the full gates.
- [ ] **Step 5:** Commit `feat: hand slow extractions over to a job`.

### Task 6: Queue metrics on the API pods

**Files:**
- Create: `src/text_extraction/queue_metrics.py`, `tests/test_queue_metrics.py`
- Modify: `app.py` (start the refresh task in `_lifespan`, register the collector in the app's registry), `docs/configuration.md` (metrics list)

**Interfaces:**
- Consumes: `FairQueue.counts()` (Task 2).
- Produces:
  - `class QueueGauges`: a cached reader that refreshes `FairQueue.counts()` at most every 5 s from a background task started in the public app's lifespan. A collector reads the last values, because Prometheus collectors are synchronous.
  - `text_extraction_queue_pending_items{pool}`, `text_extraction_queue_leased_items{pool}` and `text_extraction_queue_clients{pool}`.
  - `text_extraction_jobs_created_total{kind}`.

  API pods export the queue gauges, not workers, because workers may scale to zero and KEDA must still see the backlog. Queries use `max()` over pods.
- [ ] **Step 1: Write the failing tests:** `test_gauges_follow_the_queue`, `test_valkey_down_keeps_last_values_and_logs_once`, `test_jobs_created_counter`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests until green, then the full gates.
- [ ] **Step 5:** Commit `feat: export queue metrics`.

### Task 7: Helm, compose and Valkey hardening

**Files:**
- Create:
  - `deploy/helm/text-extraction/templates/worker-deployment.yaml`
  - `worker-service-internal.yaml`
  - `worker-scaledobject.yaml` (KEDA)
  - `worker-hpa.yaml`
  - `networkpolicy.yaml`
  - `valkey-secret.yaml`
- Modify: `values.yaml`, `README.md` (chart), `prometheusrule.yaml`, `servicemonitor.yaml` (scrape workers too), `valkey.yaml` (password, maxmemory), `deployment.yaml` (`VALKEY_PASSWORD` env + URL), `_helpers.tpl`, `docker-compose.yml` (worker service, Valkey password), `.env.example`, `docs/configuration.md`

**Interfaces / values:**
- `workers.extract`:
  - `enabled: true`, `replicaCount: 2`;
  - `concurrency: 8` (→ `WORKER_CONCURRENCY`);
  - `resources` with a 1Gi memory limit and the same comment as the API;
  - `terminationGracePeriodSeconds: 45`.
- Autoscaling (`workers.extract.autoscaling`):
  - `keda.enabled: false`, with `serverAddress`, `threshold: 50`, `minReplicaCount: 1`, `maxReplicaCount: 10`. The ScaledObject has a Prometheus trigger on `max(text_extraction_queue_pending_items{pool="extract", namespace="<ns>"})`.
  - `hpa.enabled: false` (CPU 70 %), mutually exclusive with KEDA: rendering both fails with `fail "…"`.
- Worker pods run `text-extraction-worker`. They have probes on 9090 (`/health`, `/ready`), an internal Service, and use the same ConfigMap and secrets as the API.
- Valkey auth:
  - `valkey.auth.enabled: true`, `valkey.auth.existingSecret: ""`, `valkey.auth.existingSecretKey: VALKEY_PASSWORD`.
  - The generated password is kept across upgrades with `lookup`.
  - Valkey starts with `--requirepass "$(VALKEY_PASSWORD)"`.
  - API and worker get `VALKEY_PASSWORD` from the secret and `VALKEY_URL=redis://:$(VALKEY_PASSWORD)@<valkey>:6379/0` through Kubernetes env expansion.
  - `valkey.maxmemory: 768mb` and `--maxmemory-policy noeviction` (P5); the Valkey memory limit rises to 1Gi.
- `networkPolicy.enabled: false`. When on:
  - Valkey accepts only API and worker pods of this release.
  - The API accepts 8080 from anywhere and 9090 from `networkPolicy.metricsFrom` (a list of peers, default: same namespace).
  - Workers accept only 9090 from `metricsFrom`.
- Alert rules (in `prometheusrule.yaml`):
  - `TextExtractionQueueBacklog`: pending > `alerts.queueBacklogItems` (1000) for 15 m while completions are 0.
  - `TextExtractionWorkersDown`: pending > 0 and no available worker replicas for 5 m.
  - `TextExtractionJobItemFailures`: the failed share of job items > 50 % for 15 m.
- Compose:
  - a `worker` service with the same image and command `text-extraction-worker`, `INTERNAL_PORT` 9090 not published, healthcheck on `/ready`, `stop_grace_period: 45s`;
  - Valkey with `--requirepass ${VALKEY_PASSWORD}` from `.env` (the `.env.example` entry is marked secret);
  - URLs built from it.

- [ ] **Step 1:** Run `helm template` with the defaults, then with `workers.extract.autoscaling.keda.enabled=true`, `networkPolicy.enabled=true` and `valkey.auth.existingSecret=x`. Expect the new objects to be missing (fail).
- [ ] **Step 2:** Implement the templates and values.
- [ ] **Step 3:** Verify:
  - `helm lint deploy/helm/text-extraction` passes. Build helm with `go install helm.sh/helm/v3/cmd/helm@v3.19.0` if the Docker command in CLAUDE.md is unavailable.
  - The renders show the worker Deployment, the KEDA ScaledObject, the NetworkPolicies and the Valkey password env.
  - Enabling both KEDA and HPA fails.
  - `docker compose config` is valid.
- [ ] **Step 4:** Commit `feat: ship workers, autoscaling, alerts and Valkey auth in Helm and compose`.

### Task 8: End-to-end batch run and docs

**Files:**
- Create: `tests/e2e/__init__.py`, `tests/e2e/site_farm.py`, `tests/e2e/run_batch.py`, `.github/workflows/e2e.yml`
- Modify: `pyproject.toml` (pytest ignores `tests/e2e`; ruff and mypy cover it), `docs/architecture.md` (Overview, Batch feature note, Public API rows, P1-P6), `docs/packages.md` (row 3 contents: "fair job queue on Valkey (own scheduler with leases)" instead of "Taskiq on Valkey"), `CLAUDE.md` (stack line), `README.md` (batch example)

**Interfaces:**
- `site_farm.py`: a threaded HTTP server per loopback address `127.0.0.2` … `127.0.0.11`, all on one port. Each serves `/p/<n>.html` (a 600-word article, 50-200 ms random delay) and a 404 `robots.txt`. It records per host every request's start and end (monotonic) in memory and dumps them as JSON to a file on SIGTERM.
- `run_batch.py`:
  1. Starts the farm, a local `redis-server` (or `VALKEY_URL` from env), one API process and **two worker processes** with distinct `INTERNAL_PORT`s. The env is `AUTH_MODE=none`, `FETCH_ALLOW_PRIVATE_NETWORKS=true`, `COMPLIANCE_DEFAULT=report`, `OUTBOUND_MIN_DELAY_MS=200`, `OUTBOUND_MAX_CONCURRENCY_PER_HOST=2`, `WORKER_CONCURRENCY=8`, `RATE_LIMIT_ENABLED=false`.
  2. Waits for every `/ready`.
  3. Submits **1,000 URLs**: 400 on one host and 600 spread over the other nine, plus a second client of 20 URLs submitted after the first batch.
  4. Polls `GET /v1/jobs/{id}` until both jobs complete, timeout 15 min.
  5. Asserts:
     - both jobs `completed`, with 1,000 + 20 results and 0 `failed`;
     - per host, at most 2 requests overlapped;
     - consecutive request starts per host are ≥ 180 ms apart (200 ms minus 10 % clock slack);
     - each worker's `text_extraction_job_items_total` is > 0, read from its 9090 `/metrics`;
     - the 20-URL job finished before the 1,000-URL job (fair shares).
  6. Prints a summary table, writes it to `--summary-out`, and exits 1 on any failed assertion.
- `e2e.yml`: runs on pull_request and on push to main, `timeout-minutes: 20`, with a Valkey service container. It runs `uv run python -m tests.e2e.run_batch --summary-out …` and uploads the logs and the farm record.

- [ ] **Step 1:** Write `tests/test_e2e_assertions.py` (normal suite): unit tests for the overlap and spacing checks in `run_batch.py` on synthetic records, one passing and one violating case each.
- [ ] **Step 2:** Implement the farm, the runner and the workflow. Run it locally with `redis-server`: it must pass (the done-when criterion).
- [ ] **Step 3:** Update the docs listed above, recording the rulings P1-P6 and the scheduler design in `docs/architecture.md` (CLAUDE.md: design answers go there). Run actionlint on `e2e.yml` and the full gates.
- [ ] **Step 4:** Commit `test: add end-to-end batch run across two workers`.
- [ ] **Step 5: Verify on GitHub:** the e2e workflow is green on the PR, and the summary table goes into the PR description.

---

Deferred to later packages (owner):
- browser, docling, nlp and asr pools on the same queue (4-7);
- job cancellation and listing (on request);
- S3 offload of large results (`S3_ENABLED`, later);
- `GET /v1/jobs/{id}/artifacts/{name}` (with screenshots, package 4);
- per-client weights.
