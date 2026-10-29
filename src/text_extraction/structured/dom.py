"""The DOM stage of structured output (rulings S3, S4 and S7's ``learn_html``).

The strategies that read the page's HTML run here, in ``Pipeline._process`` on the conversion
pool, for HTML pages only (fetched, rendered or uploaded); a document, a text or a media result
skips this stage. Pure: no I/O, and nothing here logs a selector, a value or the page.

* ``metadata``: for each top-level field, the first value whose name matches the field's
  normalised name (lower case, without ``_`` and ``-``), in this order: JSON-LD properties (the
  items that describe the page first, then those of its site), microdata ``itemprop``, RDFa
  ``property``, OpenGraph ``og:<name>``, ``<meta name>`` (a comma list for an array field), then
  the ``PageMetadata`` fields. A value that does not fit the field's type is no match.
* ``selectors``: the caller's CSS (soupsieve on BeautifulSoup) and XPath (lxml, admin keys only:
  ``checks.check_selector_access``) rules, merged over the learned ones (``SelectorStore``, CSS
  only): the caller's rule wins per field. An XPath that fails when it is evaluated (an unknown
  function, variable or prefix) leaves its field missing.
* ``auto``: the selectors (caller's or learned), then ``metadata``, per field (ruling R3).
* ``tables``: every ``<table>`` with at least two rows.
* ``learn_html``: the cleaned HTML that ``learn_selectors`` shows the LLM; ``RulePage`` runs a
  proposed selector on it exactly as this stage runs rules, so that a kept rule reproduces.

Bounds (rule 5; rulings R4, R5), since a selector's cost and a table's size are up to the page:

* Selector rules run only on pages of at most ``STRUCTURED_MAX_ELEMENTS`` elements; on a bigger
  page they are skipped, their fields stay missing, with the warning
  ``selectors_skipped_large_page`` (lxml cannot stop an expression once it runs, and
  ``//*//*//*`` is quadratic in the number of elements).
* A value is cut to ``MAX_VALUE_CHARS`` characters; a rule's field takes at most
  ``MAX_FIELD_VALUES`` values and ``MAX_FIELD_CHARS`` characters in all, then it is cut with the
  warning ``structured_truncated``.
* At most ``MAX_TABLES`` tables of at most ``MAX_TABLE_ROWS`` rows of at most
  ``MAX_TABLE_COLUMNS`` cells; ``colspan`` counts 1 to ``MAX_COLSPAN``; all tables together at
  most ``MAX_TABLES_CHARS`` characters of cell text, then they stop with the warning
  ``tables_truncated``.

The page is parsed once by lxml (the tree ``metadata`` made, for XPath and tables) and, only
when a CSS rule runs, once by BeautifulSoup.

``patterns`` and ``llm`` run later, in the post stage, on the final content; fields the DOM
stage did not fill stay missing (``values[field] = None``, listed in ``missing``).
"""

import hashlib
import html as html_lib
import json
from collections.abc import Iterable, Iterator, Mapping
from itertools import islice
from typing import Any

import structlog
from bs4 import BeautifulSoup, Tag
from lxml import etree
from lxml.html import HtmlElement

from text_extraction.convert.links import absolute_url
from text_extraction.licenses import License
from text_extraction.metadata import PageMetadata, StructuredItems, page_tree, structured_items
from text_extraction.structured.schema import CompiledSchema, coerce
from text_extraction.structured.types import (
    SelectorRule,
    Source,
    StructuredOptions,
    StructuredResult,
    Table,
)

_log = structlog.get_logger(__name__)

