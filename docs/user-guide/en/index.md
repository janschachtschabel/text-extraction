# User guide

Deutsch: [Benutzerhandbuch](../de/index.md)

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
- [Glossary](glossary.md): the terms of this guide and of the playground, in English and German.

## Use cases

- Extract one page: [Usage](usage.md#the-result-envelope) and
  [`POST /v1/extract`](parameters.md#post-v1extract).
- Extract many pages: [Batch](usage.md#batch).
- Map a site or part of it: [Site structure](../../../README.md#site-structure) in the README.
- Read structured data and chunks: [Structured output and chunking](../../../README.md#structured-output-and-chunking)
  in the README.
- Use the MCP server: [MCP](../../../README.md#mcp) in the README.

## For developers

These documents are in English only:

- [Architecture](../../architecture.md)
- [Configuration](../../configuration.md): every environment variable, the secrets, ports and
  headers
- [Hand-over](../../handover.md): operations, costs, maintenance and releases
