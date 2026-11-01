# Nutzung

English: [Usage](../en/usage.md)

Diese Seite erklärt, was alle Anfragen gemeinsam haben: den API-Schlüssel, die Antwort (das
Ergebnis-Envelope), die Wahl zwischen dem Warten auf die Antwort und einem Job und den
Browser-Fallback. Danach geht es um Dateien, Audio und Video, personenbezogene Daten,
Bildbeschreibungen und die Spielwiese. [Parameter](parameters.md) listet jedes Feld jedes
Anfrage-Bodys. Die Beispiele verwenden `http://localhost:8080` (Docker Compose) und einen
Schlüssel in der Shell-Variable `KEY`.

## API-Schlüssel

Jede Anfrage sendet einen API-Schlüssel des Dienstes als `Authorization: Bearer <key>`. Der
Betreiber listet die Schlüssel in `API_KEYS` (oder `API_KEYS_FILE`), jeden mit einem Namen, einer
Rolle und dem SHA-256 des Schlüssels, nie dem Schlüssel selbst:

```bash
KEY=$(openssl rand -hex 32)                       # the key you send
printf %s "$KEY" | sha256sum | cut -d' ' -f1      # the sha256 for API_KEYS
```

Ein Schlüssel hat die Rolle `user` oder `admin`. Admins dürfen außerdem `compliance: ignore` wählen
(und damit die maximale Extraktion), XPath-Selektoren und die Aktion `evaluate` verwenden und jeden
Job lesen; ein Nutzer liest nur seine eigenen Jobs. Bei einem fehlenden oder falschen Schlüssel
antwortet der Dienst mit 401 `unauthorized`, bei einer Anfrage, die die Rolle nicht stellen darf,
mit 403 `forbidden`. Ausnahme ist ein XPath-Selektor mit einem Nutzerschlüssel: Darauf antwortet
der Dienst wie bei jedem anderen ungültigen Feld mit 422 `invalid_request`. Ein Body, der nicht als
JSON gesendet wird (`Content-Type: application/json`), ergibt 415 `invalid_request`. Jeder
Schlüssel hat ein Ratenlimit (`RATE_LIMIT_PER_MINUTE`); darüber antwortet eine Anfrage mit 429
`rate_limited` und `Retry-After`.

Mit `AUTH_MODE=none` braucht der Dienst keinen Schlüssel, und jeder Aufrufer ist Admin. Verwenden
Sie das nur auf Ihrem eigenen Rechner.

Ihre eigenen Schlüssel für einen LLM- oder Spracherkennungs-Endpunkt gehören in die Header
`X-LLM-Api-Key` und `X-ASR-Api-Key`. Diese Header haben Vorrang vor `LLM_API_KEY` und
`ASR_EXTERNAL_API_KEY` des Betreibers und werden nie mit einem Ergebnis gespeichert.

## Das Ergebnis-Envelope

Jede Antwort hat dieselbe Form, das Ergebnis-Envelope:

