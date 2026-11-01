# A whole site and its metadata

Deutsch: [Eine ganze Website und ihre Metadaten](../de/whole-site-and-metadata.md)

Map a whole site, then extract every page with its metadata: the schema.org and LRMI fields
and the licences. This takes three steps: a site structure at the depth you need, batches of its
pages with `metadata` and structured metadata, and reading the jobs page by page. For one
section of a site, see [Part of a site](part-of-a-site.md).

## Map the site

`POST /v1/site-structure` maps a site: its sitemaps (robots.txt's `Sitemap:` lines, else
`/sitemap.xml`; indexes, gzip, text, RSS and Atom) with their URLs, and its link graph from the
start page, `depth` clicks deep (default 2, at most `SITE_MAX_DEPTH`), for at most `max_pages`
pages (default 100, at most `SITE_MAX_PAGES`). It answers 202 with a job of kind `site`; the
extract workers crawl, and every page, robots.txt and sitemap file they find becomes an item of
the job, so its `total` grows while it runs:

<!-- request: POST /v1/site-structure -->
```json
{"url": "https://example.org/", "depth": 3, "max_pages": 1000}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/", "depth": 3, "max_pages": 1000}' \
  http://localhost:8080/v1/site-structure
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "site", "state": "queued",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=500"
# {"status": "success", "data": {"kind": "site", "state": "completed", "progress": {"total": 17,
#  ...}, "items": [
#  {"index": 0, "url": "https://example.org/", "result": {"status": "success", "data": {
#   "title": "Start", "content": null, "site": {"role": "page", "depth": 0, "parent": null,
#   "links": [
#    {"url": "https://example.org/mathe/", "text": "Mathe", "kind": "internal", "source": "a",
#     "rel": [], "hreflang": null, "region": "nav", "crawl": "queued", "target": 2},
#    {"url": "https://example.org/intern/", "text": "Intern", "kind": "internal", "source": "a",
#     "rel": [], "hreflang": null, "region": "main", "crawl": "blocked", "target": null}, ...]}}}},
#  {"index": 1, "url": "https://example.org/robots.txt", "result": {"data": {"site": {
#   "role": "robots", "depth": 0, "parent": 0,
#   "sitemaps": ["https://example.org/sitemap.xml"]}, ...}, ...}},
#  {"index": 2, "url": "https://example.org/mathe/", "result": {"data": {"site": {
#   "role": "page", "depth": 1, "parent": 0, "links": [...]}, ...}, ...}},
#  {"index": 3, "url": "https://example.org/sitemap.xml", "result": {"data": {"site": {
#   "role": "sitemap", "depth": 0, "parent": 1, "sitemap": {"format": "urlset", "guessed": false,
#   "entries": [{"loc": "https://example.org/mathe/", "lastmod": "2026-10-01"}, ...],
#   "children": [], "entry_count": 2400, "part": 1, "parts": 3}}, ...}, ...}}, ...]}, ...}
```

Pages are fetched over HTTP only, with the service's user agent, the per-host limits and the
checks of `compliance`; the items keep title, status, metadata and quality but no text
(`content: null`). The crawl stays on the start page's host (a leading `www.` ignored);
`scope: "path"` keeps it under the start URL's directory, `sitemaps: false` skips the sitemaps,
and `depth: 0` reads the start page and the sitemaps only. A crawl counts towards
`BATCH_MAX_OPEN_PER_CLIENT` with your batches.

### Read the site structure

Read the graph from the items: each item's `data.site.role` is `page`, `robots`, `sitemap` or
`sitemap_part`, and `parent` is the index of the item that found it. A page's `links` are its
outgoing links in document order: `kind` (`internal`, `external`, `file`), `source` (`a`, `area`,
a head `link` such as `canonical` or `alternate`, `iframe`), `rel`, `hreflang`, `region` (`nav`,
`header`, `footer`, `aside`, `main`, `breadcrumb`, `pagination`, or null) and `crawl`, what the
crawl did with it.

