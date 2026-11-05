# Audit fixes 2026-11-05 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every finding of the second full audit (main f80b180) and prepare release v0.1.3.

**Architecture:** One branch, one PR. Each finding gets a failing test first where it is code. The page-type rules get stricter in `page_types.py` only (no new module); the screenshot encode limiter stays in `screenshot_image.py` but becomes pixel-weighted and cancellation-safe. Docs change in English and German together.

**Tech Stack:** Python 3.13, FastAPI, Pillow, lxml, Jinja2 templates, plain JS and CSS, pytest, Playwright (browser marker), GitHub Actions, uv.

**Spec:** `/mnt/project-files/audit-2026-11-05/findings.md` (finding ids A1-A9, U1-U9, D1-D10, P1-P3, with file:line and repro). Jan approved decisions E1-E6 = a on 2026-11-05 (`/mnt/project-files/audit-2026-11-05/plan-audit-de.md`): fix all findings (E1), stricter page-type rules (E2), cancellation-safe pixel-weighted encode slots with a time reserve (E3), lift the listed dependencies now (E4), pin Actions to SHAs and move the image push out of pull-request runs (E5), release v0.1.3 after the merge (E6).

## Global Constraints

- English in code, comments, developer docs, commit messages and API texts. A change to an English user-facing text (`README.md`, `docs/user-guide/en/`, `ui/locales/en.json`) changes its German partner (`README.de.md`, `docs/user-guide/de/`, `de.json`) in the same commit; `tests/test_docs_i18n.py`, `tests/ui/test_i18n.py`, `scripts/check_translation_pairs.py --base f80b180` must pass.
- German user docs use the formal "Sie".
- Result envelope rule: target faults answer 200 with `status: failed`; 5xx only for own faults.
- No GPL/AGPL dependencies (`scripts/check_licenses.py`). 14-day rule: a pinned release must be at least 14 days old (CONTRIBUTING.md).
- Stateless pods; every setting an env var documented in `docs/configuration.md` and `.env.example` (tests `test_env_example.py`, `test_configuration_doc.py`). This plan adds no setting.
- No team names in docs; the contact is Jan Schachtschabel.
- Quality gates per commit: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, focused pytest. Full `uv run pytest -q` before the task's last commit; browser-marker tests (`uv run pytest -m browser <files>`) where a task touches the browser path or the UI's JS/CSS.
- Known timing flakes (rerun before chasing): `test_handover_during_fast_phase_then_escalation_writes_into_p1_job`, `tests/structured/test_selector_pool.py`, `test_upload_over_rate_limit_is_429_before_the_body_is_read`.
- Commit messages end with the session's attribution lines (the controller gives them).
- New rulings go into `docs/architecture.md` next to the package 14/15 rulings, ids `A26-R<n>` ("audit 2026-11").

## Review Focus

1. A screenshot request whose render runs into the deadline while an encode is running: the encode slots must stay taken until the thread ends (Task 1 cancellation test).
2. A WordPress or TYPO3 site with plain URLs (`/?p=12`, `/index.php?id=42`): no `home`, and real home URLs with tracking parameters stay `home` (Task 2 table rows).
3. An imprint/privacy page on WordPress (Yoast sets `og:type=article` on every page) at `/impressum/` or `/datenschutz/`: still `imprint`/`privacy` after the article override (Task 2 test).
4. A German site with an IT section at `/it/` whose `<html lang="de">`: not `home` (Task 2 test); an Italian start page `/it/` with `lang="it"` stays `home`.
5. The Screenshot tab in its default state and a 422 from "Options (JSON)" at 390 px: no heavy dashed frame, the error tied to the Options field, the field visibly marked (Task 4 browser test).

---

### Task 1: Screenshot encode bounds (A1, A9)

