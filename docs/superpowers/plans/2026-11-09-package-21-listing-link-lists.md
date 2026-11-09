# Package 21: Link Lists and Tables That Survive Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A listing page keeps the link lists of its content area, a KMap lesson keeps its
attachment links (each with its own URL), and a table with a spanning header row stays one
table: in the API's `content` and in the playground's rendering.

**Architecture:** A new module `convert/listings.py` holds a small value type for headed link
lists, its renderer for the three output formats, and a pure collector `listing_lists` that
walks a page in document order and gathers headings and standalone links outside the page's
chrome. The pipeline calls the collector only for a page of type `listing`. KMap attachments
become such link lists too and are appended after Trafilatura instead of passing through it
(Trafilatura drops link-only lists). `enhance_table_structure` pads every row of a Markdown table
to the widest row, and the playground's Markdown renderer turns tables on.

**Tech Stack:** Python 3.13, BeautifulSoup (lxml), Trafilatura, markdown-it-py, pytest.

**Spec:** Jan, 2026-11-09:
1. Decision card "Wie sollen Übersichtsseiten ihre Linklisten im Text behalten?", option
   "Automatisch bei Übersichtsseiten" ("Ist eine Seite als listing erkannt, kommen die
   Linklisten des Inhaltsbereichs als Markdown-Listen mit Überschriften dazu, ohne Navigation
   und Fußzeile."). Case: https://www.leifiphysik.de/optik (type `listing`, signal
   `link_density`) keeps only its introduction; the topic lists (`div.views-row > a` under
   `<h2><h3>Sekundarstufe 1</h3></h2>` and `<h3><h3>Lichtausbreitung</h3></h3>`) are dropped by
   Trafilatura with and without `favor_recall`. A local prototype of Task 1's algorithm got all
   13 topic sections.
2. "hier noch ein zweites beispiel das du bitte zum testen einbeziehst. die kmap seite endet
   nach den basisgrößen": https://kmap.eu/app/browser/Physik/Grundlagen/Physikalische%20Größen.
   Diagnosis on the saved page (`<html lang="de">`, lesson in
   `<script id="embedded-topic" type="json">`): the API's Markdown has every table but ends with
   an empty `## Explanations` (Trafilatura drops the attachment `<ul>`); both attachment links
   would point to the last link's URL (`_attachment_sections` keys targets by `file`, which a
   link attachment has not); the table that starts with
   `<tr><th colspan="4"><br/>7 Basisgrößen</th></tr>` becomes a Markdown table whose header has
   one cell and whose rows have four, which a renderer cuts to one column; and the playground
   renders no table at all (markdown-it `commonmark` preset without the `table` rule), so every
   table shows as a paragraph of pipes.

3. 13:44: "eventuell must du auch formeln und und andere anzeige elemente für die ui prüfen um
   zumindest markdown kompatibel zu sein und auch formeln wie brüche und so weiter sauber
   anzuzeigen zu können." The conversion writes every equation (MathML, MathJax sources, `data-tex`)
   as LaTeX between `$$` and `$$`; the playground shows that source as text. Its CSP is
   `script-src 'self'`, so the rendering happens on the server: LaTeX to MathML, which browsers
   draw natively.

Neither page's HTML is committed (third-party content); tests use synthetic pages of the same
shape.

## Global Constraints

- English in code, comments, developer docs, commit messages. The user guide changes in English
  and German in the same commit (`tests/test_docs_i18n.py`,
  `uv run python scripts/check_translation_pairs.py --base origin/main`).
- Listing lists change only pages with `page_type.type == "listing"`; an upload (no page type), a
  document and a media result keep today's `content`.
- No new request field, setting or warning code; the result envelope is unchanged (rule 4).
- Constants in `convert/listings.py`: `LISTING_MIN_LINKS: Final = 5`,
  `MAX_ITEM_CHARS: Final = 150`, `LINK_SHARE_OF_ITEM: Final = 0.8`; chrome words `footer`,
  `breadcrumb`, `breadcrumbs`, `cookie`, `consent`, `sidebar`, `social`, `share`, `skip`,
  `pagination`, `pager`, `menu`, `feedback`.
