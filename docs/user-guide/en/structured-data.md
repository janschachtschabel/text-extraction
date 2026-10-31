# Structured data

Deutsch: [Strukturierte Daten](../de/structured-data.md)

`structured` fills the fields of a JSON schema from a page: a title, a duration, a licence, an
educational level, whatever the schema names. `chunking` splits the content into chunks, for
example for a search index. This page explains the strategies, the request and the answer, the
LLM and its key, and chunking, each in the API, the MCP server and the playground.

## The strategies

`strategy` says where the values come from:

| Strategy | Where the values come from |
| --- | --- |
| `metadata` | The page's JSON-LD, microdata, RDFa, OpenGraph and meta tags, by field name (such as `name`, `license` or `educationalLevel`). No LLM. |
| `selectors` | Your CSS rules per field (XPath with an admin key), and the selectors learned for your API key and the site. No LLM. |
| `patterns` | Your regular expressions per field, on the content. No LLM. |
| `llm` | The LLM reads the content and fills the schema. |
| `auto` | The default. For each field, the first source that finds a value: your selectors (or the learned ones), the page's metadata, your patterns, and the LLM only for the fields still missing. |

## The request

| Field | What it does |
| --- | --- |
| `schema` | The fields to fill, as a JSON schema of a subset: an `object` of `string`, `number`, `integer`, `boolean`, `array` and `object` fields, at most 3 levels and 50 properties. |
| `selectors` | A rule per field: `css` or `xpath`, optionally `attr` (such as `href`) and `all`. |
| `patterns` | A rule per field: `regex`, `group` (a number or a name), `flags` and `all`. |
| `tables` | `true`: also return the page's tables. |
| `learn_selectors` | `true` (with `llm` or `auto`): the LLM proposes a CSS selector for each value it found; the ones that find the same value again are kept for your API key, the site and the schema. |
| `instructions` | Free text for the LLM (what a field means, the language), with `llm` or `auto`. |

<!-- request: POST /v1/structured -->
```json
{"url": "https://example.org/mathe/brueche", "structured": {"schema": {"type": "object", "properties": {"name": {"type": "string"}, "bearbeitungszeit": {"type": "integer"}, "merksatz": {"type": "string"}}}, "patterns": {"bearbeitungszeit": {"regex": "Bearbeitungszeit: (\\d+)", "group": 1}}}}
```

## The answer

`data.structured` holds:

- `values`: every top-level field of the schema, null when it was not found;
- `sources`: where each value came from (`metadata`, `selectors`, `learned`, `patterns` or `llm`);
- `missing`: the fields without a value;
- `tables` (with `tables: true`), `learned` (with `learn_selectors`: what was `stored` and
  `rejected`), and `llm`: the LLM calls made, null without them.

A value that does not fit its field's type is dropped with the warning `structured_value_invalid`.

## With REST

`POST /v1/structured` is `POST /v1/extract` with `structured` required. `POST /v1/extract`,
`/v1/extract/file` and `/v1/batch` take `structured` too; a batch fills the schema for every item.

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
#  "llm": {"model": "<LLM_MODEL>", "calls": 1, ...}, "stage": "done", ...}, ...}, ...}
```

## The LLM and its key

The strategy `llm`, `learn_selectors` and the `semantic` chunking need the operator's LLM worker
(`LLM_ENABLED`); without it they answer 400. `auto` then keeps the values it found, and the
fields still missing come with the warning `llm_unavailable`.

- Your own key for the LLM endpoint goes into the header `X-LLM-Api-Key`. It wins over the
  operator's `LLM_API_KEY` and is never stored.
- A request that needs the LLM waits `LLM_SYNC_WAIT_SECONDS` (30 s), then answers 202 with a job.
- When the LLM step cannot run, the answer keeps its content and is `partial` with
  `error.code: llm_failed` (retryable).

With `learn_selectors`, the next pages of the same site need no LLM for the learned fields:
their `sources` say `learned`.

## Chunking

`chunking` splits the content into chunks of at most `size` characters (default 1,000, `overlap`
100): `fixed`, `recursive` (the default), `markdown` (at headings; each chunk carries its heading
path), `sentence` or `semantic` (cut where the meaning shifts, with the endpoint's embeddings).
`POST /v1/chunk` takes a `url` or your own `text` (Markdown, or `"text_type": "plain"`):

<!-- request: POST /v1/chunk -->
```json
{"text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.\n\n## Addieren\n\nGleiche Nenner bleiben.", "chunking": {"strategy": "markdown", "size": 500, "overlap": 0}}
```

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
take `chunking` too; every result of a batch carries its own chunks:

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/a", "https://example.org/b"], "chunking": {"strategy": "sentence", "size": 800}}
```

## In the playground

- **Structured**: enter the URL and edit the box "Structured (JSON: strategy, schema, rules)",
  which starts with a schema of `title` and `author` and the strategy `auto`. Press "Extract the
  fields". The result shows "Structured values", a table of each field, its value and its source,
  and the fields that are missing.
- **Chunk**: enter a URL or paste a text (not both), choose what the text is, edit the box
  "Chunking (JSON)" and press "Chunk". The result lists the chunks.

For the LLM, log in with your LLM key under "Optional keys"; the playground sends it as
`X-LLM-Api-Key`.

## With MCP

`extract_structured` takes `url` and `structured` (required); `chunk` takes `url` or `text` and
`chunking` (required). Your LLM key goes into the header `X-LLM-Api-Key` of the MCP connection,
never into a tool argument:

```json
{"url": "https://example.org/mathe/brueche", "structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"name": {"type": "string"}, "license": {"type": "string"}}}}}
```

A prompt such as "Read the title, the licence and the educational level of
https://example.org/mathe/brueche" lets the assistant call `extract_structured`. See [MCP](mcp.md).