**Files:**
- Modify: `src/text_extraction/screenshot_image.py` (limiter, `Image.open`, comments)
- Modify: `src/text_extraction/render.py` (`RenderOptions`), `src/text_extraction/browser_render.py:93, 420-430` (reserve), and wherever `RenderOptions` is built for a screenshot (find with `grep -rn "RenderOptions(" src`)
- Modify: `docs/configuration.md:1275-1277`, `docs/architecture.md:692` (figures), `deploy/helm/text-extraction/values.yaml` browser-worker comment if it states encode memory
- Test: `tests/test_screenshot_image.py`, the render reserve test next to the existing browser_render tests

**Interfaces:**
- Produces: `ENCODE_PIXEL_BUDGET: Final = 1920 * 16_384` (pixels of all encodes running at once in a process); `ENCODE_RESERVE_SECONDS: Final = 10.0`; `def encode_reserve_seconds(options: ScreenshotOptions | None) -> float` (0.0 when `options` is None or png without `width`, else `ENCODE_RESERVE_SECONDS`); `async def encode_screenshot_limited(png: bytes, options: ScreenshotOptions | None) -> EncodedScreenshot` keeps its signature. `RenderOptions.encode_reserve: float = 0.0`.

- [ ] **Step 1: Write the failing tests** in `tests/test_screenshot_image.py`:
  - `test_cancelled_encode_keeps_its_pixels_until_the_thread_ends`: monkeypatch `encode_screenshot` with a function that blocks on a `threading.Event` and counts concurrent entries; start an encode of a max-size image (a PNG header saying 1920 × 16,384 is enough if the weight is read with `png_size`), cancel its task, start a second max-size encode; assert the second has not entered while the first's event is unset; set the event; assert the second then runs. Peak concurrency must be 1.
  - `test_small_encodes_share_the_pixel_budget`: three 1920 × 5,000 encodes run at once (sum ≤ budget), a fourth waits.
  - `test_an_image_larger_than_the_budget_runs_alone` (weight capped at the budget, no deadlock).
  - `test_encode_reserve_seconds`: None → 0.0; png without width → 0.0; png with width, jpeg, webp → 10.0.
  - `test_encode_refuses_a_non_png_capture`: JPEG bytes passed as `png` with `format="webp"` raise `ScreenshotEncodeError` (A9: `Image.open(..., formats=("PNG",))`).
  - In the render tests: a render with `screenshot=True, encode_reserve=10.0` stops its page work 13 s (3 + 10) before the deadline (follow how the existing reserve test measures `_SCREENSHOT_RESERVE_SECONDS`).
- [ ] **Step 2: Run them, expect FAIL.** `uv run pytest tests/test_screenshot_image.py -q`
- [ ] **Step 3: Implement.** Replace the per-loop `asyncio.Semaphore(ENCODE_CONCURRENCY)` with a per-loop weighted limiter (an `asyncio.Condition` and a pixel counter); weight = `min(width * height, ENCODE_PIXEL_BUDGET)` from `png_size(png)` (a PNG without a readable header gets the full budget). Acquire, start the thread with `loop.run_in_executor(None, encode_screenshot, png, options)`, and release the pixels in the future's done callback, never in a `finally` of the awaiting coroutine; the awaiting coroutine awaits `asyncio.shield(future)` so a cancel ends the wait but not the slot. Remove `ENCODE_CONCURRENCY` (update its references). The PNG passthrough (png, no width) takes no slot. In `browser_render.py` the screenshot reserve becomes `_SCREENSHOT_RESERVE_SECONDS + self.options.encode_reserve`; the code that builds `RenderOptions` for a request sets `encode_reserve=encode_reserve_seconds(req.screenshot_options)` when `screenshot` is set.
- [ ] **Step 4: Docs.** Replace the "about 180 MB" figures with the measured ones (one 1920 × 16,384 encode needs about 330 MB as JPEG, about 470 MB as WebP and up to about 900 MB for a WebP of a noisy page; encodes share a budget of 1920 × 16,384 pixels per process, so this is also the most at once; an encode has 10 s of the render time set aside). Same facts in `configuration.md`, `architecture.md` and the Helm comment if one names a figure.
- [ ] **Step 5: Run** focused tests, then `uv run pytest -q` and `uv run pytest -m browser tests/browser -q -k screenshot`. Expect PASS.
- [ ] **Step 6: Commit** `fix: bound screenshot encodes by pixels and keep the slot until the thread ends`.