```json
{
  "status": "success",
  "data": {
    "url": "https://example.org/",
    "final_url": "https://example.org/",
    "upstream_status": 200,
    "mode_used": "fast",
    "title": "Example Domain",
    "content": "# Example Domain\n\nThis domain is for use in illustrative examples ...",
    "timings": {"total_ms": 412, "compliance_ms": 35, "fetch_ms": 210, "convert_ms": 18, "render_ms": 0},
    "warnings": []
  },
  "error": null,
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

- `status` ist `success`, `partial` (der Inhalt ist da, aber etwas fehlt, etwa
  `content_too_short`) oder `failed`.
- `error` erklärt eine Antwort `partial` oder `failed`: `error.code` ist ein fester Code wie
  `target_http_error` oder `blocked_by_robots`, `error.message` ein englischer Text, und
  `error.retryable` sagt, ob dieselbe Anfrage später gelingen kann.
- `data.upstream_status` ist der HTTP-Status der Seite, `data.mode_used` ist `fast`, `browser` oder
  `upload`, und `data.timings` gibt die Zeit jedes Schritts in Millisekunden an.
- `data.warnings` nennt, was nicht wie gewünscht lief, obwohl ein Ergebnis vorliegt, etwa
  `browser_unavailable`.

Wenn das Ziel scheitert (die Website antwortet mit 404, lehnt den Dienst ab oder ist nicht
erreichbar), antwortet die API trotzdem mit HTTP 200, mit `status: failed`. Ein 4xx ist ein Problem
Ihrer Anfrage (ein falsches Feld, der Schlüssel, das Ratenlimit); ein 5xx ist ein Fehler des
Dienstes.

## Synchron und Jobs

Standardmäßig antwortet eine Anfrage, wenn ihre Arbeit fertig ist. Arbeit, die länger dauert, wird
zu einem Job: Senden Sie `wait_seconds`, und läuft die Arbeit nach so vielen Sekunden noch, ist die
Antwort 202 mit der ID des Jobs und `status_url`.

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/", "wait_seconds": 10}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/", "wait_seconds": 10}' http://localhost:8080/v1/extract
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "extract", "state": "running",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/jobs/<job id>
```

Manche Arbeit ist immer ein Job: ein Batch, eine Website-Struktur, eine Transkription und eine
Anfrage, die länger als `PII_SYNC_WAIT_SECONDS` oder `LLM_SYNC_WAIT_SECONDS` auf den NLP- oder
LLM-Worker wartet. Ein Upload wartet standardmäßig 25 s. Der `state` eines Jobs geht von `queued`
über `running` zu `completed`, und `progress` zählt seine Einträge nach Status. Ein Job wird
`JOB_TTL_SECONDS` (24 h) aufbewahrt, und nur sein Eigentümer (oder ein Admin) kann ihn lesen. Mit
`JOBS_ENABLED=false` gibt es keine Jobs: `wait_seconds` wird ignoriert, und Batches und
Website-Strukturen antworten mit 503.

### Batch

`POST /v1/batch` reiht viele URLs als einen Job ein (höchstens `BATCH_MAX_URLS`, standardmäßig
1.000) und antwortet mit 202 und seiner ID. Die Extract-Worker bearbeiten die Einträge fair
zwischen API-Schlüsseln und zwischen Hosts, in den Grenzen pro Host. Lesen Sie die Ergebnisse Seite
für Seite (`offset`, `limit` bis 500):

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/", "https://example.com/"], "format": "markdown"}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/", "https://example.com/"], "format": "markdown"}' \
  http://localhost:8080/v1/batch
# {"status": "success", "data": {"id": "<job id>", "kind": "batch", "state": "queued", "total": 2,
#  "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=100"
```

Jede URL bekommt ihr eigenes Ergebnis-Envelope in `items`, auch eine fehlgeschlagene. Jeder Eintrag
zeigt außerdem `run`, seine letzte Zuteilung an einen Worker (`pool`, `attempts`, `claimed_at`;
null, solange er wartet): `attempts` über 1 heißt, dass der Eintrag nach einem verlorenen Lease oder
einer Zurückstellung erneut läuft.

### Einen Job abbrechen

`DELETE /v1/jobs/{id}` bricht einen Job ab (sein Eigentümer oder ein Admin). Es löscht den Job mit
seinen Ergebnissen und Dateien sofort und gibt seinen Platz in `BATCH_MAX_OPEN_PER_CLIENT` frei.
Einträge, die noch warten, entfallen; laufende Einträge werden fertig, aber nicht gespeichert.

## Browser-Fallback

Manche Seiten brauchen JavaScript. Mit eingeschaltetem Browsermodus (`BROWSER_ENABLED=true`; Docker
Compose schaltet ihn ein) rendert der Browser-Worker sie in Chromium. `mode` wählt den
Browser-Fallback:

| `mode` | In der Spielwiese | Was passiert |
| --- | --- | --- |
| `auto` | wenn die Seite ihn braucht | Erst HTTP; der Browser, wenn die Seite ihn braucht (eine JavaScript-Hülle, eine Bot-Prüfung, ein Soft-404). Der Standard. |
| `fast` | nie | Nur HTTP. |
| `browser` | immer | Immer der Browser. |

`data.mode_used` sagt, welcher Modus die Antwort erzeugt hat. Übernimmt kein Browser-Worker die
Seite innerhalb von `BROWSER_QUEUE_WAIT_SECONDS` (10 s), behält `auto` das HTTP-Ergebnis mit der
Warnung `browser_unavailable`, und `browser` schlägt mit `timeout` fehl (wiederholbar). Solange der
Browsermodus aus ist, antworten `browser`, `POST /v1/screenshot` und `auto` mit `actions`,
`cookies`, `headers` oder `screenshot` mit 400, und `auto` ohne sie nutzt den Browser nie.

`actions` (höchstens 20), `cookies` und `headers` (nur an die angefragte Website gesendet)
funktionieren mit `auto` und `browser`, nicht mit `fast` und nicht in Batches; die OpenAPI-Spec
unter `/docs` listet die Aktionen.

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/app", "mode": "browser", "actions": [{"type": "accept_cookies"}, {"type": "click", "selector": "button.more"}]}
```

