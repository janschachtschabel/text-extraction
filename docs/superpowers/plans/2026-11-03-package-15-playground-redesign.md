# Package 15: Playground Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The playground at `/ui` in the look of Jan's Kompendium service: a white header with the
name, the session line and the tabs; the settings in a left column and the result in a right
column (stacked on narrow screens); a status line, a toolbar, a card of figures, collapsible
cards and the text in a white reading card with a serif font. Same features, same forms, same
endpoints.

**Architecture:** A visual and structural redesign of the Jinja templates and `ui.css`, with small
additions to `ui.js` (open a closed section before focusing a field; the "Copy" button of the
result toolbar, P15-R1). Every colour, radius
and font stack becomes a token on `:root`, and a unit test holds the tokens to the contrast
ratios. Classes that tests pin with an exact `@class='…'` match keep their class attribute;
layout comes from new wrapper elements and new classes. A Playwright helper captures the main
states in both languages at desktop and phone width, before (on `main`) and after.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, htmx 2.0.11 (vendored), plain CSS, Playwright
(Chromium), axe-core 4.14.0 (vendored in `tests/ui/vendor`), pytest, lxml.

**Spec:** Jan's approved outline (German), `/mnt/project-files/package-14/plan-paket-14.md`,
section "Paket 15: Neugestaltung der Spielwiese (D6)" and decision D6 (approved 2026-11-02 with
D1-D6 = a), and the approved mockup `/mnt/project-files/package-14/mockup-paket-15.html`
(static HTML, German). The row of package 15 in `docs/packages.md` gives contents and the
done-when criterion; the UI section of `docs/architecture.md` ("MCP and UI", "The UI",
"Accessibility, help and status messages") holds the rules that stay.

## Rulings

Rulings of the coordinator on this plan (2026-11-03); they override anything below that
differs. Task 8 records them in `docs/architecture.md`.

- **P15-R1, toolbar buttons.** Jan's approved outline lists the toolbar "Markdown kopieren,
  Markdown speichern, Antwort speichern", so all three are in scope. "Copy Markdown" (or "Copy
  text" for text output) copies the shown result text to the clipboard with
  `navigator.clipboard` in `ui.js` and confirms it in the polite `#ui-status`; it is rendered
  with `hidden` and revealed by `ui.js` (so it is absent without JavaScript), with no inline
  handler. "Save" is the existing download (`a.download`, a server-built `data:` link).
  "Save response" downloads the whole JSON envelope the same way (a server-built `data:` link),
  so both downloads work without JavaScript and need no CSP change. Built in Task 5.
- **P15-R2, screenshot hosting.** This session may push only to `claude/project-thread-eexf9n`,
  so there is no assets branch. The before and after screenshots are written to
  `/mnt/project-files/package-15/screenshots/before/` and `…/after/` (outside the repository)
  and shown to Jan there; the PR body says where they are. The helper takes the output
  directory as an argument (Tasks 1 and 8).
- **P15-R3, field borders.** Inputs, selects, textareas, unchecked segment options and outlined
  buttons get `--field-line: #808993` (3.22:1 on the page grey `#f3f4f6`, 3.55:1 on white), not the mockup's `#c9cfd6` (1.57:1).
  Reason: field boundaries must meet WCAG 1.4.11 (3:1), also on the grey page (the settings column and the result toolbar sit on `--page`, not on a card). Card borders stay `#dde1e6`
  (decorative: a card's text identifies it).
- **P15-R4, three header rows.** The masthead (`h1`, intro) is outside the `#app` that the
  login replaces (R-P12-9), the session line inside it, so the white header has three rows: the
  name with the intro; the session line (signed in, "Log out", language), right-aligned; the
  tabs. Reason: moving the `h1` into `#app` would change the swap, the skip link's target and
  the focus rules of package 12 for a visual gain only.
- **P15-R5, no "Verbatim from the page" pill.** Reason: it is not true of every source (OCR,
  transcripts, chunks, LLM output).
- **P15-R6, existing section titles.** The small capital section labels are the existing legends
  ("Format", "Also", "Site rules", "Custom capture"). Reason: "Source", "Output" and "Options"
  would be new texts, and "Options" would clash with "Options (JSON)" and the user guide's
  names.
- **P15-R7, "Save response (JSON)" size.** It uses the same `DOWNLOAD_MAX_CHARS` cap as the
  content download; above it the toolbar shows a hint that points to curl. Checked: Chromium
  downloads `data:` JSON links of 1, 3, 12 and 30 MB, so there is no 2 MB `data:` URL limit for
  downloads; the cost is page weight only.
- **P15-R8, scrollable blocks and tables.** The result's scrollable `pre` blocks carry
  `tabindex="0"` (axe `scrollable-region-focusable`, WCAG 2.1.1); this adds at most three tab stops
  per result (the Markdown source, the curl command and the raw JSON, all in sections that are closed
  by default), even when a block is short. The chunks (`ol.chunks pre`) never scroll (no `max-height`,
  the text wraps), so they take no tab stop. Below 600 px tables scroll inside their card; the items
  table's URL column keeps at least 12rem, so the "Show" column may need a sideways scroll of the
  table.

## Global Constraints

- CLAUDE.md rules hold: English code, comments and developer docs; any new or changed UI text in
  `ui/locales/en.json` **and** `de.json` in the same commit; user docs in `docs/user-guide/en/`
  with their German partner in `de/` in the same commit (`tests/test_docs_i18n.py`,
  `tests/ui/test_i18n.py`, `scripts/check_translation_pairs.py`).
- No new feature, form field, endpoint, setting or htmx behaviour, apart from the toolbar
  buttons of P15-R1. Every existing field, its
  `id`, `name`, label, hint and `aria-describedby` stays; every `hx-*` attribute stays; the
  forms keep working without JavaScript.
- CSP unchanged (`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'
  data:; …`): no `style` attribute or element, no inline script, no `hx-on` in any template
  (`tests/ui/test_shell.py::test_no_inline_script_or_style_or_hx_on`). The mockup's inline
  styles become classes in `src/text_extraction/ui/static/ui.css`.
- The templates hold no literal text (`tests/ui/test_templates_i18n.py`); every catalogue key is
  used, so a key no template uses any more is deleted from both catalogues.
- D6 (a): system fonts only, no font file shipped. UI: `system-ui, -apple-system, "Segoe UI",
  Roboto, sans-serif`. Reading text: `Charter, "Bitstream Charter", "Sitka Text", Cambria,
  Georgia, serif`.
- Mockup values: page `#f3f4f6`; cards `#fff` with a 1 px `#dde1e6` border and 12 px radius;
  accent (petrol) `#1d6f6a`, hover `#155a56`, tint `#e6f2f0`; text `#1f2328`; muted `#59636e`;
  fields 8 px radius, 44 px high; section labels 12 px, weight 700, letter-spacing `.09em`,
  uppercase, muted; pill badges (999 px radius, 26 px high, 13 px, weight 700): success petrol
  on tint, partial amber, failed red; tabs 44 px with a 3 px petrol underline on the selected
  one; settings column 340-380 px; reading text 19 px, line-height 1.75, at most `70ch`, with a
  3 px petrol rule on the left; main button 48 px, petrol, white, weight 700, full width.
- Accessibility (WCAG 2.2 A and AA, architecture "Accessibility, help and status messages"):
  axe-core finds no violation in any state in either language (no rule or element excluded);
  the WAI-ARIA tabs keep their roving tabindex and keys; `:focus-visible` draws a 3 px outline
  in `--focus` at least 3:1 against `--page` and `--paper`; text at least 4.5:1; form field
  boundaries at least 3:1 (WCAG 1.4.11); `prefers-reduced-motion` turns transitions and
  animations off.
- Test-pinned markup (keep the class attribute exactly): `#app`, `#app-shell`, `div.who`,
  `div[role=tablist]` with `button[role=tab]#tab-<name>`, `section.panel#panel-<name>` with
  `./h2` and `./p[@class='intro']/a[@class='help']`, `#result`, `span.http-status`,
  `span.error-code`, `h3.title` (the only element with class exactly `title` in a result),
  `pre.content`, `a.download`, `div.markdown`, `details.raw > pre`, `pre.curl`,
  `table.values`, `table.images`, `ol.timeline`, `ol.chunks`, `form.previews`,
  `figure.preview`, `img.screenshot`, `dl.facts` (its terms and values are read pairwise),
  `dl.meta`, `dl.info`, `section.job`, `div.progress-live`,
  `table.items`, `span.done|success|failed`, `p.elapsed`, `form.cancel`, `p.cancelled`,
  `ul.tree`, `p.tree-types`, `p.tree-counts`, `span.tree-counter`, `form.tree-form`,
  `ul.problems`, `ol.first-urls`, `dd.<count>` of the batch preview.

### Rulings in this plan (P15-S*)

Decisions the mockup and the outline leave open, besides the rulings above; each is recorded
in `docs/architecture.md` (Task 8). (S1, S2, S6 and S12 of the first draft became P15-R3, R4,
R6 and R2.)

- **P15-S3, status line.** `header.answer` is the status line: the status pill, the unblocked
  pill and `HTTP <n>`. The status stays there, not in the card of figures, because an error
  without `data` has no such card.
- **P15-S4, toolbar.** The toolbar holds the title and the three buttons of P15-R1, outlined.
  "Copy" copies the text shown in `pre.content` (the source; at most `SHOWN_MAX_CHARS`, as the
  cut note says): the CSP's `connect-src 'none'` forbids reading the download's `data:` link
  back. It is revealed only when `window.isSecureContext` and `navigator.clipboard` exist (a
  plain-HTTP host other than localhost has no clipboard API). "Save response" is left out, with
  a hint, when the envelope's JSON is over `DOWNLOAD_MAX_CHARS` characters (a cut JSON file is
  no JSON).
