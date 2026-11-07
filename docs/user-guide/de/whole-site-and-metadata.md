# Eine ganze Website und ihre Metadaten

English: [A whole site and its metadata](../en/whole-site-and-metadata.md)

Erfassen Sie eine ganze Website, und extrahieren Sie dann jede Seite mit ihren Metadaten: den
schema.org- und LRMI-Feldern und den Lizenzen. Das sind drei Schritte: eine Website-Struktur in
der Tiefe, die Sie brauchen, Batches ihrer Seiten mit `metadata` und strukturierten Metadaten und
das seitenweise Lesen der Jobs. Einen einzelnen Bereich einer Website beschreibt
[Ein Teil einer Website](part-of-a-site.md).

## Die Website erfassen

`POST /v1/site-structure` erfasst eine Website: ihre Sitemaps (die `Sitemap:`-Zeilen der robots.txt,
sonst nacheinander `/sitemap.xml`, `/sitemap_index.xml` oder `/wp-sitemap.xml`, bis eine existiert;
Indizes, gzip, Text, RSS und Atom) mit ihren URLs und ihren Linkgraphen ab der Startseite, `depth` Klicks tief (Standard 2, höchstens `SITE_MAX_DEPTH`), für höchstens `max_pages`
Seiten (Standard 100, höchstens `SITE_MAX_PAGES`). Die Antwort ist 202 mit einem Job der Art `site`;
die Extract-Worker crawlen, und jede Seite, robots.txt und Sitemap-Datei, die sie finden, wird zu
einem Eintrag des Jobs, sodass sein `total` wächst, solange er läuft:

<!-- request: POST /v1/site-structure -->
```json
{"url": "https://example.org/", "depth": 3, "max_pages": 1000}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/", "depth": 3, "max_pages": 1000}' \
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
#     "rel": [], "hreflang": null, "region": "nav", "crawl": "queued", "target": 2,
#     "category": "content"},
#    {"url": "https://example.org/intern/", "text": "Intern", "kind": "internal", "source": "a",
#     "rel": [], "hreflang": null, "region": "main", "crawl": "blocked", "target": null,
#     "category": "content"},
#    {"url": "https://example.org/impressum", "text": "Impressum", "kind": "internal",
#     "source": "a", "rel": [], "hreflang": null, "region": "footer", "crawl": "queued",
#     "target": 4, "category": "imprint"}, ...]},
#   "page_type": {"type": "home", "signals": ["root_path"]}}}},
#  {"index": 1, "url": "https://example.org/robots.txt", "result": {"data": {"site": {
#   "role": "robots", "depth": 0, "parent": 0,
#   "sitemaps": ["https://example.org/sitemap.xml"]}, ...}, ...}},
#  {"index": 2, "url": "https://example.org/mathe/", "result": {"data": {"site": {
#   "role": "page", "depth": 1, "parent": 0, "links": [...]}, ...}, ...}},
#  {"index": 3, "url": "https://example.org/sitemap.xml", "result": {"data": {"site": {
#   "role": "sitemap", "depth": 0, "parent": 1, "sitemap": {"format": "urlset", "guessed": false,
#   "entries": [{"loc": "https://example.org/mathe/", "lastmod": "2026-10-01",
#                "type_guess": "listing"}, ...],
#   "children": [], "entry_count": 2400, "part": 1, "parts": 3}}, ...}, ...}}, ...]}, ...}
```

