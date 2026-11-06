# Package 16: robots marks in the site tree and a per-run site rules switch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The playground's site tree marks URLs that the site's rules (robots.txt, TDM opt-out) block, its selection buttons skip them while the rules are respected, and every playground form lets the caller switch the checks per run between `enforce`, `report` and `ignore` as far as their key allows.

**Architecture:** The tree view judges every shown URL at view time against the current origin files (robots.txt, tdmrep.json, ai.txt), read from the compliance cache or loaded once per origin in the host's outbound turn (new `ComplianceChecker.origin_rules`, `Pipeline.origin_rules`). A pure step in `ui/site_tree.py` marks the leaves. The "Site rules" choice of the forms becomes three steps offered by role (`AccessOffer`); the batch under the tree is preset with the crawl's mode, and `ui.js` disables blocked boxes while the chosen step is `enforce`. No REST, MCP or permission change.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, htmx, plain JS (`ui.js`), protego, pytest, Playwright (Chromium) for `-m browser`.

**Spec:** `/mnt/project-files/package-16/plan-paket-16.md` (German; approved by Jan 2026-11-06 with D1-D4 = a) and GitHub issue #26. D1 a: marks judged at view time. D2 a: three steps, `ignore` also without unblock mode. D3 a: under `report` or `ignore` the select buttons include blocked URLs. D4 a: the tree's batch is preset with the crawl's mode.

## Global Constraints

- English in code, comments, developer docs and commit messages; every English UI text in `ui/locales/en.json` has its German partner in `de.json`; every changed user doc in `docs/user-guide/en/` changes its partner in `docs/user-guide/de/` (and `README.md` with `README.de.md`) in the same task (`tests/test_docs_i18n.py`, `tests/ui/test_i18n.py`, `scripts/check_translation_pairs.py`).
- No REST, OpenAPI or MCP change; `compliance.resolve_mode` keeps its rules (admins any mode; others the default, and `report`/`enforce` only while `COMPLIANCE_ALLOW_OVERRIDE`; `ignore` admin only).
- Every request for an origin file goes through the existing `Fetcher` (SSRF guard) inside a `HostTurn` of the outbound gate (per-host limits); a gate refusal or a Valkey error never fails the tree view.
- The tree view loads the rules of at most `TREE_RULES_MAX_ORIGINS = 20` origins, within `TREE_RULES_SECONDS = 12` in total (constants in `ui/job_views.py`, not settings).
- Block reasons are exactly `"robots"` (robots.txt disallows), `"robots_unavailable"` (robots.txt could not be retrieved) and `"tdm"` (tdmrep.json reservation, ai.txt disallow, or `noai`).
- Accessibility: every badge's reason is readable by a screen reader (text in the label, not only `title`); axe-core stays clean at 1440 and 390 px in `en` and `de`.
- Quality gates per task: `uv run pytest` (task's tests plus `tests/ui` when UI changed), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.

## Review Focus

1. robots.txt answers 5xx for the tree's origin: every URL of that origin is marked `robots_unavailable` and, under `enforce`, cannot be ticked (the batch would refuse them all). Test in Task 4.
2. The outbound gate refuses (host busy) or Valkey fails while the tree loads rules: the view still answers 200, the origin is named in the "could not be read" line, its URLs carry no mark. Tests in Tasks 1 and 4.
3. A user key with `COMPLIANCE_ALLOW_OVERRIDE=false`: no choice is shown, the tree's blocked boxes are disabled (only `enforce` applies). Test in Task 3 and Task 4.
4. `COMPLIANCE_DEFAULT=report`: the default radio is `report` and sends nothing; a crawl that ran under `enforce` presets the tree batch to `enforce`, which is sent explicitly; blocked boxes start disabled. Test in Task 4.
5. "Options (JSON)" with `compliance` while the switch is on a non-default step: refused "compliance is given twice."; with the switch on the default step: the Options value goes through. Test in Task 3.

---

### Task 1: Origin rules in compliance and the pipeline

**Files:**
- Modify: `src/text_extraction/compliance.py`
- Modify: `src/text_extraction/pipeline.py` (next to `robots_sitemaps`, `blocked_from_cache`)
- Test: `tests/test_compliance.py`, `tests/site/test_pipeline_hooks.py` (where `robots_sitemaps` of the pipeline is tested)

**Interfaces:**
- Produces (in `compliance.py`, all exported):
  - `BlockReason = Literal["robots", "robots_unavailable", "tdm"]`
  - `def block_reason(report: ComplianceReport) -> BlockReason | None` — whatever the mode: `robots == "disallowed"` → `"robots"`; `robots == "unavailable"` → `"robots_unavailable"`; `tdm_reserved is True or noai or ai_txt == "disallowed"` → `"tdm"`; else None. (Same order as `_violation`.)
  - `def origin_of(url: str) -> str | None` — `_origin(url).base`, None for a URL `_origin` refuses.
  - `@dataclass(frozen=True) class OriginRules` holding the three `_Entry`s and the parsed robots.txt and ai.txt rules, with `def judge(self, url: str, mode: ComplianceMode) -> ComplianceReport` (sync, no I/O).
  - `async def ComplianceChecker.origin_rules(self, url: str, *, cached_only: bool) -> OriginRules | None` — `cached_only`: one MGET of the three keys, None while any is missing or the read fails; otherwise loads the missing files with `_load` within one `_FILE_TIMEOUT_SECONDS` budget, as `before_fetch` does. `ignore` plays no part (it reads in every mode).
- Produces (in `pipeline.py`): `async def Pipeline.origin_rules(self, url: str) -> OriginRules | None` — cached first; else takes a `HostTurn` for the URL's host (as `robots_sitemaps` does) and loads; `OutboundBusy`, `CircuitOpen` and `RedisError` answer None (logged like `robots_sitemaps`'s warning), never raise.