- KMap attachment headings: English `Explanations`, `Ideas`, `Applications`, `Attachments`
  (unchanged); on a page whose `<html lang>` starts with `de`: `Erklärungen`, `Ideen`,
  `Anwendungen`, `Anhänge`.
- New runtime dependency for Task 6: `latex2mathml==3.81.1` (MIT, released 2026-09-07, pure
  Python; at least 14 days old). No other new dependency.
- Version 0.1.10 (every file that names 0.1.9 today outside CHANGELOG and plans:
  `pyproject.toml`, `uv.lock`, `deploy/helm/text-extraction/Chart.yaml`,
  `deploy/helm/text-extraction/values-production.example.yaml`, `docker-compose.yml`, both
  READMEs, both `installation.md`, both `site-rules.md`, `docs/packages.md`), CHANGELOG
  `[0.1.10]`.

## Review Focus

1. An article page that is not a listing but has a related-links list: content unchanged. Task 2.
2. Inline links inside a paragraph are never standalone. Task 1.
3. Footer links in a `div` (class `region-pre-footer`, no `<footer>`), a feedback button after
   the footer, links before the `<h1>`: not collected. Task 1.
4. A URL already in the content, the page's own URL with a fragment, and a repeated URL: listed
   at most once, never twice. Task 1.
5. A KMap file attachment used inline in the lesson is still not listed again; a file
   attachment not used inline is listed with its own URL. Task 3.

---

### Task 1: Link sections and the listing collector

**Files:**
- Create: `src/text_extraction/convert/listings.py`
- Test: `tests/test_convert_listings.py`

**Interfaces:**
- Consumes: `prepare_html`, `strip_non_content`, `is_outside_content`, `attr` from
  `text_extraction.convert.markup`; `OutputFormat` as `convert/html.py` imports it.
- Produces:
  - `@dataclass(frozen=True) class LinkSection: heading: str | None; level: int; links: tuple[tuple[str, str], ...]`
    (`links` are `(text, absolute_url)`; `level` 1-6, ignored when `heading` is None)
  - `render_link_sections(sections: Sequence[LinkSection], fmt: OutputFormat) -> str` (`""` for
    no sections)
  - `listing_lists(html: str, url: str, fmt: OutputFormat, *, existing: str) -> str`
  - the constants above

- [ ] **Step 1: Write the failing tests.** A synthetic page `HUB`: a `<header>` with an
  "Anmelden" link; a `<nav>` with five links; a `div.branch_header` with `<h1>Optik</h1>` and an
  introduction paragraph with one inline link; a `div.branch_issues` with
  `<h2 class="mb-3"><h3 id="degree-1">Sekundarstufe 1</h3></h2>`, then the sections
  `<h3 class="mb-0"><h3>Lichtausbreitung</h3></h3>` (four `div.views-row` items, e.g.
  `<div class="views-row pb-1"><a href="/optik/lichtausbreitung/sehvorgang">Sehvorgang</a></div>`)
  and `Lichtreflexion` (three items), each followed by two `a.read-more-link`
  "Zum Themenbereich" links to the section URL; an `<a href="#degree-1">Sekundarstufe 1</a>`; a
  `div.region-pre-footer` with three project links; a `div#feedback-form` with a "Feedback"
  link. Tests:
  - `test_hub_sections_become_headed_lists`: `listing_lists(HUB, "https://example.org/optik", "markdown", existing="# Optik\n\nIntro")`
    contains `"## Sekundarstufe 1"`, `"### Lichtausbreitung"`,
    `"- [Sehvorgang](https://example.org/optik/lichtausbreitung/sehvorgang)"`, Lichtausbreitung
    before Lichtreflexion, and exactly one `"(https://example.org/optik/lichtausbreitung)"`.
  - `test_chrome_and_inline_links_are_left_out`: no "Anmelden", no nav link text, not the
    introduction's inline link, no pre-footer link, no "Feedback", no `#degree-1`.
  - `test_known_urls_and_headings_are_not_repeated`: with `existing` holding
    `"[Sehvorgang](https://example.org/optik/lichtausbreitung/sehvorgang)"` and the line
    `"### Lichtreflexion"`: no Sehvorgang item, no Lichtreflexion heading, its links stay
    (revised in fix wave 2: the heading stays with its links).
  - `test_too_few_links_give_nothing`: a page with four standalone links answers `""`.
  - `test_long_items_are_not_standalone`: a `li` with a link plus 200 characters of text is
    not collected.
  - `test_render_link_sections_in_each_format`: sections
    `[LinkSection("Erklärungen", 2, (("A & B", "https://x.org/a?b=1&c=2"),))]` render as
    markdown `"## Erklärungen\n\n- [A & B](https://x.org/a?b=1&c=2)"`, text
    `"Erklärungen\n\n- A & B"` (no URL), html
    `'<h2>Erklärungen</h2><ul><li><a href="https://x.org/a?b=1&amp;c=2">A &amp; B</a></li></ul>'`;
    a section with `heading=None` renders only its list; `render_link_sections([], "markdown") == ""`.
