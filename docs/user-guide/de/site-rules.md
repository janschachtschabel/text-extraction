# Website-Regeln

English: [Site rules](../en/site-rules.md)

Standardmäßig beachtet der Dienst die Regeln einer Website. Die maximale Extraktion ist für
Websites, die die Extraktion vertraglich erlaubt haben; nur ein Admin darf sie nutzen, und nur dort,
wo der Betreiber sie eingeschaltet hat. Die Prüfungen lassen sich auch pro Lauf einstellen: Ein
Schlüssel, der es wählen darf, kann sie mit `compliance: ignore` abschalten. Diese Seite erklärt den
Standard, die maximale Extraktion, die Pause, um die ein Host bitten kann, und wie Sie in der API,
im MCP-Server und in der Spielwiese wählen.

## Die Regeln der Website beachten

Das ist der Standard. Der Dienst:

- prüft robots.txt und den TDM-Nutzungsvorbehalt der EU (den Vorbehalt einer Website gegen Text-
  und Data-Mining), bevor er eine Seite holt;
- sendet nur wenige Anfragen gleichzeitig an einen Host (`OUTBOUND_MAX_CONCURRENCY_PER_HOST`, 2),
  mit einem Abstand dazwischen (`OUTBOUND_MIN_DELAY_MS`, 1 s), und pausiert einen Host, der mit
  429 oder 503 geantwortet hat; eine Anfrage, die er deshalb zurückhält, antwortet mit
  `rate_limited` (wiederholbar), die Wartezeit steht in `error.details` (`retry_after_seconds`);
- sendet seinen eigenen, ehrlichen User-Agent.

`compliance` entscheidet, was die Prüfungen bewirken:

| `compliance` | Was passiert |
| --- | --- |
| `enforce` | Eine Seite, die robots.txt verbietet, schlägt mit `blocked_by_robots` fehl, eine Seite mit TDM-Nutzungsvorbehalt mit `tdm_reserved`. |
| `report` | Die Seite wird extrahiert; `data.compliance` meldet, was die Prüfungen gefunden haben. |
| `ignore` | Nur für Admins (oder wo es der Standard ist): Die Prüfungen entfallen. Mit dem Entsperrmodus ist das die maximale Extraktion. |

Eine Anfrage ohne `compliance` bekommt `COMPLIANCE_DEFAULT` des Betreibers (`enforce`). Jedes
Ergebnis enthält den Bericht in `data.compliance`: den `mode`, der galt, die `decision`
(`allowed`, `blocked`, `reported` oder `skipped`), was robots.txt sagte und woher eine
TDM-Angabe kam.

## Maximale Extraktion

### Wer sie nutzen darf

Die maximale Extraktion braucht drei Dinge:

- einen Admin-Schlüssel (ein Nutzer-Schlüssel, der `compliance: ignore` sendet, bekommt 403
  `forbidden`);
- `"compliance": "ignore"` in der Anfrage (oder keines, wo der Betreiber
  `COMPLIANCE_DEFAULT=ignore` gesetzt hat);
- `BROWSER_UNBLOCK_ENABLED=true` (mit `BROWSER_ENABLED=true`), vom Betreiber eingeschaltet.

Mit `compliance: ignore`, aber ausgeschaltetem Entsperrmodus entfallen die Prüfungen, und der
normale Browser wird genutzt: Das ist keine maximale Extraktion. Unter `AUTH_MODE=none` ist jeder
Aufrufer Admin. Nutzen Sie die maximale Extraktion nur für eine Website, deren Inhaber sie
erlaubt hat.

### Was sie bewirkt

- Die Prüfungen von robots.txt und TDM sind aus.
- Eine Seite, die an den Browser geht, wird in einem getarnten Chrome mit dem eigenen User-Agent von
  Chrome geladen. Der Browser wartet bis zu `BROWSER_UNBLOCK_CHALLENGE_WAIT_SECONDS` (20 s), bis
  eine Bot-Prüfung durchlaufen ist, und klickt eine sichtbare Checkbox-Prüfung an, höchstens zweimal
  pro Seite.
- Mit `mode: auto` fällt die maximale Extraktion öfter auf den Browser zurück: auch, wenn die Seite
  ablehnt (HTTP 403, 429 oder 503), und wenn eine HTML-Seite zu wenig Text liefert
  (`content_too_short`, `empty_content`). Die Antwort ist das bessere Ergebnis von HTTP und Browser.
- `data.unblocked` ist true, wenn die Seite auf diese Weise geladen wurde.

### Was sie nicht tut

- Die maximale Extraktion löst keine Bild- oder Puzzle-Captchas: Eine solche Seite schlägt mit
  `bot_protection` und der Warnung `challenge_not_cleared` fehl.
- Ein bezahlter Dienst oder ein Dienst Dritter wird nicht genutzt.
- IP-Adressen und Proxys werden nicht gewechselt.
- Die Schutzmaßnahmen des Dienstes bleiben: der Schutz vor internen Adressen bei jeder Anfrage,
  die die Seite stellt, die Grenzen für Bytes und Zeit und die Grenzen pro Host mit ihrer Pause.

Wo es geht, bitten Sie den Inhaber der Website besser um eine Freigaberegel: einen geheimen
Header, den Sie mit `headers` senden, oder eine Ausnahme für die ausgehende IP-Adresse des
Dienstes.

## Die Pause nach einem 429 oder 503

Ein Host, der mit 429 oder 503 antwortet, bekommt eine Pause: so lange, wie sein `Retry-After`
angibt, sonst `OUTBOUND_BACKOFF_SECONDS` (30 s), höchstens `OUTBOUND_MAX_BACKOFF_SECONDS` (300 s).
Die maximale Extraktion hält sie ein: Der Browser übernimmt die Seite erst nach der Pause.

