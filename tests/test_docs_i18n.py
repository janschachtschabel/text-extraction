"""The user docs in English and German (rulings P12-S11 to P12-S14): ``README.md`` with
``README.de.md`` and each page of ``docs/user-guide/en/`` with its partner in ``de/`` have the same
heading levels, byte-identical code blocks, the same relative link targets and the same table
sizes; and the docs stay correct: marked request examples validate, the routes and settings they
name exist, and the parameters page names every field of each request body."""

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import CollectorRegistry
from pydantic import BaseModel

from text_extraction.app import create_public_app
from text_extraction.browser_models import ScreenshotRequest
from text_extraction.extract_models import ChunkRequest, ExtractRequest, StructuredRequest
from text_extraction.file_models import FileForm
from text_extraction.job_models import BatchRequest
from text_extraction.pii.types import ENTITY_TYPES
from text_extraction.settings import Settings
from text_extraction.site.types import SiteStructureRequest
from text_extraction.ui.i18n import CATALOGUES

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "user-guide"
GUIDE_EN = GUIDE / "en"
GUIDE_DE = GUIDE / "de"
README = ROOT / "README.md"
README_DE = ROOT / "README.de.md"
# The pages this package's first docs task writes; the use-case pages follow in the same folders.
BASE_PAGES = ("index", "installation", "usage", "parameters", "glossary")

PAIRS: list[tuple[Path, Path]] = [(README, README_DE)] + [
    (page, GUIDE_DE / page.name) for page in sorted(GUIDE_EN.glob("*.md"))
]

# Each request body by its route, as the docs name it (``<!-- request: POST /v1/extract -->``).
REQUEST_MODELS: Mapping[str, type[BaseModel]] = {
    "POST /v1/extract": ExtractRequest,
    "POST /v1/structured": StructuredRequest,
    "POST /v1/chunk": ChunkRequest,
    "POST /v1/batch": BatchRequest,
    "POST /v1/site-structure": SiteStructureRequest,
    "POST /v1/screenshot": ScreenshotRequest,
    "POST /v1/extract/file": FileForm,
}

# Backticked UPPER_SNAKE names that are not settings: shell variables of the examples, and the
# PII entity types.
NOT_SETTINGS = frozenset({"API_KEY", "AUTH_HEADER"}) | frozenset(ENTITY_TYPES)

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6}) \S")
_LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:")
_REQUEST = re.compile(r"<!-- request: (\S+) (\S+) -->\n+```json\n(.*?)\n```", re.DOTALL)
_MARKER = re.compile(r"<!-- request: ")
_ROUTE = re.compile(r"/v1(?:/(?:\{[^}\s/]*\}|<[^>/]*>|[A-Za-z0-9._-]+))*")
_SETTING = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?:=[^`]*)?`")
_FIELD_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.MULTILINE)


@dataclass
class DocStructure:
    """What both languages of a doc share: heading levels in order, the fenced code blocks
    (fences included), the relative link targets as written, and the rows of each table."""

    headings: list[int] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    table_rows: list[int] = field(default_factory=list)


def structure(text: str) -> DocStructure:
    """The structure of a Markdown text (see ``DocStructure``)."""
    doc = DocStructure()
    block: list[str] | None = None
    fence = ""
    in_table = False
    for line in text.splitlines():
        if block is not None:
            block.append(line)
            if line.strip() == fence:
                doc.code_blocks.append("\n".join(block))
                block = None
            continue
        opening = _FENCE.match(line)
        if opening:
            block, fence, in_table = [line], opening.group(1), False
            continue
        if line.startswith("|"):
            if not in_table:
                doc.table_rows.append(0)
            doc.table_rows[-1] += 1
            in_table = True
        else:
            in_table = False
        heading = _HEADING.match(line)
        if heading:
            doc.headings.append(len(heading.group(1)))
        doc.links.extend(
            target
            for target in _LINK.findall(_without_code_spans(line))
            if not _SCHEME.match(target)
        )
    return doc


def _without_code_spans(line: str) -> str:
    return re.sub(r"`[^`]*`", "", line)


def _prose(text: str) -> str:
    """A Markdown text without its fenced code blocks."""
    lines, fence = [], ""
    for line in text.splitlines():
        if fence:
            if line.strip() == fence:
                fence = ""
            continue
        opening = _FENCE.match(line)
        if opening:
            fence = opening.group(1)
            continue
        lines.append(line)
    return "\n".join(lines)


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
    for line in _prose(path.read_text(encoding="utf-8")).splitlines():
        match = re.match(r"^#{1,6} (.+)$", line)
        if not match:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _canonical(source: Path, target: str) -> str:
    """Where a relative link leads, without its anchor, with a German file taken as its English
    partner: both languages of a page then link the same targets."""
    path_part = target.partition("#")[0]
    resolved = (source.parent / path_part).resolve() if path_part else source
    if not resolved.is_relative_to(ROOT):
        return str(resolved)
    relative = resolved.relative_to(ROOT).as_posix()
    if relative == "README.de.md":
        return "README.md"
    return re.sub(r"^docs/user-guide/de/", "docs/user-guide/en/", relative)


def _docs() -> list[Path]:
    return [path for pair in PAIRS for path in pair]


def _pair_id(pair: tuple[Path, Path]) -> str:
    return pair[0].relative_to(ROOT).as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_paths() -> list[list[str]]:
    app = create_public_app(Settings(auth_mode="none"), FakeAsyncRedis(), CollectorRegistry())
    return [_segments(path) for path in app.openapi()["paths"]]


def _segments(path: str) -> list[str]:
    """A path's segments, with every parameter (``{id}``, ``<job id>``) as ``{}``."""
    return [
        "{}" if re.fullmatch(r"\{.*\}|<.*>", segment) else segment
        for segment in path.strip("/").split("/")
    ]