- **P15-S5, reading card.** A Markdown result shows its rendered Markdown in the reading card and
  its source in a closed "Show source" section (`details.source`, the old `result.show_rendered`
  key goes, `result.show_source` comes); a text result, or Markdown too long to render, shows
  `pre.content` in the reading card in the serif font. No "Verbatim from the page" pill
  (P15-R5).
- **P15-S7, collapsible cards.** Metadata, Links, Images and "What happened" are closed
  `details` cards; "What happened" is open when the result has warnings. Structured values and
  Chunks stay open cards (they are the result of their tabs); "As curl" and "Raw JSON" stay
  closed.
- **P15-S8, Advanced.** "Advanced" (`details.advanced`, closed) holds "Wait seconds" and
  "Options (JSON)" in every form that has them; it is open when the form is re-rendered with
  options (`chosen["options"]`). `ui.js` opens a closed ancestor `details` before it focuses a
  field: for a form error (`tieError`) and for the browser's own `invalid` event.
- **P15-S9, figures.** `dl.facts` becomes the card of figures; each term and value pair sits in a
  `div.fact` (HTML allows `div` groups in `dl`). It gains "Page type" (`data.page_type.type`,
  shown with the existing `page_type.<value>` text as a pill) and "Time"
  (`data.timings.total_ms` in seconds, two decimals, decimal comma in German). "Mode used"
  stays one value (no "auto → browser").
- **P15-S10, language switch.** The switch stays two links; in the header its hint
  (`#language-switch-hint`) is visually hidden and still read through `aria-describedby`.
- **P15-S11, cards without class changes.** Where a test pins an exact class (`dl.facts`,
  `dl.info`, `details.raw`, `section.job`, `form.tree-form`, `div.markdown`), the card look
  comes from CSS on that selector; only new wrappers get the class `card`.

## Review Focus

1. A form error on a field inside the closed "Advanced" section (`options` with `{`): the
   section opens, the field gets focus, `aria-invalid` and the error id. Test in Task 4.
2. A browser-side invalid value inside a closed section (`wait_seconds` 99 with
   `max="60"`): the section opens so the browser can show its message on the field. Test in
   Task 4.
3. A phone (390 px) on every tab and every result view: one column, no horizontal page scroll,
   long URLs and `pre` blocks wrap or scroll inside their card. Test in Task 7.
4. Results from before package 14 (no `page_type`), a result without `timings`, an error
   without `data` and a 202 job: the figures card shows only what exists and the status line
   still shows status and HTTP status. Tests in Task 5.
5. "Copy" on a page without the clipboard API (plain HTTP on another host, or the permission
   refused): the button stays hidden, or a refused write says so in `#ui-status` instead of
   claiming success. Test in Task 5.
6. Content hidden in a closed section is never checked by axe: axe must also run with every
   `details` of the result and the forms open, in both languages. Tests in Tasks 4, 5 and 7.

---

### Task 1: Capture the playground's states (before)

**Files:**
- Create: `tests/ui/screenshots.py` (a module, run with `python -m tests.ui.screenshots`)
- Test: `tests/ui/test_screenshots_browser.py` (marker `browser`)

**Interfaces:**
- Consumes (exist on `main`): `tests.ui.test_playground_browser.serving(settings)`,
  `serve_site(httpserver, pages)`, `PAGES`, `tests.ui.test_login_browser._chromium()`,
  `pytest_httpserver.HTTPServer(host="127.0.0.1")` started by hand.
