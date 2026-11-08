# Audit fixes 2026-11-07 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every finding of the third full audit (main dfc3dc7) and prepare release v0.1.7.

**Architecture:** One branch (`claude/project-thread-possc5`), one PR. Each code finding gets a failing test first. Fixes stay in the modules that own the behaviour (no new modules except a Helm template). Docs change in English and German together.

**Tech Stack:** Python 3.13, FastAPI, pydantic, BeautifulSoup/lxml, Jinja2, plain JS and CSS, pytest, Playwright (browser marker), Helm, GitHub Actions, uv.

**Spec:** `/mnt/project-files/audit-2026-11-07/findings.md` (finding ids R1-R4, A1-A4, S1-S3, O1-O6, U1-U6, D1-D11, with file:line and repro). Reviewer evidence (repro inputs) is summarised per task below. Jan asked on 2026-11-07 18:41 UTC to fix all findings, update GitHub and set a new tag. S3 stays as is (Jan's package 16 request: users switch the site-rules check per run); only its documentation is added.

## Global Constraints

- English in code, comments, developer docs, commit messages and API texts. A change to an English user-facing text (`README.md`, `docs/user-guide/en/`, `ui/locales/en.json`) changes its German partner (`README.de.md`, `docs/user-guide/de/`, `de.json`) in the same commit; `tests/test_docs_i18n.py`, `tests/ui/test_i18n.py`, `uv run python scripts/check_translation_pairs.py --base dfc3dc7` must pass.
- German user docs use the formal "Sie" and the terms of `docs/user-guide/de/glossary.md` (Spielwiese, Website-Baum).
- Result envelope rule: target faults answer 200 with `status: failed`; 5xx only for own faults.
- No GPL/AGPL dependencies. 14-day rule: a pinned release must be at least 14 days old.
- Stateless pods; every setting an env var documented in `docs/configuration.md` and `.env.example`. This plan adds no setting.
- Avoid the files of the open package 19 PR #51 where a task allows it: `ui/forms.py`, `ui/templates/forms/features.html`, `docker-compose.yml`, `.env.example`, `docs/user-guide/*/installation.md`. Where a task must touch them, keep the edit small.
- No team names in docs; the contact is Jan Schachtschabel.
- Quality gates per commit: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, focused pytest. Full `uv run pytest -q` before the task's last commit; `uv run pytest -m browser <files>` where a task touches the UI's JS/CSS/templates or the browser path.
- Known timing flakes (rerun before chasing): `test_handover_during_fast_phase_then_escalation_writes_into_p1_job`, `tests/structured/test_selector_pool.py`, `test_upload_over_rate_limit_is_429_before_the_body_is_read`.
- Sandbox: public websites are blocked; use local fixtures. If you start redis-server or the app, use your own port and kill only by PID.
- Commit messages end with the session's attribution lines (the controller gives them).
- New rulings go into `docs/architecture.md` next to the existing ones, ids `AU3-R<n>` (third audit).

## Review Focus

1. A rolling upgrade: a 0.1.6 pod reads a job and a result a 0.1.7 pod wrote with a page type it does not know, and the reverse; a default site job stored by 0.1.7 is parsed by 0.1.5 `SiteOptions` (Task 1 tests).
2. A PHP tutorial quoting `Warning: ... in /var/www/x.php on line 3` in a blockquote, heading, list item or table cell keeps that text, while real PHP output (html_errors form `<br />\n<b>Warning</b>:  msg in <b>/path/x.php</b> on line <b>3</b><br />`) at body level is still removed (Task 2).
3. An article page with an Event in a sidebar, or a course page linked as "Anmelden", stays content / ticked (Task 3).
4. A robots.txt that lists a sitemap answering 404 under `sitemap_first`: links are followed as in `crawl` (Task 4).
5. The site tree at 390 px with a 12-level path and a long PHP path in the server-errors fold: no overlap, no horizontal scroll, axe clean (Task 6).

---

### Task 1: Rolling upgrade (R1)

**Files:**
- Modify: `src/text_extraction/jobs.py:499-521` (`_options_json`)
- Modify: `src/text_extraction/page_types.py` (result-side tolerant types), `src/text_extraction/site/types.py` (`Crawl` in results), the result models that carry `PageType`, `LinkCategory` and `Crawl` (find with `grep -rn "PageType\|LinkCategory\|Crawl\b" src/text_extraction --include=*.py`)
- Modify: `docs/configuration.md:630-660` (upgrade notes), `CHANGELOG.md` ([Unreleased] Fixed + correct the 0.1.6 "Upgrade" note), `docs/architecture.md` (ruling AU3-R1)
- Test: `tests/test_jobs*.py` (options), a new `tests/test_rolling_compat.py`

**Interfaces:**
- Produces: in result models, an unknown page type reads as `other`, an unknown link category as `other` (or the type's existing catch-all), an unknown `crawl` value as `listed`; request models stay strict (a request with an unknown value is still 422). `_options_json` also omits `site.discovery` while it is `crawl`.

- [ ] **Step 1: Failing tests.**
  - `_options_json(JobOptions(site=SiteOptions(), compliance="enforce"))` has no `discovery` key; with `discovery="sitemap_first"` it has it; the stored JSON round-trips to an equal `JobOptions`.
  - Compatibility against the 0.1.5 models: extract the 0.1.5 `SiteOptions` field set from git is not possible in a test; instead assert the stored `site` object's keys are a subset of `{"depth","max_pages","sitemaps","scope","metadata", ...}` = the 0.1.5 field names (copy the list from `git show 1b133a5:src/text_extraction/site/types.py`) when every new option is at its default.
  - `Envelope[ExtractResult].model_validate_json` of a result whose `page_type.type` is `"future_type"` succeeds with `type == "other"`; same for a link `category` and a site link `crawl: "future_crawl"` (→ `listed`).
  - A request (e.g. any request model field typed `PageType`, if one exists) with an unknown value still answers 422.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Omit `site.discovery` at its default (nested exclude, keep the existing omit logic). Add result-side annotated aliases with a `mode="before"` validator that maps a string outside the Literal to the catch-all and logs nothing (pure). Use them only in result/stored models, not in request models. Keep the OpenAPI enum of the result fields unchanged (use `json_schema_input_type` or keep the Literal in the schema).
- [ ] **Step 4: Docs.** configuration.md: add "Upgrading to 0.1.6 or 0.1.7 (package 18): API pods first, then workers": pods of 0.1.5 and older cannot read results with the page types `event`/`learning_material` or links with `crawl: sitemap_first`, and refuse site jobs with `discovery: sitemap_first`; from 0.1.7 on readers map unknown page types, link categories and crawl values to the catch-all, so later new values need no order. Correct the package 14 sentence that says old pods read new results. CHANGELOG: fix the 0.1.6 Upgrade note the same way; add the fix under [Unreleased].
- [ ] **Step 5: Run focused + full tests. Commit** `fix: keep default site jobs and new result values readable across a rolling upgrade`.

### Task 2: Server-error stripping (R2, A1)

**Files:**
- Modify: `src/text_extraction/convert/server_errors.py`
- Modify: `docs/architecture.md:160-175`, `docs/user-guide/en/single-url.md:90-97` and `de/single-url.md` (where the server-error removal is described), `CHANGELOG.md`
- Test: `tests/test_convert_server_errors.py`

**Interfaces:** `strip_server_errors(soup) -> list[ServerError]` keeps its signature.

- [ ] **Step 1: Failing tests.**
  - Kept (not removed, no `server_errors` entry): a plain-text message `Warning: Cannot modify header information - headers already sent by (output started at /var/www/html/config.php:12) in /var/www/html/index.php on line 5` inside `<blockquote>`, `<q>`, `<h2>`, `<li>`, `<td>`, `<th>`, `<dd>`, `<figcaption>`, and in a `<p>` of a forum post after a `<br>` (`<p>Hallo,<br>nach dem Update sehe ich:<br>Warning: ... on line 5<br>Was tun?</p>`); also `<p><strong>Notice</strong>: Undefined index: x in /var/www/a.php on line 7</p>` inside a `<blockquote>`.
  - Still removed: real PHP output at body level and in a `<div>`/`<p>` outside those contexts, both the plain form and the html_errors form (`<br />\n<b>Warning</b>:  foreach() argument must be of type array in <b>/var/www/a.php</b> on line <b>12</b><br />`); the html_errors form (bold label and bold path) is removed even inside `<li>`/`<td>` (a copied quote loses the `<b>` markup, real output keeps it).
  - Drupal paths `.module`, `.theme`, `.install`, `.engine` are recognised.
  - Xdebug: a `<table class="xdebug-error">` whose `<th>` holds `( ! ) Warning: Undefined variable $x in /var/www/a.php on line 3` is removed as a whole table and listed once (level `Warning`, file, line). If that proves too invasive, document Xdebug as a known limit instead and say so in the commit message.
  - An html_errors message that is not removed keeps a space between the bold path and "on line" in the Markdown (check `convert_html` output does not contain `**on line`). If this comes from the generic inline-run joining, fix only where `server_errors` is involved or leave it and note it.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Quoting contexts: `blockquote, q, cite, h1-h6, li, dd, dt, td, th, figcaption` (and anything inside them). In those, only the html_errors form counts. Track the html_errors form by the label's and the path's `<b>`/`<strong>` parents. Add the extensions. Keep the linear-time design (no nested quantifiers).
- [ ] **Step 4: Docs** (architecture, single-url en/de, CHANGELOG [Unreleased] Fixed).
- [ ] **Step 5: Run tests incl. a 5 MB timing check (existing perf test if any). Commit** `fix: keep quoted PHP messages in page text and recognise more PHP error forms`.

### Task 3: Page types (A2, A3)

**Files:**
- Modify: `src/text_extraction/page_types.py` (schema rule ~702-703, 796-801; link-text rule ~519-520; `_segment_types` ~297-310; docstring 10-12), `src/text_extraction/ui/site_tree.py:113-121, 210-216`
- Modify: `docs/architecture.md:2655-2690` (rulings), `docs/user-guide/en|de/whole-site-and-metadata.md` and `part-of-a-site.md` where the rules are described
- Test: `tests/test_page_types.py`, `tests/ui/test_site_tree.py`

- [ ] **Step 1: Failing tests** (all with `og:type=article`, about 600 words where an article is meant):
  - article + sidebar microdata `Event` → `content`; article + JSON-LD `[NewsArticle, Event]` → `content`; article + `@graph [WebPage, Article, Course]` → `content`; a page whose only typed top-level JSON-LD node is `Event` (or `WebPage.mainEntity` is an `Event`, or the microdata scope is on `body`/`main`/`article`) → `event`; same for `Course`/`Quiz`/`LearningResource` → `learning_material`; `/impressum` with a `Quiz` node → `imprint`.
  - Link text: a linked-only URL `/fortbildungen/mathe-workshop-2026` with text "Anmelden" → not `auth` (event keywords decide or content); `/themen/medienbildung-und-privatsphaere` with "Datenschutz" → not `privacy`; `/kurse/oer-einfuehrung` with "Kontakt" → not `contact`. A link text guess may still give a non-content type when the URL's last segment says nothing and the path is short (e.g. `/p/123` with text "Impressum" → `imprint`), so the rule is: a link-text type word counts only when the URL path has at most one segment or the last segment is not a multi-word slug (contains no `-`/`_` joined words of 3+ parts). Pick and document the exact rule as ruling AU3-R2.
  - Tree: the same URL in a plain sitemap (type_guess content/default) and linked as "Datenschutz" stays ticked by "Select content pages" (a `link_text` reason never overrides a sitemap content guess).
  - First segment: `/datenschutz/schuelerfotos-im-unterricht` fetched with og:type article + 600 words → `content`; `/kontakt/elterngespraech-vorbereiten` (article) → `content`; `/datenschutz/` and `/datenschutz` stay `privacy`; `/themen/datenschutz` stays `privacy` (last segment); `/barrierefreiheit/leichte-sprache-im-unterricht` (article) → `content`. Rule: the article override (A26-R5) also beats a first-segment `url` vote when a longer slug follows; record as ruling AU3-R3.
  - Existing tests from the 2026-11-05 audit (A26 rows) keep passing.
- [ ] **Step 2: Run, expect FAIL. Step 3: Implement. Step 4: Docs. Step 5: Run full tests. Commit** `fix: type pages by their main schema entity and stop link texts from unticking content`.

### Task 4: `sitemap_first` with a stale robots.txt listing (A4)

**Files:**
- Modify: `src/text_extraction/site/sitemap_items.py:586-602` (`_sitemap_exists`)
- Modify: docstring; `docs/user-guide/en|de/part-of-a-site.md` only if it states the probe rule
- Test: `tests/site/test_sitemap_items.py` (or where `_sitemap_exists` / the runner probe is tested; harness as in the existing `sitemap_first` runner tests)

- [ ] **Step 1: Failing test:** robots.txt lists `/old-sitemap.xml` which answers 404; the start page links to /a, /b, /c; with `discovery: sitemap_first` the links are followed (crawl `queued`/fetched), not marked `sitemap_first`. Second test: the listed sitemap answers 200 with a real urlset → links are `sitemap_first` as today. Third: listed sitemap answers 200 HTML → treated as absent.
- [ ] **Step 2-3:** Fetch the head (`SITEMAP_PROBE_MAX_BYTES`) of the first listed sitemap and check `looks_like_sitemap`, as the guesses do; the verdict is still kept once per job. **Step 4: Commit** `fix: sitemap_first checks a listed sitemap before trusting it`.

### Task 5: Site tree rule checks (S1, S2)

**Files:**
- Modify: `src/text_extraction/ui/job_views.py:700-707, 768-805` (`_tree_rules`, memo), `src/text_extraction/ui/site_tree.py` (`leaf_origins`, `mark_blocked`), `src/text_extraction/compliance.py:343-368` (tdmrep parse off the loop or capped), constants `TREE_JUDGE_SECONDS`
- Modify: `docs/architecture.md` (tree rules paragraph ~3118), `docs/configuration.md` UI section (with Task 8's D4 text)
- Test: `tests/ui/test_site_tree.py`, `tests/ui/test_tree_live.py`, `tests/test_compliance.py`

- [ ] **Step 1: Failing tests.**
  - Judgement memo: two tree views of the same job within the memo window call the per-URL judge only for leaves not judged before (count calls with a fake).
  - Origins: a sitemap listing URLs on 30 foreign hosts: the tree loads rule files (non-cached) only for origins the job contacted (the start origin and hosts of page/sitemap/robots items); other origins are read with `cached_only=True` and shown as unchecked when not cached.
  - Rule caps: an `OriginRules` built from a robots.txt with 25,000 rules or a tdmrep.json with 8,000 entries keeps at most 1,000 of each (first ones), judged per URL in well under 1 ms; document the cap in architecture.md. If the cap changes what `before_fetch` decides for real sites (a site with more than 1,000 robots rules), cap only the copy used by the tree, and say so.
  - `TREE_JUDGE_SECONDS` lowered to 1.0 and the tree judge runs on the conversion pool (or stays on the default executor if the conversion pool is not reachable from the UI; then say so in the commit).
- [ ] **Step 2-4:** implement, docs, full tests + `uv run pytest -m browser tests/ui -q -k tree`. **Commit** `fix: bound the site tree's rule checks and load rules only for contacted origins`.

### Task 6: Playground layout and texts (R3, U1-U6)

**Files:**
- Modify: `src/text_extraction/ui/static/ui.css` (~1243-1310, 1568-1575, server errors), `ui/templates/tree.html:20-21, 79-95`, `ui/templates/result.html:162-171`, `ui/templates/progress_live.html:32`, `ui/locales/en.json`, `de.json`
- Test: `tests/ui/test_tree_columns_browser.py` (or the existing tree browser test), `tests/ui/test_layout_browser.py`, `tests/ui/test_site_tree*.py`, `tests/ui/test_i18n.py`

- [ ] **Step 1: Failing tests.**
  - Browser at 390 px: a tree with a 12-level path and a 320-char title: no overlap of label, page count and checkbox (bounding boxes do not intersect), no horizontal scroll, axe `target-size` clean (axe-core is vendored in `tests/ui/vendor`).
  - Browser: an extract result with a server error whose file path is 300 chars without hyphens: `document.scrollingElement.scrollWidth <= innerWidth` at 1440 and 390 px.
  - Template: `.cell-status` carries a `title` with status and error code.
  - CSS: `ul.tree .label` and `ul.tree .title` have `unicode-bidi: isolate` (or the template wraps them in `<bdi>`); a title with U+202E renders inside its isolate (check computed style or the `<bdi>`).
  - Blocked note by role: with access modes `{enforce, report}` the note names only "Check and report only"; with `{enforce, report, ignore}` both; with the choice hidden, no remedy is named.
  - `progress.site_total` note shown only while the job runs (not in `completed`/`failed`/`cancelled`).
- [ ] **Step 3: Implement.** Narrow container: indent `min(var(--level) * 12px, 6rem)`; `.cell-name` `flex-wrap: wrap`; `label.inline` keeps at least checkbox + 4rem; `ul.server-errors { overflow-wrap: anywhere; }`. de.json `hint.access.ignore`: „…; die Grenzen für ausgehende Anfragen und eine Pause des Hosts gelten weiter.“ (match the English sibling's meaning; adjust en.json only if it says "per-host limits" too). New locale keys in both files.
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`. **Commit** `fix: playground tree on phones, long paths, status titles, bidi isolation, role-aware notes`.

### Task 7: ASR off, Helm PDB, uv pin, Dependabot, defaults test (O1-O5)

**Files:**
- Modify: `src/text_extraction/asr_worker.py:1280-1330` (`main`), `deploy/helm/text-extraction/templates/pdb.yaml` (new), `values.yaml`, `values-production.example.yaml`, `deploy/helm/test.sh` (checks), `pyproject.toml` (`[tool.uv] required-version`), `.github/dependabot.yml`, `tests/test_env_example.py` (or new `tests/test_defaults_parity.py`), `docs/configuration.md` (ASR off, PDB), `deploy/helm` README if it lists values
- Test: `tests/test_asr_worker.py`, the new parity test, `deploy/helm/test.sh` (build helm from source if no binary: see the 2026-10 note `go install helm.sh/helm/v3/cmd/helm@v3.19.0`, or skip with a note)

- [ ] **O1:** with `ASR_ENABLED=false`, `asr_worker --download-models` exits 0 without downloading (logs `asr_disabled`), and the worker serves `/health` and `/ready` (ready true) and takes no queue items, without loading a model. Tests first. Do not touch `docker-compose.yml` (PR #51); document in configuration.md that `ASR_ENABLED=false` now also makes the compose ASR services idle.
- [ ] **O2:** `podDisruptionBudget.enabled` (default false), `maxUnavailable: 1`, rendered for the API and each enabled worker kind only when its replicas or HPA minimum is above 1; enabled in the production example; helm test checks; configuration.md/Helm docs row.
- [ ] **O3:** `[tool.uv] required-version = ">=0.11,<0.12"` matching the Dockerfile's `uv:0.11` (check `uv --version` in the sandbox satisfies it; if the sandbox uv is newer, use the constraint that matches the Dockerfile and note how CI's setup-uv reads it). Base-image digests are out of scope (Dependabot's docker ecosystem already proposes tag bumps); note that in findings.
- [ ] **O4:** `.github/dependabot.yml` uv entry: `ignore: [{dependency-name: "uv-build"}]` with the comment "raised together with the Dockerfile's uv (see #29, #48)".
- [ ] **O5:** one parametrised test: every active `KEY=value` in `.env.example` and every Helm `config:` entry equals the `Settings` default, with an explicit allow-list: `ASR_ENABLED` (compose turns it on), `API_KEYS`, `ASR_EXTERNAL_BASE_URL`, `LLM_EMBEDDING_MODEL`, `LLM_VISION_MODEL`, and secret placeholders. Note: PR #51 changes `.env.example` defaults (Docling, NLP, LLM on); keep the allow-list in one constant so that PR can extend it.
- [ ] **Commit(s)** per item or one `chore: ...` commit; full tests.

### Task 8: Docs (D1-D11, S3 docs)

**Files:** as listed per finding in `findings.md`; exact wordings below.

- D1 single-url.md EN: `- "Site rules": respect the rules, check and report only, or checks off, by role; see [Site rules](site-rules.md#in-the-playground).` DE: `- „Website-Regeln“: Regeln einhalten, nur prüfen und melden oder Prüfung aus, je nach Rolle; siehe [Website-Regeln](site-rules.md#in-der-spielwiese).` (check the anchors exist).
- D2 mcp.md tables: add `, \`discovery\` (\`crawl\`)` after `scope` (`host`); part-of-a-site EN "takes `url`, `depth`, `max_pages`, `scope` and `discovery`", DE "nimmt `url`, `depth`, `max_pages`, `scope` und `discovery`".
- D3 part-of-a-site recipe, after "Most pages": EN `choose "Thorough: follow links" under "How to read the site" (the preset "Fast: sitemap first" reads only the start page when the site has a sitemap, and a sitemap lists the whole site; clearing "robots.txt and sitemaps" chooses it for you),` DE `wählen Sie bei „So wird die Website gelesen“ „Gründlich: Links folgen“ (die Voreinstellung „Schnell: Sitemap zuerst“ liest nur die Startseite, wenn die Website eine Sitemap hat, und eine Sitemap listet die ganze Website; ohne Häkchen bei „robots.txt und Sitemaps“ wird es von selbst gewählt),` (verify the quoted labels against the locales).
- D4 configuration.md:860-876 and :50: Site structure adds "and "How to read the site" (`discovery`; preset `sitemap_first`, which needs the sitemaps)"; linked-only "for depth, the page limit, the site's rules (`blocked`) or `sitemap_first`"; tree refresh and rule-file cost (use the numbers after Task 5); `COMPLIANCE_ALLOW_OVERRIDE` row: "The playground offers non-admin keys `enforce` and `report` only while it is on." Plus S3: a sentence in configuration.md and handover.md that with the default `true` any user key may choose `report` per request and per run (robots.txt and TDM opt-outs are then only reported), and that operators of a public service set it to `false`.
- D5 architecture.md:2754-2755: "…guesses the type with `guess_type` (above), always marked guessed with its reason; a URL that `guess_type` cannot type shows "Type open" with its reason and is counted as such."
- D6 extract_models.py:385 "(a guessed sitemap address, `/sitemap.xml`, `/sitemap_index.xml` or `/wp-sitemap.xml`, has no sitemap)"; en.json "No sitemap at a guessed address."; de.json „Unter einer erratenen Adresse liegt keine Sitemap.“ (OpenAPI snapshot tests may need an update).
- D7 CHANGELOG.md 0.1.6 "API changes: the site option `discovery`, the fields `SitemapEntry.type_guess` and `SitemapInfo.type_hint` and the page types `event` and `learning_material`."; packages.md:58 replace "the field `SiteNode.guess`" with "the sitemap fields `type_guess` and `type_hint`"; CHANGELOG 0.1.5 skipped tags "`pre`, `code`, `kbd`, `samp`, `tt`, `var`, `textarea` and `script`"; append compare links for every version (`[Unreleased]: https://github.com/janschachtschabel/text-extraction/compare/v0.1.6...HEAD`, … `[0.1.0]: https://github.com/janschachtschabel/text-extraction/releases/tag/v0.1.0`).
- D8 parameters.md cookies "at most 50" / „höchstens 50“; headers "at most 20 (each value at most 4096 characters)" / „höchstens 20 (jeder Wert höchstens 4096 Zeichen)“.
- D9 German terms: README.de.md:18-20 „neue Gestaltung der Spielwiese“, „robots.txt-Markierungen im Website-Baum“, „Korrekturen an der Spielwiese nach dem Test von v0.1.4“, „Website-Baum mit Typen, Spalten und schnellem Lesen der Sitemap“; whole-site-and-metadata en:65 "The playground offers this as …", de:67 „Die Spielwiese bietet das als …“; de/site-rules.md:12 „## Die Regeln der Website einhalten“ (check no link uses the old anchor); de/glossary.md:28 „Regeln einhalten, nur prüfen und melden oder Prüfung aus“; de/site-rules.md:132 „in `options` des Tools“; de/single-url.md:22 „im Tab **Screenshot**“; chips „Filter-Schaltflächen“ (de part-of-a-site 89-94, whole-site-and-metadata 209); „Browser-Modus“ everywhere in the DE guide plus a glossary row (EN partner row "browser mode" if the EN glossary lacks it).
- D10 de/installation.md:94-95 „… ist standardmäßig an: `docker compose up` startet den ASR-Worker, und sein erster Start lädt das Whisper-Modell (etwa 1,6 GB; `ASR_MODEL=small`, etwa 0,5 GB).“ (keep the edit to that sentence; PR #51 edits the same file).
- D11 `.github/workflows/live.yml:231` and architecture.md:2402 "six" WLO-type sites; tests/live/README.md option table: add "`--discovery` | `crawl` | The request's `discovery`: `crawl` follows links, `sitemap_first` reads only the start page when the site has a sitemap; sent only when not `crawl`."; "guessed at `/sitemap.xml`, `/sitemap_index.xml` or `/wp-sitemap.xml`" in tests/live/README.md:138-139 and run_site.py:91, 142 (comments/strings only).
- [ ] Run doc tests and `check_translation_pairs.py --base dfc3dc7`. **Commit** `docs: third audit fixes (en/de)`.

### Task 9: Release v0.1.7 (R4 follow-up)

- [ ] Version 0.1.7 in `pyproject.toml`, `uv.lock` (`uv lock`), `deploy/helm/text-extraction/Chart.yaml` (`version`, `appVersion`), `docker-compose.release.yml` default, `values-production.example.yaml` tags, READMEs and installation docs where they name the release (small edits; PR #51 touches installation.md), CHANGELOG `[0.1.7] - 2026-11-07` from [Unreleased] plus compare links, `docs/packages.md` note. Follow `docs/handover.md` "release" steps and the previous release commits (`git show 6f5c284 --stat`).
- [ ] Full suite, browser suite, ruff, mypy, helm lint.
- [ ] **Commit** `Release v0.1.7`.
