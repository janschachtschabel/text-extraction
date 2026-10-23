# Package 1: Service Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable, ops-guide-compliant FastAPI service with no extraction yet: env settings, public port 8080 and internal port 9090, JSON logs with trace ids, the result envelope and error codes, API keys with user/admin roles, an inbound rate limit in Valkey, a Docker image, docker-compose with Valkey, a Helm chart with toggles, and CI that builds tagged images and lints the chart.

**Architecture:** One process runs two uvicorn servers on one event loop: the public app (`/v1/...`, OpenAPI) and the internal app (`/health`, `/ready`, `/metrics`, no OpenAPI). Cross-cutting concerns are small modules wired in an app factory. Valkey is reached through the `redis` asyncio client (Redis protocol), so any Redis-compatible server works.

**Tech Stack:** FastAPI, uvicorn, pydantic-settings, structlog, prometheus-client, prometheus-fastapi-instrumentator, redis (asyncio), fakeredis (tests), httpx (tests), Docker, Helm 3, GitHub Actions.

**Spec:** `docs/architecture.md`, `docs/packages.md` (row 1), `CLAUDE.md`

## Global Constraints

- English only. Python 3.13. Licence Apache-2.0. No AGPL/GPL dependencies.
- Public API on port `8080`; internal `9090` serves `/health`, `/ready`, `/metrics`. Internal paths never appear in the OpenAPI spec and `/metrics` is never reachable on 8080.
- Stateless: all shared state (rate-limit counters) in Valkey. No SQLite, no local state files.
- Every setting is an env var with a documented default; secrets listed separately.
- Logs: JSON on stdout by default, `LOG_FORMAT=console` for local dev, `LOG_LEVEL` env. `x-client-trace-id` and B3 headers (`b3`, `x-b3-traceid`, `x-b3-spanid`, `x-b3-parentspanid`, `x-b3-sampled`) are read and logged on every line of the request; query strings are never logged. Ruling: these headers are not echoed back (ops guide: no response headers by default).
- `AUTH_MODE=api_key|none`; keys sent as `Authorization: Bearer <key>`; roles `user`, `admin`.
- Result envelope fields and error codes as in `docs/architecture.md`, plus `invalid_request`, `unauthorized`, `forbidden`, `not_found` (ruling: the service needs codes for its own 4xx answers).
- Images: multi-stage, `python:3.13-slim`, non-root, no models or data; production uses SemVer tags, `latest` only from `main` (staging).
- Helm: optional parts switchable (`valkey.enabled` with `externalValkey.url`), ingress exposes only port 8080.

## Review Focus

1. A request without or with a wrong key on `AUTH_MODE=api_key` → 401 envelope, never a stack trace; a `user` key on an admin route → 403.
2. Valkey down → `/ready` returns 503 while `/health` stays 200; a rate-limited route answers 503 `internal_error` with `retryable: true` instead of crashing.
3. Malformed `API_KEYS` JSON, or `AUTH_MODE=api_key` with no keys → the service refuses to start with a clear message (exit 2), never a half-open service.
4. Validation errors (FastAPI 422) come back in the envelope (`invalid_request`), with field paths, not FastAPI's default body.
5. Trace id: a client-supplied `x-client-trace-id` appears unchanged in the access log line; when absent, a generated one appears; an oversized or non-printable value is replaced, not logged raw.

---

### Task 1: Settings

