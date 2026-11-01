# text-extraction

English: [README.md](README.md)

Ein FastAPI-Dienst, der Webseiten, Dateien, Audio und Video in sauberen Text oder Markdown
umwandelt, mit ihren Metadaten. Er liest eine Seite zuerst über HTTP und rendert sie in einem
Browser, wenn die Seite das braucht. Außerdem wandelt er Dokumente um, transkribiert Audio und
Video, behandelt personenbezogene Daten, füllt strukturierte Felder, zerlegt Text in Chunks und
erfasst ganze Websites. Er hat eine REST-API, einen MCP-Server und eine Spielwiese auf Deutsch und
Englisch.

## Status

Prototyp `v0.1.0`: Die Pakete 0 bis 13 sind gemergt (Grundlage, Dienstgerüst, schnelle
Extraktion, Jobs und Batch, Browser-Worker, Dokumente und OCR, Audio und Video, PII, strukturierte
Ausgabe und Chunking, Website-Struktur, MCP und Oberfläche, Übergabe, Sprachen der Oberfläche mit
Entsperroption und zweisprachige Dokumentation, YouTube-Transkriptansicht). Die Pakete und ihren
Stand zeigt [docs/packages.md](docs/packages.md#status) (Englisch).

## Zwei Arten der Extraktion

- **Regeln der Website beachten** (der Standard): robots.txt und der TDM-Nutzungsvorbehalt der EU
  werden geprüft, jeder Host bekommt nur wenige Anfragen gleichzeitig, und der Dienst sendet
  seinen eigenen, ehrlichen User-Agent.
- **Maximale Extraktion** für Websites, die die Extraktion vertraglich erlaubt haben: Ein Admin
  sendet `"compliance": "ignore"`, und wo der Betreiber `BROWSER_UNBLOCK_ENABLED` eingeschaltet
  hat, kommt ein getarnter Browser am Bot-Schutz vorbei, auch nach einem einfachen 403, 429 oder
  503. Er löst keine Bild-Captchas, nutzt keinen bezahlten Dienst und wechselt keine IP-Adressen;
  die Grenzen pro Host gelten weiter.

In beiden Fällen entscheidet der Browser-Fallback (`mode`), ob eine Seite im Browser gerendert
wird: wenn die Seite ihn braucht (`auto`, der Standard), nie (`fast`) oder immer (`browser`).
Siehe [Website-Regeln](docs/user-guide/de/site-rules.md) und
[Browser-Fallback](docs/user-guide/de/usage.md#browser-fallback).

## Schnellstart mit Docker

Voraussetzung ist Docker mit Compose. Compose startet die API, einen Extract-Worker, einen
Browser-Worker und ein Valkey (mit Passwort) für den gemeinsamen Zustand.

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

`.env.example` hat einen Admin-Schlüssel namens `local`, dessen SHA-256 Sie ersetzen; siehe
[API-Schlüssel](docs/user-guide/de/usage.md#api-schlüssel). Die Spielwiese liegt unter
http://localhost:8080/ui und der MCP-Endpunkt unter http://localhost:8080/mcp.
[Installation](docs/user-guide/de/installation.md) beschreibt die lokale Einrichtung mit uv, die
optionalen Worker (OCR, personenbezogene Daten, LLM, Spracherkennung), Kubernetes und die
wichtigsten Einstellungen.
Ohne Build startet `docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait`
die veröffentlichten Images: siehe
[Veröffentlichte Images](docs/user-guide/de/installation.md#veröffentlichte-images).

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

Mit `MCP_ENABLED=true` ist der Dienst ein MCP-Server unter `https://<host>/mcp` mit 11 Tools, für
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
