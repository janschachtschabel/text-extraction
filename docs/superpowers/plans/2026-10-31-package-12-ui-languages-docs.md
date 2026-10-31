# Package 12: UI Languages, Maximum Extraction, Bilingual Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the prototype for the `v0.1.0` tag. That means:

- two clear ways to extract: one that **respects the site's rules** (today's default) and one for sites that allowed extraction by contract, **maximum extraction** (unblock mode), which also falls back to the browser on a plain 403, 429 or 503;
- the browser fallback as a visible, optional choice;
- an accessible playground (WCAG 2.2 AA basics) with help texts and status messages, in German and English with a language switch;
- the README and a user guide in German and English: installation, usage, parameters, the use cases (MCP, one URL, part of a site, a whole site with metadata), the 11 MCP tools and structured data.

**Architecture:** One service change and three additions.

- **Escalation.** `escalation.needs_browser` gets the unblock-only rules from P12-S1. The pipeline then assesses every 403/429/503 page answer in unblock `auto` mode. Unblock mode itself does not change: admin, `compliance: ignore`, `BROWSER_UNBLOCK_ENABLED` (B14). Nothing changes outside unblock mode.
- **UI i18n.** Two JSON catalogues (`ui/locales/en.json`, `de.json`). A small `ui/i18n.py` reads them and gives the templates a `t()` function. The UI's Python messages become catalogue keys. API texts (envelope messages, OpenAPI, MCP) stay English.
- **UI forms and accessibility.** A "Site rules" choice and a relabelled browser-fallback choice in the forms. ARIA tabs, focus, live regions and help texts in the templates and `ui.js`. axe-core in the browser test job.
- **Docs.** `README.md` and `README.de.md` as landing pages, plus `docs/user-guide/{en,de}/` with the same file names. Tests and a CI check keep both languages in sync.

**Tech Stack:** Python 3.13, FastAPI, Jinja2 (already a dependency), htmx 2.0.11, Playwright (Chromium) for the browser tests, axe-core 4.14.0 (MPL-2.0, test-only, vendored), GitHub Actions.

**Spec:**

- Jan's request of 2026-10-31 (German, translated):
  - "Falling back to the browser should be optional. There should be one mode that keeps the target sites' rules and one that can bypass them and offers maximum extraction. The second is for the use case where we have contracts with the site."
  - "Check that the UI is accessible and offers help and status messages, and supports German and English with a selectable language switch."
  - "Explain everything in the README and docs in German and English: from installation through usage and the parameters to specific use cases": MCP use; extracting a single URL; extracting part of a website; extracting a whole website and extracting metadata.
  - "Also explain which 11 tools the MCP server has and how to extract structured data."
- `docs/packages.md` row 12 and the package 10 note: on Jan's PC, leifiphysik answered 403 and `auto` stayed fast; "plain 403, 429 and 503 escalation in unblock mode is in the scope of package 12". The package 12 note: `v0.1.0` (a prototype) is tagged after package 12 is merged.
- `docs/architecture.md`: pipeline step 4, B5, B9, B14, B15, P10-S12 to P10-S20 (the UI), and "Known limits" of "MCP and UI".
- `CLAUDE.md`, rules 1, 5, 6 and 7.

## Global Constraints

- **Languages.** Code, comments, developer docs (`docs/architecture.md`, `configuration.md`, `handover.md`, `packages.md`, the plans) and commit messages stay English, and so do API texts (envelope `error.message`, OpenAPI, MCP tool descriptions and hints). German exists only in `ui/locales/de.json`, `README.de.md` and `docs/user-guide/de/`. English is the source language.
- **CLAUDE.md rule 6.** Maximum extraction is unblock mode and nothing more: admin, `compliance: ignore` and `BROWSER_UNBLOCK_ENABLED`. These stay in place:
  - the SSRF guard on every fetch and sub-request;
  - the byte and time caps;
  - the per-host outbound gate, including a 429/503 pause.

  It adds no IP or proxy rotation, no captcha solving and no paid solver (B15).
- **No new runtime dependency.** axe-core is vendored under `tests/ui/vendor/`, which is excluded from every image (`.dockerignore` has `tests/`). Release 4.14.0 is from 2026-10-05, so it meets the 14-day rule (R16). Its licence is MPL-2.0, which is no GPL (rule 7). Keep its licence file next to it.
- **UI security must not regress (P10-S12, P10-S13).**
  - `CONTENT_SECURITY_POLICY` stays byte-identical.
  - No inline script or style, no `hx-on`, and the htmx config is unchanged.
  - No cookie under `/ui`.
  - Keys stay only in `sessionStorage` and are sent only as headers to `/ui/run/`.
  - The language header is never forwarded to `/v1`.
  - Catalogue values are plain text and are always autoescaped.
- **Result envelope.** The envelope only gets additions.
- **Tests and live checks.**
  - No test sends traffic to a third-party site.
  - Run maximum mode live only against a site whose owner allowed it (Jan names it), or against a local test site.
  - Never skip, disable or quarantine a test to get green.