- [ ] **Step 2: Run** `uv run pytest tests/test_convert_listings.py -v`: FAIL (module missing).
- [ ] **Step 3: Implement.** `listing_lists`: parse with `prepare_html`, then
  `strip_non_content`; walk `soup.find_all(True)` in document order from after the first `<h1>`
  (from the start when there is none). A heading (`h1`-`h6`, non-empty normalised text) is an
  event `(level, text)`. A link (`a[href]`, `urljoin(url, href)`, scheme http or https, not the
  page URL once fragments are dropped, not seen yet, not a substring of `existing`) is an event
  when standalone: its closest ancestor among `li, div, p, dd, dt, td, th, span` has at most
  `MAX_ITEM_CHARS` characters of normalised text and the link text is at least
  `LINK_SHARE_OF_ITEM` of it; a link inside a heading is not standalone. Skip an element with an
  ancestor-or-self that `is_outside_content` or whose `id` or `class`, split on whitespace, `-`
  and `_`, contains a chrome word. Under `LISTING_MIN_LINKS` link events answer `""`. Group the
  events into `LinkSection`s: a heading starts a section; a heading whose section has no links
  and no deeper heading with links before the next heading of its level or higher is dropped,
  and so is a heading whose text is a heading line of `existing` (`#` marks stripped) — its
  links then join the section with `heading=None` (revised in fix wave 2: such a heading keeps
  its section when it has links of its own, `test_a_heading_the_content_holds_keeps_its_section`; a held parent heading without links of its own is left out, `test_a_held_heading_without_links_of_its_own_is_not_repeated`). Render with `render_link_sections`: markdown
  `"#" * level + " " + heading`, a blank line, `- [text](url)` lines, sections separated by a
  blank line; text the same without `#` and URLs; html `<hN>…</hN><ul><li><a href>…</a></li></ul>`
  with `html.escape(…, quote=True)`.
- [ ] **Step 4: Run** the same command: PASS; `uv run ruff check src tests`; `uv run mypy`.
- [ ] **Step 5: Commit** `feat(convert): collect the link lists of a listing page`.

### Task 2: Listing pages in the pipeline

**Files:**
- Modify: `src/text_extraction/pipeline.py` (`_process`, after `classify_page`)
- Test: `tests/test_pipeline.py`, `tests/test_pipeline_render.py`

**Interfaces:**
- Consumes: `listing_lists` (Task 1).

- [ ] **Step 1: Write the failing tests** in `tests/test_pipeline.py`:
  `test_a_listing_page_keeps_its_link_lists` (a fetched synthetic hub page with a short
  introduction and eight standalone links under two headings: `page_type.type == "listing"`,
  `content` has a `- [` item of each section, `quality.word_count` counts the joined content);
  `test_a_content_page_with_related_links_is_unchanged` (a long article with a related-links
  `ul` of six links: type `content`, `content` equals `convert_html(page, url, "markdown").content`
  after the boilerplate rules, no related link item); `test_listing_lists_pass_the_boilerplate_rules`
  (a domain rule equal to one item line removes that line). In `tests/test_pipeline_render.py`:
  `test_rendered_listing_keeps_its_link_lists` (browser mode, the hub page rendered).
