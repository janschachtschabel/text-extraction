# Strukturierte Daten

English: [Structured data](../en/structured-data.md)

`structured` füllt die Felder eines JSON-Schemas aus einer Seite: einen Titel, eine Dauer, eine
Lizenz, eine Bildungsstufe, was immer das Schema nennt. `chunking` zerlegt den Inhalt in Chunks,
etwa für einen Suchindex. Diese Seite erklärt die Strategien, die Anfrage und die Antwort, das
LLM und seinen Schlüssel und das Chunking, jeweils in der API, im MCP-Server und in der
Spielwiese.

## Die Strategien

`strategy` sagt, woher die Werte kommen:

| Strategie | Woher die Werte kommen |
| --- | --- |
| `metadata` | Aus JSON-LD, Microdata, RDFa, OpenGraph und den Meta-Tags der Seite, nach Feldnamen (etwa `name`, `license` oder `educationalLevel`). Kein LLM. |
| `selectors` | Aus Ihren CSS-Regeln pro Feld (XPath mit einem Admin-Schlüssel) und den Selektoren, die für Ihren API-Schlüssel und die Website gelernt wurden. Kein LLM. |
| `patterns` | Aus Ihren regulären Ausdrücken pro Feld, auf dem Inhalt. Kein LLM. |
| `llm` | Das LLM liest den Inhalt und füllt das Schema. |
| `auto` | Der Standard. Für jedes Feld die erste Quelle, die einen Wert findet: Ihre Selektoren (oder die gelernten), die Metadaten der Seite, Ihre Muster und das LLM nur für die Felder, die dann noch fehlen. |

## Die Anfrage

| Feld | Was es bewirkt |
| --- | --- |
| `schema` | Die zu füllenden Felder als JSON-Schema einer Teilmenge: ein `object` aus Feldern der Typen `string`, `number`, `integer`, `boolean`, `array` und `object`, höchstens 3 Ebenen und 50 Eigenschaften. |
| `selectors` | Eine Regel pro Feld: `css` oder `xpath`, optional `attr` (etwa `href`) und `all`. |
| `patterns` | Eine Regel pro Feld: `regex`, `group` (eine Nummer oder ein Name), `flags` und `all`. |
| `tables` | `true`: auch die Tabellen der Seite liefern. |
| `learn_selectors` | `true` (mit `llm` oder `auto`): Das LLM schlägt für jeden gefundenen Wert einen CSS-Selektor vor; die Selektoren, die denselben Wert wiederfinden, werden für Ihren API-Schlüssel, die Website und das Schema gespeichert. |
| `instructions` | Freier Text für das LLM (was ein Feld bedeutet, die Sprache), mit `llm` oder `auto`. |

<!-- request: POST /v1/structured -->
```json
{"url": "https://example.org/mathe/brueche", "structured": {"schema": {"type": "object", "properties": {"name": {"type": "string"}, "bearbeitungszeit": {"type": "integer"}, "merksatz": {"type": "string"}}}, "patterns": {"bearbeitungszeit": {"regex": "Bearbeitungszeit: (\\d+)", "group": 1}}}}
```

## Die Antwort

`data.structured` enthält:

- `values`: jedes Feld der obersten Ebene des Schemas, null, wenn es nicht gefunden wurde;
- `sources`: woher jeder Wert kam (`metadata`, `selectors`, `learned`, `patterns` oder `llm`);
- `missing`: die Felder ohne Wert;
- `tables` (mit `tables: true`), `learned` (mit `learn_selectors`: was gespeichert (`stored`) und
  was verworfen (`rejected`) wurde) und `llm`: die Aufrufe des LLM, null ohne sie.

Ein Wert, der nicht zum Typ seines Felds passt, entfällt, mit der Warnung
`structured_value_invalid`.

## Mit REST