### Task 2: Page-type rules (A2, A3, A4, A5, D7)

**Files:**
- Modify: `src/text_extraction/page_types.py` (`url_type`, `_HEADING_START`, `page_type`, module docstring)
- Modify: `docs/user-guide/en/whole-site-and-metadata.md:90-110`, `docs/user-guide/de/whole-site-and-metadata.md:93-113` (the rules), `docs/architecture.md` (rulings A26-R1..R5 after the P14 page-type rulings)
- Test: `tests/test_page_types.py`

**Interfaces:**
- Consumes: nothing new. `url_type`, `link_category`, `page_type` keep their signatures; `ui/site_tree.py` `NON_CONTENT_TYPES` is unchanged.

Rules (these are the rulings to record as A26-R1..R5):
- **R1 Home needs an empty query.** `_HOME_PATH` matches and every query parameter is a tracking parameter or has an empty value. Tracking parameters: names starting `utm_`, and `fbclid`, `gclid`, `dclid`, `gbraid`, `wbraid`, `msclkid`, `yclid`, `igshid`, `mc_cid`, `mc_eid`, `_ga`, `_gl`, `ref`. Search and page parameters keep their meaning before home is checked (as now).
- **R2 Segment position.** A trailing `index.html|htm|php` segment is dropped before reading segments. A listing name (`kategorie`, `category`, `categories`, `tag`, `tags`, `schlagwort`, `archiv`, `archive`) counts only when at most one segment follows it. Any other type name counts in the first segment (after an optional language code from `_SITE_LANGUAGES` with optional region) or the last segment. These ambiguous names count only when they are the only segment (after an optional language code): `cookies`, `terms`, `legal`, `konto`, `account`, `profil`, `profile`, `recherche`, `team`, `password`, `passwort`, `disclaimer`. The `page/<n>` rule and the query rules are unchanged.
- **R3 Language-only path.** `url_type` still answers `home` for `/de/`, `/hr` (links have no page to look at). In `page_type`, a path that is only a language code (no empty path, not `/`, not a bare index file) is `home` only when the page's `<html lang>` is missing or its primary subtag equals that code (case-insensitive); otherwise the URL gives no vote and the other signals decide.
- **R4 Headings.** In the start form of a title, `:` is no longer a separator (a colon after a type word starts a subtitle: "Datenschutz: Was Schulen wissen müssen"); `|`, en dash, em dash, middle dot and a spaced hyphen stay. The end form is unchanged. An `<h1>` votes only when the whole h1 (folded, trimmed, trailing `:` removed) is a type word.
- **R5 Article override.** When `og:type` is `article` and the word count is at least `min_words`, the votes `title`, `h1`, `pagination`, `link_density` and a `url` vote for `listing` are dropped before the winner is chosen. A `url` vote for any other type and `schema_org` stay (a WordPress imprint at `/impressum/` stays an imprint).

