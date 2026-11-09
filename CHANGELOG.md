# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is the same in
`pyproject.toml`, in `deploy/helm/text-extraction/Chart.yaml` (`version` and `appVersion`) and
in the image tags; a release is the Git tag `vX.Y.Z` on `main`.

## [Unreleased]

## [0.1.9] - 2026-11-09

### Changed

- A page answer with status 403, 429 or 503 that is a bot challenge now fails `bot_protection`
  instead of `target_http_error`, in fast and in browser mode. It is a challenge when the answer
  has Cloudflare's `cf-mitigated: challenge` header, or when the head of its body is a challenge
  page (Cloudflare's "Just a moment...", `cf-chl` markup, Anubis, "Checking your browser").
  `upstream_status` keeps the site's status, `retryable` keeps the rule of `target_http_error`,
  and `error.message` names Cloudflare when the answer says so. Unblock mode still tries such a
  refusal in the browser; the respect mode still does not. Found on leifiphysik.de, whose
  Cloudflare protection answers the standard browser's `HeadlessChrome` user agent with a
  challenge. The browser's user agent is unchanged. Counts by error code move: the metrics'
  `outcome` label moves such answers from `target_http_error` to `bot_protection`, the live
  harness from the bucket `target_http_error` to `likely_error_page`; the success rate is
  unaffected. Other vendors' challenges (DataDome,
  PerimeterX, Akamai) are not recognised yet.

- docker compose and `.env.example` switch unblock mode on (`BROWSER_UNBLOCK_ENABLED=true`, Jan's
  decision of 2026-11-09). It still applies only to an admin's requests with
  `compliance: ignore`, for sites that allowed extraction by contract; `false` in `.env` switches
  it off. The code's default and Helm's `workers.browser.unblock` stay off.

### Added

- The playground explains a `bot_protection` error: the site's operator can allow the service, or
  an admin can use "Checks off (contract partners)" with the unblock browser
  (`BROWSER_UNBLOCK_ENABLED`). An admin whose server runs the browser without unblock mode is told
  so at the "Checks off" choice.
- User guide (English and German), "Site rules": a section on sites behind a bot protection.

## [0.1.8] - 2026-11-09

### Changed

- The root `docker-compose.yml` runs the published images and starts without a single variable,
  so Hostinger's Docker Manager ("Compose from URL", which takes only the root
  `docker-compose.yml` and copies `.env.example` to `.env`) and other Docker panels can deploy it
  from the repository. Before, the first deploy stopped at the required `VALKEY_PASSWORD`, which
  `.env.example` leaves empty. The file has no YAML anchors or merge keys, no required or nested
  variables, no build and no bind mounts, and every long-running service restarts
  `unless-stopped`.
- The former `docker-compose.yml`, which builds the images, is now `docker-compose.build.yml`
  (`docker compose -f docker-compose.build.yml up --build -d --wait`); it still needs `API_KEYS`
  and `VALKEY_PASSWORD` in `.env`. `docker-compose.release.yml` is gone: the root file replaces it.
  The load test and CI use the build file.

### Added

- `text-extraction-init` and the one-shot compose service `init`: on the first start they create
  the Valkey password and an admin API key in the volume `secrets` and keep them afterwards. The
  key is logged once (`init_admin_key_created`); `docker compose run --rm -T init
  text-extraction-init --show-admin-key` prints it later. Valkey reads its password from the
  generated `valkey.conf` (with a user `healthcheck` that may only `PING`).
