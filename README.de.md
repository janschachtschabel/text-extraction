# text-extraction

English: [README.md](README.md)

Ein FastAPI-Dienst, der Webseiten, Dateien, Audio und Video in sauberen Text oder Markdown
umwandelt, mit ihren Metadaten. Er liest eine Seite zuerst über HTTP und rendert sie in einem
Browser, wenn die Seite das braucht. Außerdem wandelt er Dokumente um, transkribiert Audio und
Video, behandelt personenbezogene Daten, füllt strukturierte Felder, zerlegt Text in Chunks und
erfasst ganze Websites. Er hat eine REST-API, einen MCP-Server und eine Spielwiese auf Deutsch und
Englisch.

## Funktionen

- **Schneller Modus und Browser-Modus.** Eine Seite wird zuerst über HTTP gelesen (`fast`) und in
  Chromium gerendert, wenn sie das braucht (`auto`: eine JavaScript-Hülle, eine Bot-Prüfung, ein
  Soft-404 oder zu wenig Text), oder immer (`browser`). Cookie-Banner werden automatisch
  abgelehnt; Seitenaktionen (`click`, `type`, `scroll`, `accept_cookies`, ...), Cookies und Header
  wirken im Browser.
- **Sauberer Text.** Markdown, reiner Text oder HTML ohne Navigation, Fußzeilen und anderen
  Boilerplate; MathML wird zu LaTeX; PHP-Fehlerausgaben, die eine Website in ihre Seiten schreibt,
  werden entfernt und aufgelistet.
  Eine dünne Seite bekommt zusätzlich ihre eingeklappten Lösungen, bis zu zwei Anhänge, die Datei
  eines edu-sharing-Knotens und, mit `transcribe`, ihr einzelnes Video.
- **Metadaten.** Titel, Beschreibung, Autoren, Daten, Sprache, Bilder, kanonische URL,
  Schlagwörter, Felder aus schema.org und LRMI, die Lizenz (Creative Commons und andere, mit
  Quelle und Sicherheit), der Seitentyp (etwa `content`, `listing` oder `imprint`) und die Links
  mit ihrer Kategorie.
- **Dokumente.** PDF, DOCX, PPTX, XLSX, XLS, EPUB, CSV, SVG (sein Text), HTML, reiner Text und Markdown, per URL
  oder als Upload (bis 20 MB, PDFs bis 100 Seiten). Mit dem OCR-Worker (Docling) auch Scans,
  Bilder (PNG, JPEG, TIFF, WebP, BMP) und OpenDocument-Dateien (ODT, ODS, ODP). Die Bytes
  entscheiden über den Typ, nicht der angegebene `Content-Type`.
- **Audio und Video.** Untertitel und Angaben von YouTube, Vimeo und den Mediatheken von ARD, ZDF,
  arte und 3sat; die Transkriptansicht von YouTube im Browser, wenn die Untertitel gesperrt sind;
  `<track>`-Texte der Player einer Seite. Mit dem ASR-Worker (faster-whisper oder ein
  OpenAI-kompatibler Endpunkt) eine Transkription von MP3, MP4, M4A, MOV, WebM, MKV, Ogg, WAV,
  FLAC und AAC, per URL oder als Upload (bis 25 MB).
- **Screenshots.** PNG, JPEG oder WebP der ganzen Seite oder des ersten Bildschirms, in jeder
  Fenstergröße von 320×240 bis 3840×2160, auf Wunsch verkleinert.
- **Personenbezogene Daten.** Personen, E-Mail-Adressen, Telefonnummern, IBANs, Kreditkarten und
  IP-Adressen (und auf Wunsch Orte, Organisationen und URLs) in deutschem und englischem Text
  werden maskiert, geschwärzt, gehasht oder markiert (Presidio und spaCy).
- **Strukturierte Daten und Chunks.** Die Felder eines JSON-Schemas aus den Metadaten, mit
  CSS-Selektoren (XPath für Admins), regulären Ausdrücken oder einem LLM (jeder
  OpenAI-kompatible Endpunkt); die Tabellen der Seite; Chunks (`fixed`, `recursive`, `markdown`,
  `sentence`, `semantic`); Bildbeschreibungen durch das LLM.