### Screenshots

Ein Screenshot ist ein PNG, 1920 px breit, der ganzen Seite (`full_page: false`: die ersten
1920×1080). `POST /v1/screenshot` antwortet mit den Daten der Seite und einem Verweis auf die
Datei, die Sie aus dem Job laden, der sie hält:

<!-- request: POST /v1/screenshot -->
```json
{"url": "https://example.org/", "full_page": true}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/screenshot
# {"status": "success", "data": {"screenshot": {"url": "/v1/jobs/<job id>/artifacts/screenshot.png",
#  "content_type": "image/png", "width": 1920, "height": 2758, ...}, ...}, ...}
curl -H "Authorization: Bearer $KEY" -o page.png \
  http://localhost:8080/v1/jobs/<job id>/artifacts/screenshot.png
```

`POST /v1/extract` nimmt `"screenshot": true` ebenfalls. Die Datei wird `ARTIFACT_TTL_SECONDS`
(eine Stunde) aufbewahrt, und nur ihr Eigentümer (oder ein Admin) kann sie abrufen.

### Maximale Extraktion

Standardmäßig beachtet der Dienst die Regeln einer Website: robots.txt und der TDM-Nutzungsvorbehalt
der EU werden so geprüft, wie der Betreiber es eingerichtet hat (`COMPLIANCE_DEFAULT`). Die maximale
Extraktion ist für Websites, die die Extraktion vertraglich erlaubt haben. Ein Admin sendet
`"compliance": "ignore"`, und wo der Betreiber `BROWSER_UNBLOCK_ENABLED` eingeschaltet hat, wird die
Seite in einem getarnten Chrome geladen, der wartet, bis Bot-Prüfungen durchlaufen sind. Mit
`mode: auto` fällt die maximale Extraktion außerdem auf den Browser zurück, wenn die Seite ablehnt
(HTTP 403, 429 oder 503) oder zu wenig Text liefert, und `data.unblocked` ist true. Die maximale
Extraktion löst keine Bild-Captchas, nutzt keinen bezahlten Dienst und wechselt keine IP-Adressen;
die Grenzen pro Host und die Pause eines Hosts nach einem 429 oder 503 gelten weiter. Wo es geht,
bitten Sie den Inhaber der Website besser um eine Freigaberegel: einen geheimen Header, den Sie mit
`headers` senden, oder eine Ausnahme für die ausgehende IP des Dienstes. Die Einzelheiten stehen
unter [Website-Regeln](site-rules.md).

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/partner-page", "compliance": "ignore"}
```

## Dateien, Medien und personenbezogene Daten

### Dokumente und Uploads

PDFs, Word-, PowerPoint- und Excel-Dateien (DOCX, PPTX, XLSX, XLS), EPUBs und CSVs werden wie
Seiten umgewandelt: `POST /v1/extract` mit ihrer URL oder `POST /v1/batch`. Die Bytes entscheiden
vor dem `Content-Type`, was eine Datei ist (ein PDF, das als `application/octet-stream` ausgeliefert
wird, bleibt ein PDF). Um eine Datei umzuwandeln, die Sie haben, laden Sie sie als
`multipart/form-data` an `POST /v1/extract/file` hoch (höchstens `DOCUMENT_MAX_BYTES`, 20 MB):

```bash
curl -H "Authorization: Bearer $KEY" -F file=@report.pdf -F format=markdown \
  http://localhost:8080/v1/extract/file
