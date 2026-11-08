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
  wählen Sie bei „So wird die Website gelesen“ „Gründlich: Links folgen“ (die Voreinstellung
  „Schnell: Sitemap zuerst“ liest nur die Startseite, wenn die Website eine Sitemap hat, und eine
  Sitemap listet die ganze Website; ohne Häkchen bei „robots.txt und Sitemaps“ wird es von selbst
  gewählt), entfernen Sie das Häkchen bei „robots.txt und Sitemaps“ für `"sitemaps": false`, und klicken
  Sie auf „Website-Struktur lesen“. Wenn der Job abgeschlossen ist, listet „Als Baum anzeigen“
  seine Seiten nach Host und Pfad, jede als gecrawlt, Sitemap oder nur verlinkt markiert;
  während er läuft, zeigt „Baum jetzt anzeigen“ eine Momentaufnahme. Markieren Sie die Seiten,
  die Sie möchten, oder einen Abschnitt für alle seine Seiten, und klicken Sie auf „Auswahl
  extrahieren“: Die markierten URLs werden zu Batches, mit dem Format, dem Browser-Fallback, den
  Metadaten und den Optionen, die Sie unter dem Baum setzen.

  Der Baum hat ausgerichtete Spalten: Seite, Typ, Gefunden, Status und Tiefe, unter einem Kopf,
  der beim Scrollen sichtbar bleibt. Bei einem schmalen Baum sind die Zeilen kompakt (über die
  eigene Breite des Baums entscheidet, nicht die des Fensters). Ein Abschnitt zeigt, wie viele
  Seiten er enthält, und seinen eigenen Typ. Eine gecrawlte Seite zeigt
  ihren eigenen Titel ohne Anführungszeichen hinter dem Pfad; eine URL, die nicht gelesen wurde,
  aber von einer gecrawlten Seite verlinkt ist, zeigt den Linktext in Anführungszeichen (ein
  Sitemap-Eintrag hat keinen eigenen Titel). Eine Zusammenfassung über dem Baum zählt die
  URLs (gecrawlt, aus der Sitemap, nur verlinkt) und bei einem beendeten Crawl, wie lange er
  dauerte; die Filter-Schaltflächen unter „Nach Typ filtern“ zeigen nur die URLs eines Typs („Alle“
  zeigt jede URL; mehrere Filter-Schaltflächen zeigen die Vereinigung). Mit JavaScript ersetzen die
  Filter-Schaltflächen, jede mit ihrer Anzahl, die Zeile „Typen:“; ohne JavaScript bleibt die Zeile
  stehen, und nichts filtert. Solange ein Typ gefiltert ist, wirken „Alle auswählen“,
  „Inhaltsseiten auswählen“ und das Häkchen eines Abschnitts nur auf die angezeigten URLs, und
  „Keine auswählen“ entfernt jedes Häkchen, auch die verborgenen.

  Der Baum erscheint von selbst, sobald der Job ein Ergebnis hat, und aktualisiert sich, solange
  der Job läuft, alle paar Sekunden (das Intervall wird länger, wenn das Lesen des Jobs langsam
  ist, und eine fehlgeschlagene Aktualisierung wird wiederholt). Eine Aktualisierung behält Ihre
  Häkchen, die Faltung, den Filter, die Batch-Optionen, die Scrollposition und den Fokus und
  wartet, solange Sie in ein Feld tippen. Der Batch unter dem Baum bleibt bei der Aktualisierung
  bestehen.

  Jede URL des Baums zeigt ihren Seitentyp, etwa „Inhalt“, „Übersicht“, „Veranstaltung“,
  „Lernmaterial“ oder „Impressum“ (siehe
  [Seitentypen und Linkkategorien](whole-site-and-metadata.md#seitentypen-und-linkkategorien)),
  und die Zeile „Typen:“ über dem Baum (mit JavaScript die Filter-Schaltflächen) zählt sie. Eine gecrawlte Seite zeigt den Typ ihres Ergebnisses;
  eine Seite, die mit einem HTTP-Fehler gescheitert ist, hat keinen. Jede andere URL wurde nicht
  gelesen; deshalb wird ihr Typ geschätzt und als „geschätzt“ markiert, mit dem Grund: „aus der
  URL“ (die URL-Regeln, unter anderem Autorenseiten (`author`, `autor`: „Übersicht“), weitere
  Namen für Kontaktseiten sowie datierte Pfade wie `/2021/05/oer-camp/` oder WordPress-Adressen
  wie `?p=12` und `?page_id=12`: „Inhalt“; Sprach- und Tracking-Parameter wie `?lang=en` zählen
  nicht), „aus der Sitemap“ (der Dateiname der Sitemap, die sie nannte, etwa `post-sitemap.xml`
  oder `wp-sitemap-posts-event-1.xml`), „aus dem Linktext“ (ein Link, der wie ein Typ heißt,
  etwa „Kontakt“, oder in einer Seitennummerierung; der Name eines Typs zählt nur für eine kurze
  Adresse oder eine, deren letzter Teil kein Name aus mehreren Wörtern ist, so macht ein Link
  „Anmelden“ auf `/fortbildungen/mathe-workshop-2026` daraus keine Anmeldeseite), „aus
  Schlüsselwörtern“ (ein Wort des Pfads
  oder des Linktexts, etwa `veranstaltung` für „Veranstaltung“ oder `arbeitsblatt` für
  „Lernmaterial“) oder „Seite mit Namen, nicht abgerufen“ („Inhalt“, die Vorgabe für eine URL mit
  lesbarem Namen). Eine Schätzung aus der Vorgabe weicht jedem anderen Grund; bei einer URL aus
  einer Sitemap ersetzt ein Linktext sie aber nicht: Eine aufgeführte Seite, die als
  „Datenschutz“ verlinkt ist, bleibt „Inhalt“ und markiert. Nur eine URL, die
  keine Regel schätzen kann (eine Zahl, eine Hex-Zeichenfolge oder eine UUID als letzter Teil,
  ein letzter Teil ohne Wort aus drei Buchstaben oder eine Abfrage, die kein Sprach- oder
  Tracking-Parameter ist), zeigt das Kennzeichen „Typ offen“ mit dem Grund „Nicht abgerufen; der Typ zeigt sich beim Extrahieren“
  (als Tooltip und für Screenreader), und die Zeile „Typen:“ und die Filter-Schaltflächen zählen „Typ offen“ mit.
  „Inhaltsseiten auswählen“ markiert jede URL außer Impressum, Datenschutz, Rechtlichem, Kontakt,
  Anmeldung, Suche und Fehlerseiten und entfernt bei diesen und bei jeder gescheiterten Seite
  das Häkchen; eine URL ohne Typ wird markiert, außer ihre Seite ist gescheitert.

  Der Baum markiert URLs, die die Regeln der Website sperren: das Kennzeichen „robots.txt“
  (robots.txt verbietet die URL, oder robots.txt konnte nicht abgerufen werden) oder „TDM“ (ein
  Vorbehalt in tdmrep.json, ein Verbot in ai.txt oder eine noai-Angabe). Der Grund steht als
  Tooltip am Kennzeichen und wird von Screenreadern vorgelesen. Jede angezeigte URL wird beim
  Anzeigen des Baums gegen die aktuellen Dateien robots.txt, tdmrep.json und ai.txt der Website
  beurteilt; die Markierungen zeigen also die Regeln zu diesem Zeitpunkt. Geladen werden die
  Regeldateien (innerhalb der Grenzen pro Host; höchstens 20 Websites und 12 s) nur für die
  Websites, die der Crawl kontaktiert hat: die Start-Website, Websites, zu denen er
  weitergeleitet wurde, die Hosts von robots.txt und Sitemaps und die Seiten, die er eingereiht
  hat. Jede andere Website wird nur aus dem Cache gelesen und, wenn sie dort fehlt, als nicht
  geprüft aufgeführt. Eine sehr große Regeldatei (über 1.000 Regeln) wird nach ihren ersten
  1.000 Regeln beurteilt, so gut es geht; die Prüfung des Batches entscheidet trotzdem.
  Links, die der Crawl wegen der Regeln nicht verfolgt hat, erscheinen als Blätter „nur
  verlinkt“. Eine Zeile über dem Baum zählt die gesperrten URLs, eine weitere nennt Websites,
  deren Regeln nicht gelesen werden konnten. Unter „Regeln der Website einhalten“ lassen sich
  gesperrte URLs nicht markieren, und „Alle auswählen“, „Inhaltsseiten auswählen“ und das Markieren
  eines Abschnitts überspringen sie; unter „Nur prüfen und melden“ oder „Prüfung aus“ lassen sie
  sich markieren, und die Schaltflächen schließen sie ein. Der Batch unter dem Baum beginnt mit der
  Stufe von „Website-Regeln“, mit der der Crawl lief.

  Ein Abschnitt, der nur seine eigene Seite enthält, ist eine Zeile und kein Abschnitt mit einem
  einzelnen Blatt. Jeden Abschnitt können Sie mit der Schaltfläche vor seinem Namen auf- und
  zuklappen: Die oberste Ebene beginnt aufgeklappt, die Ebenen darunter beginnen zugeklappt.
  „Alle aufklappen“ und „Alle zuklappen“ über dem Baum erledigen das für jeden Abschnitt. Ohne
  JavaScript zeigt der Baum jeden Knoten aufgeklappt. Das Markieren eines Abschnitts markiert auch
  jede wählbare URL darunter, zugeklappt oder nicht, und der Zähler zählt sie; „Alle auswählen“
  tut dasselbe. Eine gesperrte URL in einem zugeklappten Abschnitt bleibt unter „Regeln der
  Website einhalten“ unmarkiert.

  Unter dem Baum zeigt die Liste „Soziale Medien“ die Profil-Links der Website (ihre Links zu
  sozialen Netzwerken). Sie liegen außerhalb der Website und lassen sich nicht auswählen.
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

`site_structure` nimmt `url`, `depth`, `max_pages`, `scope` und `discovery`; `batch_extract` nimmt `urls`,
`format` und `mode`. Beide antworten mit einem Job: Rufen Sie `get_job` mit seiner `job_id` auf,
bis er abgeschlossen ist, und blättern Sie mit `offset` und `limit` durch seine Einträge.

```json
{"url": "https://example.org/mathe/", "scope": "path", "depth": 3, "max_pages": 200}
```

Mit einem Prompt wie „Den Bereich https://example.org/mathe/ dieser Website erfassen und jede
Seite über Brüche extrahieren“ ruft der Assistent `site_structure` auf, liest den Job mit
`get_job` und sendet die Seiten, die er ausgewählt hat, an `batch_extract`. Siehe [MCP](mcp.md).
