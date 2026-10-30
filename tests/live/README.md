# Live regression

Runs a started service against real, public URLs and counts how many yield usable text. It finds
what fixtures cannot: dead links, cookie and bot walls, script-only pages, sites whose text is
mostly navigation. Nothing here runs in `uv run pytest` (`tests/live` is ignored there); the
harnesses are unit-tested against fake services (`tests/test_live_classify.py`,
`tests/test_live_run.py`, `tests/test_live_site.py`).

## URL lists

All lists are CSV files in `urls/` with a header row and a `url` column. Order and duplicates are
kept; a row without a URL is skipped with a warning.

| File | URLs | Purpose |
| --- | ---: | --- |
| `wlo_4000.csv` | 4,000 | The WLO corpus the baselines were measured on (2026-09-28): fast 68 %, browser 80 %. |
| `wlo_200.csv` | 200 | Sample of the corpus. **The gate:** in `auto` mode at or above 80 % ok (the browser baseline). Also reported in `browser` mode. |
| `wlo_50.csv` | 50 | Quicker sample for local runs. |
| `smoke.csv` | 24 | Curated watchlist of known-hard pages and expected failures, with `category`, `expected_lang` and `notes`. Reported in `auto` mode, never a gate: its ok rate is not comparable to the baseline. |
| `sites.csv` | 5 | Five WLO-type sites (lehrer-online, planet-schule, Klexikon, LEIFIphysik, serlo) for the site structure run (`run_site.py`, package 9). Reported, never a gate. See "The site run" below. |
| `media.csv` | 28 | The media set (package 6): YouTube, Vimeo, ARD, ZDF, arte and 3sat videos, two media files and two pages with HTML5 tracks, with `platform`, `expected_source` (the `transcript_source` the row should get), `expected_lang`, `transcribe` (`true` for the media files, which only ASR can answer) and `notes`. Reported, never a gate. See "The media set" below. |

## Run it

```bash
docker compose up --build          # API, workers and Valkey; or start them by hand (see below)
LIVE_API_KEY=... uv run python -m tests.live.run_live --urls tests/live/urls/wlo_200.csv \
  --mode auto --min-ok-rate 80
```

