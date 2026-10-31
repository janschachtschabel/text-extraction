# text-extraction

English: [README.md](README.md)

Ein FastAPI-Dienst, der Webseiten, Dateien, Audio und Video in sauberen Text oder Markdown
umwandelt, mit ihren Metadaten. Er liest eine Seite zuerst über HTTP und rendert sie in einem
Browser, wenn die Seite das braucht. Außerdem wandelt er Dokumente um, transkribiert Audio und
Video, behandelt personenbezogene Daten, füllt strukturierte Felder, zerlegt Text in Chunks und
erfasst ganze Websites. Er hat eine REST-API, einen MCP-Server und eine Spielwiese auf Deutsch und
Englisch.

## Status

Im Aufbau: Die Pakete 0 bis 11 sind gemergt (Grundlage, Dienstgerüst, schnelle Extraktion, Jobs
und Batch, Browser-Worker, Dokumente und OCR, Audio und Video, PII, strukturierte Ausgabe und
Chunking, Website-Struktur, MCP und Oberfläche, Übergabe); Paket 12 (Sprachen der Oberfläche,
Entsperroption, zweisprachige Dokumentation) ist in Arbeit. Die Pakete und ihren Stand zeigt
[docs/packages.md](docs/packages.md#status) (Englisch).

## Zwei Arten der Extraktion

- **Regeln der Website beachten** (der Standard): robots.txt und der TDM-Nutzungsvorbehalt der EU
  werden geprüft, jeder Host bekommt nur wenige Anfragen gleichzeitig, und der Dienst sendet
  seinen eigenen, ehrlichen User-Agent.
- **Maximale Extraktion** für Websites, die die Extraktion vertraglich erlaubt haben: Ein Admin
  sendet `"compliance": "ignore"`, und wo der Betreiber `BROWSER_UNBLOCK_ENABLED` eingeschaltet
  hat, kommt ein getarnter Browser am Bot-Schutz vorbei, auch nach einem einfachen 403, 429 oder
  503. Er löst keine Bild-Captchas, nutzt keinen bezahlten Dienst und wechselt keine IP-Adressen;
  die Grenzen pro Host gelten weiter.

In beiden Fällen entscheidet der Browser-Fallback (`mode`), ob eine Seite im Browser gerendert
wird: wenn die Seite ihn braucht (`auto`, der Standard), nie (`fast`) oder immer (`browser`).
Siehe [Browser-Fallback](docs/user-guide/de/usage.md#browser-fallback).

## Schnellstart mit Docker

Voraussetzung ist Docker mit Compose. Compose startet die API, einen Extract-Worker, einen
Browser-Worker und ein Valkey (mit Passwort) für den gemeinsamen Zustand.

```bash
cp .env.example .env                           # then set API_KEYS and VALKEY_PASSWORD in it
KEY=$(openssl rand -hex 32)                    # your API key: keep it
printf %s "$KEY" | sha256sum | cut -d' ' -f1   # its sha256: replace the zeros in API_KEYS with it
openssl rand -hex 24                           # a VALKEY_PASSWORD
docker compose up --build                      # public API on http://localhost:8080
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

`.env.example` hat einen Admin-Schlüssel namens `local`, dessen SHA-256 Sie ersetzen; siehe
[API-Schlüssel](docs/user-guide/de/usage.md#api-schlüssel). Die Spielwiese liegt unter
http://localhost:8080/ui und der MCP-Endpunkt unter http://localhost:8080/mcp. [Installation](docs/user-guide/de/installation.md) beschreibt die
lokale Einrichtung mit uv, die optionalen Worker (OCR, personenbezogene Daten, LLM,
Spracherkennung), Kubernetes und die wichtigsten Einstellungen.

## Anwendungsfälle

- Eine Seite als Text oder Markdown, mit ihren Metadaten:
  [Nutzung](docs/user-guide/de/usage.md#das-ergebnis-envelope).
- Dokumente, Audio und Video, personenbezogene Daten und Bildbeschreibungen:
  [Dateien, Medien und personenbezogene Daten](docs/user-guide/de/usage.md#dateien-medien-und-personenbezogene-daten).
- Viele URLs auf einmal: [Batch](docs/user-guide/de/usage.md#batch).
- Ein Teil einer Website oder eine ganze Website: [Website-Struktur](#website-struktur).
- Strukturierte Daten und Chunks: [Strukturierte Ausgabe und Chunking](#strukturierte-ausgabe-und-chunking).
- Ein KI-Assistent mit den 11 Tools des MCP-Servers: [MCP](#mcp).
- Ausprobieren im Browser: [die Spielwiese](docs/user-guide/de/usage.md#die-spielwiese).

Jedes Feld jeder Anfrage steht unter [Parameter](docs/user-guide/de/parameters.md).

## Strukturierte Ausgabe und Chunking

`structured` nimmt ein JSON-Schema und füllt seine Felder aus der Seite: `metadata` (JSON-LD,
Microdata, RDFa, OpenGraph und Meta-Tags), `selectors` (Ihre CSS-Regeln; XPath mit einem
Admin-Schlüssel), `patterns` (Ihre regulären Ausdrücke auf dem Inhalt), `llm` oder `auto` (der
Standard): pro Feld der erste Ihrer Selektoren, der Metadaten der Seite und Ihrer Muster, der einen
Wert findet, und das LLM nur für die Felder, die dann noch fehlen. `POST /v1/structured` ist
`POST /v1/extract` mit Pflichtfeld `structured`:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/mathe/brueche", "structured": {
        "schema": {"type": "object", "properties": {"name": {"type": "string"},
          "bearbeitungszeit": {"type": "integer"}, "merksatz": {"type": "string"}}},
        "patterns": {"bearbeitungszeit": {"regex": "Bearbeitungszeit: (\\d+)", "group": 1}}}}' \
  http://localhost:8080/v1/structured
# {"status": "success", "data": {"content": "# Brüche addieren ...", "structured": {
#  "values": {"name": "Brüche addieren und subtrahieren", "bearbeitungszeit": 45,
#   "merksatz": "Brüche mit gleichem Nenner ..."},
#  "sources": {"name": "metadata", "bearbeitungszeit": "patterns", "merksatz": "llm"},
#  "missing": [], "tables": null, "learned": null,
#  "llm": {"model": "gpt-6-luna", "calls": 1, ...}, "stage": "done", ...}, ...}, ...}
```

`"tables": true` liefert auch die Tabellen der Seite; `"learn_selectors": true` (mit `llm` oder
`auto`) bittet das LLM um CSS-Selektoren, die die gefundenen Werte wiederfinden, und speichert sie
für Ihren API-Schlüssel und die Website, sodass die nächsten Seiten dieser Website für diese Felder
kein LLM brauchen (`"sources": {...: "learned"}`). Ihr eigener Header `X-LLM-Api-Key` hat Vorrang
vor dem `LLM_API_KEY` des Betreibers. Eine Anfrage, die das LLM braucht, wartet
`LLM_SYNC_WAIT_SECONDS` (30 s) und antwortet dann mit 202 und einem Job zum Abfragen; kann der
LLM-Schritt nicht laufen, behält die Antwort ihren Inhalt und ist `partial` mit `error.code:
llm_failed` (wiederholbar).

`chunking` zerlegt den Inhalt in Chunks von höchstens `size` Zeichen (Standard 1.000, `overlap`
100): `fixed`, `recursive` (der Standard), `markdown` (an Überschriften; jeder Chunk trägt seinen
Überschriftenpfad), `sentence` oder `semantic` (geschnitten, wo sich die Bedeutung ändert, mit den
Embeddings des Endpunkts). `POST /v1/chunk` nimmt eine `url` oder Ihren eigenen `text` (Markdown
oder `"text_type": "plain"`):

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.\n\n## Addieren\n\nGleiche Nenner bleiben.",
       "chunking": {"strategy": "markdown", "size": 500, "overlap": 0}}' \
  http://localhost:8080/v1/chunk
# {"status": "success", "data": {"mode_used": "upload", "chunks": [
#  {"index": 0, "text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.", "start": 0, "end": 44,
#   "headings": ["Brüche"]},
#  {"index": 1, "text": "## Addieren\n\nGleiche Nenner bleiben.", "start": 46, "end": 82,
#   "headings": ["Brüche", "Addieren"]}], ...}, ...}
```

`POST /v1/extract`, `/v1/extract/file` (als Formularfelder mit je einem JSON-Objekt) und
`/v1/batch` nehmen `structured` und `chunking` ebenfalls; jedes Ergebnis eines Batches hat seine
eigenen Chunks:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/a", "https://example.org/b"],
       "chunking": {"strategy": "sentence", "size": 800}}' \
  http://localhost:8080/v1/batch
```

## Website-Struktur

`POST /v1/site-structure` erfasst eine Website: ihre Sitemaps (die `Sitemap:`-Zeilen der
robots.txt, sonst `/sitemap.xml`; Indizes, gzip, Text, RSS und Atom) mit ihren URLs und ihren
Linkgraphen ab der Startseite, `depth` Klicks tief (Standard 2), für höchstens `max_pages` Seiten
(Standard 100). Die Antwort ist 202 mit einem Job der Art `site`; die Extract-Worker crawlen, und
jede Seite, robots.txt und Sitemap-Datei, die sie finden, wird ein Eintrag des Jobs, sodass sein
`total` wächst, solange er läuft:

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/", "depth": 2, "max_pages": 100}' \
  http://localhost:8080/v1/site-structure
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "site", "state": "queued",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=500"
# {"status": "success", "data": {"kind": "site", "state": "completed", "progress": {"total": 17,
#  ...}, "items": [
#  {"index": 0, "url": "https://example.org/", "result": {"status": "success", "data": {
#   "title": "Start", "content": null, "site": {"role": "page", "depth": 0, "parent": null,
#   "links": [
#    {"url": "https://example.org/mathe/", "text": "Mathe", "kind": "internal", "source": "a",
#     "rel": [], "hreflang": null, "region": "nav", "crawl": "queued", "target": 2},
#    {"url": "https://example.org/intern/", "text": "Intern", "kind": "internal", "source": "a",
#     "rel": [], "hreflang": null, "region": "main", "crawl": "blocked", "target": null}, ...]}}}},
#  {"index": 1, "url": "https://example.org/robots.txt", "result": {"data": {"site": {
#   "role": "robots", "depth": 0, "parent": 0,
#   "sitemaps": ["https://example.org/sitemap.xml"]}, ...}, ...}},
#  {"index": 2, "url": "https://example.org/mathe/", "result": {"data": {"site": {
#   "role": "page", "depth": 1, "parent": 0, "links": [...]}, ...}, ...}},
#  {"index": 3, "url": "https://example.org/sitemap.xml", "result": {"data": {"site": {
#   "role": "sitemap", "depth": 0, "parent": 1, "sitemap": {"format": "urlset", "guessed": false,
#   "entries": [{"loc": "https://example.org/mathe/", "lastmod": "2026-10-01"}, ...],
#   "children": [], "entry_count": 2400, "part": 1, "parts": 3}}, ...}, ...}}, ...]}, ...}
```

Lesen Sie den Graphen aus den Einträgen: `data.site.role` jedes Eintrags ist `page`, `robots`,
`sitemap` oder `sitemap_part`, und `parent` ist der Index des Eintrags, der ihn gefunden hat. Die
`links` einer Seite sind ihre ausgehenden Links in Dokumentreihenfolge: `kind` (`internal`,
`external`, `file`), `source` (`a`, `area`, ein `link` im Kopf wie `canonical` oder `alternate`,
`iframe`), `rel`, `hreflang`, `region` (`nav`, `header`, `footer`, `aside`, `main`, `breadcrumb`,
`pagination` oder null) und `crawl`, was der Crawl mit ihm gemacht hat. `queued` und `seen` führen
zu einem Seiteneintrag, dessen Index `target` ist: eine Kante des Graphen (ein Link tiefer als
`depth` auf eine Seite, die ohnehin ein Eintrag ist, sagt ebenfalls `seen`). Den anderen wird
nicht gefolgt: `depth` (tiefer als gewünscht), `listed` (Links im Kopf und Iframes), `file`,
`out_of_scope` (ein anderer Host oder außerhalb des Startverzeichnisses mit `scope: "path"`),
`nofollow`, `blocked` (robots.txt oder ein TDM-Nutzungsvorbehalt unter `compliance: enforce`),
`max_pages` und `memory_guard`. Die Einträge einer Sitemap stehen in `sitemap.entries`; eine große
setzt sich in `sitemap_part`-Einträgen mit derselben URL fort (`part` von `parts`), und ein Index
listet seine Sitemaps in `children`, jede mit dem Eintrag, der aus ihr wurde. Ein Eintrag, dessen
`final_url` die URL eines anderen Eintrags ist, ist eine Weiterleitung auf diese Seite (seine Links
wiederholen die der Seite): Entfernen Sie die Duplikate des Graphen anhand von `final_url`.

Seiten werden nur im schnellen Modus geholt, mit dem User-Agent des Dienstes, den Grenzen pro Host
und den Prüfungen von `compliance`; die Einträge behalten Titel, Status, Metadaten und Qualität,
aber keinen Text (`content: null`): Senden Sie die URLs, die Sie als Text möchten, an
`POST /v1/batch`. Der Crawl bleibt auf dem Host der Startseite (ein führendes `www.` zählt nicht);
`scope: "path"` hält ihn unter dem Verzeichnis der Start-URL, `sitemaps: false` lässt die Sitemaps
aus, und `depth: 0` liest nur die Startseite und die Sitemaps. Ein Crawl zählt zusammen mit Ihren
Batches gegen `BATCH_MAX_OPEN_PER_CLIENT`.

## MCP

Mit `MCP_ENABLED=true` (Helm `mcp.enabled=true`; Docker Compose schaltet es für die API ein) ist
der Dienst ein MCP-Server unter `https://<host>/mcp` (Streamable HTTP, zustandslos,
JSON-Antworten). Ein MCP-Client meldet sich mit einem API-Schlüssel des Dienstes an, genau wie ein
REST-Client, und jeder Tool-Aufruf durchläuft die Prüfungen der REST-Route und zählt gegen das
Ratenlimit des Schlüssels.

