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
- "Site rules": respect the rules, check and report only, or checks off, by role; see [Site rules](site-rules.md#in-the-playground).
- "Metadata" (on by default), "Metadata, structured" (the schema.org and LRMI fields, read by
  name) and "Describe images".
- "Also take a screenshot": the whole page as a PNG, taken in the browser, so the request takes
  longer. The box is disabled with "Browser fallback: never" and while the browser is off; its
  hint says why. Format and size are on the Screenshot tab.
- "Optional features": "OCR (text recognition)" (automatic, never, always), "Personal data"
  (leave as is, mask, redact, hash, mark only) and "Transcribe audio and video without
  subtitles". Nothing is chosen by default. A feature the service has switched off is disabled,
  and its hint names the switch (`DOCLING_ENABLED`, `PII_ENABLED`, `ASR_ENABLED`, `LLM_ENABLED`);
  so is a feature the service keeps for admin keys while you are signed in with a user key
  (`OCR_ALLOW_USERS`, `PII_ALLOW_USERS`, `ASR_ALLOW_USERS`, `LLM_ALLOW_USERS`).
- "Options (JSON)", under "Advanced" (a closed section, with "Wait seconds before it becomes a
  job"), takes any further field of the REST body, such as `{"links": true}`.

The result starts with a status line (the status and the HTTP status), a toolbar and a card of
figures: URL, language, words, mode used, page type and time. The toolbar has "Copy Markdown"
(or "Copy text"; it appears only where the browser allows the clipboard), the download of the
content and "Save response (JSON)", the whole answer as a file. Below come closed sections for the
screenshot (only when you asked for one), the metadata, the links, the images, the removed server
error messages (only when there are some) and "What happened", a timeline of the checks, the
fetch, the render and the conversion (it opens by itself when there are warnings), and after them
the content in a reading card (for Markdown, its source is under "Show source"). At the end, "As curl"
is the same request for the shell and "Raw JSON" the whole answer.

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
| `page_type` | What kind of page it is, by rules and without an LLM: its `type` (such as `content`, `listing`, `imprint` or `contact`) and the `signals` that say so (such as `url`, `title` or `schema_org`). HTML pages only; null for documents, media, plain text, Markdown, uploads and a page that failed with an HTTP error (4xx or 5xx). See [Page types and link categories](whole-site-and-metadata.md#page-types-and-link-categories). |
| `links` | With `links: true`: the page's links, each with its `url`, `text`, `kind` (`internal`, `external` or `file`) and `category`, what it leads to (such as `imprint`, `social` or `download`). |
| `compliance` | What the site rules found: the `mode`, the `decision` (`allowed`, `blocked`, `reported`, `skipped`), `robots` and the TDM statement. See [Site rules](site-rules.md). |
| `warnings` | What did not go as asked, although a result is returned, such as `browser_unavailable` (the browser did not take the page: the HTTP result) or `server_errors_removed` (see below). |
| `server_errors` | Error output of the site's server that was removed from the text, or null when there was none or when PII handling was requested (the messages are page text): a list of `level` (such as `Warning`, `Notice` or `Fatal error`), `message`, `file`, `line` and `count` (identical messages are merged), at most 50. |

Some sites print PHP error output into their pages, such as `Warning: Undefined variable $x in
/var/www/page.php on line 12`. The conversion removes such a message from the text when it starts a
line, as PHP writes it. A message quoted inside a sentence stays, and so does one inside `pre`,
`code`, `kbd`, `samp`, `var`, `tt`, `textarea` and `script`, where it is part of the content. For a
removed message the conversion adds the warning `server_errors_removed` and lists the messages in
`data.server_errors`. The playground shows them in a closed section after "Images". A message that
spans several lines stays in the text: PHP 8 writes an uncaught exception ("Fatal error: Uncaught
…") with its stack trace, and that is not removed.

A message that a page quotes stays as well: in a quote (`blockquote`, `q`, `cite`), a heading, a
list item, a table cell, a definition list or a figure caption only the form with a bold label and
a bold path is removed (`<b>Warning</b>: … in <b>/var/www/page.php</b> on line <b>12</b>`), as
PHP writes it with `html_errors` on. A message copied as plain text stays there. Outside those
places the plain form is removed too, so a forum post that pastes a message on a line of its own in
an ordinary paragraph loses it; in a quote it stays. Besides `.php` files the conversion
recognises `.inc`, `.phtml` and Drupal's `.module`, `.theme`, `.install` and `.engine`. A table
that Xdebug prints for a message (it starts with "( ! )") is removed as a whole and listed once.

A licence has an `id` (such as `CC-BY-SA-4.0`), a `url`, a `label`, its `source` (a `rel-license`
link, JSON-LD, microdata, RDFa, a meta tag or the page text) and a `confidence`. `mode_used`
says whether HTTP or the browser produced the answer, and `upstream_status` is the page's HTTP
status.
