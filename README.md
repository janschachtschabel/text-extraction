# text-extraction

Deutsch: [README.de.md](README.de.md)

A FastAPI service that turns web pages, files, audio and video into clean text or Markdown, with
their metadata. It reads a page over HTTP first and renders it in a browser when the page needs
it. It also converts documents, transcribes audio and video, handles personal data, fills
structured fields, splits text into chunks and maps whole sites. It has a REST API, an MCP server
and a playground in English and German.

## Status

Prototype `v0.1.1`: packages 0-13 are merged (foundation, service skeleton, fast extraction,
jobs and batch, browser worker, documents and OCR, audio and video, PII, structured output and
chunking, site structure, MCP and UI, hand-over, UI languages with the unblock option and
bilingual docs, YouTube transcript panel). See
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
[Site rules](docs/user-guide/en/site-rules.md) and
[Browser fallback](docs/user-guide/en/usage.md#browser-fallback).

## Quick start with Docker

Requires Docker with Compose. Compose starts the API, an extract worker, a browser worker and a
Valkey (with a password) for the shared state.

```bash
cp .env.example .env                           # then set API_KEYS and VALKEY_PASSWORD in it
KEY=$(openssl rand -hex 32)                    # your API key: keep it
printf %s "$KEY" | sha256sum | cut -d' ' -f1   # its sha256: replace the zeros in API_KEYS with it
openssl rand -hex 24                           # a VALKEY_PASSWORD
docker compose up --build -d --wait          # public API on http://localhost:8080
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

`.env.example` has one admin key named `local`, whose SHA-256 you replace; see
[API keys](docs/user-guide/en/usage.md#api-keys). The playground is at http://localhost:8080/ui
and the MCP endpoint at http://localhost:8080/mcp.
[Installation](docs/user-guide/en/installation.md) covers the local setup with uv, the optional
workers (OCR, personal data, LLM, speech recognition), Kubernetes and the key settings.
Without a build, `docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait`
runs the released images: see
[Released images](docs/user-guide/en/installation.md#released-images).

## Use cases

- One page as text or Markdown, with its metadata: [One URL](docs/user-guide/en/single-url.md).
- Documents, audio and video, personal data and image descriptions:
  [Files, media and personal data](docs/user-guide/en/usage.md#files-media-and-personal-data).
- Part of a site, or a list of URLs or a CSV:
  [Part of a site](docs/user-guide/en/part-of-a-site.md).
- A whole site with the metadata and licences of its pages:
  [A whole site and its metadata](docs/user-guide/en/whole-site-and-metadata.md).
- Structured data and chunks: [Structured data](docs/user-guide/en/structured-data.md).
- An AI assistant with the 11 tools of the MCP server: [MCP](docs/user-guide/en/mcp.md).
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
extracts the pages you pick: see [Part of a site](docs/user-guide/en/part-of-a-site.md) and
[A whole site and its metadata](docs/user-guide/en/whole-site-and-metadata.md).

## MCP

With `MCP_ENABLED=true` the service is an MCP server at `https://<host>/mcp` with 11 tools, for
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