Claude Code:

```bash
claude mcp add --transport http text-extraction https://<host>/mcp \
  --header "Authorization: Bearer <key>"
# local compose: http://localhost:8080/mcp
```

Claude Desktop erreicht einen Server, der einen Header braucht, über die Brücke `mcp-remote`
(braucht Node.js). In `claude_desktop_config.json` (Einstellungen, Entwickler, Konfiguration
bearbeiten):

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

Der Schlüssel steht in `env`, damit er nicht Teil der Argumente ist
(`Authorization:${AUTH_HEADER}` hat kein Leerzeichen, an dem manche Clients trennen).
`mcp-remote` nimmt einfaches HTTP nur für `localhost` und `127.0.0.1` (wie
`http://localhost:8080/mcp` von Compose); für einen `http://`-Endpunkt auf einem anderen Host
(einem anderen Rechner eines vertrauenswürdigen Netzes) ergänzen Sie `"--allow-http"` in den
Argumenten. Starten Sie Claude Desktop nach der Änderung neu. Unter `AUTH_MODE=none` lassen Sie den
Header weg. Ein eigener LLM- oder ASR-Schlüssel geht auf dieselbe Weise, als Header
`X-LLM-Api-Key` und `X-ASR-Api-Key` (je ein weiteres `--header`); er ist nie ein Argument eines
Tools. Browserbasierte Clients müssen in `MCP_ALLOWED_ORIGINS` stehen; eine Anfrage mit einem
anderen `Origin` wird abgelehnt.

