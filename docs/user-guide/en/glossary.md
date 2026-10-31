# Glossary

Deutsch: [Glossar](../de/glossary.md)

The terms of this guide and of the playground, with the German term that the German guide and the
German playground use. A field name, a value and an error code stay as they are in both
languages, in backticks (`mode`, `auto`, `target_http_error`). The German texts address the
reader as "Sie" where an address is needed.

## Playground

The labels of the playground, as the English and the German playground show them.

| English | German | Meaning |
| --- | --- | --- |
| Extract | Extrahieren | The tab for one page by its URL. |
| File | Datei | The tab for an upload. |
| Screenshot | Screenshot | The tab for a PNG of a page. |
| Structured | Strukturiert | The tab for structured data from a page. |
| Chunk | Chunks | The tab that splits a page or a text into chunks. |
| Batch | Batch | The tab for many URLs as one job. |
| Batch file | Batch-Datei | The tab for a batch from a CSV or JSON file. |
| Site structure | Website-Struktur | The tab that crawls a site; also the job and its result. |
| Info | Info | The tab with the service's name and version and the key's role. |
| Operations | Funktionen | The list of tabs. |
| Site rules | Website-Regeln | The choice between respecting a site's rules and maximum extraction. |
| Respect the site's rules (default) | Regeln der Website beachten (Standard) | The default: robots.txt and the TDM opt-out are checked. |
| Maximum extraction (contract partners) | Maximale Extraktion (Vertragspartner) | For sites that allowed extraction by contract: `compliance: ignore`, with unblock mode. |
| Browser fallback | Browser-Fallback | The `mode` field: whether the browser renders a page. |
| when the page needs it | wenn die Seite ihn braucht | `mode: auto`. |
| never | nie | `mode: fast`. |
| always | immer | `mode: browser`. |
| Most pages | Maximale Seitenzahl | The `max_pages` field of a site structure. |
| Site tree | Website-Baum | The pages of a site structure, by host and path. |
| crawled | gecrawlt | A page of the tree that the crawl fetched. |
| linked only | nur verlinkt | A URL of the tree that a page links but the crawl did not fetch. |
| Principal | Konto | The key's name and role in a result. |
| What happened | Ablauf | The timeline of a result: claims, checks, fetch, render, conversion and outcome. |
| Claim | Zuteilung | A worker taking a job item; a second claim is a retry. |
| Compliance | Regelprüfung | The step that checks robots.txt and the TDM opt-out. |
| escalated from fast | Browser-Fallback nach HTTP | The browser rendered the page after the HTTP fetch. |
| unblock mode | Entsperrmodus | The stealth browser of maximum extraction. |
| queued | wartet | A job or an item that waits for a worker. |
| running | läuft | A job or an item in progress. |
| completed | abgeschlossen | A job whose items are all done. |
| Metadata | Metadaten | Title, authors, licence, dates and images of a page. |
| Metadata, structured | Metadaten, strukturiert | The schema.org and LRMI fields of a page. |
| API key | API-Schlüssel | The key that every request sends. |
| Help (user guide) | Hilfe (Anleitung) | The link from a tab to this guide. |
| Details from the API (English) | Details der API (Englisch) | The API's own `error.message`, below the translated error. |

## Service

Terms of the API and of this guide.

| English | German | Meaning |
| --- | --- | --- |
| site | Website | All pages of one host. The German texts do not use "Site-Crawl", nor "Seite" for a whole site. |
| page | Seite | One web page (in German also "Webseite"). |
| crawl | Crawl | Following a site's links to map it. |
| job | Job | Work that runs in the background, with an id to poll. |
| item | Eintrag | One URL or file of a job, with its own result. |
| worker | Worker | A process that takes job items from the queue. |
| upload | Upload | A file sent to `POST /v1/extract/file`. |
| playground | Spielwiese | The web interface at `/ui`. |
| result envelope | Ergebnis-Envelope | The shape of every answer: `status`, `data`, `error`. |
| maximum extraction | maximale Extraktion | `compliance: ignore` with unblock mode, for contract partners. |
| unblock browser | Entsperr-Browser | The stealth Chrome of unblock mode. |
| TDM opt-out | TDM-Nutzungsvorbehalt | A site's reservation against text and data mining. |
| entity | Treffer | A piece of personal data that `pii` found (not "Funde"). |
| chunk | Chunk | One piece of split content. |
| rate limit | Ratenlimit | The most requests per minute of one key. |
| user guide | Anleitung | This guide (in German not "Benutzerhandbuch"). |