def _route_exists(named: list[str], app_paths: list[list[str]]) -> bool:
    return any(
        len(named) == len(known)
        and all(k == "{}" or n == k for n, k in zip(named, known, strict=True))
        for known in app_paths
    )


def _section(text: str, title_part: str) -> str:
    """The lines of the first section whose heading contains ``title_part``, up to the next
    heading of the same or a higher level."""
    lines = text.splitlines()
    for start, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading and title_part in line:
            level = len(heading.group(1))
            body = []
            for following in lines[start + 1 :]:
                other = _HEADING.match(following)
                if other and len(other.group(1)) <= level:
                    break
                body.append(following)
            return "\n".join(body)
    raise AssertionError(f"no section with {title_part!r}")


def test_every_user_doc_has_both_languages() -> None:
    english = {page.name for page in GUIDE_EN.glob("*.md")}
    german = {page.name for page in GUIDE_DE.glob("*.md")}
    assert english == german
    assert {f"{page}.md" for page in BASE_PAGES} <= english
    assert README_DE.exists()


@pytest.mark.parametrize("pair", PAIRS, ids=_pair_id)
def test_structures_match(pair: tuple[Path, Path]) -> None:
    english, german = (structure(_read(path)) for path in pair)
    assert german.headings == english.headings
    assert german.table_rows == english.table_rows
    links_en = [_canonical(pair[0], target) for target in english.links]
    links_de = [_canonical(pair[1], target) for target in german.links]
    assert links_de == links_en


@pytest.mark.parametrize("pair", PAIRS, ids=_pair_id)
def test_code_blocks_are_identical(pair: tuple[Path, Path]) -> None:
    english, german = (structure(_read(path)) for path in pair)
    assert german.code_blocks == english.code_blocks


def test_relative_links_resolve() -> None:
    broken = []
    for doc in _docs():
        for target in structure(_read(doc)).links:
            path_part, _, anchor = target.partition("#")
            path = (doc.parent / path_part).resolve() if path_part else doc
            resolves = path.exists() and path.is_relative_to(ROOT)
            anchor_ok = not anchor or path.suffix != ".md" or anchor in _anchors(path)
            if not (resolves and anchor_ok):
                broken.append(f"{doc.relative_to(ROOT)}: {target}")
    assert not broken, f"links that do not resolve: {broken}"


def test_request_examples_are_valid() -> None:
    checked = 0
    for doc in _docs():
        text = _read(doc)
        examples = _REQUEST.findall(text)
        assert len(examples) == len(_MARKER.findall(text)), (
            f"{doc.relative_to(ROOT)}: a request marker without a json block right after it"
        )
        for method, route, body in examples:
            model = REQUEST_MODELS[f"{method} {route}"]
            model.model_validate(json.loads(body))
            checked += 1
    assert checked >= len(REQUEST_MODELS)


def test_routes_named_exist(app_paths: list[list[str]]) -> None:
    unknown = set()
    for doc in _docs():
        for named in _ROUTE.findall(_read(doc)):
            named = named.rstrip(".")
            if named != "/v1" and not _route_exists(_segments(named), app_paths):
                unknown.add(f"{doc.relative_to(ROOT)}: {named}")
    assert not unknown, f"routes the app does not have: {sorted(unknown)}"


def test_named_settings_exist() -> None:
    settings = {name.upper() for name in Settings.model_fields}
    unknown = set()
    for doc in _docs():
        for name in _SETTING.findall(_prose(_read(doc))):
            if name not in settings and name not in NOT_SETTINGS:
                unknown.add(f"{doc.relative_to(ROOT)}: {name}")
    assert not unknown, f"named but not a setting: {sorted(unknown)}"


@pytest.mark.parametrize("language", [GUIDE_EN, GUIDE_DE], ids=["en", "de"])
def test_parameters_page_names_every_field(language: Path) -> None:
    text = _read(language / "parameters.md")
    problems = []
    for route, model in REQUEST_MODELS.items():
        rows = _FIELD_ROW.findall(_section(text, f"`{route}`"))
        if sorted(rows) != sorted(model.model_fields):
            missing = set(model.model_fields) - set(rows)
            extra = set(rows) - set(model.model_fields)
            problems.append(f"{route}: missing {sorted(missing)}, not fields {sorted(extra)}")
        if len(rows) != len(set(rows)):
            problems.append(f"{route}: a field twice")
    assert not problems, "\n".join(problems)


def test_readmes_link_each_other() -> None:
    top_en = _read(README).splitlines()[:5]
    top_de = _read(README_DE).splitlines()[:5]
    assert any("](README.de.md)" in line for line in top_en)
    assert any("](README.md)" in line for line in top_de)


@pytest.mark.parametrize("language", [GUIDE_EN, GUIDE_DE], ids=["en", "de"])
def test_glossary_matches_the_catalogue(language: Path) -> None:
    """A glossary row whose English term is a UI text gives that text's German catalogue value
    (one of them, when several keys share the English text)."""
    english_values: dict[str, set[str]] = {}
    for key, value in CATALOGUES["en"].items():
        english_values.setdefault(value, set()).add(CATALOGUES["de"][key])
    lines = _read(language / "glossary.md").splitlines()
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line, following in zip(lines, [*lines[1:], ""], strict=True)
        if line.startswith("| ")
        and not line.startswith("| ---")
        and not following.startswith("| ---")  # a header row
    ]
    problems = [
        f"{english}: {german} is not in {sorted(english_values[english])}"
        for english, german, *_ in rows
        if english in english_values and german not in english_values[english]
    ]
    assert not problems, "\n".join(problems)
    assert len(rows) > 10