- [ ] **Step 2: Run** `uv run pytest tests/test_pipeline.py tests/test_pipeline_render.py -k "listing or related" -v`: FAIL.
- [ ] **Step 3: Implement** in `_process`: when `found_type` is not None and
  `found_type.type == "listing"`, call
  `listing_lists(text, final_url, req.format, existing=conversion.content)`; when it answers
  text, join it to `conversion.content` (`"\n\n"` for markdown and text, nothing for html), run
  `strip_boilerplate(host, …)` on the joined text and `assess(text, content)` again. An exception
  from `listing_lists` logs `listing_lists_failed` with `host` and `error_type` only and keeps the
  content as it was (like `page_type_failed`).
- [ ] **Step 4: Run** `uv run pytest -q`: PASS.
- [ ] **Step 5: Commit** `feat(pipeline): a listing page keeps its link lists`.

### Task 3: KMap attachments

**Files:**
- Modify: `src/text_extraction/convert/embedded.py`, `src/text_extraction/convert/html.py`
  (`convert_html`)
- Test: `tests/test_convert_embedded.py`

**Interfaces:**
- Consumes: `LinkSection`, `render_link_sections` (Task 1).
- Produces: `@dataclass(frozen=True) class Embedded: html: str; sections: tuple[LinkSection, ...] = ()`
  and `embedded_content(soup: BeautifulSoup, url: str) -> Embedded | None`, replacing
  `embedded_html` (every caller and test moves to it; `html` is what `embedded_html` returned,
  without the attachment sections).

- [ ] **Step 1: Write the failing tests:**
  `test_link_attachments_keep_their_own_urls` (a historic payload with two `type: link`
  attachments, tag `explanation`, no `file`, and one file attachment not used inline: the
  sections hold both links with their own `href`s, in payload order, and the file under
  `Attachments` with its resolved URL); `test_attachment_headings_follow_the_page_language`
  (`<html lang="de">` gives `Erklärungen`, no `lang` gives `Explanations`);
  `test_convert_html_keeps_kmap_attachment_links` (`convert_html` on a KMap-shaped page in
  markdown ends with `"## Erklärungen\n\n- [LEIFIphysik](https://www.leifiphysik.de/…)\n- [Physikunterricht Online](https://physikunterricht-online.de/…)"`);
  update `test_historic_topic_payload_resolves_inline_attachments` to read `Ideas` and
  "Extra idea" from `sections` and keep its other assertions on `html`.
- [ ] **Step 2: Run** `uv run pytest tests/test_convert_embedded.py -v`: FAIL.
- [ ] **Step 3: Implement.** Targets by `file` only for attachments that have one; every
  attachment's own target is its resolved `href`. A file used inline in the body is not listed;
  every other attachment with a label and a target is. Headings by `tag` (`explanation`,
  `idea`, `usage`, else Attachments) in the page's language (Global Constraints), level 2.
  `convert_html` appends `render_link_sections(embedded.sections, fmt)` to the converted text
  (Trafilatura's or the fallback's) before `_with_documents`, joined like Task 2.
- [ ] **Step 4: Run** `uv run pytest tests/test_convert_embedded.py tests/test_convert_html.py -q`: PASS.
- [ ] **Step 5: Commit** `fix(convert): keep KMap attachment links with their own URLs`.

### Task 4: Tables with a spanning header row

**Files:**
- Modify: `src/text_extraction/convert/markup.py` (`enhance_table_structure`),
  `src/text_extraction/ui/markdown.py` (`_parser`)
- Test: `tests/test_convert_markup.py`, `tests/ui/test_operations.py` (or the test module that
  covers `render_markdown`)

- [ ] **Step 1: Write the failing tests:** `test_table_rows_are_padded_to_the_widest_row`
  (`"| 7 Basisgrößen |\n|---|\n| Größe | Symbol | Zeichen | Einheit |\n| Länge | l | m | Meter |"`
  becomes four columns in every row: `"| 7 Basisgrößen |  |  |  |"`,
  `"|---|---|---|---|"`, the other rows unchanged); a table whose rows are all equal stays
  byte for byte; `test_render_markdown_renders_tables` (`render_markdown` of a two-row table
  contains `<table>`, `<th>Größe</th>` and `<td>Länge</td>`; a link in a cell to `javascript:`
  stays text).
