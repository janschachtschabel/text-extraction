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
- [Glossar](glossary.md): die Begriffe dieser Anleitung und der Spielwiese, auf Englisch und
  Deutsch.

## Anwendungsfälle

- Eine Seite extrahieren: [Nutzung](usage.md#das-ergebnis-envelope) und
  [`POST /v1/extract`](parameters.md#post-v1extract).
- Viele Seiten extrahieren: [Batch](usage.md#batch).
- Eine Website oder einen Teil davon erfassen: [Website-Struktur](../../../README.de.md#website-struktur)
  in der README.
- Strukturierte Daten und Chunks lesen:
  [Strukturierte Ausgabe und Chunking](../../../README.de.md#strukturierte-ausgabe-und-chunking) in der
  README.
- Den MCP-Server nutzen: [MCP](../../../README.de.md#mcp) in der README.

## Für Entwickler

Diese Dokumente gibt es nur auf Englisch:

- [Architektur](../../architecture.md)
- [Konfiguration](../../configuration.md): jede Umgebungsvariable, die Secrets, Ports und Header
- [Übergabe](../../handover.md): Betrieb, Kosten, Wartung und Releases
