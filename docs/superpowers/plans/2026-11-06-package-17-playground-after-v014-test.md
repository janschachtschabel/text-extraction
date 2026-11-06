# Package 17: playground after the v0.1.4 test — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix what Jan found when testing v0.1.4 (2026-11-06 11:29):
- the converter splits inline text into paragraphs and passes on PHP error output;
- the form and the result have no visible frame;
- the "Extract" form cannot ask for a screenshot;
- the site tree cannot be folded, and it leaves URLs that were never fetched without a type.

**Architecture:**
- The converter gets two pre-processing steps on the parsed page before Trafilatura: it wraps runs of inline content in `<p>`, and it removes PHP error output, reported as the warning `server_errors_removed`.
- The tree model (`ui/site_tree.py`, pure):
  - folds a section that holds only its own page into one row;
  - counts and marks URLs that have no type;
  - guesses a few more types from URLs;
  - collects the site's external social media links.
- The tree view gets fold buttons (progressive enhancement in `ui.js`), a "type open" badge and a social media list.
- The workspace frames the settings as a card and shows an empty result card.
- The extract form gets a "Also take a screenshot" box that sends the existing `screenshot: true`.
- No REST, OpenAPI or MCP change besides the new warning code.

**Tech Stack:** Python 3.13, BeautifulSoup/lxml, Trafilatura 2.3, FastAPI, Jinja2, htmx, plain JS (`ui.js`), pytest, Playwright (Chromium) for `-m browser`, axe-core.

**Spec:** `/mnt/project-files/package-17/plan-paket-17.md` (German), approved by Jan on 2026-11-06 at 11:41 with F1-F4 = a:
- F1 a: the tree opens with the top level open and every section below it folded.
- F2 a: types only from rules, with no extra fetches: better URL rules, a "type open" badge and a social media group.
- F3 a: PHP error output is removed and reported.
- F4 a: v0.1.5 after Jan's PC check.

## Global Constraints

- **Languages:**
  - English in code, comments, developer docs and commit messages.
  - Every English UI text in `ui/locales/en.json` has its German partner in `de.json`.
  - Every changed user doc in `docs/user-guide/en/` changes its partner in `docs/user-guide/de/` in the same task.
  - These pairs are checked by `tests/test_docs_i18n.py`, `tests/ui/test_i18n.py` and `scripts/check_translation_pairs.py`.
  - The German playground texts address the reader with „Sie“ or impersonal forms, as the existing `de.json` does; never „du“.
- **API:** no REST, OpenAPI or MCP change except the new warning code `server_errors_removed`. It is documented where the other warning codes are listed: `extract_models.py` (the `warnings` description), `docs/architecture.md`, and the UI text `warning.server_errors_removed`.
- **Accessibility:**
  - Every new control has an accessible name.
  - The fold buttons carry `aria-expanded` and `aria-controls`.
  - A badge's meaning is readable as text, not only as a `title`.
  - axe-core stays clean at 1440 and 390 px in `en` and `de`.
  - Keyboard: every new control is reachable with Tab and works with Enter/Space.
