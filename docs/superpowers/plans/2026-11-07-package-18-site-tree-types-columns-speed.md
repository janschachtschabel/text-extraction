# Package 18: site tree with types, columns and speed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix what Jan found when testing v0.1.5 (2026-11-07 01:04). His reference was his own harvester, `janschachtschabel/metadata-agent-harvester`. He found three problems:
- the site tree shows "Type open" for almost every URL that was not fetched;
- the badges do not line up;
- reading a site's tree is slower than in the reference.

**Architecture:**
- **Typing without fetching.** One pure function, `page_types.guess_type`, types a URL that was not fetched. It reads, in a fixed order:
  - the URL;
  - WordPress query patterns;
  - a hint from the sitemap file the URL came from;
  - the text and the region of the link that found it;
  - education keywords;
  - and, failing all that, a default of "content" for a path made of words.

  It also says which of these decided the type. Two new page types join: `event` and `learning_material`.
- **Sitemaps.** A sitemap item stores the hint its file name gives (`SitemapInfo.type_hint`). When robots.txt lists no sitemap, three addresses are guessed in turn.
- **Discovery option.** The site job gets `discovery: "crawl" | "sitemap_first"`. With `sitemap_first` and a sitemap, only the start page is crawled.
- **Tree view.**
  - The tree model carries the guess's reason, the link text as a title, and the counts per source and section.
  - The view is a grid with fixed columns (page | type | found | status | depth).
  - Filter chips by type.
  - The tree loads by itself and refreshes while the job runs.
- **Spacing.** The default per-host spacing drops from 1000 to 500 ms.
- **Light crawl pages.** They come only if a measurement shows that conversion costs matter (Task 9 gate).

**Tech Stack:** Python 3.13, FastAPI, pydantic, lxml, Jinja2, htmx, plain JS (`ui.js`), pytest, pytest-httpserver, Playwright (Chromium) for `-m browser`, axe-core 4.14.0.

**Spec:** `/mnt/project-files/package-18/plan-paket-18.md` (German) and its mockup `/mnt/project-files/package-18/mockup-baum.png`. Jan approved both on 2026-11-07 at 01:09 ("ja so ist es richtig - ist angenommen"), with the recommended options:
- G1 b: new types `event` (Veranstaltung) and `learning_material` (Lernmaterial).
- G2 a: a path of words with no other hint is "content (guessed)". "Type open" remains only for opaque URLs.
- G3 a: the UI defaults to "Fast: sitemap first", and "Thorough: follow links" can be chosen. The API keeps `crawl` as its default.
- G4 a: `OUTBOUND_MIN_DELAY_MS` defaults to 500. robots.txt `Crawl-delay` still wins.
- G5 a: v0.1.6 after Jan's PC check.

## Global Constraints

- **Languages:**
  - English in code, comments, developer docs and commit messages.
  - Every English UI text in `ui/locales/en.json` has its German partner in `de.json`, and every changed `docs/user-guide/en/` page changes its `de/` partner in the same task. The pairs are checked by `tests/test_docs_i18n.py`, `tests/ui/test_i18n.py` and `scripts/check_translation_pairs.py`.
  - German UI texts use „Sie“ or impersonal forms, never „du“.
- **German names of the new types:** `page_type.event` = „Veranstaltung“, `page_type.learning_material` = „Lernmaterial“. English: "Event", "Learning material".
- **A guessed type is always marked as guessed:**
  - in the tree: a dashed pill, a "~" after the name, and a visually hidden „geschätzt“ / "guessed";
  - its reason is readable as text, not only in a `title`.
- **API changes, and only these:**
  - `PageType` gains `event` and `learning_material`;
  - `SitemapInfo.type_hint` (`PageType | None`) is new;
  - `SiteNode.guess` (`int | None`) is new;
  - `Crawl` gains `sitemap_first`;
  - `SiteOptions.discovery` is new, `Literal["crawl", "sitemap_first"]`, default `"crawl"`, also in the MCP `site_structure` tool.

  Every new field is documented in its `Field(description=...)` and in `docs/architecture.md`.
- **Respect sites (CLAUDE.md rule 6):**
  - every new fetch (sitemap guesses, the `sitemap_first` probe) goes through `Pipeline.fetch_raw` (SSRF guard, compliance, host turn);
  - no fetch bypasses the per-host gate;
  - `Crawl-delay` keeps precedence over `OUTBOUND_MIN_DELAY_MS`.
- **Accessibility:**
  - the tree stays a list of checkboxes, and the columns are layout only;
  - every cell has a visually hidden column name;
  - filter chips are `button`s with `aria-pressed`;
  - the live refresh announces nothing per refresh and does not move the focus;
  - axe-core stays clean at 1440 and 390 px in `en` and `de`;
  - every new control is reachable with Tab and works with Enter/Space.
