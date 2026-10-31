# MCP

Deutsch: [MCP](../de/mcp.md)

With `MCP_ENABLED=true` (Helm `mcp.enabled=true`; Docker Compose switches it on for the API) the
service is an MCP server at `https://<host>/mcp` (streamable HTTP, stateless, JSON answers). An
AI assistant such as Claude then extracts pages, files, sites and structured data with the 11
tools of the server. This page covers the setup, the authentication, the tools, the job pattern,
long content, errors and example prompts.

## Setup

### Claude Code

```bash
claude mcp add --transport http text-extraction https://<host>/mcp \
  --header "Authorization: Bearer <key>"
# local compose: http://localhost:8080/mcp
```

### Claude Desktop

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
Desktop after the change.

### Other clients

Any MCP client that speaks streamable HTTP and can send a header works. It needs three things,
whatever its configuration calls them:

- the URL `https://<host>/mcp`;
- the transport: streamable HTTP (sometimes called `http`);
- the header `Authorization: Bearer <key>`.

A client that cannot send headers can use `mcp-remote` as above. Clients that run in a browser
must be listed in `MCP_ALLOWED_ORIGINS`; a request with any other `Origin` is refused.

## Authentication

An MCP client authenticates with an API key of the service, exactly as a REST client does (see
[API keys](usage.md#api-keys)). Every tool call runs the REST route's checks, with the key's role,
and is counted against the key's rate limit. An admin key may also use `compliance: ignore`
(see [Site rules](site-rules.md)) and XPath selectors. Under `AUTH_MODE=none` leave the header
out.

A caller's LLM or ASR key goes the same way, as the headers `X-LLM-Api-Key` and `X-ASR-Api-Key`
(another `--header` each); it is never a tool argument.

## The 11 tools

| Tool | Purpose | Route | Main arguments (besides `options`) |
| --- | --- | --- | --- |
| `extract_url` | One page or document as Markdown or text, with metadata. | `POST /v1/extract` | `url`, `format` (`markdown`), `mode` (`auto`), `wait_seconds`, `content_offset`, `max_chars` |
| `extract_file` | A file you have, such as a PDF, as text. | `POST /v1/extract/file` | `content_base64` (a `data:` URL prefix allowed), `file_name`, `content_type`, `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `screenshot_url` | A PNG of a page. | `POST /v1/screenshot` | `url`, `full_page` (true), `wait_seconds`; the PNG is attached up to `MCP_IMAGE_MAX_BYTES` |
| `extract_structured` | The fields of a JSON schema from a page. | `POST /v1/structured` | `url`, `structured` (required), `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `chunk` | A page or a text split into chunks. | `POST /v1/chunk` | `url` or `text`, `chunking` (required), `text_type`, `wait_seconds`, `content_offset`, `max_chars` |
| `batch_extract` | Many URLs as one job. | `POST /v1/batch` | `urls`, `format`, `mode`; answers a job |
| `site_structure` | The map of a site, as a job. | `POST /v1/site-structure` | `url`, `depth` (2), `max_pages` (100), `scope` (`host`); answers a job |
| `get_job` | A job's state and one page of its items. | `GET /v1/jobs/{id}` | `job_id`, `offset`, `limit` (5, 1-50), `wait_seconds` (0, up to `MCP_WAIT_SECONDS`), `content_offset`, `max_chars`; no `options` |
| `get_job_screenshot` | The PNG of a screenshot job. | `GET /v1/jobs/{id}/artifacts/screenshot.png` | `job_id`; no `options` |
| `cancel_job` | Cancel a job and delete it with its results. | `DELETE /v1/jobs/{id}` | `job_id`; no `options` |
| `service_info` | The service's name and version, and the key's name and role. | `GET /v1/info` | none |

`options` takes any further field of the route's REST body (see [Parameters](parameters.md)),
for example `{"pii": {"mode": "mask"}}` or `{"metadata": false}`; a field given both as an
argument and in `options` is refused. A tool answer's structured content is the REST envelope,
and its text is the same JSON, after a one-line hint when there is something to do.

## Jobs and `get_job`

A call that takes longer than `MCP_WAIT_SECONDS` (25 s) answers a job: call `get_job` with its
`job_id` and `wait_seconds` until it is `completed`. `batch_extract` and `site_structure` always
answer a job. `get_job` shows `limit` items per call (5 by default); read on with `offset`.

```json
{"job_id": "<job id>", "wait_seconds": 25, "offset": 0, "limit": 5}
```

Jobs need `JOBS_ENABLED=true` (the default). Without jobs a slow call runs synchronously within
the route's own limits, and `batch_extract`, `site_structure` and the job tools answer 503.
`cancel_job` cancels a job you no longer need.

## Long content and `content_offset`

A long `content` is cut to `max_chars` (`MCP_MAX_CONTENT_CHARS`, 50,000; shared by the items of a
`get_job` page) with the warning `mcp_content_truncated`. The hint names the `content_offset` to
call again with, and the next call returns the content from there.

## Errors

- An answer that REST gives with 4xx or 5xx (a bad argument, a refused role, the rate limit) is a
  tool error with the REST error envelope.
- A page that failed (`status: failed`, a 200 in REST) is a normal answer: read its `error.code`
  and `error.retryable`.

## Example prompts

- "Extract https://example.org/mathe/brueche as Markdown and tell me its licence."
  (`extract_url`)
- "Read the name, the licence and the educational level of these three pages."
  (`extract_structured`)
- "Map the section https://example.org/mathe/ two links deep and extract every page of it."
  (`site_structure`, `get_job`, `batch_extract`)
- "Take a screenshot of https://example.org/ and describe the layout." (`screenshot_url`)
- "Split this text into chunks of at most 500 characters at its headings." (`chunk`)
