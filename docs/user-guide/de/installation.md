# Installation

English: [Installation](../en/installation.md)

Der Dienst läuft auf drei Arten: lokal mit uv (für die Entwicklung), mit Docker Compose (ein
Rechner) und auf Kubernetes mit dem Helm-Chart. Jede Art braucht ein Valkey (oder Redis 7) für den
gemeinsamen Zustand: Jobs, Ergebnisse, den Cache und die Ratenlimits. Der Dienst selbst speichert
keinen Zustand.

## Lokal mit uv

Voraussetzung sind Python 3.13, [uv](https://docs.astral.sh/uv/) und ein Valkey auf
`localhost:6379`.

```bash
uv sync                                                     # install the dependencies into .venv
docker run -d --name valkey -p 6379:6379 valkey/valkey:8.1-alpine
AUTH_MODE=none UI_ENABLED=true uv run text-extraction       # API on http://localhost:8080
uv run text-extraction-worker                               # extract worker: batches and jobs
```

`AUTH_MODE=none` lässt jeden, der den Port erreicht, den Dienst nutzen: Verwenden Sie es nur auf
Ihrem eigenen Rechner und nur, solange Sie es brauchen. Auch eine Webseite, die in Ihrem Browser
offen ist, kann ihn über DNS-Rebinding erreichen, mit der Rolle Admin. Der Browser-Worker braucht das Extra `browser` und Chromium
(`uv sync --extra browser`, dann `uv run playwright install chromium`); starten Sie ihn mit
`uv run text-extraction-browser-worker` und setzen Sie `BROWSER_ENABLED=true` für die API, den
Extract-Worker und den Browser-Worker.

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
Browser-Worker und ein Valkey mit Passwort. Es schaltet den Browsermodus, den MCP-Server und die
Spielwiese ein.

```bash
cp .env.example .env                           # then set API_KEYS and VALKEY_PASSWORD in it
KEY=$(openssl rand -hex 32)                    # your API key: keep it
printf %s "$KEY" | sha256sum | cut -d' ' -f1   # its sha256: replace the zeros in API_KEYS with it
openssl rand -hex 24                           # a VALKEY_PASSWORD
docker compose up --build                      # public API on http://localhost:8080
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

`API_KEYS` enthält den SHA-256 jedes Schlüssels, nie den Schlüssel selbst. `.env.example` hat einen
Admin-Schlüssel namens `local`, dessen SHA-256 Sie ersetzen; [API-Schlüssel](usage.md#api-schlüssel)
erklärt die Schlüssel und ihre Rollen. Der interne Port 9090 (`/health`, `/ready`, `/metrics`)
wird nicht veröffentlicht. Die Spielwiese liegt unter http://localhost:8080/ui und der
MCP-Endpunkt unter http://localhost:8080/mcp.

### Veröffentlichte Images

Statt die Images selbst zu bauen, kann Compose eine veröffentlichte Version von
`ghcr.io/janschachtschabel/text-extraction` starten (linux/amd64). Die Datei
`docker-compose.release.yml` ersetzt jeden Build durch das veröffentlichte Image; `.env` bleibt
gleich, und die Profile funktionieren wie unten beschrieben.

```bash
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait
```

`TEXT_EXTRACTION_VERSION` in `.env` oder in der Shell wählt die Version (Standard `0.1.0`; `0.1`
folgt der neuesten `0.1.x`). Die Versionen und ihre Notizen stehen auf der
[Release-Seite](https://github.com/janschachtschabel/text-extraction/releases).

### Optionale Worker

Die schwereren Teile laufen in eigenen Workern, die standardmäßig aus sind. Jeder hat ein
Compose-Profil und einen Schalter in `.env`:

| Worker | Schalter in `.env` | Profil | Was er hinzufügt |
| --- | --- | --- | --- |
| Docling | `DOCLING_ENABLED=true` | `docling` | OCR für Scans und Bilder, OpenDocument-Dateien (ein Image von etwa 2 GB, Modelle von etwa 335 MiB) |
| NLP | `PII_ENABLED=true` | `nlp` | Behandlung personenbezogener Daten mit Presidio und spaCy (Modelle von etwa 1 GB für `lg`, 115 MB für `md`) |
| LLM | `LLM_ENABLED=true` | `llm` | Strukturierte Ausgabe mit einem LLM, semantisches Chunking und Bildbeschreibungen |
| ASR | `ASR_ENABLED=true` | `asr` | Spracherkennung für Audio und Video ohne Untertitel (das Whisper-Modell, etwa 1,6 GB) |

Starten Sie ein Profil mit `docker compose --profile docling up --build` (mehrere: je ein
`--profile`). Der erste Start lädt die Modelle in ein Volume. Der LLM-Worker braucht
`LLM_BASE_URL`, `LLM_MODEL` und `LLM_API_KEY` eines OpenAI-kompatiblen Endpunkts (und
`LLM_EMBEDDING_MODEL` für semantisches Chunking);
`docker compose run --rm llm-worker text-extraction-llm-worker --check` sendet ihm eine winzige
Extraktion. Für `pii: hash` mit demselben Hash in jedem Ergebnis setzen Sie `PII_HASH_KEY`
(`openssl rand -hex 32`).

Solange ein Schalter aus ist, antwortet eine Anfrage, die seinen Worker braucht, mit 400: `pii`
ohne `PII_ENABLED`, die Strategie `llm`, `learn_selectors` und `semantic`-Chunking ohne
`LLM_ENABLED`, `transcribe` ohne `ASR_ENABLED`. Ein Scan ohne `DOCLING_ENABLED` liefert nur seine
Textebene, mit der Warnung `ocr_unavailable`.

Die maximale Extraktion (für Websites, die die Extraktion vertraglich erlaubt haben) braucht
`BROWSER_UNBLOCK_ENABLED=true` in `.env`; siehe [Website-Regeln](site-rules.md#maximale-extraktion).

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
| `OUTBOUND_MIN_DELAY_MS` | `1000` | Mindestabstand zwischen zwei Anfragen an einen Host. |
| `OUTBOUND_BACKOFF_SECONDS` | `30` | Die Pause nach einem 429 oder 503 eines Hosts ohne `Retry-After`. |
| `JOBS_ENABLED` | `true` | Jobs: Batches, Website-Strukturen, `wait_seconds` und Screenshots. |
| `JOB_TTL_SECONDS` | `86400` | Wie lange ein Job und seine Ergebnisse aufbewahrt werden. |
| `BATCH_MAX_URLS` | `1000` | Höchstens so viele URLs in einem Batch. |
| `BATCH_MAX_OPEN_PER_CLIENT` | `5` | Höchstens so viele offene Batches und Website-Strukturen eines Schlüssels. |
| `BROWSER_ENABLED` | `false` | Browsermodus: Seiten, die JavaScript brauchen, werden in Chromium gerendert. |
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