| Tool | Route | Argumente (neben `options`) |
| --- | --- | --- |
| `extract_url` | `POST /v1/extract` | `url`, `format` (`markdown`), `mode` (`auto`), `wait_seconds`, `content_offset`, `max_chars` |
| `extract_file` | `POST /v1/extract/file` | `content_base64` (ein Präfix als `data:`-URL ist erlaubt), `file_name`, `content_type`, `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `screenshot_url` | `POST /v1/screenshot` | `url`, `full_page` (true), `wait_seconds`; das PNG hängt bis `MCP_IMAGE_MAX_BYTES` an |
| `extract_structured` | `POST /v1/structured` | `url`, `structured` (Pflicht), `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `chunk` | `POST /v1/chunk` | `url` oder `text`, `chunking` (Pflicht), `text_type`, `wait_seconds`, `content_offset`, `max_chars` |
| `batch_extract` | `POST /v1/batch` | `urls`, `format`, `mode`; antwortet mit einem Job |
| `site_structure` | `POST /v1/site-structure` | `url`, `depth` (2), `max_pages` (100), `scope` (`host`); antwortet mit einem Job |
| `get_job` | `GET /v1/jobs/{id}` | `job_id`, `offset`, `limit` (5, 1-50), `wait_seconds` (0, bis `MCP_WAIT_SECONDS`), `content_offset`, `max_chars`; keine `options` |
| `get_job_screenshot` | `GET /v1/jobs/{id}/artifacts/screenshot.png` | `job_id`; keine `options` |
| `cancel_job` | `DELETE /v1/jobs/{id}` | `job_id`; keine `options` |
| `service_info` | `GET /v1/info` | keine |