- **Accessibility target.** WCAG 2.2 level A and AA, checked by axe-core with the tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa` and `wcag22aa`, plus keyboard tests.

## Rulings (P12-S*)

- **P12-S1, maximum extraction (API).** Under unblock (`unblock_for` unchanged), `needs_browser` also returns true in two cases:
  - **(a)** a fast page answer `failed`/`target_http_error` whose `upstream_status` is in `REFUSAL_STATUSES = {403, 429, 503}`, whatever its body or content type;
  - **(b)** (D2 = a) a 2xx HTML fast result whose status is not `success`, for example `content_too_short` or `empty_content`.

  Today's exclusions stay: a document, a platform video or media file, a cache `hit`/`revalidated`, a non-fast result, and `mode` other than `auto`.

  For case (a), the pipeline sets `quality` on every refusal answer in unblock `auto` mode with `BROWSER_ENABLED`. It assesses an HTML body up to `CONVERT_MAX_BYTES`; for any other body it uses `assess("", "")`. `challenge_status` is true only for a challenge body.

  `better()` keeps the better result, and the result carries `unblocked: true`. Outside unblock nothing changes: the leifiphysik case stays `target_http_error` in respect mode, as documented.
- **P12-S2, the host's pause (D3 = a).** A fast 429/503 still records its pause (`Retry-After`, else `OUTBOUND_BACKOFF_SECONDS`, at most `OUTBOUND_MAX_BACKOFF_SECONDS`). The render takes its turn after the pause: the browser worker gives the item back on `CircuitOpen`, as today. A sync call answers within its B13 bound, often with `browser_unavailable`; a call with `wait_seconds` becomes a job that finishes after the pause. A render that gets past the refusal keeps it out of the backoff (`answers_for_gate`, B14 unchanged).
- **P12-S3, "Site rules" in the UI (D1 = a).**
  - The radio group `access` has two values. `respect` is the default and sends no `compliance` (Options may still set `report`). `maximum` sends `compliance: "ignore"` as set by the user, so an Options `compliance` gets "compliance is given twice.".
  - Admins see the group. It is disabled with a hint when `BROWSER_ENABLED` or `BROWSER_UNBLOCK_ENABLED` is off. Users never see it, and if a user forges `access=maximum`, REST answers 403 `forbidden`.
  - The group is on extract, structured, screenshot, chunk (URL), batch, batch file, the tree's "Extract selected" and site structure. A site crawl stays in fast mode, so for site structure "maximum" only means `ignore`, and its hint says so. File upload has no group.
  - The result view shows the compliance mode and a "maximum extraction (unblocked)" badge when `unblocked` is true.
- **P12-S4, browser fallback.** The `mode` select becomes "Browser fallback". `auto` reads "when the page needs it" (the default), `fast` reads "never" and `browser` reads "always". While `BROWSER_ENABLED` is off, `browser` is disabled and a hint says that this deployment has no browser. With `maximum` chosen, a hint says that `fast` never reaches the browser.
- **P12-S5, the i18n mechanism.**
  - **Catalogues.** `ui/locales/{en,de}.json` hold flat dotted keys (`tab.extract`, `warning.<name>`, `error.<code>`, `timeline.<step>`) with plain-text values and `{name}` placeholders. They have no markup: templates wrap `<code>` around a placeholder's output.
  - **Lookup.** `translate()` falls back to English, then to the key. Tests make sure neither happens.
  - **Python messages.** Messages from Python are `Message(key, params)` and are rendered at the template boundary.
  - **API errors.** The UI shows the translated `error.<code>` text and, below it, "Details from the API (English)" with `error.message`.
  - **German style.** German texts address the user as "Sie" where an address is needed, and are neutral otherwise. Fixed terms are in `glossary.md`: "Job" stays "Job", and a field name stays the field name in backticks.
- **P12-S6, choosing the language (D4 = a).**
  - **The page.** `GET /ui` takes the `lang` query (`en` or `de`), then `Accept-Language` (q-values, primary subtag), then the new setting `UI_DEFAULT_LANGUAGE` (`en` or `de`, default `en`).
  - **Fragments.** `/ui/run/*` takes the `X-UI-Language` header, which `ui.js` sends from `<html lang>`, then the same fallbacks.
  - **Answers.** Every UI answer sets `Content-Language`. An unknown value is ignored and never reflected.
  - **Storage.** The choice is kept in `localStorage` as `te.lang`. On load, `ui.js` moves a page without a `lang` query to the stored language with `location.replace`.
- **P12-S7, the switch.** A `<nav>` with two links, "Deutsch" and "English". Each has its own `lang` and `hreflang`, and the active one has `aria-current="true"`. The switch is on the login page and in the app. Switching reloads the page. The keys survive (`sessionStorage`) and log in again. An open job view closes, but its jobs keep running; the hint says so.
- **P12-S8, the accessibility baseline.**
  - **Page structure.** `<html lang>`, a skip link to `<main>`, one `h1`, and an `h2` per panel.
  - **Labels and hints.** Every control has a `<label>`, and every hint is tied to its control with `aria-describedby`.
  - **Tabs.** WAI-ARIA tabs: arrow keys, Home and End, roving `tabindex`, and panels with `tabindex="0"`.
  - **Focus and contrast.** `:focus-visible` gets a 3 px outline with at least 3:1 contrast, and text has at least 4.5:1. `<progress>` elements have an accessible name, and `prefers-reduced-motion` is respected.
  - **Errors.** A form error sets `aria-invalid` and `aria-describedby` on its field (`data-error-for`) and moves focus there. A whole-form error is `role="alert"`.
  - **Refresh.** Buttons in the 3 s refresh have stable ids, so htmx keeps focus on them.
- **P12-S9, status messages.**
  - **Request status.** `#ui-status` (`role="status"`) reads "Sending request …" while a request runs (`htmx:beforeRequest`). Afterwards it reads a short summary ("Result: success, 0.15 s", or the translated error).
  - **Job progress.** The progress fragment carries `data-announce` with a job's state. `ui.js` writes it to `#ui-status` only when it changes, so a refresh is never read out.
  - **Failures.** `.request-status` (`role="alert"`) stays for answers that are not swapped (R-P10-F3), now translated.
- **P12-S10, help.** Each tab starts with an intro (`help.<tab>.intro`) and a "Help" link. The link goes to the user guide page in the page's language (`DOCS_BASE_URL` on GitHub; links leave the CSP-protected page in the normal way). Each field has a hint (`hint.<field>`). The README and the guide describe the same tabs with the catalogue's labels.
- **P12-S11, doc layout (D5 = a).**
  - **README.** `README.md` (English) and `README.de.md` are short landing pages: what the service is, the two modes, quick start with Docker, the use cases with links, and the docs index. Each links to the other at the top.
  - **User guide.** `docs/user-guide/{en,de}/` holds the same file names: `index`, `installation`, `usage`, `parameters`, `site-rules`, `single-url`, `part-of-a-site`, `whole-site-and-metadata`, `structured-data`, `mcp`, `glossary`.
  - **Moved content.** The long examples from today's README move into the guide.
  - **Developer docs** stay where they are, in English.
- **P12-S12, keeping the two languages in sync.**
  1. **Structure.** For each pair (`README.md`/`README.de.md`, each guide page, `en.json`/`de.json`), `tests/test_docs_i18n.py` and `tests/ui/test_i18n.py` check the same keys and placeholders, the same heading levels in order, byte-identical fenced code blocks, the same relative link targets and the same table row counts.
  2. **Pull requests.** `scripts/check_translation_pairs.py` runs in CI on pull requests. It fails when an English file of a pair changed and its German partner did not. A German-only change passes. The trailer `I18n-Unchanged: <path>` in a commit of the range exempts an English typo fix.
  3. **Rule amendment.** CLAUDE.md rule 1 is amended as in Task 2.
- **P12-S13, doc correctness.** Tests in the default suite:
  - Every ```json block that follows `<!-- request: <METHOD> <route> -->` validates against that route's model.
  - Every `/v1/…` path named exists in the app.
  - Every backticked `UPPER_SNAKE` name is a setting or is on a short allowlist (shell variables such as `API_KEY`).
  - Every MCP tool of `TOOL_ROUTES` and every structured strategy is named in both languages.
  - Every bold tab name in a guide is a tab label of that language's catalogue.
- **P12-S14, configuration reference (D6 = a).** `docs/configuration.md` stays English only. Both guides get a "Key settings" section in `installation.md`, listing about 25 settings a user or operator touches, with links into the English reference. P12-S13 checks that each one exists.
- **P12-S15, release.** The CHANGELOG `[0.1.0]` section gets package 12. Tag `v0.1.0` on the squash-merge commit.

## Decisions for Jan (recommended = a)

- **D1. How the two modes are offered.**
  - a) In the UI as "Site rules: respect (default) / maximum extraction (contract partners)". The choice maps to the existing `compliance: ignore`, so the API gets no new field; the guide shows the REST and MCP way (`options: {"compliance": "ignore"}` with an admin key). Admins see the choice; it is disabled with a reason while unblock mode is off.
  - b) Like a), plus a new request field `access: respect|maximum` on every route and MCP tool.
  - c) Like a), but users see the choice too and get a 403.
- **D2. What falls back to the browser in maximum mode.**
  - a) Today's triggers (the quality gate, a bot wall, a challenge body), plus every 403/429/503 page answer, plus any 2xx HTML fast result that is not `success`. This gives the most text; `better()` keeps the better result.
  - b) Only what Jan named: today's triggers plus a plain 403/429/503.
  - c) Maximum mode always renders (`mode: browser`, no fast phase).
- **D3. A 429 or 503 versus the host's pause.**
  - a) Fall back after the pause the host asked for (`Retry-After`, else 30 s, at most 300 s). A sync call may answer the fast failure first, with `browser_unavailable`; with `wait_seconds` the job finishes after the pause. This keeps rule 6 ("outbound limits still apply").
  - b) Only a 403 falls back; a 429 or 503 stays a failure.
  - c) 403 and 503 fall back after the pause; 429 never does.
- **D4. Where the language choice is kept.**
  - a) In `localStorage`, with `?lang=` in the URL and a request header. There is no cookie, so P10-S13 ("no cookie under /ui") stays.
  - b) A `te_lang` cookie (SameSite=Strict, no secret), which amends P10-S13.
  - c) In `sessionStorage` only: per tab, gone when the tab closes.
- **D5. Doc layout.**
  - a) Short `README.md` and `README.de.md`, plus `docs/user-guide/en/` and `docs/user-guide/de/` with the same file names (P12-S11). The developer docs stay English.
  - b) Keep the long README and translate it in full as `README.de.md`, with no user guide.
  - c) `docs/en/` and `docs/de/`, with the developer docs moved into `docs/en/`.
- **D6. The configuration reference (1,267 lines).**
  - a) It stays English. Both guides get a "Key settings" section with about 25 settings, which a test checks.
  - b) Translate it in full as `docs/configuration.de.md`, kept in sync by the same tests.
  - c) English only, with no German settings section.
- **D7. Automated accessibility check.**
  - a) Vendored axe-core 4.14.0 (MPL-2.0) in the `browser` CI job. Every UI state is checked in both languages, and the gate is zero violations; Playwright keyboard tests are added.
  - b) The `axe-playwright-python` dev dependency. Its PyPI entry has no licence metadata, so it would need an allowlist entry.
  - c) Our own smaller checks only (lang, labels, ids, contrast from computed styles).
- **D8. Live check and tag.**
  - a) Before merging, on Jan's PC through Remote Control, in a fresh clone (about 25 minutes). The checks:
    - `docker compose up` with `BROWSER_UNBLOCK_ENABLED=true` and an admin key;
    - the UI in German and English;
    - a keyboard-only pass;
    - one page in both modes on a site Jan names as allowing extraction (else only the local 403 test site).

    After the squash merge, tag `v0.1.0`.
  - b) Jan runs a checklist himself.
  - c) Sandbox checks only.

## Review Focus

1. **Maximum mode for someone who may not use it.** For example: a user key that posts `access=maximum`; a batch item of a user's job; an admin while `BROWSER_UNBLOCK_ENABLED` is off. Expected: REST answers 403, or the item stays in the standard browser without the refusal fallback. Tests: Task 1 `test_plain_refusal_escalates_only_under_unblock`, Task 3 `test_forged_maximum_from_a_user_is_refused_by_rest`.
2. **A fallback that ignores the host or the result kind.** For example: a render before a 429/503 pause has ended, or a fallback on a document, media file, sitemap item or cached result. Expected: the pause holds and those results never escalate. Tests: Task 1 `test_a_refused_429_keeps_the_hosts_pause`, `test_refusal_of_a_document_media_or_cached_result_does_not_escalate`.
3. **Text in the wrong language or with broken escaping.** For example:
   - a literal English string left in a template or a Python message;
   - placeholders that differ between en and de;
   - markup in a catalogue value;
   - German in an API answer;
   - the switch reflecting `?lang=<script>`.

   Tests: Task 4 `test_templates_have_no_literal_text`, Task 2 `test_placeholders_match`, `test_api_texts_stay_english`, `test_unknown_lang_is_ignored_and_not_reflected`.
4. **Screen reader noise and lost focus from the 3 s progress refresh.** For example, a live region that announces every refresh, or focus on "Next" lost when the fragment is swapped. Expected: one announcement per state change and focus kept. Test: Task 6 `test_progress_refresh_keeps_focus_and_announces_once`.
5. **Docs drifting from the code or from each other.** For example: an example body the current model refuses; a renamed setting, tool or tab label; the German page lagging behind. Tests: Task 7 and Task 8 doc tests, and the CI pair check.

---

### Task 1: Browser fallback on a plain 403, 429 or 503 in maximum mode

**Files:**
- Modify: `src/text_extraction/escalation.py` (`REFUSAL_STATUSES`, `needs_browser`)
- Modify: `src/text_extraction/pipeline_render.py` (import `REFUSAL_STATUSES` from `escalation` as its `CHALLENGE_STATUSES`; check `python -c "import text_extraction.pipeline"` for an import cycle)
- Modify: `src/text_extraction/pipeline.py` (`_assesses_challenge` → `_assesses_refusal`, `_challenge_quality` → `_refusal_quality`, per P12-S1)
- Modify: `docs/architecture.md` (pipeline step 4, B5, B14 "Escalation", a new **B16, maximum extraction** with P12-S1 and P12-S2)
- Test: `tests/test_escalation.py`, `tests/test_pipeline_render.py`

**Interfaces:**
- Produces:
  - `escalation.REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({403, 429, 503})`;
  - `needs_browser(envelope: Envelope[ExtractResult], *, unblock: bool) -> bool` with the same signature and the P12-S1 rules;
  - `Pipeline._assesses_refusal(run, fetched) -> bool` and `Pipeline._refusal_quality(run, fetched) -> Quality`, which always returns a `Quality` (`challenge_status` set only for a challenge body).

- [ ] **Step 1: Write the failing tests.**
  - `test_plain_refusal_escalates_only_under_unblock[403|429|503]`: `failed`, `target_http_error`, `upstream_status` = s, `quality()` without a challenge. True with `unblock=True`, False without.
  - `test_other_error_statuses_never_escalate[401|404|410|500|502]`: False under unblock.
  - `test_refusal_of_a_document_media_or_cached_result_does_not_escalate`: `document` set, a platform `media`, `cache="hit"` → False.
  - `test_non_success_html_escalates_under_unblock`: a 2xx HTML `partial` (`content_too_short`, `browser_recommended=False`) is True under unblock and False without (D2 = a; drop it for D2 = b).
  - `test_refusal_answer_is_assessed_under_unblock[403|429|503]` (RenderHarness, admin, `ignore`, the switch on): a plain `<p>Forbidden</p>` HTML body and a `text/plain` body both get `quality` set, `challenge_status is False` and `needs_browser(...)` True. With `enforce`, or with the switch off, `quality is None`.
  - `test_refusal_escalation_renders_and_keeps_the_better`: fast 403, then the renderer answers 200 with the article: `success`, `mode_used == "browser"`, `unblocked is True`.
  - `test_a_refused_429_keeps_the_hosts_pause`: a fast 429 with `Retry-After: 5` leaves `out:cooldown:*`, and `DomainGate.slot` raises `CircuitOpen` until the pause ends.
- [ ] **Step 2: Run** `uv run pytest tests/test_escalation.py tests/test_pipeline_render.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** In `needs_browser`, move the unblock branch so that a refusal is judged before the `browser_recommended` test. Widen `_assesses_refusal` to every refusal status, whatever the content type; `_refusal_quality` assesses the HTML body or `assess("", "")`. Update the docstrings and `architecture.md`.
- [ ] **Step 4: Run** the tests above plus `uv run pytest -q tests/test_extract_api.py tests/test_worker.py tests/test_browser_worker.py` and `uv run mypy`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: fall back to the browser on a plain 403, 429 or 503 in unblock mode`.

**Done when:** the new tests pass; nothing outside unblock mode changed (the existing escalation and render tests pass unchanged); B16 is in `architecture.md`.

### Task 2: i18n core, language choice and the rule amendment

**Files:**
- Create: `src/text_extraction/ui/i18n.py`, `src/text_extraction/ui/locales/en.json`, `src/text_extraction/ui/locales/de.json` (the keys of the shell, login, switch and status texts at first)
- Modify: `src/text_extraction/ui/templating.py` (a context processor that adds `t`, `lang` and `languages`), `ui/routes.py` (a router dependency that sets `request.state.ui_language`, and `Content-Language` on answers), `templates/base.html` (`<html lang="{{ lang }}">` and the `data-text-*` attributes for `ui.js`), `templates/index.html` and `login.html` (the switch, P12-S7), `ui/static/ui.js` (the `X-UI-Language` header on `/ui/run/` requests, `te.lang`, the move on load, texts read from `data-text-*`)
- Modify: `src/text_extraction/settings.py` (`ui_default_language: Literal["en", "de"] = "en"`), `docs/configuration.md` (its row), `deploy/helm/text-extraction/values.yaml` (`config.UI_DEFAULT_LANGUAGE: "en"`)
- Modify: `CLAUDE.md` rule 1. The new text: "**English only** in code, comments, developer docs, commit messages and API texts. User-facing docs (`README.md` and `README.de.md`, `docs/user-guide/en|de/`) and the UI texts (`ui/locales/en.json`, `de.json`) exist in English and German. English is the source: a change to an English text changes its German partner in the same pull request (checked by `tests/test_docs_i18n.py`, `tests/ui/test_i18n.py` and `scripts/check_translation_pairs.py`)."
- Modify: `docs/architecture.md` ("MCP and UI": P12-S5 to P12-S7; remove "a UI language other than English" from "Known limits")
- Test: `tests/ui/test_i18n.py`, `tests/ui/test_language.py`, `tests/test_settings.py`

**Interfaces:**
- Produces, in `ui/i18n.py`:
  - `Language = Literal["en", "de"]` and `LANGUAGES: Final[tuple[Language, ...]] = ("en", "de")`;
  - `CATALOGUES: Final[Mapping[Language, Mapping[str, str]]]`, loaded once at import;
  - `translate(language: Language, key: str, /, **params: object) -> str`;
  - `negotiate(accept_language: str | None, default: Language) -> Language`;
  - `page_language(request: Request, settings: Settings) -> Language` (query `lang`, `Accept-Language`, default);
  - `run_language(request: Request, settings: Settings) -> Language` (the `X-UI-Language` header, `Accept-Language`, default);
  - `LANGUAGE_HEADER: Final = "x-ui-language"`;
  - `@dataclass(frozen=True) class Message`, with `key: str`, `params: Mapping[str, object]` (default empty) and `text(language: Language) -> str`.
- Templates get `t(key, **params)`, `lang` and `languages`. `ui.js` uses the storage key `te.lang`.

- [ ] **Step 1: Write the failing tests.**
  - `test_catalogues_have_the_same_keys`
  - `test_placeholders_match`: the `{name}` set per key is equal in en and de.
  - `test_values_are_plain_text`: no `<` or `>` in any value.
  - `test_translate_falls_back_to_english`
  - `test_negotiate` with these cases:
    - `"de-DE,de;q=0.9,en;q=0.8"` → `de`;
    - `"en-US"` → `en`;
    - `"fr, de;q=0.5"` → `de`;
    - `"fr"` → default;
    - `""` → default;
    - `"de;q=0"` → default;
    - `"*;q=abc"` → default.
  - `test_page_language_from_query_header_and_default`: `/ui?lang=de` gives `<html lang="de">` and `Content-Language: de`.
  - `test_unknown_lang_is_ignored_and_not_reflected`: `?lang=<script>` gives `en`, and the body has no `<script>`.
  - `test_run_fragment_uses_the_language_header`
  - `test_no_cookie_and_csp_unchanged`
  - `test_language_header_is_not_forwarded`: the headers `forward` sends.
  - `test_api_texts_stay_english`: `POST /v1/extract` with `X-UI-Language: de` and `Accept-Language: de` answers an English `error.message`.
  - `test_switch_marks_the_active_language`: `aria-current`, plus `lang` and `hreflang` per link.
  - `test_ui_default_language_setting`
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_i18n.py tests/ui/test_language.py tests/test_settings.py tests/test_configuration_doc.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the module, the catalogues, the dependency, the switch and the `ui.js` changes. Amend CLAUDE.md rule 1, and update the configuration row and the Helm value.
- [ ] **Step 4: Run** the tests, the whole `tests/ui` suite (the English texts are unchanged) and `bash deploy/helm/test.sh`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: add German and English UI catalogues with a language switch`.

**Done when:** both languages render the shell and login; the switch works without a cookie; the CSP is unchanged; rule 1 is amended.

### Task 3: "Site rules" and "Browser fallback" in the forms

**Files:**
- Create: `src/text_extraction/ui/templates/forms/access.html`
- Modify: `templates/forms/mode.html` (P12-S4), `extract.html`, `structured.html`, `screenshot.html`, `chunk.html`, `batch_options.html` (so also the batch, batch file and tree forms), `site_structure.html`, `result.html` (the compliance mode and the unblocked badge)
- Modify: `ui/forms.py` (`_access`), `ui/routes.py` (`_app_context`), `ui/job_views.py` (batch, batch file, site and tree pass the field on), the locales (`access.*`, `mode.*`, `hint.access.*` in en and de)
- Test: `tests/ui/test_access.py`, `tests/ui/test_operations.py`

**Interfaces:**
- Consumes: Task 2's `t`, `Message`.
- Produces:
  - `ACCESS_FIELD: Final = "access"` with the values `"respect"` and `"maximum"`;
  - `_access(fields: Fields, form: Mapping[str, str]) -> None`, which adds `compliance: "ignore"` (`set_by_user=True`) for `maximum`;
  - `access_choice(role: str, settings: Settings) -> Literal["hidden", "enabled", "disabled"]`;
  - the context keys `access_choice` and `browser_enabled`.

- [ ] **Step 1: Write the failing tests.**
  - `test_maximum_adds_compliance_ignore`: `build_body("extract", {"url": U, "access": "maximum"})["compliance"] == "ignore"`.
  - `test_respect_sends_no_compliance`
  - `test_maximum_and_options_compliance_is_given_twice`
  - `test_choice_shown_only_to_admins`: for a user there is no `name="access"`; an admin with both switches on gets `enabled`; an admin with the unblock switch off gets `disabled` plus the hint.
  - `test_forged_maximum_from_a_user_is_refused_by_rest`: `/ui/run/extract` with a user key and `access=maximum` shows 403 `forbidden`, and nothing is fetched.
  - `test_browser_option_disabled_while_browser_mode_is_off`
  - `test_batch_tree_and_site_carry_the_access_choice`: the forwarded `/v1/batch` and `/v1/site-structure` bodies have `compliance: "ignore"`.
  - `test_file_form_has_no_access_choice`
  - `test_result_shows_the_unblocked_badge`
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_access.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Use a radio group in a `<fieldset>` with a `<legend>`; each option has a hint tied with `aria-describedby`. Write the new texts as catalogue keys from the start.
- [ ] **Step 4: Run** `uv run pytest -q tests/ui`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: offer respect or maximum extraction and an optional browser fallback in the UI`.

**Done when:** an admin can choose maximum extraction in every listed form, and users never see it; the browser fallback reads as a choice.

### Task 4: Translate the templates

**Files:**
- Modify: every file under `src/text_extraction/ui/templates/` not covered by Tasks 2 and 3, `ui/routes.py` (`Tab.label` → `Tab.label_key`; `BANNER`, `NOT_VALID` and `NO_FILE` become `Message`s), the locales
- Test: `tests/ui/test_templates_i18n.py`

**Interfaces:**
- Consumes: Task 2's `t`, `Message`, `CATALOGUES`.
- Produces: `Tab(name: str, label_key: str, template: str)`, and the key families `tab.*`, `login.*`, `result.*`, `progress.*`, `tree.*`, `batch.*` and `form.*`.

- [ ] **Step 1: Write the failing tests.**
  - `test_templates_have_no_literal_text`. Strip the Jinja blocks and expressions, comments and tags. Any remaining word with a letter must be in `ALLOWED_LITERALS` (`{"curl", "JSON", "URL", "URLs", "HTTP", "API", "MCP", "PNG", "CSV", "LLM", "ASR", "https"}`). Check the attributes `placeholder`, `title`, `aria-label`, `alt` and `hx-confirm` the same way.
  - `test_every_key_used_exists`: every `t("…")` in the templates and every `Message("…")` in `ui/*.py`.
  - `test_no_unused_keys`: every key is used, or is under a dynamic prefix (`warning.`, `error.`, `timeline.`).
  - `test_page_renders_in_both_languages`: in `de`, the login heading is "Anmelden" and the tab "Extrahieren"; in `en` it is "Log in" and "Extract".
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement**, template by template, writing the English values word for word as the texts are today, so the existing English tests keep passing.
- [ ] **Step 4: Run** `uv run pytest -q tests/ui` and `uv run pytest -m browser -q tests/ui`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: translate the UI templates into German`.

**Done when:** no template holds literal text; both languages render every view.

### Task 5: Translate the UI's Python messages

**Files:**
- Modify:
  - `ui/texts.py`: `WARNING_TEXTS` moves into the catalogues as `warning.<name>`, with `warning_text(name: str, language: Language) -> str | None`.
  - `ui/answers.py`: `envelope_message`.
  - `ui/forms.py`: `OptionsError` carries a `Message`.
  - `ui/job_views.py`, `ui/url_lists.py`, `ui/results.py` (labels), `ui/timeline.py` (steps and details), `ui/site_tree.py` (`found_labels`), `ui/previews.py` (skip reasons).
  - The locales.
- Test: `tests/ui/test_texts.py` (extend), `tests/ui/test_url_lists.py`, `tests/ui/test_jobs_views.py`

**Interfaces:**
- Consumes: Task 2's `Message`, `translate`.
- Produces:
  - `envelope_message(body: Mapping[str, Any] | None, status: int, language: Language) -> tuple[str, str | None]`, which returns the translated `error.<code>` text and the API's English message;
  - `OptionsError(message: Message)`;
  - `error.<code>` keys for every `ErrorCode` member.

- [ ] **Step 1: Write the failing tests.**
  - `test_every_documented_warning_has_a_text`: the documented warnings equal the `warning.` keys in en and in de.
  - `test_every_error_code_has_a_text_in_both_languages`
  - `test_action_failed_text_in_german`: "Browser-Aktion 2 ist fehlgeschlagen."
  - `test_url_list_problems_in_german`
  - `test_fragment_shows_translated_error_and_english_detail`: a 422 in a `de` fragment.
  - `test_timeline_in_german`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Python returns `Message`s; the templates render them with `t`.
- [ ] **Step 4: Run** `uv run pytest -q tests/ui` and `uv run mypy`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: translate the UI's warnings, errors and job messages`.

**Done when:** every text the UI shows comes from a catalogue; only API detail lines stay English.

### Task 6: Accessibility, help texts and status messages

**Files:**
- Modify: `ui/static/ui.js` (the tabs keyboard pattern, `#ui-status`, `data-error-for`, `data-announce`), `ui/static/ui.css` (`:focus-visible`, contrast, `.visually-hidden`, reduced motion), the templates (skip link, landmarks, `aria-describedby`, intros and help links, `<progress aria-label>`, stable ids in `progress_live.html`, the `hx-confirm` text from the catalogue), `ui/forms.py` (`OptionsError.field`), `form_error.html` (`data-error-for`), `ui/i18n.py` (`DOCS_BASE_URL`, `help_url`), the locales (`help.*`, `hint.*`, `status.*`)
- Create: `tests/ui/vendor/axe-core-4.14.0.min.js` (from the npm tarball `axe-core-4.14.0.tgz`, unmodified), `tests/ui/vendor/axe-core-LICENSE.txt`
- Test: `tests/ui/test_a11y_markup.py` (default suite), `tests/ui/test_a11y_browser.py` (`browser`)

**Interfaces:**
- Consumes: Tasks 2 to 5.
- Produces:
  - `DOCS_BASE_URL: Final = "https://github.com/janschachtschabel/text-extraction/blob/main/docs/user-guide"`;
  - `help_url(language: Language, page: str) -> str`;
  - `OptionsError.field: str | None`;
  - the HTML contracts `#ui-status[role=status][aria-live=polite]`, `[data-error-for=<control id>]` and `[data-announce]`.

- [ ] **Step 1: Write the failing markup tests** (rendered with TestClient in both languages, as an admin):
  - `test_every_control_has_a_label`
  - `test_ids_are_unique_on_the_page`
  - `test_described_by_targets_exist`
  - `test_tabs_have_roving_tabindex`
  - `test_progress_has_an_accessible_name`
  - `test_progress_buttons_have_stable_ids`
  - `test_help_link_points_to_the_guide_in_the_page_language`: `…/de/single-url.md` on the German page.
  - `test_vendored_axe_is_pinned`: sha256 constant, licence file present.
- [ ] **Step 2: Write the failing browser tests:**
  - `test_axe_finds_no_violations[en|de]` over these states: the login; the login with an error; every tab; an extract result; a form error; a running progress view; a completed one; the site tree. Inject the axe source with `page.evaluate` (no `<script>` tag, so the CSP stays on), run with the tags from Global Constraints, and assert `violations == []`, printing each violation's id and target on failure.
  - `test_tabs_follow_the_aria_keyboard_pattern`: ArrowRight, ArrowLeft, Home and End move focus and selection.
  - `test_focus_is_visible`: the computed `outline-width` of a focused button is at least 2 px.
  - `test_progress_refresh_keeps_focus_and_announces_once`
  - `test_busy_status_while_a_request_runs`
  - `test_error_is_associated_with_its_field`: invalid Options JSON → `aria-invalid="true"` on the textarea, `aria-describedby` holding the error's id, and focus on the field.
  - `test_language_switch_keeps_the_login`: a German app with no console message.
- [ ] **Step 3: Run** `uv run pytest tests/ui/test_a11y_markup.py -q` and `uv run pytest -m browser tests/ui/test_a11y_browser.py -q`. Expected: FAIL.
- [ ] **Step 4: Implement** until both pass. Fix each axe finding at its cause; never exclude a rule or an element.
- [ ] **Step 5: Run** `uv run pytest -q tests/ui` and `uv run pytest -m browser -q tests/ui`. Expected: PASS.
- [ ] **Step 6: Commit** `feat: make the playground accessible, with help texts and status messages`.

**Done when:** axe reports zero violations in every listed state in both languages; the keyboard tests pass; every tab has an intro and a help link.

### Task 7: Bilingual README, the user guide's base pages and the sync checks

**Files:**
- Create: `README.de.md`, and `docs/user-guide/{en,de}/index.md`, `installation.md` (local with uv, Docker Compose, a Helm pointer, "Key settings" per P12-S14), `usage.md` (API keys, the envelope, sync and jobs, the playground, Browser fallback), `parameters.md` (every top-level field of each request model, with type, default and meaning), `glossary.md`
- Modify: `README.md` (a landing page per P12-S11, with "Deutsch: README.de.md" at the top; the sections Task 8 moves stay until Task 8)
- Create: `tests/test_docs_i18n.py`, `scripts/check_translation_pairs.py`, `tests/test_check_translation_pairs.py`
- Modify: `.github/workflows/ci.yml` (lint job, `pull_request` only: `fetch-depth: 0`, then `uv run python scripts/check_translation_pairs.py --base ${{ github.event.pull_request.base.sha }}`)

**Interfaces:**
- Produces:
  - `tests/test_docs_i18n.py`:
    - `PAIRS: list[tuple[Path, Path]]`, from `docs/user-guide/en/*.md` and the README pair;
    - `structure(text: str) -> DocStructure`, which holds `headings: list[int]`, `code_blocks: list[str]`, `links: list[str]` and `table_rows: list[int]`;
    - `REQUEST_MODELS: Mapping[str, type[BaseModel]]`: `POST /v1/extract` → `ExtractRequest`, `/v1/structured` → `StructuredRequest`, `/v1/chunk` → `ChunkRequest`, `/v1/batch` → `BatchRequest`, `/v1/site-structure` → `SiteStructureRequest`, `/v1/screenshot` → `ScreenshotRequest`, `/v1/extract/file` → `FileForm`.
  - `scripts/check_translation_pairs.py`: `main(argv: Sequence[str] | None = None) -> int` with `--base <ref>`.

- [ ] **Step 1: Write the failing tests.**
  - In `tests/test_docs_i18n.py`:
    - `test_every_user_doc_has_both_languages`
    - `test_structures_match`
    - `test_code_blocks_are_identical`
    - `test_relative_links_resolve`
    - `test_request_examples_are_valid`
    - `test_routes_named_exist`
    - `test_named_settings_exist`
    - `test_parameters_page_names_every_field`: every field of each `REQUEST_MODELS` model, in both languages.
    - `test_readmes_link_each_other`
  - In `tests/test_check_translation_pairs.py` (on a temporary git repo):
    - `test_english_only_change_fails`
    - `test_both_changed_passes`
    - `test_german_only_change_passes`
    - `test_trailer_exempts_a_path`
    - `test_catalogue_pair_is_checked`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Write** the English pages, then the German ones, the script and the CI step.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_docs_i18n.py tests/test_check_translation_pairs.py tests/test_handover_doc.py`. Expected: PASS.
- [ ] **Step 5: Commit** `docs: add the German README, the user guide's base pages and translation checks`.

**Done when:** installation, usage, parameters and the glossary exist in both languages and pass the sync and correctness tests; CI runs the pair check on pull requests.

### Task 8: The use-case guides, MCP and structured data

**Files:**
- Create: `docs/user-guide/{en,de}/site-rules.md`, `single-url.md`, `part-of-a-site.md`, `whole-site-and-metadata.md`, `structured-data.md`, `mcp.md`
- Modify: `README.md` and `README.de.md` (the moved sections become one paragraph and a link each), `docs/user-guide/{en,de}/index.md`
- Test: `tests/test_docs_i18n.py` (extend)

**Interfaces:**
- Consumes: Task 7's `structure`, `REQUEST_MODELS`, `PAIRS`; `TOOL_ROUTES` from `mcp_server/server.py`; `StructuredStrategy`; the catalogue tab labels.
- Produces, per page:
  - **`site-rules.md`.** Respect versus maximum extraction, who may use maximum extraction, what it does and does not do (B15, rule 6), the 429/503 pause, and the REST, MCP and UI ways.
  - **`single-url.md`.** The UI (Extract tab), curl and `extract_url`, and the fields to read: `content`, `metadata`, `quality`, `compliance` and `warnings`.
  - **`part-of-a-site.md`.** Site structure with `scope: path` and `depth`; the tree with "Extract selected", and a batch of URLs or a CSV.
  - **`whole-site-and-metadata.md`.** Site structure at depth N, a batch of every page with `metadata` and "Metadata, structured" (schema.org and LRMI fields, licences), and reading the job pages.
  - **`structured-data.md`.** The strategies `metadata`, `selectors`, `patterns`, `llm` and `auto`, with `schema`, `tables`, `learn_selectors`, `sources` and `missing`; `POST /v1/structured`, `extract_structured` and the Structured tab; the LLM key header.
  - **`mcp.md`.** Setup (Claude Code, Claude Desktop through `mcp-remote`, other clients), authentication, all 11 tools in a table (tool, purpose, route, main arguments), the job pattern with `get_job`, `content_offset`, errors, and example prompts.

- [ ] **Step 1: Write the failing tests:**
  - `test_mcp_guide_names_every_tool`: each of the 11 `TOOL_ROUTES` values in both languages, and the count 11.
  - `test_structured_guide_names_every_strategy`
  - `test_use_case_pages_exist`
  - `test_bold_tab_names_match_the_catalogue`: `**…**` tab names in `en` pages are `en` labels, and in `de` pages `de` labels.
  - `test_readme_has_no_moved_section`: no README heading named "Site structure", "MCP" or "Structured output and chunking" holds more than one paragraph.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Write** the English pages, then the German ones; mark every request example with `<!-- request: … -->`.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_docs_i18n.py`. Expected: PASS.
- [ ] **Step 5: Commit** `docs: add the bilingual use-case guides for MCP, single URLs, sites and structured data`.

**Done when:** every use case Jan named has a page in both languages with UI, REST and MCP ways; the MCP page lists all 11 tools.

### Task 9: Done-when, live checks and the release

**Files:**
- Create: `tests/browser/test_maximum_extraction.py` (`browser`). The local site (pytest-httpserver) answers 403 to a `User-Agent` containing `text-extraction` and the article to Chrome's. It runs real Patchright Chromium through the pipeline and the browser worker loop.
- Modify: `tests/ui/test_done_when.py` (every entry of `OPERATIONS` and the job views in `en` and `de`; the access choice as an admin)
- Modify: `docs/packages.md` (row 12 and its note), `CHANGELOG.md` (`[0.1.0]`: package 12), `README.md` and `README.de.md` ("Status")

- [ ] **Step 1: Write the failing tests:**
  - `test_admin_maximum_extraction_gets_past_a_plain_403`: `success`, `mode_used` `browser`, `unblocked` true.
  - `test_respect_mode_keeps_the_403`: `target_http_error`, `mode_used` `fast`.
  - `test_user_maximum_is_forbidden`
  - The `de` variants in `test_done_when.py`.
- [ ] **Step 2: Run** them to FAIL, then implement whatever glue is missing, then run them to PASS.
- [ ] **Step 3: Full verification:**
  - `uv run pytest -q`
  - `uv run pytest -m browser -q`
  - `uv run ruff check .` and `uv run ruff format --check .`
  - `uv run mypy`
  - `bash deploy/helm/test.sh`
  - `uv run python scripts/check_licenses.py --lock uv.lock --extra browser`
- [ ] **Step 4 (controller):** Dispatch `live.yml` on the branch (`wlo_200`, auto) and record "Ok rate:" (gate 80 %).
- [ ] **Step 5 (controller, D8 = a):** Start Remote Control on Jan's PC in a fresh clone (`C:\Users\jan\te-pkg12-verify`):
  1. `docker compose up -d --wait` with `BROWSER_UNBLOCK_ENABLED=true` and an admin key.
  2. The UI in German and English through the switch.
  3. A keyboard-only extract and site tree.
  4. One page in respect mode and one in maximum mode on the site Jan named.
  5. `docker compose down -v`.

  Record the results in the row 12 note.
- [ ] **Step 6:** Commit `docs: record the package 12 evidence`. Open the PR; after the review findings are fixed and CI is green, squash-merge.
- [ ] **Step 7 (controller, after the merge):** Tag the merge commit `v0.1.0`, push the tag, and check that the image workflow published `0.1.0` for all five images.

**Proposed `docs/packages.md` row 12 "Done when":**

> Maximum extraction escalates a plain 403, 429 or 503 for an admin with `compliance: ignore` and `BROWSER_UNBLOCK_ENABLED`, and nowhere else (`tests/test_escalation.py`, `tests/browser/test_maximum_extraction.py`).
>
> The UI drives every endpoint in German and English (`tests/ui/test_done_when.py`), with a language switch and no cookie. axe-core 4.14.0 finds no WCAG 2.2 A/AA violation in any UI state in either language, and the keyboard tests pass (`pytest -m browser tests/ui`). Every text comes from a catalogue whose en and de keys and placeholders match (`tests/ui/test_i18n.py`, `tests/ui/test_templates_i18n.py`).
>
> `README.md`/`README.de.md` and `docs/user-guide/{en,de}/` cover installation, usage, parameters, site rules, one URL, part of a site, a whole site with metadata, structured data and the 11 MCP tools, and pass the sync and correctness tests (`tests/test_docs_i18n.py`) and the CI pair check.
>
> Live: `wlo_200` auto ≥ 80 %, and the check on Jan's PC (D8).

**Done when:** the row 12 criterion above holds and the PR is merged; `v0.1.0` is tagged.