# {"status": "success", "data": {"mode_used": "upload", "url": null, "content": "# ...",
#  "document": {"kind": "pdf", "converter": "markitdown", "pages": 12, "pages_converted": 12,
#  "ocr": false, "file_name": "report.pdf"}, ...}, ...}
```

Das Formular nimmt außerdem `metadata`, `ocr` (`auto`, `never` oder `always`) und `wait_seconds`
(standardmäßig 25: Ein Upload, der länger dauert, antwortet mit 202 und einem Job). PDFs behalten
ihre ersten `DOCUMENT_MAX_PAGES` (100) Seiten, mit der Warnung `pages_truncated`. Mit dem
Docling-Worker werden Scans und Bilder (PNG, JPEG, TIFF, WebP, BMP) mit OCR gelesen und
OpenDocument-Dateien (ODT, ODS, ODP) ebenfalls umgewandelt; ohne ihn liefert ein Scan nur seine
Textebene, mit der Warnung `ocr_unavailable`, und Bilder und OpenDocument-Dateien ergeben
`unsupported_content_type`.

### Audio und Video

Für eine Video-URL von YouTube, Vimeo oder den Mediatheken von ARD, ZDF, arte und 3sat enthält die
Antwort die Untertitel des Videos (zuerst manuelle, dann die automatischen der Plattform), seinen
Titel, seine Beschreibung, Dauer und Lizenz. Die Seite selbst wird nicht geholt:

<!-- request: POST /v1/extract -->
```json
{"url": "https://www.youtube.com/watch?v=<id>", "languages": ["de"]}
```

Für ein Video ohne Untertitel enthält die Antwort nur seine Angaben
(`"transcript_source": "metadata_only"`, `status: partial`, `error.code: no_transcript_available`);
ebenso ein Video, dessen Plattform den Dienst abgelehnt hat (Warnung `subtitles_blocked`: YouTube
sperrt viele IPs von Rechenzentren). Ist der Browser-Modus an, bekommt ein YouTube-Video, dessen
Untertitel der Dienst nicht lesen konnte (Warnung `subtitles_blocked` oder `subtitles_failed`),
im Modus `auto` ohne `transcribe` einen zweiten Versuch im Browser: Wo YouTube seine neue
Transkriptansicht anbietet, enthält die Antwort dieses Transkript
(`"transcript_source": "transcript_panel"`, `"mode_used": "browser"`); sonst enthält sie die
Angaben mit der Warnung `transcript_panel_unavailable`. Eine HTML-Seite, deren `<video>` oder
`<audio>` ein `<track>` hat, bekommt dessen Text nach ihrem eigenen, unter `## Transcript`, und
`data.media.embeds` listet die Player, die eine Seite einbettet, damit Sie sie einzeln extrahieren.

Mit dem ASR-Worker transkribiert `"transcribe": true` ein Video ohne Untertitel oder die URL einer
Audio- oder Videodatei. Für ein Video mit Untertiteln kommt die Antwort weiterhin sofort; eine
Transkription ist immer ein Job, die Antwort ist sofort 202. Eine hochgeladene Audio- oder
Videodatei wird immer transkribiert (die Bytes entscheiden), bis `MEDIA_UPLOAD_MAX_BYTES` (25 MB;
größere Medien per URL):