Seiten werden nur über HTTP geholt, mit dem User-Agent des Dienstes, den Grenzen pro Host und den
Prüfungen von `compliance`; die Einträge behalten Titel, Status, Metadaten und Qualität, aber
keinen Text (`content: null`). Der Crawl bleibt auf dem Host der Startseite (ein führendes `www.`
zählt nicht); `scope: "path"` hält ihn unter dem Verzeichnis der Start-URL, `sitemaps: false`
lässt die Sitemaps aus, und `depth: 0` liest nur die Startseite und die Sitemaps. Mit
`discovery: "sitemap_first"` liest der Crawl nur die Startseite, wenn robots.txt eine Sitemap nennt
oder eine der drei Adressen eine liefert, und entnimmt die übrigen Seiten der Sitemap; ohne Sitemap
folgt er den Links wie sonst. `discovery: "sitemap_first"` mit `sitemaps: false` ergibt 422. Die
Weboberfläche bietet das als „Schnell: Sitemap zuerst“ (Voreinstellung; „robots.txt und Sitemaps“
muss angehakt sein) neben „Gründlich: Links folgen“ an; der API-Standard ist `discovery: "crawl"`. Ein Crawl wird
zusammen mit Ihren Batches auf `BATCH_MAX_OPEN_PER_CLIENT` angerechnet.

### Die Website-Struktur lesen

