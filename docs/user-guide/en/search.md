# Search

Deutsch: [Suche](../de/search.md)

Search the web for pages, then extract the hits you want. The service asks its own SearXNG, a
search service that runs next to it as a separate container. SearXNG passes the query to a few
free sources and returns their hits together. The search itself fetches no
page. You send the hits you pick to an extraction: one in the playground, through
`POST /v1/extract` or with `extract_url`, several as a batch. This page covers the sources, the
playground, REST, MCP, the errors and what an operator sets up.

The search is there when the operator switched it on (`SEARCH_ENABLED`). The root
`docker-compose.yml` does that by default. In the Helm chart it is off. `GET /v1/info` says whether
your key may search (`features.search`) and how many hits one search may return
(`limits.search_max_results`).

## The sources

Every source is free. All but Bing, DuckDuckGo, Mojeek and Startpage have an official API. Each hit names the sources
that found it.

| Source | What it finds | Limits |
| --- | --- | --- |
| `wikipedia` | The Wikipedia article on the search terms, from the Wikipedia of `language` | At most one hit per search: the source asks for the article that fits the terms best, not for a list of articles. |
| `wikibooks` | Textbooks and their chapters on Wikibooks | Books on some subjects only. |
| `wikiversity` | Learning material and course pages on Wikiversity | Small, uneven coverage. |
| `wikinews` | News articles on Wikinews | Small; few topics. |
| `mwmbl` | General web pages from Mwmbl, a free, non-profit search engine with its own index | Small index, mostly English pages; German educational sites are often missing. Without a key, Mwmbl allows about 1,000 requests a month from one IP address; the cache spares that quota. |
| `openalex` | Scholarly works (articles, books, data sets) from OpenAlex | Science only; titles are often English. |
| `arxiv` | Preprints from arXiv (physics, mathematics, computer science and more) | Science only, almost all English. |
| `crossref` | Scholarly works with a DOI from Crossref | Science only; metadata, often without a summary. |
| `bing` | General web pages from Bing, in the language of `language` | No official API: SearXNG reads Bing's result pages (scraping). A search engine may block a server's address after a while; the source is then listed in `sources_failed`, and the answer is `partial`. |
| `duckduckgo` | General web pages from DuckDuckGo | Scraping as with `bing`, with the same risk of a block. |
| `mojeek` | General web pages from Mojeek, a search engine with its own index | Scraping as with `bing`, with the same risk of a block; a smaller index than Bing's. |
| `startpage` | General web pages from Startpage, which passes on Google's results | Scraping as with `bing`, with the same risk of a block. |

