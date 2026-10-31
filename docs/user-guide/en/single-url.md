# One URL

Deutsch: [Eine URL](../de/single-url.md)

Extract one web page or document by its URL, as Markdown or text, with its metadata. This page
shows the three ways (the playground, REST and MCP) and the fields of the answer to read. Files
you have, audio and video are in [Usage](usage.md#files-media-and-personal-data).

## In the playground

Open the **Extract** tab, enter the URL and press "Extract".

- "Format": Markdown or text.
- "Browser fallback": when the page needs it (the default), never or always; see
  [Browser fallback](usage.md#browser-fallback).
- "Site rules" (admins only): see [Site rules](site-rules.md).
- "Metadata" (on by default), "Metadata, structured" (the schema.org and LRMI fields, read by
  name) and "Describe images".
- "Options (JSON)" takes any further field of the REST body, such as `{"links": true}`.

The result shows the content, the metadata, the images, the links, the warnings and "What
happened", a timeline of the checks, the fetch, the render and the conversion. "Raw JSON" is the
whole answer, and "As curl" the same request for the shell.

## With REST

`POST /v1/extract` takes the URL and the options of [Parameters](parameters.md#post-v1extract):

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/mathe/brueche", "format": "markdown", "links": true}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/mathe/brueche", "format": "markdown", "links": true}' \
  http://localhost:8080/v1/extract
# {"status": "success", "data": {"url": "https://example.org/mathe/brueche",
#  "upstream_status": 200, "mode_used": "fast", "language": "de", "word_count": 812,
#  "title": "Brüche addieren", "content": "# Brüche addieren\n\n...",
#  "quality": {"visible_chars": 5230, "word_count": 812, "js_heavy": false,
#   "bot_challenge": false, "soft_404": false, "login_wall": false, ...},
#  "compliance": {"mode": "enforce", "decision": "allowed", "robots": "allowed", ...},
#  "metadata": {"title": "Brüche addieren", "authors": ["A. Beispiel"],
#   "license": {"id": "CC-BY-SA-4.0", "source": "rel-license", ...},
#   "lrmi": {"educationalLevel": ["Sekundarstufe I"]}, "images": [...], ...},
#  "links": [...], "warnings": [], ...}, "error": null, ...}
```

A page that needs JavaScript is rendered in the browser on its own (`mode: auto`). A page that
takes long can become a job: send `wait_seconds` and read the job when the answer is 202; see
[Sync and jobs](usage.md#sync-and-jobs).

## With MCP

The tool `extract_url` takes the URL and the same options:

```json
{"url": "https://example.org/mathe/brueche", "format": "markdown", "options": {"links": true}}
```

A long `content` is cut to `max_chars`; call again with the `content_offset` that the answer's
hint names. A slow page answers a job: call `get_job` until it is completed. See [MCP](mcp.md).

## The fields to read

Every answer is a [result envelope](usage.md#the-result-envelope): read `status` first, and
`error` when it is not `success`. In `data`:

| Field | What it holds |
| --- | --- |
| `content` | The text of the page in `format`, without navigation, footers and other boilerplate. `title`, `language` and `word_count` describe it. |
| `metadata` | Title, description, authors, dates, site name, canonical URL, keywords, images, the licence (`license`, the best candidate, and `licenses`, all that were found) and the LRMI fields (`lrmi`). Off with `metadata: false`. |
| `quality` | Facts about the page: `visible_chars`, `word_count`, `js_heavy`, `bot_challenge`, `soft_404`, `login_wall` and `browser_recommended`. A page with `browser_recommended` is one the browser should render. |
| `compliance` | What the site rules found: the `mode`, the `decision` (`allowed`, `blocked`, `reported`, `skipped`), `robots` and the TDM statement. See [Site rules](site-rules.md). |
| `warnings` | What did not go as asked, although a result is returned, such as `browser_unavailable` (the browser did not take the page: the HTTP result). |

A licence has an `id` (such as `CC-BY-SA-4.0`), a `url`, a `label`, its `source` (a `rel-license`
link, JSON-LD, microdata, RDFa, a meta tag or the page text) and a `confidence`. `mode_used`
says whether HTTP or the browser produced the answer, and `upstream_status` is the page's HTTP
status.
