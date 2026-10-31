# Anleitung

English: [User guide](../en/index.md)

text-extraction wandelt Webseiten, Dateien, Audio und Video in sauberen Text oder Markdown um, mit
ihren Metadaten. Diese Anleitung erklärt, wie Sie ihn installieren, wie Sie die API, den MCP-Server
und die Spielwiese nutzen und was jeder Parameter bewirkt. Sie gibt es auf Englisch und auf
Deutsch, mit denselben Seiten.

## Seiten

- [Installation](installation.md): lokal mit uv, mit Docker Compose, auf Kubernetes und die
  wichtigsten Einstellungen.
- [Nutzung](usage.md): API-Schlüssel, das Ergebnis-Envelope, synchron und Jobs, der
  Browser-Fallback, Dateien und Medien, personenbezogene Daten und die Spielwiese.
- [Parameter](parameters.md): jedes Feld jedes Anfrage-Bodys, mit seinem Typ, seinem Standardwert
  und seiner Bedeutung.
- [Website-Regeln](site-rules.md): die Regeln einer Website beachten und die maximale
  Extraktion für Vertragspartner.
- [Eine URL](single-url.md): eine Seite in der Spielwiese, mit REST und mit MCP, und die Felder
  der Antwort.
- [Ein Teil einer Website](part-of-a-site.md): ein Bereich einer Website, eine Liste von URLs
  oder eine CSV.
- [Eine ganze Website und ihre Metadaten](whole-site-and-metadata.md): eine Website-Struktur,
  Batches ihrer Seiten mit ihren Metadaten und Lizenzen und das Lesen der Jobs.
- [Strukturierte Daten](structured-data.md): die Strategien von `structured`, das LLM und sein
  Schlüssel und das Chunking.
- [MCP](mcp.md): die Einrichtung für Claude Code, Claude Desktop und andere Clients und die
  11 Tools.
- [Glossar](glossary.md): die Begriffe dieser Anleitung und der Spielwiese, auf Englisch und
  Deutsch.

## Anwendungsfälle

- Eine Seite extrahieren: [Eine URL](single-url.md).
- Viele Seiten extrahieren: [Batch](usage.md#batch).
- Einen Teil einer Website, eine Liste von URLs oder eine CSV extrahieren:
  [Ein Teil einer Website](part-of-a-site.md).
- Eine ganze Website erfassen und die Metadaten ihrer Seiten lesen:
  [Eine ganze Website und ihre Metadaten](whole-site-and-metadata.md).
- Strukturierte Daten und Chunks lesen: [Strukturierte Daten](structured-data.md).
- Den MCP-Server nutzen: [MCP](mcp.md).
- Eine Website extrahieren, die es vertraglich erlaubt hat:
  [Website-Regeln](site-rules.md#maximale-extraktion).

## Für Entwickler

Diese Dokumente gibt es nur auf Englisch:

- [Architektur](../../architecture.md)
- [Konfiguration](../../configuration.md): jede Umgebungsvariable, die Secrets, Ports und Header
- [Übergabe](../../handover.md): Betrieb, Kosten, Wartung und Releases
