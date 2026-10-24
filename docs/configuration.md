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
| `VALKEY_URL` | `redis://localhost:6379/0` | Valkey (Redis protocol) that holds all shared state: rate-limit counters, the per-host outbound state, the compliance file cache, cached results and fingerprints. A secret when it carries a password, see below. |
| `FETCH_TIMEOUT_SECONDS` | `20` | Total time budget of one outbound fetch, in seconds. Must be greater than 0. |
| `FETCH_MAX_BYTES` | `20000000` | Largest response body the fetcher accepts, in bytes. Minimum 1024. |
| `FETCH_MAX_REDIRECTS` | `5` | Redirects followed per fetch (0 to 20). Every hop passes the SSRF guard. |
| `FETCH_ALLOW_PRIVATE_NETWORKS` | `false` | **Unsafe.** `true` switches the SSRF guard off for private, loopback and link-local addresses. For local development and tests only; never set it in a deployment. |
| `OUTBOUND_MAX_CONCURRENCY_PER_HOST` | `2` | Requests in flight to one host at the same time, across all pods. Minimum 1. |
| `OUTBOUND_MIN_DELAY_MS` | `1000` | Least gap between the starts of two requests to one host, in milliseconds. 0 switches the spacing off. |
| `OUTBOUND_MAX_CRAWL_DELAY_SECONDS` | `10` | Upper limit for the `Crawl-delay` of a robots.txt, in seconds. A longer value is cut to this. |
| `OUTBOUND_MAX_WAIT_SECONDS` | `10` | Longest time a request waits for its turn at a host. Longer waits are refused as `OutboundBusy`. |
| `OUTBOUND_BACKOFF_SECONDS` | `30` | Pause for a host after 429 or 503 without a usable `Retry-After`. Minimum 1. |
| `OUTBOUND_MAX_BACKOFF_SECONDS` | `300` | Upper limit for any pause after 429 or 503, including `Retry-After`. Minimum 1. |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive connection failures or 5xx answers from a host that open its circuit. Minimum 1. |
| `CIRCUIT_OPEN_SECONDS` | `60` | How long an open circuit refuses requests to the host. Minimum 1. |
| `OUTBOUND_MAX_CONNECTIONS` | `200` | Open outbound connections of one pod at most, over all hosts (the fetcher's connection pool). Minimum 1. |
| `OUTBOUND_MAX_KEEPALIVE` | `50` | Idle outbound connections a pod keeps for reuse; an idle connection is closed after 30 s. Minimum 0. |
| `COMPLIANCE_DEFAULT` | `enforce` | Compliance mode when a request does not choose one: `enforce` blocks what robots.txt, TDMRep, ai.txt or `noai` forbid; `report` only reports it; `ignore` skips the checks. |
| `COMPLIANCE_ALLOW_OVERRIDE` | `true` | Whether non-admin callers may choose `enforce` or `report` per request. `ignore` stays admin-only (unless it is the default). Admins may always choose. A refused choice answers 403 `forbidden`. |
| `COMPLIANCE_CACHE_SECONDS` | `3600` | How long robots.txt, `tdmrep.json` and ai.txt of an origin are cached in Valkey. 0 switches the cache off. A file that could not be fetched (5xx, 429, network error) is cached at most 300 s, so a short outage does not block a site for the whole period. |
| `QUALITY_MIN_WORDS` | `50` | Fewest words (tokens with a letter or digit) the extracted content must have. Fewer ends as `partial` with `content_too_short`; no words at all is `failed` with `empty_content`. 0 switches the length check off. |
| `RESULT_CACHE_TTL_SECONDS` | `3600` | How long a stored extraction result counts as fresh and is served without asking the site, measured from the time it was produced. 0 means a stored result is never served unchecked; it can still be revalidated. |
| `RESULT_STORE_TTL_SECONDS` | `86400` | How long a result stays in Valkey so a stale one can be revalidated with ETag/Last-Modified or the raw-content hash instead of being extracted again. 0 stores no results. Results over 2 MB as JSON are never stored. |
| `FINGERPRINT_TTL_SECONDS` | `2592000` | How long the last text fingerprint of a URL is kept for change detection (`changed`, `similarity_to_previous`). 30 days by default. 0 stores no fingerprints, so nothing is reported as changed. |
| `CONVERT_MAX_BYTES` | `5242880` | Largest HTML or text body that is converted, in bytes (5 MiB; minimum 1024). A larger page fails with `too_large` (not retryable) before it is decoded. `FETCH_MAX_BYTES` stays the download limit. |
| `CONVERT_CONCURRENCY` | `2` | Conversions (decoding, HTML conversion, metadata and licences, fingerprint) running at once in one pod, on a thread pool of their own; DNS lookups keep the default pool. A request waits for a free slot within its `EXTRACT_TIMEOUT_SECONDS`, else it fails with `timeout`. Python cannot stop a running thread: a conversion that outlives its request finishes in the background and holds its slot until then, so this bound is what limits CPU and memory. Peak memory is roughly `CONVERT_CONCURRENCY` times the peak of one page (about 450 MB for a 5 MB page), see the Helm memory limit. Minimum 1. |
| `EXTRACT_TIMEOUT_SECONDS` | `25` | Time budget of one `POST /v1/extract`, from the compliance checks to the finished result, in seconds. Must be greater than 0. The fetch gets at most the time that is left minus 0.25 s (and never more than `FETCH_TIMEOUT_SECONDS`); with less than 1 s left when the host's turn comes, the target is not fetched. Running out answers `status: failed` with `timeout` (retryable). A fetch timeout counts against the host's circuit breaker only when the fetch had at least half of `FETCH_TIMEOUT_SECONDS`. The graceful shutdown lasts `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds, so raise the Helm value `terminationGracePeriodSeconds` (40) with it. |

Result cache and fingerprint keys are `res:{<sha256>}` and `fp:{<sha256>}` in Valkey. The hash is
taken over the normalised URL: lower-case scheme and host, no fragment, no default port (80 for
`http`, 443 for `https`); the path and the query are kept exactly as given. For results it also
covers the extraction options, in sorted key order (`mode`, `format`, `metadata`, `links` and the
compliance mode that applied). Fingerprints are kept per URL and output format, since Markdown,
text and HTML of one page are different texts; a revalidated result renews its fingerprint's TTL.
Validators (`ETag`, `Last-Modified`) are only stored and sent back when they are printable ASCII
of at most 1 KB. A Valkey outage turns the cache and the change
detection off for the affected requests (logged without the URL) and never fails them.

The fetcher identifies itself on every outbound request with a fixed, honest user agent,
`text-extraction/<version> (+https://github.com/janschachtschabel/text-extraction)`. It is not
configurable. The robots.txt product token is `text-extraction`; robots.txt is read per RFC 9309
(4xx and unfollowable redirects mean no rules; 5xx, 429 or a network error means "unavailable",
which `enforce` treats as disallowed; a file over 500 KiB is parsed from its first 500 KiB). The
compliance files of an origin are loaded once per pod for concurrent requests. Every redirect
target is checked like the requested URL (robots.txt is path-specific) before it is fetched; under
`enforce` a disallowed target ends the request with `blocked_by_robots` or `tdm_reserved`, under
`report` its findings join the report. Client trace headers
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
`POST /v1/extract` takes at most `EXTRACT_TIMEOUT_SECONDS` (25 s); a cached result comes back in a
few milliseconds. Work that takes longer than about 30 s will run as a job in a later package. On
SIGTERM the server stops accepting connections and gives requests in flight
`max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds (30 by default) to finish; the Helm chart's
`terminationGracePeriodSeconds` (40) must stay above that.

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

## Outbound limits per host

Every fetch of a target site passes a per-host gate. All state is in Valkey, so the pods share it.
Hosts are compared lower-cased and in IDNA form. The braces around the host in the keys are
literal (a cluster hash tag), so all keys of one host live in the same slot.

| Key | Content | Lifetime |
| --- | --- | --- |
| `out:inflight:{<host>}` | Requests in flight: one entry per lease, scored by its expiry time. | A lease expires 120 s after it starts, so a crashed pod frees its slot by itself. |
| `out:next:{<host>}` | Earliest start time of the next request. | Until the slot has passed. |
| `out:cooldown:{<host>}` | End of the pause after 429 or 503. | Until it ends. |
| `out:fail:{<host>}` | Consecutive failures (connection error or 5xx). | 10 minutes, renewed on each failure. |
| `out:circuit:{<host>}` | End of an open circuit. | Until it ends. |

A request waits for a free lease (at most `OUTBOUND_MAX_CONCURRENCY_PER_HOST`) and for its spacing
slot. The spacing is `max(OUTBOUND_MIN_DELAY_MS, min(Crawl-delay, OUTBOUND_MAX_CRAWL_DELAY_SECONDS))`.
If the wait would exceed `OUTBOUND_MAX_WAIT_SECONDS` the request is refused as busy. A redirect to
another host gives the first host's lease back and waits for a turn at the new host; the outcome
counts for the host that answered. While a pause
or an open circuit is set, requests to that host are refused at once. A 429 or 503 sets the pause to
`Retry-After` (seconds or HTTP date) or `OUTBOUND_BACKOFF_SECONDS`, never above
`OUTBOUND_MAX_BACKOFF_SECONDS`; a new pause never shortens one that is already running.
`CIRCUIT_FAILURE_THRESHOLD` failures in a row open the circuit for `CIRCUIT_OPEN_SECONDS`; any
answer below 500 other than 429 resets the count.

Spacing, pauses and lease expiry use each pod's own clock, so the pods need NTP-synchronised
clocks.