- **Progressive enhancement:** without JavaScript the tree shows every node unfolded and no fold buttons, and every form still posts.
- **Quality gates per task:** `uv run pytest` (the task's tests, plus `tests/ui` when UI changed, plus `-m browser` for browser tests that the task touches), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.

## Review Focus

1. **Ordinary prose must not move into separate paragraphs.** A page whose content is `<div>` text with links and bold words, and which has no leading inline element, keeps its paragraphs exactly as before the change (golden output of `tests/test_convert_html.py` stays). Test in Task 1.
2. **PHP-like text inside real prose is kept.** An article that explains "the message `Warning: foo in /var/www/x.php on line 3`" inside a `<code>` or `<pre>` element keeps it. The filter skips `pre`, `code`, `textarea` and `script`. Test in Task 1.
3. **A section with only its own page.** `/a/` with a single leaf `/` folds into one row, which keeps its URL, checkbox, badges and `data-blocked`. A section with its page and one more child does not fold. Test in Task 2.
4. **Screenshot box under "Browser fallback: never" (`mode: fast`).** The box is disabled and its hint says why. A posted `screenshot=on` with `mode=fast` sends no `screenshot` (the API would refuse it with 422). Tests in Task 5.
5. **Folded sections and selection.** Ticking a folded section ticks every enabled box below it, and the counter counts them. "Select all" ticks inside folded sections too. Under `enforce` a blocked box inside a folded section stays unticked. Browser test in Task 3.

---

### Task 1: Converter: inline runs as paragraphs, PHP error output removed

**Files:**
- Create: `src/text_extraction/convert/server_errors.py`
- Modify: `src/text_extraction/convert/html.py` (`convert_html`, a new `wrap_inline_runs` step next to `flatten_deep_tables`)
- Modify: `src/text_extraction/extract_models.py` (`warnings` description), `docs/architecture.md` (the warning code list near line 166), `src/text_extraction/ui/locales/en.json` and `de.json` (`warning.server_errors_removed`)
- Test: `tests/test_convert_html.py`, new `tests/test_convert_server_errors.py`

**Interfaces:**
- Produces:
  - `SERVER_ERRORS_REMOVED_WARNING: Final = "server_errors_removed"` in `convert/server_errors.py`.
  - `def strip_server_errors(soup: BeautifulSoup) -> int` in `convert/server_errors.py`: removes PHP error output in place and returns how many messages it removed.
  - `def wrap_inline_runs(soup: BeautifulSoup) -> None` in `convert/html.py`: in place.
  - `convert_html` adds `SERVER_ERRORS_REMOVED_WARNING` to `Conversion.warnings` once when `strip_server_errors` removed one or more messages.

**Root cause (reproduced on 2026-11-06 with Trafilatura 2.3.1):**
- A block container (`div`, `section`, `article`, `main`, `aside`, `header`, `footer`, `li`, `blockquote`, `figure`, `figcaption`, `form`) whose first meaningful child is an inline element or a `<br>`, with no text before it, is split by Trafilatura. Every inline element becomes its own paragraph, and the space after it is lost:
  - `<div><b>W</b>: msg in <b>/x.php</b> on line <b>1</b></div>` gives `**W**: msg in\n\n**/x.php**on line\n\n**1**`.
  - `<div><br/>Start <b>b</b> danach</div>` gives `Start\n\n**b**danach`.
- The same content wrapped in a `<p>` comes out right: `**W**: msg in **/x.php** on line **1**`.

**PHP error output:**
- With `html_errors` on, PHP writes `<br />\n<b>Warning</b>:  message in <b>/var/www/path/file.php</b> on line <b>128</b><br />`.
- With `html_errors` off, it writes the same as text: `Warning: message in /path/file.php on line 128`, optionally prefixed `PHP `.
- The labels are `Warning`, `Notice`, `Deprecated`, `Fatal error`, `Parse error`, `Strict Standards`, `Recoverable fatal error` and `Catchable fatal error`.
- wirlernenonline.de's start page carries 14 such warnings (Jan's screenshots).

