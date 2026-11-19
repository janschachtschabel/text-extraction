# Package 26: Site APIs as link sources, and places in metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A site-structure crawl also reads the content lists a site publishes itself (RSS/Atom
feeds named in the start page's head, the WordPress REST API, Drupal JSON:API), so the tree gets
every entry with its real title and a type from its content type; and every extraction's metadata
lists the places the page declares (`metadata.places`).

**Architecture:** (H1) The start page's typed-link pass (`site/links.py`) also reports the site's
API signals (`SiteApis`: feeds, WordPress API root, Drupal). When the request's `apis` is true and
the operator allows it, the start page (item 0) appends them as `sitemap` items with a `source`
(`feed`, `wordpress`, `drupal`) and, for the APIs, an `api` format. Feeds are read by the existing
XML sitemap parser; the four API formats (`wordpress_types`, `wordpress_posts`, `drupal_index`,
`drupal_nodes`) are parsed by pure functions in a new `site/apis.py` into the same
`ParsedSitemap`, so caps, parts, reruns, the tree and the API reuse the sitemap machinery. A types
or index item lists one child per content type (one level deeper); a list page appends its next
page as a sibling at its own depth. (H2) `metadata.py` reads `Place` records from the page's own
schema.org items and its geo meta tags.

**Tech Stack:** Python 3.13, FastAPI, pydantic, lxml/BeautifulSoup, pytest; no new dependency.

**Spec:** Jan's choice "H1 + H2" on 2026-11-19 (decision card), the findings
`/mnt/project-files/harvester-vergleich/befund-harvester-seiteneinstufung.md` and the German plan
`/mnt/project-files/package-26/plan-paket-26.md` (rulings R1-R5, repeated as R26-1 to R26-5
below).

## Global Constraints

- English in code, comments, developer docs and API texts; `docs/user-guide/en|de/`, `README.md`
  / `README.de.md` and `ui/locales/en.json` / `de.json` change in pairs.
- Every outbound request of the new items goes through `Pipeline.fetch_raw` (SSRF guard incl.
  redirects, robots.txt/TDM under the job's compliance mode, the host's outbound turn, byte cap
  `SITE_SITEMAP_MAX_BYTES`, deadline). No direct httpx call.
- State only in Valkey (the site store); no new state elsewhere.
- New setting `SITE_APIS_ENABLED` (bool, default `true`) documented in `.env.example`,
  `docs/configuration.md`, `deploy/helm/text-extraction/values.yaml` and its `README.md`.
- Result models stay tolerant (ruling AU3-R1): a new Literal value read by an older reader maps to
  a catch-all; new fields have defaults.
- `ruff check .`, `ruff format --check .`, `mypy`, `pytest` green; browser tests for the tree.

## Rulings (record in `docs/architecture.md` under package 26)

- **R26-1** Request field `apis: bool = True` in `SiteOptions` (also the MCP tool and the UI form,
  checkbox on). `SITE_APIS_ENABLED=false` makes the crawl ignore it (no warning); `/v1/info`
  lists the feature `site_apis`.
- **R26-2** Caps: each API list page counts as a sitemap file (`SITE_MAX_SITEMAP_FILES`), each
  entry as a sitemap entry (`SITE_MAX_SITEMAP_URLS`). A 401/403/404 answer fails that item
  (`target_http_error`, not retryable) and the crawl goes on.
- **R26-3** `scope: path`: only content types whose name matches a segment of the start URL's
  path prefix (`path_matches`); `scope: host`: all content types except `EXCLUDED_WP_TYPES`.
- **R26-4** `discovery: sitemap_first` is unchanged: the APIs do not count as a sitemap.
- **R26-5** Only `/v1/site-structure`; `/v1/extract` listing link lists stay as in package 21.
- **R26-6** Only the start page (item 0) reports signals and appends API items; at most
  `MAX_FEEDS = 3` feeds, each on the start page's site host.
