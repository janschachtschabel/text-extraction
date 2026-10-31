# Site rules

Deutsch: [Website-Regeln](../de/site-rules.md)

Every request extracts in one of two ways. By default the service respects a site's rules.
Maximum extraction is for sites that allowed extraction by contract; only an admin may use it,
and only where the operator switched it on. This page explains both, the pause a host may ask
for, and how to choose the way in the API, the MCP server and the playground.

## Respect the site's rules

This is the default. The service:

- checks robots.txt and the EU TDM opt-out (the site's reservation against text and data mining)
  before it fetches a page;
- sends only a few requests to a host at a time (`OUTBOUND_MAX_CONCURRENCY_PER_HOST`, 2) with a
  gap between them (`OUTBOUND_MIN_DELAY_MS`, 1 s), and pauses a host that answered 429 or 503;
- sends its own, honest user agent.

`compliance` decides what the checks do:

| `compliance` | What happens |
| --- | --- |
| `enforce` | A page that robots.txt forbids fails with `blocked_by_robots`, a page whose TDM rights are reserved with `tdm_reserved`. |
| `report` | The page is extracted; `data.compliance` reports what the checks found. |
| `ignore` | Admins only: the checks are skipped. With unblock mode this is maximum extraction. |

A request without `compliance` gets the operator's `COMPLIANCE_DEFAULT` (`enforce`). Every result
carries the report in `data.compliance`: the `mode` that applied, the `decision` (`allowed`,
`blocked`, `reported` or `skipped`), what robots.txt said and where a TDM statement came from.

## Maximum extraction

### Who may use it

Maximum extraction needs three things:

- an admin key (a user key that sends `compliance: ignore` gets 403 `forbidden`);
- `"compliance": "ignore"` in the request;
- `BROWSER_UNBLOCK_ENABLED=true` (with `BROWSER_ENABLED=true`), switched on by the operator.

With `compliance: ignore` but unblock mode off, the checks are skipped and the normal browser is
used: that is not maximum extraction. Under `AUTH_MODE=none` every caller is an admin. Use
maximum extraction only for a site whose owner allowed it.

### What it does

- The robots.txt and TDM checks are off.
- A page that goes to the browser loads in a stealth Chrome with Chrome's own user agent. The
  browser waits up to `BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS` (20 s) for a bot check to clear,
  and clicks a visible checkbox challenge, at most twice per page.
- With `mode: auto` it falls back to the browser more often: also when the page refuses
  (HTTP 403, 429 or 503) and when an HTML page gives too little text (`content_too_short`,
  `empty_content`). The answer is the better of the HTTP and the browser result.
- `data.unblocked` is true when the page was loaded this way.

### What it does not do

- It solves no image or puzzle captchas: such a page fails with `bot_protection` and the warning
  `challenge_not_cleared`.
- It uses no paid or third-party service.
- It rotates no IP address and no proxy.
- The protections of the service stay: the guard against internal addresses on every request
  the page makes, the byte and time limits, and the per-host limits with their pause.

Where you can, ask the site owner for an allowlist rule instead: a secret header that you send
with `headers`, or an exemption for the service's outgoing IP address.

## The pause after a 429 or 503

A host that answers 429 or 503 gets a pause: as long as its `Retry-After` says, else
`OUTBOUND_BACKOFF_SECONDS` (30 s), at most `OUTBOUND_MAX_BACKOFF_SECONDS` (300 s). Maximum
extraction keeps it: the browser takes the page only after the pause.

- A request without `wait_seconds` does not wait that long. It often answers the HTTP failure
  with the warning `browser_unavailable`.
- A request with `wait_seconds` becomes a job, and the job finishes after the pause. For such
  sites, send `wait_seconds` or use a batch.

## With REST

Send `compliance` with an admin key:

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/partner-page", "compliance": "ignore", "wait_seconds": 10}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/partner-page", "compliance": "ignore", "wait_seconds": 10}' \
  http://localhost:8080/v1/extract
```

`POST /v1/batch`, `/v1/structured`, `/v1/chunk`, `/v1/screenshot` and `/v1/site-structure` take
`compliance` the same way. A site structure reads its pages over HTTP only, so there
`compliance: ignore` only switches the checks off.

## With MCP

The MCP client's key must be an admin key. `compliance` goes into `options`, for example with
`extract_url`:

```json
{"url": "https://example.org/partner-page", "options": {"compliance": "ignore"}}
```

`batch_extract`, `extract_structured`, `chunk`, `screenshot_url` and `site_structure` take it the
same way. See [MCP](mcp.md).

## In the playground

Admins see the choice "Site rules" on the tabs **Extract**, **Structured**, **Screenshot**,
**Chunk** (for a URL), **Batch**, **Batch file** (in the preview) and **Site structure**, and
with "Extract selected" in the site tree. The **File** tab has none.

- "Respect the site's rules (default)" sends no `compliance`.
- "Maximum extraction (contract partners)" sends `compliance: ignore`. If you also set
  `compliance` in "Options (JSON)", the form refuses it: compliance is given twice.

While `BROWSER_ENABLED` or `BROWSER_UNBLOCK_ENABLED` is off, the choice is disabled and its hint
says why. With "Browser fallback" set to "never", maximum extraction stays with HTTP. The result
shows the compliance mode, and the badge "maximum extraction (unblocked)" when `data.unblocked` is
true.
