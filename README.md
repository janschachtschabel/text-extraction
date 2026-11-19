# text-extraction

Deutsch: [README.de.md](README.de.md)

A FastAPI service that turns web pages, files, audio and video into clean text or Markdown, with
their metadata. It reads a page over HTTP first and renders it in a browser when the page needs
it. It also converts documents, transcribes audio and video, handles personal data, fills
structured fields, splits text into chunks and maps whole sites. It has a REST API, an MCP server
and a playground in English and German.

## Features

- **Fast mode and browser mode.** A page is read over HTTP first (`fast`) and rendered in
  Chromium when it needs it (`auto`: a JavaScript shell, a bot challenge, a soft 404 or too little
  text), or always (`browser`). Cookie banners are declined automatically; page actions (`click`,
  `type`, `scroll`, `accept_cookies`, ...), cookies and headers work in the browser.
- **Clean text.** Markdown, plain text or HTML without navigation, footers and other boilerplate;
  MathML becomes LaTeX; PHP error output that a site prints into its pages is removed and listed.
  A thin page also gets its collapsed solutions, up to two attachments, an edu-sharing node's file
  and, with `transcribe`, its one video.
- **Metadata.** Title, description, authors, dates, language, images, canonical URL, keywords,
  schema.org and LRMI fields, the licence (Creative Commons and others, with its source and
  confidence), the page type (such as `content`, `listing` or `imprint`) and the links with their
  category, and the places it names (address and coordinates).
- **Documents.** PDF, DOCX, PPTX, XLSX, XLS, EPUB, CSV, SVG (its text), HTML, plain text and Markdown, by URL or
  as an upload (up to 20 MB, PDFs up to 100 pages). With the OCR worker (Docling) also scans,
  images (PNG, JPEG, TIFF, WebP, BMP) and OpenDocument files (ODT, ODS, ODP). The bytes decide the
  type, not the declared `Content-Type`.
- **Audio and video.** Subtitles and details of YouTube, Vimeo and the Mediatheken of ARD, ZDF,
  arte and 3sat; YouTube's transcript view in the browser when the subtitles are blocked;
  `<track>` texts of a page's players. With the ASR worker (faster-whisper, or an
  OpenAI-compatible endpoint) a transcription of MP3, MP4, M4A, MOV, WebM, MKV, Ogg, WAV, FLAC and
  AAC, by URL or as an upload (up to 25 MB).
- **Screenshots.** PNG, JPEG or WebP of the whole page or the first screen, in any window size
  from 320×240 to 3840×2160, scaled down on request.
- **Personal data.** Persons, e-mail addresses, phone numbers, IBANs, credit cards and IP addresses
  (and on request places, organisations and URLs) in German and English are masked, redacted,
  hashed or annotated (Presidio and spaCy).
- **Structured data and chunks.** The fields of a JSON schema from the metadata, CSS selectors
  (XPath for admins), regular expressions or an LLM (any OpenAI-compatible endpoint); the page's tables;
  chunks (`fixed`, `recursive`, `markdown`, `sentence`, `semantic`); image descriptions by the LLM.
- **Many pages and whole sites.** Batches of up to 1,000 URLs as jobs (the playground also reads
  a CSV or JSON file); the map of a site from its sitemaps and its link graph, with page types,
  shown in the playground as a tree that marks the pages robots.txt forbids.
- **Web search.** A search through the service's own SearXNG, which asks only sources with an
  official, free API (Wikipedia, Wikibooks, Wikiversity, Wikinews, Mwmbl, OpenAlex, arXiv,
  Crossref); the playground extracts the hits you tick as a batch. On in docker compose, off in
  Helm.
- **Site rules.** robots.txt and the EU TDM opt-out (`enforce`, `report` or `ignore` per request),
  a few requests per host at a time, an honest user agent. For sites that allowed extraction by
  contract an admin can switch the checks off and use the unblock browser (see below).
- **Interfaces.** A REST API with OpenAPI at `/docs`, jobs for long work, an MCP server with 12
  tools and a playground at `/ui` in English and German.
