# Suche

English: [Search](../en/search.md)

Durchsuchen Sie das Web nach Seiten und extrahieren Sie dann die Treffer, die Sie brauchen. Der
Dienst fragt sein eigenes SearXNG, einen Suchdienst, der als eigener Container neben ihm läuft.
SearXNG gibt die Anfrage an einige freie Quellen weiter und liefert ihre Treffer gemeinsam
zurück. Die Suche selbst holt keine Seite. Die Treffer, die Sie auswählen,
senden Sie an eine Extraktion: einen in der Spielwiese, über `POST /v1/extract` oder mit
`extract_url`, mehrere als Batch. Diese Seite beschreibt die Quellen, die Spielwiese, REST, MCP,
die Fehler und was ein Betreiber einrichtet.

Die Suche gibt es, wenn der Betreiber sie eingeschaltet hat (`SEARCH_ENABLED`). Die
`docker-compose.yml` im Hauptordner tut das von sich aus. Im Helm-Chart ist sie aus.
`GET /v1/info` sagt, ob Ihr Schlüssel suchen darf (`features.search`) und wie viele Treffer eine
Suche höchstens liefert (`limits.search_max_results`).

## Die Quellen

Jede Quelle ist kostenlos. Alle außer Bing, DuckDuckGo, Mojeek und Startpage haben eine offizielle API. Jeder Treffer
nennt die Quellen, die ihn gefunden haben.

| Quelle | Was sie findet | Grenzen |
| --- | --- | --- |
| `wikipedia` | Den Wikipedia-Artikel zu den Suchbegriffen, aus der Wikipedia der Sprache `language` | Höchstens ein Treffer pro Suche: Die Quelle fragt nach dem Artikel, der am besten zu den Begriffen passt, nicht nach einer Liste von Artikeln. |
| `wikibooks` | Lehrbücher und ihre Kapitel auf Wikibooks | Bücher nur zu manchen Themen. |
| `wikiversity` | Lernmaterial und Kursseiten auf Wikiversity | Klein, ungleichmäßig abgedeckt. |
| `wikinews` | Nachrichtenartikel auf Wikinews | Klein; wenige Themen. |
| `mwmbl` | Allgemeine Webseiten aus Mwmbl, einer freien, gemeinnützigen Suchmaschine mit eigenem Index | Kleiner Index, vor allem englische Seiten; deutsche Bildungsseiten fehlen oft. Ohne Schlüssel erlaubt Mwmbl etwa 1.000 Anfragen im Monat von einer IP-Adresse; der Cache schont dieses Kontingent. |
| `openalex` | Wissenschaftliche Werke (Artikel, Bücher, Datensätze) aus OpenAlex | Nur Wissenschaft; Titel oft englisch. |
| `arxiv` | Preprints aus arXiv (Physik, Mathematik, Informatik und mehr) | Nur Wissenschaft, fast alles englisch. |
| `crossref` | Wissenschaftliche Werke mit DOI aus Crossref | Nur Wissenschaft; Metadaten, oft ohne Zusammenfassung. |
| `bing` | Allgemeine Webseiten von Bing, in der Sprache von `language` | Keine offizielle API: SearXNG liest die Ergebnisseiten von Bing aus (Scraping). Eine Suchmaschine kann die Adresse eines Servers nach einiger Zeit sperren; die Quelle steht dann in `sources_failed`, und die Antwort ist `partial`. |
| `duckduckgo` | Allgemeine Webseiten von DuckDuckGo | Scraping wie bei `bing`, mit derselben Gefahr einer Sperre. |
| `mojeek` | Allgemeine Webseiten von Mojeek, einer Suchmaschine mit eigenem Index | Scraping wie bei `bing`, mit derselben Gefahr einer Sperre; ein kleinerer Index als der von Bing. |
| `startpage` | Allgemeine Webseiten von Startpage, das die Ergebnisse von Google weitergibt | Scraping wie bei `bing`, mit derselben Gefahr einer Sperre. |