`POST /v1/structured` ist `POST /v1/extract` mit Pflichtfeld `structured`. `POST /v1/extract`,
`/v1/extract/file` und `/v1/batch` nehmen `structured` ebenfalls; ein Batch füllt das Schema für
jeden Eintrag.

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
#  "llm": {"model": "<LLM_MODEL>", "calls": 1, ...}, "stage": "done", ...}, ...}, ...}
```

## Das LLM und sein Schlüssel

Die Strategie `llm`, `learn_selectors` und das `semantic`-Chunking brauchen den LLM-Worker des
Betreibers (`LLM_ENABLED`); ohne ihn ergeben sie 400. `auto` behält dann die gefundenen Werte, und
für die Felder, die noch fehlen, kommt die Warnung `llm_unavailable`.

- Ihr eigener Schlüssel für den LLM-Endpunkt gehört in den Header `X-LLM-Api-Key`. Er hat Vorrang
  vor `LLM_API_KEY` des Betreibers und wird nie gespeichert.
- Eine Anfrage, die das LLM braucht, wartet `LLM_SYNC_WAIT_SECONDS` (30 s) und antwortet dann mit
  202 und einem Job.
- Kann der LLM-Schritt nicht laufen, behält die Antwort ihren Inhalt und ist `partial` mit
  `error.code: llm_failed` (wiederholbar).

Mit `learn_selectors` brauchen die nächsten Seiten derselben Website für die gelernten Felder
kein LLM; in ihren `sources` steht `learned`.

## Chunking

`chunking` zerlegt den Inhalt in Chunks von höchstens `size` Zeichen (Standard 1.000, `overlap`
100): `fixed`, `recursive` (der Standard), `markdown` (an Überschriften; jeder Chunk trägt seinen
Überschriftenpfad), `sentence` oder `semantic` (geschnitten, wo sich die Bedeutung ändert, mit den
Embeddings des Endpunkts). `POST /v1/chunk` nimmt eine `url` oder Ihren eigenen `text` (Markdown
oder `"text_type": "plain"`):

<!-- request: POST /v1/chunk -->
```json
{"text": "# Brüche\n\nEin Bruch beschreibt einen Anteil.\n\n## Addieren\n\nGleiche Nenner bleiben.", "chunking": {"strategy": "markdown", "size": 500, "overlap": 0}}
```

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
`/v1/batch` nehmen `chunking` ebenfalls; jedes Ergebnis eines Batches hat seine eigenen Chunks:

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/a", "https://example.org/b"], "chunking": {"strategy": "sentence", "size": 800}}
```

## In der Spielwiese

- **Strukturiert**: Geben Sie die URL ein, und bearbeiten Sie das Feld „Strukturiert (JSON:
  strategy, schema, rules)“, das mit einem Schema aus `title` und `author` und der Strategie
  `auto` beginnt. Klicken Sie auf „Felder extrahieren“. Das Ergebnis zeigt „Strukturierte Werte“,
  eine Tabelle mit jedem Feld, seinem Wert und seiner Quelle, und die Felder, die fehlen.
- **Chunks**: Geben Sie eine URL ein oder fügen Sie einen Text ein (nicht beides), wählen Sie, was
  der Text ist, bearbeiten Sie das Feld „Chunking (JSON)“, und klicken Sie auf „In Chunks
  zerlegen“. Das Ergebnis listet die Chunks.

Für das LLM melden Sie sich mit Ihrem LLM-Schlüssel unter „Optionale Schlüssel“ an; die Spielwiese
sendet ihn als `X-LLM-Api-Key`.

## Mit MCP

`extract_structured` nimmt `url` und `structured` (Pflicht); `chunk` nimmt `url` oder `text` und
`chunking` (Pflicht). Ihr LLM-Schlüssel gehört in den Header `X-LLM-Api-Key` der MCP-Verbindung,
nie in ein Argument eines Tools:

```json
{"url": "https://example.org/mathe/brueche", "structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"name": {"type": "string"}, "license": {"type": "string"}}}}}
```

Mit einem Prompt wie „Titel, Lizenz und Bildungsstufe von https://example.org/mathe/brueche
lesen“ ruft der Assistent `extract_structured` auf. Siehe [MCP](mcp.md).