- **R26-7** API entries whose host is not the crawl's site host (`site_host`) are dropped (not
  counted).

## Review Focus

1. A WordPress site whose REST API is locked (401/403 from a security plugin) or answers its HTML
   login page with 200: the item fails cleanly (`target_http_error` or `unsupported_content_type`),
   the pages and sitemaps of the crawl are unaffected. Test in Task 4.
2. A site with 20,000 posts: the list pages stop at `SITE_MAX_SITEMAP_FILES` / entries at
   `SITE_MAX_SITEMAP_URLS` with `sitemap_files_truncated` / `sitemap_urls_truncated`, never an
   unbounded chain. Test in Task 4.
3. Hostile JSON: nesting deeper than Python's recursion limit, a `title` of 1 MB, a `link` that is
   `javascript:` or not a string, `X-WP-TotalPages: 999999999`: bounded, dropped, no crash. Tests
   in Task 3.
4. A lost lease reruns an API list item: the next-page child gets the same key and is not
   appended twice. Test in Task 4.
5. The same URL from a sitemap and from the API: one tree node; the API's title fills a missing
   title, and a crawled page's own title and type still win. Test in Task 6.

---

## File Structure

- Create `src/text_extraction/site/apis.py`: API signal detection (`detect_site_apis`), the four
  pure parsers, `path_matches`, `api_type_hint`. Pure: no fetch, no Valkey.
- Modify `src/text_extraction/site/types.py`: `SiteApis`, `SitemapSource`, `ApiFormat`,
  `SitemapFormat` (+4), `SiteNode.source` / `.api`, `SitemapInfo.source`, `SitemapEntry.title`,
  `SiteInfo.apis`, `SiteOptions.apis`.
- Modify `src/text_extraction/site/sitemaps.py`: RSS/Atom entry titles; `ParsedFormat` (+4).
- Modify `src/text_extraction/site/links.py`: `PageLinks.apis`.
- Modify `src/text_extraction/pipeline.py` (~3306): pass `found.apis` into `SiteInfo`.
- Modify `src/text_extraction/site/sitemap_items.py`: API items in `_sitemap`, `source` carried.
- Modify `src/text_extraction/site/runner.py` (`_page`, ~318): append API items from item 0.
- Modify `src/text_extraction/page_types.py`: `GuessReason` + `"api"`, `post_type_hint`.
- Modify `src/text_extraction/settings.py`, `info.py`, `mcp_server/tools_jobs.py`,
  `ui/forms.py`, `ui/site_tree.py`, `ui/results.py`, templates, `ui/locales/*.json`.
- Modify `src/text_extraction/metadata.py`: `Place`, `PageMetadata.places`.
- Docs: `docs/architecture.md`, `docs/configuration.md`, `docs/packages.md`,
  `docs/user-guide/en|de/` (site structure, metadata pages), `README.md`/`README.de.md`
  feature lists, `CHANGELOG.md` `[Unreleased]`, `.env.example`, Helm values and README.

---

### Task 1: Models and API signals of the start page

**Files:**
- Create: `src/text_extraction/site/apis.py`
- Modify: `src/text_extraction/site/types.py`, `src/text_extraction/site/links.py`,
  `src/text_extraction/pipeline.py:3299-3310`
- Test: `tests/site/test_apis.py`, `tests/site/test_types.py`, `tests/site/test_links.py`

