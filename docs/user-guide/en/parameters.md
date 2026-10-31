# Parameters

Deutsch: [Parameter](../de/parameters.md)

This page lists every field of each request body, with its type, its default and what it does.
The OpenAPI spec at `/docs` of a running service has the same fields with every detail, and the
nested objects in full. A field that is not listed answers 422; so does a value out of range.
"unset" means that the field is left out (or `null`); "required" means that it has no default.

## `POST /v1/extract`

One page or file by its URL. See [Usage](usage.md) for the answer.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `url` | string | required | The http(s) URL to extract. |
| `mode` | `fast`, `auto` or `browser` | `auto` | The browser fallback: `auto` uses the browser when the page needs it, `fast` never, `browser` always. See [Browser fallback](usage.md#browser-fallback). |
| `format` | `markdown`, `text` or `html` | `markdown` | The format of `content`. |
| `metadata` | boolean | `true` | Include the page's metadata (title, authors, licence, dates, images, schema.org and LRMI fields). |
| `links` | boolean | `false` | Include the page's links. |
| `compliance` | `enforce`, `report` or `ignore` | unset | The site rules: `enforce` refuses what robots.txt or a TDM opt-out forbids, `report` only reports it, `ignore` (admins only) skips the checks and allows maximum extraction. Unset: the operator's `COMPLIANCE_DEFAULT`. |
| `cache` | boolean | `true` | Use the result cache; `false` neither reads nor stores. |
| `wait_seconds` | number, 0 to 60 | unset | After this many seconds a running extraction becomes a job (202); `0`: a job at once. Unset: the request waits for its answer, with three exceptions: with `transcribe: true` the default is 20, and a request that waits for the NLP or the LLM worker answers 202 after `PII_SYNC_WAIT_SECONDS` or `LLM_SYNC_WAIT_SECONDS`. |
| `actions` | list of objects | empty | Page actions in the browser, in order, at most 20 (such as `click`, `type`, `scroll`, `accept_cookies`). Not with `mode: fast`. |
| `cookies` | list of objects | empty | Cookies for the requested site. Not with `mode: fast`. |
| `headers` | object | empty | Request headers for the requested site. Not with `mode: fast`. |
| `screenshot` | boolean | `false` | Also capture a PNG screenshot. Not with `mode: fast`. |
| `full_page` | boolean | `true` | The screenshot shows the whole page; `false`: the first viewport. `false` is refused with `mode: fast`. |
| `ocr` | `auto`, `never` or `always` | `auto` | OCR of documents: `auto` reads scans and images with OCR where the service has it. |
| `transcribe` | boolean | `false` | Transcribe audio and video without subtitles (always a job). |
| `languages` | list of language codes | empty | Preferred subtitle and transcription languages, at most 5, such as `["de", "en"]`. |
| `pii` | mode or object | unset | Handle personal data: `mask`, `redact`, `hash` or `annotate`. See [`pii`](#pii). |
| `structured` | object | unset | Fill the fields of a JSON schema from the page. See [`structured`](#structured). |
| `chunking` | object | unset | Split the content into chunks. See [`chunking`](#chunking). |
| `describe_images` | object | unset | Describe the page's images with the LLM. See [`describe_images`](#describe_images). |

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/", "format": "text", "links": true, "metadata": true}
```

## `POST /v1/structured`

`POST /v1/extract` with `structured` required.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `url` | string | required | As for `POST /v1/extract`. |
| `structured` | object | required | The JSON schema and how to fill it. See [`structured`](#structured). |
| `mode` | `fast`, `auto` or `browser` | `auto` | As for `POST /v1/extract`. |
| `format` | `markdown`, `text` or `html` | `markdown` | As for `POST /v1/extract`. |
| `metadata` | boolean | `true` | As for `POST /v1/extract`. |
| `links` | boolean | `false` | As for `POST /v1/extract`. |
| `compliance` | `enforce`, `report` or `ignore` | unset | As for `POST /v1/extract`. |
| `cache` | boolean | `true` | As for `POST /v1/extract`. |
| `wait_seconds` | number, 0 to 60 | unset | As for `POST /v1/extract`. |
| `actions` | list of objects | empty | As for `POST /v1/extract`. |
| `cookies` | list of objects | empty | As for `POST /v1/extract`. |
| `headers` | object | empty | As for `POST /v1/extract`. |
| `screenshot` | boolean | `false` | As for `POST /v1/extract`. |
| `full_page` | boolean | `true` | As for `POST /v1/extract`. |
| `ocr` | `auto`, `never` or `always` | `auto` | As for `POST /v1/extract`. |
| `transcribe` | boolean | `false` | As for `POST /v1/extract`. |
| `languages` | list of language codes | empty | As for `POST /v1/extract`. |
| `pii` | mode or object | unset | As for `POST /v1/extract`. |
| `chunking` | object | unset | As for `POST /v1/extract`. |
| `describe_images` | object | unset | As for `POST /v1/extract`. |

<!-- request: POST /v1/structured -->
```json
{"url": "https://example.org/mathe/brueche", "structured": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}, "strategy": "metadata"}}
```

## `POST /v1/chunk`

A page by its URL, or your own text, split into chunks. Exactly one of `url` and `text`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `url` | string | unset | The page to extract and chunk, with the options of `POST /v1/extract`. |
| `text` | string | unset | Your own text to chunk, at most `DOCUMENT_MAX_BYTES`. With `text` the fields of a page (`mode`, `links`, `compliance`, `cache`, `actions`, `cookies`, `headers`, `screenshot`, `full_page`, `ocr`, `transcribe`, `languages`, `describe_images`) answer 422. |
| `text_type` | `markdown` or `plain` | `markdown` | What `text` is; Markdown chunks carry their heading path. |
| `chunking` | object | required | How to split. See [`chunking`](#chunking). |
| `format` | `markdown` or `text` | `markdown` | The format of `content` and of the chunks. |
| `mode` | `fast`, `auto` or `browser` | `auto` | As for `POST /v1/extract`. |
| `metadata` | boolean | `true` | As for `POST /v1/extract`. |
| `links` | boolean | `false` | As for `POST /v1/extract`. |
| `compliance` | `enforce`, `report` or `ignore` | unset | As for `POST /v1/extract`. |
| `cache` | boolean | `true` | As for `POST /v1/extract`. |
| `wait_seconds` | number, 0 to 60 | unset | As for `POST /v1/extract`. |
| `actions` | list of objects | empty | As for `POST /v1/extract`. |
| `cookies` | list of objects | empty | As for `POST /v1/extract`. |
| `headers` | object | empty | As for `POST /v1/extract`. |
| `screenshot` | boolean | `false` | As for `POST /v1/extract`. |
| `full_page` | boolean | `true` | As for `POST /v1/extract`. |
| `ocr` | `auto`, `never` or `always` | `auto` | As for `POST /v1/extract`. |
| `transcribe` | boolean | `false` | As for `POST /v1/extract`. |
| `languages` | list of language codes | empty | As for `POST /v1/extract`. |
| `pii` | mode or object | unset | As for `POST /v1/extract`. |
| `structured` | object | unset | As for `POST /v1/extract`. |
| `describe_images` | object | unset | As for `POST /v1/extract`. |

<!-- request: POST /v1/chunk -->
```json
{"text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.\n\n## Addieren\n\nGleiche Nenner bleiben.", "chunking": {"strategy": "markdown", "size": 500, "overlap": 0}}
```

## `POST /v1/batch`

Many URLs as one job; every item has the options of `POST /v1/extract`, without the browser
options and `wait_seconds`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `urls` | list of strings | required | The URLs, at most `BATCH_MAX_URLS`. A URL twice gets two items; an invalid URL an item that failed with `invalid_url`. |
| `mode` | `fast`, `auto` or `browser` | `auto` | As for `POST /v1/extract`; `browser` queues every URL for the browser workers. |
| `format` | `markdown`, `text` or `html` | `markdown` | As for `POST /v1/extract`. |
| `metadata` | boolean | `true` | As for `POST /v1/extract`. |
| `links` | boolean | `false` | As for `POST /v1/extract`. |
| `compliance` | `enforce`, `report` or `ignore` | unset | As for `POST /v1/extract`. |
| `cache` | boolean | `true` | As for `POST /v1/extract`. |
| `ocr` | `auto`, `never` or `always` | `auto` | As for `POST /v1/extract`. |
| `transcribe` | boolean | `false` | As for `POST /v1/extract`. |
| `languages` | list of language codes | empty | As for `POST /v1/extract`. |
| `pii` | mode or object | unset | As for `POST /v1/extract`, for every item. |
| `structured` | object | unset | As for `POST /v1/extract`, for every item. |
| `chunking` | object | unset | As for `POST /v1/extract`, for every item. |
| `describe_images` | object | unset | As for `POST /v1/extract`, for every item. |

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/a", "https://example.org/b"], "format": "markdown", "chunking": {"strategy": "sentence", "size": 800}}
```

## `POST /v1/site-structure`

The map of a site, as a job: its sitemaps and its link graph from the start page. Pages are read
over HTTP only, without their text.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `url` | string | required | The start page. |
| `depth` | integer, 0 to `SITE_MAX_DEPTH` | `2` | How many links deep the crawl goes from the start page; `0`: the start page and the sitemaps only. |
| `max_pages` | integer, 1 to `SITE_MAX_PAGES` | `100` | Most pages, the start page included. |
| `sitemaps` | boolean | `true` | Read robots.txt and the sitemaps it lists (or `/sitemap.xml`). |
| `scope` | `host` or `path` | `host` | Which links are followed: `host` the start page's host, `path` only URLs under the start URL's directory. |
| `metadata` | boolean | `true` | Include each page's metadata. |
| `cache` | boolean | `true` | Use the result cache for pages. |
| `compliance` | `enforce`, `report` or `ignore` | unset | As for `POST /v1/extract`; under `enforce` a page that robots.txt or a TDM opt-out forbids is not fetched. |

<!-- request: POST /v1/site-structure -->
```json
{"url": "https://example.org/", "depth": 2, "max_pages": 100, "scope": "host"}
```

## `POST /v1/screenshot`

A PNG of a page, rendered in the browser.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `url` | string | required | The page to capture. |
| `full_page` | boolean | `true` | The whole page (up to `SCREENSHOT_MAX_HEIGHT_PX`); `false`: the first 1920×1080. |
| `actions` | list of objects | empty | Page actions before the capture, as for `POST /v1/extract`. |
| `cookies` | list of objects | empty | As for `POST /v1/extract`. |
| `headers` | object | empty | As for `POST /v1/extract`. |
| `compliance` | `enforce`, `report` or `ignore` | unset | As for `POST /v1/extract`. |
| `wait_seconds` | number, 0 to 60 | unset | As for `POST /v1/extract`: after this many seconds the capture becomes a job. |

<!-- request: POST /v1/screenshot -->
```json
{"url": "https://example.org/", "full_page": false}
```

## `POST /v1/extract/file`

An upload as `multipart/form-data`: the file in the part `file`, and these form fields. The
options objects are JSON strings.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `format` | `markdown`, `text` or `html` | `markdown` | The format of `content`. |
| `metadata` | boolean | `true` | Include the document's metadata (title, authors, licence, dates). |
| `ocr` | `auto`, `never` or `always` | `auto` | As for `POST /v1/extract`. |
| `wait_seconds` | number, 0 to 60 | `25` | After this many seconds a running upload becomes a job (202). |
| `pii` | mode or JSON object | unset | As for `POST /v1/extract`. |
| `structured` | JSON object | unset | As for `POST /v1/extract`. |
| `chunking` | JSON object | unset | As for `POST /v1/extract`. |
| `describe_images` | JSON object | unset | Describe an uploaded PNG, JPEG or WebP; as for `POST /v1/extract`. |

```bash
curl -H "Authorization: Bearer $KEY" -F file=@report.pdf -F format=markdown \
  -F 'chunking={"strategy": "markdown", "size": 800}' http://localhost:8080/v1/extract/file
```

## Option objects

### `pii`

A bare string is the mode: `"pii": "mask"` is `"pii": {"mode": "mask"}`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `mask`, `redact`, `hash` or `annotate` | required | What happens to an entity. |
| `entities` | list of entity types | unset | Which entities to find; unset: the operator's `PII_DEFAULT_ENTITIES`. |
| `language` | `auto`, `de` or `en` | `auto` | The language of the text. |
| `score_threshold` | number | unset | The least score of an entity; unset: `PII_SCORE_THRESHOLD`. |

### `structured`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `schema` | JSON schema | unset | The fields to fill. |
| `strategy` | `metadata`, `selectors`, `patterns`, `llm` or `auto` | `auto` | Where the values come from; `auto` tries the others in turn and asks the LLM only for what is missing. |
| `selectors` | object | empty | CSS rules per field (XPath for admins). |
| `patterns` | object | empty | Regular expressions per field, on the content. |
| `tables` | boolean | `false` | Also return the page's tables. |
| `learn_selectors` | boolean | `false` | Let the LLM propose selectors that the next pages of the site reuse. |
| `instructions` | string | unset | Extra instructions for the LLM. |

### `chunking`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `strategy` | `fixed`, `recursive`, `markdown`, `sentence` or `semantic` | `recursive` | Where to cut. |
| `size` | integer | `1000` | Most characters per chunk. |
| `overlap` | integer | `100` | Characters that two chunks share. |
| `breakpoint_percentile` | integer | `90` | For `semantic`: how large a change of meaning cuts. |

### `describe_images`

`{}` takes the defaults.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `max_images` | integer, 1 to 20 | `5` | Most images to describe. |
