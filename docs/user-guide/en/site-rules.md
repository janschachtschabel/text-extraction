# Site rules

Deutsch: [Website-Regeln](../de/site-rules.md)

By default the service respects a site's rules. Maximum extraction is for sites that allowed
extraction by contract; only an admin may use it, and only where the operator switched it on. The
checks can also be set for each run: a key that is allowed to choose it can switch them off with
`compliance: ignore`. This page explains the default, maximum extraction, the pause a host may ask
for, and how to choose in the API, the MCP server and the playground.

## Respect the site's rules

This is the default. The service:

- checks robots.txt and the EU TDM opt-out (the site's reservation against text and data mining)
  before it fetches a page;
- sends only a few requests to a host at a time (`OUTBOUND_MAX_CONCURRENCY_PER_HOST`, 2) with a
  gap between them (`OUTBOUND_MIN_DELAY_MS`, 0.5 s), and pauses a host that answered 429 or 503;
  a request it holds back for this answers `rate_limited` (retryable), with the wait in
  `error.details` (`retry_after_seconds`);
- sends its own, honest user agent.

`compliance` decides what the checks do:

| `compliance` | What happens |
| --- | --- |
| `enforce` | A page that robots.txt forbids fails with `blocked_by_robots`, a page whose TDM rights are reserved with `tdm_reserved`. |
| `report` | The page is extracted; `data.compliance` reports what the checks found. |
| `ignore` | Admins only (or where it is the default): the checks are skipped. With unblock mode this is maximum extraction. |

A request without `compliance` gets the operator's `COMPLIANCE_DEFAULT` (`enforce`). Every result
carries the report in `data.compliance`: the `mode` that applied, the `decision` (`allowed`,
`blocked`, `reported` or `skipped`), what robots.txt said and where a TDM statement came from.

## Maximum extraction

### Who may use it

Maximum extraction needs three things:

- an admin key (a user key that sends `compliance: ignore` gets 403 `forbidden`);
- `"compliance": "ignore"` in the request (or none, where the operator set
  `COMPLIANCE_DEFAULT=ignore`);
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

### Sites behind a bot protection

Many sites sit behind a bot protection such as Cloudflare. It may refuse the service with a bot
check, while the same page opens in a normal browser. The service then answers `failed` with
`bot_protection`, `upstream_status` 403, 429 or 503, and an `error.message` such as "The target
answered HTTP 403 with a bot challenge (Cloudflare).".

- The normal browser says honestly that it is an automated, headless browser
  (`HeadlessChrome` and `text-extraction/<version>` in its user agent). A protection that refuses
  headless browsers refuses it from any network, so `mode: browser` does not help.
- A data-centre IP address (a VPS, a cloud server) gets refused more often than a home
  connection, even in the fast mode.
- Ways past it: the site's operator allows the service (the service's user agent `text-extraction`
  or the server's outgoing IP address), or, for a site that allowed extraction by contract, maximum
  extraction with `BROWSER_UNBLOCK_ENABLED=true` (see above).

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

The MCP client's key must be allowed to choose the value (see
[Switch the checks per run](#switch-the-checks-per-run)). `compliance` goes into `options`, for
example with `extract_url`:

```json
{"url": "https://example.org/partner-page", "options": {"compliance": "ignore"}}
```

`batch_extract`, `extract_structured`, `chunk`, `screenshot_url` and `site_structure` take it the
same way. See [MCP](mcp.md).

## Switch the checks per run

`compliance` (`enforce`, `report` or `ignore`) is a field of the request, so you choose the
checks for each run and not only for the whole deployment:

- One request: `compliance` in the body of `POST /v1/extract`, `/v1/structured`, `/v1/chunk`
  and `/v1/screenshot`.
- One batch: `compliance` in the body of `POST /v1/batch`. The one value applies to all of its
  URLs.
- One site run: `compliance` in the body of `POST /v1/site-structure`. It applies to the whole run.
- MCP: `compliance` goes into `options` of the tool.

A key may only choose what it is allowed to: an admin chooses all three values; another key
chooses `enforce` or `report` while the operator allows overrides (`COMPLIANCE_ALLOW_OVERRIDE`),
and `ignore` only where it is the operator's default. REST refuses any other value with 403
`forbidden`.

## In the playground

The choice "Site rules" has three steps. It is in every form with a URL (**Extract**,
**Structured**, **Screenshot**, **Chunk** for a URL, **Batch**, **Batch file**,
**Site structure**) and under the site tree, for "Extract selected". The **File** tab has none.

| Step | Sends | Meaning |
| --- | --- | --- |
| "Respect the site's rules" | `compliance: enforce` | robots.txt and TDM reservations refuse a page. |
| "Check and report only" | `compliance: report` | The checks run and are reported; nothing is refused. |
| "Checks off (contract partners)" | `compliance: ignore` | The checks are skipped. For sites that allowed extraction by contract. |

The operator's default (`COMPLIANCE_DEFAULT`) is preselected and marked "(default)"; it sends
nothing, so the operator's value applies. Choosing another step sends `compliance`. Who sees which
step:

| Who | Steps |
| --- | --- |
| Admins (and every visitor under `AUTH_MODE=none`) | all three |
| Other keys, `COMPLIANCE_ALLOW_OVERRIDE` on | "Respect the site's rules" and "Check and report only"; "Checks off" only when it is the default |
| Other keys, `COMPLIANCE_ALLOW_OVERRIDE` off | no choice |

"Checks off" works without unblock mode: the checks are skipped and the normal browser is used.
Where the operator also switched on unblock mode (`BROWSER_ENABLED` and
`BROWSER_UNBLOCK_ENABLED`), it is maximum extraction as described above. If you also set
`compliance` in "Options (JSON)" (under "Advanced"), the form refuses it: compliance is given
twice. With "Browser fallback" set to "never", maximum extraction stays with HTTP. The result
shows the compliance mode, and the badge "maximum extraction (unblocked)" when `data.unblocked` is
true. A `bot_protection` error comes with a note on the ways past the protection (left out after
an unblocked render), and an admin whose server runs the browser without unblock mode reads at
"Checks off" that `BROWSER_UNBLOCK_ENABLED` is off.

In the site tree, URLs that the site's rules block are marked; see
[Part of a site](part-of-a-site.md#in-the-playground).
