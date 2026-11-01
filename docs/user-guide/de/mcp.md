# MCP

English: [MCP](../en/mcp.md)

Mit `MCP_ENABLED=true` (Helm `mcp.enabled=true`; Docker Compose schaltet es für die API ein) ist
der Dienst ein MCP-Server unter `https://<host>/mcp` (Streamable HTTP, zustandslos,
JSON-Antworten). Ein KI-Assistent wie Claude extrahiert dann Seiten, Dateien, Websites und
strukturierte Daten mit den 11 Tools des Servers. Diese Seite beschreibt die Einrichtung, die
Anmeldung, die Tools, den Umgang mit Jobs, langen Inhalt, Fehler und Beispiel-Prompts.

## Einrichtung

### Claude Code

```bash
claude mcp add --transport http text-extraction https://<host>/mcp \
  --header "Authorization: Bearer <key>"
# local compose: http://localhost:8080/mcp
```

### Claude Desktop

Claude Desktop erreicht einen Server, der einen Header braucht, über die Brücke `mcp-remote`
(braucht Node.js). In `claude_desktop_config.json` (Einstellungen, Entwickler, Konfiguration
bearbeiten):

```json
{
  "mcpServers": {
    "text-extraction": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<host>/mcp", "--header", "Authorization:${AUTH_HEADER}"],
      "env": {"AUTH_HEADER": "Bearer <key>"}
    }
  }
}
```

Der Schlüssel steht in `env`, damit er nicht Teil der Argumente ist
(`Authorization:${AUTH_HEADER}` hat kein Leerzeichen, an dem manche Clients trennen).
`mcp-remote` nimmt einfaches HTTP nur für `localhost` und `127.0.0.1` (wie
`http://localhost:8080/mcp` von Compose); für einen `http://`-Endpunkt auf einem anderen Host
(einem anderen Rechner eines vertrauenswürdigen Netzes) ergänzen Sie `"--allow-http"` in den
Argumenten. Starten Sie Claude Desktop nach der Änderung neu.

### Andere Clients

Jeder MCP-Client, der Streamable HTTP spricht und einen Header senden kann, funktioniert. Er
braucht drei Angaben, wie auch immer seine Konfiguration sie nennt:

- die URL `https://<host>/mcp`;
- den Transport: Streamable HTTP (manchmal `http` genannt);
- den Header `Authorization: Bearer <key>`.

Ein Client, der keine Header senden kann, kann `mcp-remote` wie oben nutzen. Clients, die in einem
Browser laufen, müssen in `MCP_ALLOWED_ORIGINS` stehen; eine Anfrage mit einem anderen `Origin`
wird abgelehnt.

## Anmeldung