Lesen Sie den Graphen aus den Einträgen: `data.site.role` jedes Eintrags ist `page`, `robots`,
`sitemap` oder `sitemap_part`, und `parent` ist der Index des Eintrags, der ihn gefunden hat. Die
`links` einer Seite sind ihre ausgehenden Links in Dokumentreihenfolge: `kind` (`internal`,
`external`, `file`), `source` (`a`, `area`, ein `link` im Kopf wie `canonical` oder `alternate`,
`iframe`), `rel`, `hreflang`, `region` (`nav`, `header`, `footer`, `aside`, `main`, `breadcrumb`,
`pagination` oder null), `crawl`, was der Crawl mit ihm gemacht hat, und `category`, wohin er
führt (siehe [Seitentypen und Linkkategorien](#seitentypen-und-linkkategorien)).

- `queued` und `seen` führen zu einem Seiteneintrag, dessen Index `target` ist: eine Kante des
  Graphen (ein Link tiefer als `depth` auf eine Seite, die ohnehin ein Eintrag ist, hat ebenfalls
  `seen`).
- Den anderen wird nicht gefolgt: `depth` (tiefer als gewünscht), `listed` (Links im Kopf und
  Iframes), `file`, `out_of_scope` (ein anderer Host oder außerhalb des Startverzeichnisses mit
  `scope: "path"`), `nofollow`, `blocked` (robots.txt oder ein TDM-Nutzungsvorbehalt unter
  `compliance: enforce`), `max_pages`, `memory_guard` und `sitemap_first` (ein Crawl mit
  `discovery: "sitemap_first"` hat eine Sitemap gefunden, daher wurde dem Link nicht gefolgt).

Die Einträge einer Sitemap stehen in `sitemap.entries`; eine große setzt sich in
`sitemap_part`-Einträgen mit derselben URL fort (`part` von `parts`), und ein Index listet seine
Sitemaps in `children`, jede mit dem Eintrag, der aus ihr wurde. Jeder Eintrag trägt ein
`type_guess`: den Seitentyp, der aus seiner URL und dem `type_hint` der Sitemap (was ihr Dateiname
nahelegt, etwa `listing` bei einer WordPress-Kategorie-Sitemap) geschätzt wird, ohne die Seite
abzurufen; sagt die URL nichts, fehlt das `type_guess`. Es ist eine Schätzung, kein `page_type`;
Sitemaps, die vor diesem Feld gelesen wurden, haben keines. Ein Eintrag, dessen `final_url`
die URL eines anderen Eintrags ist, ist eine Weiterleitung auf diese Seite (seine Links
wiederholen die der Seite): Entfernen Sie die Duplikate des Graphen anhand von `final_url`.

### Seitentypen und Linkkategorien

Jede HTML-Seite, die der Crawl liest, bekommt einen `page_type`, ebenso eine Seite aus
`POST /v1/extract`: ihren `type` und die `signals`, die dafür sprechen. Es entscheiden Regeln,
ohne LLM. Sie werden in dieser Reihenfolge geprüft, und die erste, die zutrifft, gewinnt:

1. `error`: Die Seite hat mit 2xx geantwortet, ist aber eine Fehlerseite (ein Soft-404,
   `quality.soft_404`). Eine Seite, die mit einem HTTP-Fehler (4xx oder 5xx) gescheitert ist,
   wird nicht umgewandelt und hat keinen Typ (`page_type: null`); ihr Status sagt, was geschah.
2. `home`: Der Pfad ist leer, `/`, eine Indexdatei wie `/index.html` oder nur ein Sprachkürzel wie
   `/de/` oder `/en-gb/` (mit oder ohne Indexdatei), und die Adresse hat keine Abfrage außer
   Tracking-Parametern wie `utm_source` oder `fbclid` und Sprachparametern (`lang`, `hl`,
   `locale`): `/?lang=en` ist die Startseite, `/?p=12` nicht. Es zählen nur Sprachen, die Websites
   üblicherweise nutzen, darum ist ein Bereich wie `/ki/` oder `/ui/` keine Startseite. Ein Pfad,
   der nur ein Sprachkürzel ist, ist die Startseite, es sei denn, die Seite gibt eine andere
   Sprache an (`<html lang>`): Über `/it/` auf einer deutschen Seite entscheiden die übrigen
   Regeln.
3. schema.org: Das JSON-LD oder die Microdata der Seite sagen `ContactPage` (`contact`),
   `AboutPage` (`about`), `SearchResultsPage` (`search`) oder `CollectionPage` (`listing`); trifft
   keine davon zu, nennt ein `Event`-Typ (`Event`, `EducationEvent` und andere Namen, die auf
   `Event` enden) `event`, und `LearningResource`, `Quiz` oder `Course` nennt `learning_material`.
4. Die URL: ein ganzes Pfadsegment wie `impressum` oder `imprint` (`imprint`), `datenschutz`
   oder `privacy` (`privacy`), `agb`, `terms`, `cookies` oder `barrierefreiheit` (`legal`),
   `login`, `registrieren` oder `konto` (`auth`), `suche` oder `search` oder eine Suchanfrage
   wie `?q=...` (`search`), `kontakt` oder `contact` (`contact`), `ueber-uns`, `about` oder `team`
   (`about`), `kategorie`, `tag`, `archiv` oder `page/2` (`listing`). `/impressum.html` ist ein
   Impressum, `/impressum-archiv-2019` nicht. Ein Name zählt als erster oder letzter Teil des
   Pfads (ein Sprachkürzel davor zählt nicht mit): `/de/impressum` und `/service/impressum` sind
   Impressen. Ein Listenname zählt mit höchstens einem Teil danach: `/kategorie/mathe` ist eine
   Liste, `/kategorie/mathe/bruchrechnung` nicht. Einige Wörter, die auch gewöhnliche Seiten
   benennen, zählen nur allein: `cookies`, `terms`, `legal`, `konto`, `account`, `profil`,
   `profile`, `recherche`, `team`, `password`, `passwort` und `disclaimer`. So ist `/cookies`
   `legal`, `/rezepte/cookies` aber nicht.
5. Der Titel beginnt oder endet mit dem Namen eines Typs, etwa „Impressum“, „Kontakt“, „Login“
   oder „Suchergebnisse“ („Impressum | Firma“ und „Firma | Impressum“). Ein Doppelpunkt nach dem
   Namen leitet einen Untertitel ein: „Datenschutz: Was Schulen wissen müssen“ ist keine
   Datenschutzseite („Impressum:“ ohne etwas danach ist ein Impressum). Die erste Überschrift
   zählt nur, wenn sie allein aus dem Namen besteht: „Kontakt“, nicht „Kontakt zur Schule
   aufnehmen“.
6. `auth`: ein Passwortfeld auf einer Seite mit wenig Text.
7. `listing`: ein Link auf die nächste oder vorige Seite (`rel="next"`) oder eine Seite, die
   vor allem aus Links besteht (mindestens 20 Links, die 60 % ihres Textes ausmachen).
8. `content`, wenn die Seite mindestens `QUALITY_MIN_WORDS` (50) Wörter hat, sonst `other`.

Ein Artikel (`og:type` ist `article`) mit mindestens `QUALITY_MIN_WORDS` Wörtern bleibt
`content`, gleich was sein Titel, seine erste Überschrift, seine Links oder eine Listenadresse
(etwa `/blog/page/2` oder `/tag/python`) sagen. Nur eine andere Adresse (etwa `/impressum/`),
schema.org oder ein Passwortfeld auf einer kurzen Seite (`auth`) können ihn zu einem anderen Typ
machen.

`signals` nennt jede Regel, die zum Typ passt, in dieser Reihenfolge, auch die nach der
entscheidenden: `/impressum` mit dem Titel „Impressum“ hat `["url", "title"]`.

Jeder Link (hier `site.links`, bei `POST /v1/extract` `links`) hat eine `category`: `download`
für eine Datei, `social` für ein soziales Netzwerk oder eine Videoplattform (etwa Mastodon,
YouTube, Instagram oder LinkedIn) und für einen Link innerhalb der Website den Typ, den seine URL
sagt (`home`, `listing`, `imprint`, `privacy`, `legal`, `contact`, `about`, `auth`, `search`).
Sonst ist er `nav` für eine Navigationshilfe wie „Nach oben“ oder „Menü“ und `content` für alles
andere, auch für jeden anderen Link auf eine fremde Website. Ergebnisse, die vor den Seitentypen
gespeichert wurden, haben keines der beiden Felder (null).

## Jede Seite mit ihren Metadaten extrahieren

Die URLs zum Extrahieren sind die Seiteneinträge und, wenn Sie möchten, die Einträge der
Sitemaps. Senden Sie sie an `POST /v1/batch`, höchstens `BATCH_MAX_URLS` (1.000) pro Batch, mit
`metadata` und einer `structured`-Anfrage der Strategie `metadata`:

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://example.org/", "https://example.org/mathe/"], "metadata": true, "structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"name": {"type": "string"}, "license": {"type": "string"}, "educationalLevel": {"type": "array", "items": {"type": "string"}}, "learningResourceType": {"type": "array", "items": {"type": "string"}}}}}}
```

Das Ergebnis jedes Eintrags enthält dann:

- `metadata`: Titel, Autoren, Datumsangaben, Schlagwörter und Bilder; die Lizenz als `license` (der
  beste Kandidat, mit `id` wie `CC-BY-4.0`, `url`, `source` und `confidence`) und `licenses` (alle
  gefundenen); und `lrmi`, die LRMI-Felder der Seite (`educationalLevel`, `learningResourceType`,
  `audience`, `typicalAgeRange`, `teaches` und weitere).
- `structured.values`: die Felder Ihres Schemas, gelesen aus JSON-LD, Microdata, RDFa, OpenGraph
  und den Meta-Tags der Seite, mit `sources` und `missing`. Siehe
  [Strukturierte Daten](structured-data.md).

Die Strategie `metadata` braucht kein LLM. Ein Schlüssel darf höchstens
`BATCH_MAX_OPEN_PER_CLIENT` (5) unfertige Batches und Website-Strukturen gleichzeitig haben;
starten Sie den nächsten Batch, wenn einer abgeschlossen ist.

## Den Job lesen

`GET /v1/jobs/{id}` zeigt den `state` eines Jobs (`queued`, `running`, `completed`), seinen
`progress` (die Einträge nach Status) und eine Seite seiner Einträge. `offset` ist der erste
Eintrag und `limit` die Zahl der Einträge (höchstens 500):

```bash
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=0&limit=500"
curl -H "Authorization: Bearer $KEY" "http://localhost:8080/v1/jobs/<job id>?offset=500&limit=500"
```

Lesen Sie mit `next_offset` weiter, bis es null ist (die letzte Seite): Eine Seite endet früher,
sobald ihre Ergebnisse etwa 20 MB erreichen, setzen Sie also immer bei `next_offset` fort, nicht
bei `offset + limit`. Jeder Eintrag hat seinen
`index`, seine `url`, seinen `state`, sein `result` (null, bis er fertig ist) und `run`, seine
letzte Zuteilung an einen Worker. Ein Job wird `JOB_TTL_SECONDS` (24 h) aufbewahrt;
`DELETE /v1/jobs/{id}` bricht ihn ab und löscht ihn sofort.

## In der Spielwiese

1. **Website-Struktur**: Geben Sie die Start-URL ein, setzen Sie „Linktiefe“ und „Maximale
   Seitenzahl“, lassen Sie „Bereich“ beim Host, wählen Sie bei „So wird die Website gelesen“
   „Schnell: Sitemap zuerst“ (Voreinstellung) oder „Gründlich: Links folgen“, und klicken Sie auf
   „Website-Struktur lesen“.
2. Wenn der Job abgeschlossen ist, listet „Als Baum anzeigen“ die Seiten nach Host und Pfad;
   während er läuft, zeigt „Baum jetzt anzeigen“ eine Momentaufnahme; der Baum erscheint auch von
   selbst, sobald der Job ein Ergebnis hat, und aktualisiert sich, solange er läuft. Jede URL zeigt
   ihren Seitentyp, und die Zeile „Typen:“ über dem Baum zählt sie; mit JavaScript ersetzen
   Schaltflächen unter „Nach Typ filtern“ diese Zeile und zeigen nur die URLs eines Typs (ohne
   JavaScript bleibt die Zeile stehen). „Alle auswählen“ markiert jede angezeigte URL (unter einem
   Typfilter nur die angezeigten) und „Inhaltsseiten auswählen“ jede angezeigte URL außer Impressum, Datenschutz, Rechtlichem, Kontakt,
   Anmeldung, Suche, Fehlerseiten und gescheiterten Seiten; oder markieren Sie die Abschnitte,
   die Sie möchten. URLs, die die Regeln der Website sperren, sind mit „robots.txt“ oder „TDM“
   markiert und werden von den Schaltflächen übersprungen, solange „Website-Regeln“ auf „Regeln der
   Website einhalten“ steht; der Batch unter dem Baum beginnt mit der Stufe, mit der der Crawl lief,
   und die Markierungen zeigen die Regeln zum Zeitpunkt der Anzeige des Baums (siehe
   [Teil einer Website](part-of-a-site.md#in-der-spielwiese)).
3. Lassen Sie „Metadaten“ an, und tragen Sie für die schema.org- und LRMI-Felder eine
   `structured`-Anfrage in „Optionen (JSON)“ (unter „Erweitert“) ein, zum Beispiel
   `{"structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"license": {"type": "string"}}}}}`.
   Klicken Sie auf „Auswahl extrahieren“.
4. Jeder Batch zeigt seinen Fortschritt und seine Einträge; „Vorherige“ und „Nächste“ blättern
   durch sie, und „Anzeigen“ öffnet das Ergebnis eines Eintrags mit seinen Metadaten.

Für eine einzelne Seite hat der Tab **Extrahieren** das Kästchen „Metadaten, strukturiert“, das
die schema.org- und LRMI-Felder nach Namen abfragt.

## Mit MCP

`site_structure` erfasst die Website, `batch_extract` extrahiert die Seiten, und `get_job` liest
beide Jobs seitenweise (`offset`, `limit` bis 50). `structured` gehört in `options`:

```json
{"urls": ["https://example.org/", "https://example.org/mathe/"], "options": {"metadata": true, "structured": {"strategy": "metadata", "schema": {"type": "object", "properties": {"license": {"type": "string"}}}}}}
```

Mit einem Prompt wie „https://example.org/ drei Links tief erfassen und für jede Seite die Lizenz
und die Bildungsstufe auflisten“ führt der Assistent diese Schritte aus. Siehe [MCP](mcp.md).