`--mode auto` and `--mode browser` need the service started with `BROWSER_ENABLED=true` and a
browser worker (`uv sync --extra browser`, `uv run playwright install --only-shell chromium`,
`uv run text-extraction-browser-worker`; give it `INTERNAL_PORT=9091` next to the API). Without
a browser worker `auto` answers what fast mode finds, with the warning `browser_unavailable`
after `BROWSER_QUEUE_WAIT_SECONDS`, and `browser` fails with `timeout`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8080` | The service's public API. |
| `--api-key` | env `LIVE_API_KEY` | Sent as `Authorization: Bearer`; not needed with `AUTH_MODE=none`. |
| `--urls` | `urls/wlo_200.csv` | CSV with a `url` column (and optionally `transcribe`, `platform`, `expected_source`); none by default when `--upload` is given. |
| `--upload` | none | An audio or video file to send to `POST /v1/extract/file` and transcribe; repeatable. Its row is `upload:<file name>` with platform `upload` and expected source `asr`. |
| `--no-transcribe` | off | Never send `transcribe`, whatever a row says (a service without ASR answers it 400). |
| `--concurrency` | 8 | Requests in flight in total. |
| `--min-words` | 50 | Fewer words than this is `too_short`. |
| `--mode` | `auto` | Sent as the request's `mode`: `fast`, `auto` or `browser`. |
| `--compliance` | `report` | Sent as the request's `compliance` mode. |
| `--min-ok-rate` | none | Exit 1 when the ok rate (percent of all URLs) is below this. |
| `--out` | `results/live_<UTC time>.jsonl` | One JSON line per URL. |
| `--summary-out` | none | Also write the Markdown summary to this file. |
| `--job-timeout` | 3600 | Seconds to wait for a job (a transcription answered 202) to complete; longer is `client_TimeoutError`. |
| `--poll-seconds` | 5 | Seconds between two reads of a job. |
| `--timeout` | 90 | Total client deadline per request in seconds (a slower answer is recorded as `client_TimeoutError`); keep it above the longest answer: `EXTRACT_TIMEOUT_SECONDS` + `BROWSER_QUEUE_WAIT_SECONDS` + `BROWSER_TIMEOUT_SECONDS` + 5 s for an escalating `auto` request (70 s by default; the live workflow sets the queue wait to 30 s and passes 120). |

Every request is `POST /v1/extract` with the `--mode` (default `auto`), `format=text` and `cache=false`,
plus `transcribe: true` where the row says so (unless `--no-transcribe`); an upload is
`POST /v1/extract/file` with `format=text`. An answer that is a job (202 with a `status_url`: a
transcription is always one) is read every `--poll-seconds` until its state is `completed`, and
its item's result is recorded with `http_status` 200. Each domain
has its own queue and at most one request in flight (a job holds its domain's turn until it
completes); the domains take turns under the global
`--concurrency`. A domain with many URLs (GeoGebra is about a quarter of the corpus) therefore
sets the run time. Run the service with the default per-domain outbound limits to see what
production sees.

Each JSONL line has `url`, `domain`, `bucket`, `http_status`, `status`, `error_code`,
`word_count`, `compliance_decision`, `mode_used`, `warnings`, `elapsed_ms`, `platform`
(`data.media.platform`, else the row's `platform`), `transcript_source`
(`data.media.transcript_source`), `expected_source` (the row's) and `transcribe` (whether the
request asked for a transcription). `error_code` is
the envelope's code, or `client_<ExceptionName>` when the request got no answer; `mode_used` is
the envelope's `data.mode_used` (`fast` or `browser`: what produced the answer, null without
data); `warnings` is `data.warnings`. The summary on stdout lists the buckets overall, the ok
rate, how many URLs `enforce` would have blocked (`compliance.decision == "reported"`), how many
answers carry the warning `browser_unavailable` (no browser worker answered in time), the URLs
and ok count per `mode_used`, the buckets of the ten domains with the most URLs and an "Error
codes" table: the `error.code` of every failed or partial answer and of every
`connection_error`, most frequent first, with the three domains that gave it most, for a list with
an `expected_source` column (the media set) a "Media" table (per platform: the URLs, how many got
each `transcript_source` or none, and how many got the expected one; a row expecting subtitles
counts with manual or automatic ones, as platforms move tracks between the two), and a "URLs
not ok" table with each URL whose bucket is not `ok` (bucket, code, mode used, words). To see where the browser helps, run a set in `fast` and in
`browser` mode and compare the domains, as the 2026-09-28 baseline did.

## The media set

`media.csv` holds the samples of the spike (2026-10-25): videos with manual subtitles, with
automatic captions only and without subtitles on every platform, the two WLO Vimeo videos that
only give their details (embed-restricted), an arte video whose only German track is `-forced`,
two media files for transcription and two Wikimedia Commons pages with `<track>`. Mediathek items
and the tagesschau MP4 expire; replace a row when its URL is gone, with the same kind of sample.

Its rows count as `ok` like any other (50 words or more); what matters is the Media table:

```bash
# Subtitles and metadata only (no ASR worker), as the live workflow runs it:
uv run python -m tests.live.run_live --urls tests/live/urls/media.csv --no-transcribe
# With transcription (docker compose --profile asr up, ASR_ENABLED=true): the media files are
# transcribed as jobs, and an upload of the corpus clip with them.
uv run python -m tests.live.run_live --urls tests/live/urls/media.csv \
  --upload tests/media/corpus/speech_de.ogg --timeout 120
```

Package 6 is done when every YouTube and Mediathek row that has subtitles answers
`manual_subtitles` or `auto_subtitles` and the upload is transcribed (`asr`). From a data-centre
IP (the GitHub runners) YouTube may refuse the service: the answer is then the video's details
(`metadata_only`, `partial`/`no_transcript_available`, warning `subtitles_blocked`; decision D1),
so CI reports the media set but does not judge it. Run it from an office or home line for the
done-when check.

The upload alone, without the harness (`$KEY` only with `AUTH_MODE=api_key`):

```bash
curl -H "Authorization: Bearer $KEY" -F file=@tests/media/corpus/speech_de.ogg \
  http://localhost:8080/v1/extract/file          # 202 with data.status_url
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/jobs/<job id>
```

## The site run

`run_site.py` maps each site of a list with `POST /v1/site-structure` (package 9) and reports what
the crawl found. The crawl is a `site` job, so the service needs an extract worker
(`uv run text-extraction-worker`; give it `INTERNAL_PORT=9092` next to the API):

```bash
uv run python -m tests.live.run_site --urls tests/live/urls/sites.csv --depth 1 \
  --max-pages 25 --compliance report
