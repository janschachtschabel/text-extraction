# Part of a site

Deutsch: [Ein Teil einer Website](../de/part-of-a-site.md)

Extract a part of a site, such as one section or a list of pages. There are two ways:

- You do not know the URLs yet: map the part with a site structure that stays under one
  directory (`scope: path`), then extract the pages you pick.
- You have the URLs: send them as a batch, as a list or as a CSV or JSON file.

For a whole site with its metadata, see [A whole site and its metadata](whole-site-and-metadata.md).

## Map the part

A site structure crawls from a start URL. With `scope: "path"` it follows only links under the
start URL's directory: from `https://example.org/mathe/` it reads `/mathe/brueche` but not
`/deutsch/`. `depth` is the number of clicks from the start page (default 2, at most
`SITE_MAX_DEPTH`), and `max_pages` caps the pages (default 100).

<!-- request: POST /v1/site-structure -->
```json
{"url": "https://example.org/mathe/", "scope": "path", "depth": 3, "max_pages": 200}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/mathe/", "scope": "path", "depth": 3, "max_pages": 200}' \
  http://localhost:8080/v1/site-structure
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "site", "state": "queued",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
```

The answer is a job. Its items are the pages it read, robots.txt and the sitemaps; a link that
leaves the directory says `crawl: out_of_scope`. The sitemaps list the whole site, not only the
directory: switch them off with `"sitemaps": false`, or skip their entries when you pick the
pages. How to read the items is in
[Read the site structure](whole-site-and-metadata.md#read-the-site-structure).

A site structure reads the pages over HTTP and keeps no text (`content: null`). To get the text,
send the URLs you picked as a batch.

## Extract the pages

`POST /v1/batch` extracts many URLs as one job (at most `BATCH_MAX_URLS`, 1,000 by default):

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/mathe/brueche", "https://example.org/mathe/prozent"], "format": "markdown", "metadata": true}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/mathe/brueche", "https://example.org/mathe/prozent"],
       "format": "markdown", "metadata": true}' \
  http://localhost:8080/v1/batch
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "batch", "state": "queued",
#  "total": 2, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=100"
```

Each URL gets its own result in `items`, with the fields of
[One URL](single-url.md#the-fields-to-read). A batch has no browser options (`actions`, `cookies`,
`headers`) and no `wait_seconds`, but it takes `structured`, `chunking` and `pii` for every item.
How to page through the items is in [Read the job](whole-site-and-metadata.md#read-the-job).

## In the playground

- **Site structure**: enter the start URL, set "Scope" to "path: only below the start URL's
  directory", "Link depth" and "Most pages", clear "robots.txt and sitemaps" for
  `"sitemaps": false`, and press "Read the site structure". Once the job is completed, "Show as
  tree" lists its pages by host and path, each marked crawled, sitemap or linked only; while it
  runs, "Show tree now" shows a snapshot. Check the pages you want, or a section to check all
  its pages, and press "Extract selected": the checked URLs become batches, with the format,
  the browser fallback, the metadata and the options you set below the tree.
- **Batch**: paste the URLs, one per line, and press "Start batch". More URLs than one batch
  takes are sent as several batches.
- **Batch file**: choose a CSV or JSON file and press "Check the file". A CSV has a `url` column
  (or the URLs in its first column), with commas, semicolons or tabs. A JSON file is an array of
  URLs, or of objects with `url`. The preview shows how many rows are URLs, which are not and
  which repeat, before you press "Start batch".

Every batch shows its state, a progress bar and its items, and refreshes every 3 s until it is
completed.

## With MCP

`site_structure` takes `url`, `depth`, `max_pages` and `scope`; `batch_extract` takes `urls`,
`format` and `mode`. Both answer a job: call `get_job` with its `job_id` until it is completed,
and page through its items with `offset` and `limit`.

```json
{"url": "https://example.org/mathe/", "scope": "path", "depth": 3, "max_pages": 200}
```

A prompt such as "Map the section https://example.org/mathe/ of that site and extract every page
about fractions" lets the assistant call `site_structure`, read the job with `get_job` and send
the pages it picked to `batch_extract`. See [MCP](mcp.md).
