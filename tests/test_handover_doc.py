"""docs/handover.md answers the ops guide's hand-over checklist (section 6) item by item, in the
guide's order, and has the maintenance section (section 7); every relative link in it resolves
(ruling P11-S9). The item texts are the guide's, with the team names taken out (P11-S12)."""

import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDOVER = ROOT / "docs" / "handover.md"

# The ops guide, section 6, items 1 to 14 verbatim; item 14 without the team (P11-S12).
CHECKLIST = (
    "Repo-Link und CI-Pipeline, die getaggte, schlanke Images ohne Daten baut",
    "Pfad zur OpenAPI-Spec, Anzeigename und Beschreibungstext für Endnutzende",
    "Liste auszublendender Pfade bzw. Port-Aufteilung (öffentlich / intern)",
    "Besondere Header, Content-Types, erwartete Antwortzeiten, ggf. asynchrone Job-Endpunkte",
    "Mit B-API: Modell-Routing, Bedarf an virtuellem Key, Berechtigungen. Ohne B-API: "
    "Env-Variablen für LLM-Keys und Auth, Auth-Konzept",
    "Prometheus-Endpunkt, Applikationsmetriken und Alarmregeln",
    "Log-Format und Durchreichen von x-client-trace-id",
    "Aussage zu Zustand und horizontaler Skalierbarkeit, kurze Architekturübersicht",
    "Ressourcen-Baseline (RAM, CPU, Storage)",
    "Datenbankwahl (kein SQLite in Produktion) und abschaltbare Komponenten im Helm-Chart "
    "(DB, Redis, Worker)",
    "Datenbereitstellung für große Daten (Quelle, Volume-Größe)",
    "Env-Variablen mit Defaults, Beispiel-Values-YAML, Liste der Secrets, Dockerfile und "
    "docker-compose.yml als Referenz",
    "Kosteninformationen (z. B. erwartete LLM-Kosten, externe APIs)",
    "Ansprechperson für Rückfragen und Updates",
)

_LINK = re.compile(r"\]\(([^)\s]+)\)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _lines_outside_code(text: str) -> list[str]:
    """The lines of a Markdown text that are not inside a fenced code block."""
    lines, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """The ``##`` sections of a Markdown text: each heading with the lines up to the next one."""
    sections: list[tuple[str, list[str]]] = []
    for line in _lines_outside_code(text):
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _subtitle(body: list[str]) -> str:
    """The first non-blank line of a section, without the emphasis around it."""
    first = next((line.strip() for line in body if line.strip()), "")
    return first.strip("*_").strip()


def _slug(heading: str) -> str:
    """GitHub's anchor of a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = heading.strip().lower()
    kept = "".join(
        char for char in text if char in " -_" or unicodedata.category(char)[0] in ("L", "N")
    )
    return kept.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """Every heading anchor of a Markdown file, duplicates numbered as GitHub numbers them."""
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for line in _lines_outside_code(path.read_text(encoding="utf-8")):
        match = re.match(r"^#{1,6} (.+)$", line)
        if not match:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def test_every_checklist_item_has_a_section() -> None:
    subtitles = [_subtitle(body) for _, body in _sections(HANDOVER.read_text(encoding="utf-8"))]
    missing = [item for item in CHECKLIST if item not in subtitles]
    assert not missing, f"checklist items without a section: {missing}"
    in_order = [subtitle for subtitle in subtitles if subtitle in CHECKLIST]
    assert in_order == list(CHECKLIST), "the sections must follow the guide's order, once each"


def test_no_team_is_named() -> None:
    text = HANDOVER.read_text(encoding="utf-8")
    assert not re.search(r"\bT[46]\b", text)


def test_relative_links_resolve() -> None:
    text = HANDOVER.read_text(encoding="utf-8")
    broken = []
    for target in _LINK.findall("\n".join(_lines_outside_code(text))):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue  # https:, mailto: and the like
        path_part, _, anchor = target.partition("#")
        path = (HANDOVER.parent / path_part).resolve() if path_part else HANDOVER
        resolves = path.exists() and path.is_relative_to(ROOT)
        anchor_ok = not anchor or path.suffix != ".md" or anchor in _anchors(path)
        if not (resolves and anchor_ok):
            broken.append(target)
    assert not broken, f"links that do not resolve: {broken}"


def test_maintenance_section_exists() -> None:
    sections = dict(_sections(HANDOVER.read_text(encoding="utf-8")))
    assert "Maintenance and lifecycle" in sections
    body = "\n".join(sections["Maintenance and lifecycle"])
    for needle in ("Dependabot", "CHANGELOG.md", "prototype", "helm rollback", "v0.1.0"):
        assert needle in body, needle


def test_architecture_overview_has_one_mermaid_diagram() -> None:
    text = HANDOVER.read_text(encoding="utf-8")
    assert text.count("```mermaid") == 1


def test_slug_follows_github() -> None:
    assert _slug("Docling workers and the models volume") == "docling-workers-and-the-models-volume"
    assert _slug("Image descriptions (issue #20)") == "image-descriptions-issue-20"
    assert _slug("Public API (port 8080)") == "public-api-port-8080"
    assert _slug("1. Repository and CI") == "1-repository-and-ci"