- Produces:
  - `STATES: Final[tuple[str, ...]] = ("login", "extract-form", "extract-result",
    "extract-result-expanded", "extract-text", "form-error", "batch-progress",
    "batch-file-preview", "site-tree", "screenshot-custom", "info")`
  - `WIDTHS: Final = {1440: 900, 390: 844}` (viewport width to height)
  - `def capture(out: Path, languages: Sequence[Language] = LANGUAGES, widths: Sequence[int] =
    tuple(WIDTHS)) -> list[Path]`: one full-page PNG per state, language and width, named
    `f"{n:02d}-{state}-{language}-{width}.png"` (`n` the state's position from 1), in
    `STATES` order; returns the paths written.
  - `def main(argv: Sequence[str] | None = None) -> int`: `--out DIR` (required), `--lang`
    (repeatable, default both), `--width` (repeatable, default both); prints each path.
- Rules: an admin key (so "Site rules" shows, disabled with its hint: no browser), the local
  `PAGES` site, `ExtractWorker` through `serving`. Only stable selectors that exist before and
  after this package: ids (`#login-api-key`, `#tab-<name>`, `#extract-url`, `#extract-options`,
  `#batch-urls`, `#site-url`, `#site-depth`, `#screenshot-preset`), `#result h3.title`,
  `#result section.job .state[data-value=completed]`, `button.tree-button`,
  `#result .tree-view`, `#result .batch-preview`. Fields are set with `page.evaluate` (value
  plus an `input` event) where they may sit in a closed section, and every `details.advanced`
  present is opened for `extract-form`. `extract-result-expanded` opens every `#result details`.

- [ ] **Step 1: Write the failing test**

```python
pytestmark = pytest.mark.browser


def test_capture_writes_every_state(tmp_path: Path) -> None:
    written = capture(tmp_path, languages=["en"], widths=[390])
    assert [path.name for path in written] == [
        f"{n:02d}-{state}-en-390.png" for n, state in enumerate(STATES, start=1)
    ]
    for path in written:
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.width == 390


def test_main_takes_out_lang_and_width(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--out", str(tmp_path), "--lang", "de", "--width", "1440"]) == 0
    printed = capsys.readouterr().out.split()
    assert printed == [str(tmp_path / f"{n:02d}-{s}-de-1440.png") for n, s in enumerate(STATES, 1)]
```

- [ ] **Step 2: Run it, expect FAIL** (`uv run pytest -m browser tests/ui/test_screenshots_browser.py -q`: `ModuleNotFoundError: tests.ui.screenshots`)
- [ ] **Step 3: Implement** `tests/ui/screenshots.py` (`page.set_viewport_size`, `page.screenshot(path=…, full_page=True)`; one Chromium per language; a fresh `serving` per run).
- [ ] **Step 4: Run it, expect PASS**; `uv run ruff check tests/ui/screenshots.py`, `uv run mypy` clean.
- [ ] **Step 5: Capture "before"** from the unchanged UI:

```bash
git worktree add /tmp/te-main origin/main
cp tests/ui/screenshots.py /tmp/te-main/tests/ui/screenshots.py
(cd /tmp/te-main && uv run python -m tests.ui.screenshots \
  --out /mnt/project-files/package-15/screenshots/before)
git worktree remove --force /tmp/te-main
```

Expected: 44 PNGs (11 states x 2 languages x 2 widths) in
`/mnt/project-files/package-15/screenshots/before/`. They are not committed (P15-R2).
- [ ] **Step 6: Commit** `test(ui): capture the playground's states as screenshots`

### Task 2: Design tokens, base styles and the page frame

**Files:**
- Modify: `src/text_extraction/ui/static/ui.css` (`:root`, base elements, `.masthead`, `main`,
  `.login`, buttons, fields, `.card`, `.pill`, `.section-label` styles)
- Modify: `src/text_extraction/ui/templates/index.html` (masthead markup)
- Modify: `src/text_extraction/ui/templates/login.html` (classes only)
- Create: `tests/ui/test_palette.py`
- Test: `tests/ui/test_shell.py` (masthead)

**Interfaces:**
- Produces (later tasks use these names only):
  - Tokens on `:root`: `--page: #f3f4f6`, `--paper: #ffffff`, `--line: #dde1e6`,
    `--field-line: #808993`, `--ink: #1f2328`, `--muted: #59636e`, `--accent: #1d6f6a`,
    `--accent-strong: #155a56`, `--accent-wash: #e6f2f0`, `--accent-ink: #ffffff`,
    `--neutral-wash: #eef0f3`, `--warn: #7a4d00`, `--warn-wash: #fdf3d7`, `--error: #a3222b`,
    `--error-wash: #fde8ea`, `--focus: #1d6f6a`, `--font-ui`, `--font-read`, `--font-code`
    (the stacks of Global Constraints; code: `ui-monospace, SFMono-Regular, Menlo, Consolas,
    monospace`), `--radius-card: 12px`, `--radius-field: 8px`, `--control: 44px`,
    `--gutter: 24px` (16 px below 600 px), `--content-max: 1440px`.
  - Classes: `.card` (paper, 1 px `--line` border, `--radius-card`), `.pill` (+ `.pill-success`,
    `.pill-partial`, `.pill-failed`, `.pill-neutral`), `.section-label` (the label style, also
    applied to `legend`), `.bar-inner` (max-width `--content-max`, side padding `--gutter`,
    centred), `button` (primary: `--accent` on, `--accent-ink` text, hover `--accent-strong`,
    min-height `--control`, radius `--radius-field`), `button.secondary` (outlined: paper,
    `--field-line` border, `--ink` text, min-height 40 px).
  - `index.html`: `<header class="masthead"><div class="bar-inner brand">` with a decorative
    `<svg class="logo" aria-hidden="true" focusable="false" viewBox="0 0 24 24">` (the mockup's
    two paths, no `style`; stroke from CSS), the `h1` and `<p class="masthead-intro">` (the
    existing `page.intro`).
- Contrast pairs the test holds (ratio, WCAG 2.x relative luminance):
  text 4.5:1 for `ink`, `muted`, `accent`, `accent-strong`, `warn`, `error` on their
  backgrounds (`page`, `paper`, `accent-wash`, `neutral-wash`, `warn-wash`, `error-wash` as used)
  and `accent-ink` on `accent` and `accent-strong`; non-text 3:1 for `field-line` on `paper` and `page`,
  `focus` on `page` and `paper`. The ratios of these values: ink/page 14.35, ink/paper 15.80, muted/page
  5.56, muted/paper 6.11, muted/neutral-wash 5.36, accent/paper 5.94, accent/page 5.40,
  accent-ink/accent 5.94, accent-ink/accent-strong 7.99, accent-strong/accent-wash 6.97,
  warn/warn-wash 6.57, error/error-wash 6.35, field-line/paper 3.55, field-line/page 3.22, focus/page 5.40, accent/accent-wash 5.18, muted/accent-wash 5.33.

- [ ] **Step 1: Write the failing tests**

```python
TEXT_PAIRS = [
    ("ink", "page"),
    ("ink", "paper"),
    ("ink", "accent-wash"),
    ("ink", "neutral-wash"),
    ("muted", "page"),
    ("muted", "paper"),
    ("muted", "neutral-wash"),
    ("accent", "page"),
    ("accent", "paper"),
    ("accent-ink", "accent"),
    ("accent-ink", "accent-strong"),
    ("accent-strong", "accent-wash"),
    ("warn", "warn-wash"),
    ("error", "error-wash"),
]
NON_TEXT_PAIRS = [("field-line", "paper"), ("focus", "page"), ("focus", "paper")]


def test_tokens_are_the_mockups_values() -> None:
    found = tokens()  # the --name: #rrggbb pairs of ui.css's :root
    assert {k: found[k] for k in ("page", "paper", "line", "ink", "muted")} == {
        "page": "#f3f4f6",
        "paper": "#ffffff",
        "line": "#dde1e6",
        "ink": "#1f2328",
        "muted": "#59636e",
    }
    assert (found["accent"], found["accent-strong"], found["accent-wash"]) == (
        "#1d6f6a",
        "#155a56",
        "#e6f2f0",
    )


@pytest.mark.parametrize(("fore", "back"), TEXT_PAIRS)
def test_text_contrast(fore: str, back: str) -> None:
    assert contrast(tokens()[fore], tokens()[back]) >= 4.5


@pytest.mark.parametrize(("fore", "back"), NON_TEXT_PAIRS)
def test_non_text_contrast(fore: str, back: str) -> None:
    assert contrast(tokens()[fore], tokens()[back]) >= 3.0


def test_contrast_formula() -> None:
    assert round(contrast("#000000", "#ffffff"), 2) == 21.0
    assert round(contrast("#59636e", "#f3f4f6"), 2) == 5.56


def test_reading_and_ui_fonts_are_system_fonts() -> None:
    css = UI_CSS.read_text(encoding="utf-8")
    assert '--font-read: Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif' in css
    assert "@font-face" not in css and "url(" not in css
```

In `tests/ui/test_shell.py`: the page's `header.masthead` holds one `h1` with `page.title`, an
`svg.logo` with `aria-hidden="true"` and `p.masthead-intro`; the static files still serve
`ui.css` (existing test).
- [ ] **Step 2: Run, expect FAIL** (`uv run pytest tests/ui/test_palette.py tests/ui/test_shell.py -q`)
- [ ] **Step 3: Implement** `tokens() -> dict[str, str]` and `contrast(fore: str, back: str) -> float` in the test module (regex over the `:root` block), the new `:root`, base styles and masthead. Replace every hard-coded colour in `ui.css` with a token (today's `#1d6b35` success green becomes `--accent-strong`).
- [ ] **Step 4: Run** `uv run pytest tests/ui -q`, expect PASS (no markup test changes in this task).
- [ ] **Step 5: Commit** `feat(ui): design tokens, base styles and the masthead`

### Task 3: Header bar, tabs and the two columns

**Files:**
- Modify: `src/text_extraction/ui/templates/app.html`
- Modify: `src/text_extraction/ui/static/ui.css`
- Create: `tests/ui/test_layout_browser.py` (marker `browser`)
- Test: `tests/ui/test_a11y_markup.py`, `tests/ui/test_done_when.py` (unchanged pins must hold)

**Interfaces:**
- Consumes: tokens and classes of Task 2.
- Produces, `app.html` structure (ids, roles and the order of focusable elements unchanged):

```html
<section id="app-shell" class="shell">
  <div class="app-bar"><div class="bar-inner">
    banner (auth none) · div.who · details.keys (auth none) · div.tabs[role=tablist]
  </div></div>
  <div class="workspace bar-inner">
    <div class="settings"> section.panel × 9 </div>
    <section id="result" class="result"></section>
  </div>
</section>
```

  - `.app-bar`: paper, 1 px `--line` bottom border, full bleed (so `main` loses its max-width
    and margin; `.login` keeps a centred 30 rem card).
  - `.who`: right-aligned row, 14 px, muted; `button.secondary` 36 px; `.language-switch` links;
    `.who .language-switch .hint` gets the `.visually-hidden` rules in CSS (P15-S10; the
    template is unchanged, the login box keeps the hint visible).
  - `.tabs [role=tab]`: 44 px, no box, muted text, `border-bottom: 3px solid transparent`;
    selected: `--accent` underline, `--ink`, weight 600; the tab list's bottom edge sits on the
    bar's border (`margin-bottom: -1px`). Tabs wrap on narrow screens.
  - `.workspace`: grid `minmax(300px, 380px) minmax(0, 1fr)`, gap 32 px, padding-top 24 px;
    below `60rem` one column (`minmax(0, 1fr)`), result below settings.
  - `.settings .intro`: 14 px muted, above the form.

- [ ] **Step 1: Write the failing tests**

Markup (`tests/ui/test_a11y_markup.py`, new test):

```python
def test_workspace_holds_the_panels_then_the_result(admin: TestClient) -> None:
    page = _page(admin, "en")
    (workspace,) = page.xpath("//div[contains(@class, 'workspace')]")
    (settings,) = workspace.xpath("./div[@class='settings']")
    assert [p.get("id") for p in settings.xpath("./section")] == [f"panel-{t.name}" for t in TABS]
    assert workspace.xpath("./section[@id='result']")
    (bar,) = page.xpath("//div[@class='app-bar']")
    assert bar.xpath(".//div[@class='who']") and bar.xpath(".//*[@role='tablist']")
```

Browser (`tests/ui/test_layout_browser.py`; reuse `serving`, `serve_site`, `browsing`,
`log_in` of `test_playground_browser`):

```python
def test_two_columns_on_a_wide_screen(ui: tuple[str, str]) -> None:
    # 1440 x 900, extract /docs/a.html, then:
    settings, result = boxes(page, ".settings", "#result")
    assert settings["right"] <= result["left"]
    assert abs(settings["top"] - result["top"]) <= 1
    assert 300 <= settings["width"] <= 380


def test_one_column_on_a_phone(ui: tuple[str, str]) -> None:
    # 390 x 844, extract /docs/a.html, then:
    settings, result = boxes(page, ".settings", "#result")
    assert result["top"] >= settings["bottom"]
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_selected_tab_has_the_petrol_underline(ui: tuple[str, str]) -> None:
    style = computed(page, "#tab-extract", "borderBottomColor", "borderBottomWidth")
    assert style == ["rgb(29, 111, 106)", "3px"]
```

`boxes(page, *selectors) -> list[dict[str, float]]` (`getBoundingClientRect`) and
`computed(page, selector, *properties) -> list[str]` are helpers in the test module.
- [ ] **Step 2: Run, expect FAIL** (`uv run pytest tests/ui/test_a11y_markup.py -q` and `uv run pytest -m browser tests/ui/test_layout_browser.py -q`)
- [ ] **Step 3: Implement** the markup and CSS.
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`, expect PASS; in particular `test_tabs_follow_the_aria_keyboard_pattern` (Tab from the tab list lands on `#panel-extract`), `test_login_moves_focus_into_the_app`, `test_focus_is_visible` and both axe tests.
- [ ] **Step 5: Commit** `feat(ui): header bar, tabs and two columns`

### Task 4: The settings column

**Files:**
- Create: `src/text_extraction/ui/templates/forms/advanced.html`
- Modify: `src/text_extraction/ui/templates/forms/common.html`, `forms/batch_options.html`,
  `forms/site_structure.html` (options inside Advanced), `forms/format.html` (segmented
  control), `forms/extract.html`, `forms/file.html`, `forms/access.html`,
  `forms/screenshot.html`, `forms/chunk.html`, `forms/structured.html`, `forms/batch.html`,
  `forms/batch_file.html`, `forms/info.html` (wrapper classes only where needed)
- Modify: `src/text_extraction/ui/static/ui.js` (`revealField`, an `invalid` listener)
- Modify: `src/text_extraction/ui/static/ui.css`
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json` (`form.advanced`,
  `hint.advanced`)
- Test: `tests/ui/test_a11y_markup.py`, `tests/ui/test_operations.py`,
  `tests/ui/test_a11y_browser.py`, `tests/ui/test_layout_browser.py`

**Interfaces:**
- Consumes: Task 2 classes.
- Produces:
  - `forms/advanced.html`: `<details class="advanced"{% if chosen and chosen["options"] %}
    open{% endif %}><summary>{{ t("form.advanced") }}</summary><p class="hint">{{
    t("hint.advanced") }}</p>` + the wait field when `with_wait` + `forms/options.html` +
    `</details>`. `common.html` includes it in `{% with with_wait=true %}`;
    `batch_options.html` and `site_structure.html` in `{% with with_wait=false %}` (they have no
    wait field today). The field markup moves unchanged (ids, hints, `aria-describedby`).
  - Catalogue texts:
    - `form.advanced`: en "Advanced", de "Erweitert"
    - `hint.advanced`: en "How long to wait before the request continues as a job, and any
      further field of the REST body as JSON (for example actions, cookies, headers or
      cache).", de "Wie lange gewartet wird, bevor die Anfrage als Job weiterläuft, und jedes
      weitere Feld des REST-Bodys als JSON (etwa Aktionen, Cookies, Header oder Cache)."
  - `forms/format.html`: the two radio labels inside `<div class="segmented">` (each label a
    44 px box, `--field-line` border; the checked one via `:has(input:checked)`: 2 px
    `--accent` border, `--accent-wash`, weight 600). The radio stays visible.
  - `legend` and `.section-label`: 12 px, 700, `.09em`, uppercase, `--muted`; `fieldset.choice`
    loses its box (no border, no padding), sections 22 px apart.
  - Fields: 44 px (textarea auto), 15 px, `--radius-field`, `--field-line`; `label` 15 px, 600;
    `.hint` 13 px, line-height 1.45, muted; checkboxes and radios 18 px with
    `accent-color: var(--accent)`.
  - `form.op > button[type=submit]`, `.tree-actions > button[type=submit]`,
    `#login-form button[type=submit]`: width 100 %, min-height 48 px, weight 700.
  - `ui.js`: `function revealField(control)` sets `open` on every closed `details` ancestor of
    `control`; `tieError` calls it before `control.focus()`; a capture-phase `invalid` listener
    on `document` calls it for the event's target.

- [ ] **Step 1: Write the failing tests**

Markup (`tests/ui/test_a11y_markup.py`):

```python
@pytest.mark.parametrize("language", LANGUAGES)
def test_wait_and_options_are_in_a_closed_advanced_section(
    admin: TestClient, language: Language
) -> None:
    page = _page(admin, language)
    for field in page.xpath("//textarea[@name='options'] | //input[@name='wait_seconds']"):
        (section,) = field.xpath("ancestor::details")
        assert "advanced" in section.get("class")
        assert section.get("open") is None
        summary = section.xpath("./summary")[0].text_content().strip()
        assert summary == translate(language, "form.advanced")
```

In `tests/ui/test_jobs_views.py` (a list with an invalid line re-renders the preview with the
fields the user chose):

```python
def test_advanced_is_open_when_the_preview_keeps_options(client: TestClient) -> None:
    html = run(client, "batch-urls", urls=f"{PAGE}\nnot a url", options='{"links": true}').text
    (section,) = tree(html).xpath("//form[@id='preview-form']//details[@class='advanced']")
    assert section.get("open") is not None
    assert texts(section, ".//textarea[@name='options']") == ['{"links": true}']
```

Browser (`tests/ui/test_a11y_browser.py`):

```python
def test_error_in_a_closed_section_opens_it(ui: tuple[str, str]) -> None:
    # extract tab; set #extract-options to "{" with page.evaluate (the section is closed),
    # fill #extract-url, submit; then:
    expect(page.locator("#panel-extract details.advanced")).to_have_attribute("open", "")
    assert page.evaluate("document.activeElement.id") == "extract-options"


def test_invalid_value_in_a_closed_section_opens_it(ui: tuple[str, str]) -> None:
    # set #extract-wait-seconds to "99" with page.evaluate, close the section, submit; then:
    expect(page.locator("#panel-extract details.advanced")).to_have_attribute("open", "")
```

Update the browser tests that fill a field now in Advanced: in
`test_axe_finds_no_violations` and `test_error_is_associated_with_its_field` click
`#panel-extract details.advanced > summary` before `page.fill("#extract-options", …)`; in
`test_an_error_that_replaces_its_target_is_not_announced_as_done` wait for
`#result .batch-preview #preview-options` with `state="attached"` and open the preview's
`details.advanced` before the fill. In `test_axe_finds_no_violations`, open each tab's
`details.advanced` (when present) before its axe run, so its fields are checked.

Layout (`tests/ui/test_layout_browser.py`):

```python
def test_main_button_is_wide_and_petrol(ui: tuple[str, str]) -> None:
    button, form = boxes(page, "#extract-form > button[type=submit]", "#extract-form")
    assert button["width"] == form["width"] and button["height"] >= 48
    assert computed(page, "#extract-form > button[type=submit]", "backgroundColor") == [
        "rgb(29, 111, 106)"
    ]
```

- [ ] **Step 2: Run, expect FAIL** (`uv run pytest tests/ui -q`; `uv run pytest -m browser tests/ui -q`)
- [ ] **Step 3: Implement** the templates, CSS, `ui.js` and the two catalogue keys (en and de together).
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` (incl. `test_templates_i18n.py`, `test_i18n.py`, `test_shell.py::test_no_inline_script_or_style_or_hx_on`, `test_shell.py::test_ui_js_writes_text_only`) and `uv run pytest -m browser tests/ui -q`, expect PASS.
- [ ] **Step 5: Commit** `feat(ui): settings column with section labels and Advanced`

### Task 5: The result view

**Files:**
- Modify: `src/text_extraction/ui/templates/result.html`, `timeline.html`, `api_detail.html`
  (classes only), `images.html` (classes only)
- Modify: `src/text_extraction/ui/results.py` (`result_context`: `page_type`,
  `response_download`)
- Modify: `src/text_extraction/ui/templates/base.html` (`data-text-copied`,
  `data-text-copy-failed` on `body`)
- Modify: `src/text_extraction/ui/static/ui.css`, `src/text_extraction/ui/static/ui.js` (copy)
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json` (add `result.show_source`,
  `result.page_type`, `result.time`, `result.copy_markdown`, `result.copy_text`,
  `result.save_response`, `result.response_too_large`, `status.copied`, `status.copy_failed`;
  delete `result.show_rendered`)
- Test: `tests/ui/test_operations.py`, `tests/ui/test_access.py`, `tests/ui/test_done_when.py`,
  `tests/ui/test_a11y_markup.py`, `tests/ui/test_layout_browser.py`,
  `tests/ui/test_toolbar_browser.py` (new, marker `browser`), `tests/ui/test_shell.py`
  (`test_ui_js_writes_text_only` must still pass)

**Interfaces:**
- Consumes: Task 2 classes (`.card`, `.pill-*`), Task 3 `.workspace`.
- Produces:
  - `result_context(...)["page_type"]: str | None`: `data["page_type"]["type"]` when it is a
    mapping with a string `type`, else None (results cached before package 14 have none).
  - `def _response_download(envelope: Mapping[str, Any]) -> Download | None` in `results.py`:
    `json.dumps(envelope, indent=2, ensure_ascii=False)`; None when longer than
    `DOWNLOAD_MAX_CHARS`; else `Download(href="data:application/json;charset=utf-8;base64,…",
    name="response.json", suffix=".json", note=None)`.
    `result_context(...)["response_download"]` holds it, and `["response_too_large"]` is True
    when the envelope exists but was too long. Set for every answer that has `data` (not for a
    202 job, info or login).
  - `div.toolbar` (P15-R1, P15-S4): `h3.title`; then
    `<button type="button" class="secondary copy" data-action="copy" hidden>` with
    `t("result.copy_markdown")` when `data.format == "markdown"`, else `t("result.copy_text")`
    (only when `data.content` is a string); `a.download` (unchanged: class exactly `download`,
    its `href`, `download` and text); `<a class="download-response" href="…"
    download="response.json">{{ t("result.save_response") }}</a>`, or a `p.hint` with
    `result.response_too_large`; the download's cut note. All three look like outlined buttons
    (40 px, `--field-line` border, `--radius-field`).
  - `ui.js`: on `DOMContentLoaded` and `htmx:load`, every `button[data-action=copy][hidden]`
    loses `hidden` when `window.isSecureContext && navigator.clipboard`. A click on
    `data-action="copy"` writes the `textContent` of the first `pre.content` in the button's
    `.result-view` with `navigator.clipboard.writeText`, then `announce(text("copied"))`, or on a
    rejected promise `announce(text("copy-failed"))`. Text only, no `innerHTML`.
  - Catalogue texts: `result.copy_markdown` en "Copy Markdown", de "Markdown kopieren";
    `result.copy_text` en "Copy text", de "Text kopieren"; `result.save_response` en "Save
    response (JSON)", de "Antwort speichern (JSON)"; `result.response_too_large` en "The answer
    is too large to save here; the curl command reads all of it.", de "Die Antwort ist zu groß,
    um sie hier zu speichern; der curl-Befehl liest sie ganz."; `status.copied` en "Copied to
    the clipboard.", de "In die Zwischenablage kopiert."; `status.copy_failed` en "The browser
    did not allow copying to the clipboard.", de "Der Browser hat das Kopieren in die
    Zwischenablage nicht erlaubt."
  - `result.html` order: `header.answer` (status line, P15-S3: the status `span` with class
    `badge badge-<status> pill pill-<status>` and `data-value`, the unblocked pill
    `badge badge-unblocked pill pill-partial`, `span.http-status`) · error box · job line and
    "Show progress" · `dl.info` (a card by CSS) · screenshot (in a `.card`) · when `data`:
    `div.toolbar` (below) · `dl.facts` (a card by CSS, P15-S11) · structured values `section.card` (open) ·
    `details.card.fold` for Metadata (`dl.meta`), Links (`ul.links`), Images (`table.images`, `form.previews`,
    `.previews-out`) and `timeline.html` (`details.card.fold.timeline-view`, `open` when there
    are warnings) · `section.content-view.card.reading` · chunks `section.card` (open) ·
    `details.curl-box` and `details.raw` (cards by CSS, class attributes unchanged).
  - `dl.facts` (P15-S9): every pair in `<div class="fact">`; pairs in this order and only when
    the value exists: URL (`div.fact.fact-url`, full row), Language and Words (when
    `word_count` exists), Mode used, Page type (`span.pill.pill-neutral` with
    `t("page_type." ~ page_type)`), Time (`data.timings.total_ms / 1000`, `"%.2f"`, comma in
    German, then `" s"` through the existing `progress.seconds` text), Site rules
    (`compliance_mode`). Terms: 12 px small caps (`.section-label` style); values weight 700;
    the pairs in a wrapping row, 28 px apart.
  - Reading card (P15-S5): `section.content-view.card.reading` with `h4` "Content", the cut
    note, then either `div.markdown` (rendered) followed by `<details class="source"><summary>{{
    t("result.show_source") }}</summary><pre class="content">…</pre></details>`, or
    `pre.content` alone. `.reading .markdown` and `.reading > pre.content`: `--font-read`,
    19 px (17 px below 600 px), line-height 1.75, `max-width: 70ch`, 3 px `--accent` left
    border, padding-left 20 px, no background, `white-space: pre-wrap` for the `pre`; headings
    inside `.markdown` in `--font-ui`. `details.source pre.content` keeps `--font-code` on
    `--page`. Card padding 40 px 48 px 48 px (20 px 16 px below 600 px).
  - Catalogue texts: `result.show_source` en "Show source", de "Quelltext anzeigen";
    `result.page_type` en "Page type", de "Seitentyp"; `result.time` en "Time", de "Zeit".

- [ ] **Step 1: Write the failing tests** (`tests/ui/test_operations.py`, with its own
  `result_data(**overrides)` (Markdown by default), `rendered`, `pairs`, `run` and `fields`)

```python
def test_figures_have_page_type_and_time() -> None:
    data = result_data(page_type={"type": "content", "signals": ["word_count"]})
    data["timings"]["total_ms"] = 1530
    root = tree(rendered(data))
    figures = pairs(root, "facts")
    assert (figures["Page type"], figures["Time"]) == ("Content", "1.53 s")
    german = pairs(tree(rendered(data, language="de")), "facts")
    assert (german["Seitentyp"], german["Zeit"]) == ("Inhalt", "1,53 s")


def test_figures_without_page_type_or_timings() -> None:
    data = result_data()
    del data["timings"]
    figures = pairs(tree(rendered(data)), "facts")
    assert "Page type" not in figures and "Time" not in figures


def test_markdown_in_the_reading_card_and_source_folded(client: TestClient) -> None:
    root = tree(run(client, "extract", **fields()).text)
    (card,) = root.xpath("//section[contains(@class, 'reading')]")
    assert card.xpath("./div[@class='markdown']//h1")
    (source,) = card.xpath("./details[@class='source']")
    assert source.get("open") is None and source.xpath("./pre[@class='content']")


def test_text_in_the_reading_card() -> None:
    root = tree(rendered(result_data(format="text")))
    (card,) = root.xpath("//section[contains(@class, 'reading')]")
    assert card.xpath("./pre[@class='content']") and not card.xpath(".//details")


def test_what_happened_is_open_only_with_warnings() -> None:
    quiet = tree(rendered(result_data()))
    loud = tree(rendered(result_data(warnings=["content_too_short"])))
    assert quiet.xpath("//details[contains(@class, 'timeline-view')]/@open") == []
    assert loud.xpath("//details[contains(@class, 'timeline-view')]/@open") == ["open"]


def test_error_without_data_keeps_the_status_line() -> None:
    envelope = {"status": "failed", "error": {"code": "forbidden", "message": "No."}}
    root = tree(render_result(op="extract", http_status=403, envelope=envelope))
    assert texts(root, "//header[@class='answer']/span[@class='http-status']") == ["HTTP 403"]
    assert root.xpath("//header[@class='answer']/span[@data-value='failed']")
    assert root.xpath("//dl[@class='facts']") == []


def test_job_answer_has_no_figures() -> None:
    job = {"id": "j1", "kind": "extract", "status_url": "/v1/jobs/j1"}
    root = tree(render_result(op="extract", http_status=202, envelope={"data": job}))
    assert texts(root, "//header[@class='answer']/span[@class='http-status']") == ["HTTP 202"]
    assert root.xpath("//button[contains(@class, 'progress-button')]")
    assert root.xpath("//dl[@class='facts']") == []


@pytest.mark.parametrize(("language", "fmt"), [("en", "markdown"), ("de", "text")])
def test_toolbar_has_copy_save_and_save_response(language: Language, fmt: str) -> None:
    envelope = {"status": "success", "data": result_data(format=fmt), "trace_id": "t"}
    root = tree(render_result(op="extract", http_status=200, envelope=envelope, language=language))
    (toolbar,) = root.xpath("//div[@class='toolbar']")
    (copy,) = toolbar.xpath("./button[@data-action='copy']")
    assert copy.get("hidden") is not None and copy.get("type") == "button"
    key = "result.copy_markdown" if fmt == "markdown" else "result.copy_text"
    assert copy.text_content().strip() == translate(language, key)
    assert toolbar.xpath("./a[@class='download']")
    (saved,) = toolbar.xpath("./a[@class='download-response']")
    assert saved.get("download") == "response.json"
    head, encoded = saved.get("href").split(",", 1)
    assert head == "data:application/json;charset=utf-8;base64"
    assert json.loads(base64.b64decode(encoded)) == envelope


def test_save_response_is_left_out_when_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(results, "DOWNLOAD_MAX_CHARS", 100)
    root = tree(rendered(result_data(content="x" * 200)))
    assert root.xpath("//a[@class='download-response']") == []
    assert texts(root, "//div[@class='toolbar']/p[@class='hint']") == [
        translate("en", "result.response_too_large")
    ]
```

Browser (`tests/ui/test_toolbar_browser.py`; the `ui` fixture, `browsing` and `log_in` of
`test_playground_browser`; the context gets
`grant_permissions(["clipboard-read", "clipboard-write"])`; the app runs on 127.0.0.1, a
secure context):

```python
def test_copy_puts_the_text_on_the_clipboard(ui: tuple[str, str]) -> None:
    # extract /docs/a.html as Markdown, then:
    copy = page.locator("#result button[data-action=copy]")
    expect(copy).to_be_visible()
    copy.click()
    expect(page.locator("#ui-status")).to_have_text(translate("en", "status.copied"))
    shown = page.locator("#result pre.content").text_content()
    assert page.evaluate("navigator.clipboard.readText()") == shown


def test_copy_refused_says_so(ui: tuple[str, str]) -> None:
    # as above, but without clipboard permissions and with navigator.clipboard.writeText
    # replaced by page.evaluate with a function that rejects; then:
    expect(page.locator("#ui-status")).to_have_text(translate("en", "status.copy_failed"))


def test_both_saves_download_files(ui: tuple[str, str]) -> None:
    # extract /docs/a.html as Markdown, then:
    with page.expect_download() as text_file:
        page.click("#result a.download")
    assert text_file.value.suggested_filename == "result.md"
    assert "Docs A" in Path(text_file.value.path()).read_text(encoding="utf-8")
    with page.expect_download() as answer:
        page.click("#result a.download-response")
    assert answer.value.suggested_filename == "response.json"
    saved = json.loads(Path(answer.value.path()).read_text(encoding="utf-8"))
    assert (saved["status"], saved["data"]["title"]) == ("success", "Docs A")


def test_copy_stays_hidden_without_javascript(ui: tuple[str, str]) -> None:
    # a context with java_script_enabled=False cannot log in (the login is script), so render
    # the fragment: POST /ui/run/extract with the key and HX-Request through page.request,
    # set it as the page's content, then:
    expect(page.locator("button[data-action=copy]")).to_be_hidden()
```

The axe run "extract result, expanded" (below) covers the toolbar with "Copy" shown.

Update the pinned xpaths to the grouped `dl` (`/dt` and `/dd` become `//dt` and `//dd`; the
class match stays exact): `tests/ui/test_access.py` (three places in
`test_result_shows_the_unblocked_badge` and `test_choice_and_result_follow_the_language`),
`tests/ui/test_done_when.py::facts`, `tests/ui/test_operations.py::pairs` (it serves `facts`,
`meta`, `info`). `test_markdown_shown_rendered_and_as_source` stays as it is and must pass.

Browser (`tests/ui/test_layout_browser.py`):

```python
def test_reading_card_typography(ui: tuple[str, str]) -> None:
    # extract /docs/a.html as Markdown, then on "#result .reading .markdown":
    family, size, rule, colour = computed(
        page,
        "#result .reading .markdown",
        "fontFamily",
        "fontSize",
        "borderLeftWidth",
        "borderLeftColor",
    )
    assert family.startswith("Charter") and (size, rule) == ("19px", "3px")
    assert colour == "rgb(29, 111, 106)"
    (width, zero) = page.evaluate(ZERO_WIDTH)  # the block's width, a "0"'s width in its font
    assert width <= 70 * zero + 1
```

`ZERO_WIDTH` is a JS function in the test module that appends a `span` holding "0" to the
block, measures both with `getBoundingClientRect`, removes the span and returns the two
widths. The page is 1440 px wide, so the block is held by `max-width`, not by its card.

In `test_a11y_browser.py::test_axe_finds_no_violations`, after the extract result, open every
`#result details` (`page.evaluate`) and run axe again as "extract result, expanded".
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** `result_context`'s `page_type` and `response_download`, the template, the copy code in `ui.js` (and its paragraph in the file's header comment), the two `data-text-*` attributes in `base.html`, the CSS and the catalogue changes (en and de together).
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`, expect PASS; `uv run mypy` clean.
- [ ] **Step 5: Commit** `feat(ui): result view with status line, toolbar, figures, cards and reading text`

### Task 6: Jobs, tree, batch preview and the other fragments

**Files:**
- Modify: `src/text_extraction/ui/templates/progress.html`, `progress_live.html`, `tree.html`,
  `batch_preview.html`, `images.html`, `form_error.html`, `cancel.html`, `login.html`
  (added classes only; no pinned class attribute changes)
- Modify: `src/text_extraction/ui/static/ui.css` (`.request-status`, the box `ui.js` builds,
  is styled by its existing class; `ui.js` is unchanged)
- Test: `tests/ui/test_layout_browser.py`, `tests/ui/test_a11y_browser.py`

**Interfaces:**
- Consumes: Task 2 classes and tokens.
- Produces (CSS, in the mockup's language):
  - `section.job`: a card by CSS (P15-S11); `h4` 16 px; `.state` a pill: `state-completed`
    success colours, `state-failed` failed colours, others neutral; `progress` with `accent-color:
    var(--accent)`, 100 % wide up to 24 rem; `.counts` spans `success`, `partial`, `failed`
    coloured with `--accent-strong`, `--warn`, `--error`; `table.items`: 14 px, header row in
    the section-label style, rows with a `--line` bottom border; item badges as pills.
  - `.pager`, `.actions`, `.tree-tools`: outlined buttons (`button.secondary`), 8 px gap.
  - `section.batch-preview`: card; `dl.facts.counts` as a figures row (it is a `dl.facts`
    too, so it shares Task 5's style without `div.fact` groups: give `dl.counts` a two-column
    grid instead).
  - `section.tree-view`: card; `ul.tree` on `--page` with `--line` border and
    `--radius-field`; `.page-type` a `pill-neutral` (guessed: dashed `--field-line` border);
    `.found` a smaller neutral pill; `.tree-actions` separated by a `--line` top border;
    `form.tree-form` keeps its class.
  - `.preview-grid figure.preview`: card, 8 px padding, image radius 8 px; `.skipped` a
    `pill-partial`.
  - `.error`, `.request-status`: `--error-wash`, `--error`, `--radius-field`, a 3 px `--error`
    left border; `.banner`: `--warn-wash`, `--warn`; `p.cancelled`: `--neutral-wash`.
  - `.login`: card, 32 px padding, centred, 30 rem.

- [ ] **Step 1: Write the failing test** (`tests/ui/test_layout_browser.py`)

```python
def test_pills_use_the_status_colours(ui: tuple[str, str]) -> None:
    # a batch of /docs/a.html until completed, then:
    assert computed(page, "#result section.job .state", "backgroundColor", "color") == [
        "rgb(230, 242, 240)",
        "rgb(21, 90, 86)",
    ]
    assert computed(page, "#result section.job progress", "accentColor") == ["rgb(29, 111, 106)"]
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** the CSS (and the few wrapper classes).
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`, expect PASS (axe: running and completed progress, site tree, batch preview, both languages).
- [ ] **Step 5: Commit** `feat(ui): jobs, tree and previews in the new style`

### Task 7: Phone width, focus and reduced motion

**Files:**
- Modify: `src/text_extraction/ui/static/ui.css`
- Test: `tests/ui/test_layout_browser.py`, `tests/ui/test_a11y_browser.py`

**Interfaces:**
- Consumes: everything above.
- Produces: below `600px`: `--gutter: 16px`; tables in result cards scroll inside a wrapper
  (`overflow-x: auto` on `.card` children that are tables, or `display: block` on
  `table.items`, `table.images`, `table.values`); `pre` scrolls inside its card; `dd` and
  `.url` cells break anywhere; tabs wrap. `:focus-visible` (3 px `--focus`, offset 2 px) also
  on `summary`, the segment labels' radios, `a.download` and tree checkboxes. Transitions (colour only, 120 ms) are declared only inside
  `@media (prefers-reduced-motion: no-preference)`; the existing reduce block stays.

- [ ] **Step 1: Write the failing tests**

```python
PHONE = {"width": 390, "height": 844}


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_horizontal_scroll_on_a_phone(ui: tuple[str, str], language: Language) -> None:
    # 390 x 844: each tab, then an extract result with every #result details open, a
    # completed batch progress, the site tree; after each:
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def test_focus_ring_on_summary_and_segment(ui: tuple[str, str]) -> None:
    for selector in ("#panel-extract details.advanced > summary", "#extract-form .segmented input"):
        page.locator(selector).first.focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        assert computed(page, ":focus", "outlineStyle", "outlineWidth", "outlineColor") == [
            "solid",
            "3px",
            "rgb(29, 111, 106)",
        ]


def test_reduced_motion_turns_transitions_off(ui: tuple[str, str]) -> None:
    page.emulate_media(reduced_motion="reduce")
    (duration,) = computed(page, "#tab-file", "transitionDuration")
    assert float(duration.rstrip("s")) <= 0.001
```

`tests/ui/test_a11y_browser.py`: a new `test_axe_finds_no_violations_on_a_phone` (both
languages, 390 x 844): the login, every tab with its Advanced open, an extract result with
every `#result details` open, a form error.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** the CSS.
- [ ] **Step 4: Run** `uv run pytest -m browser tests/ui -q`, expect PASS (all axe, keyboard, focus, status and layout tests).
- [ ] **Step 5: Commit** `feat(ui): phone width, focus rings and reduced motion`

### Task 8: Docs, changelog, package status and the "after" screenshots

**Files:**
- Modify: `docs/user-guide/en/usage.md` and `de/usage.md` ("The playground" / "Die Spielwiese":
  the layout in one paragraph: header with tabs, settings left and result right, stacked on a
  phone; "Advanced" holds "Wait seconds" and "Options (JSON)"; the result's status line,
  figures, closed sections and reading text)
- Modify: `docs/user-guide/en|de/single-url.md` ("In the playground": "Options (JSON)" is under
  "Advanced"; the result's order; the toolbar: copy, save, save response), `site-rules.md` and `whole-site-and-metadata.md` (where they
  name "Options (JSON)": "under Advanced"), `glossary.md` (the term "Advanced" / "Erweitert")
- Modify: `docs/architecture.md` ("The UI": a bullet "Look (package 15)" with the layout, the
  tokens, P15-R1 to P15-R6 and the P15-S rulings; "Accessibility": `revealField`, the 3:1 field boundary, the
  phone-width axe run)
- Modify: `CHANGELOG.md` (`[Unreleased]`: a "Package 15" lead line with the plan link and
  `### Changed`: the playground's new look, Advanced, reading card, "Show source"; `### Added`:
  "Copy Markdown"/"Copy text" and "Save response (JSON)" in the result's toolbar; no API change)
- Modify: `docs/packages.md` (status row 15: `in review`, the PR, the evidence: tests, axe,
  screenshots in the PR; filled in when the PR is open)
- Test: `uv run pytest tests/test_docs_i18n.py tests/ui/test_i18n.py -q`,
  `uv run python scripts/check_translation_pairs.py --base origin/main`

**Interfaces:**
- Consumes: the texts of Tasks 4 and 5 (`form.advanced` "Advanced"/"Erweitert",
  `result.show_source` "Show source"/"Quelltext anzeigen") as the docs name them.

- [ ] **Step 1: Write the docs** (English first, the German partner in the same commit; same heading levels, links and tables).
- [ ] **Step 2: Run the doc checks**, expect PASS.
- [ ] **Step 3: Capture "after"** on the branch: `uv run python -m tests.ui.screenshots --out /mnt/project-files/package-15/screenshots/after` (44 PNGs, the names of the "before" set; not committed, P15-R2).
- [ ] **Step 4: Commit** `docs: package 15`
- [ ] **Step 5: Pull request** (after `requesting-code-review` and its fixes), pushed only to `claude/project-thread-eexf9n` (P15-R2): the body says that the screenshots are in `/mnt/project-files/package-15/screenshots/before/` and `…/after/` (11 states, en and de, 1440 px and 390 px, named `<n>-<state>-<lang>-<width>.png`), with a list of the states. Then fill in status row 15 and commit `docs: package 15 status`.

## Done when

- `uv run pytest`, `uv run pytest -m browser tests/ui` (CI `ci`, `browser`), `uv run ruff check
  .`, `uv run ruff format --check .` and `uv run mypy` green; the licence check green.
- axe-core finds no WCAG 2.2 A/AA violation in any state in either language, at desktop and
  phone width; the keyboard, focus, status and layout tests pass; `tests/ui/test_palette.py`
  holds every text pair at 4.5:1 or more and field boundaries and focus at 3:1 or more.
- Before and after screenshots (11 states, en and de, 1440 px and 390 px) are in
  `/mnt/project-files/package-15/screenshots/{before,after}/`, and the pull request says so
  (P15-R2).
- Jan checks the playground on his PC (`docker compose up --build`, `/ui`, both languages, a
  narrow window) and approves it.
