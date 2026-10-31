# User guide

Deutsch: [Anleitung](../de/index.md)

text-extraction turns web pages, files, audio and video into clean text or Markdown, with their
metadata. This guide explains how to install it, how to use the API, the MCP server and the
playground, and what each parameter does. It exists in English and in German, with the same
pages.

## Pages

- [Installation](installation.md): locally with uv, with Docker Compose, on Kubernetes, and the
  key settings.
- [Usage](usage.md): API keys, the result envelope, sync and jobs, the browser fallback, files
  and media, personal data, and the playground.
- [Parameters](parameters.md): every field of each request body, with its type, its default and
  its meaning.
- [Site rules](site-rules.md): respecting a site's rules, and maximum extraction for contract
  partners.
- [One URL](single-url.md): one page in the playground, with REST and with MCP, and the fields
  of the answer.
- [Part of a site](part-of-a-site.md): a section of a site, a list of URLs or a CSV.
- [A whole site and its metadata](whole-site-and-metadata.md): a site structure, batches of its
  pages with their metadata and licences, and reading the jobs.
- [Structured data](structured-data.md): the strategies of `structured`, the LLM and its key, and
  chunking.
- [MCP](mcp.md): the setup for Claude Code, Claude Desktop and other clients, and the 11 tools.
- [Glossary](glossary.md): the terms of this guide and of the playground, in English and German.

## Use cases

- Extract one page: [One URL](single-url.md).
- Extract many pages: [Batch](usage.md#batch).
- Extract part of a site, a list of URLs or a CSV: [Part of a site](part-of-a-site.md).
- Map a whole site and read the metadata of its pages:
  [A whole site and its metadata](whole-site-and-metadata.md).
- Read structured data and chunks: [Structured data](structured-data.md).
- Use the MCP server: [MCP](mcp.md).
- Extract a site that allowed it by contract: [Site rules](site-rules.md#maximum-extraction).

## For developers

These documents are in English only:

- [Architecture](../../architecture.md)
- [Configuration](../../configuration.md): every environment variable, the secrets, ports and
  headers
- [Hand-over](../../handover.md): operations, costs, maintenance and releases