Ein MCP-Client meldet sich mit einem API-Schlüssel des Dienstes an, genau wie ein REST-Client
(siehe [API-Schlüssel](usage.md#api-schlüssel)). Jeder Tool-Aufruf durchläuft die Prüfungen der
REST-Route, mit der Rolle des Schlüssels, und wird auf das Ratenlimit des Schlüssels angerechnet,
ebenso `initialize` und `tools/list`; ein wartendes `get_job` zählt für jede weitere 10 s einen
Aufruf mehr. Ein Admin-Schlüssel darf außerdem `compliance: ignore` (siehe [Website-Regeln](site-rules.md))
und XPath-Selektoren verwenden. Unter `AUTH_MODE=none` lassen Sie den Header weg.

Ein eigener LLM- oder ASR-Schlüssel wird auf dieselbe Weise übergeben, als Header `X-LLM-Api-Key`
und `X-ASR-Api-Key` (je ein weiteres `--header`); er ist nie ein Argument eines Tools.

## Die 11 Tools

| Tool | Zweck | Route | Wichtige Argumente (neben `options`) |
| --- | --- | --- | --- |
| `extract_url` | Eine Seite oder ein Dokument als Markdown oder Text, mit Metadaten. | `POST /v1/extract` | `url`, `format` (`markdown`), `mode` (`auto`), `wait_seconds`, `content_offset`, `max_chars` |
| `extract_file` | Eine eigene Datei, etwa ein PDF, als Text. | `POST /v1/extract/file` | `content_base64` (ein Präfix als `data:`-URL ist erlaubt), `file_name`, `content_type`, `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `screenshot_url` | Ein PNG einer Seite. | `POST /v1/screenshot` | `url`, `full_page` (true), `wait_seconds`; das PNG wird bis zu `MCP_IMAGE_MAX_BYTES` angehängt |
| `extract_structured` | Die Felder eines JSON-Schemas aus einer Seite. | `POST /v1/structured` | `url`, `structured` (Pflicht), `format`, `wait_seconds`, `content_offset`, `max_chars` |
| `chunk` | Eine Seite oder ein Text, in Chunks zerlegt. | `POST /v1/chunk` | `url` oder `text`, `chunking` (Pflicht), `text_type`, `wait_seconds`, `content_offset`, `max_chars` |
| `batch_extract` | Viele URLs als ein Job. | `POST /v1/batch` | `urls`, `format`, `mode`; antwortet mit einem Job |
| `site_structure` | Die Karte einer Website, als Job. | `POST /v1/site-structure` | `url`, `depth` (2), `max_pages` (100), `scope` (`host`); antwortet mit einem Job |
| `get_job` | Der Zustand eines Jobs und eine Seite seiner Einträge. | `GET /v1/jobs/{id}` | `job_id`, `offset`, `limit` (5, 1-50), `wait_seconds` (0, bis `MCP_WAIT_SECONDS`), `content_offset`, `max_chars`; keine `options` |
| `get_job_screenshot` | Das PNG eines Screenshot-Jobs. | `GET /v1/jobs/{id}/artifacts/screenshot.png` | `job_id`; keine `options` |
| `cancel_job` | Einen Job abbrechen und mit seinen Ergebnissen löschen. | `DELETE /v1/jobs/{id}` | `job_id`; keine `options` |
| `service_info` | Name und Version des Dienstes sowie Name und Rolle des Schlüssels. | `GET /v1/info` | keine |

`options` nimmt jedes weitere Feld des REST-Bodys der Route (siehe [Parameter](parameters.md)),
zum Beispiel `{"pii": {"mode": "mask"}}` oder `{"metadata": false}`; ein Feld, das sowohl als
Argument als auch in `options` steht, wird abgelehnt. Der strukturierte Inhalt einer Tool-Antwort
ist das REST-Envelope, und ihr Text ist dasselbe JSON, nach einem einzeiligen Hinweis, wenn etwas
zu tun ist.

## Jobs und `get_job`

Ein Aufruf, der länger als `MCP_WAIT_SECONDS` (25 s) dauert, antwortet mit einem Job: Rufen Sie
`get_job` mit seiner `job_id` und `wait_seconds` auf, bis er `completed` ist. `batch_extract` und
`site_structure` antworten immer mit einem Job. `get_job` zeigt `limit` Einträge pro Aufruf
(standardmäßig 5); lesen Sie mit `offset` weiter.

```json
{"job_id": "<job id>", "wait_seconds": 25, "offset": 0, "limit": 5}
```

Jobs brauchen `JOBS_ENABLED=true` (den Standard). Ohne Jobs läuft ein langsamer Aufruf synchron in
den Grenzen der Route, und `batch_extract`, `site_structure` und die Job-Tools antworten mit 503.
`cancel_job` bricht einen Job ab, den Sie nicht mehr brauchen.

## Langer Inhalt und `content_offset`

Ein langer `content` wird auf `max_chars` gekürzt (`MCP_MAX_CONTENT_CHARS`, 50.000; geteilt von
den Einträgen einer `get_job`-Seite), mit der Warnung `mcp_content_truncated`. Der Hinweis nennt
den `content_offset` für den nächsten Aufruf, und dieser liefert den Inhalt ab dort.

Eine ganze Antwort umfasst höchstens `MCP_MAX_ANSWER_CHARS` (200.000) Zeichen. Eine größere
`get_job`-Seite enthält weniger Einträge, und ihr `next_offset` nennt den Beginn der nächsten Seite.
In einem einzelnen Ergebnis werden die Listen `chunks`, `links`, `structured.tables` und
`site.links` in dieser Reihenfolge vom Ende her gekürzt, mit der Warnung `mcp_answer_truncated`;
die REST-Route liefert sie vollständig.

## Fehler

- Was REST mit 4xx oder 5xx beantwortet (ein falsches Argument, eine abgelehnte Rolle, das
  Ratenlimit), ist ein Tool-Fehler mit dem Fehler-Envelope von REST.
- Eine Seite, deren Extraktion fehlschlug (`status: failed`, in REST ein 200), ist eine normale
  Antwort: Lesen Sie ihren `error.code` und `error.retryable`.

## Beispiel-Prompts

- „https://example.org/mathe/brueche als Markdown extrahieren und die Lizenz nennen.“
  (`extract_url`)
- „Name, Lizenz und Bildungsstufe dieser drei Seiten lesen.“ (`extract_structured`)
- „Den Bereich https://example.org/mathe/ zwei Links tief erfassen und jede seiner Seiten
  extrahieren.“ (`site_structure`, `get_job`, `batch_extract`)
- „Einen Screenshot von https://example.org/ aufnehmen und das Layout beschreiben.“
  (`screenshot_url`)
- „Diesen Text an seinen Überschriften in Chunks von höchstens 500 Zeichen zerlegen.“ (`chunk`)
