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
| `VALKEY_URL` | `redis://localhost:6379/0` | Valkey (Redis protocol) that holds all shared state: rate-limit counters, the per-host outbound state, the compliance file cache, cached results and fingerprints, jobs and the job queue. Needs Valkey 7.2+ or Redis 7.0+ (the queue scripts use script flags). A secret when it carries a password, see below. |
| `FETCH_TIMEOUT_SECONDS` | `20` | Total time budget of one outbound fetch, in seconds. Minimum 1 (the least time a target fetch is started with). |
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
| `RESULT_CACHE_TTL_SECONDS` | `3600` | How long a stored extraction result counts as fresh and is served without asking the site, measured from the time it was produced. 0 means a stored result is never served unchecked; it can still be revalidated. A fresh hit skips the compliance checks too: it carries the compliance report of the extraction that produced it, so a new robots.txt or TDM opt-out reaches cached results only after up to `RESULT_CACHE_TTL_SECONDS` plus `COMPLIANCE_CACHE_SECONDS`. Setting this above `COMPLIANCE_CACHE_SECONDS` lengthens that lag. |
| `RESULT_STORE_TTL_SECONDS` | `86400` | How long a result stays in Valkey so a stale one can be revalidated with ETag/Last-Modified or the raw-content hash instead of being extracted again. 0 stores no results. Results over 2 MB as JSON are never stored. |
| `FINGERPRINT_TTL_SECONDS` | `2592000` | How long the last text fingerprint of a URL is kept for change detection (`changed`, `similarity_to_previous`). 30 days by default. 0 stores no fingerprints, so nothing is reported as changed. |
| `CONVERT_MAX_BYTES` | `5242880` | Largest HTML or text body that is converted, in bytes (5 MiB; minimum 1024). A larger page fails with `too_large` (not retryable) before it is decoded. `FETCH_MAX_BYTES` stays the download limit. |
| `CONVERT_CONCURRENCY` | `2` | Conversions (decoding, HTML conversion, metadata and licences, fingerprint) running at once in one pod, on a thread pool of their own; DNS lookups keep the default pool. A request waits for a free slot within its `EXTRACT_TIMEOUT_SECONDS`, else it fails with `timeout`. Python cannot stop a running thread: a conversion that outlives its request runs to its end and holds its slot until then, so this bound is what limits CPU and memory. Such a conversion also holds the shutdown up, because Python joins its worker threads when the process exits. That is usually seconds, but the worst case can run to the end of the grace period, where the pod is killed (Trafilatura is quadratic on repeated paragraphs: one 5.7 MB page took 125 s). That is harmless, because pods are stateless: the request it served has already been answered `timeout`. Peak memory is roughly `CONVERT_CONCURRENCY` times the peak of one page (about 450 MB for a 5 MB page), see the Helm memory limit. Minimum 1. |
| `EXTRACT_TIMEOUT_SECONDS` | `25` | Time budget of one `POST /v1/extract`, from the compliance checks to the finished result, in seconds. Minimum 2, so that the fetch can always get its 1 s minimum. The fetch gets at most the time that is left minus 0.25 s (and never more than `FETCH_TIMEOUT_SECONDS`); with less than 1 s left when the host's turn comes, the target is not fetched. Running out answers `status: failed` with `timeout` (retryable). A fetch timeout counts against the host's circuit breaker only when the fetch had at least half of `FETCH_TIMEOUT_SECONDS` (after a redirect: what was left of the chain's `FETCH_TIMEOUT_SECONDS` when the hop to that host began). The graceful shutdown lasts `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds, so raise the Helm value `terminationGracePeriodSeconds` (45) with it. After it, the lifespan shutdown waits for extractions that became a job (`wait_seconds`) for up to `EXTRACT_TIMEOUT_SECONDS` + 7 s (5 s of grace after the deadline, then it cancels them and allows 2 s for their writes); see Response times. |
| `JOB_TTL_SECONDS` | `86400` | How long a job (its status, URL list and results) stays in Valkey, in seconds. The time starts at creation and starts again when the job completes, so a caller has this long to fetch the results of a finished job. Minimum 60. |
| `JOB_ITEM_MAX_BYTES` | `2000000` | Largest stored result of one job item, as UTF-8 JSON. A larger result is stored without its `content` (`data.content` is null and `data.warnings` has `content_not_stored_too_large`). Minimum 10000. |
| `JOB_ITEM_MAX_ATTEMPTS` | `5` | How often one job item may lose its worker lease (the worker died or hung past the lease) or be deferred (its host was paused or busy) before it fails. The counter covers both; a lost lease beyond it fails the item with `internal_error` (retryable). Minimum 1. |
| `SCHEDULER_CLAIM_SCAN_BUDGET` | `200` | Client-domain pairs one claim of the job queue looks at, at most. A claim that finds nothing within the budget answers "try again in 0.05 s", and the next claim goes on where it stopped (a client whose domains the budget ran out in moves to the end of the rotation), so fairness holds across claims. Workers back off from 0.05 s up to 1 s while such answers repeat. It bounds the time one claim blocks Valkey when many domains are deferred or at their limit. Minimum 1, maximum 5000. |
| `WORKER_CONCURRENCY` | `8` | Job items one extract worker pod (`text-extraction-worker`) processes at once. Conversions are still capped by `CONVERT_CONCURRENCY` in that pod, and requests to one host by `OUTBOUND_MAX_CONCURRENCY_PER_HOST` across all pods. Minimum 1. |
| `BATCH_MAX_URLS` | `1000` | Most URLs one `POST /v1/batch` may carry. More answers 422 `invalid_request`, and so does a body over `BATCH_MAX_URLS` × (`MAX_URL_LENGTH` + 8) bytes plus 4 KiB, checked before the list is validated. Minimum 1, maximum 100000. |
| `BATCH_MAX_OPEN_PER_CLIENT` | `5` | Batches one API key may have unfinished (queued or running) at a time. The next `POST /v1/batch` answers 429 `rate_limited` (retryable) until one completes or expires. With `AUTH_MODE=none` all callers share one anonymous owner, so the limit applies to the whole deployment, and every caller can read every job. Minimum 1. |
| `JOBS_ENABLED` | `true` | `false` makes `POST /v1/batch` and `GET /v1/jobs/{id}` answer 503 `internal_error` ("Jobs are disabled on this deployment."), for API pods without workers. `POST /v1/extract` then ignores `wait_seconds` and answers when the extraction ends. |
| `EXTRACT_MAX_BACKGROUND` | `32` | Extractions one API pod holds for `POST /v1/extract` requests with `wait_seconds` (the ones that became a job and the ones still waiting), at most. Beyond it a request does not become a job: it keeps waiting for its result and answers 200 (or its error). Bounds the pod's memory and the tasks the shutdown waits for. Minimum 1. |
| `SHUTDOWN_DRAIN_SECONDS` | `5` | After SIGTERM, how long `/ready` answers 503 while both ports keep serving, so Kubernetes can take the pod out of the Service before connections are refused. The graceful shutdown starts afterwards (a second signal starts it at once). Drain plus graceful shutdown (35 s by default) must stay below the Helm value `terminationGracePeriodSeconds` (45). Minimum 0. |

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
- `/ready` is the readiness probe. It answers 200 only when the public port listens (after the
  start-up, which loads the language model), no shutdown has begun and Valkey answers. Pods
  without Valkey leave the load balancer, and so does a pod as soon as it gets SIGTERM.
- `/metrics` is Prometheus text and is not reachable on 8080.

The extract worker (`text-extraction-worker`) serves only port 9090, with the same three paths.
Its `/ready` answers 200 once it has loaded the language model and its loops claim items, while
Valkey answers. On SIGTERM `/ready` turns 503 at once, the worker stops claiming and gives the
items in flight `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds to finish; a claim already sent
when the signal arrives counts as in flight, so its item is processed too. An item still running
then is cancelled, and its lease expires so that another worker runs it. There is no drain wait,
since no Service sends traffic to the worker.

docker compose assumes the default ports 8080 and 9090: its port mapping and health check are
written for them. Change `PUBLIC_PORT` or `INTERNAL_PORT` there and you must edit
`docker-compose.yml` as well. The Helm chart pins both ports.

## Response times

`/v1/info` answers in under 50 ms (one Valkey round trip for the rate limit, no other I/O).
`POST /v1/extract` takes at most `EXTRACT_TIMEOUT_SECONDS` (25 s); a cached result comes back in a
few milliseconds. A caller that does not want to hold the connection sends `wait_seconds` (0-60):
an extraction still running after that long continues in the pod and becomes a job (202,
`GET /v1/jobs/{id}`); `JOBS_ENABLED=false` ignores `wait_seconds`. On SIGTERM `/ready` turns 503
at once and both ports keep serving for `SHUTDOWN_DRAIN_SECONDS` (5); then the public port stops
accepting connections and gives requests in flight `max(30, EXTRACT_TIMEOUT_SECONDS + 5)` seconds
(30 by default) to finish; then the app's own shutdown waits for extractions that became a job,
for at most `EXTRACT_TIMEOUT_SECONDS` + 7 s (it cancels those still running 5 s after
`EXTRACT_TIMEOUT_SECONDS` and gives the writes of their error results 2 s); the internal port
(`/health`, `/metrics`) stops last. Such an extraction ends within `EXTRACT_TIMEOUT_SECONDS` of
its start and runs while the graceful phase does, so normally the whole sequence fits in the Helm
chart's `terminationGracePeriodSeconds` (45); its worst case does not, and then the pod is killed
and the job's lease expires (the job then reports a lost extraction). The Helm value must stay
above drain plus graceful time (35 s by default).

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

## Job keys

A job lives in four Valkey keys. The id is a literal cluster hash tag, so one job's keys share a
slot.

| Key | Content | Lifetime |
| --- | --- | --- |
| `job:{<id>}:meta` | Hash: kind, owner, owner role, options (JSON), state, counters, times, lease. | `JOB_TTL_SECONDS` from creation, renewed on completion. |
| `job:{<id>}:urls` | List of the job's URLs in input order. | As `meta`. |
| `job:{<id>}:results` | Hash of item index to the result envelope (JSON). | As `meta`; set with the first result. |
| `job:{<id>}:done` | Bitmap of the finished item indexes. | As `meta`; set with the first result. |

A result is written once: storing the same index again changes nothing, so an item that is run a
second time after a lost lease counts once. Times are epoch seconds from the pod's clock.

Per API key (the job's owner) one more key lists the unfinished batches:
`jobs:{owner:<name>}:open` is a set of job ids with a TTL of `JOB_TTL_SECONDS`, renewed on each
batch. `POST /v1/batch` removes the ids of finished or expired jobs before it counts, and refuses
the batch when the owner has `BATCH_MAX_OPEN_PER_CLIENT` open ones. The name is escaped (`%` as
`%25`, `{` as `%7B`, `}` as `%7D`), so the braces always enclose exactly the name. No script
touches this key together with a job key or a queue key.

## Queue keys

Batch items wait in a fair queue per worker pool (`extract` for the fast workers). The pool name is
a literal cluster hash tag, so all keys of one pool share a slot. Client names (the API key name)
and domains appear escaped in the keys (`%` as `%25`, `:` as `%3A`, tab as `%09`).

| Key | Content |
| --- | --- |
| `q:{<pool>}:clients` | List of the clients with queued items, in rotation order. |
| `q:{<pool>}:c:<client>:domains` | List of that client's domains with queued items, in rotation order. |
| `q:{<pool>}:c:<client>:d:<domain>` | List of that client's items of that domain (`<job_id>:<index>:<attempts>`). |
| `q:{<pool>}:notbefore` | Hash of domain to the time before which its items are not claimed (deferred). |
| `q:{<pool>}:running` | Hash of domain to its number of leased items. |
| `q:{<pool>}:leases` | Sorted set of leased items, scored by lease expiry. |
| `q:{<pool>}:leased` | Hash of leased item to its client, domain and lease token. |
| `q:{<pool>}:pending` | Number of queued items, claimable or deferred (leased items not counted). |

A claim rotates the clients, then the domains of the client, so a small batch is not queued behind
a large one. It skips a domain that is deferred or holds `OUTBOUND_MAX_CONCURRENCY_PER_HOST` leased
items, and looks at no more than `SCHEDULER_CLAIM_SCAN_BUDGET` client-domain pairs. Before that, it
takes back up to 20 expired leases: each item goes to the front of its domain with one more attempt.
An item past `JOB_ITEM_MAX_ATTEMPTS` is handed out once more, marked exhausted: the worker records
its failure and then completes it, and if the worker dies in between, the item comes back after its
lease. Every lease has a token, so a worker whose lease was taken back cannot complete or defer the
item under the next worker's lease. The keys have no TTL; the lists empty themselves as items are
claimed.

A worker leases an item for `EXTRACT_TIMEOUT_SECONDS * 2 + 10` seconds (60 by default) and runs it
through the fast pipeline with the job's options, as the job's owner. When the outbound gate
refuses the host (busy, or paused after errors or a 429), the item goes back to the front of its
domain, which is not claimed before the gate's retry time; an item at `JOB_ITEM_MAX_ATTEMPTS` is
stored with the gate's error (`rate_limited` or `connection_error`, retryable) instead. An item
whose job has expired is dropped. While Valkey fails, a worker backs off for 1, 2, 4 … 30 s and
then goes on; the item it held (an exhausted one too) is claimed again once its lease expires.