- **Viele Seiten und ganze Websites.** Batches mit bis zu 1.000 URLs als Jobs (die Spielwiese
  liest auch eine CSV- oder JSON-Datei); die Karte einer Website aus ihren Sitemaps und ihrem
  Linkgraphen, mit Seitentypen, in der Spielwiese als Baum, der die von robots.txt verbotenen
  Seiten markiert.
- **Website-Regeln.** robots.txt und der TDM-Nutzungsvorbehalt der EU (`enforce`, `report` oder
  `ignore` pro Anfrage), wenige Anfragen pro Host gleichzeitig, ein ehrlicher User-Agent. Für
  Websites, die die Extraktion vertraglich erlaubt haben, kann ein Admin die Prüfungen ausschalten
  und den Entsperr-Browser nutzen (siehe unten).
- **Schnittstellen.** Eine REST-API mit OpenAPI unter `/docs`, Jobs für lange Arbeiten, ein
  MCP-Server mit 11 Tools und eine Spielwiese unter `/ui` auf Deutsch und Englisch.
- **Betrieb.** Docker Compose und ein Helm-Chart; zustandslose Pods, der Zustand liegt in Valkey;
  API-Schlüssel mit den Rollen `user` und `admin` und einer Ratenbegrenzung pro Schlüssel; ein
  Ergebnis-Cache; ein SSRF-Schutz und Grenzen für Bytes und Zeit bei jedem Abruf; JSON-Logs,
  Prometheus-Metriken und Health-Checks auf einem internen Port; Trace-Header werden
  durchgereicht.

## Endpunkte

| Endpunkt | Was er tut |
| --- | --- |
| `POST /v1/extract` | Eine Seite, ein Dokument oder ein Video über die URL. |
| `POST /v1/extract/file` | Eine hochgeladene Datei (Dokument, Bild, Audio, Video). |
| `POST /v1/screenshot` | Ein Bild einer Seite. |
| `POST /v1/structured` | Die Felder eines JSON-Schemas aus einer Seite. |
| `POST /v1/chunk` | Eine Seite oder ein Text, in Chunks zerlegt. |
| `POST /v1/batch` | Viele URLs als ein Job. |
| `POST /v1/site-structure` | Die Karte einer Website, als Job. |
| `GET /v1/jobs/{id}`, `DELETE /v1/jobs/{id}` | Zustand und Ergebnisse eines Jobs; ihn abbrechen. |
| `GET /v1/jobs/{id}/artifacts/{name}` | Eine Datei eines Jobs, etwa ein Screenshot. |
| `GET /v1/info` | Die Version des Dienstes und die Rolle des Schlüssels. |

Jede Antwort hat dieselbe Ergebnishülle (`status`, `error.code`, `error.retryable`,
`upstream_status`, `mode_used`, Zeiten): Wenn das Ziel scheitert, antwortet die API trotzdem mit
200 und `status: failed`. Siehe [Nutzung](docs/user-guide/de/usage.md) und
[Parameter](docs/user-guide/de/parameters.md).

## Status