Deutsche Ergebnisse kommen also von Bing, DuckDuckGo, Mojeek und Startpage, Wikipedia-Wissen aus
den Wikimedia-Quellen und Wissenschaft aus OpenAlex, arXiv und Crossref. Google und Brave bleiben
aus: Google hat einen Server schon am ersten Tag gesperrt. Ein Betreiber kann die vier per Scraping
gelesenen Suchmaschinen ausschalten oder eine weitere einschalten (siehe
[Weitere Quellen](#weitere-quellen)).

Werden mehrere Quellen gefragt, wechseln sich die Treffer ab: zuerst der beste Treffer jeder
Quelle, dann der zweite jeder Quelle und so weiter. Eine Seite, die mehrere Quellen gefunden haben
(auch als `http` und `https`, mit oder ohne `www.`, Schrägstrich am Ende oder `utm_`-Parameter),
steht nur einmal da, an ihrem besten Platz, mit allen ihren Quellen.

## In der Spielwiese

Der Tab **Suche** erscheint nur, wenn Ihr Schlüssel suchen darf. Er steht vor dem Tab **Info**.
Ist die Suche Admin-Schlüsseln vorbehalten (`SEARCH_ALLOW_USERS=false`) und sind Sie mit einem
Nutzer-Schlüssel angemeldet, fehlt der Tab, und der Tab **Info** sagt, warum.

1. Geben Sie die „Suchbegriffe“ ein (höchstens 500 Zeichen).
2. Optional: Kreuzen Sie die „Quellen“ an, die gefragt werden. Ohne Kreuz werden alle Quellen
   gefragt.
3. Optional: „Sprache“ ist „Alle Sprachen“, „Deutsch“ oder „Englisch“. Vorbelegt ist die Sprache
   der Seite, auf der deutschen Seite fragt die Suche also nach deutschen Treffern. „Treffer“ ist die Zahl der angezeigten Treffer (10 als Standard, höchstens
   `SEARCH_MAX_RESULTS` des Dienstes). „Zeitraum“ behält Treffer aus dem letzten Tag, Monat oder
   Jahr, wo eine Quelle das unterstützt. „Ergebnisseite“ 2 zeigt die nächsten Treffer.
4. Klicken Sie auf „Suchen“.

Das Ergebnis beginnt mit einer Statuszeile und der Zahl der Treffer. Ein Hinweis nennt die
Quellen, die nicht geantwortet haben, falls es welche gibt, und „Vorschläge:“ listet andere
Suchbegriffe, die SearXNG vorschlägt. Jeder Treffer zeigt seinen Titel als Link (er öffnet sich in
einem neuen Tab), seine URL, einen Ausschnitt, „Gefunden von“ mit seinen Quellen und, wo eine
Quelle eines hat, sein Datum („Veröffentlicht“).

Um Treffer zu extrahieren, kreuzen Sie sie an (oder klicken Sie auf „Alle auswählen“; „Keine
auswählen“ entfernt jedes Kreuz), wählen Sie unter der Liste das Format und die Optionen, und
klicken Sie auf „Auswahl extrahieren“. Die angekreuzten URLs werden ein Batch, der unter den
Treffern mit seinem Fortschritt erscheint, wie im Website-Baum (siehe
[Ein Teil einer Website](part-of-a-site.md#in-der-spielwiese)). Ist nichts angekreuzt, sagt das
Formular „Geben Sie mindestens eine URL an.“ und startet nichts. „Als curl“ und „Rohes JSON“ am
Ende zeigen die Suchanfrage und ihre ganze Antwort.

## Mit REST

`POST /v1/search` nimmt die Suchbegriffe und liefert die Treffer im Ergebnis-Envelope. Jedes Feld
steht in [Parameter](parameters.md#post-v1search).

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

Jeder Treffer in `data.results` hat seine `url` (immer http oder https; andere Links von SearXNG
fallen weg), seinen `title`, einen `snippet` (oder null), die `sources`, die ihn gefunden haben,
`published` (ein Datum, wo die Quelle eines hat) und SearXNGs `score`. `title` und `snippet` sind
reiner Text: Der Dienst entfernt die Auszeichnung, die manche Quellen mitliefern (die JATS-Tags von
Crossref und OpenAlex, die Hervorhebungen von Wikipedia). `data.suggestions` listet andere
Suchbegriffe.

Die Treffer sind nach ihrem Platz in der eigenen Liste jeder Quelle geordnet: Zuerst kommt der beste
Treffer jeder Quelle, dann der zweite jeder Quelle und so weiter. Eine Seite, die mehrere Quellen
gefunden haben, ist ein Treffer an ihrem besten Platz, dessen `sources` alle nennt; als dieselbe
Seite gilt eine Adresse ohne Rücksicht auf `http` oder `https`, ein führendes `www.`, einen
Schrägstrich am Ende, den Teil nach `#` oder `utm_`-Parameter. `max_results` kürzt die Liste nach
dieser Ordnung.

Haben manche Quellen nicht geantwortet, ist die Antwort `partial`, und `data.sources_failed` nennt
jede mit dem Grund von SearXNG (etwa eine Zeitüberschreitung oder eine abgelehnte Verbindung). Die
Treffer kommen dann von den übrigen Quellen; `data.results` kann auch leer sein, wenn die übrigen
nichts gefunden haben. Eine Quelle, die ohne Treffer geantwortet hat, ist kein Ausfall. Nur wenn
keine der gefragten Quellen geantwortet hat, ist die Suche `failed`, und `error.details` listet jede
Quelle mit ihrem Grund.

Der Dienst behält eine `success`-Antwort für `SEARCH_CACHE_SECONDS` (standardmäßig eine Stunde) in
seinem Cache. Dieselben Suchbegriffe mit derselben Sprache, Seite, demselben Zeitraum, denselben
Quellen und demselben `max_results` kommen dann aus dem Cache (`data.cache: "hit"`). Eine
`partial`-Antwort wird nicht behalten, die nächste Suche fragt also neu. Eine Suche zählt einmal
gegen die Ratenbegrenzung Ihres Schlüssels.

Um die Seiten zu lesen, senden Sie die URLs an eine Extraktion, zum Beispiel als Batch:

<!-- request: POST /v1/batch -->
```json
{"urls": ["https://de.wikipedia.org/wiki/Photosynthese", "https://de.wikipedia.org/wiki/Chlorophyll"], "format": "markdown"}
```

Wie man einen Batch liest, steht in [Ein Teil einer Website](part-of-a-site.md#die-seiten-extrahieren).

## Mit MCP

Das Tool `web_search` nimmt die Felder von `POST /v1/search` als Argumente (`query` ist Pflicht)
und antwortet mit demselben Envelope. Es hat keine `options`. Seine Beschreibung nennt die Quellen
und die höchste Trefferzahl des Dienstes, damit ein Assistent sie nicht raten muss. Danach liest
`extract_url` einen Treffer und `batch_extract` mehrere. Ein Assistent verkettet sie von selbst:

```json
{"name": "web_search", "arguments": {"query": "Photosynthese", "language": "de", "max_results": 5}}
{"name": "batch_extract", "arguments": {"urls": ["https://de.wikipedia.org/wiki/Photosynthese"]}}
```

Das Tool ist auch dann gelistet, wenn die Suche aus ist; ein Aufruf liefert dann die 400 der
Route. Die Einrichtung und die anderen Tools beschreibt [MCP](mcp.md).

## Fehler

| Antwort | Bedeutung |
| --- | --- |
| 200, `status: partial` | Manche Quellen haben nicht geantwortet; `data.sources_failed` nennt sie. Die Treffer der übrigen sind da; keine Treffer heißt, die übrigen haben nichts gefunden. |
| 200, `status: failed`, `search_failed`, `retryable: true` | SearXNG ist nicht erreichbar oder hat nicht innerhalb von `SEARCH_TIMEOUT_SECONDS` (standardmäßig 10 s) geantwortet, oder keine der gefragten Quellen hat geantwortet (`error.details` listet jede mit ihrem Grund). Versuchen Sie es später noch einmal. |
| 200, `status: failed`, `search_failed`, `retryable: false` | SearXNG hat die Anfrage abgelehnt, weil sein JSON-Format aus ist: eine Einstellung des Betreibers (`search.formats` in SearXNGs `settings.yml`). |
| 200, `status: success`, keine Treffer | Alle Quellen haben geantwortet, und keine hatte einen Treffer. Versuchen Sie andere Wörter, eine andere Sprache oder mehr Quellen. |
| 400 `invalid_request` | Die Suche ist in diesem Dienst aus (`SEARCH_ENABLED=false`). |
| 403 `forbidden` | Die Suche ist Admin-Schlüsseln vorbehalten (`SEARCH_ALLOW_USERS=false`), und Ihr Schlüssel ist ein Nutzer-Schlüssel. |
| 422 `invalid_request` | Der Body ist falsch: keine `query`, `max_results` über `SEARCH_MAX_RESULTS`, eine `language`, die kein Sprachcode ist (etwa `German` statt `de`), eine Quelle außerhalb von `SEARCH_SOURCES` (die Meldung listet die erlaubten), ein leerer Name oder `sources: []` (lassen Sie `sources` weg, um alle Quellen zu fragen). |
| 429 `rate_limited` | Die Ratenbegrenzung Ihres Schlüssels ist aufgebraucht. |

## Für Betreiber

### Ein- und ausschalten

Die `docker-compose.yml` im Hauptordner startet den Dienst `searxng` und schaltet die Suche ein.
`SEARCH_ENABLED=false` in `.env` schaltet sie für die API aus; um auch den Container zu sparen,
starten Sie den Stack mit `--scale searxng=0`. `docker-compose.build.yml` tut dasselbe.

Im Helm-Chart ist beides aus. `searxng.enabled=true` startet SearXNG im Release und richtet
`SEARCH_URL` darauf; `config.SEARCH_ENABLED: "true"` schaltet die Suche ein. Ohne
`searxng.enabled` setzen Sie `config.SEARCH_URL` auf ein eigenes SearXNG. Siehe das
[Helm-Chart](../../../deploy/helm/text-extraction/README.md#web-search-searxng) (Englisch).

Die übrigen Einstellungen (`SEARCH_TIMEOUT_SECONDS`, `SEARCH_MAX_RESULTS`, `SEARCH_CACHE_SECONDS`,
`SEARCH_SOURCES`, `SEARCH_ALLOW_USERS`) stehen in der
[Konfigurationsreferenz](../../configuration.md#web-search) (Englisch). Lassen Sie
`SEARCH_TIMEOUT_SECONDS` über 8: SearXNG bricht jede Quelle nach 8 s ab, mit einem kleineren Wert
lässt also eine einzige langsame Quelle die ganze Suche scheitern, statt in
`data.sources_failed` zu stehen.

SearXNG läuft als unveränderter, eigener Container und hat keinen veröffentlichten Port: Nur die
API erreicht es. Seine Lizenz ist die AGPL-3.0; der Dienst importiert nie seinen Code.

### Hostinger und die Reihenfolge der Releases

Die `docker-compose.yml` im Hauptordner gibt SearXNG ein eigenes Volume, `searxng-config`, sodass
es die anderen Geheimnisse nie sieht. Der Dienst `init` schreibt die Einstellungen von SearXNG bei
jedem Start in dieses Volume, mit einem Schlüssel, den er einmal erzeugt und behält. Dieses `init`
kommt mit Release 0.1.13. Die Compose-Datei folgt dem neuesten 0.1.x-Release, spielen Sie die neue
Compose-Datei also erst ein, wenn 0.1.13 veröffentlicht ist: Mit einem älteren Image bekommt
SearXNG keine Einstellungen und startet nicht. Zeigt `docker compose logs init` die Meldung
`init_searxng_settings_failed`, löschen Sie das Volume `searxng-config` (es enthält nur diese
Einstellungen) und spielen Sie neu ein. SearXNG fragt von der
eigenen Adresse des VPS aus. Die Quellen mit offizieller API sperren keinen Server; Mwmbl zählt
Anfragen pro Adresse. Bing, DuckDuckGo, Mojeek und Startpage können die Adresse des VPS nach einiger Zeit sperren: Sie
stehen dann in `sources_failed`, und die übrigen Quellen antworten weiter. Eine solche Antwort ist
`partial` und kommt nicht in den Cache, solange die Sperre dauert, fragt also jede Suche wieder
alle Quellen; lassen Sie eine gesperrte Suchmaschine aus `SEARCH_SOURCES` weg, wenn das lange anhält (siehe
[Weitere Quellen](#weitere-quellen)).

### Weitere Quellen

Um die per Scraping gelesenen Suchmaschinen auszuschalten, setzen Sie `SEARCH_SOURCES` ohne sie (etwa
`wikipedia,wikibooks,wikiversity,wikinews,mwmbl,openalex,arxiv,crossref`): Der Dienst fragt sie
dann nie. Eine Quelle außerhalb von `SEARCH_SOURCES` wird mit 422 abgelehnt.

So schalten Sie eine weitere Suchmaschine von SearXNG ein (etwa `google` oder `brave`), auf
eigene Verantwortung:

1. Tragen Sie ihren Namen in `use_default_settings.engines.keep_only` und in die Liste `engines:`
   (mit `disabled: false`, und `inactive: false` bei einer Suchmaschine, die SearXNG nicht von
   sich aus lädt) der `settings.yml` von SearXNG ein.
2. Tragen Sie ihren Namen in `SEARCH_SOURCES` ein, das die Suchmaschinen nennt, die der Dienst
   fragen darf.

Woher die Einstellungen kommen, hängt von der Installation ab:

- `docker-compose.yml` im Hauptordner: `init` schreibt die Einstellungen bei jedem Start neu, aus
  der Kopie im Image des Dienstes (`src/text_extraction/data/searxng-settings.yml`); eine Änderung im
  Volume geht also beim nächsten Start verloren. Betreiben Sie ein eigenes SearXNG mit Ihren
  Einstellungen und setzen Sie `SEARCH_URL` darauf (und lassen Sie das des Stacks mit
  `--scale searxng=0` weg), oder binden Sie, wo Bind-Mounts erlaubt sind (nicht bei Hostinger),
  in einer Compose-Override-Datei eine eigene `settings.yml` unter `/etc/searxng/settings.yml` des
  Dienstes `searxng` ein, mit einem eigenen `secret_key`.
- `docker-compose.build.yml`: Es bindet `src/text_extraction/data/searxng-settings.yml` aus dem
  Checkout ein. Ändern Sie diese Datei und starten Sie `searxng` neu.
- Helm: Die ConfigMap enthält die `files/searxng-settings.yml` des Charts. Ändern Sie sie in einer
  Kopie des Charts, oder setzen Sie `searxng.enabled=false` und `config.SEARCH_URL` auf ein
  eigenes SearXNG.

Scraping von Suchmaschinen führt zur Sperre der Server-Adresse und verstößt gegen ihre
Nutzungsbedingungen. Das Projekt lässt Bing, DuckDuckGo, Mojeek und Startpage standardmäßig an, weil deutsche
Web-Ergebnisse sie brauchen (Entscheidungen D6 und D7 in
[architecture.md](../../architecture.md#web-search), Englisch); jede andere Scraping-Suchmaschine
bleibt aus.