`options` nimmt jedes weitere Feld des REST-Bodys der Route (siehe `/docs`), zum Beispiel
`{"pii": {"mode": "mask"}}` oder `{"metadata": false}`; ein Schlüssel, der sowohl als Argument als
auch in `options` steht, wird abgelehnt. Der strukturierte Inhalt einer Tool-Antwort ist das
REST-Envelope, und ihr Text ist dasselbe JSON, nach einem einzeiligen Hinweis, wenn etwas zu tun
ist:

- Ein Aufruf, der länger als `MCP_WAIT_SECONDS` (25 s) dauert, antwortet mit einem Job: Rufen Sie
  `get_job` mit seiner `job_id` und `wait_seconds` auf, bis er `completed` ist. Nur mit
  `JOBS_ENABLED=true` (dem Standard): Ohne Jobs läuft ein langsamer Aufruf synchron in den Grenzen
  der Route, und `batch_extract`, `site_structure` und die Job-Tools antworten mit 503. Die
  Formulare der Spielwiese verhalten sich genauso.
- Ein langer `content` wird auf `max_chars` gekürzt (`MCP_MAX_CONTENT_CHARS`, 50.000; geteilt von
  den Einträgen einer `get_job`-Seite), mit der Warnung `mcp_content_truncated`; rufen Sie erneut
  mit dem `content_offset` auf, den der Hinweis nennt.
- Eine Antwort, die REST mit 4xx oder 5xx gibt (ein falsches Argument, eine abgelehnte Rolle, das
  Ratenlimit), ist ein Tool-Fehler mit dem Fehler-Envelope von REST; eine Seite, die fehlschlug
  (`status: failed`, in REST ein 200), ist eine normale Antwort.

## Dokumentation

- [Anleitung](docs/user-guide/de/index.md): Installation, Nutzung, Parameter und Glossar
  (auch auf [Englisch](docs/user-guide/en/index.md))
- [Architektur](docs/architecture.md) (Englisch)
- [Konfiguration](docs/configuration.md) (Englisch): Umgebungsvariablen, Secrets, Ports, Header
- [Helm-Chart](deploy/helm/text-extraction/README.md) (Englisch)
- [Übergabe](docs/handover.md) (Englisch): die Übergabe-Checkliste des Betriebsleitfadens, Kosten,
  Wartung und Releases
- [CLAUDE.md](CLAUDE.md) (Englisch): Regeln und Arbeitsweise für Mitwirkende

## Lizenz

Apache-2.0. Siehe [LICENSE](LICENSE).