**Files:** Create `src/text_extraction/settings.py`, `tests/test_settings.py`; modify `pyproject.toml` (runtime deps: `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `structlog`, `prometheus-client`, `prometheus-fastapi-instrumentator`, `redis`; dev: `httpx`, `fakeredis`).

**Interfaces — Produces:**
- `class ApiKey(BaseModel)`: `name: str`, `role: Literal["user", "admin"]`, `sha256: str` (hex of the key), `rate_limit_per_minute: int | None = None`.
- `class Settings(BaseSettings)` (no env prefix, case-insensitive): `public_host: str = "0.0.0.0"`, `public_port: int = 8080`, `internal_host: str = "0.0.0.0"`, `internal_port: int = 9090`, `log_level: str = "INFO"`, `log_format: Literal["json", "console"] = "json"`, `auth_mode: Literal["api_key", "none"] = "api_key"`, `api_keys: list[ApiKey] = []` (env `API_KEYS`, JSON), `api_keys_file: Path | None = None` (env `API_KEYS_FILE`, JSON file, merged with `api_keys`), `valkey_url: str = "redis://localhost:6379/0"`, `rate_limit_per_minute: int = 60`, `rate_limit_enabled: bool = True`.
- `def get_settings() -> Settings` (cached).
- Validation: `auth_mode == "api_key"` with no keys raises `ValueError("AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE")`; `sha256` must be 64 lowercase hex chars; key names unique.

- [ ] **Step 1: Failing tests** `test_defaults` (ports 8080/9090, json, api_key mode with one key from env), `test_api_keys_from_json_env`, `test_api_keys_file_merged`, `test_api_key_mode_without_keys_fails`, `test_invalid_sha256_rejected`, `test_duplicate_key_names_rejected`, `test_auth_none_needs_no_keys`. Use `monkeypatch.setenv`.
- [ ] **Step 2:** Run, see FAIL. **Step 3:** Implement. **Step 4:** Run, see PASS; run all gates.
- [ ] **Step 5: Commit** `feat: add environment settings`.

### Task 2: Envelope and errors

**Files:** Create `src/text_extraction/envelope.py`, `src/text_extraction/errors.py`, `tests/test_errors.py`.

**Interfaces — Produces:**
- `class ErrorCode(StrEnum)` with every code in `docs/architecture.md` plus `invalid_request`, `unauthorized`, `forbidden`, `not_found`.
- `class ErrorInfo(BaseModel)`: `code: ErrorCode`, `message: str`, `retryable: bool = False`, `details: list[dict[str, Any]] | None = None`.
- `class Envelope(BaseModel, Generic[T])`: `status: Literal["success", "partial", "failed"]`, `data: T | None = None`, `error: ErrorInfo | None = None`, `trace_id: str | None = None`. Extraction fields (`mode_used`, `timings`, …) arrive with package 2; YAGNI here.
- `class ServiceError(Exception)`: `(code: ErrorCode, message: str, http_status: int, retryable: bool = False)`.
- `def install_error_handlers(app: FastAPI) -> None`: maps `ServiceError` → its status; `RequestValidationError` → 422 `invalid_request` with `details` = list of `{"loc": [...], "msg": ...}`; Starlette `HTTPException` 404/405 → `not_found` / `invalid_request`; any other exception → 500 `internal_error` (logged with traceback, message generic). All bodies are `Envelope(status="failed")` with `trace_id` from the request context (Task 3 provides `current_trace_id()`; until then `None`).

- [ ] Steps: failing tests on a tiny FastAPI app (`test_service_error_envelope`, `test_validation_error_envelope_has_field_paths`, `test_unknown_route_is_not_found`, `test_unhandled_exception_is_generic_500`), implement, pass, gates, commit `feat: add result envelope and error handlers`.

### Task 3: Logging and trace context

**Files:** Create `src/text_extraction/logging.py`, `src/text_extraction/tracing.py`, `tests/test_tracing.py`; modify `errors.py` to fill `trace_id`.

**Interfaces — Produces:**
- `def configure_logging(level: str, fmt: Literal["json", "console"]) -> None` (structlog + stdlib, stdout only; uvicorn loggers routed through it; uvicorn's own access log disabled).
- `class TraceMiddleware` (pure ASGI): reads `x-client-trace-id` (accept `^[A-Za-z0-9._:-]{1,128}$`, else generate `uuid4().hex`), the B3 headers listed in Global Constraints (same validation), binds them to structlog contextvars, and logs one access line per request: `method`, `path` (no query), `status`, `duration_ms`, `trace_id`, B3 fields present.
- `def current_trace_id() -> str | None`.

- [ ] Steps: failing tests with `structlog.testing.capture_logs` (`test_client_trace_id_logged_unchanged`, `test_trace_id_generated_when_missing`, `test_invalid_trace_id_replaced`, `test_b3_headers_logged`, `test_query_string_not_logged`, `test_error_envelope_carries_trace_id`, `test_trace_header_not_echoed`), implement, pass, gates, commit `feat: add JSON logging and trace context`.

### Task 4: API keys and roles

**Files:** Create `src/text_extraction/auth.py`, `tests/test_auth.py`.

**Interfaces — Consumes:** `Settings`, `ApiKey`, `ServiceError`. **Produces:**
- `@dataclass(frozen=True) class Principal`: `name: str`, `role: Literal["user", "admin", "anonymous"]`, `rate_limit_per_minute: int | None`.
- `async def authenticate(request: Request) -> Principal` (FastAPI dependency): `AUTH_MODE=none` → `Principal("anonymous", "admin", None)` (ruling: behind the B-API the gateway authorises); else parse `Authorization: Bearer`, sha256 the key, compare with `hmac.compare_digest` against every configured hash; missing/invalid → `ServiceError(unauthorized, 401)`.
- `def require_role(role: Literal["user", "admin"]) -> Callable[..., Awaitable[Principal]]`: admin passes everywhere, user fails admin routes with `ServiceError(forbidden, 403)`.
- The key itself is never logged; the principal name is bound to the log context.

- [ ] Steps: failing tests (`test_missing_header_401`, `test_wrong_scheme_401`, `test_unknown_key_401`, `test_user_key_ok`, `test_user_key_on_admin_route_403`, `test_admin_key_on_admin_route_ok`, `test_auth_none_allows_all`, `test_key_not_in_logs`), implement, pass, gates, commit `feat: add API key authentication with roles`.

### Task 5: Inbound rate limit

**Files:** Create `src/text_extraction/valkey.py`, `src/text_extraction/ratelimit.py`, `tests/test_ratelimit.py`.

**Interfaces — Produces:**
- `def create_valkey(url: str) -> redis.asyncio.Redis` and `async def valkey_ping(client) -> bool` (False on any connection error, never raises).
- `class RateLimiter`: `__init__(client, default_per_minute: int)`; `async def hit(principal_name: str, limit: int) -> RateDecision` using a fixed 60 s window: key `rl:{name}:{epoch_minute}`, `INCR` + `EXPIRE 60` in one pipeline. `RateDecision(allowed: bool, remaining: int, retry_after: int)`.
- `def rate_limited(...)` dependency, depending on `authenticate` so it always runs after it: uses the principal's own limit or the default; disabled when `RATE_LIMIT_ENABLED=false`; denied → `ServiceError(rate_limited, 429, retryable=True)` with `Retry-After` header (ruling: 429 needs it); Valkey unreachable → `ServiceError(internal_error, 503, retryable=True)`. With `AUTH_MODE=none` all callers share the `anonymous` bucket (ruling: the gateway limits per caller there).

- [ ] Steps: failing tests with `fakeredis.aioredis.FakeRedis` (`test_allows_up_to_limit`, `test_blocks_over_limit_with_retry_after`, `test_per_key_limit_overrides_default`, `test_disabled_never_blocks`, `test_valkey_down_is_503_retryable`), implement, pass, gates, commit `feat: add inbound rate limit in Valkey`.

### Task 6: Apps and two-port server

**Files:** Create `src/text_extraction/app.py`, `src/text_extraction/internal.py`, `src/text_extraction/server.py`, `tests/test_app.py`; modify `pyproject.toml` (`[project.scripts] text-extraction = "text_extraction.server:main"`).

**Interfaces — Produces:**
- `def create_public_app(settings: Settings, valkey: Redis) -> FastAPI`: title `Text Extraction API`, version `__version__`, description (end-user text: what the service does, from README's first paragraph), `GET /v1/info` (requires `user`, rate limited) → `Envelope[InfoData]` with `name`, `version`, `principal`, `role`. Middleware + handlers from Tasks 2-5; Prometheus instrumentation via `Instrumentator().instrument(app)` without `expose`.
- `def create_internal_app(settings: Settings, valkey: Redis) -> FastAPI` (`openapi_url=None`, `docs_url=None`, `redoc_url=None`): `GET /health` → 200 `{"status": "ok"}`; `GET /ready` → 200 or 503 by `valkey_ping`; `GET /metrics` → `prometheus_client.generate_latest()` with its content type.
- `def main() -> None`: `configure_logging`, build both apps, run two `uvicorn.Server` instances with `asyncio.gather`; SIGTERM stops both. Refuses to start (exit 2, clear log line) when settings validation fails.

- [ ] Steps: failing tests (`test_info_requires_key`, `test_info_ok_with_user_key`, `test_openapi_has_no_internal_paths`, `test_metrics_not_on_public_app`, `test_metrics_on_internal_app_counts_requests`, `test_ready_503_when_valkey_down`, `test_health_200_when_valkey_down`, `test_openapi_title_and_description`), implement, pass, gates; then start `uv run text-extraction` with `AUTH_MODE=none` and check `curl :9090/health` and `curl :8080/v1/info` by hand, record output; commit `feat: serve public and internal apps on two ports`.

### Task 7: Docker image and compose

**Files:** Create `Dockerfile`, `docker-compose.yml`, `.env.example`; modify `.dockerignore` if needed.

- [ ] **Step 1:** `Dockerfile`: stage `build` from `python:3.13-slim` with uv (copied from `ghcr.io/astral-sh/uv` pinned minor) running `uv sync --locked --no-dev --no-editable` into `/app/.venv`; stage `runtime` from `python:3.13-slim`, user `app` uid/gid `10001`, copies only the venv, `ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`, `EXPOSE 8080 9090`, `USER 10001`, `CMD ["text-extraction"]`. No `HEALTHCHECK` in the image (Kubernetes probes own it; compose defines its own).
- [ ] **Step 2:** `docker-compose.yml` (reference only): `api` (build `.`, ports `8080:8080`, `9090` not published, env from `.env`, `VALKEY_URL=redis://valkey:6379/0`, healthcheck via `python -c` urllib on `http://127.0.0.1:9090/ready`, `read_only: true`, `security_opt: [no-new-privileges:true]`, depends on valkey healthy) and `valkey` (`valkey/valkey:8.1-alpine`, healthcheck `valkey-cli ping`, named volume). `.env.example` lists every setting with its default and a placeholder `API_KEYS` entry with how to compute the hash.
- [ ] **Step 3: Verify:** `docker build -t text-extraction:dev .` succeeds; image size printed (`docker image ls`); container runs as uid 10001 (`docker run --rm text-extraction:dev id -u` → `10001`); `docker compose up -d --wait`, then `curl -fsS localhost:8080/v1/info -H "Authorization: Bearer <test key>"` returns the envelope with `"status":"success"`, and `curl localhost:8080/metrics` returns 404 envelope; `docker compose down -v`. (In this environment Docker Hub may rate-limit; `mirror.gcr.io/library/python:3.13-slim` and `mirror.gcr.io/valkey/valkey:8.1-alpine` are pre-pulled and tagged locally.)
- [ ] **Step 4: Commit** `feat: add Docker image and compose reference`.

### Task 8: Helm chart

**Files:** Create `deploy/helm/text-extraction/` (`Chart.yaml` version `0.1.0` appVersion `0.1.0`, `values.yaml`, `templates/_helpers.tpl`, `deployment.yaml`, `service.yaml`, `service-internal.yaml`, `configmap.yaml`, `secret.yaml`, `ingress.yaml`, `hpa.yaml`, `valkey.yaml`, `servicemonitor.yaml`, `prometheusrule.yaml`, `NOTES.txt`), `deploy/helm/text-extraction/README.md` (values table).

- [ ] **Step 1:** values: `image.repository` `ghcr.io/janschachtschabel/text-extraction`, `image.tag` (defaults to appVersion), `replicaCount: 2`, `config:` (map of non-secret env), `secrets.apiKeys` (JSON) or `secrets.existingSecret`, `resources` (requests 100m/256Mi, limits 1/512Mi — placeholder until package 11 measures), `autoscaling` (off, min 2 max 10, CPU 70 %), `ingress` (off; targets service port `http` 8080 only), `valkey.enabled: true` (single Deployment + Service + optional PVC, image `valkey/valkey:8.1-alpine`) and `externalValkey.url` used when disabled, `metrics.serviceMonitor.enabled: false`, `metrics.prometheusRule.enabled: false` (rules: `TextExtractionDown` — no ready pod 5 m; `TextExtractionHigh5xx` — 5xx rate > 5 % for 10 m), pod `securityContext` (runAsNonRoot, uid 10001, readOnlyRootFilesystem, drop ALL, seccomp RuntimeDefault), probes on port `internal` 9090 (`/health` liveness, `/ready` readiness).
- [ ] **Step 2: Verify** with `docker run --rm -v "$PWD/deploy/helm:/apps" -w /apps alpine/helm:3.19.0 lint text-extraction` (0 failures) and `helm template` twice: defaults, and `--set valkey.enabled=false --set externalValkey.url=redis://ext:6379/0 --set ingress.enabled=true --set metrics.serviceMonitor.enabled=true --set metrics.prometheusRule.enabled=true`; check rendered output contains no Valkey objects in the second run and the ingress backend port is 8080.
- [ ] **Step 3: Commit** `feat: add Helm chart with optional Valkey`.

### Task 9: CI images, chart lint and docs

**Files:** Create `.github/workflows/image.yml`, `docs/configuration.md`; modify `.github/workflows/ci.yml`, `CLAUDE.md`, `README.md`.

- [ ] **Step 1:** `image.yml`: on pull_request (build only, no push), push to `main` (push `latest` + `sha-<short>`), tags `v*.*.*` (push `X.Y.Z`, `X.Y`); `docker/metadata-action`, `docker/setup-buildx-action`, `docker/login-action` to GHCR with `GITHUB_TOKEN`, `docker/build-push-action` with GHA cache; `permissions: contents: read, packages: write` on the job only; `timeout-minutes: 30`.
- [ ] **Step 2:** `ci.yml`: add a `helm` job (`azure/setup-helm` pinned major, `helm lint deploy/helm/text-extraction`), `timeout-minutes: 15` on jobs, `cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}`.
- [ ] **Step 3:** `docs/configuration.md`: table of every env var (name, default, meaning), a separate "Secrets" table (`API_KEYS`, `API_KEYS_FILE` content, `VALKEY_URL` when it carries a password), the Helm value each maps to, ports, expected response times (`/v1/info` < 50 ms), headers read (`Authorization`, `x-client-trace-id`, B3) and that none are returned except `Retry-After` on 429.
- [ ] **Step 4:** `CLAUDE.md` Commands: add `docker compose up --build`, `helm lint` via docker; README quick start for Docker.
- [ ] **Step 5:** Deferred from package 0: `.gitattributes` marks binaries (`*.png`, `*.jpg`, `*.pdf`, `*.gz` as `binary`); the local ruff hooks in `.pre-commit-config.yaml` pass `--force-exclude` so excluded paths stay excluded when pre-commit passes file names.
- [ ] **Step 6: Verify** actionlint on both workflows; all gates; commit `ci: build tagged images and lint the Helm chart`.
