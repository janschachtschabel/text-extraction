# Ein Teil einer Website

English: [Part of a site](../en/part-of-a-site.md)

Extrahieren Sie einen Teil einer Website, etwa einen Bereich oder eine Liste von Seiten. Dafür
gibt es zwei Wege:

- Sie kennen die URLs noch nicht: Erfassen Sie den Teil mit einer Website-Struktur, die unter
  einem Verzeichnis bleibt (`scope: path`), und extrahieren Sie dann die Seiten, die Sie auswählen.
- Sie haben die URLs: Senden Sie sie als Batch, als Liste oder als CSV- oder JSON-Datei.

Eine ganze Website mit ihren Metadaten beschreibt
[Eine ganze Website und ihre Metadaten](whole-site-and-metadata.md).

## Den Teil erfassen

Eine Website-Struktur crawlt ab einer Start-URL. Mit `scope: "path"` folgt sie nur Links unter
dem Verzeichnis der Start-URL: Ab `https://example.org/mathe/` liest sie `/mathe/brueche`, aber
nicht `/deutsch/`. `depth` ist die Zahl der Klicks ab der Startseite (Standard 2, höchstens
`SITE_MAX_DEPTH`), und `max_pages` begrenzt die Seiten (Standard 100).

<!-- request: POST /v1/site-structure -->
```json
{"url": "https://example.org/mathe/", "scope": "path", "depth": 3, "max_pages": 200}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/mathe/", "scope": "path", "depth": 3, "max_pages": 200}' \
  http://localhost:8080/v1/site-structure
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "site", "state": "queued",
#  "total": 1, "status_url": "/v1/jobs/<job id>"}, ...}
```

Die Antwort ist ein Job. Seine Einträge sind die gelesenen Seiten, robots.txt und die Sitemaps;
ein Link, der das Verzeichnis verlässt, hat `crawl: out_of_scope`. Die Sitemaps listen die ganze
Website, nicht nur das Verzeichnis: Schalten Sie sie mit `"sitemaps": false` aus, oder lassen Sie
ihre Einträge bei der Auswahl der Seiten weg. Wie Sie die Einträge lesen, steht unter
[Die Website-Struktur lesen](whole-site-and-metadata.md#die-website-struktur-lesen).

Eine Website-Struktur liest die Seiten über HTTP und behält keinen Text (`content: null`). Um den
Text zu bekommen, senden Sie die ausgewählten URLs als Batch.

## Die Seiten extrahieren

`POST /v1/batch` extrahiert viele URLs als einen Job (höchstens `BATCH_MAX_URLS`, standardmäßig
1.000):

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/mathe/brueche", "https://example.org/mathe/prozent"], "format": "markdown", "metadata": true}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"urls": ["https://example.org/mathe/brueche", "https://example.org/mathe/prozent"],
       "format": "markdown", "metadata": true}' \
  http://localhost:8080/v1/batch