- Eine Anfrage ohne `wait_seconds` wartet nicht so lange. Die Antwort ist dann oft der HTTP-Fehler,
  mit der Warnung `browser_unavailable`.
- Eine Anfrage mit `wait_seconds` wird zu einem Job, und der Job wird nach der Pause fertig. Senden
  Sie für solche Websites `wait_seconds`, oder verwenden Sie einen Batch.

## Mit REST

Senden Sie `compliance` mit einem Admin-Schlüssel:

<!-- request: POST /v1/extract -->
```json
{"url": "https://example.org/partner-page", "compliance": "ignore", "wait_seconds": 10}
```

```bash
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/partner-page", "compliance": "ignore", "wait_seconds": 10}' \
  http://localhost:8080/v1/extract
```

`POST /v1/batch`, `/v1/structured`, `/v1/chunk`, `/v1/screenshot` und `/v1/site-structure`
nehmen `compliance` genauso. Eine Website-Struktur liest ihre Seiten nur über HTTP, daher schaltet
`compliance: ignore` dort nur die Prüfungen aus.

## Mit MCP

Der Schlüssel des MCP-Clients muss den Wert wählen dürfen (siehe
[Prüfung pro Lauf an- und abschalten](#prüfung-pro-lauf-an--und-abschalten)). `compliance` gehört
in `options`, zum Beispiel bei `extract_url`:

```json
{"url": "https://example.org/partner-page", "options": {"compliance": "ignore"}}
```

`batch_extract`, `extract_structured`, `chunk`, `screenshot_url` und `site_structure` nehmen es
genauso. Siehe [MCP](mcp.md).

## Prüfung pro Lauf an- und abschalten

`compliance` (`enforce`, `report` oder `ignore`) ist ein Feld der Anfrage; Sie wählen die
Prüfungen also für jeden Lauf und nicht nur für die ganze Installation:

- Eine Anfrage: `compliance` im Body von `POST /v1/extract`, `/v1/structured`, `/v1/chunk`
  und `/v1/screenshot`.
- Ein Batch: `compliance` im Body von `POST /v1/batch`. Der eine Wert gilt für alle seine
  URLs.
- Ein Website-Lauf: `compliance` im Body von `POST /v1/site-structure`. Es gilt für den ganzen Lauf.
- MCP: `compliance` gehört in `options` des Werkzeugs.

Ein Schlüssel darf nur wählen, was ihm erlaubt ist: Ein Admin wählt alle drei Werte; ein anderer
Schlüssel wählt `enforce` oder `report`, solange der Betreiber Überschreibungen erlaubt
(`COMPLIANCE_ALLOW_OVERRIDE`), und `ignore` nur, wo es der Standard des Betreibers ist. REST lehnt
jeden anderen Wert mit 403 `forbidden` ab.

## In der Spielwiese

Die Auswahl „Website-Regeln“ hat drei Stufen. Sie steht in jedem Formular mit einer URL
(**Extrahieren**, **Strukturiert**, **Screenshot**, **Chunks** für eine URL, **Batch**,
**Batch-Datei**, **Website-Struktur**) und unter dem Website-Baum bei „Auswahl extrahieren“. Der
Tab **Datei** hat sie nicht.

| Stufe | Sendet | Bedeutung |
| --- | --- | --- |
| „Regeln der Website einhalten“ | `compliance: enforce` | robots.txt und TDM-Vorbehalte lehnen eine Seite ab. |
| „Nur prüfen und melden“ | `compliance: report` | Die Prüfungen laufen und werden gemeldet; nichts wird abgelehnt. |
| „Prüfung aus (Vertragspartner)“ | `compliance: ignore` | Die Prüfungen entfallen. Für Websites, die die Extraktion vertraglich erlaubt haben. |

Der Standard des Betreibers (`COMPLIANCE_DEFAULT`) ist vorausgewählt und mit „(Standard)“
gekennzeichnet; er sendet nichts, daher gilt der Wert des Betreibers. Eine andere Stufe sendet
`compliance`. Wer welche Stufe sieht:

| Wer | Stufen |
| --- | --- |
| Admins (und jeder Besucher unter `AUTH_MODE=none`) | alle drei |
| Andere Schlüssel, `COMPLIANCE_ALLOW_OVERRIDE` an | „Regeln der Website einhalten“ und „Nur prüfen und melden“; „Prüfung aus“ nur, wenn es der Standard ist |
| Andere Schlüssel, `COMPLIANCE_ALLOW_OVERRIDE` aus | keine Auswahl |

„Prüfung aus“ funktioniert ohne Entsperrmodus: Die Prüfungen entfallen, und der normale Browser
wird genutzt. Hat der Betreiber zusätzlich den Entsperrmodus eingeschaltet (`BROWSER_ENABLED` und
`BROWSER_UNBLOCK_ENABLED`), ist es die maximale Extraktion, wie oben beschrieben. Setzen Sie
`compliance` zusätzlich in „Optionen (JSON)“ (unter „Erweitert“), lehnt das Formular es ab:
`compliance` ist dann doppelt angegeben. Steht „Browser-Fallback“ auf „nie“, bleibt die maximale
Extraktion bei HTTP. Das Ergebnis zeigt den Wert der Website-Regeln (`compliance`) und, wenn
`data.unblocked` true ist, das Kennzeichen „maximale Extraktion (entsperrt)“.

Im Website-Baum sind URLs markiert, die die Regeln der Website sperren; siehe
[Teil einer Website](part-of-a-site.md#in-der-spielwiese).