```bash
curl -H "Authorization: Bearer $KEY" -F file=@lecture.mp3 http://localhost:8080/v1/extract/file
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "file", ...,
#  "status_url": "/v1/jobs/<job id>"}, ...}
```

Eine Transkription läuft auf 4 CPU-Threads etwa in Echtzeit (Medien bis 2 Stunden). Mit
`ASR_BACKEND=openai` sendet der Worker das Audio stattdessen an einen OpenAI-kompatiblen Endpunkt
(`ASR_EXTERNAL_BASE_URL`).

### Personenbezogene Daten (PII)

`pii` findet personenbezogene Daten im Ergebnis und behandelt sie, in deutschem und englischem Text:
den Inhalt und die Freitextfelder (Titel, Autoren, Beschreibung, Linktexte, ...). `pii` ist ein
Modus oder ein Objekt mit `entities`, `language` und `score_threshold`. Standardmäßig werden
Personen sowie Kontakt- und Zahlungsdaten gefunden (`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`,
`IBAN_CODE`, `CREDIT_CARD`, `IP_ADDRESS`); `LOCATION`, `ORGANIZATION` und `URL` nur, wenn Sie sie
nennen.

| Modus | Ein Treffer wird zu |
| --- | --- |
| `mask` | `[PERSON]`, `[EMAIL_ADDRESS]`, ... |
| `redact` | `[REDACTED]` |
| `hash` | `[PERSON:3f9a1c7d02be]`: ein Hash mit Schlüssel, gleich für gleiche Werte |
| `annotate` | der Text bleibt; `data.pii.items` listet Typ, Feld, Positionen, Score und Text (nur Markdown und Text, nicht `format: html`) |

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/team", "pii": {"mode": "annotate", "entities": ["PERSON", "ORGANIZATION"]}}
```

Ein Batch und ein Upload nehmen `pii` genauso (ein Upload als Formularfeld `-F pii=redact`). Ein
Ergebnis wird nie unbearbeitet beantwortet: Kann die Behandlung nicht laufen, ist die Antwort
`failed` mit `error.code: pii_failed` (wiederholbar) und ohne den Text. `hash` verwendet den
Schlüssel `PII_HASH_KEY`: gesetzt, hat derselbe Wert in jedem Ergebnis denselben Hash; nicht
gesetzt, nur innerhalb eines Ergebnisses. `pii` lässt sich nicht mit `screenshot` kombinieren.
Namen findet ein statistisches Modell, daher werden einige übersehen und einige Wörter für Namen
gehalten.

### Bildbeschreibungen

`describe_images` bittet das LLM um eine kurze Beschreibung jedes Bildes der Seite, für einen
Suchindex und für Menschen, die es nicht sehen können. Es braucht `metadata` (standardmäßig an):
Die Beschreibungen stehen an den Einträgen von `metadata.images` (Titelbilder zuerst), höchstens
`max_images` davon (Standard 5, höchstens 20). `content` bleibt der Text der Seite. Ein
hochgeladenes PNG, JPEG oder WebP wird ebenfalls beschrieben, und seine Beschreibung wird an
`content` angehängt.

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/natur/wasserkreislauf", "describe_images": {"max_images": 3}}
```

Die Bilder werden wie Seiten geholt: mit den Prüfungen und Grenzen pro Host des Dienstes, mit dem
User-Agent des Dienstes und nie mit Ihren Cookies oder Headern. Ein Bild, das nicht geholt wird, zu
groß ist (`LLM_VISION_MAX_IMAGE_BYTES`) oder nicht beschrieben wird, behält `description: null`, mit
der Warnung `image_not_described`. `describe_images` lässt sich nicht mit `pii` kombinieren (422):
Kein unmaskiertes Bild geht an das LLM.

## Die Spielwiese

