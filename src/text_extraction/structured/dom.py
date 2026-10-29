"""The DOM stage of structured output (rulings S3, S4 and S7's ``learn_html``).

The strategies that read the page's HTML run here, in ``Pipeline._process`` on the conversion
pool, for HTML pages only (fetched, rendered or uploaded); a document, a text or a media result
skips this stage. Pure: no I/O, and nothing here logs a selector, a value or the page.

* ``metadata``: for each top-level field, the first value whose name matches the field's
  normalised name (lower case, without ``_`` and ``-``), in this order: JSON-LD properties (the
  items that describe the page first, then those of its site), microdata ``itemprop``, RDFa
  ``property``, OpenGraph ``og:<name>``, ``<meta name>`` (a comma list for an array field), then
  the ``PageMetadata`` fields. A value that does not fit the field's type is no match.
* ``selectors``: the caller's CSS (soupsieve on BeautifulSoup) and XPath (lxml) rules, and the
  learned ones (``SelectorStore``) for the fields without a caller rule. An XPath that fails
  when it is evaluated (an unknown function, variable or prefix) leaves its field missing.
* ``auto``: learned selectors, then ``metadata``, then the caller's selectors, per field.
* ``tables``: every ``<table>`` with at least two rows.
* ``learn_html``: the cleaned HTML that ``learn_selectors`` shows the LLM.

Bounds: a value is cut to ``MAX_VALUE_CHARS`` characters, a rule takes at most ``MAX_MATCHES``
matches, at most ``MAX_TABLES`` tables of at most ``MAX_TABLE_ROWS`` rows. The page is parsed
once by lxml (the tree ``metadata`` made, for XPath and tables) and, only when a CSS rule runs,
once by BeautifulSoup.

``patterns`` and ``llm`` run later, in the post stage, on the final content; fields the DOM
stage did not fill stay missing (``values[field] = None``, listed in ``missing``).
"""

import hashlib
import html as html_lib
import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import structlog
from bs4 import BeautifulSoup, Tag
from lxml import etree
from lxml.html import HtmlElement

from text_extraction.convert.links import absolute_url
from text_extraction.licenses import License
from text_extraction.metadata import PageMetadata, StructuredItems, page_tree
from text_extraction.structured.schema import CompiledSchema, coerce
from text_extraction.structured.types import (
    SelectorRule,
    Source,
    StructuredOptions,
    StructuredResult,
    Table,
)

_log = structlog.get_logger(__name__)

MAX_VALUE_CHARS = 10_000
MAX_MATCHES = 1_000
MAX_TABLES = 50
MAX_TABLE_ROWS = 1_000
MAX_TABLE_COLUMNS = 1_000
_URL_ATTRIBUTES = frozenset({"href", "src"})
_DOM_STRATEGIES = frozenset({"metadata", "selectors", "auto"})
_PAGE_FIELDS = (
    "title",
    "description",
    "authors",
    "keywords",
    "published",
    "modified",
    "language",
    "license",
    "site_name",
)
_TEXT_KEYS = ("@value", "name", "text", "headline")


# --- when the stage runs, and the cache key --------------------------------------------------


def touches_dom(options: StructuredOptions) -> bool:
    """Whether the options read the page's HTML for values or tables: strategy ``metadata``,
    ``selectors`` or ``auto``, or ``tables``. Their result is then part of the cached result."""
    return options.strategy in _DOM_STRATEGIES or options.tables


def runs_dom_stage(options: StructuredOptions) -> bool:
    """Whether an HTML page gets the DOM stage: ``touches_dom``, or ``learn_selectors`` (its
    ``learn_html``)."""
    return touches_dom(options) or options.learn_selectors


def reads_learned(options: StructuredOptions) -> bool:
    """Whether learned selectors may fill fields: strategy ``selectors`` or ``auto``."""
    return options.compiled is not None and options.strategy in ("selectors", "auto")


def reads_metadata(options: StructuredOptions) -> bool:
    """Whether the ``metadata`` strategy runs (``metadata`` or ``auto``)."""
    return options.compiled is not None and options.strategy in ("metadata", "auto")