So German results come from Bing, DuckDuckGo, Mojeek and Startpage, Wikipedia knowledge from
the Wikimedia sources and science from OpenAlex, arXiv and Crossref. Google and Brave stay off:
Google blocked a server on its first day. An operator can switch the four scraped engines off, or
switch another engine on (see [More sources](#more-sources)).

When several sources are asked, the hits take turns: first every source's top hit, then every
source's second hit, and so on. A page that several sources found (also as `http` and `https`,
with or without `www.`, a trailing slash or `utm_` parameters) is listed once, at its best place,
with all its sources.

## In the playground

The **Search** tab appears only when your key may search. It sits before the **Info** tab. When the
search is kept for admin keys (`SEARCH_ALLOW_USERS=false`) and you are signed in with a user key,
the tab is missing, and the **Info** tab says why.

1. Enter the "Search terms" (at most 500 characters).
2. Optional: tick the "Sources" to ask. With none ticked, every source is asked.
3. Optional: "Language" is "All languages", "German" or "English". It is preset to the language
   of the page, so on the German page the search asks for German hits. "Hits" is how many hits to show (10 by default, at most the service's
   `SEARCH_MAX_RESULTS`). "Time range" keeps hits from the last day, month or year, where a source
   supports it. "Result page" 2 shows the next hits.
4. Press "Search".

The result starts with a status line and the number of hits. A notice names the sources that did
not answer, if any, and "Suggestions:" lists other search terms that SearXNG proposes. Each hit
shows its title as a link (it opens in a new tab), its URL, a snippet, "Found by" with its
sources and, where a source has one, its date ("Published").

To extract hits, tick them (or press "Select all"; "Select none" clears every tick), choose the
format and the options below the list, and press "Extract selected". The ticked URLs become a
batch, shown below the hits with its progress, as in the site tree (see
[Part of a site](part-of-a-site.md#in-the-playground)). With nothing ticked, the form says "Give
at least one URL." and starts nothing. "As curl" and "Raw JSON" at the end show the search request
and its whole answer.

## With REST

`POST /v1/search` takes the search terms and returns the hits in the result envelope. Every field
is in [Parameters](parameters.md#post-v1search).

<!-- request: POST /v1/search -->
```json
{"query": "Photosynthese", "language": "de", "max_results": 20, "sources": ["wikipedia", "wikibooks", "openalex"]}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query": "Photosynthese", "language": "de", "max_results": 20}' \
  http://localhost:8080/v1/search
# 200 {"status": "success", "data": {"query": "Photosynthese", "results": [
#   {"url": "https://de.wikipedia.org/wiki/Photosynthese", "title": "Photosynthese",
#    "snippet": "...", "sources": ["wikipedia"], "published": null, "score": 1.0}, ...],
#  "suggestions": [], "sources_failed": [], "cache": "miss"}, ...}
```

Each hit in `data.results` has its `url` (always http or https; SearXNG's other links are
dropped), its `title`, a `snippet` (or null), the `sources` that found it, `published` (a date,
where the source has one) and SearXNG's `score`. `title` and `snippet` are plain text: the
service drops the markup some sources hand on (Crossref's and OpenAlex's JATS tags, Wikipedia's
highlights). `data.suggestions` lists other search terms.

The hits are ordered by their place in each source's own list: every source's top hit comes
first, then every source's second, and so on. A page found by several sources is one hit, at its
best place, whose `sources` names all of them; the address counts as the same page without regard
to `http` or `https`, a leading `www.`, a trailing slash, the `#` part or `utm_` parameters.
`max_results` cuts the list after this ordering.

When some sources did not answer, the answer is `partial`, and `data.sources_failed` names each
with SearXNG's reason (such as a timeout or a refused connection). The hits then come from the
other sources; `data.results` may also be empty when the other sources found nothing. A source that
answered without a hit is not a failure. Only when every source asked did not answer is the search
`failed`, and `error.details` lists each source with its reason.

The service keeps a `success` answer in its cache for `SEARCH_CACHE_SECONDS` (one hour by
default). The same search terms with the same language, page, time range, sources and
`max_results` then come from the cache (`data.cache: "hit"`). A `partial` answer is not kept, so
the next search asks again. A search counts once against your key's rate limit.

To read the pages, send the URLs to an extraction, for example as a batch:

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://de.wikipedia.org/wiki/Photosynthese", "https://de.wikipedia.org/wiki/Chlorophyll"], "format": "markdown"}
```

How to read a batch is in [Part of a site](part-of-a-site.md#extract-the-pages).

## With MCP

The tool `web_search` takes the fields of `POST /v1/search` as its arguments (`query` is required)
and answers the same envelope. It has no `options`. Its description names the sources and the
most hits of the service, so an assistant does not have to guess them. Then `extract_url` reads one
hit and `batch_extract` several. An assistant chains them on its own:

```json
{"name": "web_search", "arguments": {"query": "Photosynthese", "language": "de", "max_results": 5}}
{"name": "batch_extract", "arguments": {"urls": ["https://de.wikipedia.org/wiki/Photosynthese"]}}
```

The tool is listed even while the search is off; a call then answers the 400 of the route. The
setup and the other tools are in [MCP](mcp.md).

## Errors

| Answer | Meaning |
| --- | --- |
| 200, `status: partial` | Some sources did not answer; `data.sources_failed` names them. The hits of the others are there; no hits means the others found nothing. |
| 200, `status: failed`, `search_failed`, `retryable: true` | SearXNG is not reachable or did not answer within `SEARCH_TIMEOUT_SECONDS` (10 s by default), or every source asked did not answer (`error.details` lists each with its reason). Try again later. |
| 200, `status: failed`, `search_failed`, `retryable: false` | SearXNG refused the request because its JSON format is off: a setting of the operator (`search.formats` in SearXNG's `settings.yml`). |
| 200, `status: success`, no hits | Every source answered, and none had a hit. Try other words, another language or more sources. |
| 400 `invalid_request` | The search is off on this service (`SEARCH_ENABLED=false`). |
| 403 `forbidden` | The search is kept for admin keys (`SEARCH_ALLOW_USERS=false`), and your key is a user key. |
| 422 `invalid_request` | The body is wrong: no `query`, `max_results` over `SEARCH_MAX_RESULTS`, a `language` that is not a language code (such as `German` instead of `de`), a source outside `SEARCH_SOURCES` (the message lists the allowed ones), an empty name, or `sources: []` (leave `sources` out to ask every source). |
| 429 `rate_limited` | Your key's rate limit is used up. |

## For operators

### Switch it on or off

The root `docker-compose.yml` runs the service `searxng` and switches the search on.
`SEARCH_ENABLED=false` in `.env` switches it off for the API; to save the container as well, start
the stack with `--scale searxng=0`. `docker-compose.build.yml` does the same.

In the Helm chart both are off. `searxng.enabled=true` runs SearXNG in the release and points
`SEARCH_URL` at it; `config.SEARCH_ENABLED: "true"` switches the search on. Without
`searxng.enabled`, set `config.SEARCH_URL` to a SearXNG of your own. See the
[Helm chart](../../../deploy/helm/text-extraction/README.md#web-search-searxng).

The other settings (`SEARCH_TIMEOUT_SECONDS`, `SEARCH_MAX_RESULTS`, `SEARCH_CACHE_SECONDS`,
`SEARCH_SOURCES`, `SEARCH_ALLOW_USERS`) are in the
[configuration reference](../../configuration.md#web-search) (English). Keep
`SEARCH_TIMEOUT_SECONDS` above 8: SearXNG cuts each source at 8 s, so with a lower value one slow
source fails the whole search instead of being listed in `data.sources_failed`.

SearXNG runs as an unmodified container of its own and has no published port: only the API
reaches it. Its licence is AGPL-3.0; the service never imports its code.

### Hostinger and the release order

The root `docker-compose.yml` gives SearXNG a volume of its own, `searxng-config`, so it never
sees the other secrets. The service `init` writes SearXNG's settings into that volume on every
start, with a key it creates once and keeps. This `init` comes with release 0.1.13. The compose
file follows the newest 0.1.x release, so deploy the new compose file only once 0.1.13 is
published: with an older image SearXNG gets no settings and does not start. If
`docker compose logs init` shows `init_searxng_settings_failed`, remove the volume
`searxng-config` (it holds only these settings) and deploy again. SearXNG answers from the VPS's
own address. The sources with an official API do not block a server; Mwmbl counts requests per
address. Bing, DuckDuckGo, Mojeek and Startpage may block the VPS's address after a while: they then appear in
`sources_failed`, and the other sources still answer. Such an answer is `partial` and is not
cached, so while a block lasts every search asks all sources again; leave a blocked engine out of
`SEARCH_SOURCES` if that happens for long (see [More sources](#more-sources)).

### More sources

To keep the scraped engines off, set `SEARCH_SOURCES` without them (for example
`wikipedia,wikibooks,wikiversity,wikinews,mwmbl,openalex,arxiv,crossref`): the service then
never asks them. A source outside `SEARCH_SOURCES` is refused with a 422.

To switch on another SearXNG engine (such as `google` or `brave`), at your own risk:

1. Add its name to `use_default_settings.engines.keep_only` and to the `engines:` list (with
   `disabled: false`, and `inactive: false` for an engine SearXNG does not load by default) of
   SearXNG's `settings.yml`.
2. Add its name to `SEARCH_SOURCES`, which names the engines the service may ask.

Where the settings come from depends on the setup:

- Root `docker-compose.yml`: `init` writes the settings again on every start, from the copy in the
  service's image (`src/text_extraction/data/searxng-settings.yml`), so an edit in the volume is
  lost at the next start. Run a SearXNG of your own with your settings and set `SEARCH_URL` to it
  (and leave the stack's one out with `--scale searxng=0`), or, where bind mounts are allowed (not
  on Hostinger), mount your own `settings.yml` at `/etc/searxng/settings.yml` of the service
  `searxng` in a compose override file, with a `secret_key` of your own.
- `docker-compose.build.yml`: it mounts `src/text_extraction/data/searxng-settings.yml` from the
  checkout. Edit that file and restart `searxng`.
- Helm: the ConfigMap holds the chart's `files/searxng-settings.yml`. Change it in a copy of the
  chart, or set `searxng.enabled=false` and `config.SEARCH_URL` to a SearXNG of your own.

Scraping search engines gets the server's address blocked and goes against their terms. The
project keeps Bing, DuckDuckGo, Mojeek and Startpage on by default because German web results need them (rulings D6 and D7 in
[architecture.md](../../architecture.md#web-search)); every other
scraping engine stays off.