- [ ] **Step 1: Write the failing tests** in `tests/test_compliance.py`:
  - `test_origin_rules_judge_equals_before_fetch` — for a robots.txt `User-agent: *\nDisallow: /private`, a tdmrep.json reserving `/tdm/*` and an ai.txt disallowing `/ai`, and URLs `/`, `/private/x`, `/tdm/a`, `/ai/b`: `(await checker.origin_rules(url, cached_only=False)).judge(url, mode) == await checker.before_fetch(url, mode)` for `mode` in `enforce`, `report`.
  - `test_origin_rules_cached_only_needs_all_three_files` — fresh cache: `cached_only=True` is None and the fetcher saw no request; after one `before_fetch`, `cached_only=True` answers without a request.
  - `test_block_reason_maps_each_violation` — reports with `robots="disallowed"` → `"robots"`, `robots="unavailable"` → `"robots_unavailable"`, `tdm_reserved=True` / `noai=True` / `ai_txt="disallowed"` → `"tdm"`, an allowed report and a `mode="ignore"` skipped report → None.
  - `test_origin_of` — `origin_of("https://Example.org:443/a?b") == "https://example.org"`, `origin_of("http://example.org:8080/") == "http://example.org:8080"`, `origin_of("ftp://x/")` is None.
  In `tests/site/test_pipeline_hooks.py`: `test_pipeline_origin_rules_loads_in_a_turn_then_from_cache` (first call fetches robots.txt, tdmrep.json and ai.txt once; second call fetches nothing) and `test_pipeline_origin_rules_answers_none_when_the_gate_refuses` (gate raising `OutboundBusy`, as the `robots_sitemaps` refusal test sets it up).