def _digest(rules: Mapping[str, SelectorRule]) -> str:
    canonical = json.dumps(
        {name: rule.model_dump(mode="json") for name, rule in rules.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def structured_cache_part(
    options: StructuredOptions, learned: Mapping[str, SelectorRule]
) -> dict[str, Any] | None:
    """The part of the result cache key for ``structured`` (S4), or None when the options do
    not touch the DOM: then the result has no DOM part, and the key stays the one of a request
    without ``structured``. ``learned`` are the learned rules the stage uses (by field); they
    join the key only when there are some."""
    if not touches_dom(options):
        return None
    compiled = options.compiled
    part: dict[str, Any] = {
        "dom": options.strategy,
        "schema": compiled.schema_id if compiled is not None else None,
        "selectors": _digest(options.selectors),
        "tables": options.tables,
    }
    if learned:
        part["learned"] = _digest(learned)
    return part


def usable_learned(
    options: StructuredOptions, learned: Mapping[str, SelectorRule]
) -> dict[str, SelectorRule]:
    """The learned rules the stage would use: of fields of the schema without a caller rule;
    none when the strategy does not read them."""
    compiled = options.compiled
    if compiled is None or not reads_learned(options):
        return {}
    return {
        name: rule
        for name, rule in learned.items()
        if name in compiled.fields and name not in options.selectors
    }


# --- the stage -------------------------------------------------------------------------------


def dom_stage(
    html: str,
    final_url: str,
    options: StructuredOptions,
    metadata: PageMetadata | None,
    items: StructuredItems,
    *,
    learn: bool,
    learn_max_chars: int,
    learned: Mapping[str, SelectorRule] | None = None,
) -> StructuredResult:
    """The DOM strategies of ``options`` on the page ``html`` (final URL ``final_url``).
    ``metadata`` are the page's ``PageMetadata`` (the last source of ``metadata``; None: not
    used), ``items`` its ``StructuredItems`` (for ``metadata``: with the syntaxes read),
    ``learned`` the learned rules (by field; the caller's own rules win). With ``learn`` the
    result carries ``learn_html`` (cut to ``learn_max_chars``). Never raises for a rule."""
    compiled = options.compiled
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    if compiled is not None:
        page = _Page(html, items)
        rules = options.selectors if options.strategy in ("selectors", "auto") else {}
        own_learned = usable_learned(options, learned or {})
        uses_metadata = reads_metadata(options)
        for name in compiled.fields:
            value: Any = None
            source: Source | None = None
            if name in own_learned:
                value, source = page.rule_value(compiled, name, own_learned[name]), "learned"
            if value is None and uses_metadata:
                value, source = _metadata_value(compiled, name, items, metadata), "metadata"
            if value is None and name in rules:
                value, source = page.rule_value(compiled, name, rules[name]), "selectors"
            values[name] = value
            if value is not None and source is not None:
                sources[name] = source
    return StructuredResult(
        values=values,
        sources=sources,
        missing=[name for name, value in values.items() if value is None],
        tables=_tables(items.tree) if options.tables else None,
        stage="dom",
        learn_html=clean_html_for_learning(html, learn_max_chars) if learn else None,
    )


def _cut(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, list):
        return [_cut(item) for item in value[:MAX_MATCHES]]
    if isinstance(value, dict):
        return {key: _cut(item) for key, item in value.items()}
    return value


def _is_array(compiled: CompiledSchema, name: str) -> bool:
    return bool(compiled.properties[name].get("type") == "array")


def _squash(text: str) -> str:
    return " ".join(text.split())


def _bounded(pieces: Iterable[str]) -> str:
    """The pieces of an element's text joined and whitespace collapsed, read only as far as
    ``MAX_VALUE_CHARS`` needs."""
    parts: list[str] = []
    size = 0
    for piece in pieces:
        squashed = _squash(piece)
        if not squashed:
            continue
        parts.append(squashed)
        size += len(squashed) + 1
        if size > MAX_VALUE_CHARS:
            break
    return " ".join(parts)[:MAX_VALUE_CHARS]


# --- metadata --------------------------------------------------------------------------------


def _normal(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


def _plain(value: Any, depth: int = 0) -> Any:
    """A JSON-LD, microdata or RDFa value as data: ``{"@value": x}`` is ``x``, a nested
    microdata item its properties."""
    if depth > 6:
        return None
    if isinstance(value, list):
        return [_plain(item, depth + 1) for item in value[:MAX_MATCHES]]
    if isinstance(value, dict):
        if "@value" in value:
            return _plain(value["@value"], depth + 1)
        if isinstance(value.get("properties"), dict) and "@type" not in value:
            value = value["properties"]
        return {str(key): _plain(item, depth + 1) for key, item in value.items()}
    return value


def _texts(value: Any, depth: int = 0) -> Iterator[str]:
    """The texts of a value: a string, an item's name (or ``@value``, text, headline)."""
    if depth > 4:
        return
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, list):
        for item in value[:MAX_MATCHES]:
            yield from _texts(item, depth + 1)
    elif isinstance(value, dict):
        if isinstance(value.get("properties"), dict):
            value = value["properties"]
        for key in _TEXT_KEYS:
            if key in value:
                yield from _texts(value[key], depth + 1)
                return


def _fit(compiled: CompiledSchema, name: str, raw: Any) -> Any | None:
    """``raw`` made to fit the field: as data, else as its texts."""
    value = coerce(compiled, name, _plain(raw))
    if value is not None:
        return value
    texts = list(_texts(raw))
    if not texts:
        return None
    return coerce(
        compiled, name, texts if len(texts) > 1 or _is_array(compiled, name) else texts[0]
    )


def _node_value(
    compiled: CompiledSchema, name: str, key: str, nodes: list[dict[str, Any]]
) -> Any | None:
    for node in nodes:
        for prop, raw in node.items():
            if prop.startswith("@") or _normal(prop) != key:
                continue
            value = _fit(compiled, name, raw)
            if value is not None:
                return value
    return None


def _pair_value(
    compiled: CompiledSchema,
    name: str,
    key: str,
    pairs: list[tuple[str, str]],
    *,
    comma_list: bool,
) -> Any | None:
    contents = [content for pair_key, content in pairs if _normal(pair_key) == key]
    if not contents:
        return None
    if _is_array(compiled, name):
        if comma_list:
            contents = [part.strip() for content in contents for part in content.split(",")]
        return coerce(compiled, name, [content for content in contents if content])
    for content in contents:
        value = coerce(compiled, name, content)
        if value is not None:
            return value
    return None


def _page_value(compiled: CompiledSchema, name: str, raw: Any) -> Any | None:
    if isinstance(raw, License):
        if compiled.properties[name].get("type") == "object":
            return coerce(compiled, name, raw.model_dump(mode="json"))
        raw = raw.url or raw.id or raw.label
    return coerce(compiled, name, raw)


def _metadata_value(
    compiled: CompiledSchema, name: str, items: StructuredItems, metadata: PageMetadata | None
) -> Any | None:
    key = _normal(name)
    for nodes in (items.json_ld, items.microdata, items.rdfa):
        value = _node_value(compiled, name, key, nodes)
        if value is not None:
            return _cut(value)
    for pairs, comma_list in ((items.opengraph, False), (items.meta, True)):
        value = _pair_value(compiled, name, key, pairs, comma_list=comma_list)
        if value is not None:
            return _cut(value)
    if metadata is not None:
        for page_field in _PAGE_FIELDS:
            if _normal(page_field) == key:
                value = _page_value(compiled, name, getattr(metadata, page_field))
                if value is not None:
                    return _cut(value)
    return None


# --- selectors -------------------------------------------------------------------------------


class _Page:
    """The page for selector rules: lxml's tree for XPath, BeautifulSoup for CSS, parsed on
    the first CSS rule."""

    def __init__(self, html: str, items: StructuredItems) -> None:
        self._html = html
        self._items = items
        self._soup: BeautifulSoup | None = None

    def rule_value(self, compiled: CompiledSchema, name: str, rule: SelectorRule) -> Any | None:
        take_all = rule.all if rule.all is not None else _is_array(compiled, name)
        try:
            found = (
                self._css(rule, take_all) if rule.css is not None else self._xpath(rule, take_all)
            )
        except Exception as error:  # an XPath that fails at evaluation (M2.5), or worse
            _log.debug("structured_rule_failed", error_type=type(error).__name__)
            return None
        if not found:
            return None
        return _cut(coerce(compiled, name, found if take_all else found[0]))

    def _url(self, attr: str | None, value: str) -> str:
        if attr is not None and attr.lower() in _URL_ATTRIBUTES:
            return absolute_url(self._items.base, value.strip()) or value
        return value

    def _css(self, rule: SelectorRule, take_all: bool) -> list[Any]:
        if self._soup is None:
            self._soup = BeautifulSoup(self._html, "lxml")
        found: list[Any] = []
        for tag in self._soup.css.iselect(rule.css or "", limit=MAX_MATCHES):
            value = _tag_value(tag, rule.attr)
            if value is None or not value.strip():
                continue
            found.append(self._url(rule.attr, value))
            if not take_all:
                break
        return found

    def _xpath(self, rule: SelectorRule, take_all: bool) -> list[Any]:
        result = self._items.tree.getroottree().xpath(rule.xpath or "")
        matches: Iterable[Any] = result if isinstance(result, list) else [result]
        found: list[Any] = []
        for match in matches:
            value = self._match_value(match, rule.attr)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            found.append(value)
            if not take_all or len(found) >= MAX_MATCHES:
                break
        return found

    def _match_value(self, match: Any, attr: str | None) -> Any | None:
        if isinstance(match, etree._Element):
            if not isinstance(match.tag, str):  # a comment or a processing instruction
                return match.text
            if attr is not None:
                value = match.get(attr)
                return None if value is None else self._url(attr, value)
            return _bounded(match.itertext())
        if isinstance(match, str):
            # An attribute's value is made absolute like a CSS rule's; other strings (text,
            # ``string(...)``) are taken as they are.
            attrname = getattr(match, "attrname", None)
            return self._url(attrname, str(match)) if isinstance(attrname, str) else str(match)
        if isinstance(match, bool | float | int):
            return match
        return None


def _tag_value(tag: Tag, attr: str | None) -> str | None:
    if attr is None:
        return _bounded(tag.strings)
    value = tag.get(attr)
    if isinstance(value, list):  # `class` and the other multi-valued attributes
        return " ".join(value)
    return value


# --- tables ----------------------------------------------------------------------------------


def _cells(row: HtmlElement) -> list[HtmlElement]:
    return [cell for cell in row if cell.tag in ("th", "td")]


def _expanded(cells: list[HtmlElement]) -> list[str]:
    """The cells' texts, a cell with ``colspan`` repeated, at most ``MAX_TABLE_COLUMNS``."""
    out: list[str] = []
    for cell in cells:
        try:
            span = int((cell.get("colspan") or "1").strip())
        except ValueError:
            span = 1
        text = _bounded(cell.itertext())
        out.extend([text] * min(max(span, 1), MAX_TABLE_COLUMNS - len(out)))
        if len(out) >= MAX_TABLE_COLUMNS:
            break
    return out


def _rows(table: HtmlElement) -> list[HtmlElement]:
    """The table's own rows in document order: not those of a table inside a cell."""
    rows: list[HtmlElement] = []
    for child in table:
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in ("thead", "tbody", "tfoot"):
            rows.extend(row for row in child if row.tag == "tr")
    return rows


def _tables(tree: HtmlElement) -> list[Table]:
    tables: list[Table] = []
    for table in tree.getroottree().iter("table"):
        rows = _rows(table)
        if len(rows) < 2:
            continue
        first = _cells(rows[0])
        headers: list[str] = []
        if first and all(cell.tag == "th" for cell in first):
            headers = _expanded(first)
            rows = rows[1:]
        caption = table.find("caption")
        caption_text = _bounded(caption.itertext()) if caption is not None else ""
        tables.append(
            Table(
                index=len(tables),
                caption=caption_text or None,
                headers=headers,
                rows=[_expanded(_cells(row)) for row in rows[:MAX_TABLE_ROWS]],
            )
        )
        if len(tables) >= MAX_TABLES:
            break
    return tables


# --- learn_html ------------------------------------------------------------------------------

_DROPPED_TAGS = ("script", "style", "svg")
_KEPT_ATTRIBUTES = frozenset(
    {"id", "class", "itemprop", "property", "name", "href", "src", "content", "datetime"}
)
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def clean_html_for_learning(html: str, max_chars: int) -> str:
    """The page as ``learn_selectors`` shows it to the LLM (S7): without scripts, styles,
    comments, SVG and the ``<head>`` but its ``<title>``; of the attributes only ``id``,
    ``class``, ``itemprop``, ``property``, ``name``, ``href``, ``src``, ``content`` and
    ``datetime``. Cut to ``max_chars`` characters after the last whole tag that fits, so no tag
    is cut in half (elements may stay open)."""
    root = page_tree(html).getroottree().getroot()
    doomed = [
        element
        for element in root.iter(etree.Comment, etree.ProcessingInstruction, *_DROPPED_TAGS)
        if element is not root
    ]
    for head in root.iter("head"):
        doomed.extend(child for child in head if child.tag != "title")
    for element in doomed:
        if element.getparent() is not None:
            element.drop_tree()  # keeps its tail text
    return _serialised(root, max_chars)


def _start_tag(element: HtmlElement) -> str:
    attributes = "".join(
        f' {name}="{html_lib.escape(value, quote=True)}"'
        for name, value in element.attrib.items()
        if name in _KEPT_ATTRIBUTES
    )
    return f"<{element.tag}{attributes}>"


def _serialised(root: HtmlElement, max_chars: int) -> str:
    out: list[str] = []
    size = 0
    border = 0  # pieces up to the last whole tag

    def add(piece: str, *, tag: bool) -> bool:
        nonlocal size, border
        if size + len(piece) > max_chars:
            return False
        out.append(piece)
        size += len(piece)
        if tag:
            border = len(out)
        return True

    for event, element in etree.iterwalk(root, events=("start", "end")):
        if not isinstance(element.tag, str):
            continue
        if event == "start":
            if not add(_start_tag(element), tag=True):
                break
            if element.text and not add(html_lib.escape(element.text, quote=False), tag=False):
                break
        else:
            if element.tag not in _VOID and not add(f"</{element.tag}>", tag=True):
                break
            if (
                element.tail
                and element is not root
                and not add(html_lib.escape(element.tail, quote=False), tag=False)
            ):
                break
    else:
        return "".join(out)
    return "".join(out[:border])
