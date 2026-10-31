# Parameter

English: [Parameters](../en/parameters.md)

Diese Seite listet jedes Feld jedes Anfrage-Bodys, mit seinem Typ, seinem Standardwert und seiner
Wirkung. Die OpenAPI-Spec unter `/docs` eines laufenden Dienstes hat dieselben Felder mit allen
Einzelheiten und die verschachtelten Objekte vollständig. Ein Feld, das nicht gelistet ist,
antwortet mit 422; ebenso ein Wert außerhalb des Bereichs. „nicht gesetzt“ heißt, dass das Feld
fehlt (oder `null` ist); „Pflicht“ heißt, dass es keinen Standardwert hat.

## `POST /v1/extract`

Eine Seite oder Datei über ihre URL. Die Antwort beschreibt [Nutzung](usage.md).

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `url` | String | Pflicht | Die http(s)-URL, die extrahiert wird. |
| `mode` | `fast`, `auto` oder `browser` | `auto` | Der Browser-Fallback: `auto` nutzt den Browser, wenn die Seite ihn braucht, `fast` nie, `browser` immer. Siehe [Browser-Fallback](usage.md#browser-fallback). |
| `format` | `markdown`, `text` oder `html` | `markdown` | Das Format von `content`. |
| `metadata` | Boolean | `true` | Die Metadaten der Seite mitliefern (Titel, Autoren, Lizenz, Daten, Bilder, schema.org- und LRMI-Felder). |
| `links` | Boolean | `false` | Die Links der Seite mitliefern. |
| `compliance` | `enforce`, `report` oder `ignore` | nicht gesetzt | Die Seitenregeln: `enforce` lehnt ab, was robots.txt oder ein TDM-Nutzungsvorbehalt verbietet, `report` meldet es nur, `ignore` (nur Admins) lässt die Prüfungen aus und erlaubt die maximale Extraktion. Nicht gesetzt: `COMPLIANCE_DEFAULT` des Betreibers. |
| `cache` | Boolean | `true` | Den Ergebnis-Cache nutzen; `false` liest und speichert nichts. |
| `wait_seconds` | Zahl, 0 bis 60 | nicht gesetzt | Nach so vielen Sekunden wird eine laufende Extraktion ein Job (202). Nicht gesetzt: Die Anfrage wartet auf ihre Antwort. |
| `actions` | Liste von Objekten | leer | Seitenaktionen im Browser, der Reihe nach, höchstens 20 (etwa `click`, `type`, `scroll`, `accept_cookies`). Nicht mit `mode: fast`. |
| `cookies` | Liste von Objekten | leer | Cookies für die angefragte Website. Nicht mit `mode: fast`. |
| `headers` | Objekt | leer | Header für die angefragte Website. Nicht mit `mode: fast`. |
| `screenshot` | Boolean | `false` | Auch einen Screenshot als PNG aufnehmen. Nicht mit `mode: fast`. |
| `full_page` | Boolean | `true` | Der Screenshot zeigt die ganze Seite; `false`: den ersten Viewport. |
| `ocr` | `auto`, `never` oder `always` | `auto` | OCR für Dokumente: `auto` liest Scans und Bilder mit OCR, wo der Dienst sie hat. |
| `transcribe` | Boolean | `false` | Audio und Video ohne Untertitel transkribieren (immer ein Job). |
| `languages` | Liste von Sprachcodes | leer | Bevorzugte Sprachen für Untertitel und Transkription, höchstens 5, etwa `["de", "en"]`. |
| `pii` | Modus oder Objekt | nicht gesetzt | Personenbezogene Daten behandeln: `mask`, `redact`, `hash` oder `annotate`. Siehe [`pii`](#pii). |
| `structured` | Objekt | nicht gesetzt | Die Felder eines JSON-Schemas aus der Seite füllen. Siehe [`structured`](#structured). |
| `chunking` | Objekt | nicht gesetzt | Den Inhalt in Chunks zerlegen. Siehe [`chunking`](#chunking). |
| `describe_images` | Objekt | nicht gesetzt | Die Bilder der Seite mit dem LLM beschreiben. Siehe [`describe_images`](#describe_images). |

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/", "format": "text", "links": true, "metadata": true}
```

## `POST /v1/structured`

`POST /v1/extract` mit Pflichtfeld `structured`.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `url` | String | Pflicht | Wie bei `POST /v1/extract`. |
| `structured` | Objekt | Pflicht | Das JSON-Schema und wie es gefüllt wird. Siehe [`structured`](#structured). |
| `mode` | `fast`, `auto` oder `browser` | `auto` | Wie bei `POST /v1/extract`. |
| `format` | `markdown`, `text` oder `html` | `markdown` | Wie bei `POST /v1/extract`. |
| `metadata` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `links` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `compliance` | `enforce`, `report` oder `ignore` | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `cache` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `wait_seconds` | Zahl, 0 bis 60 | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `actions` | Liste von Objekten | leer | Wie bei `POST /v1/extract`. |
| `cookies` | Liste von Objekten | leer | Wie bei `POST /v1/extract`. |
| `headers` | Objekt | leer | Wie bei `POST /v1/extract`. |
| `screenshot` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `full_page` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `ocr` | `auto`, `never` oder `always` | `auto` | Wie bei `POST /v1/extract`. |
| `transcribe` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `languages` | Liste von Sprachcodes | leer | Wie bei `POST /v1/extract`. |
| `pii` | Modus oder Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `chunking` | Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `describe_images` | Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |

<!-- request: POST /v1/structured -->
```json
{"url": "https://example.org/mathe/brueche", "structured": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}, "strategy": "metadata"}}
```

## `POST /v1/chunk`

Eine Seite über ihre URL oder Ihr eigener Text, in Chunks zerlegt. Genau eins von `url` und `text`.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `url` | String | nicht gesetzt | Die Seite, die extrahiert und zerlegt wird, mit den Optionen von `POST /v1/extract`. |
| `text` | String | nicht gesetzt | Ihr eigener Text, höchstens `DOCUMENT_MAX_BYTES`. Mit `text` antworten die Felder einer Seite (`mode`, `links`, `compliance`, `cache`, `actions`, `cookies`, `headers`, `screenshot`, `full_page`, `ocr`, `transcribe`, `languages`, `describe_images`) mit 422. |
| `text_type` | `markdown` oder `plain` | `markdown` | Was `text` ist; Markdown-Chunks tragen ihren Überschriftenpfad. |
| `chunking` | Objekt | Pflicht | Wie zerlegt wird. Siehe [`chunking`](#chunking). |
| `format` | `markdown` oder `text` | `markdown` | Das Format von `content` und der Chunks. |
| `mode` | `fast`, `auto` oder `browser` | `auto` | Wie bei `POST /v1/extract`. |
| `metadata` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `links` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `compliance` | `enforce`, `report` oder `ignore` | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `cache` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `wait_seconds` | Zahl, 0 bis 60 | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `actions` | Liste von Objekten | leer | Wie bei `POST /v1/extract`. |
| `cookies` | Liste von Objekten | leer | Wie bei `POST /v1/extract`. |
| `headers` | Objekt | leer | Wie bei `POST /v1/extract`. |
| `screenshot` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `full_page` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `ocr` | `auto`, `never` oder `always` | `auto` | Wie bei `POST /v1/extract`. |
| `transcribe` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `languages` | Liste von Sprachcodes | leer | Wie bei `POST /v1/extract`. |
| `pii` | Modus oder Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `structured` | Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `describe_images` | Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |

<!-- request: POST /v1/chunk -->
```json
{"text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.\n\n## Addieren\n\nGleiche Nenner bleiben.", "chunking": {"strategy": "markdown", "size": 500, "overlap": 0}}
```

## `POST /v1/batch`

Viele URLs als ein Job; jeder Eintrag hat die Optionen von `POST /v1/extract`, ohne die
Browser-Optionen und `wait_seconds`.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `urls` | Liste von Strings | Pflicht | Die URLs, höchstens `BATCH_MAX_URLS`. Eine URL zweimal ergibt zwei Einträge; eine ungültige URL einen Eintrag, der mit `invalid_url` fehlschlägt. |
| `mode` | `fast`, `auto` oder `browser` | `auto` | Wie bei `POST /v1/extract`; `browser` reiht jede URL für die Browser-Worker ein. |
| `format` | `markdown`, `text` oder `html` | `markdown` | Wie bei `POST /v1/extract`. |
| `metadata` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `links` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `compliance` | `enforce`, `report` oder `ignore` | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `cache` | Boolean | `true` | Wie bei `POST /v1/extract`. |
| `ocr` | `auto`, `never` oder `always` | `auto` | Wie bei `POST /v1/extract`. |
| `transcribe` | Boolean | `false` | Wie bei `POST /v1/extract`. |
| `languages` | Liste von Sprachcodes | leer | Wie bei `POST /v1/extract`. |
| `pii` | Modus oder Objekt | nicht gesetzt | Wie bei `POST /v1/extract`, für jeden Eintrag. |
| `structured` | Objekt | nicht gesetzt | Wie bei `POST /v1/extract`, für jeden Eintrag. |
| `chunking` | Objekt | nicht gesetzt | Wie bei `POST /v1/extract`, für jeden Eintrag. |
| `describe_images` | Objekt | nicht gesetzt | Wie bei `POST /v1/extract`, für jeden Eintrag. |

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/a", "https://example.org/b"], "format": "markdown", "chunking": {"strategy": "sentence", "size": 800}}
```

## `POST /v1/site-structure`

Die Karte einer Website, als Job: ihre Sitemaps und ihr Linkgraph ab der Startseite. Seiten werden
nur über HTTP gelesen, ohne ihren Text.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `url` | String | Pflicht | Die Startseite. |
| `depth` | Ganzzahl, 0 bis `SITE_MAX_DEPTH` | `2` | Wie viele Links weit der Crawl von der Startseite aus geht; `0`: nur die Startseite und die Sitemaps. |
| `max_pages` | Ganzzahl, 1 bis `SITE_MAX_PAGES` | `100` | Höchstens so viele Seiten, die Startseite mitgezählt. |
| `sitemaps` | Boolean | `true` | robots.txt und die Sitemaps lesen, die sie nennt (oder `/sitemap.xml`). |
| `scope` | `host` oder `path` | `host` | Welchen Links gefolgt wird: `host` dem Host der Startseite, `path` nur URLs unter dem Verzeichnis der Start-URL. |
| `metadata` | Boolean | `true` | Die Metadaten jeder Seite mitliefern. |
| `cache` | Boolean | `true` | Den Ergebnis-Cache für Seiten nutzen. |
| `compliance` | `enforce`, `report` oder `ignore` | nicht gesetzt | Wie bei `POST /v1/extract`; unter `enforce` wird eine Seite, die robots.txt oder ein TDM-Nutzungsvorbehalt verbietet, nicht geholt. |

<!-- request: POST /v1/site-structure -->
```json
{"url": "https://example.org/", "depth": 2, "max_pages": 100, "scope": "host"}
```

## `POST /v1/screenshot`

Ein PNG einer Seite, im Browser gerendert.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `url` | String | Pflicht | Die Seite, die aufgenommen wird. |
| `full_page` | Boolean | `true` | Die ganze Seite (bis `SCREENSHOT_MAX_HEIGHT_PX`); `false`: die ersten 1920×1080. |
| `actions` | Liste von Objekten | leer | Seitenaktionen vor der Aufnahme, wie bei `POST /v1/extract`. |
| `cookies` | Liste von Objekten | leer | Wie bei `POST /v1/extract`. |
| `headers` | Objekt | leer | Wie bei `POST /v1/extract`. |
| `compliance` | `enforce`, `report` oder `ignore` | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `wait_seconds` | Zahl, 0 bis 60 | nicht gesetzt | Wie bei `POST /v1/extract`: Nach so vielen Sekunden wird die Aufnahme ein Job. |

<!-- request: POST /v1/screenshot -->
```json
{"url": "https://example.org/", "full_page": false}
```

## `POST /v1/extract/file`

Ein Upload als `multipart/form-data`: die Datei im Teil `file` und diese Formularfelder. Die
Optionsobjekte sind JSON-Strings.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `format` | `markdown`, `text` oder `html` | `markdown` | Das Format von `content`. |
| `metadata` | Boolean | `true` | Die Metadaten des Dokuments mitliefern (Titel, Autoren, Lizenz, Daten). |
| `ocr` | `auto`, `never` oder `always` | `auto` | Wie bei `POST /v1/extract`. |
| `wait_seconds` | Zahl, 0 bis 60 | `25` | Nach so vielen Sekunden wird ein laufender Upload ein Job (202). |
| `pii` | Modus oder JSON-Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `structured` | JSON-Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `chunking` | JSON-Objekt | nicht gesetzt | Wie bei `POST /v1/extract`. |
| `describe_images` | JSON-Objekt | nicht gesetzt | Ein hochgeladenes PNG, JPEG oder WebP beschreiben; wie bei `POST /v1/extract`. |

```bash
curl -H "Authorization: Bearer $KEY" -F file=@report.pdf -F format=markdown \
  -F 'chunking={"strategy": "markdown", "size": 800}' http://localhost:8080/v1/extract/file
```

## Optionsobjekte

### `pii`

Ein einfacher String ist der Modus: `"pii": "mask"` ist `"pii": {"mode": "mask"}`.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `mode` | `mask`, `redact`, `hash` oder `annotate` | Pflicht | Was mit einem Treffer geschieht. |
| `entities` | Liste von Entitätstypen | nicht gesetzt | Welche Entitäten gesucht werden; nicht gesetzt: `PII_DEFAULT_ENTITIES` des Betreibers. |
| `language` | `auto`, `de` oder `en` | `auto` | Die Sprache des Textes. |
| `score_threshold` | Zahl | nicht gesetzt | Der kleinste Score eines Treffers; nicht gesetzt: `PII_SCORE_THRESHOLD`. |

### `structured`

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `schema` | JSON-Schema | nicht gesetzt | Die Felder, die gefüllt werden. |
| `strategy` | `metadata`, `selectors`, `patterns`, `llm` oder `auto` | `auto` | Woher die Werte kommen; `auto` versucht die anderen der Reihe nach und fragt das LLM nur nach dem, was fehlt. |
| `selectors` | Objekt | leer | CSS-Regeln pro Feld (XPath für Admins). |
| `patterns` | Objekt | leer | Reguläre Ausdrücke pro Feld, auf dem Inhalt. |
| `tables` | Boolean | `false` | Auch die Tabellen der Seite liefern. |
| `learn_selectors` | Boolean | `false` | Das LLM Selektoren vorschlagen lassen, die die nächsten Seiten der Website wiederverwenden. |
| `instructions` | String | nicht gesetzt | Zusätzliche Anweisungen für das LLM. |

### `chunking`

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `strategy` | `fixed`, `recursive`, `markdown`, `sentence` oder `semantic` | `recursive` | Wo geschnitten wird. |
| `size` | Ganzzahl | `1000` | Höchstens so viele Zeichen pro Chunk. |
| `overlap` | Ganzzahl | `100` | Zeichen, die zwei Chunks teilen. |
| `breakpoint_percentile` | Ganzzahl | `90` | Für `semantic`: wie groß ein Bedeutungswechsel sein muss, damit geschnitten wird. |

### `describe_images`

`{}` nimmt die Standardwerte.

| Feld | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `max_images` | Ganzzahl, 1 bis 20 | `5` | Höchstens so viele Bilder werden beschrieben. |