- [ ] **Step 1: Write the failing tests.** Add rows to the `url_type` table: `/?p=12` → None, `/?page_id=42` → None, `/index.php?id=42` → None, `/?utm_source=x&fbclid=y` → home, `/?ref=nav` → home, `/rezepte/cookies` → None, `/recipes/cookies` → None, `/glossary/terms` → None, `/services/legal` → None, `/girokonto/konto` → None, `/profile/jan` → None, `/themen/recherche` → None, `/cookies` → legal, `/de/terms` → legal, `/service/impressum/index.html` → imprint, `/ueber-uns/kontakt` → contact, `/kategorie/mathe/bruchrechnung-einfach-erklaert` → None, `/archiv/2019/artikel-titel` → None, `/category/news/2024/05/headline` → None, `/magazine/archive/2019/05/slug/588011/` → None, `/blog/category/news/page/2` → listing. Change the existing rows `/team/archiv` → listing and `/terms/login` → auth (R2). `page_type` tests: `test_language_path_with_another_html_lang_is_no_home` (`/it/`, `<html lang="de">`, 900-word article → content), `test_language_path_with_its_own_html_lang_is_home` (`/it/`, `lang="it-IT"` → home), `test_language_path_without_html_lang_is_home`; `test_title_with_a_colon_subtitle_is_no_type` ("Datenschutz: Was Schulen wissen müssen" at `/blog/x`, 900 words → content); `test_h1_votes_only_as_the_whole_type_word` (h1 "Kontakt" → contact; h1 "Kontakt zur Schule aufnehmen" → no h1 vote); `test_article_overrides_title_listing_and_link_density` ("Kontakt – So gelingt das Elterngespräch", og:type article, 900 words → content; `/blog/article?page=2` with og:type article and enough words → content; a 220-word og:type article page with 60 menu links and `min_words=50` → content); `test_article_override_keeps_a_url_type` (`/impressum/`, og:type article, 900 words → imprint); `test_article_override_needs_enough_words` (og:type article, 10 words, title "Kontakt" → contact).
- [ ] **Step 2: Run, expect FAIL.** `uv run pytest tests/test_page_types.py -q`
- [ ] **Step 3: Implement R1-R5** in `page_types.py`; update the module docstring's precedence list and the `url_type` docstring.
- [ ] **Step 4: Run** `uv run pytest tests/test_page_types.py tests/site tests/ui -q`, then `uv run pytest -q`. Fix callers' tests that pinned the old classification only where the new rules make the old expectation wrong; list each such change in the report.
- [ ] **Step 5: Docs.** The page-type rules section in both user guides states R1-R4 in plain words (home: empty path, `/`, an index file, or only a language code, optionally with an index file, and no query besides tracking parameters; a type name in the path counts as the first or last part, some ambiguous words only alone; a title colon starts a subtitle; an article with enough words stays content unless its address or schema.org says otherwise). Architecture rulings A26-R1..R5.
- [ ] **Step 6: Commit** `fix: stricter page-type rules for queries, path positions, headings and articles`.

### Task 3: API texts, mode_used and the upgrade note (A6, A7, A8)

**Files:**
- Modify: `src/text_extraction/extract_api.py:411-421`, `src/text_extraction/screenshot_api.py:51`, `src/text_extraction/extract_models.py:103`, `src/text_extraction/mcp_server/tools_extract.py:392-394`
- Modify: `docs/configuration.md:630-646` (upgrade-order note), `CHANGELOG.md` [Unreleased]
- Test: `tests/test_extract_api.py`, `tests/mcp_server/` description test if one pins the text, an OpenAPI test if one pins the description