Mit `UI_ENABLED=true` (Docker Compose schaltet es ein) bietet der Dienst unter `/ui` eine Spielwiese
(lokal: http://localhost:8080/ui). Melden Sie sich mit einem API-Schlüssel des Dienstes an, optional
auch mit Ihren LLM- und ASR-Schlüsseln. Die Seite hält die Schlüssel nur im Tab des Browsers
(`sessionStorage`, gelöscht, wenn der Tab geschlossen wird; „Abmelden“ löscht sie) und sendet sie
als Header der Anfragen, nie als Cookie. Unter `AUTH_MODE=none` öffnet sich die Spielwiese sofort,
unter einem Hinweisbanner: Wer den Dienst erreichen kann, kann ihn nutzen. Für eine gemeinsam
genutzte Installation lassen Sie `UI_ENABLED=false`, oder Sie setzen `AUTH_MODE=api_key`.

Die Spielwiese spricht Deutsch und Englisch. Der Sprachwechsel („Deutsch“, „English“) steht oben auf
jeder Seite; die Wahl bleibt im Browser gespeichert. Jeder Tab beginnt mit einer kurzen Einführung
und einem Link „Hilfe (Anleitung)“ zu dieser Anleitung, der sich in einem neuen Tab öffnet, und
jedes Feld hat einen Hinweis.

Jedes Formular führt einen REST-Aufruf aus, mit denselben Prüfungen und demselben Ratenlimit wie
jeder Client, und zeigt den HTTP-Status, das Envelope, das rohe JSON und die Anfrage als
`curl`-Befehl (mit `$API_KEY` anstelle Ihres Schlüssels). Die Tabs:

- **Extrahieren**: eine URL als Markdown oder Text, mit Metadaten, strukturierten Metadaten
  (schema.org- und LRMI-Felder wie Lizenz, Autor und Bildungsstufe) und Bildbeschreibungen. Das
  Ergebnis zeigt den Inhalt, die Metadaten, die Bilder, die Links und eine Zeitleiste des Ablaufs.
- **Datei**, **Screenshot**, **Strukturiert**, **Chunks** und **Info**: die anderen einzelnen
  Aufrufe, mit derselben Ergebnisansicht; das Feld „Optionen (JSON)“ nimmt jedes weitere Feld des
  REST-Bodys.
- **Batch**: URLs, eine pro Zeile. **Batch-Datei**: eine CSV- oder JSON-Datei mit URLs, zuerst auf
  dem Server geprüft, mit einer Vorschau vor „Batch starten“.
- **Website-Struktur**: startet einen Crawl; wenn er abgeschlossen ist, listet „Als Baum
  anzeigen“ seine Seiten nach Host und Pfad (während er läuft, zeigt „Baum jetzt anzeigen“ eine
  Momentaufnahme), mit einem Kontrollkästchen pro Seite und pro Abschnitt, und „Auswahl
  extrahieren“ startet Batches der markierten URLs.

Jeder Job, der in der Spielwiese gestartet wurde, zeigt seinen Zustand, einen Fortschrittsbalken und
seine Einträge, aktualisiert sich alle 3 s, bis er abgeschlossen ist, und hat eine Schaltfläche
„Abbrechen“.

### Website-Regeln in der Spielwiese

Die Formulare für eine URL haben die Auswahl „Website-Regeln“:

- „Regeln der Website beachten (Standard)“ sendet kein `compliance`: Es gelten die Prüfungen des
  Betreibers.
- „Maximale Extraktion (Vertragspartner)“ sendet `compliance: ignore`. Diese Wahl ist für Websites,
  die die Extraktion vertraglich erlaubt haben (siehe [Website-Regeln](site-rules.md)).

Nur Admins sehen die Auswahl, und sie ist mit einem Hinweis gesperrt, solange `BROWSER_ENABLED` oder
`BROWSER_UNBLOCK_ENABLED` aus ist. Mit `AUTH_MODE=none` zählt jeder Besucher als Admin, daher sieht
jeder Besucher die Auswahl. Das Feld „Browser-Fallback“ setzt `mode`: „wenn die Seite ihn braucht“
(`auto`), „nie“ (`fast`) oder „immer“ (`browser`). Bei maximaler Extraktion bleibt „nie“ bei HTTP:
Die Seite erreicht den Browser nie.