# 202 {"status": "success", "data": {"id": "<job id>", "kind": "batch", "state": "queued",
#  "total": 2, "status_url": "/v1/jobs/<job id>"}, ...}
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=100"
```

Jede URL bekommt ihr eigenes Ergebnis in `items`, mit den Feldern aus
[Eine URL](single-url.md#die-felder-der-antwort). Ein Batch hat keine Browser-Optionen
(`actions`, `cookies`, `headers`) und kein `wait_seconds`, nimmt aber `structured`, `chunking`
und `pii` für jeden Eintrag. Wie Sie die Einträge seitenweise lesen, steht unter
[Den Job lesen](whole-site-and-metadata.md#den-job-lesen).

## In der Spielwiese

- **Website-Struktur**: Geben Sie die Start-URL ein, setzen Sie „Bereich“ auf „path: nur
  unterhalb des Verzeichnisses der Start-URL“, dazu „Linktiefe“ und „Maximale Seitenzahl“,
  entfernen Sie das Häkchen bei „robots.txt und Sitemaps“ für `"sitemaps": false`, und klicken
  Sie auf „Website-Struktur lesen“. Wenn der Job abgeschlossen ist, listet „Als Baum anzeigen“
  seine Seiten nach Host und Pfad, jede als gecrawlt, Sitemap oder nur verlinkt markiert;
  während er läuft, zeigt „Baum jetzt anzeigen“ eine Momentaufnahme. Markieren Sie die Seiten,
  die Sie möchten, oder einen Abschnitt für alle seine Seiten, und klicken Sie auf „Auswahl
  extrahieren“: Die markierten URLs werden zu Batches, mit dem Format, dem Browser-Fallback, den
  Metadaten und den Optionen, die Sie unter dem Baum setzen.

  Jede URL des Baums zeigt ihren Seitentyp, etwa „Inhalt“, „Übersicht“ oder „Impressum“ (siehe
  [Seitentypen und Linkkategorien](whole-site-and-metadata.md#seitentypen-und-linkkategorien)),
  und „Typen:“ über dem Baum zählt sie. Eine gecrawlte Seite zeigt den Typ ihres Ergebnisses;
  eine Seite, die mit einem HTTP-Fehler gescheitert ist, hat keinen. Ein Sitemap-Eintrag oder
  eine nur verlinkte URL wurde nicht gelesen; deshalb wird ihr Typ allein aus ihrer URL geschätzt
  und als „aus der URL geschätzt“ markiert. Eine URL, die nichts sagt, zeigt keinen Typ.
  „Inhaltsseiten auswählen“ markiert jede URL außer Impressum, Datenschutz, Rechtlichem, Kontakt,
  Anmeldung, Suche und Fehlerseiten und entfernt bei diesen und bei jeder gescheiterten Seite
  das Häkchen; eine URL ohne Typ wird markiert, außer ihre Seite ist gescheitert.

  Der Baum markiert URLs, die die Regeln der Website sperren: das Kennzeichen „robots.txt“
  (robots.txt verbietet die URL, oder robots.txt konnte nicht abgerufen werden) oder „TDM“ (ein
  Vorbehalt in tdmrep.json, ein Verbot in ai.txt oder eine noai-Angabe). Der Grund steht als
  Tooltip am Kennzeichen und wird von Screenreadern vorgelesen. Jede angezeigte URL wird beim
  Anzeigen des Baums gegen die aktuellen Dateien robots.txt, tdmrep.json und ai.txt der Website
  beurteilt (aus dem Cache oder einmal pro Website geladen, innerhalb der Grenzen pro Host;
  höchstens 20 Websites und 12 s); die Markierungen zeigen also die Regeln zu diesem Zeitpunkt.
  Links, die der Crawl wegen der Regeln nicht verfolgt hat, erscheinen als Blätter „nur
  verlinkt“. Eine Zeile über dem Baum zählt die gesperrten URLs, eine weitere nennt Websites,
  deren Regeln nicht gelesen werden konnten. Unter „Regeln der Website beachten“ lassen sich
  gesperrte URLs nicht markieren, und „Alle auswählen“, „Inhaltsseiten auswählen“ und das Markieren
  eines Abschnitts überspringen sie; unter „Nur prüfen und melden“ oder „Prüfung aus“ lassen sie
  sich markieren, und die Schaltflächen schließen sie ein. Der Batch unter dem Baum beginnt mit der
  Stufe von „Website-Regeln“, mit der der Crawl lief.
- **Batch**: Fügen Sie die URLs ein, eine pro Zeile, und klicken Sie auf „Batch starten“. Mehr
  URLs, als ein Batch fasst, werden als mehrere Batches gesendet.
- **Batch-Datei**: Wählen Sie eine CSV- oder JSON-Datei und klicken Sie auf „Datei prüfen“. Eine
  CSV hat eine Spalte `url` (oder die URLs in ihrer ersten Spalte), mit Kommas, Semikolons oder
  Tabs. Eine JSON-Datei ist ein Array von URLs oder von Objekten mit `url`. Die Vorschau zeigt,
  wie viele Zeilen URLs sind, welche nicht und welche doppelt vorkommen, bevor Sie auf „Batch
  starten“ klicken.

Jeder Batch zeigt seinen Zustand, einen Fortschrittsbalken und seine Einträge und aktualisiert
sich alle 3 s, bis er abgeschlossen ist.

## Mit MCP

`site_structure` nimmt `url`, `depth`, `max_pages` und `scope`; `batch_extract` nimmt `urls`,
`format` und `mode`. Beide antworten mit einem Job: Rufen Sie `get_job` mit seiner `job_id` auf,
bis er abgeschlossen ist, und blättern Sie mit `offset` und `limit` durch seine Einträge.

```json
{"url": "https://example.org/mathe/", "scope": "path", "depth": 3, "max_pages": 200}
```

Mit einem Prompt wie „Den Bereich https://example.org/mathe/ dieser Website erfassen und jede
Seite über Brüche extrahieren“ ruft der Assistent `site_structure` auf, liest den Job mit
`get_job` und sendet die Seiten, die er ausgewählt hat, an `batch_extract`. Siehe [MCP](mcp.md).