- [ ] **Step 1: Failing test** `test_invalid_url_has_no_mode_used_in_any_mode`: `POST /v1/extract {"url": "ftp://x/"}` with `mode` auto, fast and browser all answer 200, `status: failed`, `error.code: invalid_url`, `mode_used: null`.
- [ ] **Step 2: Run, expect FAIL** for auto and fast.
- [ ] **Step 3: Implement** `mode_used=None` for `invalid_url` in every mode.
- [ ] **Step 4: Texts.** The default screenshot is "the whole page as PNG, 1920 px wide (window 1920 x 1080)". MCP `screenshot_url`: the default as above; `options` can set `viewport`, `format` (png, jpeg, webp), `quality` and `width`; `full_page: false` (the tool's own argument, not inside `options`) takes only the first screen.
- [ ] **Step 5: Upgrade note** in `configuration.md` next to the package 9 note and in CHANGELOG [Unreleased] "Upgrade": update browser workers (and the other workers) before API pods, because jobs with `screenshot_options` are refused by 0.1.2 workers (`extra="forbid"`) and 0.1.2 API pods cannot read such jobs; results with `page_type` and link `category` are read by old pods.
- [ ] **Step 6: Run** focused tests and `uv run pytest -q`; **commit** `fix: null mode_used for invalid URLs, accurate screenshot texts, upgrade note`.

### Task 4: Playground fixes (U1-U9)

**Files:**
- Modify: `src/text_extraction/ui/static/ui.css` (U1 549-551, U3 146-160 and 1156, U5 183, U6), `src/text_extraction/ui/forms.py:147-178` and the caller that builds the result context (U2), `src/text_extraction/ui/static/ui.js:465-481` (U2), `src/text_extraction/ui/templates/result.html:90,100,110,150,156` and `timeline.html` (U4, U7), `src/text_extraction/ui/templates/progress_live.html:36` and the job/result templates with `h4` (U8), `src/text_extraction/ui/templates/base.html` (U9 favicon), `src/text_extraction/ui/i18n.py` and `templating.py` (U9 plural), `src/text_extraction/ui/locales/en.json`, `de.json`
- Test: `tests/ui/` (unit and `-m browser` tests next to the existing ones, e.g. `tests/ui/test_toolbar_browser.py` for the shape)

**Interfaces:**
- Produces: `def translate_count(language: Language, key: str, count: int, /, **params: object) -> str` in `i18n.py`: looks up `f"{key}.one"` when `count == 1`, else `f"{key}.other"`, fills `{count}` and the params; exposed to templates as `tn(key, count, **params)` next to `t`.

Decisions:
- U1: the disabled custom fieldset gets `border: 1px dashed var(--line)`, padding and `border-radius: var(--radius-card)`, and its controls are visibly dimmed (`color: var(--muted)`, `opacity` on inputs); the enabled state has the same box with a solid border, so the layout does not jump.
- U2: a 422 location is mapped to a custom-capture control only when the form's preset is `custom`; otherwise to `screenshot-options` when the field name came from "Options (JSON)", else to no control (the status line still shows the error).
- U3: `.page-type` gets `gap: 0.3em`; `.page-type-guessed` may wrap (`white-space: normal`).
- U4: each card's `<summary>` holds an `<h4 class="fold-title">`, styled to look as now.
- U6: `[aria-invalid="true"]` gets a 2px `var(--error)` border; after a server-side error the first invalid control is scrolled into view (`scrollIntoView({block: "center"})`) before focus.
- U7: the reading card and the source `pre` get `lang` from `data.language` when the API gave one (a valid BCP 47 primary tag; otherwise no attribute).
- U8: the empty `<th>` gets visually hidden text (key `progress.actions`); headings follow h2 → h3 → h4 (use `h3` where an `h4` follows an `h2` directly).
- U9: `tree.urls` and `tree.hidden` and every other count text in the catalogues that is shown with a number that can be 1 become `.one`/`.other` pairs used through `tn`; `<link rel="icon" href="data:,">` in `base.html`.
- U5: drop the `.section-label` selector.

- [ ] **Step 1: Failing tests:** unit tests for `translate_count` (1 → one, 0 and 2 → other, both languages); a form test for U2's mapping with preset `whole` and Options `{"width": 3000}` (→ `screenshot-options`) and preset `custom` with width field 3000 (→ `screenshot-width`); a template test for U7 (`lang="de"` on the reading card when `data.language` is `de`, none when absent); a template test for U8 (no empty `th`); browser test at 390 px: Screenshot tab default state — the custom fieldset's computed `border-top-width` is `1px`; submit preset "The whole page" with Options `{"width": 3000}` — `#screenshot-options` has `aria-invalid="true"` and focus, its computed border colour is the error colour; tree with a guessed badge — the badge text has a space before the bracket and its right edge is inside the tree box.
- [ ] **Step 2: Run, expect FAIL.** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q -k "<new names>"`
- [ ] **Step 3: Implement** U1-U9 as decided.
- [ ] **Step 4: Run** `uv run pytest tests/ui -q`, `uv run pytest -m browser tests/ui -q`, `uv run pytest -q`; check the i18n tests.
- [ ] **Step 5: Screenshots** (for the PR, not committed): Screenshot tab default and the U2 error at 390 and 1440 px in de and en, the tree with a guessed badge at 390 px; save under `/mnt/project-files/audit-2026-11-05/screenshots/` (network mount: write each file once, no directory search).
- [ ] **Step 6: Commit** `fix: playground details from the audit (fieldset, errors, headings, lang, plurals)`.

### Task 5: Documentation (D1-D6, D8-D10)

**Files:**
- Modify: `docs/configuration.md:780-784, 797-798, 846-856`; `docs/architecture.md:743, 1517, 2782-2783, 2846`; `CHANGELOG.md`; `README.md:13`, `README.de.md:14`; `docs/user-guide/en/parameters.md` and `de/parameters.md`; `docs/user-guide/de/single-url.md:30-31`, `de/usage.md:148, 204, 376`, `de/whole-site-and-metadata.md:95`; `docs/user-guide/en/glossary.md`, `de/glossary.md`
- Test: the docs tests (`tests/test_docs_i18n.py`, `tests/test_check_translation_pairs.py`, `tests/test_configuration_doc.py`, `tests/test_handover_doc.py`)

- [ ] **Step 1: Edit** per the findings' "should say":
  - D1: previews accept PNG, JPEG, GIF, WebP or SVG by their bytes; an SVG only as a `data:` image inside `<img>` (no script runs); image descriptions skip SVG.
  - D2: rewrite the configuration.md UI paragraph and the site-tree bullet to the package 15 view (reading card, "Show source", toolbar with copy and "Save response (JSON)", closed "Advanced", card of figures with page type, closed metadata/links/images/"What happened" cards, screenshot "Capture" presets, tree type badges, "Types:" and "Select content pages"), or point to architecture.md "Look (package 15)" with a two-sentence summary; mark the old architecture.md 2782-2783 text as superseded by P15-S5.
  - D3: `connect-src 'self'` does not allow fetching a `data:` URL.
  - D4: Patchright 1.64.1, CTranslate2 4.8.3 (and any version Task 7 changes, if it ran first: check `pyproject.toml`).
  - D5: CHANGELOG [Unreleased] gets one heading per type (Added, Changed, Fixed, Upgrade), PR #36's bumps under Changed (openai 3.29.0 → 3.35.1, patchright 1.63.0 → 1.64.1, ctranslate2 4.8.2 → 4.8.3), "Dependency rule:" folded into Changed, `DOWNLOAD_MAX_CHARS` reworded as "10 million characters (fixed in code)"; a Fixed entry per audit finding group (screenshot encode bounds, page-type rules, invalid_url mode_used, screenshot texts, playground details, docs).
  - D6: "packages 0-15 are merged (…, screenshot formats and page types, playground redesign)" in both READMEs (version line handled by Task 8).
  - D8: parameters.md rows in both languages: `chunking.size` 200 to 20000; `chunking.overlap` at most half of `size`; `chunking.breakpoint_percentile` 50 to 99; `pii.score_threshold` 0 to 1; `structured.instructions` at most 2000 characters; `actions` notes that `evaluate` is admin only (403). Verify each bound in the models before writing it.
  - D9: "stehen"; "(dem Status, …, und dem HTTP-Status)"; "je Kante".
  - D10: "Soft-404" everywhere in the German guide; a glossary entry "Soft-404" / "soft 404" in both glossaries.
- [ ] **Step 2: Run** the docs tests and `uv run python scripts/check_translation_pairs.py --base f80b180`. Expect PASS.
- [ ] **Step 3: Commit** `docs: audit fixes (SVG previews, playground description, bounds, CHANGELOG, German wording)`.

### Task 6: GitHub Actions pins and the image push (P3)

**Files:**
- Modify: `.github/workflows/*.yml`, `.github/dependabot.yml` only if it needs the actions ecosystem settings changed (it already covers actions)
- Test: `tests/` has workflow tests (`grep -rln "workflows" tests`); add `test_actions_are_pinned_to_commits` there: every `uses:` of a third-party or `actions/*` action has a 40-hex SHA followed by a `# vX.Y.Z` comment (local `./` actions excepted)

- [ ] **Step 1: Failing test** as above. Run, expect FAIL.
- [ ] **Step 2: Pin.** Resolve each `owner/repo@tag` to its commit with `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag> refs/tags/<tag>^{}` (take the peeled `^{}` commit for annotated tags); write `uses: owner/repo@<sha> # <full version tag>` (resolve a major tag like `v5` to the exact release it points at and name that release in the comment).
- [ ] **Step 3: Split the image workflow** so the job that runs on `pull_request` has `packages: read` (build only), and login plus push run in a job (or the same matrix with a job-level `if`) that never runs for `pull_request` and alone holds `packages: write`. Keep the existing matrix, caching and tags; `release.yml` keeps working (check how it triggers image builds).
- [ ] **Step 4: Run** the workflow tests and `uv run pytest -q tests/test_release*.py`; lint the YAML with `uv run python -c "import yaml,glob;[yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')]"`. Expect PASS.
- [ ] **Step 5: Commit** `ci: pin actions to commits, push images only outside pull requests`.

### Task 7: Dependencies (P1, P2)

**Files:**
- Modify: `pyproject.toml` (pins), `uv.lock`, `CHANGELOG.md` [Unreleased] Changed, `docs/architecture.md` if it names a changed version

- [ ] **Step 1:** Raise the pins: `yt-dlp[deno]==2026.10.21`, `openai==3.36.0`, `markitdown[...]==0.1.9`, `av==19.0.2`, `deno==2.9.8` (keep extras and licence comments; check each release date is at least 14 days before the commit date and the licence is unchanged).
- [ ] **Step 2: Lock.** The cloud session cannot reach download.pytorch.org, so the controller runs the lock on Jan's PC (Remote Control, clone `C:\Users\jan\te-p13-check`, `uvx --from uv==0.11.32 uv lock --upgrade-package joserfc --upgrade-package setuptools --upgrade-package yt-dlp --upgrade-package openai --upgrade-package markitdown --upgrade-package av --upgrade-package deno`) and brings the `uv.lock` back to the branch. Expected in the lock: joserfc 1.7.6, setuptools 84.1.0, and the five pins; no other package moves beyond what these require (list any that do).
- [ ] **Step 3: Verify** `uv sync --frozen`, `uv run python scripts/check_licenses.py`, `uvx pip-audit` on the export (no known vulnerabilities besides the un-auditable torch builds), `uv run pytest -q`, `uv run pytest -m browser -q tests/media tests/browser -k "youtube or transcript"` where they exist.
- [ ] **Step 4: CHANGELOG** Changed entry with old → new versions. **Commit** `build(deps): joserfc 1.7.6, setuptools 84.1.0, yt-dlp, openai, markitdown, av, deno`.

### Task 8: Release preparation 0.1.3 (E6)

**Files:**
- Modify (as in release commit baf12e8): `pyproject.toml` version, `uv.lock` (the project's own version line), `deploy/helm/text-extraction/Chart.yaml` (`version`, `appVersion`), `deploy/helm/text-extraction/values-production.example.yaml` (tags and the comment), `docker-compose.release.yml` (default), `docs/user-guide/en|de/installation.md` (default version), `README.md`, `README.de.md` (version line), `CHANGELOG.md` ([Unreleased] → `[0.1.3] - <merge date>` with the packages 14 and 15 entries, the dependency bumps and the audit fixes; a new empty [Unreleased])
- Test: `tests/test_release*.py`, `tests/test_handover_doc.py`, helm render tests that pin the version

- [ ] **Step 1:** Change every `0.1.2` that names the current release to `0.1.3` (leave historical mentions such as "Jan waived … (release v0.1.2)" and the CHANGELOG's older sections). `uv lock` cannot run here for a version-only change if it needs the network: edit the project's own `version` line in `uv.lock` by hand (as baf12e8 did) and check with `uv lock --check`.
- [ ] **Step 2: Run** the release tests and `uv run pytest -q`. **Commit** `Release v0.1.3`.

---

## After the tasks (controller)

Whole-branch review (most capable model), one fix wave, `uv run pytest -q` and `uv run pytest -m browser -q`, CI green, live runs (label `live`: wlo_200 auto against the baseline ~83-85 %, and the 5-site run, whose page-type table is compared with the last run on main), PR, squash merge. Then Jan pushes tag `v0.1.3` himself (two git commands given in the thread); `release.yml` makes the pre-release page.