```

Each site is one request (`depth`, `max_pages`, `compliance` as given, `cache: false`); all sites
run at once (each is a host of its own, so the per-host limits keep them apart). The harness reads
the job every `--poll-seconds` until it is `completed`, then all its items in pages of 500, and
prints a Markdown summary: per site the job's state and duration (created to finished), its items,
page items, page items with `success`, failed items, sitemap files, further sitemap parts and
sitemap entries; a histogram of the pages' links by `region` and one by `crawl` decision; and the
warnings and error codes of the items. A request the service refuses (a 4xx or 5xx) or a job that
does not complete within `--job-timeout` is reported in the State column, with what the job had
so far. The exit code is always 0: the run is a report.

| Option | Default | Meaning |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8080` | The service's public API. |
| `--api-key` | env `LIVE_API_KEY` | As for `run_live.py`. |
| `--urls` | `urls/sites.csv` | CSV with a `url` column: the start pages. |
| `--depth` | 1 | The request's `depth`. |
| `--max-pages` | 25 | The request's `max_pages`. |
| `--compliance` | `report` | The request's `compliance` (`report`: nothing is blocked, the reports say what `enforce` would block). |
| `--timeout` | 60 | Deadline of one HTTP request in seconds. |
| `--job-timeout` | 1800 | Seconds to wait for a site's job to complete. |
| `--poll-seconds` | 5 | Seconds between two reads of a job. |
| `--out` | `results/site_<UTC time>.jsonl` | One JSON line per site, the numbers of the summary. |
| `--summary-out` | none | Also write the Markdown summary to this file. |

The summary's formatting and paging are unit-tested against a fake service in
`tests/test_live_site.py`. The build sandbox of package 9 had no route to the five sites, so the
run is a report-only step of the live workflow (below); its summary goes into the pull request.

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

`.github/workflows/live.yml` starts Valkey, the service and the browser worker (`BROWSER_ENABLED=true`,
`BROWSER_UNBLOCK_ENABLED=false`: the gate measures standard behaviour; Playwright's Chromium is
installed from the source checkout; the worker's internal port is 9091), waits for both `/ready`,
runs the harness, adds the summaries to the job summary and uploads the JSONL files and the server
and worker logs as the `live-results` artifact.

- **Manually** (Actions, "live", Run workflow): choose `url_set` and `min_ok_rate` (default 80), run
  in `auto` mode, then the reports below. With `url_set=smoke` the run is a report only;
  `min_ok_rate` does not apply. `service_ref` (a branch, tag or full commit SHA) measures the
  service of that commit with this run's harness and URL lists: start one run with it and one
  without, at the same time, for an A/B comparison under the same site conditions.
- **Pull requests** that carry the label `live`: `wlo_200` with `--mode auto --min-ok-rate 80` is
  the gate. Reported alongside, with no gate: `smoke` with `--mode auto`, and `wlo_200` with
  `--mode browser`. A new push or the `live` label starts a new run and cancels the one in
  progress; adding any other label does neither.
- **Weekly**, Mondays 03:00 UTC: the same as for pull requests.
- **Every run** also reports the media set (`media.csv`, `--mode auto --no-transcribe`; no ASR
  worker runs there), never as a gate: YouTube may refuse the runners' data-centre IPs (D1).
- **Every run** also reports the site run (`run_site.py` on `sites.csv`, depth 1, 25 pages per
  site, `compliance: report`), last and never as a gate: it starts an extract worker (internal
  port 9092; the runs before it measure the rig without one), the step may fail without failing
  the job (`continue-on-error`, at most 20 minutes, `--job-timeout 900`), and its summary goes to
  the job log and the job summary; the worker's log is in the artifact.

If the gate misses, do not tune a setting first: compare the buckets per domain, fast against
browser, and debug the domains that differ. When the ok rate drops between two commits, run both
with `service_ref` side by side and compare their "URLs not ok" tables: a URL that fails in both
runs is the site, a URL that fails only under the newer service is a regression.
