# text-extraction

Deutsch: [README.de.md](README.de.md)

A FastAPI service that turns web pages, files, audio and video into clean text or Markdown, with
their metadata. It reads a page over HTTP first and renders it in a browser when the page needs
it. It also converts documents, transcribes audio and video, handles personal data, fills
structured fields, splits text into chunks and maps whole sites. It has a REST API, an MCP server
and a playground in English and German.

## Status

Under construction: packages 0-11 are merged (foundation, service skeleton, fast extraction,
jobs and batch, browser worker, documents and OCR, audio and video, PII, structured output and
chunking, site structure, MCP and UI, hand-over); package 12 (UI languages, unblock option,
bilingual docs) is in progress. See
[docs/packages.md](docs/packages.md#status) for the implementation packages and their status.

## Two ways to extract

- **Respect the site's rules** (the default): robots.txt and the EU TDM opt-out are checked, every
  host gets only a few requests at a time, and the service sends its own honest user agent.
- **Maximum extraction**, for sites that allowed extraction by contract: an admin sends
  `"compliance": "ignore"`, and where the operator switched on `BROWSER_UNBLOCK_ENABLED`, a
  stealth browser gets past bot protection, also after a plain 403, 429 or 503. It solves no
  image captchas, uses no paid service and rotates no IP address; the per-host limits still
  apply.

In both, the browser fallback (`mode`) decides whether a page is rendered in the browser: when
the page needs it (`auto`, the default), never (`fast`) or always (`browser`). See
[Browser fallback](docs/user-guide/en/usage.md#browser-fallback).

## Quick start with Docker

Requires Docker with Compose. Compose starts the API, an extract worker, a browser worker and a
Valkey (with a password) for the shared state.

```bash
cp .env.example .env         # set API_KEYS (the sha256 of your key) and VALKEY_PASSWORD (openssl rand -hex 24)
docker compose up --build    # public API on http://localhost:8080
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

The playground is at http://localhost:8080/ui and the MCP endpoint at http://localhost:8080/mcp.
[Installation](docs/user-guide/en/installation.md) covers the local setup with uv, the optional
workers (OCR, personal data, LLM, speech recognition), Kubernetes and the key settings.

## Use cases

- One page as text or Markdown, with its metadata:
  [Usage](docs/user-guide/en/usage.md#the-result-envelope).
- Documents, audio and video, personal data and image descriptions:
  [Files, media and personal data](docs/user-guide/en/usage.md#files-media-and-personal-data).
- Many URLs at once: [Batch](docs/user-guide/en/usage.md#batch).
- Part of a site or a whole site: [Site structure](#site-structure).
- Structured data and chunks: [Structured output and chunking](#structured-output-and-chunking).
- An AI assistant with the 11 tools of the MCP server: [MCP](#mcp).
- Trying it out in the browser: [the playground](docs/user-guide/en/usage.md#the-playground).

Every field of each request is in [Parameters](docs/user-guide/en/parameters.md).

## Structured output and chunking

`structured` takes a JSON schema and fills its fields from the page: `metadata` (JSON-LD,
microdata, RDFa, OpenGraph and meta tags), `selectors` (your CSS rules; XPath with an admin key),
`patterns` (your regular expressions on the content), `llm`, or `auto` (the default): per field the
first of your selectors, the page's metadata and your patterns that finds a value, and the LLM only
for the fields still missing. `POST /v1/structured` is `POST /v1/extract` with `structured`
required:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/mathe/brueche", "structured": {
        "schema": {"type": "object", "properties": {"name": {"type": "string"},
          "bearbeitungszeit": {"type": "integer"}, "merksatz": {"type": "string"}}},
        "patterns": {"bearbeitungszeit": {"regex": "Bearbeitungszeit: (\\d+)", "group": 1}}}}' \
  http://localhost:8080/v1/structured
# {"status": "success", "data": {"content": "# Brüche addieren ...", "structured": {
#  "values": {"name": "Brüche addieren und subtrahieren", "bearbeitungszeit": 45,
#   "merksatz": "Brüche mit gleichem Nenner ..."},
#  "sources": {"name": "metadata", "bearbeitungszeit": "patterns", "merksatz": "llm"},
#  "missing": [], "tables": null, "learned": null,
#  "llm": {"model": "gpt-6-luna", "calls": 1, ...}, "stage": "done", ...}, ...}, ...}
```

`"tables": true` adds the page's tables; `"learn_selectors": true` (with `llm` or `auto`) asks the
LLM for CSS selectors that reproduce the values it found and keeps them for your API key and the
site, so that the next pages of that site need no LLM for those fields (`"sources": {...:
"learned"}`). Your own `X-LLM-Api-Key` header wins over the operator's `LLM_API_KEY`. A request
that needs the LLM waits `LLM_SYNC_WAIT_SECONDS` (30 s), then answers 202 with a job to poll; when
the LLM step cannot run, the answer keeps its content and is `partial` with `error.code:
llm_failed` (retryable).

`chunking` splits the content into chunks of at most `size` characters (default 1,000, `overlap`
100): `fixed`, `recursive` (the default), `markdown` (at headings; each chunk carries its heading
path), `sentence` or `semantic` (cut where the meaning shifts, with the endpoint's embeddings).
`POST /v1/chunk` takes a `url` or your own `text` (Markdown, or `"text_type": "plain"`):

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.\n\n## Addieren\n\nGleiche Nenner bleiben.",
       "chunking": {"strategy": "markdown", "size": 500, "overlap": 0}}' \
  http://localhost:8080/v1/chunk
# {"status": "success", "data": {"mode_used": "upload", "chunks": [
#  {"index": 0, "text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.", "start": 0, "end": 44,
#   "headings": ["Brüche"]},
#  {"index": 1, "text": "## Addieren\n\nGleiche Nenner bleiben.", "start": 46, "end": 82,
#   "headings": ["Brüche", "Addieren"]}], ...}, ...}
```

`POST /v1/extract`, `/v1/extract/file` (as form fields with a JSON object each) and `/v1/batch`
take `structured` and `chunking` too; every result of a batch carries its own chunks:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/a", "https://example.org/b"],
       "chunking": {"strategy": "sentence", "size": 800}}' \
  http://localhost:8080/v1/batch
```

## Site structure

`POST /v1/site-structure` maps a site: its sitemaps (robots.txt's `Sitemap:` lines, else
`/sitemap.xml`; indexes, gzip, text, RSS and Atom) with their URLs, and its link graph from the
start page, `depth` clicks deep (default 2), for at most `max_pages` pages (default 100). It
answers 202 with a job of kind `site`; the extract workers crawl, and every page, robots.txt and
sitemap file they find becomes an item of the job, so its `total` grows while it runs:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/", "depth": 2, "max_pages": 100}' \
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

Read the graph from the items: each item's `data.site.role` is `page`, `robots`, `sitemap` or
`sitemap_part`, and `parent` is the index of the item that found it. A page's `links` are its
outgoing links in document order: `kind` (`internal`, `external`, `file`), `source` (`a`, `area`,
a head `link` such as `canonical` or `alternate`, `iframe`), `rel`, `hreflang`, `region` (`nav`,
`header`, `footer`, `aside`, `main`, `breadcrumb`, `pagination`, or null) and `crawl`, what the
crawl did with it. `queued` and `seen` lead to a page item, whose index is `target`: an edge of
the graph (a link deeper than `depth` to a page that is an item anyway says `seen` too). The
others are not followed: `depth` (deeper than asked), `listed` (head links and iframes), `file`,
`out_of_scope` (another host, or outside the start directory with `scope: "path"`), `nofollow`,
`blocked` (robots.txt or a TDM opt-out under `compliance: enforce`), `max_pages` and
`memory_guard`. A sitemap's entries are in
`sitemap.entries`; a large one continues in `sitemap_part` items with the same URL (`part` of
`parts`), and an index lists its sitemaps in `children`, each with the item it became. An item
whose `final_url` is another item's URL is a redirect to that page (its links repeat the page's):
dedupe the graph by `final_url`.

Pages are fetched in fast mode only, with the service's user agent, the per-host limits and the
checks of `compliance`; the items keep title, status, metadata and quality but no text
(`content: null`): send the URLs you want as text to `POST /v1/batch`. The crawl stays on the start
page's host (a leading `www.` ignored); `scope: "path"` keeps it under the start URL's directory,
`sitemaps: false` skips the sitemaps, and `depth: 0` reads the start page and the sitemaps only. A
crawl counts against `BATCH_MAX_OPEN_PER_CLIENT` with your batches.

## MCP

With `MCP_ENABLED=true` (Helm `mcp.enabled=true`; docker compose switches it on for the API) the
service is an MCP server at `https://<host>/mcp` (streamable HTTP, stateless, JSON answers). An MCP
client authenticates with an API key of the service, exactly as a REST client does, and every tool
call runs the REST route's checks and counts against the key's rate limit.

Claude Code:

```bash
claude mcp add --transport http text-extraction https://<host>/mcp \
  --header "Authorization: Bearer <key>"
# local compose: http://localhost:8080/mcp
```

Claude Desktop reaches a server that needs a header through the `mcp-remote` bridge (needs
Node.js). In `claude_desktop_config.json` (Settings, Developer, Edit Config):

```json
{
  "mcpServers": {
    "text-extraction": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<host>/mcp", "--header", "Authorization:${AUTH_HEADER}"],
      "env": {"AUTH_HEADER": "Bearer <key>"}
    }
  }
}
```

The key goes in `env`, so it is not part of the arguments (`Authorization:${AUTH_HEADER}` has no
blank, which some clients split). `mcp-remote` takes plain HTTP only for `localhost` and
`127.0.0.1` (such as compose's `http://localhost:8080/mcp`); for an `http://` endpoint on any other
host (another machine of a trusted network) add `"--allow-http"` to the arguments. Restart Claude
Desktop after the change. Under `AUTH_MODE=none` leave the header out. A caller's LLM or ASR key
goes the same way, as the headers `X-LLM-Api-Key` and `X-ASR-Api-Key` (another `--header` each);
it is never a tool argument. Browser-based clients must be listed in `MCP_ALLOWED_ORIGINS`; a
request with any other `Origin` is refused.

| Tool | Route | Arguments (besides `options`) |
| --- | --- | --- |
| `extract_url` | `POST /v1/extract` | `url`, `format` (`markdown`), `mode` (`auto`), `wait_seconds`, `content_offset`, `max_chars` |
| `extract_file` | `POST /v1/extract/file` | `content_base64` (a `data:` URL prefix allowed), `file_name`, `content_type`, `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `screenshot_url` | `POST /v1/screenshot` | `url`, `full_page` (true), `wait_seconds`; the PNG is attached up to `MCP_IMAGE_MAX_BYTES` |
| `extract_structured` | `POST /v1/structured` | `url`, `structured` (required), `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `chunk` | `POST /v1/chunk` | `url` or `text`, `chunking` (required), `text_type`, `wait_seconds`, `content_offset`, `max_chars` |
| `batch_extract` | `POST /v1/batch` | `urls`, `format`, `mode`; answers a job |
| `site_structure` | `POST /v1/site-structure` | `url`, `depth` (2), `max_pages` (100), `scope` (`host`); answers a job |
| `get_job` | `GET /v1/jobs/{id}` | `job_id`, `offset`, `limit` (5, 1-50), `wait_seconds` (0, up to `MCP_WAIT_SECONDS`), `content_offset`, `max_chars`; no `options` |
| `get_job_screenshot` | `GET /v1/jobs/{id}/artifacts/screenshot.png` | `job_id`; no `options` |
| `cancel_job` | `DELETE /v1/jobs/{id}` | `job_id`; no `options` |
| `service_info` | `GET /v1/info` | none |

`options` takes any further field of the route's REST body (see `/docs`), for example
`{"pii": {"mode": "mask"}}` or `{"metadata": false}`; a key given both as an argument and in
`options` is refused. A tool answer's structured content is the REST envelope, and its text is the
same JSON, after a one-line hint when there is something to do:

- A call that takes longer than `MCP_WAIT_SECONDS` (25 s) answers a job: call `get_job` with its
  `job_id` and `wait_seconds` until it is `completed`. Only with `JOBS_ENABLED=true` (the default):
  without jobs a slow call runs synchronously within the route's own limits, and `batch_extract`,
  `site_structure` and the job tools answer 503. The playground's forms behave the same way.
- A long `content` is cut to `max_chars` (`MCP_MAX_CONTENT_CHARS`, 50,000; shared by the items of a
  `get_job` page) with the warning `mcp_content_truncated`; call again with the `content_offset`
  the hint names.
- An answer that REST gives with 4xx or 5xx (a bad argument, a refused role, the rate limit) is a
  tool error with the REST error envelope; a page that failed (`status: failed`, a 200 in REST) is
  a normal answer.

## Documentation

- [User guide](docs/user-guide/en/index.md): installation, usage, parameters and the glossary
  (also in [German](docs/user-guide/de/index.md))
- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md): environment variables, secrets, ports, headers
- [Helm chart](deploy/helm/text-extraction/README.md)
- [Hand-over](docs/handover.md): the ops guide's hand-over checklist, costs, maintenance and
  releases
- [CLAUDE.md](CLAUDE.md): rules and workflow for contributors

## Licence

Apache-2.0. See [LICENSE](LICENSE).
