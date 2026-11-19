# Installation

English: [Installation](../en/installation.md)

Der Dienst läuft auf drei Arten: mit Docker Compose (ein Rechner, der einfachste Weg), lokal mit
uv (ohne Docker-Images, für die Entwicklung) und auf Kubernetes mit dem Helm-Chart. Jede Art braucht
ein Valkey (oder Redis 7) für den gemeinsamen Zustand: Jobs, Ergebnisse, den Cache und die
Ratenlimits. Der Dienst selbst speichert keinen Zustand.

Jede Anfrage braucht einen API-Schlüssel des Dienstes (außer Sie schalten Schlüssel mit
`AUTH_MODE=none` ab). Docker Compose legt den ersten für Sie an;
[API-Schlüssel: anzeigen, erzeugen, eintragen](#api-schlüssel-anzeigen-erzeugen-eintragen) erklärt, wie Sie ihn sehen, weitere
erzeugen und wo sie hingehören, für jeden Weg und unter Windows.

Unter Windows läuft Docker Compose in [Docker Desktop](https://docs.docker.com/desktop/), in der
PowerShell oder der Eingabeaufforderung. Der lokale Weg ohne Docker-Images braucht Linux oder
macOS; unter Windows führen Sie ihn in [WSL 2](https://learn.microsoft.com/windows/wsl/install)
(Ubuntu) aus, weil die Dokument-Worker Prozessgrenzen von Linux nutzen.

## Lokal mit uv

Voraussetzung sind Python 3.13, [uv](https://docs.astral.sh/uv/) und ein Valkey auf
`localhost:6379`.

```bash
uv sync                                                     # install the dependencies into .venv
docker run -d --name valkey -p 6379:6379 valkey/valkey:9.1-alpine
export AUTH_MODE=none                                       # in each terminal below
UI_ENABLED=true uv run text-extraction                      # terminal 1: API on http://localhost:8080
INTERNAL_PORT=9091 uv run text-extraction-worker            # terminal 2: extract worker (jobs)
```

`uv run` liest keine `.env`: Jedes Terminal nimmt seine Einstellungen aus der Shell, setzen Sie
sie dort also mit `export` (oder schreiben Sie sie in eine Datei und führen Sie in jedem Terminal
`set -a; . ./my.env; set +a` aus). `AUTH_MODE=none` lässt jeden, der den Port erreicht, den Dienst nutzen: Verwenden Sie es nur auf
Ihrem eigenen Rechner und nur, solange Sie es brauchen. Er antwortet nur den Hostnamen aus
`ALLOWED_HOSTS` (`localhost`, `127.0.0.1`, `[::1]` und `api`), sodass eine Webseite, die ihre
eigene Domain auf Ihren Rechner zeigen lässt (DNS-Rebinding), abgewiesen wird. Jeder Prozess liest
dieselben Einstellungen, also braucht jedes Terminal `AUTH_MODE=none` (oder, ohne es,
dieselben `API_KEYS`), und jeder bedient seinen internen Port: Geben Sie jedem weiteren Prozess einen
eigenen `INTERNAL_PORT`. Der Browser-Worker braucht das Extra `browser` und Chromium
(`uv sync --extra browser`, dann `uv run playwright install chromium`); starten Sie ihn mit
`INTERNAL_PORT=9092 uv run text-extraction-browser-worker` und setzen Sie `BROWSER_ENABLED=true`
für die API, den Extract-Worker und den Browser-Worker.

Mit einem API-Schlüssel statt `AUTH_MODE=none` (etwa wenn andere Rechner den Port erreichen)
nehmen Sie `AUTH_MODE=none` zuerst wieder weg, sonst bleiben die Schlüssel unbeachtet:

```bash
uv run text-extraction-init --new-key me --role admin   # prints a key and an "export API_KEYS=..." line
unset AUTH_MODE                                         # in each terminal: keys are on again
export API_KEYS='[{"name":"me","role":"admin","sha256":"..."}]'  # the printed line, in each terminal
UI_ENABLED=true uv run text-extraction                  # log in to http://localhost:8080/ui with the key
```

Lassen Sie die einfachen Anführungszeichen um den Wert stehen: Sie bewahren die doppelten
Anführungszeichen des JSON vor der Shell.

Für die Entwicklung:

```bash
uv run pytest                # run the tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type-check (strict)
uv run pre-commit install    # install the git hooks
```

## Docker Compose

Voraussetzung ist Docker mit Compose. Compose startet die API, einen Extract-Worker, einen
Browser-Worker, die optionalen Worker (OCR, Transkription, personenbezogene Daten, LLM), ein
Valkey mit Passwort und SearXNG für die Websuche. Es schaltet den Browser-Modus, OCR, die
Transkription, die Behandlung personenbezogener Daten, das LLM, die Websuche, den MCP-Server und
die Spielwiese ein. Die Websuche braucht die Images von Release 0.1.13 oder neuer; wie man sie
ausschaltet, steht unter [Suche](search.md). Es gibt zwei Compose-Dateien:

- `docker-compose.yml` startet die veröffentlichten Images von
  `ghcr.io/janschachtschabel/text-extraction` (linux/amd64) und kommt ohne eine einzige Variable
  aus: für einen Server und für Docker-Oberflächen wie den Docker Manager von Hostinger.
- `docker-compose.build.yml` baut die Images aus Ihrem Checkout: für die Entwicklung.

### Veröffentlichte Images

```bash
cp .env.example .env                           # optional: the file starts without it
docker compose up -d --wait                    # public API on http://localhost:8080
docker compose logs init                       # the generated admin key (admin_key), shown once
KEY=$(docker compose run --rm -T init text-extraction-init --show-admin-key)
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

Beim ersten Start legt der einmalige Dienst `init` die Geheimnisse im Volume `secrets` an und
behält sie danach: das Valkey-Passwort und einen Admin-API-Schlüssel. Der Schlüssel steht einmal
im Log von `init`; der Befehl mit `--show-admin-key` oben gibt ihn jederzeit aus. Weitere Schlüssel
(für andere Personen oder mit der Rolle `user`) gehören in `API_KEYS` in `.env`; sie gelten
zusammen mit dem erzeugten: siehe [API-Schlüssel: anzeigen, erzeugen, eintragen](#api-schlüssel-anzeigen-erzeugen-eintragen).

In der Windows-PowerShell lauten dieselben Schritte:

```powershell
Copy-Item .env.example .env
docker compose up -d --wait
$KEY = docker compose run --rm -T init text-extraction-init --show-admin-key
Invoke-RestMethod -Headers @{ Authorization = "Bearer $KEY" } http://localhost:8080/v1/info
Invoke-RestMethod -Method Post -Headers @{ Authorization = "Bearer $KEY" } `
  -ContentType "application/json" -Body '{"url": "https://example.org/"}' `
  http://localhost:8080/v1/extract
```

Der interne Port 9090 (`/health`, `/ready`, `/metrics`) wird nicht veröffentlicht. Die Spielwiese
liegt unter http://localhost:8080/ui und der MCP-Endpunkt unter http://localhost:8080/mcp.

Die Datei folgt von selbst der neuesten Version `0.1.x`: Ihr Standard-Tag ist `0.1`, und jedes
`docker compose up` (auch das Neu-Bereitstellen in einem Panel) lädt es erneut, braucht also
ghcr.io; ein Container, den Docker nur neu startet, behält sein Image.
`TEXT_EXTRACTION_VERSION` in `.env` oder in der Shell hält eine Version fest, etwa `0.1.10`;
setzen Sie es nur, wenn Sie das wollen. Diese Datei braucht `0.1.8` oder neuer: Ältere Images
haben keinen Dienst `init`. Die Versionen und ihre Notizen stehen auf der
[Release-Seite](https://github.com/janschachtschabel/text-extraction/releases).

Für HTTPS trägt der Dienst `api` Labels für ein vorgeschaltetes Traefik (der VPS von Hostinger hat
eines): Setzen Sie `DOMAIN` in `.env` auf den reinen Hostnamen, dessen DNS auf den Server zeigt,
zum Beispiel `api.example.org`, ohne `https://`. `API_PORT=127.0.0.1:8080` hält Port 8080 dann
auf dem Server selbst. Die [Konfigurationsreferenz](../../configuration.md#root-compose-file-published-images)
(Englisch) beschreibt die Geheimnisse, die Traefik-Einstellungen und das Netz.

### Hostinger-VPS

Der Docker Manager eines Hostinger-VPS stellt die `docker-compose.yml` im Hauptordner direkt aus
dem Repository bereit. Planen Sie einen VPS mit 8 GB Arbeitsspeicher oder mehr: Mit allen Workern
braucht der Stack im Leerlauf etwa 6 GiB.

1. Docker Manager, Compose, „Compose from URL“: `https://github.com/janschachtschabel/text-extraction`
   eintragen, den vorgeschlagenen Namen (`text-extraction`) übernehmen und bereitstellen. Die erste
   Bereitstellung lädt die Images und die Modelle (etwa 3 GB Modelle) und dauert einige Minuten.
2. Den Admin-Schlüssel holen: in der Webkonsole (siehe unten)
   `cd /docker/text-extraction && docker compose run --rm -T init text-extraction-init --show-admin-key`
   ausführen, oder ihn in den Logs des Projekts beim Dienst `init` finden (Ereignis
   `init_admin_key_created`, Feld `admin_key`).
3. Das Projekt bearbeiten: `DOMAIN` in `.env` setzen (etwa den VPS-Hostnamen
   `srvNNNNNN.hstgr.cloud` oder Ihre eigene Domain mit einem A-Eintrag auf den VPS), wahlweise
   `API_PORT=127.0.0.1:8080`, dann erneut bereitstellen. Das Zertifikat holt Traefik selbst.
   Solange `DOMAIN` leer ist, zeigt das Traefik-Log einen Fehler zu einer leeren `Host()`-Regel:
   Der ist harmlos.
4. `https://<DOMAIN>/ui` öffnen und mit dem Schlüssel anmelden.
5. Wahlweise weitere Schlüssel: in der Webkonsole mit `--new-key` erzeugen und in den
   Umgebungsvariablen des Projekts eintragen, wie [Hostinger und andere Docker-Oberflächen](#hostinger-und-andere-docker-oberflächen)
   zeigt.

Eine fehlgeschlagene Bereitstellung zeigt nur „Bereitstellung fehlgeschlagen“ oder „Projekt nicht
gefunden“; die Webkonsole (eine Root-Shell) zeigt die eigentliche Meldung:

```bash
cat /docker/text-extraction/.build.log                 # the real error of a failed deploy
cd /docker/text-extraction && docker compose logs init  # the admin key
docker compose run --rm -T init text-extraction-init --show-admin-key
docker compose logs api | tail -n 20                   # why the API does not start
```

### Aus dem Checkout bauen

```bash
cp .env.example .env                           # then set VALKEY_PASSWORD and API_KEYS in it
openssl rand -hex 24                           # a VALKEY_PASSWORD
uv run text-extraction-init --new-key me --role admin   # a key and its API_KEYS line for .env
docker compose -f docker-compose.build.yml up --build -d --wait
```

Diese Datei hat keinen Dienst `init`: Sie braucht `API_KEYS` und `VALKEY_PASSWORD` in `.env`.
Ersetzen Sie die Zeile `API_KEYS` in `.env` durch die Zeile, die `--new-key` ausgegeben hat, und
behalten Sie den Schlüssel selbst für Ihre Anfragen und die Spielwiese. Ohne uv führen Sie den
Befehl im Image aus, sobald es gebaut und `VALKEY_PASSWORD` gesetzt ist (ohne es lädt die Datei
nicht):
`docker compose -f docker-compose.build.yml run --rm --no-deps api text-extraction-init --new-key me --role admin`.
Ein `VALKEY_PASSWORD` darf nur Buchstaben und Ziffern enthalten. Ohne `openssl` (unter Windows bringt
Git Bash es mit) erzeugt auch `python -c "import secrets; print(secrets.token_hex(24))"` eines.

### Optionale Worker

Die schwereren Teile laufen in eigenen Workern. Compose startet sie alle, und jeder hat einen
Schalter in `.env`, der standardmäßig `true` ist:

| Worker | Schalter in `.env` | Dienste | Was er hinzufügt |
| --- | --- | --- | --- |
| Docling | `DOCLING_ENABLED` | `docling-models`, `docling-worker` | OCR für Scans und Bilder, OpenDocument-Dateien (ein Image von etwa 2 GB, Modelle von etwa 335 MiB) |
| ASR | `ASR_ENABLED` | `asr-models`, `asr-worker` | Spracherkennung für Audio und Video (das Whisper-Modell, etwa 1,6 GB; `ASR_MODEL=small`, etwa 0,5 GB) |
| NLP | `PII_ENABLED` | `nlp-models`, `nlp-worker` | Behandlung personenbezogener Daten mit Presidio und spaCy (Modelle von etwa 1 GB für `lg`, 115 MB für `md`) |
| LLM | `LLM_ENABLED` | `llm-worker` | Strukturierte Ausgabe mit einem LLM, semantisches Chunking und Bildbeschreibungen |

Der erste Start lädt die Modelle in Volumes (zusammen etwa 3 GB); mit allen Workern braucht der
Stack im Leerlauf etwa 6 GiB Arbeitsspeicher. Um ohne einen Worker zu starten, setzen Sie seinen
Schalter in `.env` auf `false` und lassen seine Dienste weg, zum Beispiel
`docker compose up -d --wait --scale docling-models=0 --scale docling-worker=0` (für die
Build-Datei mit `-f docker-compose.build.yml`). Eine `.env`, die
aus einer älteren `.env.example` kopiert ist, hat die Schalter noch auf `false`: Setzen Sie sie auf `true`.

Der LLM-Worker braucht `LLM_BASE_URL` und `LLM_MODEL` eines OpenAI-kompatiblen Endpunkts und einen
Schlüssel: `LLM_API_KEY` in `.env` oder den `X-LLM-Api-Key` des Aufrufers (das Schlüsselfeld der
Spielwiese); ohne beides antwortet eine Anfrage, die LLM-Arbeit ausdrücklich verlangt, mit 400, und
die Strategie `auto` läuft ohne das LLM.
`LLM_EMBEDDING_MODEL` ist für semantisches Chunking;
`docker compose run --rm llm-worker text-extraction-llm-worker --check` sendet dem Endpunkt eine
winzige Extraktion. Für `pii: hash` mit demselben Hash in jedem Ergebnis setzen Sie
`PII_HASH_KEY` (`openssl rand -hex 32`).

Solange ein Schalter aus ist, antwortet eine Anfrage, die seinen Worker braucht, mit 400: `pii`
ohne `PII_ENABLED`, die Strategie `llm`, `learn_selectors` und `semantic`-Chunking ohne
`LLM_ENABLED`, `describe_images` ohne `LLM_ENABLED`, `transcribe` ohne `ASR_ENABLED` und
`ocr: always` ohne `DOCLING_ENABLED`. Mit `ocr: auto` (der Vorgabe) liefert ein Scan ohne
`DOCLING_ENABLED` nur seine Textebene, mit der Warnung `ocr_unavailable`.

Um eine Funktion Admin-Schlüsseln vorzubehalten, setzen Sie ihren zweiten Schalter auf `false`:
`OCR_ALLOW_USERS`, `ASR_ALLOW_USERS`, `PII_ALLOW_USERS` oder `LLM_ALLOW_USERS` (alle standardmäßig
`true`). Ein Nutzer-Schlüssel, der die Funktion anfordert, bekommt dann 403 `forbidden`:
`ocr: always`, `transcribe` oder das Hochladen von Audio oder Video, `pii` sowie die Strategie
`llm`, `learn_selectors`, `semantic`-Chunking oder `describe_images`. Wo ein Nutzer-Schlüssel sie
nicht ausdrücklich anfordert, läuft die Anfrage ohne die Funktion: Ein Scan bekommt kein OCR
(`ocr_unavailable`), und die Strategie `auto` fragt für die noch fehlenden Felder kein LLM (Warnung `llm_unavailable`).
Die Spielwiese zeigt eine solche Auswahl deaktiviert, mit einem Hinweis. Admin-Schlüssel dürfen
jede Funktion nutzen, die der Dienst hat.

Die maximale Extraktion (für Websites, die die Extraktion vertraglich erlaubt haben) braucht
`BROWSER_UNBLOCK_ENABLED=true`. Das setzen docker compose und `.env.example` seit 0.1.9 (ein Admin
muss weiterhin `compliance: ignore` wählen); `false` in `.env` schaltet es aus. Siehe
[Website-Regeln](site-rules.md#maximale-extraktion).

## API-Schlüssel: anzeigen, erzeugen, eintragen

Der Dienst prüft jede Anfrage gegen eine Liste von Schlüsseln. Für jeden Schlüssel enthält die
Liste einen Namen, eine Rolle und den SHA-256 des Schlüssels, nie den Schlüssel selbst: Es gibt
also immer zwei Teile.

- Der Schlüssel: 64 Buchstaben und Ziffern. Sie behalten ihn, senden ihn mit jeder Anfrage als
  `Authorization: Bearer <key>` und melden sich damit an der Spielwiese an. Aus der Liste lässt er
  sich nicht zurückgewinnen: Ist er verloren, erzeugen Sie einen neuen.
- Sein Eintrag in `API_KEYS`: `[{"name":"jan","role":"user","sha256":"…"}]`, eine JSON-Liste
  in den Einstellungen. Mehrere Schlüssel teilen sich eine Liste: `[{…},{…}]`.

Die Rolle ist `user` oder `admin`: Ein Admin darf außerdem die Website-Regeln abschalten (und damit
die maximale Extraktion nutzen), XPath-Selektoren und die Aktion `evaluate` verwenden und jeden Job
lesen. [API-Schlüssel](usage.md#api-schlüssel) in der Anleitung zur Nutzung beschreibt die Rollen
und die Fehlerantworten.

### Den erzeugten Admin-Schlüssel anzeigen

Die `docker-compose.yml` im Hauptordner legt beim ersten Start einen Admin-Schlüssel namens `admin`
an und bewahrt ihn im Volume `secrets` auf. Dieser Befehl gibt ihn jederzeit aus, gleich in bash,
PowerShell und der Webkonsole von Hostinger (dort zuerst `cd /docker/text-extraction`):

```bash
docker compose run --rm -T init text-extraction-init --show-admin-key
```

Für die meisten Einrichtungen mit einer Person genügt dieser Schlüssel. `docker compose down -v`
löscht die Volumes und mit ihnen diesen Schlüssel (und die geladenen Modelle); der nächste Start
legt einen neuen an.

### Einen weiteren Schlüssel erzeugen

`--new-key` gibt einen neuen Schlüssel und seinen Eintrag zum Einfügen aus; von sich aus ändert es
nichts. Geben Sie jedem Schlüssel einen eigenen Namen (Buchstaben, Ziffern, `.`, `_` oder `-`;
`admin` gehört dem erzeugten Schlüssel) und die Rolle, die er braucht, standardmäßig `user`:

```bash
docker compose run --rm -T init text-extraction-init --new-key anna                 # a user key
docker compose run --rm -T init text-extraction-init --new-key jan --role admin     # an admin key
uv run text-extraction-init --new-key anna                                          # without Docker
```

Die Antwort hat vier Teile (hier gekürzt; Ihr Schlüssel und Hash sehen anders aus):

```text
Key (send it as 'Authorization: Bearer <key>' or enter it in the playground; it is shown only now):
  9f2c…
.env line (docker compose; replace the API_KEYS line, then run docker compose up -d):
  API_KEYS=[{"name":"anna","role":"user","sha256":"4be1…"}]
Shell (uv run without Docker; run it in each terminal):
  export API_KEYS='[{"name":"anna","role":"user","sha256":"4be1…"}]'
Docker panel variable (such as Hostinger): name API_KEYS, value:
  [{"name":"anna","role":"user","sha256":"4be1…"}]
More than one key: put every entry, comma-separated, into one list: [{...},{...}]
```

Legen Sie den Schlüssel jetzt sicher ab: Er wird nirgends gespeichert. `--new-key` braucht Version
`0.1.13` oder neuer; ohne den Befehl (ältere Images) zeigt [API-Schlüssel](usage.md#api-schlüssel),
wie Sie einen Schlüssel mit `openssl` erzeugen.

### Den Schlüssel eintragen

| Wo der Dienst läuft | Was zu tun ist |
| --- | --- |
| `docker compose` (beide Dateien) | `.env` in einem Texteditor öffnen, die ganze Zeile, die mit `API_KEYS=` beginnt, durch die ausgegebene `.env`-Zeile ersetzen, speichern, `docker compose up -d --wait` ausführen (mit `-f docker-compose.build.yml` für die Build-Datei). |
| Hostinger und andere Docker-Oberflächen | Siehe [unten](#hostinger-und-andere-docker-oberflächen). |
| `uv run` ohne Docker | Die ausgegebene Zeile `export API_KEYS=…` in jedem Terminal ausführen, dann die Prozesse neu starten. |
| Kubernetes | Der Wert gehört in `secrets.apiKeys` (oder Ihr eigenes Secret), siehe [Helm-Chart](../../../deploy/helm/text-extraction/README.md) (Englisch). |

Für mehrere Schlüssel enthält die Zeile eine Liste mit allen Einträgen, zum Beispiel
`API_KEYS=[{"name":"anna","role":"user","sha256":"…"},{"name":"jan","role":"admin","sha256":"…"}]`.
Schreiben Sie sie in eine Zeile und ohne Anführungszeichen um die Liste. Der erzeugte Schlüssel
`admin` steht nicht in dieser Liste (er kommt aus dem Volume) und gilt weiter. Um einen Schlüssel zu
entziehen, entfernen Sie seinen Eintrag und führen `docker compose up -d --wait` erneut aus.

Compose gibt `.env` nur beim Anlegen der Container weiter: `docker compose up -d` tut das nach
einer Änderung, `docker compose restart` behält dagegen die alten Werte.

### Hostinger und andere Docker-Oberflächen

1. Den Schlüssel in der Webkonsole des VPS (einer Root-Shell) erzeugen:
   `cd /docker/text-extraction && docker compose run --rm -T init text-extraction-init --new-key anna`.
2. Docker Manager, das Projekt, „Bearbeiten“ (Umgebungsvariablen): eine Variable hinzufügen.
   - Name: `API_KEYS`
   - Wert: die Zeile unter „Docker panel variable“, die mit `[` beginnt. Nicht die
     `.env`-Zeile: `API_KEYS=` im Feld „Wert“ lässt den Namen doppelt erscheinen, und die API und
     die Worker brechen mit „API_KEYS must be a JSON list“ ab.
3. Erneut bereitstellen. Die Variablen landen in `.env`, und die Container starten mit der neuen
   Liste.

Gibt es schon eine Variable `API_KEYS` (aus `.env.example`, mit Nullen als Hash), ersetzen Sie
ihren Wert, statt eine zweite anzulegen.

### Den Schlüssel prüfen

```bash
curl -H "Authorization: Bearer <key>" http://localhost:8080/v1/info
```

```powershell
Invoke-RestMethod -Headers @{ Authorization = "Bearer <key>" } http://localhost:8080/v1/info
```

Die Antwort nennt die `role` des Schlüssels. In Windows PowerShell 5 ist `curl` ein anderer
Befehl: Nehmen Sie `curl.exe` oder `Invoke-RestMethod`. Hinter einer Domain nehmen Sie
`https://<DOMAIN>` statt `http://localhost:8080`.

### Wenn es nicht klappt

| Was Sie sehen | Ursache und Abhilfe |
| --- | --- |
| 401 `unauthorized` | Der Schlüssel passt zu keinem Eintrag: erneut kopieren, ohne Leerzeichen. Nach einer Änderung von `.env` `docker compose up -d --wait` ausführen (ein Neustart behält die alte Liste). |
| Die API startet nicht, ihr Log sagt `API_KEYS must be a JSON list` | Der Wert ist keine reine Liste: Er beginnt mit `API_KEYS=` (Feld „Wert“ einer Oberfläche), steht in einem Feld in Anführungszeichen oder geht über mehrere Zeilen. |
| Das Log sagt `duplicate API key name` | Zwei Einträge haben denselben Namen, oder einer heißt `admin` neben dem erzeugten Schlüssel: umbenennen (bei `--new-key` einen anderen Namen wählen). |
| Das Log sagt `AUTH_MODE=api_key requires API_KEYS or API_KEYS_FILE` | Es gibt gar keinen Schlüssel: einen eintragen, oder die `docker-compose.yml` im Hauptordner nehmen, die einen anlegt. |
| Der Schlüssel ist verloren | Einen eigenen Schlüssel kann niemand zurückholen: einen neuen erzeugen und seinen Eintrag ersetzen. Den erzeugten Admin-Schlüssel: `--show-admin-key`. |

## Kubernetes mit Helm

Das [Helm-Chart](../../../deploy/helm/text-extraction/README.md) (Englisch) installiert die API und
die Worker, mit einem eigenen Valkey oder Ihrem; jeder Worker, der MCP-Server und die Spielwiese
sind Schalter in seinen Values. Seine README listet die Values und die Secrets.

## Wichtige Einstellungen

Jede Einstellung ist eine Umgebungsvariable mit einem Standardwert. Dies sind die, die Nutzer und
Betreiber am häufigsten anfassen; die
[Konfigurationsreferenz](../../configuration.md#environment-variables) (Englisch) listet alle, mit
den [Secrets](../../configuration.md#secrets).

| Einstellung | Standard | Was sie bewirkt |
| --- | --- | --- |
| `AUTH_MODE` | `api_key` | `api_key`: Jede Anfrage braucht einen Schlüssel. `none`: keine Schlüssel, jeder Aufrufer ist Admin. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],api` | Mit `AUTH_MODE=none`: die Hostnamen, denen der Dienst antwortet; `*` antwortet jedem. |
| `API_KEYS` | leer | Secret. Die Schlüssel als JSON-Liste aus `name`, `role` (`user` oder `admin`) und `sha256`. |
| `API_KEYS_FILE` | nicht gesetzt | Eine JSON-Datei derselben Form, zusammengeführt mit `API_KEYS`. |
| `VALKEY_URL` | `redis://localhost:6379/0` | Die URL des Valkey für den gemeinsamen Zustand; ein Secret, wenn sie ein Passwort enthält. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` oder `CRITICAL`. |
| `LOG_FORMAT` | `json` | `json` für den Betrieb, `console` für die lokale Entwicklung. |
| `RATE_LIMIT_PER_MINUTE` | `60` | Anfragen pro Minute und Schlüssel; darüber antwortet eine Anfrage mit 429. |
| `COMPLIANCE_DEFAULT` | `enforce` | Die Website-Regeln, wenn eine Anfrage keine nennt: `enforce`, `report` oder `ignore`. |
| `FETCH_TIMEOUT_SECONDS` | `20` | Zeit für einen Abruf einer Seite. |
| `FETCH_MAX_BYTES` | `20000000` | Höchstens so viele Bytes einer Seite oder Datei. |
| `OUTBOUND_MAX_CONCURRENCY_PER_HOST` | `2` | Höchstens so viele gleichzeitige Anfragen an einen Host. |
| `OUTBOUND_MIN_DELAY_MS` | `500` | Mindestabstand zwischen zwei Anfragen an einen Host. |
| `OUTBOUND_BACKOFF_SECONDS` | `30` | Die Pause nach einem 429 oder 503 eines Hosts ohne `Retry-After`. |
| `JOBS_ENABLED` | `true` | Jobs: Batches, Website-Strukturen, `wait_seconds` und Screenshots. |
| `JOB_TTL_SECONDS` | `86400` | Wie lange ein Job und seine Ergebnisse aufbewahrt werden. |
| `BATCH_MAX_URLS` | `1000` | Höchstens so viele URLs in einem Batch. |
| `BATCH_MAX_OPEN_PER_CLIENT` | `5` | Höchstens so viele offene Batches und Website-Strukturen eines Schlüssels. |
| `BROWSER_ENABLED` | `false` | Browser-Modus: Seiten, die JavaScript brauchen, werden in Chromium gerendert. |
| `BROWSER_QUEUE_WAIT_SECONDS` | `10` | Wie lange eine Anfrage darauf wartet, dass ein Browser-Worker ihre Seite übernimmt. |
| `BROWSER_UNBLOCK_ENABLED` | `false` | Erlaubt Admins die maximale Extraktion (Entsperrmodus). |
| `DOCUMENT_MAX_BYTES` | `20000000` | Höchstens so viele Bytes eines hochgeladenen Dokuments. |
| `DOCLING_ENABLED` | `false` | OCR und OpenDocument-Dateien im Docling-Worker. |
| `ASR_ENABLED` | `false` | Spracherkennung im ASR-Worker. |
| `PII_ENABLED` | `false` | Behandlung personenbezogener Daten im NLP-Worker. |
| `LLM_ENABLED` | `false` | LLM-Arbeit im LLM-Worker. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Der OpenAI-kompatible Endpunkt. |
| `LLM_API_KEY` | nicht gesetzt | Secret. Der Schlüssel des Betreibers; ein `X-LLM-Api-Key` des Aufrufers hat Vorrang. |
| `SITE_MAX_PAGES` | `1000` | Höchstens so viele Seiten darf eine Website-Struktur verlangen. |
| `MCP_ENABLED` | `false` | Der MCP-Server unter `/mcp`. |
| `UI_ENABLED` | `false` | Die Spielwiese unter `/ui`. |
| `UI_DEFAULT_LANGUAGE` | `en` | Die Sprache der Spielwiese, wenn der Browser weder `en` noch `de` verlangt. |
| `UI_DOCS_BASE_URL` | `https://github.com/janschachtschabel/text-extraction/blob/main/docs/user-guide` | Die Anleitung, die die Hilfe-Links der Spielwiese öffnen; für einen Fork oder eine Installation ohne Internet lassen Sie die Einstellung auf eine eigene Kopie zeigen. |
