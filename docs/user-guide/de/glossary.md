# Glossar

English: [Glossary](../en/glossary.md)

Die Begriffe dieser Anleitung und der Spielwiese, mit dem englischen Begriff, den die englische
Anleitung und die englische Spielwiese verwenden. Ein Feldname, ein Wert und ein Fehlercode bleiben
in beiden Sprachen, wie sie sind, in Backticks (`mode`, `auto`, `target_http_error`). „Sie“ ist die
Anrede, wo eine Anrede nötig ist.

## Spielwiese

Die Beschriftungen der Spielwiese, wie die englische und die deutsche Spielwiese sie zeigen.

| Englisch | Deutsch | Bedeutung |
| --- | --- | --- |
| Extract | Extrahieren | Der Tab für eine Seite über ihre URL. |
| File | Datei | Der Tab für einen Upload. |
| Screenshot | Screenshot | Der Tab für ein PNG einer Seite. |
| Structured | Strukturiert | Der Tab für strukturierte Daten aus einer Seite. |
| Chunk | Chunks | Der Tab, der eine Seite oder einen Text in Chunks zerlegt. |
| Batch | Batch | Der Tab für viele URLs als ein Job. |
| Batch file | Batch-Datei | Der Tab für einen Batch aus einer CSV- oder JSON-Datei. |
| Site structure | Website-Struktur | Der Tab, der eine Website crawlt; auch der Job und sein Ergebnis. |
| Info | Info | Der Tab mit Name und Version des Dienstes und der Rolle des Schlüssels. |
| Operations | Funktionen | Die Liste der Tabs. |
| Site rules | Website-Regeln | Die Wahl zwischen dem Beachten der Regeln einer Website und der maximalen Extraktion. |
| Respect the site's rules (default) | Regeln der Website beachten (Standard) | Der Standard: robots.txt und der TDM-Nutzungsvorbehalt werden geprüft. |
| Maximum extraction (contract partners) | Maximale Extraktion (Vertragspartner) | Für Websites, die die Extraktion vertraglich erlaubt haben: `compliance: ignore`, mit Entsperrmodus. |
| Browser fallback | Browser-Fallback | Das Feld `mode`: ob der Browser eine Seite rendert. |
| when the page needs it | wenn die Seite ihn braucht | `mode: auto`. |
| never | nie | `mode: fast`. |
| always | immer | `mode: browser`. |
| Most pages | Maximale Seitenzahl | Das Feld `max_pages` einer Website-Struktur. |
| Site tree | Website-Baum | Die Seiten einer Website-Struktur, nach Host und Pfad. |
| crawled | gecrawlt | Eine Seite des Baums, die der Crawl geholt hat. |
| linked only | nur verlinkt | Eine URL des Baums, die eine Seite verlinkt, die der Crawl aber nicht geholt hat. |
| Principal | Konto | Name und Rolle des Schlüssels in einem Ergebnis. |
| What happened | Ablauf | Die Zeitleiste eines Ergebnisses: Zuteilungen, Prüfungen, Abruf, Rendern, Umwandlung und Ergebnis. |
| Claim | Zuteilung | Ein Worker übernimmt einen Job-Eintrag; eine zweite Zuteilung ist eine Wiederholung. |
| Compliance | Regelprüfung | Der Schritt, der robots.txt und den TDM-Nutzungsvorbehalt prüft. |
| escalated from fast | Browser-Fallback nach HTTP | Der Browser hat die Seite nach dem HTTP-Abruf gerendert. |
| unblock mode | Entsperrmodus | Der getarnte Browser der maximalen Extraktion. |
| queued | wartet | Ein Job oder Eintrag, der auf einen Worker wartet. |
| running | läuft | Ein Job oder Eintrag in Arbeit. |
| completed | abgeschlossen | Ein Job, dessen Einträge alle fertig sind. |
| Metadata | Metadaten | Titel, Autoren, Lizenz, Daten und Bilder einer Seite. |
| Metadata, structured | Metadaten, strukturiert | Die schema.org- und LRMI-Felder einer Seite. |
| API key | API-Schlüssel | Der Schlüssel, den jede Anfrage sendet. |
| Help (user guide) | Hilfe (Anleitung) | Der Link von einem Tab zu dieser Anleitung. |
| Details from the API (English) | Details der API (Englisch) | Die eigene `error.message` der API, unter dem übersetzten Fehler. |

## Dienst

Begriffe der API und dieser Anleitung.

| Englisch | Deutsch | Bedeutung |
| --- | --- | --- |
| site | Website | Alle Seiten eines Hosts. „Site-Crawl“ und „Seite“ für eine ganze Website werden nicht verwendet. |
| page | Seite | Eine einzelne Webseite (auch „Webseite“). |
| crawl | Crawl | Den Links einer Website folgen, um sie zu erfassen. |
| job | Job | Arbeit, die im Hintergrund läuft, mit einer ID zum Abfragen. |
| item | Eintrag | Eine URL oder Datei eines Jobs, mit ihrem eigenen Ergebnis. |
| worker | Worker | Ein Prozess, der Job-Einträge aus der Warteschlange übernimmt. |
| upload | Upload | Eine Datei, die an `POST /v1/extract/file` gesendet wird. |
| playground | Spielwiese | Die Weboberfläche unter `/ui`. |
| result envelope | Ergebnis-Envelope | Die Form jeder Antwort: `status`, `data`, `error`. |
| maximum extraction | maximale Extraktion | `compliance: ignore` mit Entsperrmodus, für Vertragspartner. |
| unblock browser | Entsperr-Browser | Der getarnte Chrome des Entsperrmodus. |
| TDM opt-out | TDM-Nutzungsvorbehalt | Der Vorbehalt einer Website gegen Text- und Data-Mining. |
| entity | Treffer | Ein personenbezogenes Datum, das `pii` gefunden hat („Funde“ wird nicht verwendet). |
| chunk | Chunk | Ein Stück zerlegten Inhalts. |
| rate limit | Ratenlimit | Die höchste Zahl der Anfragen pro Minute eines Schlüssels. |
| user guide | Anleitung | Diese Anleitung („Benutzerhandbuch“ wird nicht verwendet). |
