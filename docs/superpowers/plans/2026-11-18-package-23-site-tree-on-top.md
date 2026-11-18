# Package 23: Site Tree on Top Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After "Read site structure" the playground shows the live site tree at once, with the job's status in the tree's head, instead of a job card with the tree below it.

**Architecture:** `POST /ui/run/site-structure` answers a small "site run" view whose live wrapper loads `GET /ui/run/site-tree?id=…&live=1` at once; the tree view reads the job's state and progress (it already reads every page of the job) and shows them in its head with a cancel button while the job runs. The per-fetch list (robots.txt, sitemaps, pages with "Show") moves into a fold under the tree that loads `GET /ui/run/progress?id=…&items=1` only when opened and is kept across tree refreshes (`hx-preserve`). One poll (the tree, every 4 s) replaces two (job card every 3 s, tree every 4 s).

**Tech Stack:** FastAPI, Jinja2, htmx 2, plain `ui.js`, pytest, Playwright (browser tests).

**Spec:** Jan's request of 2026-11-18 (thread "Seitenbaum live oben anzeigen") and the German plan `/mnt/project-files/package-23/plan-paket-23.md` (mockups next to it); decision D1 (per-fetch list folded under the tree, recommended; or removed).

## Global Constraints

- English in code and developer docs; UI texts in `ui/locales/en.json` and `de.json`, user guide en/de changed together (`tests/test_docs_i18n.py`, `tests/ui/test_i18n.py`, `scripts/check_translation_pairs.py`).
- CSP: no inline `style` attributes or inline scripts; layout through classes and `data-*` only.
- A refresh announces nothing except a changed job state (ruling P12-S9); htmx keeps the focus on an element whose id survives a swap (P12-S8).
- Without `ui.js` the tree still loads once (the wrapper's `hx-trigger="load"` is htmx) and every control works; without htmx the site run shows the job id and a link-free note.
- The REST API and MCP do not change.

## Review Focus

- A job cancelled while the tree refreshes: the cancel answer replaces the whole site run, a refresh already in flight must not bring the tree back (the wrapper is gone, htmx drops the swap).
- A crawl whose start page fails: the tree has no roots and the job is completed; the head says so ("No pages found") and the fetch fold shows the failed fetch with its error.
- The fold open while the tree refreshes: it stays open with its content (hx-preserve by id) and does not double its polling.
- A focused cancel button across a refresh keeps the focus (same id).
- The state change running → completed is announced once; tree refreshes stay silent.

---

### Task 1: The tree's head carries the job's status

**Files:**
- Modify: `src/text_extraction/ui/job_views.py` (`_TreeRead`, `_read_tree_page`, `site_tree_view`)
- Modify: `src/text_extraction/ui/templates/tree.html`
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json`
- Test: `tests/ui/test_tree_live.py`, `tests/ui/test_jobs_views.py` (`fake_reads` gains `progress=`)

**Interfaces:**
- Produces: `_TreeRead` fields `done`, `total`, `failed: int`, `elapsed: Message | None`; template context keys `job_state`, `progress` (`done`, `total`, `failed`), `elapsed`, `cancel` (bool: state != completed).
- Tree markup: `section.tree-view[data-announce]` (the state text `status.job`), `.tree-head-line` with `h3.tree-title`, `span.state[data-value=<state>]`, `form.cancel` (`button#tree-<job>-cancel`, `hx-post=/ui/run/cancel`, `hx-target="closest .site-run"`, `hx-swap=innerHTML`, `hx-confirm`), `p.tree-progress` (running: `tree.progress_running` "{done} of {total} fetches done so far · running for {elapsed}", completed: existing `tree.duration`), failed fetches `tree.fetches_failed` when > 0, `p.tree-starting` (`tree.starting` "The crawl is starting …") when no roots and running, `p.tree-empty` (`tree.empty` "The crawl found no pages. The fetches below say why.") when no roots and completed.

- [ ] Tests: running tree has state `running`, cancel button with the id and target above, progress text with done/total/elapsed, `data-announce` "Job … : running."; completed tree has no cancel, has duration; zero items running shows `tree.starting`; zero items completed shows `tree.empty`; German texts present.
- [ ] Run, see them fail; implement; run, pass; commit.

### Task 2: The site-structure form opens the site run

**Files:**
- Create: `src/text_extraction/ui/templates/site_run.html`
- Modify: `src/text_extraction/ui/job_views.py` (`site_structure_view`)
- Test: `tests/ui/test_jobs_views.py::test_site_structure_form_starts_a_job`

**Interfaces:**
- `site_run.html`: `section.site-run.jobs-view[tabindex=-1][aria-label=tree.label]` holding `div.tree-live-wrapper[data-tree-live=4][data-refresh][hx-get=/ui/run/site-tree?id=<id>&live=1][hx-trigger="load"][hx-swap=outerHTML]` around a `section.tree-view` with the title, state `queued` and `tree.starting`.
- [ ] Test: the 202 answer renders `.site-run` with the wrapper's `hx-get` and no `section.job`; a 422 still renders the REST answer. Fail, implement, pass, commit.

### Task 3: The per-fetch fold

**Files:**
- Modify: `src/text_extraction/ui/job_views.py` (`progress_view`: `items=1`), `templates/progress_live.html`, `templates/progress.html`, `templates/tree.html`
- Test: `tests/ui/test_jobs_views.py`

**Interfaces:**
- `GET /ui/run/progress?id=<id>&items=1[&live=1]`: the job's items table, pager and `.job-detail` only (no heading, bar, counts, tree button or cancel); refresh and pager URLs keep `items=1`.
- tree.html: `details.fold.tree-fetches#tree-<job>-fetches[hx-preserve]` after the tree box (also when there are no roots), summary `tree.fetches` ("Single fetches"), body `div[hx-get=/ui/run/progress?id=<id>&items=1][hx-trigger="toggle once from:closest details"]`.
- [ ] Tests: `items=1` answers no `h3`/`progress`/cancel and keeps `items=1` in `hx-get` of the refresh and the pager; the tree holds the fold with the id and trigger. Fail, implement, pass, commit.

### Task 4: Browser behaviour, styles, harness

**Files:**
- Modify: `src/text_extraction/ui/static/ui.css` (head line: title, state, cancel right; wraps at 390 px), `ui.js` only if a browser test shows a need (focus, announce)
- Modify: every browser test and `tests/ui/screenshots.py` that submitted the site form and clicked `button.tree-button` (they now wait for `#result .tree-view .state[data-value=completed]`)
- Test: `tests/ui/test_tree_live_browser.py` new cases: the tree appears without a click and grows; the state change is announced once in `#ui-status`; the cancel button removes the tree and shows the cancelled note; the fold opens, lists the fetches, "Show" shows an item, and stays open across a refresh.
- [ ] Fail, implement, pass, commit.

### Task 5: Docs and records

**Files:** `docs/user-guide/{en,de}/whole-site-and-metadata.md`, `usage.md`, `part-of-a-site.md`; `CHANGELOG.md` (Unreleased, Changed); `docs/packages.md` (row 23 in both tables).
- [ ] Texts describe the tree on top, status in its head, the fold; no "Show tree now" for the site form. Docs tests and translation check pass; commit.