- **Operations.** Docker Compose and a Helm chart; stateless pods with the state in Valkey;
  API keys with the roles `user` and `admin` and a rate limit per key; a result cache; an SSRF
  guard and byte and time limits on every fetch; JSON logs, Prometheus metrics and health checks
  on an internal port; trace headers passed through.

## Endpoints

| Endpoint | What it does |
| --- | --- |
| `POST /v1/extract` | One page, document or video by its URL. |
| `POST /v1/extract/file` | An uploaded file (document, image, audio, video). |
| `POST /v1/screenshot` | An image of a page. |
| `POST /v1/structured` | The fields of a JSON schema from a page. |
| `POST /v1/chunk` | A page or a text split into chunks. |
| `POST /v1/batch` | Many URLs as one job. |
| `POST /v1/site-structure` | The map of a site, as a job. |
| `POST /v1/search` | A web search through the service's SearXNG: the hits with URL, title and snippet. |
| `GET /v1/jobs/{id}`, `DELETE /v1/jobs/{id}` | A job's state and results; cancel it. |
| `GET /v1/jobs/{id}/artifacts/{name}` | A file of a job, such as a screenshot. |
| `GET /v1/info` | The service's version, the key's role, the features the key can use and the limits. |

Every answer is the same result envelope (`status`, `error.code`, `error.retryable`,
`upstream_status`, `mode_used`, timings): when the target fails, the API still answers 200 with
`status: failed`. See [Usage](docs/user-guide/en/usage.md) and
[Parameters](docs/user-guide/en/parameters.md).

## Status