- [ ] **Step 2: Run** `uv run pytest tests/test_convert_markup.py tests/ui -k "table" -q`: FAIL.
- [ ] **Step 3: Implement:** in `enhance_table_structure`, per table block outside fences
  (the separator it adds or finds included), pad each row with `|  |` cells to the block's
  widest row and the separator with `---|`; `_parser` adds `.enable("table")` to the
  `commonmark` parser (html stays off, links stay http(s) only).
- [ ] **Step 4: Run** `uv run pytest -q`: PASS.
- [ ] **Step 5: Commit** `fix: tables with a spanning header row stay one table, and the playground renders tables`.

### Task 6: Formulas and Markdown in the playground

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (`latex2mathml==3.81.1`), `src/text_extraction/ui/markdown.py`,
  the playground's stylesheet if MathML needs a rule (`src/text_extraction/ui/static/`)
- Test: the module that tests `render_markdown` (Task 4 added its table test there)

**Interfaces:**
- Consumes: Task 4's parser with tables on.
- Produces: `render_markdown(text) -> Markup` that also renders `$$…$$` as MathML and
  `~~text~~` as `<s>`.

- [ ] **Step 1: Write the failing tests:** `test_double_dollar_latex_becomes_mathml`
  (`render_markdown("Es gilt $$\\frac{a}{b}$$ hier.")` contains `<math` and `<mfrac>`, not the
  `$$` source); `test_an_equation_alone_in_its_paragraph_is_a_block`
  (`"$$x^2$$"` alone gives `display="block"`); `test_latex_inside_math_is_not_markdown`
  (`$$a_1 * b_2 * c$$` gives no `<em>`); `test_broken_latex_stays_readable` (an input
  latex2mathml raises on renders as escaped `<code>` with the source, never an exception);
  `test_mathml_carries_no_links_or_handlers` (`$$\\href{javascript:alert(1)}{x}$$` output has no
  `href` and no attribute starting with `on`); `test_single_dollars_stay_text` (`"5 $ und 10 $"`
  unchanged text); `test_strikethrough_renders` (`~~alt~~` gives `<s>alt</s>`).
- [ ] **Step 2: Run** the module's tests: FAIL.
- [ ] **Step 3: Implement:** add the dependency with `uv add latex2mathml==3.81.1` (lock
  updated); in `_parser`, enable `strikethrough` and add an inline rule before `escape` that
  takes `$$…$$` (non-empty, no `$$` inside) raw as a `math` token; its renderer calls
  `latex2mathml.converter.convert(source, display=…)` (`block` when the token is the only child of
  its paragraph, else `inline`), removes `href`, `xlink:href`, `src` and `on*` attributes from the
  result, and on any exception renders `<code>` with the escaped source. Raw HTML stays off.
  Run `uv run python scripts/check_licenses.py` (or the repo's licence check) for the new
  dependency.
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and the licence check: PASS.
- [ ] **Step 5: Commit** `feat(ui): render formulas as MathML and strikethrough in results`.

### Task 5: Docs and version 0.1.10

**Files:** `docs/architecture.md` (step 5 "Convert", "Page types and link categories"),
`docs/user-guide/en|de/single-url.md` (the `content` row), `docs/packages.md` (status row and
contents row 21), `CHANGELOG.md`, the version files of the Global Constraints.

- [ ] **Step 1:** Document: a `listing` page's `content` ends with the link lists of its content
  area (headings and standalone links, chrome left out, at least five links), in each format;
  a KMap lesson ends with its attachment links under headings in the page's language; table
  rows are padded to the widest row; the playground renders tables, strikethrough and formulas
  (`$$…$$` LaTeX as MathML; other notations such as AsciiMath in backticks stay code).
- [ ] **Step 2:** Bump to 0.1.10; run `uv run pytest -q`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`,
  `uv run python scripts/check_translation_pairs.py --base origin/main`.
- [ ] **Step 3: Commit** `docs: link lists, KMap attachments and tables; version 0.1.10`.
