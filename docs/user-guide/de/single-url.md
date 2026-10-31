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
- „Optionen (JSON)“ nimmt jedes weitere Feld des REST-Bodys, etwa `{"links": true}`.

Das Ergebnis zeigt den Inhalt, die Metadaten, die Bilder, die Links, die Warnungen und „Ablauf“,
eine Zeitleiste der Prüfungen, des Abrufs, des Renderns und der Umwandlung. „Rohes JSON“ ist die
ganze Antwort und „Als curl“ dieselbe Anfrage für die Shell.

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
| `compliance` | Was die Website-Regeln ergeben haben: der `mode`, die `decision` (`allowed`, `blocked`, `reported`, `skipped`), `robots` und die TDM-Angabe. Siehe [Website-Regeln](site-rules.md). |
| `warnings` | Was nicht wie gewünscht lief, obwohl ein Ergebnis vorliegt, etwa `browser_unavailable` (der Browser hat die Seite nicht übernommen: das HTTP-Ergebnis). |

Eine Lizenz hat eine `id` (etwa `CC-BY-SA-4.0`), eine `url`, ein `label`, ihre `source` (ein Link
`rel-license`, JSON-LD, Microdata, RDFa, ein Meta-Tag oder der Text der Seite) und eine
`confidence`. `mode_used` sagt, ob HTTP oder der Browser die Antwort erzeugt hat, und
`upstream_status` ist der HTTP-Status der Seite.