Prototype `v0.1.14`: packages 0-26 are merged (foundation, service skeleton, fast extraction, jobs
and batch, browser worker, documents and OCR, audio and video, PII, structured output and
chunking, site structure, MCP and UI, hand-over, UI languages with the unblock option and
bilingual docs, YouTube transcript panel, screenshot formats and page types, playground redesign,
robots marks in the site tree with a per-run site rules switch, playground fixes after the v0.1.4
test, site tree with types, columns and a fast sitemap reading, optional features on by default,
bot-challenge refusals reported as `bot_protection`, link lists of listing pages, KMap
attachments, tables and formulas, more text from thin pages, the site tree on top, MCP agent
usability, free web search through SearXNG, site APIs as link sources and places in
metadata).
See [docs/packages.md](docs/packages.md#status) for the implementation packages and their status.

## Two ways to extract

- **Respect the site's rules** (the default): robots.txt and the EU TDM opt-out are checked, every
  host gets only a few requests at a time, and the service sends its own honest user agent.
- **Maximum extraction**, for sites that allowed extraction by contract: an admin sends
  `"compliance": "ignore"`, and where the operator switched on `BROWSER_UNBLOCK_ENABLED` (on in
  docker compose), a stealth browser gets past bot protection, also after a plain 403, 429 or 503. It solves no
  image captchas, uses no paid service and rotates no IP address; the per-host limits still
  apply. The checks can be switched per run (`compliance`: `enforce`, `report` or `ignore`); the
  playground offers "Respect the site's rules", "Check and report only" and "Checks off
  (contract partners)" by role.

In both, the browser fallback (`mode`) decides whether a page is rendered in the browser: when
the page needs it (`auto`, the default), never (`fast`) or always (`browser`). See
[Site rules](docs/user-guide/en/site-rules.md) and
[Browser fallback](docs/user-guide/en/usage.md#browser-fallback).

## Quick start with Docker

Requires Docker with Compose. Compose starts the API, an extract worker, a browser worker, the
optional workers for OCR, transcription, personal data and the LLM (their first start downloads
about 3 GB of models), a Valkey (with a password) for the shared state and SearXNG for the web
search. The root `docker-compose.yml` runs the published images and
needs no variables: its service `init` creates the Valkey password and an admin API key on the
first start.

```bash
docker compose up -d --wait                    # public API on http://localhost:8080
KEY=$(docker compose run --rm -T init text-extraction-init --show-admin-key)
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

The playground is at http://localhost:8080/ui and the MCP endpoint at http://localhost:8080/mcp;
log in with the key. The same steps for Windows PowerShell are under
[Published images](docs/user-guide/en/installation.md#published-images). Hostinger's Docker
Manager deploys the same file from this repository's URL: see
[Hostinger VPS](docs/user-guide/en/installation.md#hostinger-vps).

From version 0.1.13 on, `--new-key <name>` instead of `--show-admin-key` makes one more key
(for another person, or with the role `user`) and prints the line for `API_KEYS`;
[API keys: show, create, enter](docs/user-guide/en/installation.md#api-keys-show-create-enter)
shows where it goes with Docker, on Hostinger and without Docker.
[Installation](docs/user-guide/en/installation.md) also covers HTTPS with `DOMAIN`, building the
images with `docker-compose.build.yml`, the local setup with uv, the optional workers (OCR,
personal data, LLM, speech recognition), Kubernetes and the key settings.

## Updating

Redeploy (on Hostinger: redeploy from the repository URL) or run `docker compose up -d --wait`:
the root `docker-compose.yml` pulls the newest `0.1.x` release on every `docker compose up` and
every redeploy (tag `0.1`, `pull_policy: always`); a container the Docker daemon merely restarts
keeps its image, and nothing updates running containers by itself. Each `up` therefore needs
ghcr.io. Set `TEXT_EXTRACTION_VERSION` in `.env` only to pin one release, for example `0.1.10`;
remove an old pinned value to follow the newest again. Your `.env` is kept, and a value in it wins
over a changed default: compare it with `.env.example` after an update.
[CHANGELOG.md](CHANGELOG.md) lists every change and, under "Upgrade", what to do.

## Use cases

- One page as text or Markdown, with its metadata: [One URL](docs/user-guide/en/single-url.md).
- Documents, audio and video, personal data and image descriptions:
  [Files, media and personal data](docs/user-guide/en/usage.md#files-media-and-personal-data).
- Part of a site, or a list of URLs or a CSV:
  [Part of a site](docs/user-guide/en/part-of-a-site.md).
- A whole site with the metadata and licences of its pages:
  [A whole site and its metadata](docs/user-guide/en/whole-site-and-metadata.md).
- Structured data and chunks: [Structured data](docs/user-guide/en/structured-data.md).
- Finding pages on a topic and extracting them: [Search](docs/user-guide/en/search.md).
- An AI assistant with the 12 tools of the MCP server: [MCP](docs/user-guide/en/mcp.md).
- Respecting a site's rules, or maximum extraction for contract partners:
  [Site rules](docs/user-guide/en/site-rules.md).
- Trying it out in the browser: [the playground](docs/user-guide/en/usage.md#the-playground).

Every field of each request is in [Parameters](docs/user-guide/en/parameters.md).

## Structured output and chunking

`structured` fills the fields of a JSON schema from a page, from its metadata, with your
selectors or patterns, or with an LLM, and `chunking` splits the content into chunks: see
[Structured data](docs/user-guide/en/structured-data.md).

## Site structure

`POST /v1/site-structure` maps a site, its sitemaps and its link graph, as a job, and a batch then
extracts the pages you pick. The crawl also reads the feeds and the WordPress and Drupal content
lists a site publishes (`apis`): see [Part of a site](docs/user-guide/en/part-of-a-site.md) and
[A whole site and its metadata](docs/user-guide/en/whole-site-and-metadata.md).

## MCP

With `MCP_ENABLED=true` (on in docker compose) the service is an MCP server at `https://<host>/mcp` with 12 tools, for
Claude Code, Claude Desktop and other clients, authenticated with an API key of the service: see
[MCP](docs/user-guide/en/mcp.md).

## Documentation

- [User guide](docs/user-guide/en/index.md): installation, usage, the use cases, parameters and
  the glossary (also in [German](docs/user-guide/de/index.md))
- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md): environment variables, secrets, ports, headers
- [Helm chart](deploy/helm/text-extraction/README.md)
- [Hand-over](docs/handover.md): the ops guide's hand-over checklist, costs, maintenance and
  releases
- [CLAUDE.md](CLAUDE.md): rules and workflow for contributors

## Licence

Apache-2.0. See [LICENSE](LICENSE).
