# Live regression

Runs a started service against real, public URLs and counts how many yield usable text. It finds
what fixtures cannot: dead links, cookie and bot walls, script-only pages, sites whose text is
mostly navigation. Nothing here runs in `uv run pytest` (`tests/live` is ignored there); only the
classifier is unit-tested, in `tests/test_live_classify.py`.

## URL lists

All lists are CSV files in `urls/` with a header row and a `url` column. Order and duplicates are
kept.

| File | URLs | Purpose |
| --- | ---: | --- |
| `wlo_4000.csv` | 4,000 | The WLO corpus the 68 % fast baseline was measured on (2026-09-28). |
| `wlo_200.csv` | 200 | Sample of the corpus. **The gate:** at or above 68 % ok. |
| `wlo_50.csv` | 50 | Quicker sample for local runs. |
| `smoke.csv` | 24 | Curated watchlist of known-hard pages and expected failures, with `category`, `expected_lang` and `notes`. Reported, never a gate: its ok rate is not comparable to the baseline. |

## Run it

```bash
docker compose up --build          # or: uv run text-extraction (needs Valkey)
LIVE_API_KEY=... uv run python -m tests.live.run_live --urls tests/live/urls/wlo_200.csv \
  --min-ok-rate 68
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8080` | The service's public API. |
| `--api-key` | env `LIVE_API_KEY` | Sent as `Authorization: Bearer`; not needed with `AUTH_MODE=none`. |
| `--urls` | `urls/wlo_200.csv` | CSV with a `url` column. |
| `--concurrency` | 8 | Requests in flight in total. |
| `--min-words` | 50 | Fewer words than this is `too_short`. |
| `--compliance` | `report` | Sent as the request's `compliance` mode. |
| `--min-ok-rate` | none | Exit 1 when the ok rate (percent of all URLs) is below this. |
| `--out` | `results/live_<UTC time>.jsonl` | One JSON line per URL. |
| `--summary-out` | none | Also write the Markdown summary to this file. |
| `--timeout` | 90 | Client timeout per request in seconds; keep it above `EXTRACT_TIMEOUT_SECONDS`. |

Every request is `POST /v1/extract` with `mode=auto`, `format=text` and `cache=false`. Each domain
has its own queue and at most one request in flight; the domains take turns under the global
`--concurrency`. A domain with many URLs (GeoGebra is about a quarter of the corpus) therefore
sets the run time. Run the service with the default per-domain outbound limits to see what
production sees.

Each JSONL line has `url`, `domain`, `bucket`, `http_status`, `status`, `error_code`,
`word_count`, `compliance_decision` and `elapsed_ms`. `error_code` is the envelope's code, or
`client_<ExceptionName>` when the request got no answer. The summary on stdout lists the buckets
overall, the ok rate, how many URLs `enforce` would have blocked (`compliance.decision ==
"reported"`) and the buckets of the ten domains with the most URLs.

## Buckets

`classify` in `classify.py` reads the envelope: `status`, `error.code`, `data.word_count` and
`data.compliance.decision`.

| Bucket | When |
| --- | --- |
| `ok` | `success` or `partial` with at least `--min-words` words and no error-page code. |
| `too_short` | `content_too_short`, or fewer words than `--min-words`. |
| `likely_error_page` | `bot_protection`, `login_required`, `likely_error_page`. |
| `no_content` | `empty_content`, `unsupported_content_type`, `too_large`, `conversion_failed`, and the media codes. |
| `target_http_error` | The target answered with an HTTP error. |
| `connection_error` | `dns_error`, `connection_error`, `timeout`. |
| `blocked` | `blocked_by_robots`, `tdm_reserved`, or `compliance.decision == "blocked"`. Only appears under `--compliance enforce`. |
| `rate_limited` | HTTP 429, or the `rate_limited` code. |
| `service_error` | HTTP 5xx, a non-JSON body, no answer at all, `internal_error`, `llm_failed`. |
| `invalid_request` | Other HTTP 4xx from the service, `invalid_url`, `blocked_url`. Usually a harness setup problem (key, URL list). |
| `unknown` | A code that is not mapped. A test fails for every `ErrorCode` without a bucket, so this should stay empty. |

## GitHub Actions

`.github/workflows/live.yml` starts Valkey and the service (`AUTH_MODE=none`,
`RATE_LIMIT_ENABLED=false`), waits for `/ready`, runs the harness, adds the summaries to the job
summary and uploads the JSONL files and the server log as the `live-results` artifact.

- **Manually** (Actions, "live", Run workflow): choose `url_set` and `min_ok_rate`.
- **Pull requests** that carry the label `live`: `wlo_200` with `--min-ok-rate 68`, then `smoke`
  as a report.
- **Weekly**, Mondays 03:00 UTC: the same as for pull requests.