- `queued` and `seen` lead to a page item, whose index is `target`: an edge of the graph (a link
  deeper than `depth` to a page that is an item anyway says `seen` too).
- The others are not followed: `depth` (deeper than asked), `listed` (head links and iframes),
  `file`, `out_of_scope` (another host, or outside the start directory with `scope: "path"`),
  `nofollow`, `blocked` (robots.txt or a TDM opt-out under `compliance: enforce`), `max_pages`
  and `memory_guard`.

A sitemap's entries are in `sitemap.entries`; a large one continues in `sitemap_part` items with
the same URL (`part` of `parts`), and an index lists its sitemaps in `children`, each with the
item it became. An item whose `final_url` is another item's URL is a redirect to that page (its
links repeat the page's): dedupe the graph by `final_url`.

## Extract every page with its metadata

The URLs to extract are the page items and, if you want them, the sitemap entries. Send them to
`POST /v1/batch`, at most `BATCH_MAX_URLS` (1,000) per batch, with `metadata` and a `structured`
request of the strategy `metadata`:

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/", "https://example.org/mathe/"], "metadata": true, "structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"name": {"type": "string"}, "license": {"type": "string"}, "educationalLevel": {"type": "array", "items": {"type": "string"}}, "learningResourceType": {"type": "array", "items": {"type": "string"}}}}}}
```

Every item's result then has:

- `metadata`: title, authors, dates, keywords and images; the licence as `license` (the best
  candidate, with `id` such as `CC-BY-4.0`, `url`, `source` and `confidence`) and `licenses` (all
  that were found); and `lrmi`, the LRMI fields of the page (`educationalLevel`,
  `learningResourceType`, `audience`, `typicalAgeRange`, `teaches` and more).
- `structured.values`: the fields of your schema, read from the page's JSON-LD, microdata, RDFa,
  OpenGraph and meta tags, with `sources` and `missing`. See
  [Structured data](structured-data.md).

The strategy `metadata` needs no LLM. A key may have at most `BATCH_MAX_OPEN_PER_CLIENT` (5)
unfinished batches and site structures at a time; start the next batch when one is completed.

## Read the job

`GET /v1/jobs/{id}` shows a job's `state` (`queued`, `running`, `completed`), its `progress`
(the items by status) and one page of its items. `offset` is the first item and `limit` the
number of items (at most 500):

```bash
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=500"
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=500&limit=500"
```

Read on with `next_offset` until it is null (the last page): a page stops early once its results
reach about 20 MB, so always continue from `next_offset`, not from `offset + limit`. Each item has its `index`, its `url`,
its `state`, its `result` (null until it is done) and `run`, its last claim by a worker. A job is
kept for `JOB_TTL_SECONDS` (24 h); `DELETE /v1/jobs/{id}` cancels it and deletes it at once.

## In the playground

1. **Site structure**: enter the start URL, set "Link depth" and "Most pages", leave "Scope" at
   the host, and press "Read the site structure".
2. Once the job is completed, "Show as tree" lists the pages by host and path; while it runs,
   "Show tree now" shows a snapshot. "Select all" checks every URL; or check the sections you
   want.
3. Leave "Metadata" on, and for the schema.org and LRMI fields put a `structured` request into
   "Options (JSON)", for example
   `{"structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"license": {"type": "string"}}}}}`.
   Press "Extract selected".
4. Each batch shows its progress and its items; "Previous" and "Next" page through them, and
   "Show" opens an item's result with its metadata.

For one page, the **Extract** tab has the box "Metadata, structured", which asks for the
schema.org and LRMI fields by name.

## With MCP

`site_structure` maps the site, `batch_extract` extracts the pages, and `get_job` reads both jobs
page by page (`offset`, `limit` up to 50). `structured` goes into `options`:

```json
{"urls": ["https://example.org/", "https://example.org/mathe/"], "options": {"metadata": true, "structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"license": {"type": "string"}}}}}}
```

A prompt such as "Map https://example.org/ three links deep and list the licence and the
educational level of every page" lets the assistant run these steps. See [MCP](mcp.md).
