# Eine URL

English: [One URL](../en/single-url.md)

Extrahieren Sie eine Webseite oder ein Dokument über die URL, als Markdown oder Text, mit den
Metadaten. Diese Seite zeigt die drei Wege (die Spielwiese, REST und MCP) und die Felder der
Antwort, auf die es ankommt. Eigene Dateien, Audio und Video beschreibt
[Nutzung](usage.md#dateien-medien-und-personenbezogene-daten).

## In der Spielwiese

Öffnen Sie den Tab **Extrahieren**, geben Sie die URL ein und klicken Sie auf „Extrahieren“.

- „Format“: Markdown oder Text.
- „Browser-Fallback“: wenn die Seite ihn braucht (der Standard), nie oder immer; siehe
  [Browser-Fallback](usage.md#browser-fallback).
- „Website-Regeln“ (nur Admins): siehe [Website-Regeln](site-rules.md).
- „Metadaten“ (standardmäßig an), „Metadaten, strukturiert“ (die schema.org- und LRMI-Felder,
  nach Namen gelesen) und „Bilder beschreiben“.
- „Screenshot mitliefern“: die ganze Seite als PNG, im Browser aufgenommen; die Anfrage dauert
  daher länger. Das Kästchen ist bei „Browser-Fallback: nie“ und solange der Browser aus ist
  gesperrt; sein Hinweis nennt den Grund. Format und Größe gibt es im Reiter Screenshot.
- „Optionale Funktionen“: „OCR (Texterkennung)“ (automatisch, nie, immer), „Personenbezogene
  Daten“ (unverändert lassen, maskieren, schwärzen, hashen, nur markieren) und „Audio und Video
  ohne Untertitel transkribieren“. Standardmäßig ist nichts gewählt. Eine Funktion, die der Dienst
  ausgeschaltet hat, ist gesperrt, und ihr Hinweis nennt den Schalter (`DOCLING_ENABLED`,
  `PII_ENABLED`, `ASR_ENABLED`, `LLM_ENABLED`); ebenso eine Funktion, die der Dienst
  Admin-Schlüsseln vorbehält, solange Sie mit einem Nutzer-Schlüssel angemeldet sind
  (`OCR_ALLOW_USERS`, `PII_ALLOW_USERS`, `ASR_ALLOW_USERS`, `LLM_ALLOW_USERS`).
- „Optionen (JSON)“ unter „Erweitert“ (ein zugeklappter Abschnitt, mit „Sekunden warten, bevor
  daraus ein Job wird“) nimmt jedes weitere Feld des REST-Bodys, etwa `{"links": true}`.

Das Ergebnis beginnt mit einer Statuszeile (dem Status und dem HTTP-Status), einer Werkzeugleiste
und einer Karte mit Kennzahlen: URL, Sprache, Wörter, verwendeter Modus, Seitentyp und Zeit. Die
Werkzeugleiste hat „Markdown kopieren“ (oder „Text kopieren“; es erscheint nur, wo der Browser die
Zwischenablage erlaubt), den Download des Inhalts und „Antwort speichern (JSON)“, die ganze Antwort
als Datei. Darunter folgen zugeklappte Abschnitte für den Screenshot (nur wenn Sie einen verlangt haben),
die Metadaten, die Links, die Bilder, die entfernten Fehlermeldungen des Servers (nur wenn es
welche gibt) und „Ablauf“, eine Zeitleiste der Prüfungen, des Abrufs, des Renderns und der
Umwandlung (sie öffnet sich von selbst, wenn es Warnungen gibt), und danach der Inhalt in einer Lesekarte (bei Markdown
steht der Quelltext unter „Quelltext anzeigen“). Am Ende stehen „Als curl“, dieselbe Anfrage für
die Shell, und „Rohes JSON“, die ganze Antwort.

## Mit REST

`POST /v1/extract` nimmt die URL und die Optionen aus [Parameter](parameters.md#post-v1extract):

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/mathe/brueche", "format": "markdown", "links": true}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/mathe/brueche", "format": "markdown", "links": true}' \
  http://localhost:8080/v1/extract
# {"status": "success", "data": {"url": "https://example.org/mathe/brueche",
#  "upstream_status": 200, "mode_used": "fast", "language": "de", "word_count": 812,
#  "title": "Brüche addieren", "content": "# Brüche addieren\n\n...",
#  "quality": {"visible_chars": 5230, "word_count": 812, "js_heavy": false,
#   "bot_challenge": false, "soft_404": false, "login_wall": false, ...},
#  "compliance": {"mode": "enforce", "decision": "allowed", "robots": "allowed", ...},
#  "metadata": {"title": "Brüche addieren", "authors": ["A. Beispiel"],
#   "license": {"id": "CC-BY-SA-4.0", "source": "rel-license", ...},
#   "lrmi": {"educationalLevel": ["Sekundarstufe I"]}, "images": [...], ...},
#  "links": [...], "warnings": [], ...}, "error": null, ...}
```

Eine Seite, die JavaScript braucht, wird von selbst im Browser gerendert (`mode: auto`). Eine
Seite, die lange dauert, kann zu einem Job werden: Senden Sie `wait_seconds`, und lesen Sie den Job,
wenn die Antwort 202 ist; siehe [Synchron und Jobs](usage.md#synchron-und-jobs).

## Mit MCP

Das Tool `extract_url` nimmt die URL und dieselben Optionen:

```json
{"url": "https://example.org/mathe/brueche", "format": "markdown", "options": {"links": true}}
```

Ein langer `content` wird auf `max_chars` gekürzt; rufen Sie erneut mit dem `content_offset` auf,
den der Hinweis der Antwort nennt. Für eine langsame Seite kommt ein Job zurück: Rufen Sie
`get_job` auf, bis er abgeschlossen ist. Siehe [MCP](mcp.md).

## Die Felder der Antwort

Jede Antwort ist ein [Ergebnis-Envelope](usage.md#das-ergebnis-envelope): Lesen Sie zuerst
`status` und, wenn er nicht `success` ist, `error`. In `data`:

| Feld | Was es enthält |
| --- | --- |
| `content` | Der Text der Seite in `format`, ohne Navigation, Fußzeilen und andere wiederkehrende Seitenelemente. `title`, `language` und `word_count` beschreiben ihn. |
| `metadata` | Titel, Beschreibung, Autoren, Datumsangaben, Name der Website, kanonische URL, Schlagwörter, Bilder, die Lizenz (`license`, der beste Kandidat, und `licenses`, alle gefundenen) und die LRMI-Felder (`lrmi`). Aus mit `metadata: false`. |
| `quality` | Angaben zur Seite: `visible_chars`, `word_count`, `js_heavy`, `bot_challenge`, `soft_404`, `login_wall` und `browser_recommended`. Eine Seite mit `browser_recommended` sollte der Browser rendern. |
| `page_type` | Was für eine Seite es ist, nach Regeln und ohne LLM: ihr `type` (etwa `content`, `listing`, `imprint` oder `contact`) und die `signals`, die dafür sprechen (etwa `url`, `title` oder `schema_org`). Nur für HTML-Seiten; null für Dokumente, Medien, reinen Text, Markdown, Uploads und eine Seite, die mit einem HTTP-Fehler (4xx oder 5xx) gescheitert ist. Siehe [Seitentypen und Linkkategorien](whole-site-and-metadata.md#seitentypen-und-linkkategorien). |
| `links` | Mit `links: true`: die Links der Seite, jeder mit `url`, `text`, `kind` (`internal`, `external` oder `file`) und `category`, wohin er führt (etwa `imprint`, `social` oder `download`). |
| `compliance` | Was die Website-Regeln ergeben haben: der `mode`, die `decision` (`allowed`, `blocked`, `reported`, `skipped`), `robots` und die TDM-Angabe. Siehe [Website-Regeln](site-rules.md). |
| `warnings` | Was nicht wie gewünscht lief, obwohl ein Ergebnis vorliegt, etwa `browser_unavailable` (der Browser hat die Seite nicht übernommen: das HTTP-Ergebnis) oder `server_errors_removed` (siehe unten). |
| `server_errors` | Fehlerausgabe des Servers der Website, die aus dem Text entfernt wurde, oder null, wenn es keine gab oder eine Behandlung personenbezogener Daten angefordert wurde (die Meldungen sind Text der Seite): eine Liste mit `level` (etwa `Warning`, `Notice` oder `Fatal error`), `message`, `file`, `line` und `count` (gleiche Meldungen werden zusammengefasst), höchstens 50. |

Manche Websites geben PHP-Fehlerausgaben in ihre Seiten aus, etwa `Warning: Undefined variable $x in
/var/www/page.php on line 12`. Die Umwandlung entfernt eine solche Meldung aus dem Text, wenn sie
eine Zeile beginnt, wie PHP sie schreibt. Eine Meldung, die mitten in einem Satz zitiert wird,
bleibt stehen, ebenso eine innerhalb von `pre`, `code`, `kbd`, `samp`, `var`, `tt`, `textarea` und
`script`, wo sie zum Inhalt gehört. Für eine entfernte Meldung fügt die Umwandlung die Warnung
`server_errors_removed` hinzu und listet die Meldungen in `data.server_errors`. Die Spielwiese zeigt
sie in einem zugeklappten Abschnitt nach „Bilder“. Eine Meldung, die über mehrere Zeilen geht,
bleibt im Text: PHP 8 schreibt eine nicht abgefangene Ausnahme („Fatal error: Uncaught …“) mit
ihrem Stacktrace, und die wird nicht entfernt.

Eine Lizenz hat eine `id` (etwa `CC-BY-SA-4.0`), eine `url`, ein `label`, ihre `source` (ein Link
`rel-license`, JSON-LD, Microdata, RDFa, ein Meta-Tag oder der Text der Seite) und eine
`confidence`. `mode_used` sagt, ob HTTP oder der Browser die Antwort erzeugt hat, und
`upstream_status` ist der HTTP-Status der Seite.