- [ ] **Step 1: Write the failing tests**
  - In `tests/test_convert_server_errors.py`:
    - `test_strips_html_php_warnings`: a body with two PHP warnings in the `html_errors` form, followed by `Mechatronik Germanistik` in one `<div>`. `strip_server_errors` returns 2. The soup's text no longer contains `Warning`, `front-page.php` or `on line`, and still contains `Mechatronik Germanistik`.
    - `test_strips_plain_text_php_errors`: `PHP Fatal error:  Uncaught Error in /srv/app/index.php on line 7` and `Notice: Undefined index: x in C:\\inetpub\\site\\a.php on line 3` in a `<div>` with other text. Returns 2, and the other text stays.
    - `test_keeps_php_error_text_in_code_and_pre`: the same message inside `<pre>` and inside `<code>`. Returns 0, and the text stays.
    - `test_keeps_prose_that_mentions_warnings`: `<p>Warning: do not touch the wire in the lab.</p>` (no path, no line). Returns 0.
    - `test_linear_on_hostile_input`: `"<b>Warning</b>: " + "a " * 200_000` without ` on line`. Finishes in under 1 s and returns 0.
  - In `tests/test_convert_html.py`:
    - `test_inline_runs_after_br_stay_one_paragraph`: `<div><br/>Start <b>b</b> danach</div>` (plus the filler paragraph the module's tests use, so Trafilatura keeps the main content) gives Markdown containing `Start **b** danach`.
    - `test_leading_inline_element_keeps_spaces`: `<div><a href="https://e.org/z">W</a> danach text <b>x</b> mehr</div>` gives `[W](https://e.org/z) danach text **x** mehr`.
    - `test_mixed_block_and_inline_children`: `<div><b>W</b>: msg<div>Inner <b>z</b> end</div>tail <i>t</i> after</div>` gives the three paragraphs `**W**: msg`, `Inner **z** end` and `tail *t* after`, in that order.
    - `test_php_warnings_removed_and_reported`: the PHP case of `test_strips_html_php_warnings` through `convert_html` gives no `Warning` in the content, `Mechatronik Germanistik` in it, and `"server_errors_removed"` in `warnings`.
    - `test_page_without_server_errors_has_no_warning`: ordinary content has no `server_errors_removed`.
- [ ] **Step 2: Run them:** `uv run pytest tests/test_convert_server_errors.py tests/test_convert_html.py -q`. The new tests FAIL.
- [ ] **Step 3: Implement**
  - **`strip_server_errors`**
    - Walk the text of each block (the `html_errors` form spans `<b>` elements and their tails, so match on the concatenated text of a block's direct inline children, not on single strings).
    - Remove the matched nodes, including the `<br>` right before and right after a message.
    - Skip anything inside `pre`, `code`, `textarea` and `script`.
    - The message pattern: an optional `PHP `, one of the eight labels, `:`, whitespace, a message of at most 1000 characters without a newline, ` in `, a path that starts with `/` or a drive letter and `\`, ends in `.php`, `.inc` or `.phtml` and has at most 500 characters, then ` on line ` and digits.
    - Anchor the scan on the label, so the work stays linear on hostile input.
  - **`wrap_inline_runs`**
    - For every block container listed above whose own leading text is empty or whitespace and whose first non-whitespace child is an inline element or a `<br>`, wrap each maximal run of inline children (text, inline elements and `<br>`) in a new `<p>`.
    - A run that is only whitespace and `<br>` is dropped.
    - `p`, table cells (`td`, `th`) and anything inside `pre` keep their structure; Markdown tables must not change.
  - **`convert_html`**
    - Call `strip_server_errors` right after `strip_non_content`, and `wrap_inline_runs` right after `flatten_deep_tables`.
    - The `bs4` fallback uses the same soup, so it profits from both.
  - **Docs and texts**
    - Document the warning in `extract_models.py` and `docs/architecture.md`.
    - Add `warning.server_errors_removed`:
      - en: "Error messages of the site's server (PHP warnings) were removed from the text."
      - de: „Fehlermeldungen des Website-Servers (PHP-Warnungen) wurden aus dem Text entfernt.“
- [ ] **Step 4: Run** `uv run pytest tests/test_convert_html.py tests/test_convert_server_errors.py tests/test_convert_markup.py tests/test_pipeline.py tests/ui/test_i18n.py -q`, then the whole `uv run pytest -q -x`. Everything PASSES, including existing golden outputs. If an existing expected output changes, stop and report it in the report as a concern with the before/after; do not edit the expectation silently.
- [ ] **Step 5: Commit** `fix(convert): keep inline runs as one paragraph and remove PHP error output`.

### Task 2: Site tree model: one-row sections, type open, more URL types, social links

**Files:**
- Modify: `src/text_extraction/page_types.py` (`_SEGMENT_NAMES`, new `guess_url_type`)
- Modify: `src/text_extraction/ui/site_tree.py`
- Test: `tests/test_page_types.py`, `tests/ui/test_site_tree.py`

**Interfaces:**
- Produces in `page_types.py`:
  - `def guess_url_type(url: str) -> PageType | None`:
    - `url_type(url)` first;
    - else `"content"` for a path with a date (`/YYYY/MM/` with YYYY from 1990 to 2099 and MM from 01 to 12, optionally `DD/`, followed by one more segment);
    - else `"content"` for the WordPress queries `?p=<digits>` or `?page_id=<digits>` (the only non-tracking parameter);
    - else None.
  - It is used only for URLs that were not fetched. `url_type` itself does not return `"content"`, because `page_type()` uses it as a signal.
- `_SEGMENT_NAMES` gains:
  - listing: `"author"`, `"authors"`, `"autor"`, `"autoren"`;
  - contact: `"ansprechpartner"`, `"ansprechpersonen"`, `"kontakt-aufnehmen"`, `"get-in-touch"`.
- Produces in `site_tree.py`:
  - `SiteTree.untyped: int`: the shown leaves that were not fetched (`kind` `sitemap` or `linked`) and have no page type.
  - `SiteTree.social: tuple[SocialLink, ...]`, with `@dataclass(frozen=True) class SocialLink: url: str; host: str` (host without `www.`).
  - The constant `TREE_MAX_SOCIAL = 50`.
  - `_guessed` uses `guess_url_type`.
  - A section whose only child is a leaf labelled `/` with no children becomes that leaf, with the section's label. It keeps the leaf's url, kind, badges and fields.

- [ ] **Step 1: Write the failing tests**
  - `guess_url_type`:
    - `https://x.org/2021/05/oer-camp/` gives `"content"`;
    - `https://x.org/2021/05/17/oer-camp/` gives `"content"`;
    - `https://x.org/2021/13/a/` gives None;
    - `https://x.org/?page_id=45952` gives `"content"`;
    - `https://x.org/?p=12` gives `"content"`;
    - `https://x.org/?p=12&cat=3` gives None;
    - `https://x.org/impressum/` gives `"imprint"`;
    - `https://x.org/author/jan/` gives `"listing"`;
    - `https://x.org/ansprechpartner/` gives `"contact"`;
    - `https://x.org/2-dezember/` gives None.
  - In `tests/ui/test_site_tree.py` (build items with the helpers in `tests/ui/site_items.py`):
    - `test_section_with_only_its_page_is_one_row`: sitemap entries `https://x.org/a/` and `https://x.org/b/` and `https://x.org/b/c`. The root `x.org` has the children `a/` (a leaf with url `https://x.org/a/`, no children) and `b/` (a section with the children `/` and `c`).
    - `test_folded_row_keeps_blocked_and_type`: a crawled page `https://x.org/impressum/`, marked blocked through `mark_blocked`. Its one-row node has `page_type == "imprint"` and `blocked` set.
    - `test_untyped_counts_unfetched_leaves_without_type`: entries `/2-dezember/` and `/impressum/`, plus a crawled page without a page type (failed). `untyped == 1`.
    - `test_social_links_collected_once`: a crawled page with external links to `https://www.facebook.com/wlo`, `https://facebook.com/wlo` (a duplicate after dropping `www.`, keep the first), `https://mastodon.social/@wlo` and `https://example.com/x` (not social). `social` holds two links with hosts `facebook.com` and `mastodon.social`.
    - `test_social_links_capped`: 60 distinct social links give 50.
    - `test_social_links_use_category_or_host`: a link with `category="social"` counts, and so does a link stored without `category` (None) whose host is social.
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_site_tree.py tests/test_page_types.py -q`. The new tests FAIL.
- [ ] **Step 3: Implement**
  - Collect social links in `TreeBuilder.add` from `site.links` with `kind == "external"` and (`category == "social"` or, when `category` is None, the module's social host check; make `_is_social` public as `is_social_url`).
  - Do the one-row folding in `_assemble`, after sorting.
  - Count `untyped` in `tree()`.
  - Update the module docstring (the type sentence and the one-row rule).
- [ ] **Step 4: Run** `uv run pytest tests/ui tests/test_page_types.py -q` and `uv run pytest -q -x`. Everything PASSES. Template tests that relied on the two-row form are updated to the one-row form.
- [ ] **Step 5: Commit** `feat(site-tree): one row per lone page, untyped count, more URL types, social links`.

### Task 3: Site tree view: fold buttons, "type open", social media group

**Files:**
- Modify: `src/text_extraction/ui/templates/tree.html`, `src/text_extraction/ui/static/ui.js`, `src/text_extraction/ui/static/ui.css`, `src/text_extraction/ui/locales/en.json`, `de.json`
- Test: `tests/ui/test_jobs_views.py` (markup), new `tests/ui/test_tree_fold_browser.py` (`pytest -m browser`), `tests/ui/test_a11y_browser.py` (its tree state gets a folded section, a "type open" leaf and a social link)

**Interfaces:**
- Consumes `SiteTree.untyped`, `SiteTree.social`, `SocialLink` and the one-row nodes (Task 2).
- **Markup:**
  - Every node with children gets `<button type="button" class="fold" aria-expanded="true" aria-controls="<id of its child ul>" hidden>` before its label, with the accessible name `t("tree.fold", section=node.label)`. The child `<ul>` gets a unique `id`.
  - The tools row gets `data-action="tree-unfold-all"` and `data-action="tree-fold-all"` buttons, also `hidden` until JS.
  - A leaf with `kind` in (`sitemap`, `linked`) and no `page_type` gets `<span class="page-type page-type-open" title="{{ t('tree.type_open_reason') }}">{{ t("tree.type_open") }}<span class="visually-hidden">: {{ t("tree.type_open_reason") }}</span></span>` inside its label.
  - The types line ends with `t("tree.type_open") N` when `tree.untyped`.
  - **Social media:** after the `</ul>` of the tree and before `.tree-actions`, a `<section class="tree-social" aria-labelledby=…>` with an `h5` `t("tree.social_title")`, a hint `t("tree.social_hint")` and a `<ul>` of `<a href rel="noopener noreferrer nofollow" target="_blank">` links. The link text is the URL, followed by the visually hidden `help.new_tab` text. The section appears only when `tree.social`, and has no checkboxes.
- **`ui.js` (in the `htmx:load` initialisation of a `.tree-view`):**
  - Reveal the fold buttons and the two tool buttons.
  - Fold every node below the top level (F1 a): the host roots stay open, and every section inside a root is folded, with `aria-expanded="false"` and the child `ul` `hidden`.
  - Clicking toggles the node. Unfold all and fold all set every button.
  - Selection logic (section ticks, "Select all", "Select content pages", the counter, blocked boxes under `enforce`) keeps working on hidden boxes.
- **CSS:**
  - The fold button is a 24×24 px target with a ▸/▾ indicator from `aria-expanded`, with no layout shift.
  - The "type open" pill uses the muted pill style.
  - The social list wraps on 390 px.
- **Texts (en / de):**
  - `tree.fold`: "Pages under {section}" / „Seiten unter {section}“.
  - `tree.unfold_all`: "Unfold all" / „Alle aufklappen“.
  - `tree.fold_all`: "Fold all" / „Alle zuklappen“.
  - `tree.type_open`: "Type open" / „Typ offen“.
  - `tree.type_open_reason`: "Not fetched; its type shows when it is extracted" / „Nicht abgerufen; der Typ zeigt sich beim Extrahieren“.
  - `tree.social_title`: "Social media" / „Soziale Medien“.
  - `tree.social_hint`: "Profiles the site links to. They are outside the site, so they cannot be selected." / „Profile, auf die die Website verlinkt. Sie liegen außerhalb der Website und lassen sich nicht auswählen.“

- [ ] **Step 1: Write the failing tests**
  - In `tests/ui/test_jobs_views.py`:
    - `test_tree_has_fold_buttons_for_sections`: every `li` with a child `ul` has one `button.fold[hidden][aria-expanded=true]` whose `aria-controls` names that `ul`'s id. Ids are unique.
    - `test_tree_marks_type_open`: an unfetched leaf without a type has `.page-type-open` with the visually hidden reason, and the types line counts it.
    - `test_tree_lists_social_links`: the `.tree-social` links have `rel` containing `noopener`, and there is no checkbox inside `.tree-social`.
    - `test_tree_without_social_has_no_section`.
  - In `tests/ui/test_tree_fold_browser.py` (`pytest.mark.browser`, the same app fixture as `tests/ui/test_tree_rules_browser.py`):
    - `test_top_level_open_sections_folded`;
    - `test_fold_button_toggles_with_keyboard` (Tab to it, Space unfolds, `aria-expanded` follows);
    - `test_unfold_all_and_fold_all`;
    - `test_ticking_a_folded_section_ticks_its_pages` (the counter counts them);
    - `test_select_all_reaches_folded_pages_but_not_blocked_under_enforce`.
  - `tests/ui/test_a11y_browser.py`: the tree state includes the new parts, once folded and once unfolded.
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_jobs_views.py -q` and `uv run pytest -m browser tests/ui/test_tree_fold_browser.py -q`. The new tests FAIL.
- [ ] **Step 3: Implement** the template, JS, CSS and texts.
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`. Everything PASSES, and axe is clean.
- [ ] **Step 5: Commit** `feat(ui): fold the site tree, mark untyped URLs, list social media links`.

### Task 4: Workspace frame: the settings as a card, an empty result card

**Files:**
- Modify: `src/text_extraction/ui/templates/app.html`, `src/text_extraction/ui/static/ui.css`, `src/text_extraction/ui/locales/en.json`, `de.json`
- Test: `tests/ui/test_shell.py` (markup), `tests/ui/test_layout_browser.py` (browser), `tests/ui/test_a11y_browser.py` (the empty state at 1440 and 390 px)

**Interfaces:**
- **Markup:**
  - `#result` starts with `<div class="result-empty"><p>{{ t("result.empty") }}</p></div>`.
  - Every swap into `#result` (htmx `innerHTML`) replaces it, so no JS is needed.
- **CSS:**
  - `.settings` becomes a card: `var(--paper)` background, the `.card` border and radius, and the padding of the result cards.
  - `.result-empty` is a card with a 1 px dashed `var(--line)` border, muted text, a min-height of 12rem, and the text centred.
  - `.result:empty { display: none }` goes.
  - Below 60rem both cards stack with the existing gap; the top border rule on `.workspace .result` goes, because the cards separate the columns.
- **Texts (en / de):** `result.empty`: "The result appears here. Fill in the form on the left and start." / „Hier erscheint das Ergebnis. Formular links ausfüllen und starten.“ (impersonal, as the playground's German texts are).

- [ ] **Step 1: Write the failing tests**
  - `tests/ui/test_shell.py`: `test_result_starts_with_empty_state` (the signed-in shell has `#result .result-empty` with the text in both languages).
  - `tests/ui/test_layout_browser.py`:
    - `test_settings_and_empty_result_are_framed`: at 1440 px the computed `border-top-width` of `.settings` and `.result-empty` is at least 1px, both are visible, and they sit side by side (`.result-empty`'s left edge is greater than `.settings`'s right edge).
    - `test_empty_state_replaced_by_result`: after the info tab's submit, `.result-empty` is gone and `.result-view` is there.
    - `test_cards_stack_at_390`.
- [ ] **Step 2: Run** them. They FAIL.
- [ ] **Step 3: Implement.** Check `tests/ui/test_palette.py`: the empty-state text on its background meets 4.5:1. Add the pair to the palette test if that test lists pairs explicitly.
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`. Everything PASSES.
- [ ] **Step 5: Commit** `feat(ui): frame the settings and show an empty result card`.

### Task 5: "Also take a screenshot" in the Extract form

**Files:**
- Modify: `src/text_extraction/ui/templates/forms/extract.html`, `src/text_extraction/ui/forms.py` (`_extract_fields`), `src/text_extraction/ui/templates/result.html`, `src/text_extraction/ui/static/ui.js`, `src/text_extraction/ui/locales/en.json`, `de.json`
- Test: `tests/ui/test_operations.py` (body building), `tests/ui/test_jobs_views.py` or the module that renders extract results (`grep -rln "render_result" tests/ui`), new browser test in `tests/ui/test_playground_browser.py`

**Interfaces:**
- **Form:**
  - In the "Also" fieldset after "Metadata": `<label class="inline"><input type="checkbox" name="screenshot" value="on" aria-describedby="extract-screenshot-hint extract-screenshot-fast-hint"> {{ t("form.screenshot") }}</label>`.
  - Then `<p class="hint" id="extract-screenshot-hint">{{ t("hint.screenshot_also") }}</p>` and `<p class="hint" id="extract-screenshot-fast-hint" hidden>{{ t("hint.screenshot_needs_browser") }}</p>`.
  - When `browser_enabled` is false, the box is rendered `disabled` and the second hint is shown.
- **`_extract_fields`:** `screenshot` on and `mode` not `fast` gives `fields["screenshot"] = _Field(True, set_by_user=True)`. With `mode == "fast"` it sends nothing (Review Focus 4). There is no `screenshot_options`: the API's default is a full-page PNG, 1920 px wide.
- **`ui.js`:** in the extract form, while the mode select is `fast`, the box is `disabled` and unticked, and the fast hint is shown; otherwise the box is enabled and the hint hidden. This runs on load and on `change`.
- **Result:**
  - For `op == "extract"` with a `screenshot` (or a `screenshot_note`), the image goes into a closed `<details class="fold">` titled `t("result.screenshot_title")`, placed after the facts card. Use the same fold markup as "Metadata".
  - Other operations (the Screenshot tab) keep today's card at the top.
- **Texts (en / de):**
  - `form.screenshot`: "Also take a screenshot" / „Screenshot mitliefern“.
  - `hint.screenshot_also`: "The whole page as a PNG. Loads the page in the browser, so it takes longer. Format and size are in the Screenshot tab." / „Die ganze Seite als PNG. Lädt die Seite im Browser und dauert daher länger. Format und Größe gibt es im Reiter Screenshot.“
  - `hint.screenshot_needs_browser`: "Needs the browser: not with “Browser fallback: never”, and not while the browser is off." / „Braucht den Browser: nicht mit „Browser-Fallback: nie“ und nicht, solange der Browser aus ist.“
  - `result.screenshot_title`: "Screenshot" / „Screenshot“.
  -

- [ ] **Step 1: Write the failing tests**
  - `tests/ui/test_operations.py`:
    - `test_extract_screenshot_box_sends_screenshot`: `mode=auto` and `screenshot=on` give `"screenshot": True` in the body.
    - `test_extract_screenshot_ignored_in_fast_mode`.
    - `test_extract_without_box_sends_no_screenshot`.
  - Result rendering: `test_extract_result_shows_screenshot_in_a_fold`. An extract envelope plus a `screenshot` data URI gives `details.fold` with the `img.screenshot` inside, placed after `dl.facts`. The screenshot op keeps the top card.
  - Template: `test_extract_screenshot_box_disabled_without_browser` (`BROWSER_ENABLED=false`).
  - Browser (`tests/ui/test_playground_browser.py`): `test_screenshot_box_follows_mode`. With mode `fast` the box is disabled and the hint is visible; with `auto` it is enabled.
- [ ] **Step 2: Run** them. They FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`. Everything PASSES, and axe is clean.
- [ ] **Step 5: Commit** `feat(ui): take a screenshot along with an extraction`.

### Task 6: Docs, changelog, package row

**Files:**
- Modify:
  - `docs/user-guide/en/part-of-a-site.md` and `de/` (folding, one-row sections, "type open", the guessed types, the social media list);
  - `docs/user-guide/en/single-url.md` and `de/` (the screenshot box, and that PHP error output is removed, with the warning);
  - `docs/user-guide/en/usage.md` and `de/` (the empty result card, if the page describes the layout);
  - `CHANGELOG.md` (`[Unreleased]`: Added, Changed, Fixed);
  - `docs/packages.md` (the contents row and the Status row 17, as for 16).
- Test: `uv run pytest tests/test_docs_i18n.py tests/test_handover_doc.py -q`, `uv run python scripts/check_translation_pairs.py`.

- [ ] **Step 1:** Write the docs. Keep the existing style: short sentences, „einhalten“, no API change claimed beyond the warning.
- [ ] **Step 2:** Run the doc checks. They PASS.
- [ ] **Step 3: Commit** `docs: package 17 (tree folding, types, screenshot box, server errors)`.

## After the tasks

- The final whole-branch review (see `references/final-review.md`) and one fix wave.
- Open the PR (draft first).
- Label it `live` once for the WLO regression run. Pass: wlo_200 auto at or above 83 %, against the baseline of 85.0 % for package 14 and the usual ±2 points. If it lands below that, A/B the `wrap_inline_runs` step with `service_ref` before merging.
- Write the PC check for Jan in `/mnt/project-files/package-17/pc-pruefung-paket-17.md`.