**Interfaces:**
- Produces (in `site/types.py`):
  - `SitemapSource = Literal["robots", "guess", "feed", "wordpress", "drupal"]`
  - `ApiFormat = Literal["wordpress_types", "wordpress_posts", "drupal_index", "drupal_nodes"]`
  - `SitemapFormat` gains the four `ApiFormat` values (readers: unknown → `"absent"` is wrong;
    add a tolerant reader `TolerantSitemapFormat = Annotated[SitemapFormat, unknown_as(SitemapFormat, "urlset")]`
    and use it in `SitemapInfo.format`).
  - `class SiteApis(BaseModel)`: `feeds: list[str] = []`, `wordpress: str | None = None` (the API
    root, ending in `/`), `drupal: str | None = None` (`<origin>/jsonapi`). Field descriptions
    for the OpenAPI spec.
  - `SiteNode.source: SitemapSource | None = None`, `SiteNode.api: ApiFormat | None = None`,
    `SiteNode.page: int | None = None` (an API list page: which page, from 1).
  - `SitemapInfo.source: SitemapSource | None = None` (tolerant: unknown → None).
  - `SitemapEntry.title: str | None = None` (whitespace collapsed, at most
    `LINK_TEXT_MAX_CHARS`; `_drop_nulls` keeps it out when null).
  - `SiteInfo.apis: SiteApis | None = None` (description: "The start page only: the feeds and
    APIs its head names.").
  - `SiteOptions.apis: bool = True` (description: "Also read the content lists the site
    publishes itself: RSS/Atom feeds named in the start page's head, the WordPress REST API and
    Drupal JSON:API (when the start page shows them). Each list page counts as a sitemap file,
    each entry as a sitemap entry. Ignored when the operator turned it off.").
- Produces (in `site/apis.py`): `MAX_FEEDS = 3`;
  `detect_site_apis(soup: BeautifulSoup, base_url: str, headers: Mapping[str, str]) -> SiteApis`.
- Produces (in `site/links.py`): `PageLinks.apis: SiteApis` (default empty).

- [ ] **Step 1: Write the failing tests** in `tests/site/test_apis.py`:
  - `test_wordpress_from_link_tag`: head `<link rel="https://api.w.org/" href="https://ex.org/wp-json/">` → `wordpress == "https://ex.org/wp-json/"`.
  - `test_wordpress_from_link_header`: header `link: <https://ex.org/wp-json/>; rel="https://api.w.org/"` → same.
  - `test_wordpress_root_on_other_host_ignored`: `href="https://cdn.other.net/wp-json/"` → `None`.
  - `test_drupal_markers`: each of `<script>…drupalSettings…</script>`, `<div data-drupal-selector="x">`, `<meta name="generator" content="Drupal 10 (https://www.drupal.org)">` → `drupal == "https://ex.org/jsonapi"`; plain page → `None`.
  - `test_feeds_from_head`: rss and atom `<link rel="alternate" type=…>` → both, in document order, absolute and normalized; `type="text/html"` and a `rel="alternate stylesheet"` are ignored; a feed on another host is ignored; five feeds → the first `MAX_FEEDS`.
  - `test_no_signals`: `SiteApis()` equals the result for a bare page.
  - In `tests/site/test_types.py`: `SitemapEntry(loc=…, title=None).model_dump()` has no `title`
    key; `SitemapInfo.model_validate({"format": "future_format"}).format == "urlset"`;
    `SiteOptions().apis is True`.
  - In `tests/site/test_links.py`: `site_links(...)` of a WordPress page has `apis.wordpress` set.
- [ ] **Step 2:** Run `uv run pytest tests/site/test_apis.py tests/site/test_types.py tests/site/test_links.py -q` → FAIL.
- [ ] **Step 3:** Implement the models, `detect_site_apis` (head `<link>` and the `Link` header,
  same site host via `site_host`, `normalize_url`), call it from `site_links` on the same soup,
  and set `SiteInfo(apis=found.apis if found.apis != SiteApis() else None, ...)` in the pipeline.
- [ ] **Step 4:** Tests pass; also `uv run pytest tests/site tests/test_site_api.py -q` (the
  OpenAPI snapshot, if any, updated on purpose).
- [ ] **Step 5:** Commit `feat(site): report feeds and CMS APIs of the start page`.

### Task 2: Titles of feed entries

**Files:**
- Modify: `src/text_extraction/site/sitemaps.py` (the `rss` and `feed` layouts, ~107)
- Test: `tests/site/test_sitemaps.py`

**Interfaces:**
- Consumes: `SitemapEntry.title` (Task 1).
- Produces: `parse_sitemap` fills `title` for `rss` (`item/title`) and `atom` (`entry/title`)
  entries; `urlset` and `text` entries keep `title=None`.

- [ ] **Step 1: Tests:** `test_rss_titles` (two items, titles `"Erster Beitrag"` and a CDATA
  title with `&amp;` → `"Tom & Jerry"`), `test_atom_titles`, `test_title_bounded` (a 1,000-char
  title → 200 chars, whitespace collapsed), `test_urlset_no_title`.
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Map `title` in both layouts; collapse whitespace and cut to
  `LINK_TEXT_MAX_CHARS` while reading (never hold more than that per entry).
- [ ] **Step 4:** `uv run pytest tests/site/test_sitemaps.py -q` passes.
- [ ] **Step 5:** Commit `feat(site): keep the titles of RSS and Atom entries`.

### Task 3: The four API parsers and the type hint

**Files:**
- Modify: `src/text_extraction/site/apis.py`, `src/text_extraction/site/sitemaps.py`
  (`ParsedFormat` gains the four `ApiFormat` values), `src/text_extraction/page_types.py`
- Test: `tests/site/test_apis.py`, `tests/test_page_types.py`

**Interfaces:**
- Produces (in `site/apis.py`), all returning `ParsedSitemap` with `format` set to the API format,
  `error="invalid"` for a body that is no JSON of the expected shape (also on `RecursionError` and
  `ValueError`), entries/children bounded by `max_entries` like `parse_sitemap`:
  - `WP_PER_PAGE = 100`, `DRUPAL_PAGE_LIMIT = 50`, `MAX_API_TYPES = 50`,
    `EXCLUDED_WP_TYPES = frozenset({"attachment", "nav_menu_item", "wp_block", "wp_template", "wp_template_part", "wp_navigation", "wp_global_styles", "wp_font_family", "wp_font_face"})`
  - `wordpress_list_url(api_root: str, rest_base: str, page: int = 1) -> str` →
    `f"{api_root}wp/v2/{rest_base}?per_page=100&_fields=link,title,modified_gmt&page={page}"`
    (`page` left out when 1).
  - `parse_wordpress_types(body: bytes, api_root: str, *, path_prefix: str | None) -> ParsedSitemap`:
    the JSON object's values with a string `rest_base` and a `slug` not in `EXCLUDED_WP_TYPES`;
    with `path_prefix`, only those where `path_matches(slug or rest_base, path_prefix)`; children
    `SitemapChild(url=wordpress_list_url(...), crawl="listed")`, at most `MAX_API_TYPES`.
  - `parse_wordpress_posts(body: bytes, url: str, *, site_host: str, total_pages: int | None, max_entries: int) -> ParsedSitemap`:
    entries `loc=link` (http(s), normalized, on `site_host`), `title=` `title.rendered` with tags
    stripped and entities unescaped, `lastmod=modified_gmt`; `entry_count` counts usable links;
    one child (the next page) when this page is below `total_pages` or, without it, when the page
    held `WP_PER_PAGE` items. The current page is read from the URL's `page` query (default 1).
  - `parse_drupal_index(body: bytes, origin: str, *, path_prefix: str | None) -> ParsedSitemap`:
    the `links` object's `node--*` keys with an `href` on `origin`'s site host; with
    `path_prefix`, only types whose name (after `node--`) matches; child URL
    `f"{href}?page[limit]=50&fields[{key}]=title,path,changed,drupal_internal__nid"` (URL-encoded
    brackets as `urlencode` makes them), at most `MAX_API_TYPES`.
  - `parse_drupal_nodes(body: bytes, origin: str, *, site_host: str, max_entries: int) -> ParsedSitemap`:
    `data[].attributes`: `loc = origin + path.alias` when it starts with `/`, else
    `f"{origin}/node/{drupal_internal__nid}"`; `title`; `lastmod=changed`; one child
    `links.next.href` when it is on `site_host`.
  - `path_matches(name: str, path_prefix: str) -> bool`: fold both (`page_types._fold`), split the
    prefix into segments; true when a segment equals the name, or one starts with the other and
    the shorter has at least 5 characters (`veranstaltungen`/`veranstaltung`, `events`/`event`).
    `event` vs `eventlocations` is true too, accepted.
  - `api_type_hint(api: ApiFormat, url: str) -> PageType | None`: for `wordpress_posts` the
    `rest_base` (path segment after `/wp/v2/`), for `drupal_nodes` the type after `node--`;
    mapped by `page_types.post_type_hint`; other formats None.
- Produces (in `page_types.py`): `post_type_hint(name: str) -> PageType | None` = the existing
  `_sitemap_name_type` rules applied to the name and, failing them, to the name without a
  trailing `s` (`posts` → `post` → content, `pages` → content, `events` → event); and
  `GuessReason` gains `"api"`; `guess_type(..., hint_reason: Literal["sitemap", "api"] = "sitemap")`
  gives that reason where it now gives `"sitemap"`.

- [ ] **Step 1: Tests** (fixtures inline as dicts dumped to bytes):
  - `test_wordpress_types_children`: `post`, `page`, `tribe_events`, `attachment`,
    `wp_block` → three children with `wordpress_list_url` URLs.
  - `test_wordpress_types_path_prefix`: prefix `/veranstaltungen/` keeps only a type with
    `rest_base: "veranstaltung"`.
  - `test_wordpress_posts_entries_and_next`: 100 items, `total_pages=3`, URL without `page` →
    100 entries with titles (`"Fortbildung &#8211; Mathe"` → `"Fortbildung – Mathe"`, `<b>` tags
    stripped) and one child with `page=2`; on `page=3` no child; `total_pages=None` and 40 items
    → no child.
  - `test_wordpress_posts_drops_bad_links`: `javascript:alert(1)`, `42`, a link on another host
    → dropped and not counted.
  - `test_wordpress_posts_hostile`: 1 MB title → 200 chars; JSON nested 100,000 deep →
    `error == "invalid"`; `total_pages=999_999_999` on page 1 → exactly one child (page 2).
  - `test_drupal_index_and_nodes`: index with `node--article`, `node--event`, `file--file` →
    two children; nodes with an alias and one without → `/node/7`; `links.next.href` → child;
    a `next` on another host → no child.
  - `test_invalid_body`: `b"<html>"` and `b"[]"` for a types parser → `error == "invalid"`.
  - `test_path_matches`: the pairs above plus `("kurs", "/veranstaltungen/")` false.
  - In `tests/test_page_types.py`: `post_type_hint("posts") == "content"`,
    `post_type_hint("tribe_events") == "event"`, `post_type_hint("arbeitsblaetter") == "learning_material"`,
    `post_type_hint("product") is None`; `guess_type("https://ex.org/a-b-c/", sitemap_hint="event", hint_reason="api").reason == "api"`.
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Implement. Parse with `json.loads` inside `try` (catch `ValueError`,
  `RecursionError`); never trust types (check `isinstance` at each step).
- [ ] **Step 4:** `uv run pytest tests/site/test_apis.py tests/test_page_types.py -q` passes.
- [ ] **Step 5:** Commit `feat(site): parse WordPress and Drupal content lists`.

### Task 4: API and feed items in the crawl

**Files:**
- Modify: `src/text_extraction/site/sitemap_items.py` (`_sitemap`, `_read_sitemap`,
  `_judge_children`, results built with `SitemapInfo`), `src/text_extraction/site/runner.py`
  (`_page`, after the robots candidate ~318), `src/text_extraction/settings.py` (after
  `site_max_links_per_page`)
- Test: `tests/site/test_runner.py` (existing fakes for the pipeline and the store)

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `Settings.site_apis_enabled: bool = True`.
  - Item 0, when `options.apis and settings.site_apis_enabled` and the page result has
    `site.apis`: candidates after the robots candidate, in this order: each feed
    (`SiteNode(role="sitemap", depth=0, parent=0, source="feed")`), the WordPress types
    (`url=f"{root}wp/v2/types"`, `api="wordpress_types"`, `source="wordpress"`), the Drupal index
    (`url=drupal`, `api="drupal_index"`, `source="drupal"`). Keys by `seen_key(url)`; they count
    against `SITE_MAX_SITEMAP_FILES` like any sitemap item (`max_files`).
  - A sitemap item with `node.api` set: no `looks_like_sitemap` / guess logic; status not 2xx →
    failed as now (401/403/404 not retryable); otherwise the matching parser from Task 3 on the
    runner's pool under the same deadline (`total_pages` from the `x-wp-totalpages` header, an
    int or None; `path_prefix` from `context.scope.path_prefix` when the job's `scope` is
    `path`); `type_hint = api_type_hint(node.api, url)`; entries get `type_guess` from
    `guess_type(entry.loc, link_text=entry.title or "", sitemap_hint=type_hint, hint_reason="api")`.
  - Children: of `wordpress_types` → `api="wordpress_posts"`, of `drupal_index` →
    `api="drupal_nodes"`, one level deeper (nesting rule as now); the next page of a
    `wordpress_posts` / `drupal_nodes` item → same `api`, **same depth**, `page=n+1`. Children
    inherit `source`. Every `SitemapInfo` of an item (and its parts) carries `source=node.source`
    and `format=node.api` for API items.
  - Robots-listed sitemaps get `source="robots"`, guessed ones `source="guess"`, index children
    their parent's source.

- [ ] **Step 1: Tests** (with the runner's existing fake pipeline answering per URL):
  - `test_start_page_appends_api_items`: start page with `site.apis` (one feed, WordPress) →
    items robots, feed, `…/wp-json/wp/v2/types` in that order with the nodes above.
  - `test_apis_false_or_disabled`: `apis=False` → no API items; `site_apis_enabled=False` → none.
  - `test_wordpress_chain`: types → `posts` child (depth 1); posts page 1 with
    `x-wp-totalpages: 2` → 100 entries with titles, `source == "wordpress"`,
    `format == "wordpress_posts"`, a page-2 item at depth 1; page 2 → no further item.
  - `test_api_401_and_html_200`: 401 → `failed`, `target_http_error`, `retryable` false; a 200
    HTML login page → `failed` `conversion_failed` (invalid); the start page and robots items
    are untouched.
  - `test_api_caps`: `site_max_sitemap_files=3` and a site claiming 50 pages → the chain stops,
    the refused child says `max_files` and the item has `sitemap_files_truncated`;
    `site_max_sitemap_urls=150` → `sitemap_urls_truncated`.
  - `test_api_rerun_same_children`: run a posts page item twice (stored answers) → the next page
    is appended once.
  - `test_scope_path_types`: job `scope="path"` from `/veranstaltungen/` → only the matching
    type's child.
  - `test_feed_item_titles`: a feed item → entries with titles, `source == "feed"`, format `rss`.
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Implement. Keep `_sitemap`'s part packing, grants and warnings for API items;
  branch only for fetch validation and parsing. Add the `source` to every `SitemapInfo` built.
- [ ] **Step 4:** `uv run pytest tests/site -q` passes; then `uv run pytest tests/test_site_api.py tests/test_settings.py -q`.
- [ ] **Step 5:** Commit `feat(site): read feeds and CMS content lists as sitemaps`.

### Task 5: Switches: settings docs, info, MCP, UI form

**Files:**
- Modify: `.env.example`, `docs/configuration.md`, `deploy/helm/text-extraction/values.yaml`,
  `deploy/helm/text-extraction/README.md` (wherever `SITE_MAX_SITEMAP_URLS` is documented, add
  `SITE_APIS_ENABLED` next to it), `src/text_extraction/info.py` (feature `site_apis`),
  `src/text_extraction/mcp_server/tools_jobs.py` (site tool param `apis: bool = True` with the
  `SiteOptions.apis` description, passed through), `src/text_extraction/ui/forms.py` and the
  site-structure form template (checkbox `apis`, checked), `ui/locales/en.json`/`de.json`
  (`form.site.apis`: "Read feeds and CMS interfaces" / "Feeds und CMS-Schnittstellen lesen",
  plus a hint line).
- Test: `tests/test_settings.py`, `tests/test_info.py`, `tests/mcp_server/test_tools_jobs.py`,
  `tests/mcp_server/test_option_hints.py`, `tests/ui/test_site_run.py`

- [ ] **Step 1: Tests:** env `SITE_APIS_ENABLED=false` parses; `/v1/info` features hold
  `site_apis` true by default and false when disabled; the MCP site tool forwards `apis: false`;
  the option hints test still covers every field (add the hint for `apis` if the tool keeps it
  in `options`); the UI form posts `apis` and unchecking it sends `false`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; keep the MCP input schema under its size test.
- [ ] **Step 4:** Run the five test files → pass; `uv run python scripts/check_translation_pairs.py` passes.
- [ ] **Step 5:** Commit `feat: switch for site APIs in settings, info, MCP and the form`.

### Task 6: The tree shows API and feed titles and the "api" reason

**Files:**
- Modify: `src/text_extraction/ui/site_tree.py` (sitemap entries, ~285-305), the tree template
  (tooltip for the reason), `ui/locales/en.json`/`de.json` (`tree.reason.api`: "from the site's
  interface" / "aus der Schnittstelle der Website"; `tree.title_from_feed`: "title from the
  site's list" / "Titel aus der Liste der Website")
- Test: `tests/ui/test_site_tree.py`, `tests/ui/test_tree_view.py`

**Interfaces:**
- Consumes: `SitemapEntry.title`, `SitemapInfo.source`, `GuessReason "api"`.
- Produces: a sitemap-entry node takes `entry.title` as its title when it has none
  (`title_from_link` false, new flag `title_from_list` true); its `type_reason` is `"api"` when
  the item's `source` is `wordpress` or `drupal`. Merge rules (`_Found.merge`): a crawled page's
  own title and type win; an entry title beats a link text title; `"api"` ranks like
  `"sitemap"` (AU3-R2 applies to it the same way).

- [ ] **Step 1: Tests:** `test_api_entry_title_and_reason` (a `wordpress_posts` item → node
  title "Fortbildung – Mathe", reason `api`); `test_feed_title_beats_link_text`;
  `test_crawled_title_wins` (same URL crawled → page title); `test_sitemap_and_api_one_node`
  (same URL in a urlset and an API item → one node, the API title); `test_tree_view` renders the
  reason tooltip in en and de.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest tests/ui -q -k "tree"` and the browser tests
  `uv run pytest tests/ui/test_tree_columns_browser.py -q` pass (axe-core included).
- [ ] **Step 5:** Commit `feat(ui): titles and reason from feeds and site APIs in the tree`.

### Task 7: Places in metadata (H2)

**Files:**
- Modify: `src/text_extraction/metadata.py`, `src/text_extraction/ui/results.py` (one row per
  place under the metadata), `ui/locales/en.json`/`de.json` (`result.meta.place`: "Place" /
  "Ort")
- Test: `tests/test_metadata.py`, `tests/ui/test_results.py` (or the file that tests
  `results.py` rows)

**Interfaces:**
- Produces: `MAX_PLACES = 5`;
  `class Place(BaseModel)`: `kind: str` (the schema.org type the place belongs to: `Event`,
  `Place`, `LocalBusiness`, `Organization`, `PostalAddress`, `GeoCoordinates` …, or `meta`),
  `name: str | None`, `street: str | None`, `postal_code: str | None`, `locality: str | None`,
  `region: str | None`, `country: str | None`, `latitude: float | None`, `longitude: float | None`
  (JSON drops nulls like `SitemapEntry`); `PageMetadata.places: list[Place] = []`.
- Rules: from the page's own items (`_own_nodes`, every syntax): an item of a `Place` type (or a
  type ending in `Place`, `LocalBusiness` and its subtypes as extruct names them) → its `name`,
  `address`, `geo`; any own item with an `address` (`PostalAddress` or a string → `street`) or
  `geo`; an `Event`'s `location` (a `Place`, a `PostalAddress` or a string → `name`); a top-level
  `PostalAddress` / `GeoCoordinates`. `country` is `addressCountry` (string or its `name`).
  Meta: `geo.position` (`"lat;lon"`), `ICBM` (`"lat, lon"`), `geo.placename` (name),
  `geo.region` (region) → one `kind="meta"` place. Coordinates: floats in range (latitude
  -90..90, longitude -180..180), else both None. Strings collapsed, at most 200 characters.
  Places without any field are dropped; duplicates (same fields) once; at most `MAX_PLACES`, in
  order: events, places, other items, meta.

- [ ] **Step 1: Tests:** `test_event_location_place` (JSON-LD Event with a Place, address and
  geo → one place, `kind == "Event"`, `locality == "Erfurt"`, `latitude == 50.98`);
  `test_local_business_address`; `test_geo_meta` (`geo.position` `"50.9787;11.0328"` and
  `geo.placename` `"Erfurt"` → one meta place); `test_icbm`; `test_bad_coordinates`
  (`"999;abc"` → no coordinates, place kept only if another field exists); `test_microdata_place`;
  `test_cap_and_dedup` (8 places, two equal → 5); `test_no_places` → `[]` and no `places` key
  change elsewhere; UI row test: a result with one place shows "Ort: Erfurt" in de.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement on the existing `_Structured` nodes and `_meta_pairs`; a failing
  reader costs only the places (wrap like the other readers).
- [ ] **Step 4:** `uv run pytest tests/test_metadata.py tests/ui -q -k "meta or result"` passes.
- [ ] **Step 5:** Commit `feat(metadata): places from schema.org and geo meta tags`.

### Task 8: Documentation and package bookkeeping

**Files:**
- Modify: `docs/architecture.md` (package 26 section: the flow, rulings R26-1 to R26-7),
  `docs/user-guide/en/` and `docs/user-guide/de/` (the site-structure page: feeds and CMS
  interfaces, the checkbox, titles and the reason; the metadata page: `places`),
  `README.md`/`README.de.md` (feature list line), `CHANGELOG.md` `[Unreleased]`,
  `docs/packages.md` (status row 26 "in progress", PR number later; contents row 26 with
  "Done when").
- Test: `tests/test_docs_i18n.py`, `scripts/check_translation_pairs.py`

- [ ] **Step 1:** Write the docs. Contents row 26: "A site-structure crawl with `apis: true`
  (default) reads RSS/Atom feeds named in the start page's head, the WordPress REST API and Drupal
  JSON:API as sitemap items (`source`, formats `wordpress_types`, `wordpress_posts`,
  `drupal_index`, `drupal_nodes`), entries with `title` and a type from their content type
  (reason `api`); `SITE_APIS_ENABLED`; every extraction's metadata lists `places`; user guide
  en/de". Done when: "the tests named in the status table pass, linters green, the live site run
  on the WLO sites shows API entries with titles for wirlernenonline.de".
- [ ] **Step 2:** `uv run pytest tests/test_docs_i18n.py -q` and
  `uv run python scripts/check_translation_pairs.py` pass.
- [ ] **Step 3:** Full check: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`.
- [ ] **Step 4:** Commit `docs: package 26 site APIs and places`.