- `VALKEY_URL_FILE`: a file that holds the Valkey URL (secret content), winning over `VALKEY_URL`.
- Traefik labels on the `api` service of the root compose file (`DOMAIN`, `TRAEFIK_NETWORK`,
  `TRAEFIK_ROUTER`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER`), a named default network and
  `API_PORT` for the published port. `init` refuses a `DOMAIN` that is not a bare host name.
- Installation guide (English and German): published images, Hostinger VPS, building from the
  checkout.

## [0.1.7] - 2026-11-08

### Changed

- docker compose starts the ASR worker by default: transcription of audio and video uploads and
  of `transcribe: true` is on without the profile `asr` (which is gone). `ASR_ENABLED` in
  `.env.example` is `true`; `ASR_ENABLED=false` with `--scale asr-models=0 --scale asr-worker=0`
  switches it off. The service's own default and the Helm chart (`workers.asr.enabled`) are
  unchanged. An existing `.env` with `ASR_ENABLED=false` keeps transcription off: set it to `true`.
- docker compose starts every optional worker by default (package 19): the Docling, NLP and llm
  workers have no profiles any more (`docling`, `nlp`, `llm` are gone), and `DOCLING_ENABLED`,
  `PII_ENABLED` and `LLM_ENABLED` are `true` in compose and `.env.example`. Each switch at `false`,
  with `--scale` of the feature's services to 0, switches a feature off. The LLM still needs a key
  (`LLM_API_KEY` or the caller's `X-LLM-Api-Key`). The Helm chart is unchanged.
- uv is pinned to the Dockerfile's minor (`[tool.uv] required-version = ">=0.11,<0.12"`), and
  Dependabot ignores `uv-build` until uv and uv_build are raised together.
- A test checks that every `.env.example` and Helm `config:` value equals its `Settings` default,
  apart from a documented allow-list.
- Dependencies: fastmcp 4.1.3, openai 3.37.0, patchright 1.64.2, reportlab 5.1.0 (#47); GitHub
  Action astral-sh/setup-uv 10.3.2 (#46).

### Added

- `OCR_ALLOW_USERS`, `ASR_ALLOW_USERS`, `PII_ALLOW_USERS` and `LLM_ALLOW_USERS` (default `true`)
  keep a feature for admin keys (package 19): a user key that asks for it gets 403 `forbidden`
  (`ocr: always`; `transcribe` or an audio or video upload; `pii`; strategy `llm`,
  `learn_selectors`, `semantic` chunking, `describe_images`); such a key's documents get no OCR
  (`ocr_unavailable`) and strategy `auto` runs without the LLM (`llm_unavailable`). The playground disables
  such a choice with a hint.
- Strategy `auto` with a schema and no LLM key at all (neither `X-LLM-Api-Key` nor `LLM_API_KEY`)
  runs without the LLM (`llm_unavailable`) instead of answering 400: compose now switches the LLM
  on without a key, and the playground's default Structured request must work. LLM work that
  names the LLM (`llm`, `learn_selectors`, `semantic`, `describe_images`) still answers 400.
- The playground offers the optional features as choices (package 19): "Optional features" in
  Extract, File and Batch (OCR automatic/never/always, personal data, "Transcribe" for Extract and
  Batch), a "Strategy" select in Structured and a "Method" select in Chunk. Nothing is chosen by
  default; a feature the service has switched off is disabled with a hint naming its switch, and
  the LLM choices say when the caller's LLM key is needed.
- Helm: optional PodDisruptionBudgets (`podDisruptionBudget.enabled`, `maxUnavailable: 1`) for the
  API and every worker pool with more than one replica; on in the production example.

### Fixed

- ASR off no longer breaks the worker: with `ASR_ENABLED=false`,
  `text-extraction-asr-worker --download-models` and `--check` exit 0 without a model (log event
  `asr_disabled`), and the worker idles on `/health` and `/ready` without loading a model or
  taking queue items; in docker compose the ASR services then idle.
- The site tree's rule checks are bounded: rule files are loaded only for the sites the crawl
  itself visited (a site that a sitemap or a link merely names is read from the cache, else shown
  as not checked), the tree judges with the first 1,000 rules of each robots.txt and ai.txt user
  agent and the first 1,000 tdmrep.json entries (a fetch still reads the whole file), the
  judgement runs on a thread of its own within 1 s (a view that cannot get it shows the sites as
  not checked instead of waiting behind conversions), and a live refresh judges only the URLs
  that are new. A hostile 500 KiB rule file cost a view about 5 s of CPU, at every refresh. tdmrep.json
  is parsed off the event loop and once per file text, like robots.txt. A sitemap entry without a type no longer clears the type a link
  text gave a URL in the tree.
- `discovery: sitemap_first` no longer trusts a `Sitemap:` line in robots.txt blindly: the first
  listed sitemap is probed like the guessed addresses, and one that answers 404 or is no sitemap
  (an HTML page) counts as absent, so the page's links are crawled instead of being left unvisited.
- Page types: a schema.org `Event`, `Course`, `Quiz` or `LearningResource` makes a page `event` or
  `learning_material` only as the page's one main entity (the `mainEntity` of its `WebPage`, its
  only such JSON-LD node, or microdata on `<html>`, `<body>`, `<main>` or `<article>`), and only
  an address that is a type name alone (such as `/impressum` or `/de/impressum`) wins over it, so
  `/kurse/datenschutz` with one `Course` stays `learning_material` as in 0.1.6. An article with an
  event in its sidebar stays `content`, and a page of several events is not typed `event` by
  schema.org (it is `listing` when it is mostly links). An article (`og:type` `article`, enough
  words) below a type name with a slug of words, such as `/datenschutz/schuelerfotos-im-unterricht`, is `content`. A
  link text such as "Anmelden" or "Datenschutz" no longer types a linked URL whose last part is a
  slug of words, and in the site tree it never replaces a sitemap's default "Content", so "Select
  content pages" keeps such pages checked.
- PHP error removal keeps a message that the page quotes: in a quote, a heading, a list item, a
  table cell, a definition list or a figure caption only the `html_errors` form (bold label and
  bold path) is removed, and a message copied as plain text stays in `content`. It now also
  recognises Drupal paths (`.module`, `.theme`, `.install`, `.engine`) and removes Xdebug's
  message tables, each listed once in `data.server_errors`.
- Rolling upgrades from 0.1.5: a site job with the default `discovery: crawl` no longer stores
  `discovery`, so pods of 0.1.5 read it; only `discovery: sitemap_first` needs every pod on the new
  release. Results read a page type, a link category or a `crawl` value of a later release as
  `other`, `content` and `listed` instead of failing, so later new values need no upgrade order;
  requests still refuse unknown values (422) and the OpenAPI spec is unchanged.
- The playground names switched-off transcription ("Transcription is switched off in this
  service") instead of "This kind of content cannot be converted" when an audio or video upload
  or `transcribe: true` meets a service without ASR.
- A short transcript is a `success`, not `partial` with `content_too_short`: subtitles, the
  transcript view and a transcription are all that the media says, so `QUALITY_MIN_WORDS` does
  not apply to them (a 27-word video was marked partial).
- Playground: on a phone the site tree's checkbox, path and page count no longer overlap from
  about level 6 (the indentation stops at 6rem, a page count or title moves under its path, a wide
  type pill ends in an ellipsis), and every checkbox keeps its target size; a long file path in
  "Server errors" wraps instead of widening the page; the status column's title holds the whole
  status and error code; paths, titles and link texts are bidi isolates, so a right-to-left
  override cannot reorder the text around them; the note on blocked URLs names only the "Site
  rules" choices the caller is offered; "The total grows while it crawls" shows only while the
  crawl runs. German: the "Checks off" hint says „die Grenzen für ausgehende Anfragen“ like its
  siblings.

## [0.1.6] - 2026-11-07

Package 18: the site tree after the v0.1.5 test (plan
[docs/superpowers/plans/2026-11-07-package-18-site-tree-types-columns-speed.md](docs/superpowers/plans/2026-11-07-package-18-site-tree-types-columns-speed.md)).
API changes: the site option `discovery`, the fields `SitemapEntry.type_guess` and
`SitemapInfo.type_hint` and the page types `event` and `learning_material`.

### Added

- The site tree guesses a type for every URL the crawl did not fetch, always marked "guessed", with
  the reason: the URL (rules), the file name of the sitemap that listed it (`type_hint`), the link
  text, keywords of the path or the link text, or "Content" by default for a URL with a readable
  name. A bare default guess gives way to the reason of another source. Only opaque id-like URLs
  stay "Type open".
- The page types `event` (Veranstaltung) and `learning_material` (Lernmaterial): from schema.org
  (`Event` and its subtypes; `LearningResource`, `Quiz`, `Course`), from event and material words
  in URLs, link texts and sitemap file names.
- Sitemaps carry `sitemap.type_hint`, read from their file name (Yoast and Rank Math
  `post-sitemap`, WordPress `wp-sitemap-posts-*` and the like).
- Without a sitemap in robots.txt the crawl tries `/sitemap.xml`, then `/sitemap_index.xml`, then
  `/wp-sitemap.xml`; a 404, a 410 or a 2xx that is no sitemap (a CMS answering with an HTML page)
  moves on to the next address (the position is `SiteNode.guess`).
  A site without any sitemap costs up to two extra 404 fetches per crawl.
- Each sitemap entry has `type_guess`, the page type guessed from its URL and the sitemap's
  `type_hint` (omitted when the URL says nothing); about 24 bytes per entry.
- `POST /v1/site-structure` and the MCP tool `site_structure` take `discovery`: `crawl` (default)
  or `sitemap_first`, which reads only the start page when a sitemap exists and takes the other
  pages from it (links not followed say `crawl: sitemap_first`); with `sitemaps: false` it is 422.
- The playground's site tree has aligned columns (page, type, found, status, depth) under a sticky
  head, compact rows when the tree is narrow, chips to filter by type, page counts and the type of
  each section, link texts in quotes as titles, and a summary line with counts and duration.
- The tree appears by itself once the job has a result and refreshes while the job runs. A refresh
  keeps ticks, folds, the filter, the batch options, the scroll position and the focus; the
  interval grows with the cost of reading the job, retryable failures are tried again, and the
  batch below the tree survives a refresh.
- The site form has "How to read the site": "Fast: sitemap first" (the preset) or "Thorough: follow
  links".
- The live harness `tests/live/run_site.py` takes `--discovery` (the live workflow's
  `site_discovery` runs both) and measures the sitemap state, the tree URLs, the types
  left open (`type open %`), the median convert time and the crawl time, and waits out the
  service's own 429 `Retry-After`.

### Changed

- `OUTBOUND_MIN_DELAY_MS` defaults to 500 (was 1000): the least gap between the starts of two
  requests to one host is halved; in the live harness a 25-page crawl of wirlernenonline.de
  took 26 s instead of 46 s. robots.txt `Crawl-delay` still wins where it is longer. Operators
  who want the old pace set `OUTBOUND_MIN_DELAY_MS=1000`.

### Upgrade

- Update the API pods first, then the workers. Pods of 0.1.5 and older cannot read results with
  the page types `event` or `learning_material` or a link with `crawl: sitemap_first` (extract,
  batch and site jobs), and they refuse every site job of 0.1.6, which stores `discovery` also at
  its default `crawl` (0.1.7 stores it only for `sitemap_first`). Create site jobs only once every
  pod runs 0.1.6 (see `docs/configuration.md`, "Upgrading to 0.1.6 or 0.1.7").

## [0.1.5] - 2026-11-06

Package 17: the playground after the v0.1.4 test (plan
[docs/superpowers/plans/2026-11-06-package-17-playground-after-v014-test.md](docs/superpowers/plans/2026-11-06-package-17-playground-after-v014-test.md)).
The only API change is the new warning code `server_errors_removed` and the optional result field
`data.server_errors`.

### Added

- PHP error output that a site prints into its pages (`Warning`, `Notice`, `Deprecated`,
  `Fatal error` and the like, "in /path.php on line N") is removed from the text, except inside
  `pre`, `code`, `kbd`, `samp`, `tt`, `var`, `textarea` and `script`. The result carries the warning `server_errors_removed`
  and the new optional field `data.server_errors` (level, message, file, line, count; at most 50,
  identical messages merged; null when there are none). The playground lists them in a closed
  section after "Images".
- The site tree guesses the type of URLs that were not fetched from more rules: author pages
  (`author`, `autor`) are listings, more contact names are contact pages, and dated paths
  (`/YYYY/MM/slug`) and WordPress `?p=` and `?page_id=` addresses are content, marked as guessed.
  A URL that no rule knows shows the badge "Type open" with its reason, and the types line counts
  it.
- The site tree lists the site's social media profile links in a "Social media" list below the
  tree. They cannot be selected.
- Every section of the site tree can be folded and unfolded (the top level starts open, the levels
  below folded), with "Unfold all" and "Fold all". Without JavaScript the tree stays unfolded.
- The extract form has the box "Also take a screenshot": a whole-page PNG taken in the browser,
  shown in a closed "Screenshot" section of the result. The box is disabled with "Browser
  fallback: never" and while the browser is off.

### Changed

- In the site tree a section that holds only its own page is one row.
- The playground's settings are a card, and the result area shows an empty card until a result
  arrives.

### Fixed

- Inline content in a block whose first child is an inline element or a `<br>` stays one
  paragraph, with its spaces, instead of falling apart into pieces.

## [0.1.4] - 2026-11-06

Package 16: robots marks in the site tree and the site rules switch (plan
[docs/superpowers/plans/2026-11-06-package-16-robots-tree-compliance-switch.md](docs/superpowers/plans/2026-11-06-package-16-robots-tree-compliance-switch.md)).
No API change.

### Added

- The playground's site tree marks URLs that the site's rules block: the badge "robots.txt"
  (robots.txt disallows, or could not be retrieved) or "TDM" (tdmrep.json, ai.txt or noai), with
  the reason as tooltip and for screen readers. Every shown URL is judged when the tree is shown,
  against the site's current rules (from the cache, or loaded once per site within the per-host
  limits; at most 20 sites, 12 s). Links the crawl did not follow because the rules blocked them
  appear as "linked only" leaves. A line above the tree counts the blocked URLs, and another names
  sites whose rules could not be read or checked in time. Under "Respect the site's rules" blocked URLs cannot be
  ticked and the selection buttons skip them. Closes #26.

### Changed

- The playground's "Site rules" choice has three steps in every form with a URL and under the
  site tree: "Respect the site's rules" (`enforce`), "Check and report only" (`report`) and
  "Checks off (contract partners)" (`ignore`). The deployment default is preselected and sends
  nothing. Not only admins see it: other keys see `enforce` and `report` while
  `COMPLIANCE_ALLOW_OVERRIDE` is on. "Checks off" works without unblock mode; with unblock mode
  on it is maximum extraction as before. The batch below the site tree starts with the step the
  crawl ran with.

## [0.1.3] - 2026-11-05

Package 15: the playground's new look (plan
[docs/superpowers/plans/2026-11-03-package-15-playground-redesign.md](docs/superpowers/plans/2026-11-03-package-15-playground-redesign.md)).
No API change.

Package 14: screenshot formats, SVG previews and page types (plan
[docs/superpowers/plans/2026-11-02-package-14-screenshots-svg-page-types.md](docs/superpowers/plans/2026-11-02-package-14-screenshots-svg-page-types.md)).

### Added

- The result's toolbar in the playground has "Copy Markdown" (for text output "Copy text"), which
  copies the shown text where the browser allows clipboard access, and "Save response (JSON)",
  which saves the whole envelope as a file (above 10 million characters (fixed in code) the toolbar
  points to the `curl` command instead).
- Screenshots in other formats and sizes. `POST /v1/screenshot` takes `viewport` (the browser
  window, 320 to 3840 by 240 to 2160 px, default 1920×1080; a narrow one gets the page's mobile
  layout), `format` (`png`, `jpeg` or `webp`, default `png`), `quality` (1 to 100, default 80;
  `jpeg` and `webp` only) and `width` (scales the image down to that width, keeping its aspect
  ratio; never up), and `POST /v1/extract`, `/v1/structured` and `/v1/chunk` take them as
  `screenshot_options`. The browser worker encodes and scales the capture with Pillow. The file
  is `screenshot.png`, `screenshot.jpg` or `screenshot.webp`, and `data.screenshot` names it.
- The playground's Screenshot tab has the choice "Capture": the whole page, the first screen, a
  social preview (1200×630, WebP), a thumbnail (400 px wide, WebP), a phone (390 px wide, the
  whole page) and custom values. `screenshot_url` takes the new fields in `options`, and both
  MCP screenshot tools attach the image with its own type.
- The playground's image preview shows SVG images, as a `data:` image inside `<img>`, where no
  script in them runs. Image descriptions still skip SVG.
- Page types: every HTML page that is fetched or rendered and converted has `page_type` (its
  `type`, such as `content`, `listing`, `imprint` or `contact`, and the `signals` that say so),
  found by rules without an LLM, in `/v1/extract`, batches and site structures. Every entry of
  `links` and `site.links` has a `category` (the page types, plus `social`, `download` and
  `nav`). Both are null in results stored before.
- The site tree shows the page type of each URL (for a sitemap entry or a linked-only URL
  guessed from the URL alone), counts the types, and has the button "Select content pages",
  which leaves imprint, privacy, legal, contact, login, search and error pages unchecked.

### Changed

- The playground has a new look: a white header with the name, the language switch and the tabs;
  the settings on the left and the result on the right (stacked on a phone); a status line, a
  card of figures (with page type and time), closed cards for metadata, links, images and "What
  happened", and the text in a white reading card in a serif font. Colours, radii and fonts are
  tokens, and the form fields keep a 3:1 boundary. It works at phone width, with visible focus
  rings and without animation for `prefers-reduced-motion`.
- "Wait seconds" (where a form has it) and "Options (JSON)" are under a closed "Advanced"
  section; a form error in them opens it.
- A Markdown result shows the rendered text in the reading card; the Markdown source is under
  "Show source" (before, the source was shown and the rendered text was under "Show rendered").
- `auto` mode also tries the browser on a 2xx HTML page that is too short or empty
  (`content_too_short`, `empty_content`) when the page has a script. In A/B live runs on
  `wlo_200` the usable share rose from 83.0 % to 83.5 % and 84.5 % with no higher median time per
  URL; such pages take longer.
- A full-page screenshot in a window wider than 1920 px is cut at `SCREENSHOT_MAX_HEIGHT_PX` ×
  1920 / the window's width, so it never holds more pixels than a 1920 px wide one.
  `SCREENSHOT_MAX_BYTES` applies to the encoded file. A `webp` image is at most 16,383 px high
  (the WebP limit): a taller one loses its bottom rows, with the warning `screenshot_clipped`.
- A render whose screenshot is encoded (`jpeg`, `webp` or a `width`) keeps 10 s of its time for
  the encode, at most a third of the render time. Encoded screenshots need a
  `BROWSER_TIMEOUT_SECONDS` of about 8 s or more (a plain PNG about 6 s).
- `GET /v1/jobs/{id}/artifacts/{name}` also serves `screenshot.jpg` (`image/jpeg`) and
  `screenshot.webp` (`image/webp`); `get_job_screenshot` reads the file's name from the job's
  result instead of assuming `screenshot.png`.
- Pillow 13.0.0 is a direct runtime dependency (the browser worker imports it); before, it came in
  only through other dependencies.
- The 14-day rule applies again to docling-core, docling-parse and docling-ibm-models: their
  `exclude-newer-package` entries are back to "14 days" instead of the fixed date
  2026-10-23 that v0.1.2 used. The locked releases (2.104.0, 7.23.0, 4.0.4) are now older than
  14 days, so no package changes.
- Dependency updates: openai 3.29.0 → 3.35.1 → 3.36.0, patchright 1.63.0 → 1.64.1 and
  ctranslate2 4.8.2 → 4.8.3 (PR #36); yt-dlp 2026.8.19 → 2026.10.21, markitdown 0.1.8 → 0.1.9
  (with mammoth 1.11.0 → 1.13.0), av 19.0.1 → 19.0.2 and deno 2.9.7 → 2.9.8; joserfc 1.7.5 →
  1.7.7 and setuptools 84.0.0 → 84.2.1 for the advisories CVE-2026-119284 and
  GHSA-xmj3-9gf7-h52w, neither of which is reachable in the service.

### Fixed

- `POST /v1/extract` with an invalid URL answers `mode_used: null` in every mode (before, `fast`
  in `auto` and `fast` mode, `null` in `browser` mode).
- The default screenshot is described as the whole page as PNG, 1920 px wide (window
  1920 x 1080), in the OpenAPI texts and the MCP tool `screenshot_url`, which now names `options`
  and the tool's own `full_page` argument.
- Screenshot encoding is bounded by pixels and keeps its slot until the encoding thread ends; the
  memory figures in configuration.md and architecture.md follow.
- Page types: stricter rules for queries, path positions, headings and articles (A26-R1 to
  A26-R5), described in the user guides and architecture.md. A language parameter (`lang`, `hl`,
  `locale`) keeps a start page home (`/?lang=en`).
- Playground: the "Custom capture" box no longer has a heavy dashed frame, an error in "Options
  (JSON)" is marked on that field, the page-type badge in the site tree has its space and stays
  inside the tree, the result cards have real headings, invalid fields are marked visibly, the
  reading card carries the content's language, and counts use plural forms.
- Documentation: SVG previews, the package 15 playground view, the `connect-src 'self'` note,
  the Patchright and CTranslate2 versions, the READMEs' package list, the parameter bounds in the
  user guide and German wording and terms (Soft-404) match the code.

### Upgrade

- Update the browser workers (and the other workers) before the API pods. A job whose options
  carry `screenshot_options` (package 14) is refused by 0.1.2 workers (`extra="forbid"`), and a
  0.1.2 API pod cannot read such a job. Results with `page_type` and a link `category` are read
  by old pods.

## [0.1.2] - 2026-11-01

Dependency updates that v0.1.1 left out. Jan waived the 14-day rule for new dependency versions
(CONTRIBUTING.md) for these on 2026-11-01.

### Changed

- fastmcp 4.1.2 (was 4.1.0): fixes advisories in code paths the MCP endpoint does not use.
- Pillow 13.0.0 (was 12.3.0), with the Docling releases that allow it: docling-slim 2.142.1 (was
  2.137.0), docling-core 2.104.0, docling-parse 7.23.0 and docling-ibm-models 4.0.4. Pillow 13
  fixes the TIFF out-of-bounds reads of 12.3.0; the image check of uploads already opened only
  the formats it sniffs.

### Fixed

- CHANGELOG: the entry for `docker-compose.release.yml` in 0.1.1 named the default version
  `0.1.0`; the default is the version of the release the file comes with.

## [0.1.1] - 2026-11-01

Fixes from the audit of v0.1.0 (plans in
[docs/superpowers/plans/2026-11-01-api-audit-docs-release.md](docs/superpowers/plans/2026-11-01-api-audit-docs-release.md)
and
[docs/superpowers/plans/2026-11-01-remaining-audit-findings.md](docs/superpowers/plans/2026-11-01-remaining-audit-findings.md)).

### Added

- `tests/e2e/audit_probes.py`: hostile probes against a running service, run by hand before a
  release (a redirect loop, a 30 MB body, a gzip bomb, a slow drip, binary and mis-declared
  pages; with `--ssrf`, private addresses in their various spellings and forbidden schemes).
- `docker-compose.release.yml`: runs the stack of `docker-compose.yml` with the published
  images of a release (`TEXT_EXTRACTION_VERSION`, default: that release's version) instead of
  building them.
- The `release` workflow: a pushed tag `vX.Y.Z`, or a manual run for an existing tag, puts the
  tag's section of this file on its GitHub release page, with the images and where to start
  (`scripts/release_notes.py`); a `0.x` release is marked as a pre-release.
- The OpenAPI document has request examples for `POST /v1/extract`, `/v1/extract/file`,
  `/v1/screenshot` and `/v1/batch`, and the description of `ErrorCode` explains every code.
- The OpenAPI document declares the bearer key (`AUTH_MODE=api_key`), the error envelope of
  every error answer (instead of FastAPI's `HTTPValidationError`), the error statuses each
  operation can answer, and a tag per area.

### Changed

- `/ready` no longer fails while Valkey is down: the pods stay in the Service and answer 503
  `internal_error` (retryable, with the trace id) instead of dropping out behind the ingress's
  bare 503. The new gauge `text_extraction_valkey_up` and the Helm alert
  `TextExtractionValkeyDown` (critical, 2 minutes) report the outage.
- MCP tool answers are bounded as a whole by `MCP_MAX_ANSWER_CHARS` (new, 200,000 characters):
  a `get_job` page drops its trailing items, then a result's `chunks`, `links`,
  `structured.tables` and `site.links` are cut from the end, with the warning
  `mcp_answer_truncated`. Before, only `content` was cut and an answer could reach megabytes.
  The budget counts the envelope JSON, which an answer carries twice; with MCP on, the service
  refuses to start when it is not larger than `MCP_MAX_CONTENT_CHARS`.
- JSON request bodies are read strictly: a value of the wrong JSON type (`"5"` for a number,
  `"yes"` for a boolean) answers 422. The form fields of `POST /v1/extract/file` are unchanged.
- A JSON route answers 415 `invalid_request` for a body sent with another `Content-Type`,
  instead of a 422 about a missing body. A body without a `Content-Type` is read as JSON, as
  before.
- The service's own pause of a host (after errors or a 429) answers `rate_limited` instead of
  `connection_error`, still retryable; a refusal of the outbound gate carries the wait in
  `error.details` (`retry_after_seconds`), in the extract and the ASR workers alike.
- Under `AUTH_MODE=none` the public port answers only requests whose `Host` is in
  `ALLOWED_HOSTS` (new; `localhost,127.0.0.1,[::1],api` by default, `*` switches the check off),
  so a web page cannot reach the service through DNS rebinding. Add the host name your clients use.
  An empty list under `AUTH_MODE=none` stops the start (exit code 2); the Helm notes name the
  setting.
- One counted call does about one unit of work: MCP `initialize` and `tools/list` count one
  rate-limit hit each, a waiting `get_job` one more per further 10 s, the UI's image previews
  one per image and its site tree one per 500 items; the UI reads a screenshot's file without a
  second hit.
- `JOBS_ENABLED=false` makes the job routes answer 400 `invalid_request` instead of 503
  `internal_error`, like every other switch that is off. A retryable 503 carries
  `Retry-After: 5`.
- OpenAPI operation ids are plain names (`extract`, `extract_file`, `get_job`, `delete_job`,
  ...) instead of FastAPI's path-derived ones (`extract_v1_extract_post`); a client generated
  from the document gets new method names.
- A job item that failed through a fault of the service before any step ran (a lost lease, an
  expired input) had `data: null`; it has `data` now, with `mode_used` and `upstream_status`
  null, `attempts` 0, no content and zero timings, so every item carries the envelope's fields.
  `mode_used` may therefore be null.
- A host of digits that is no IPv4 literal (`0177.0.0.1`, `127.1`, `2130706433`) is
  `invalid_url` now. `0177.0.0.1` failed its robots.txt as `blocked_by_robots` (retryable),
  since the HTTP client refuses it; no such URL was ever fetched.
- A gzip bomb in browser mode kept Chromium decompressing until the render timed out (`timeout`,
  retryable, after about 27 s): the egress proxy counts the bytes on the wire. The renderer now
  counts the decoded bytes too and ends the render with `too_large` once they pass twice
  `BROWSER_MAX_BYTES`. A request with the caller's `headers` was decoded whole in the browser
  driver's memory first; the service fetches it itself now, through the page's proxy, and
  counts as it reads.
- A page or upload declared UTF-8 whose bytes are not (a Latin-1 page behind a default UTF-8
  header) came out with every umlaut as U+FFFD; it is read as windows-1252 (or the legacy
  encoding its bytes fit) now, with the new warning `charset_mismatch`.
- A body declared `text/html` that starts with a document's or image's signature (a PDF behind
  a `download.php`) answered `partial` with a page of U+FFFD characters; it is converted as that
  document now, as a declared `text/plain` one is. A body declared `text/html` that is binary
  data (more than 2 % of its first KiB are bytes no text has; UTF-16 and UTF-32 text excepted)
  is `unsupported_content_type`, with a message that says so.

### Fixed

- A batch item moving to the Docling or ASR workers whose job was deleted between storing its
  bytes and its draft kept the bytes in Valkey and in `DOCLING_INPUT_BUDGET_BYTES` until their
  TTL; the move now gives them back at once. An enqueue whose reply is lost keeps them, since
  the item may be queued.
- Valkey: the client retried a command up to 10 times on a connection error, so a Lua script, a
  MULTI or a job claim lost in flight could run twice. Commands are no longer retried; instead a
  pooled connection idle for more than 5 s is checked with PING and opened again once.
- B3 trace headers: a job keeps them with its trace id and its workers log them; without
  `x-client-trace-id` the B3 trace id is the `trace_id`; the LLM and the external ASR endpoint
  receive `x-client-trace-id` and the B3 headers. CLAUDE.md no longer names OpenTelemetry, which
  the service does not use.
- `POST /v1/screenshot` and a browser-mode `POST /v1/extract` with an invalid URL started a job
  (202); they fail at once with `invalid_url`, as fast mode does, with `mode_used` null.
- A host name without a DNS form (a label over 63 characters, an empty label) answered 500;
  it is `invalid_url` now.
- Under `compliance: enforce`, a robots.txt that could not be fetched because of the host (DNS,
  connection, timeout, a blocked address) answered `blocked_by_robots`, not retryable; the page
  now fails with that error and its own retryable flag.
- More than five stacked `Content-Encoding`s answered 500; they are `unsupported_content_type`.
- Tables nested more than 16 deep held a conversion thread for half a minute; deeper tables are
  flattened before Trafilatura.
- The image check of uploads opens only the formats it sniffs (PNG, JPEG, TIFF, WebP, BMP), so
  Pillow's other decoders are not reachable.
- MCP refused request bodies over the SDK's 4 MiB with a plain-text 413, below the advertised
  upload limit; it takes bodies up to its own limit now.
- A page of job results read up to 500 stored items whole (about 1 GB); it stops at 20 MB and
  `next_offset` continues from there.
- A full Valkey (`noeviction`) made the rate limit answer 503 to every route, `DELETE` of a job
  included; the limit lets calls through uncounted then and logs `rate_limit_backend_full`.
- `partial` job items counted as failures in `TextExtractionJobItemFailures`.
- A worker pool scaled to zero by KEDA with `countLeased: false` never took back the items of
  expired leases; at `minReplicaCount: 0` the scaler counts leased items too.
- The 429 for too many unfinished batches and site jobs carries `Retry-After: 60`.
- `.env.example` lacked `LLM_GRACEFUL_SECONDS`, `UI_DEFAULT_LANGUAGE` and `UI_DOCS_BASE_URL`; a
  test pins it to the settings now.
- Documentation: an XPath selector of a user answers 422, not 403; the extraction metrics, the
  kinds of `text_extraction_job_duration_seconds` and every pool of `escalated`; the bounds of
  twelve settings; that `JOB_TTL_SECONDS` is not renewed while a job runs; the DNS-rebinding risk
  of `AUTH_MODE=none`.
- User guide and Helm README: the local uv recipe gives each process `AUTH_MODE=none` and its own
  `INTERNAL_PORT`; the quick start runs `docker compose up` detached; `auto` with a schema needs
  an LLM key once the service has an LLM; the 400s of `describe_images`, `ocr: always` and the
  browser options while their switch is off; the grace period of the API pods with browser
  workers; and that a floating `0.1` tag needs a `pull`.

### Not in this release

- fastmcp 4.1.2 and Pillow 13 (with docling-slim 2.142): they came in v0.1.2.

## [0.1.0] - 2026-11-01

The first release, a prototype: packages 0 to 13 of [docs/packages.md](docs/packages.md), tagged
`v0.1.0` on the merge of the package 13 pull request; packages 11 and 12 are merged without a
tag.

### Added

- Package 0, repo foundation: a uv project on Python 3.13, ruff, mypy, pytest, pre-commit and
  the GitHub Actions workflows for lint and tests.
- Package 1, service skeleton: settings from environment variables, the public port 8080 and
  the internal port 9090 (`/health`, `/ready`, `/metrics`), JSON logs with `x-client-trace-id`
  and B3 headers, the result envelope with its error codes, API keys with user and admin roles,
  an inbound rate limit in Valkey, a multi-stage non-root Dockerfile, SemVer image tags, a
  compose stack and the base Helm chart with its toggles.
- Package 2, fast extraction: an HTTP fetcher with an SSRF guard and limits, robots.txt and EU
  TDM checks, per-domain outbound limits, the Trafilatura pipeline with its quality gate, DOM
  metadata and licences, fingerprints, the result cache and `POST /v1/extract`.
- Package 3, jobs and batch: a fair job queue on Valkey with leases, the job API (create,
  status, paged results), the fallback from a sync request to a job, `POST /v1/batch` with fair
  per-domain scheduling, queue metrics, and KEDA, HPA and alert rules in the Helm chart.
- Package 4, browser worker: a Playwright pool, escalation from fast to browser mode in `auto`,
  `actions`, cookie-consent handling, challenge detection, full-page screenshots and
  `POST /v1/screenshot`.
- Package 5, documents and OCR: `POST /v1/extract/file`, MarkItDown, a Docling worker with PDF
  routing and a model volume filled by an init container.
- Package 6, audio and video: subtitles through youtube-transcript-api and yt-dlp, HTML5 tracks,
  a faster-whisper ASR worker and a switch for an external ASR backend.
- Package 7, PII: Presidio and spaCy for German and English with the modes `mask`, `redact`,
  `hash` and `annotate`, and the spaCy models from a volume.
- Package 8, structured output and chunking: the strategies `metadata`, `selectors`, `patterns`,
  `llm` and `auto`, learned selectors, an OpenAI-compatible LLM client that prefers the key of the
  request header, `POST /v1/structured`, `POST /v1/chunk` and five chunkers.
- Package 9, site structure: sitemap discovery through robots.txt, indexes and gzip, a link graph
  with link types to a set depth, and `POST /v1/site-structure` as a growing job.
- Package 10, MCP and UI: a FastMCP server at `/mcp` with 11 tools and the htmx playground at
  `/ui`, both off by default and switchable in Helm.
- Package 11, hand-over: a load test (`tests/load/`, run by hand in the `load.yml` GitHub Actions
  workflow against a local site farm) with the resource baselines from `docker stats`, the Helm
  sizing and an example production values file, the environment reference, the cost notes for the
  LLM and ASR, the hand-over document, Dependabot, this changelog and a `mode` input for the
  `live.yml` regression.
- Package 12, UI languages, unblock option and bilingual docs: maximum extraction, which falls
  back to the browser on a plain 403, 429 or 503 for an admin with `compliance: ignore` and
  `BROWSER_UNBLOCK_ENABLED`; the playground in German and English with a language switch and no
  cookie, a "Site rules" choice and a "Browser fallback" select, help texts and status messages,
  and checks for WCAG 2.2 A and AA with a vendored axe-core 4.14.0 and keyboard tests; and the
  READMEs and a user guide in German and English, kept in sync by tests and a pull request
  check.
- Package 13, YouTube transcript panel: a YouTube video whose subtitles could not be read gets a
  second try in the browser in `auto` mode, where YouTube serves its new transcript view
  (`transcript_source: transcript_panel`, `mode_used: browser`); otherwise its details answer
  with the warning `transcript_panel_unavailable`. The standard browser only, no request of its
  own to YouTube; the switch `MEDIA_TRANSCRIPT_PANEL_ENABLED` (default true, needs
  `BROWSER_ENABLED`) and the metric `text_extraction_media_transcript_panel_total`.

### Not in this release

- A full 4,000-URL regression run against the WLO set. The `live` workflow takes a `mode` input
  for it, and the run is started by hand later.
- A decision on permanent operation. This release is a prototype, and Jan Schachtschabel decides.

[Unreleased]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.9...HEAD
[0.1.9]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/janschachtschabel/text-extraction/releases/tag/v0.1.0