STRUCTURED_MAX_ELEMENTS = 50_000  # selector rules run on pages of at most this many elements
MAX_VALUE_CHARS = 10_000  # one value
MAX_FIELD_VALUES = 1_000  # values of one field from a rule
MAX_FIELD_CHARS = 100_000  # characters of one field from a rule, all values together
MAX_TABLES = 50
MAX_TABLE_ROWS = 1_000
MAX_TABLE_COLUMNS = 100  # cells of a row, colspan included
MAX_COLSPAN = 20
MAX_TABLES_CHARS = 1_000_000  # cell text of all tables together
SELECTORS_SKIPPED_WARNING = "selectors_skipped_large_page"
STRUCTURED_TRUNCATED_WARNING = "structured_truncated"
TABLES_TRUNCATED_WARNING = "tables_truncated"
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
    """The learned rules the stage would use: CSS rules of fields of the schema without a
    caller rule; none when the strategy does not read them."""
    compiled = options.compiled
    if compiled is None or not reads_learned(options):
        return {}
    return {
        name: rule
        for name, rule in learned.items()
        if name in compiled.fields and name not in options.selectors and rule.css is not None
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
    warnings: list[str] | None = None,
) -> StructuredResult:
    """The DOM strategies of ``options`` on the page ``html`` (final URL ``final_url``).
    ``metadata`` are the page's ``PageMetadata`` (the last source of ``metadata``; None: not
    used), ``items`` its ``StructuredItems`` (for ``metadata``: with the syntaxes read),
    ``learned`` the learned rules (by field; the caller's own rules win). With ``learn`` the
    result carries ``learn_html`` (cut to ``learn_max_chars``). The stage's warnings
    (``selectors_skipped_large_page``, ``structured_truncated``, ``tables_truncated``) are
    appended to ``warnings``. Never raises for a rule."""
    noted: list[str] = warnings if warnings is not None else []
    compiled = options.compiled
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    if compiled is not None:
        own_learned = usable_learned(options, learned or {})
        caller = options.selectors if options.strategy in ("selectors", "auto") else {}
        rules = {**own_learned, **caller}
        if rules and _too_large(items.tree):
            rules = {}
            noted.append(SELECTORS_SKIPPED_WARNING)
        page = _Page(html, items)
        uses_metadata = reads_metadata(options)
        truncated = False
        for name in compiled.fields:
            value: Any = None
            source: Source | None = None
            if name in rules:
                value, cut = page.rule_value(compiled, name, rules[name])
                truncated = truncated or cut
                source = "selectors" if name in caller else "learned"
            if value is None and uses_metadata:
                value, source = _metadata_value(compiled, name, items, metadata), "metadata"
            values[name] = value
            if value is not None and source is not None:
                sources[name] = source
        if truncated:
            noted.append(STRUCTURED_TRUNCATED_WARNING)
    tables = None
    if options.tables:
        tables, cut_tables = _tables(items.tree)
        if cut_tables:
            noted.append(TABLES_TRUNCATED_WARNING)
    return StructuredResult(
        values=values,
        sources=sources,
        missing=[name for name, value in values.items() if value is None],
        tables=tables,
        stage="dom",
        learn_html=clean_html_for_learning(html, learn_max_chars) if learn else None,
    )


def _too_large(tree: HtmlElement) -> bool:
    """Whether the page has more than ``STRUCTURED_MAX_ELEMENTS`` elements; stops counting
    there."""
    elements = tree.getroottree().iter(etree.Element)
    return sum(1 for _ in islice(elements, STRUCTURED_MAX_ELEMENTS + 1)) > STRUCTURED_MAX_ELEMENTS


def _cut(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, list):
        return [_cut(item) for item in value[:MAX_FIELD_VALUES]]
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
        return [_plain(item, depth + 1) for item in value[:MAX_FIELD_VALUES]]
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
        for item in value[:MAX_FIELD_VALUES]:
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


class _Found:
    """The values of one rule, within ``MAX_FIELD_VALUES`` and ``MAX_FIELD_CHARS``."""

    def __init__(self, take_all: bool) -> None:
        self.take_all = take_all
        self.values: list[Any] = []
        self.chars = 0
        self.truncated = False

    def add(self, value: Any) -> bool:
        """Keep ``value`` (blank text is skipped); False once no more are wanted."""
        if isinstance(value, str):
            value = value[:MAX_VALUE_CHARS]
            if not value.strip():
                return True
        if len(self.values) >= MAX_FIELD_VALUES:
            self.truncated = True
            return False
        if isinstance(value, str):
            room = MAX_FIELD_CHARS - self.chars
            if len(value) > room:
                self.truncated = True
                if value[:room].strip():
                    self.values.append(value[:room])
                return False
            self.chars += len(value)
        self.values.append(value)
        return self.take_all


