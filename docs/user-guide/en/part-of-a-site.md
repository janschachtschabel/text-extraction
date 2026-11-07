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

  The tree has aligned columns: page, type, found, status and depth, under a head that stays in
  view while you scroll. On a narrow tree the rows are compact (the tree's own width decides, not
  the window's). A section shows how many pages it holds and its own type. A crawled or sitemap
  page with a title shows it in quotes after the path. A summary line above the tree counts the
  URLs (crawled, from the sitemap, only linked) and, for a finished crawl, how long it took, and
  the chips under "Filter by type" show only the URLs of one type ("All" shows every URL).

  The tree appears by itself once the job has a result, and while the job runs it refreshes every
  few seconds (the interval grows when reading the job is slow, and a failed refresh is tried
  again). A refresh keeps your ticks, the folds, the filter, the batch options, the scroll
  position and the focus, and waits while you type in a field. The batch below the tree stays when
  the tree refreshes.

  Each URL of the tree shows its page type, such as "Content", "Listing", "Event", "Learning
  material" or "Imprint" (see
  [Page types and link categories](whole-site-and-metadata.md#page-types-and-link-categories)),
  and "Types:" above the tree counts them. A crawled page shows the type of its result; a page
  that failed with an HTTP error has none. Every other URL was not read, so its type is guessed
  and marked "guessed", with the reason: "from the URL" (the URL rules, among others author pages
  (`author`, `autor`: "Listing"), more names for contact pages, and dated paths such as
  `/2021/05/oer-camp/` or WordPress addresses such as `?p=12` and `?page_id=12`: "Content";
  language and tracking parameters such as `?lang=en` do not count), "from the sitemap" (the file
  name of the sitemap that listed it, such as `post-sitemap.xml` or
  `wp-sitemap-posts-event-1.xml`), "from the link text" (a link named like a type, such as
  "Kontakt", or in a pagination), "from keywords" (a word of the path or the link text, such as
  `veranstaltung` for "Event" or `arbeitsblatt` for "Learning material") or "a page with a name,
  not fetched" ("Content", the default for a URL with a readable name). A guess from the default
  gives way to any other reason. Only a URL that no rule can guess (a number, a hex string
  or a UUID as its last part, a last part without a word of three letters, or a query that is not
  a language or tracking parameter) shows the badge "Type open", with the reason "Not fetched; its type shows when it is extracted" (as
  tooltip and for screen readers), and the "Types:" line counts "Type open" as well. "Select
  content pages" checks every URL but the imprint, privacy, legal, contact, login, search and
  error pages, and unchecks those and every page that failed; a URL without a type is checked
  unless its page failed.

  The tree marks URLs that the site's rules block: the badge "robots.txt" (robots.txt disallows
  the URL, or robots.txt could not be retrieved) or "TDM" (a tdmrep.json reservation, an ai.txt
  disallow or a noai statement). The reason is the badge's tooltip and is read out by screen
  readers. Every shown URL is judged when the tree is shown, against the site's current
  robots.txt, tdmrep.json and ai.txt (from the cache, or loaded once per site within the per-host
  limits; at most 20 sites and 12 s), so the marks show the rules at that time. Links that the
  crawl did not follow because the rules blocked them appear as "linked only" leaves. A line above
  the tree counts the blocked URLs, and another names sites whose rules could not be read. Under
  "Respect the site's rules", blocked URLs cannot be checked, and "Select all", "Select content
  pages" and checking a section skip them; under "Check and report only" or "Checks off" they can
  be checked and the buttons include them. The batch below the tree starts with the "Site rules"
  step that the crawl ran with.

  A section that holds only its own page is one row, not a section with a single leaf. Every
  section can be folded and unfolded with the button before its name: the top level starts open,
  the levels below start folded. "Unfold all" and "Fold all" above the tree do it for every
  section. Without JavaScript the tree shows every node unfolded. Checking a section also checks
  every enabled URL below it, folded or not, and the counter counts them; "Select all" does the
  same. A blocked URL in a folded section stays unchecked under "Respect the site's rules".

  Below the tree, a "Social media" list shows the profile links of the site (its links to
  social networks). They lie outside the site, so they cannot be selected.
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
