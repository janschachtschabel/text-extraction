# Configuration

Every setting is an environment variable with a default. Variable names are case-insensitive.
[`.env.example`](../.env.example) lists them for docker compose, and the Helm
chart README has the [full values table](../deploy/helm/text-extraction/README.md#values).

## Environment variables

Non-secret settings. In Helm, put a variable under `config.<NAME>` in the values; it lands in the
chart's ConfigMap with the same name (values are strings). The chart overrides `PUBLIC_PORT`,
`INTERNAL_PORT` and `VALKEY_URL`; values for them under `config` are ignored.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLIC_HOST` | `0.0.0.0` | Bind address of the public API. |
| `PUBLIC_PORT` | `8080` | Port of the public API. Fixed to 8080 by the Helm chart. |
| `INTERNAL_HOST` | `0.0.0.0` | Bind address of the internal listener. |
| `INTERNAL_PORT` | `9090` | Port of the internal listener (`/health`, `/ready`, `/metrics`). Fixed to 9090 by the Helm chart. |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` (case-insensitive). Anything else makes the service exit with code 2. |
| `LOG_FORMAT` | `json` | `json` (stdout, one object per line) or `console` (plain text for local development). |
| `AUTH_MODE` | `api_key` | `api_key` requires a key on every public request; `none` disables authentication and gives every caller the `admin` role. With `api_key` the service exits with code 2 at start-up when no key is configured. |
| `RATE_LIMIT_ENABLED` | `true` | Switch the inbound rate limit on or off. |
| `RATE_LIMIT_PER_MINUTE` | `60` | Default requests per minute per API key. A key can override it with `rate_limit_per_minute`. |
| `VALKEY_URL` | `redis://localhost:6379/0` | Valkey (Redis protocol) used for the rate-limit counters. A secret when it carries a password, see below. |
| `FETCH_TIMEOUT_SECONDS` | `20` | Total time budget of one outbound fetch, in seconds. Must be greater than 0. |
| `FETCH_MAX_BYTES` | `20000000` | Largest response body the fetcher accepts, in bytes. Minimum 1024. |
| `FETCH_MAX_REDIRECTS` | `5` | Redirects followed per fetch (0 to 20). Every hop passes the SSRF guard. |
| `FETCH_ALLOW_PRIVATE_NETWORKS` | `false` | **Unsafe.** `true` switches the SSRF guard off for private, loopback and link-local addresses. For local development and tests only; never set it in a deployment. |

The fetcher identifies itself on every outbound request with a fixed, honest user agent,
`text-extraction/<version> (+https://github.com/janschachtschabel/text-extraction)`. It is not
configurable. The robots.txt product token is `text-extraction`. Client trace headers
(`x-client-trace-id`, B3) are logged but never forwarded to target sites, and proxy environment
variables are ignored.

## Secrets

Keep these out of ConfigMaps, images and logs.

| Variable | Content | Helm |
| --- | --- | --- |
| `API_KEYS` | JSON list of `{"name", "role", "sha256", "rate_limit_per_minute"}`. `role` is `user` or `admin`; `rate_limit_per_minute` is optional. Only the SHA-256 of a key is stored. | `secrets.apiKeys` (chart-managed Secret) or `secrets.existingSecret` + `secrets.existingSecretKey` |
| `API_KEYS_FILE` | Path to a JSON file with the same shape as `API_KEYS`, merged with it. The file content is the secret. | None. The chart mounts no key file; use `API_KEYS`. |
| `VALKEY_URL` | Only when the URL carries a password. | `externalValkey.existingSecret.name` + `.key`. Without a password use `externalValkey.url`; the in-chart Valkey (`valkey.enabled`) needs no secret. |

Generate a key with `openssl rand -hex 32` and compute its hash with
`printf %s "$KEY" | sha256sum | cut -d' ' -f1`. Clients send the key as
`Authorization: Bearer <key>`. Key names and hashes must be unique.

## Ports and probes

| Port | Serves | Exposed |
| --- | --- | --- |
| `8080` | Public API (`/v1/...`, OpenAPI, docs). | Ingress and the public Service. |
| `9090` | `/health`, `/ready`, `/metrics`. Never part of the OpenAPI spec. | Cluster-internal only. |

- `/health` is the liveness probe. It never touches Valkey, so a Valkey outage does not restart pods.
- `/ready` is the readiness probe. It checks Valkey, so pods without Valkey leave the load balancer.
- `/metrics` is Prometheus text and is not reachable on 8080.

docker compose assumes the default ports 8080 and 9090: its port mapping and health check are
written for them. Change `PUBLIC_PORT` or `INTERNAL_PORT` there and you must edit
`docker-compose.yml` as well. The Helm chart pins both ports.

## Response times

`/v1/info` answers in under 50 ms (one Valkey round trip for the rate limit, no other I/O).
Work that takes longer than about 30 s will run as a job in a later package.

## Headers

Read from the request:

| Header | Use |
| --- | --- |
| `Authorization` | `Bearer <key>` when `AUTH_MODE=api_key`. |
| `x-client-trace-id` | Client trace id; logged on every line of the request and used as `trace_id` in the response envelope. |
| `b3`, `x-b3-traceid`, `x-b3-spanid`, `x-b3-parentspanid`, `x-b3-sampled` | B3 trace context; logged on every line of the request. |

Query strings are never logged.

Responses carry no custom headers. The only exceptions are `Retry-After` (seconds until the
window ends) on 429 and `WWW-Authenticate: Bearer` on 401. Trace headers are not echoed back.

## Rate limit

The limit is a fixed 60-second window per API key, counted in Valkey so all pods share it. A
caller can therefore make up to twice the limit across a window boundary: the full limit at the end
of one window and the full limit again at the start of the next. Requests over the limit get 429
with `Retry-After`. If Valkey is unavailable the request is refused with 503 (`internal_error`, retryable) and a warning is logged. Set `RATE_LIMIT_ENABLED=false` to run without the limit.