- **Progressive enhancement:** without JavaScript the tree shows all nodes unfolded, no chips (the type counts remain as text), no auto refresh (the existing "Show tree now" link stays), and the form posts.
- **Quality gates per task:** `uv run pytest` (the task's tests, plus `tests/ui` when UI changed, plus `-m browser` for browser tests the task touches), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`.

## Review Focus

1. **A strict URL rule beats every guess.**
   - `/impressum/` listed in `page-sitemap.xml` with the link text "Mehr" is `imprint`, reason `url`.
   - `/datenschutz/` in `post-sitemap.xml` is `privacy`.
   - Test in Task 2.
2. **Opaque URLs stay open.** `/?id=8f3a2c`, `/12345/`, `/a1b2c3d4e5f6/` and `/` with an unknown query have no guess, also with an empty link text. Test in Task 2.
3. **Keyword matches are whole tokens.** `/eventually-later/` and `/pretermination/` are not `event` (no whole token); `/termine-und-fristen-im-recht/` is `event` (`termine` is a token) and stays so as documented behaviour of a guess. Test in Task 2.
4. **A refresh keeps the user's work.** Ticked boxes, folded sections, the active type filter and the scroll position survive an auto refresh. A box ticked inside a section survives when new children appear in that section. Browser test in Task 7.
5. **`sitemap_first` without any sitemap crawls normally.** No robots `Sitemap:` line, and every guessed address 404s: the crawl follows links as with `crawl`, and no link says `sitemap_first`. With a sitemap, the start page's links say `sitemap_first` and nothing else is fetched besides robots.txt, the sitemaps and the start page. Test in Task 4.

---

### Task 1: Live measurement of the tree, WLO included

**Files:**
- Modify: `tests/live/urls/sites.csv` (add `https://wirlernenonline.de/,WLO itself (WordPress)`)
- Modify: `tests/live/run_site.py` (the summary), `tests/live/README.md`
- Test: `tests/live/test_run_site.py` (exists? if not, create `tests/test_live_run_site.py` next to the other harness tests; find them with `rg -l "run_site" tests`)

**Interfaces:**
- Produces: for each site, the summary gains these columns:
  - `sitemap` (`listed`, `guessed`, `none`), from the robots item's `site.sitemaps` and the sitemap items' `guessed`/`format`;
  - `tree URLs`, `type open`, `type open %`: computed by `text_extraction.ui.site_tree.build_tree(items, max_urls=100_000)`, `SiteTree.urls` and `SiteTree.untyped`;
  - `median convert ms` of the page items (`data.timings.convert_ms`);
  - `crawl s`: the job's `finished_at - created_at`.

- [ ] **Step 1:** Write a failing test that feeds `summarize` (or the harness's existing summary function) two fixed job item lists (a site with a listed sitemap and one without), and asserts the new columns and values in the Markdown.
- [ ] **Step 2:** Run it, expect FAIL.
- [ ] **Step 3:** Implement the columns. `CRAWLS` gains `"sitemap_first"` after `"memory_guard"`, so the harness runs against this branch and older services.
- [ ] **Step 4:** Run the test, expect PASS.
- [ ] **Step 5:** Commit `test(live): measure the site tree's open types, sitemap state and timings`.

The controller then dispatches `live.yml` with `service_ref` = `1b133a5` (v0.1.5) to record the baseline (the "before" numbers in the PR).

### Task 2: `guess_type`, the sitemap hint and the two new types

**Files:**
- Modify: `src/text_extraction/page_types.py`
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json` (`page_type.event`, `page_type.learning_material`, `tree.reason.url|sitemap|link_text|keywords|default`)
- Modify: `docs/architecture.md` (page types section: the new types, the order of `guess_type`, ruling P14-R8 replaced by P18-R1)
- Test: `tests/test_page_types.py`

**Interfaces:**
- Produces, in `page_types.py`:
  - `PageType` gains `"event"` and `"learning_material"` (inserted after `"listing"`; `_PAGE_TYPES` order in `ui/site_tree.py` follows `get_args(PageType)`).
  - `GuessReason = Literal["url", "sitemap", "link_text", "keywords", "default"]`
  - `@dataclass(frozen=True) class TypeGuess: type: PageType; reason: GuessReason`
  - `def guess_type(url: str, *, link_text: str = "", region: str | None = None, sitemap_hint: PageType | None = None) -> TypeGuess | None`
  - `def sitemap_type_hint(sitemap_url: str) -> PageType | None`
  - `guess_url_type` stays as it is (its callers move to `guess_type` in Task 5; keep it for other callers, or delete it if `rg guess_url_type` finds none after Task 5).
- `page_type` (fetched pages): the schema.org table `_SCHEMA_TYPES` gains `Event` and its schema.org subtypes ending in `Event` (`EducationEvent`, `BusinessEvent` …: match `@type` ending with `Event`) → `event`; `LearningResource`, `Quiz`, `Course` → `learning_material`.

The order of `guess_type` (first match wins):
1. `url_type(url)` → reason `url`.
2. WordPress and dated patterns → reason `url`:
   - `?p=N`, `?page_id=N` and a dated path → `content` (as `guess_url_type`);
   - `?post_type=X&p=N` → the post type's type (`_post_type_type(X)`: an event name → `event`, a material name → `learning_material`, else `content`);
   - `?cat=`, `?tag=`, `?author=` with a value → `listing`;
   - a last segment `__trashed` or ending in `__trashed` → `other`.
3. `sitemap_hint` when it is not `content` → reason `sitemap`.
4. The link text, whitespace-collapsed and folded, equal to a type word of `_HEADING_TYPE` → that type, reason `link_text`. `region == "pagination"` → `listing`, reason `link_text`.
5. A keyword token in the URL path's tokens (split on `/ - _ .`, folded) or in the link text's word tokens → reason `keywords`. Event tokens are checked before material tokens.
   - `_EVENT_WORDS`: veranstaltung, veranstaltungen, event, events, termin, termine, workshop, workshops, seminar, seminare, webinar, webinare, konferenz, tagung, symposium, fortbildung, fortbildungen, barcamp, fachtag, kongress, messe, meetup, hackathon.
   - `_MATERIAL_WORDS`: arbeitsblatt, arbeitsblaetter, unterrichtsmaterial, unterrichtsmaterialien, lernmaterial, lernmaterialien, worksheet, worksheets, quiz, uebung, uebungen, lernvideo, lernvideos, lernpfad, lernpfade, tutorial, oer.
6. `sitemap_hint == "content"` → reason `sitemap`.
7. The default → `content`, reason `default`. It applies when every rule below holds:
   - the URL has no query besides tracking and language parameters;
   - the last path segment's stem holds a run of at least 3 letters;
   - the stem is not a hex or digit id: not `^[0-9a-f]{8,}$`, not all digits, not a UUID.
8. Otherwise None.

`sitemap_type_hint` reads the file name of the sitemap URL (lower case, `.gz` and `.xml` stripped):
- Yoast and Rank Math `<name>-sitemap<N>`. The name maps like this:
  - `post` or `page` → `content`;
  - `category`, `post_tag`, `tag`, `author`, `product_cat`, or any name ending in `_tag`, `_category` or `_cat` → `listing`;
  - an event name (`event`, `events`, `tribe_events`, `veranstaltung`, `veranstaltungen`) → `event`;
  - a material name (any `_MATERIAL_WORDS` token) → `learning_material`;
  - else None.
- WordPress core:
  - `wp-sitemap-posts-<type>-<N>` → the same name rule;
  - `wp-sitemap-taxonomies-…` and `wp-sitemap-users-…` → `listing`.
- Else None.

- [ ] **Step 1: Write failing tests** in `tests/test_page_types.py`, parametrized, each asserting `guess_type(...) == TypeGuess(type, reason)` or `None`:
  - `/impressum/` with `sitemap_hint="content"`, `link_text="Mehr"` → (`imprint`, `url`) — Review Focus 1
  - `/datenschutz/` with `sitemap_hint="content"` → (`privacy`, `url`)
  - `/?post_type=portal&p=21528` → (`content`, `url`); `/?post_type=event&p=3` → (`event`, `url`); `/?cat=7` → (`listing`, `url`); `/foo__trashed/` and `/__trashed/` → (`other`, `url`)
  - `/x/` with `sitemap_hint="listing"` → (`listing`, `sitemap`)
  - `/kontakt-aufnehmen-und-mehr/` with `link_text="Kontakt"` → (`contact`, `link_text`); `/seite-3/` with `region="pagination"` → (`listing`, `link_text`)
  - `/3-online-symposium-begabungsfoerderung/` → (`event`, `keywords`); `/8-monate-wirlernenonline/` with `link_text="Arbeitsblatt Bruchrechnen"` → (`learning_material`, `keywords`); `/termine-und-fristen-im-recht/` → (`event`, `keywords`); `/eventually-later/` and `/pretermination/` → (`content`, `default`) — Review Focus 3
  - `/8-monate-wirlernenonline/` with `sitemap_hint="content"` → (`content`, `sitemap`)
  - `/achtsamkeit-gegen-stress-in-der-schule/` → (`content`, `default`)
  - None for `/?id=8f3a2c`, `/12345/`, `/a1b2c3d4e5f6/`, `/550e8400-e29b-41d4-a716-446655440000/`, `/?foo=bar`, `/` + `?sessionid=1` — Review Focus 2
  - `sitemap_type_hint`: `https://x.de/post-sitemap.xml` → `content`; `/page-sitemap2.xml` → `content`; `/category-sitemap.xml` → `listing`; `/post_tag-sitemap.xml` → `listing`; `/tribe_events-sitemap.xml` → `event`; `/wp-sitemap-posts-post-1.xml` → `content`; `/wp-sitemap-posts-veranstaltung-1.xml` → `event`; `/wp-sitemap-taxonomies-category-1.xml` → `listing`; `/wp-sitemap-users-1.xml` → `listing`; `/sitemap.xml` → None; `/portal-sitemap.xml` → None; `/sitemap.xml.gz` → None
  - `page_type` of a fetched page whose JSON-LD `@type` is `EducationEvent` → `event`, with `schema_org` in its signals; `LearningResource` → `learning_material`
- [ ] **Step 2:** Run them, expect FAIL.
- [ ] **Step 3:** Implement in `page_types.py`. Add the two locale keys per type and the five reason texts. The reason texts:
  - en: "from the URL", "from the sitemap", "from the link text", "from keywords", "a page with a name, not fetched".
  - de: „aus der URL“, „aus der Sitemap“, „aus dem Linktext“, „aus Schlüsselwörtern“, „Seite mit Namen, nicht abgerufen“.
- [ ] **Step 4:** Run the tests and `uv run pytest tests/test_page_types.py tests/ui -q`. Expect PASS: existing tests that list `PageType` values and the OpenAPI snapshot, if any, are updated deliberately.
- [ ] **Step 5:** Commit `feat(page-types): guess_type with reasons, sitemap hints, event and learning material`.

### Task 3: Sitemap type hint and three guessed sitemap addresses

**Files:**
- Modify: `src/text_extraction/site/types.py` (`SitemapInfo.type_hint`, `SiteNode.guess`)
- Modify: `src/text_extraction/site/sitemap_items.py` (`_robots`, `_sitemap`, and the parts built in `pack_parts`'s caller)
- Modify: `docs/architecture.md`, `docs/configuration.md` (the `sitemaps` text), `src/text_extraction/site/types.py` (`SiteOptions.sitemaps` description)
- Test: `tests/site/test_sitemap_items.py` (or the module's existing test file: `rg -l "_robots\|SITEMAP_NOT_FOUND" tests`)

**Interfaces:**
- Produces:
  - `SitemapInfo.type_hint: PageType | None = Field(default=None, description="The page type the sitemap's file name suggests for its entries (WordPress, Yoast, Rank Math), else null.")`. It is set on every part of a sitemap and on an index.
  - `SiteNode.guess: int | None = None`: the position in `GUESSED_SITEMAP_PATHS` of a guessed sitemap.
  - `GUESSED_SITEMAP_PATHS: Final = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")` in `sitemap_items.py`.
- Behaviour:
  - When robots.txt lists no sitemap, the robots item appends only the first guess (`guessed=True, guess=0`).
  - A guessed sitemap that answers 404 or 410 stays `format: absent`, as today, and appends the next guess (`guess + 1`, `depth=0`, `parent` = its own index) while one is left. It uses `_extend`, so a rerun stores the same answer.
  - The first guess that answers is parsed as today.
  - A guess that the SSRF guard or compliance blocks ends the chain (no next guess).

- [ ] **Step 1:** Write failing tests:
  - `test_unlisted_sitemap_tries_three_addresses_in_turn`: pytest-httpserver, no `Sitemap:` line, `/sitemap.xml` and `/sitemap_index.xml` answer 404, and `/wp-sitemap.xml` is an index with `wp-sitemap-posts-post-1.xml` (2 URLs) and `wp-sitemap-taxonomies-category-1.xml` (1 URL). Assert:
    - three sitemap items with `guess` 0, 1, 2;
    - the third's index children are parsed;
    - their `type_hint` is `content` and `listing`;
    - every entry is in the job.
  - `test_first_guess_found_stops_the_chain`: `/sitemap.xml` answers. Assert that there is no item for `/sitemap_index.xml`.
  - `test_listed_sitemap_gets_its_type_hint`: robots lists `/post-sitemap.xml`. Assert that the sitemap item's `type_hint` is `content`, and that every part's (with `JOB_ITEM_MAX_BYTES` small enough for 2 parts) is too.
- [ ] **Step 2:** Run them, expect FAIL.
- [ ] **Step 3:** Implement. Compute `type_hint` from the item's URL, not from the redirect target (the file name of the URL that was listed or guessed).
- [ ] **Step 4:** Run the tests and the existing site tests (`uv run pytest tests/site -q`), expect PASS.
- [ ] **Step 5:** Commit `feat(site): sitemap type hints and three guessed sitemap addresses`.

### Task 4: `discovery: sitemap_first`

**Files:**
- Modify: `src/text_extraction/site/types.py` (`SiteOptions.discovery`, `Crawl` gains `"sitemap_first"` and its description in `SiteLink.crawl`)
- Modify: `src/text_extraction/site/runner.py` (`_page` for item 0)
- Modify: `src/text_extraction/site/sitemap_items.py` (the probe helper)
- Modify: `src/text_extraction/mcp_server/tools_jobs.py` (the `site_structure` tool's parameter, if the tool lists parameters one by one; else nothing)
- Modify: `docs/architecture.md`, `docs/configuration.md` if it lists site request fields, the user guide pages `docs/user-guide/{en,de}/whole-site-and-metadata.md` and `parameters.md`
- Test: `tests/site/test_runner.py` (or the done-when test file of package 9: `rg -l "site-structure" tests/site`), `tests/mcp/` for the tool schema

**Interfaces:**
- Produces:
  - `SiteOptions.discovery: Literal["crawl", "sitemap_first"] = Field(default="crawl", description="`crawl`: follow links up to `depth` and `max_pages`. `sitemap_first`: when robots.txt lists a sitemap or a guessed sitemap address answers, only the start page is crawled and its links say `crawl: sitemap_first`; otherwise as `crawl`. Needs `sitemaps: true` (422 otherwise).")`
  - `async def SitemapItems._sitemap_exists(self, job: JobRecord, url: str, gate_refusals: Literal["fail", "raise"]) -> bool`:
    - true when `Pipeline.robots_sitemaps(url)` has state `found` with at least one URL;
    - else true when one of `GUESSED_SITEMAP_PATHS`, tried in turn, answers 2xx to `Pipeline.fetch_raw(..., max_bytes=SITEMAP_PROBE_MAX_BYTES, truncate=True)`;
    - `SITEMAP_PROBE_MAX_BYTES: Final = 65_536`.
- Behaviour in `_page` for `item.index == 0` with `options.discovery == "sitemap_first"`:
  - after the start page's fetch, call `_sitemap_exists(final_url or url)`;
  - if true, every link that `judge_links` would make a candidate (`queued`) gets `crawl="sitemap_first"` and no candidate is extended (the robots candidate still is);
  - if false, the crawl continues as `crawl`.
  - Other items never look at `discovery`.
- A `model_validator` on `SiteStructureRequest` refuses `discovery="sitemap_first"` with `sitemaps=False` (422 `invalid_request`).

- [ ] **Step 1:** Write failing tests (pytest-httpserver, real `SiteRunner` as in the package 9 done-when test):
  - `test_sitemap_first_with_listed_sitemap_crawls_only_the_start_page`: robots lists a sitemap with 5 URLs, and the start page links 3 pages. Assert:
    - exactly one page item;
    - its 3 links say `sitemap_first`;
    - the 5 sitemap entries are in the job;
    - the server log shows no request for the 3 linked pages.
  - `test_sitemap_first_with_guessed_sitemap`: no `Sitemap:` line, but `/sitemap.xml` answers. Same assertions.
  - `test_sitemap_first_without_sitemap_crawls_links`: no sitemap anywhere (all three guesses 404). Assert that the 3 linked pages are page items and that no link says `sitemap_first` (Review Focus 5).
  - `test_sitemap_first_needs_sitemaps`: `POST /v1/site-structure` with `discovery: sitemap_first, sitemaps: false` answers 422.
  - MCP: the `site_structure` tool's input schema has `discovery` with both values.
- [ ] **Step 2:** Run them, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them and `uv run pytest tests/site tests/mcp -q`, expect PASS.
- [ ] **Step 5:** Commit `feat(site): discovery sitemap_first crawls only the start page when a sitemap exists`.

### Task 5: Tree model: guesses with reasons, link titles, counts

**Files:**
- Modify: `src/text_extraction/ui/site_tree.py`
- Test: `tests/ui/test_site_tree.py`

**Interfaces:**
- Consumes: `guess_type`, `TypeGuess`, `GuessReason` (Task 2); `SitemapInfo.type_hint` (Task 3); `Crawl` `sitemap_first` (Task 4).
- Produces:
  - `LINKED_ONLY` gains `"sitemap_first"`.
  - `TreeNode` gains:
    - `type_reason: GuessReason | None` (set when `type_guessed`);
    - `title_from_link: bool` (the title is a link's text);
    - for a section: `pages: int` (URL leaves below it) and `section_type: PageType | None` (the type every leaf below shares, guessed or not, else None).
  - `SiteTree` gains `found_counts: Mapping[NodeKind, int]` (shown URLs per kind: `page`, `sitemap`, `linked`).
  - The sources:
    - a sitemap entry gets `guess_type(entry.loc, sitemap_hint=info.type_hint)`;
    - a linked-only URL gets `guess_type(link.url, link_text=link.text, region=link.region)`;
    - the first link to a URL sets its title (`title_from_link=True`), and a crawled page's own title replaces it in `merge`.
  - `SiteTree.untyped` keeps its meaning (shown, not fetched, no type).

- [ ] **Step 1:** Write failing tests:
  - a page item whose links include `/achtsamkeit/` (`crawl: depth`, text "Achtsamkeit") → a leaf with `page_type="content"`, `type_guessed=True`, `type_reason="default"`, `title="Achtsamkeit"`, `title_from_link=True`;
  - the same URL also crawled as a page with title "Achtsamkeit | WLO" → title "Achtsamkeit | WLO", `title_from_link=False`, `type_guessed=False`;
  - a sitemap item with `type_hint="listing"` and entry `/category/news/` → `listing`, reason `url` (strict rule first); entry `/thema-x/` → `listing`, reason `sitemap`;
  - a link with `crawl: sitemap_first` is shown as `linked`;
  - `found_counts == {"page": 1, "sitemap": 2, "linked": 1}` for that fixture;
  - a section `category/` with two `listing` leaves has `pages=2`, `section_type="listing"`; one with mixed types has `section_type=None`.
- [ ] **Step 2:** Run them, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/ui/test_site_tree.py -q`, expect PASS.
- [ ] **Step 5:** Commit `feat(ui): the tree types every URL it can, with reasons and link titles`.

### Task 6: Tree view in columns with type filter chips

**Files:**
- Modify: `src/text_extraction/ui/templates/tree.html`
- Modify: `src/text_extraction/ui/static/ui.css`, `src/text_extraction/ui/static/ui.js`
- Modify: `src/text_extraction/ui/job_views.py` (`site_tree_view`: pass `found_counts`, the duration from `JobView.created_at`/`finished_at`)
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json`
- Test: `tests/ui/test_tree_view.py` (or the existing tree template tests: `rg -l "tree.html\|site-tree" tests/ui`), `tests/ui/test_a11y_browser.py`, `tests/ui/test_playground_browser.py`, `tests/ui/screenshots.py` (the tree shots)

**Interfaces:**
- Consumes: Task 5's `TreeNode` and `SiteTree` fields.
- Layout (the mockup):
  - The tree is still `ul.tree > li`, and each row is a CSS grid `grid-template-columns: minmax(0, 1fr) 10rem 7.5rem 8rem 4.5rem`. Its cells:
    1. `.cell-name`: the fold button, the checkbox label (path in `.label`, then the title in `.title`, italic in „…“ when `title_from_link`);
    2. `.cell-type`: the type pill;
    3. `.cell-found`;
    4. `.cell-status`: the status, or the error code;
    5. `.cell-depth`: right-aligned.
  - The indentation is a padding on `.cell-name` by depth (`--level` custom property set in the template), not the `ul` margin, so the columns stay aligned.
  - A header row `.tree-head` (`aria-hidden="true"`) above the list holds the column names and sticks to the top of the tree's scroll box (`position: sticky`). Every cell starts with a `.visually-hidden` column name and a colon, so a screen reader reads „Typ: Inhalt, geschätzt, aus der URL“.
  - Long paths and titles end in an ellipsis, and the full text is in `title`.
  - The pills use these classes:

    | Class | Types | Background | Text |
    |---|---|---|---|
    | `.type-home` | `home` | `#e0ecff` | `#1c4fa0` |
    | `.type-content` | `content` and `learning_material` | `--accent-wash` | `--accent` |
    | `.type-listing` | `listing` and `search` | `#efe7fb` | `#5b3a99` |
    | `.type-event` | `event` | `--warn-wash` | `--warn` |
    | `.type-legal` | `imprint`, `privacy`, `legal`, `contact`, `about`, `auth` | `--neutral-wash` | `#3d4650` |
    | `.type-error` | `error` | `--error-wash` | `--error` |
    | `.type-open` | `other` and "Type open" | `--paper` | `--muted` |

    Every text and background pair meets 4.5:1. A guessed pill adds `.guessed`: a dashed `currentColor` border, a white background, and a "~" after the name in an `aria-hidden` span.
  - The `lastmod` moves into the `title` of the found cell ("Sitemap, changed 2024-05-01").
  - At 390 px the grid has two columns: name and type. Found, status and depth wrap into a small line under the name (`grid-column: 1 / -1`).
- Header: `tree.summary` en "{urls} URLs · {crawled} crawled · {sitemap} from the sitemap · {linked} only linked", de „{urls} URLs · {crawled} gecrawlt · {sitemap} aus der Sitemap · {linked} nur verlinkt“, plus `tree.duration` "finished in {seconds} s" / „fertig in {seconds} s“ when the job is completed.
- Chips:
  - The `.tree-types` paragraph becomes `div.tree-filter[role=group][aria-label=t("tree.filter")]` with one `button.chip[data-filter-type=<type>|open|all][aria-pressed]` per type count, plus "All" first and "Type open" last.
  - Without JS the buttons are `hidden` and the existing text line stays (`.tree-types` with `.js` hiding it, as the fold buttons do).
  - A click on "All" clears the filter.
  - A click on a type toggles it; several types make a union.
  - A filter hides every leaf `li` whose type is not in it (a `hidden` attribute set by `ui.js`), and every section with no visible leaf. A hidden box keeps its checked state, and "Select content pages" acts on visible boxes only while a filter is active.
- Section rows show `t("tree.section_pages", count=pages)` after the label, and the type pill of `section_type` (guessed style when any leaf is guessed).

- [ ] **Step 1:** Write failing template tests (render `tree.html` from a fixed `SiteTree`):
  - every URL row has the five cells in order;
  - the hidden column names are in both languages;
  - a guessed pill has `guessed`, the "~" and the reason text;
  - a link title is in „…“ in de;
  - the summary line has the counts;
  - the chips exist with `aria-pressed="false"` and `hidden`;
  - a section has its page count.
- [ ] **Step 2:** Write failing browser tests (`-m browser`):
  - with a tree of 3 types, a click on the "Event" chip leaves only event leaves and their sections visible and sets `aria-pressed="true"`; "All" restores everything;
  - the type column's left edge is the same for every visible row (bounding boxes within 1 px) at 1440 px;
  - at 390 px no row overflows horizontally;
  - axe-core is clean in `en` and `de` at 1440 and 390 px with a filter active.
- [ ] **Step 3:** Run them, expect FAIL.
- [ ] **Step 4:** Implement. Update `tests/ui/screenshots.py` so the tree shots show the new view.
- [ ] **Step 5:** Run `uv run pytest tests/ui -q` and `uv run pytest -m browser tests/ui -q`, expect PASS.
- [ ] **Step 6:** Commit `feat(ui): the site tree in aligned columns with type filter chips`.

### Task 7: The tree loads by itself and refreshes while the job runs

**Files:**
- Modify: `src/text_extraction/ui/job_views.py` (`site_tree_view`, the progress panel of a site job)
- Modify: `src/text_extraction/ui/templates/tree.html`, the progress template that shows the "Show tree" link (`rg -n "tree_url" src/text_extraction/ui/templates`)
- Modify: `src/text_extraction/ui/static/ui.js`
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json` (`tree.live`: "Updates while the job runs" / „Wird aktualisiert, solange der Job läuft“)
- Test: `tests/ui/test_job_views.py` (or the file with the site tree view tests), `tests/ui/test_playground_browser.py`

**Interfaces:**
- Produces:
  - `TREE_REFRESH_SECONDS: Final = 4` in `job_views.py`.
  - While the job is not finished, the tree section has `hx-get="/ui/run/site-tree?id=…&live=1" hx-trigger="load delay:4s" hx-swap="outerHTML"` on a wrapper.
  - A finished job's tree has no trigger.
  - With JS, a site job's progress panel loads the tree into its `.tree-slot` once the job has at least one sitemap or page result (the first refresh after `progress.done >= 1`). The "Show tree now" link stays for no-JS.
- State kept across swaps, by `ui.js` on `htmx:beforeSwap` / `htmx:afterSwap` of a tree wrapper:
  - the set of ticked `url` values;
  - the URLs and section ids of folded nodes (key: the section's path, not the generated list id);
  - the active filter types;
  - the batch options form values;
  - `scrollTop` of the tree box.

  A refresh does not move the focus. If the focused element is inside the tree, the refresh waits for the next trigger (skip the swap while `document.activeElement` is inside the tree form; `htmx:beforeRequest` cancels).
- A live refresh reads at most `UI_TREE_MAX_URLS` as today. Under `live=1` the rule judgement (`_tree_rules`) is reused from the previous refresh for 30 s per job (an in-process cache keyed by job id and origin set), so a refresh does not load the rule files every 4 s.

- [ ] **Step 1:** Write failing tests:
  - the view of a running job has the trigger and `tree.live`; a completed job's has neither;
  - the progress panel of a running site job has the slot;
  - browser (`-m browser`):
    1. start a site job on pytest-httpserver whose sitemap URLs are served slowly (the page handlers sleep 0.5 s, `OUTBOUND_MIN_DELAY_MS=0`);
    2. wait for the tree;
    3. tick one leaf, fold one section and press the "Content" chip;
    4. wait for the next refresh (a new URL appears);
    5. assert that the tick, the fold, the chip and `scrollTop` are unchanged (Review Focus 4).
- [ ] **Step 2:** Run them, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/ui -q` and the browser tests, expect PASS.
- [ ] **Step 5:** Commit `feat(ui): the site tree shows itself and grows while the crawl runs`.

### Task 8: The "Fast / Thorough" choice in the form, and 500 ms spacing

**Files:**
- Modify: `src/text_extraction/ui/templates/forms/site_structure.html`, `src/text_extraction/ui/forms.py` (if it maps form fields to the body explicitly)
- Modify: `src/text_extraction/settings.py` (`outbound_min_delay_ms` default 500), `.env.example`, `docs/configuration.md`, the spacing example sentences in `docs/configuration.md` (`SITE_MAX_PAGES` row: recompute "100,000 pages at 1 s take about 28 h" for 0.5 s: about 14 h)
- Modify: `src/text_extraction/ui/locales/en.json`, `de.json`, `docs/user-guide/{en,de}/whole-site-and-metadata.md`
- Test: `tests/ui/test_forms.py` (or where site form posting is tested), `tests/test_settings.py`

**Interfaces:**
- The form gets a `fieldset.choice` "How to read the site" / „So wird die Website gelesen“ with radios `discovery`:
  - `sitemap_first` (checked): „Schnell: Sitemap zuerst“ / "Fast: sitemap first", with the hint „Liest robots.txt, die Sitemaps und die Startseite. Ohne Sitemap folgt der Crawl den Links.“ / "Reads robots.txt, the sitemaps and the start page. Without a sitemap the crawl follows links.";
  - `crawl`: „Gründlich: Links folgen“ / "Thorough: follow links", with the hint „Folgt den Links bis zur Tiefe und Seitenzahl unten.“ / "Follows links up to the depth and page count below.".
- Unticking "Also read sitemaps" disables the `sitemap_first` radio and checks `crawl` (`ui.js`). Without JS, the API's 422 is shown as today's form error.

- [ ] **Step 1:** Write failing tests:
  - the rendered form has the `sitemap_first` radio checked, and a posted `discovery=crawl` reaches the request body as `"discovery": "crawl"`.
  - `Settings().outbound_min_delay_ms == 500`.
  - `.env.example` says `OUTBOUND_MIN_DELAY_MS=500`, and `docs/configuration.md` gives `500` (a docs test may already compare defaults: `rg -l "configuration.md" tests`).
- [ ] **Step 2:** Run them, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/ui tests/test_settings.py tests/test_docs_i18n.py -q`, expect PASS.
- [ ] **Step 5:** Commit `feat: fast sitemap-first reading in the UI; per-host spacing 500 ms by default`.

### Task 9: Light crawl pages — measurement gate

**Files:** decided by the measurement.

- [ ] **Step 1:** Read the Task 1 baseline live summary (`median convert ms` of crawl pages) and measure locally: `uv run python -m tests.e2e.run_batch` is not a site run, so write a throwaway script under the SDD workspace (not committed). It runs `Pipeline.run(..., site_links=True)` 50 times on the WLO-like fixture pages in `tests/fixtures` and reports the median `convert_ms` against the median total.
- [ ] **Step 2:** The gate:
  - If the median `convert_ms` is under 150 ms, or under 20 % of the per-page time at 500 ms spacing: record `Ruling: no light crawl mode — conversion is <x> ms of <y> ms per page — cost if wrong: crawls stay CPU-bound on large pages` in the ledger, and add the numbers to `docs/architecture.md` (site structure section). Stop.
  - Otherwise: plan a sub-task. It skips `Trafilatura` for `site_links` runs and takes `word_count` from the visible text of the parsed tree (`quality.visible_word_count`, if it exists, else a new pure helper). It must keep `status`, `page_type`, `title`, `metadata`, the links and `quality.verdict` for `soft_404` unchanged on the fixture corpus (a parametrized test over `tests/fixtures/*.html`, comparing the full and the light run). Implement it with TDD.
- [ ] **Step 3:** Commit the docs note (or the feature) `docs(site): measured cost of converting crawl pages` / `perf(site): light conversion for crawl pages`.

### Task 10: Docs, CHANGELOG, packages, release prep

**Files:**
- Modify: `docs/user-guide/{en,de}/whole-site-and-metadata.md` (the tree: columns, guessed types and reasons, chips, live refresh, Fast/Thorough), `docs/user-guide/{en,de}/glossary.md` (the two new types; "guessed type")
- Modify: `README.md`, `README.de.md` only if they describe the tree or the spacing (`rg -n "Typ offen\|Type open\|1000 ms\|OUTBOUND_MIN_DELAY" README*`)
- Modify: `CHANGELOG.md` (`[Unreleased]`: Added, Changed — the 500 ms default is a behaviour change for operators)
- Modify: `docs/packages.md` (row 18 in both tables: the done-when criterion "the tree types at least 95 % of the shown URLs of wirlernenonline.de in the live run (`type open %` ≤ 5), the tree's columns line up at 1440 and 390 px, a `sitemap_first` reading of wirlernenonline.de finishes in under 30 s in the live run, the UI tests and browser tests pass, and Jan's PC check passes")
- Test: `uv run pytest tests/test_docs_i18n.py tests/ui/test_i18n.py -q`, `uv run python scripts/check_translation_pairs.py`

- [ ] **Step 1:** Update the docs pairs.
- [ ] **Step 2:** Run the doc tests and the pair script, expect PASS.
- [ ] **Step 3:** Commit `docs: package 18 site tree, sitemap-first reading and spacing`.

After the final review, the controller labels the PR `live` (or dispatches `live.yml` on the branch). It compares against the Task 1 baseline (`type open %`, `crawl s` per site, `wlo_200` auto ≥ 83 %) and writes the PC check guide for Jan to `/mnt/project-files/package-18/pc-pruefung-paket-18.md` (German).