- [ ] **Step 2: Run them**: `uv run pytest tests/test_compliance.py tests/site/test_pipeline_hooks.py -q` — the new tests FAIL (names not defined).
- [ ] **Step 3: Implement.** Make `_judge` build an `OriginRules` (parsing through `_rules`, so the parse cache still applies) and return `rules.judge(url, mode)`; move the bodies of `_apply_robots`, `_apply_tdmrep`, `_apply_ai_txt` where `OriginRules.judge` can call them without the checker (they use no checker state). `before_fetch`/`cached_before_fetch` behave exactly as before. Update the module docstring (step list) with `origin_rules`.
- [ ] **Step 4: Run** `uv run pytest tests/test_compliance.py tests/site tests/test_pipeline.py -q` — PASS; ruff, mypy clean.
- [ ] **Step 5: Commit** `feat(compliance): judge many URLs of an origin from one load of its rules`.

### Task 2: Blocked leaves in the site tree model

**Files:**
- Modify: `src/text_extraction/ui/site_tree.py`
- Test: `tests/ui/test_site_tree.py` (items from `tests/ui/site_items.py`)

**Interfaces:**
- Consumes: `BlockReason`, `block_reason`, `origin_of`, `OriginRules.judge` (Task 1).
- Produces:
  - `TreeNode.blocked: BlockReason | None = None`.
  - `SiteTree` gains `crawl_mode: ComplianceMode | None = None`, `blocked: int = 0` (leaves with a reason), `unchecked: tuple[str, ...] = ()` (origins, as `origin_of` gives them, whose URLs could not be judged), all with defaults.
  - `LINKED_ONLY` gains `"blocked"`: a link the crawl did not follow because the rules block it under `enforce` (issue #26: "URLs with a `blocked` decision") becomes a linked-only leaf like a `depth` or `max_pages` link; it carries no reason of its own (the link does not say robots or TDM), `mark_blocked` judges it.
  - `TreeBuilder.add` records the crawl's mode from the first item whose `data.compliance` is set (`data.compliance.mode`), and a crawled page's own reason: `block_reason(data.compliance)` (so a page that failed `blocked_by_robots`, or was `reported` under `report`, is marked; `_Found` carries it; `merge` keeps the first non-None reason).
  - `def leaf_origins(tree: SiteTree) -> dict[str, str]` — origin → the first leaf URL of it, ordered by the number of leaves of the origin (most first, then the origin's text); leaves whose URL `origin_of` refuses are left out.
  - `def mark_blocked(tree: SiteTree, rules: Mapping[str, OriginRules | None]) -> SiteTree` — pure: each leaf whose `blocked` is None and whose origin has rules gets `block_reason(rules.judge(url, "enforce"))`; origins of `leaf_origins(tree)` that are missing from `rules` or map to None form `unchecked` (in `leaf_origins` order); `blocked` counts the leaves with a reason (also those the crawl marked). Returns a new `SiteTree` (nodes may be updated in place).

- [ ] **Step 1: Write the failing tests:**
  - `test_crawled_page_blocked_by_robots_is_marked` — a page item that failed with `blocked_by_robots` and `data.compliance.robots == "disallowed"` gives a leaf with `blocked == "robots"`; an allowed page has None.
  - `test_tree_records_the_crawl_mode` — items whose `data.compliance.mode` is `"ignore"` → `tree.crawl_mode == "ignore"`; a tree of no item → None.
  - `test_mark_blocked_judges_sitemap_and_linked_leaves` — with `OriginRules` from a robots.txt `Disallow: /sammlung/_page` (built through `ComplianceChecker.origin_rules` on a `FakeFetcher`), a sitemap entry `/sammlung/_page2` and a linked-only `/sammlung/_page3` get `"robots"`, `/sammlung/` stays None, `tree.blocked == 2`.
  - `test_mark_blocked_names_origins_without_rules` — two origins, rules for one only (the other None): the other is in `unchecked`, its leaves unmarked.
  - `test_leaf_origins_orders_by_leaf_count`.
  - `test_blocked_links_are_linked_only_leaves` — a page item with a link of `crawl: "blocked"` gives a `linked` leaf for it (blocked None before `mark_blocked`).
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_site_tree.py -q` — new tests FAIL.
- [ ] **Step 3: Implement** in `site_tree.py`; extend the module docstring with a paragraph on marks (sources: the page's own report, then the view's judgement under `enforce`).
- [ ] **Step 4: Run** `uv run pytest tests/ui/test_site_tree.py -q` — PASS; ruff, mypy clean.
- [ ] **Step 5: Commit** `feat(ui): mark the site tree's leaves that the site's rules block`.

### Task 3: Three-step "Site rules" choice in every form

**Files:**
- Modify: `src/text_extraction/ui/forms.py` (`_access`, replace `access_choice`), `ui/routes.py`, `ui/job_views.py` (batch preview context), `ui/templates/forms/access.html`, `ui/templates/forms/mode.html`, `ui/locales/en.json`, `ui/locales/de.json`
- Test: `tests/ui/test_access.py` (rewrite its choice tests), `tests/ui/test_done_when.py` (its "Site rules" step)

**Interfaces:**
- Produces (in `forms.py`):
  - `ACCESS_DEFAULT: Final = "default"` (the radio value of the deployment's default step; sends nothing). `ACCESS_RESPECT`/`ACCESS_MAXIMUM` are removed.
  - `@dataclass(frozen=True) class AccessOffer: modes: tuple[ComplianceMode, ...]; default: ComplianceMode; unblock: bool` with `shown` (property: `len(modes) > 1`).
  - `def access_offer(role: str, settings: Settings) -> AccessOffer` — `modes` in the order `enforce`, `report`, `ignore`: an admin all three; anyone else `enforce` and `report` while `compliance_allow_override`, plus `ignore` only when it is the default; without override only the default. `unblock` is True for an admin while `browser_enabled` and `browser_unblock_enabled`.
  - `def access_value(offer: AccessOffer, mode: ComplianceMode | None) -> str` — the radio value that presets `mode`: `ACCESS_DEFAULT` for None, the default, or a mode not in `offer.modes`; else the mode's name.
  - `_access`: a posted `access` of `enforce`, `report` or `ignore` becomes `compliance` (set by the user); `default`, empty or any other value sends nothing.
- Template context: `access` (an `AccessOffer`) replaces `access_choice` everywhere; `access_preset` (a radio value) is optional (Task 4 sets it). `access.html` renders the fieldset only when `access.shown`; one radio per mode with `value` = `default` for `access.default` else the mode, `data-mode` = the mode, label `t("access." ~ mode)` plus `t("access.default")` on the default; checked: `chosen["access"]` if it is one of the radio values, else `access_preset`, else `default`. Hints: `hint.access.enforce`, `hint.access.report`; for `ignore` the existing variant hints `hint.access.maximum*` when `access.unblock`, else `hint.access.ignore`. `mode.html` shows `mode.fast_with_maximum` when `access.unblock` and `"ignore" in access.modes`.
- Texts (en / de), new: `access.enforce` "Respect the site's rules" / "Regeln der Website einhalten"; `access.report` "Check and report only" / "Nur prüfen und melden"; `access.ignore` "Checks off (contract partners)" / "Prüfung aus (Vertragspartner)"; `access.default` " (default)" / " (Standard)"; `hint.access.enforce` "A page that robots.txt forbids, or whose TDM rights are reserved, is not extracted." / "Eine Seite, die robots.txt verbietet oder deren TDM-Rechte vorbehalten sind, wird nicht extrahiert."; `hint.access.report` "The checks run but block nothing: the result reports what they found." / "Die Prüfung läuft, sperrt aber nichts: Das Ergebnis meldet, was sie gefunden hat."; `hint.access.ignore` "For sites that allowed extraction by contract: sends {value}, so the checks are off. Pages load as usual; the outbound limits and a host's pause still apply." / "Für Websites, die die Extraktion vertraglich erlaubt haben: sendet {value}, die Prüfung ist also aus. Die Seiten laden wie sonst; die Limits pro Host und die Pause eines Hosts gelten weiter.". Removed: `access.respect`, `access.maximum`, `hint.access.respect`, `hint.access.off`. The `maximum*` hints keep their keys and texts.

- [ ] **Step 1: Write the failing tests** in `tests/ui/test_access.py` (keep the existing structure and helpers; replace the respect/maximum tests):
  - `test_access_offer_by_role_and_settings` — admin → `("enforce","report","ignore")`; user → `("enforce","report")`; user with `compliance_allow_override=False` → `("enforce",)` and `shown` False; user with `compliance_default="ignore"` → contains `ignore`; `unblock` True only for an admin with both switches.
  - `test_each_step_sends_its_compliance` — `build_body("extract", {"url": PAGE, "access": m})["compliance"] == m` for each mode; `"default"` and `"maximum"` send no `compliance`.
  - `test_switch_and_options_compliance` — `access=report` plus Options `{"compliance":"enforce"}` raises `OptionsError` ("compliance is given twice."); `access=default` plus the same Options sends `enforce`.
  - `test_choice_offered_by_role` — over `WITH_CHOICE`: a user's form shows two radios (`default` checked with data-mode `enforce`, `report`); an admin's three, also with `browser_unblock_enabled` off (no disabled fieldset any more); a user under `compliance_allow_override=False` sees no fieldset.
  - `test_ignore_hint_depends_on_unblock` — admin with `BOTH_ON`: the `ignore` radio is described by the variant's `hint.access.maximum*` text; without unblock by `hint.access.ignore`.
  - Keep and adapt: forged `ignore` from a user is refused by REST (403) and starts no job; the batch, tree and site forms carry the choice; the batch-file preview offers it by role; the language test.
  - In `tests/ui/test_done_when.py`, the admin's "Site rules" step posts `access=ignore` instead of `maximum`.
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_access.py tests/ui/test_done_when.py -q` — FAIL.
- [ ] **Step 3: Implement** forms, templates, routes/job_views contexts, both locales; update the `forms.py` docstring paragraph on "Site rules".
- [ ] **Step 4: Run** `uv run pytest tests/ui -q -m "not browser"` and `uv run python scripts/check_translation_pairs.py` — PASS; ruff, mypy clean.
- [ ] **Step 5: Commit** `feat(ui): offer the site rules as three steps by role in every form`.

### Task 4: The tree view: marks, preset and selection

**Files:**
- Modify: `src/text_extraction/ui/job_views.py` (`site_tree_view`), `ui/templates/tree.html`, `ui/static/ui.js`, `ui/static/ui.css`, `ui/locales/en.json`, `ui/locales/de.json`
- Test: `tests/ui/test_jobs_views.py` (tree tests with `fake_reads`), new `tests/ui/test_tree_rules_browser.py` (`pytest.mark.browser`, set up like `tests/ui/test_playground_browser.py`), `tests/ui/test_a11y_browser.py` (its site tree state)

**Interfaces:**
- Consumes: `Pipeline.origin_rules` (Task 1, via `request.app.state.pipeline`), `leaf_origins`, `mark_blocked`, `SiteTree.crawl_mode` (Task 2), `access_offer`, `access_value` (Task 3).
- Produces: `TREE_RULES_MAX_ORIGINS: Final = 20`, `TREE_RULES_SECONDS: Final = 12` and `async def _tree_rules(pipeline: Pipeline, tree: SiteTree) -> SiteTree` in `job_views.py`: takes the first `TREE_RULES_MAX_ORIGINS` of `leaf_origins(tree)`, loads their rules concurrently (`asyncio.wait` with `TREE_RULES_SECONDS`; pending loads are cancelled; an exception or a timeout counts as None), and returns `await asyncio.to_thread(mark_blocked, tree, rules)`.
- View context: `access` = `access_offer(role, settings)`, `access_preset` = `access_value(access, tree.crawl_mode)`, `tree_mode` = the mode of that preset (`tree.crawl_mode` if it is in `access.modes`, else `access.default`).
- `tree.html`: the form gets `data-access-mode="{{ tree_mode }}"`; a leaf with `node.blocked` gets on its checkbox `data-blocked="<reason>"` and `disabled` while `tree_mode == "enforce"`, and inside its label after the type badge `<span class="blocked-badge" data-blocked="<reason>" title="<reason text>">` with the short text and a `.visually-hidden` ": <reason text>"; `data-failed` is set only for a failed page without a block reason (a refusal by the rules is not a failure of the page: under `report` or `ignore` "Select content pages" includes it). Above the tree, after the counts: `tree.blocked` (plural forms) when `tree.blocked`, and `tree.unchecked` naming `tree.unchecked` origins (in `code()`) when any.
- Texts (en / de): `tree.blocked_badge.robots` "robots.txt" / "robots.txt"; `tree.blocked_badge.robots_unavailable` "robots.txt" / "robots.txt"; `tree.blocked_badge.tdm` "TDM" / "TDM"; `tree.blocked_reason.robots` "robots.txt disallows this URL" / "robots.txt verbietet diese URL"; `tree.blocked_reason.robots_unavailable` "robots.txt could not be retrieved; while the rules are respected, the URL is blocked" / "robots.txt war nicht abrufbar; solange die Regeln gelten, ist die URL gesperrt"; `tree.blocked_reason.tdm` "The site reserves text and data mining (TDM opt-out)" / "Die Website behält sich Text- und Data-Mining vor (TDM-Vorbehalt)"; `tree.blocked.one`/`.other` "{count} URL blocked by the site's rules: it can be selected only with “Check and report only” or “Checks off”." / "{count} URL durch die Regeln der Website gesperrt: auswählbar nur mit „Nur prüfen und melden“ oder „Prüfung aus“." (plural: "{count} URLs … they can …" / "{count} URLs … gesperrt: auswählbar …"); `tree.unchecked` "The rules of {origins} could not be read; their URLs carry no mark." / "Die Regeln von {origins} konnten nicht gelesen werden; ihre URLs sind nicht markiert.".
- `ui.js`: a tree form's mode is the `data-mode` of its checked `input[name=access]`, else its `data-access-mode`. On load of a tree fragment and on every change of its `access` radios: each `input[data-blocked]` is disabled (and unchecked) when the mode is `enforce`, enabled otherwise; then every section settles and the counter updates. "Select all", "Select content pages" and ticking a section skip disabled boxes; `settle` counts enabled boxes only (a section with no enabled box below it is unchecked and not indeterminate).
- `ui.css`: `.blocked-badge` styled like `.page-type` with the palette's warning pair (contrast checked by `tests/ui/test_palette.py` if it lists badge pairs; add the pair there if it does); a disabled box's label dimmed.

- [ ] **Step 1: Write the failing tests** in `tests/ui/test_jobs_views.py` (the client's `FakeFetcher` answers `https://<origin>/robots.txt` with `User-agent: *\nDisallow: /sammlung/_page`, everything else 404):
  - `test_tree_marks_robots_disallowed_urls` (issue #26 done-when, server side) — a crawl under `enforce` with a page `/sammlung/`, a linked-only `/sammlung/_page2` and a sitemap entry `/sammlung/_page3`: both are marked (`data-blocked="robots"`, `disabled`, badge text "robots.txt", reason in the label's text), `/sammlung/` is not; the line "2 URLs blocked by the site's rules" is shown.
  - `test_tree_batch_is_preset_with_the_crawl_mode` — admin, items with `compliance.mode == "ignore"`: the `ignore` radio is checked, the form's `data-access-mode` is `ignore`, blocked boxes are not disabled.
  - `test_tree_report_default_presets_enforce_crawl` (Review Focus 4) — `compliance_default="report"`, crawl under `enforce`: the `enforce` radio (value `enforce`) is checked and blocked boxes are disabled.
  - `test_tree_without_choice_keeps_blocked_disabled` (Review Focus 3) — a user under `compliance_allow_override=False`: no `access` radios, `data-access-mode="enforce"`, blocked boxes disabled.
  - `test_tree_robots_unavailable_marks_every_url` (Review Focus 1) — robots.txt answers 503: every leaf of the origin has `data-blocked="robots_unavailable"`.
  - `test_tree_rules_not_read_are_named` (Review Focus 2) — `app.state.pipeline.origin_rules` patched to answer None: 200, the `tree.unchecked` line names the origin, no `data-blocked`.
  - `test_tree_tdm_reservation_is_marked` — tdmrep.json reserving `/tdm/*`: a sitemap leaf `/tdm/a` has `data-blocked="tdm"`.
  - In `tests/ui/test_tree_rules_browser.py` (issue #26 done-when, in the browser), with the same robots.txt and a tree from a real site job or `fake_reads`: ticking the section `sammlung/` under "Respect the site's rules" selects only the allowed URL ("1 selected"); the blocked box cannot be ticked; after choosing "Check and report only" the blocked box can be ticked by hand and "Select all" includes it; switching back to "Respect the site's rules" unticks and disables it again and the counter follows.
  - In `tests/ui/test_a11y_browser.py`: the site tree state contains one blocked URL (its sample site gets a robots.txt disallowing one crawled-page link), and axe stays clean.
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_jobs_views.py -q` and `uv run pytest -m browser tests/ui/test_tree_rules_browser.py -q` — FAIL.
- [ ] **Step 3: Implement** view, template, JS, CSS, texts; extend the `job_views.py` docstring of `site_tree_view` (rules loaded per origin, limits).
- [ ] **Step 4: Run** `uv run pytest tests/ui -q -m "not browser"`, `uv run pytest -m browser tests/ui -q`, `uv run python scripts/check_translation_pairs.py` — PASS; ruff, mypy clean.
- [ ] **Step 5: Commit** `feat(ui): mark blocked URLs in the site tree and follow the site rules switch`.

### Task 5: Docs, changelog and package entry

**Files:**
- Modify: `docs/user-guide/en/site-rules.md` + `de/site-rules.md`, `docs/user-guide/en/part-of-a-site.md` + `de/`, `docs/user-guide/en/whole-site-and-metadata.md` + `de/` (tree step), `README.md` + `README.de.md` (the "Maximum extraction" bullet: the three steps), `CHANGELOG.md` (`[Unreleased]`: Added, Changed), `docs/packages.md` (package 16 row in the contents table), `docs/architecture.md` (one paragraph where the site tree is described: rules judged at view time, D1 a)
- Test: `tests/test_docs_i18n.py`, `scripts/check_translation_pairs.py`

- [ ] **Step 1:** `site-rules.md`: a section "Switch the checks per run" (de: "Prüfung pro Lauf an- und abschalten"): `compliance` per request, per batch (one value for all its URLs) and per site run, through `options` in MCP, and the playground's three steps with who sees which (table as in Task 3); "Checks off" works without unblock mode, and with unblock mode it is maximum extraction. Replace the playground text that says the choice needs unblock mode.
- [ ] **Step 2:** `part-of-a-site.md` and `whole-site-and-metadata.md`: the tree's badges and reasons, that the selection buttons skip blocked URLs while the rules are respected, that the batch below the tree starts with the crawl's step, and that the marks show the site's rules at the time the tree is shown.
- [ ] **Step 3:** README pair, CHANGELOG `[Unreleased]` (Added: tree marks, closes #26; Changed: three-step "Site rules" choice, also for users and without unblock mode), `docs/packages.md` contents row `| 16 | Robots marks in the site tree, per-run site rules switch | issue #26; three-step "Site rules" choice by role; tree batch preset with the crawl's mode | the UI test of issue #26 (a disallowed URL is marked, a section select skips it, a tick is possible only under report or ignore) and the browser test pass; the check on Jan's PC passes |`, architecture paragraph.
- [ ] **Step 4: Run** `uv run pytest tests/test_docs_i18n.py tests/test_configuration_doc.py tests/test_handover_doc.py -q` and `uv run python scripts/check_translation_pairs.py` — PASS.
- [ ] **Step 5: Commit** `docs: site rules per run and robots marks in the site tree`.