Prototyp `v0.1.11`: Die Pakete 0 bis 21 sind gemergt (Grundlage, Dienstgerüst, schnelle Extraktion,
Jobs und Batch, Browser-Worker, Dokumente und OCR, Audio und Video, PII, strukturierte Ausgabe und
Chunking, Website-Struktur, MCP und Oberfläche, Übergabe, Sprachen der Oberfläche mit
Entsperroption und zweisprachige Dokumentation, YouTube-Transkriptansicht, Screenshot-Formate und
Seitentypen, neue Gestaltung der Spielwiese, robots.txt-Markierungen im Website-Baum mit einem
Schalter für die Website-Regeln pro Lauf, Korrekturen an der Spielwiese nach dem Test von v0.1.4,
Website-Baum mit Typen, Spalten und schnellem Lesen der Sitemap, Zusatzfunktionen standardmäßig an,
Bot-Prüfungen als `bot_protection` gemeldet, Linklisten von Übersichtsseiten, KMap-Anhänge,
Tabellen und Formeln). Die Pakete und ihren Stand zeigt
[docs/packages.md](docs/packages.md#status) (Englisch).

## Zwei Arten der Extraktion

- **Regeln der Website einhalten** (der Standard): robots.txt und der TDM-Nutzungsvorbehalt der EU
  werden geprüft, jeder Host bekommt nur wenige Anfragen gleichzeitig, und der Dienst sendet
  seinen eigenen, ehrlichen User-Agent.
- **Maximale Extraktion** für Websites, die die Extraktion vertraglich erlaubt haben: Ein Admin
  sendet `"compliance": "ignore"`, und wo der Betreiber `BROWSER_UNBLOCK_ENABLED` eingeschaltet
  hat (in docker compose an), kommt ein getarnter Browser am Bot-Schutz vorbei, auch nach einem einfachen 403, 429 oder
  503. Er löst keine Bild-Captchas, nutzt keinen bezahlten Dienst und wechselt keine IP-Adressen;
  die Grenzen pro Host gelten weiter. Die Prüfungen lassen sich pro Lauf umschalten (`compliance`:
  `enforce`, `report` oder `ignore`); die Spielwiese bietet je nach Rolle „Regeln der Website
  einhalten“, „Nur prüfen und melden“ und „Prüfung aus (Vertragspartner)“.

In beiden Fällen entscheidet der Browser-Fallback (`mode`), ob eine Seite im Browser gerendert
wird: wenn die Seite ihn braucht (`auto`, der Standard), nie (`fast`) oder immer (`browser`).
Siehe [Website-Regeln](docs/user-guide/de/site-rules.md) und
[Browser-Fallback](docs/user-guide/de/usage.md#browser-fallback).

## Schnellstart mit Docker

Voraussetzung ist Docker mit Compose. Compose startet die API, einen Extract-Worker, einen
Browser-Worker, die optionalen Worker für OCR, Transkription, personenbezogene Daten und das LLM
(ihr erster Start lädt etwa 3 GB Modelle) und ein Valkey (mit Passwort) für den gemeinsamen Zustand.
Die `docker-compose.yml` im Hauptordner startet die veröffentlichten Images und braucht keine
Variablen: Ihr Dienst `init` legt beim ersten Start das Valkey-Passwort und einen
Admin-API-Schlüssel an.

```bash
docker compose up -d --wait                    # public API on http://localhost:8080
KEY=$(docker compose run --rm -T init text-extraction-init --show-admin-key)
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

Die Spielwiese liegt unter http://localhost:8080/ui und der MCP-Endpunkt unter
http://localhost:8080/mcp; Sie melden sich mit dem Schlüssel an. Der Docker Manager von Hostinger
stellt dieselbe Datei über die URL dieses Repositorys bereit: siehe
[Hostinger-VPS](docs/user-guide/de/installation.md#hostinger-vps).
[Installation](docs/user-guide/de/installation.md) beschreibt HTTPS mit `DOMAIN`, eigene
Schlüssel ([API-Schlüssel](docs/user-guide/de/usage.md#api-schlüssel)), das Bauen der Images mit
`docker-compose.build.yml`, die lokale Einrichtung mit uv, die optionalen Worker (OCR,
personenbezogene Daten, LLM, Spracherkennung), Kubernetes und die wichtigsten Einstellungen.

## Aktualisieren

Stellen Sie neu bereit (bei Hostinger: über die URL des Repositorys neu bereitstellen) oder
führen Sie `docker compose up -d --wait` aus: Die `docker-compose.yml` im Hauptordner lädt bei
jedem `docker compose up` und jedem Neu-Bereitstellen die neueste Version `0.1.x` (Tag `0.1`,
`pull_policy: always`); ein Container, den Docker nur neu startet, behält sein Image, und
laufende Container aktualisiert nichts von selbst. Jedes `up` braucht deshalb ghcr.io. Setzen Sie
`TEXT_EXTRACTION_VERSION` in `.env` nur, um eine Version festzuhalten, etwa `0.1.10`; entfernen
Sie einen alten festen Wert, damit wieder die neueste geladen wird. Ihre `.env` bleibt erhalten,
und ein Wert darin geht einem geänderten Standard vor: Vergleichen Sie sie nach einem Update mit
`.env.example`.
[CHANGELOG.md](CHANGELOG.md) (Englisch) nennt jede Änderung und unter „Upgrade“, was zu tun ist.

## Anwendungsfälle

- Eine Seite als Text oder Markdown, mit ihren Metadaten:
  [Eine URL](docs/user-guide/de/single-url.md).
- Dokumente, Audio und Video, personenbezogene Daten und Bildbeschreibungen:
  [Dateien, Medien und personenbezogene Daten](docs/user-guide/de/usage.md#dateien-medien-und-personenbezogene-daten).
- Ein Teil einer Website oder eine Liste von URLs oder eine CSV:
  [Ein Teil einer Website](docs/user-guide/de/part-of-a-site.md).
- Eine ganze Website mit den Metadaten und Lizenzen ihrer Seiten:
  [Eine ganze Website und ihre Metadaten](docs/user-guide/de/whole-site-and-metadata.md).
- Strukturierte Daten und Chunks: [Strukturierte Daten](docs/user-guide/de/structured-data.md).
- Ein KI-Assistent mit den 11 Tools des MCP-Servers: [MCP](docs/user-guide/de/mcp.md).
- Die Regeln einer Website beachten oder die maximale Extraktion für Vertragspartner:
  [Website-Regeln](docs/user-guide/de/site-rules.md).
- Ausprobieren im Browser: [die Spielwiese](docs/user-guide/de/usage.md#die-spielwiese).

Jedes Feld jeder Anfrage steht unter [Parameter](docs/user-guide/de/parameters.md).

## Strukturierte Ausgabe und Chunking

`structured` füllt die Felder eines JSON-Schemas aus einer Seite, aus ihren Metadaten, mit Ihren
Selektoren oder Mustern oder mit einem LLM, und `chunking` zerlegt den Inhalt in Chunks: siehe
[Strukturierte Daten](docs/user-guide/de/structured-data.md).

## Website-Struktur

`POST /v1/site-structure` erfasst eine Website, ihre Sitemaps und ihren Linkgraphen, als Job, und
ein Batch extrahiert dann die Seiten, die Sie auswählen: siehe
[Ein Teil einer Website](docs/user-guide/de/part-of-a-site.md) und
[Eine ganze Website und ihre Metadaten](docs/user-guide/de/whole-site-and-metadata.md).

## MCP

Mit `MCP_ENABLED=true` (in docker compose an) ist der Dienst ein MCP-Server unter `https://<host>/mcp` mit 11 Tools, für
Claude Code, Claude Desktop und andere Clients, angemeldet mit einem API-Schlüssel des Dienstes:
siehe [MCP](docs/user-guide/de/mcp.md).

## Dokumentation

- [Anleitung](docs/user-guide/de/index.md): Installation, Nutzung, die Anwendungsfälle, Parameter
  und Glossar (auch auf [Englisch](docs/user-guide/en/index.md))
- [Architektur](docs/architecture.md) (Englisch)
- [Konfiguration](docs/configuration.md) (Englisch): Umgebungsvariablen, Secrets, Ports, Header
- [Helm-Chart](deploy/helm/text-extraction/README.md) (Englisch)
- [Übergabe](docs/handover.md) (Englisch): die Übergabe-Checkliste des Betriebsleitfadens, Kosten,
  Wartung und Releases
- [CLAUDE.md](CLAUDE.md) (Englisch): Regeln und Arbeitsweise für Mitwirkende

## Lizenz

Apache-2.0. Siehe [LICENSE](LICENSE).