class _Page:
    """The page for selector rules: lxml's tree for XPath, BeautifulSoup for CSS, parsed on
    the first CSS rule."""

    def __init__(self, html: str, items: StructuredItems) -> None:
        self._html = html
        self._items = items
        self._soup: BeautifulSoup | None = None

    def rule_value(
        self, compiled: CompiledSchema, name: str, rule: SelectorRule
    ) -> tuple[Any | None, bool]:
        """The field's value by ``rule`` (None: nothing found) and whether it was cut to the
        field's limits."""
        found = _Found(rule.all if rule.all is not None else _is_array(compiled, name))
        try:
            if rule.css is not None:
                self._css(rule, found)
            else:
                self._xpath(rule, found)
        except Exception as error:  # an XPath that fails at evaluation (M2.5), or worse
            _log.debug("structured_rule_failed", error_type=type(error).__name__)
            return None, False
        if not found.values:
            return None, found.truncated
        raw = found.values if found.take_all else found.values[0]
        return _cut(coerce(compiled, name, raw)), found.truncated

    def _url(self, attr: str | None, value: str) -> str:
        if attr is not None and attr.lower() in _URL_ATTRIBUTES:
            return absolute_url(self._items.base, value.strip()) or value
        return value

    def _css(self, rule: SelectorRule, found: _Found) -> None:
        if self._soup is None:
            self._soup = BeautifulSoup(self._html, "lxml")
        for tag in self._soup.css.iselect(rule.css or "", limit=MAX_FIELD_VALUES + 1):
            value = _tag_value(tag, rule.attr)
            if value is not None and not found.add(self._url(rule.attr, value)):
                break

    def _xpath(self, rule: SelectorRule, found: _Found) -> None:
        result = self._items.tree.getroottree().xpath(rule.xpath or "")
        matches: Iterable[Any] = result if isinstance(result, list) else [result]
        for match in matches:
            value = self._match_value(match, rule.attr)
            if value is not None and not found.add(value):
                break

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


class RulePage:
    """A page to run single selector rules on as the DOM stage does (``learn_selectors`` checks
    the LLM's proposals with it); ``base_url`` makes ``href`` and ``src`` values absolute."""

    def __init__(self, html: str, base_url: str) -> None:
        self._page = _Page(html, structured_items(html, base_url, syntaxes=False))

    def value(self, compiled: CompiledSchema, name: str, rule: SelectorRule) -> Any | None:
        """The value ``rule`` finds for the field ``name``; None when it finds none (or
        fails)."""
        return self._page.rule_value(compiled, name, rule)[0]


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
    """The cells' texts, a cell with ``colspan`` repeated (1 to ``MAX_COLSPAN`` times), at most
    ``MAX_TABLE_COLUMNS``."""
    out: list[str] = []
    for cell in cells:
        try:
            span = int((cell.get("colspan") or "1").strip())
        except ValueError:
            span = 1
        text = _bounded(cell.itertext())
        out.extend([text] * min(max(span, 1), MAX_COLSPAN, MAX_TABLE_COLUMNS - len(out)))
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


def _tables(tree: HtmlElement) -> tuple[list[Table], bool]:
    """The page's tables, and whether they were cut at ``MAX_TABLES_CHARS`` (the table that
    reached it keeps the rows before; nothing after it is read)."""
    tables: list[Table] = []
    budget = MAX_TABLES_CHARS
    for table in tree.getroottree().iter("table"):
        rows = _rows(table)
        if len(rows) < 2:
            continue
        first = _cells(rows[0])
        headers: list[str] = []
        if first and all(cell.tag == "th" for cell in first):
            headers = _expanded(first)
            rows = rows[1:]
        budget -= sum(map(len, headers))
        if budget < 0:
            return tables, True
        caption = table.find("caption")
        caption_text = _bounded(caption.itertext()) if caption is not None else ""
        kept: list[list[str]] = []
        cut = False
        for row in rows[:MAX_TABLE_ROWS]:
            cells = _expanded(_cells(row))
            size = sum(map(len, cells))
            if size > budget:
                cut = True
                break
            budget -= size
            kept.append(cells)
        tables.append(
            Table(index=len(tables), caption=caption_text or None, headers=headers, rows=kept)
        )
        if cut:
            return tables, True
        if len(tables) >= MAX_TABLES:
            break
    return tables, False


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
