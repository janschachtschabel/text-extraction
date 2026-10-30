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
  only): the caller's rule wins per field. The rules do not run here: ``selector_rules`` names
  them, the caller runs them in a selector child (``selector_pool``, ruling R19: killed at
  ``STRUCTURED_SELECTOR_TIMEOUT_SECONDS``) and passes what they ``found`` to ``dom_stage``. A
  rule that found nothing, failed when it was evaluated (an unknown XPath function, variable or
  prefix) or did not finish in time leaves its field missing.
* ``auto``: the selectors (caller's or learned), then ``metadata``, per field (ruling R3).
* ``tables``: every ``<table>`` with at least two rows.
* ``learn_html``: the cleaned HTML that ``learn_selectors`` shows the LLM; its proposals run on
  it in a selector child and are read with ``rule_value`` exactly as this stage reads rules, so
  that a kept rule reproduces.

Bounds (rule 5; rulings R4, R5), since a selector's cost and a table's size are up to the page:

* Selector rules run only on pages of at most ``STRUCTURED_MAX_ELEMENTS`` elements; on a bigger
  page they are skipped, their fields stay missing, with the warning
  ``selectors_skipped_large_page`` (``//*//*//*`` is quadratic in the number of elements). This
  is policy; the bound is the selector child's deadline, after which its fields stay missing
  with ``selectors_timed_out`` (``selectors_failed`` for a child that crashed or ran out of
  memory).
* A value is cut to ``MAX_VALUE_CHARS`` characters; a rule's field takes at most
  ``MAX_FIELD_VALUES`` values and ``MAX_FIELD_CHARS`` characters in all, then it is cut with the
  warning ``structured_truncated``.
* At most ``MAX_TABLES`` tables of at most ``MAX_TABLE_ROWS`` rows of at most
  ``MAX_TABLE_COLUMNS`` cells; ``colspan`` counts 1 to ``MAX_COLSPAN``; all tables together at
  most ``MAX_TABLES_CHARS`` characters of cell text, then they stop with the warning
  ``tables_truncated``.

Here the page is parsed once by lxml (the tree ``metadata`` made, for tables and the element
count); a selector child parses it again for its rules (``rules.py``).

``patterns`` and ``llm`` run later, in the post stage, on the final content; fields the DOM
stage did not fill stay missing (``values[field] = None``, listed in ``missing``).

A request with ``pii`` has no DOM stage (``dom_options``, ruling R12): the stage reads the raw
page, which the nlp step never masks.
"""

import hashlib
import html as html_lib
import json
from collections.abc import Iterator, Mapping
from itertools import islice
from typing import Any

from lxml import etree
from lxml.html import HtmlElement

from text_extraction.licenses import License
from text_extraction.metadata import PageMetadata, StructuredItems, page_tree
from text_extraction.pii.types import PiiOptions
from text_extraction.structured.rules import MAX_FIELD_CHARS as MAX_FIELD_CHARS
from text_extraction.structured.rules import MAX_FIELD_VALUES as MAX_FIELD_VALUES
from text_extraction.structured.rules import MAX_VALUE_CHARS as MAX_VALUE_CHARS
from text_extraction.structured.rules import Found, RuleRequest, bounded
from text_extraction.structured.schema import CompiledSchema, coerce
from text_extraction.structured.types import (
    SelectorRule,
    Source,
    StructuredOptions,
    StructuredResult,
    Table,
)

STRUCTURED_MAX_ELEMENTS = 50_000  # selector rules run on pages of at most this many elements
MAX_TABLES = 50
MAX_TABLE_ROWS = 1_000
MAX_TABLE_COLUMNS = 100  # cells of a row, colspan included
MAX_COLSPAN = 20
MAX_TABLES_CHARS = 1_000_000  # cell text of all tables together
SELECTORS_SKIPPED_WARNING = "selectors_skipped_large_page"
STRUCTURED_TRUNCATED_WARNING = "structured_truncated"
TABLES_TRUNCATED_WARNING = "tables_truncated"
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


def dom_options(
    structured: StructuredOptions | None, pii: PiiOptions | None
) -> StructuredOptions | None:
    """The ``structured`` options the DOM stage works with: none for a request with ``pii``
    (ruling R12, D3 a). The DOM stage reads the raw page (its metadata, the caller's and the
    learned selectors, its tables), which the nlp step never masks: with ``pii`` only
    ``patterns`` and the LLM fill fields, on the masked content. The request then has no DOM
    stage, no learned selectors are read, and its cache key is the one of a request without
    DOM work."""
    return None if pii is not None else structured


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


def selector_rules(
    options: StructuredOptions,
    learned: Mapping[str, SelectorRule] | None,
    items: StructuredItems,
    warnings: list[str],
) -> dict[str, SelectorRule]:
    """The selector rules the DOM stage runs on this page, by field: the caller's (strategy
    ``selectors`` or ``auto``) over the usable ``learned`` ones. Empty (``{}``) without a
    compiled schema, and on a page over ``STRUCTURED_MAX_ELEMENTS`` elements: then
    ``selectors_skipped_large_page`` is appended to ``warnings``."""
    compiled = options.compiled
    if compiled is None:
        return {}
    caller = options.selectors if options.strategy in ("selectors", "auto") else {}
    rules = {
        name: rule
        for name, rule in {**usable_learned(options, learned or {}), **caller}.items()
        if name in compiled.fields
    }
    if rules and _too_large(items.tree):
        warnings.append(SELECTORS_SKIPPED_WARNING)
        return {}
    return rules


def _take_all(compiled: CompiledSchema, name: str, rule: SelectorRule) -> bool:
    return rule.all if rule.all is not None else _is_array(compiled, name)


def rule_requests(compiled: CompiledSchema, rules: Mapping[str, SelectorRule]) -> list[RuleRequest]:
    """``rules`` as a selector child runs them (``rules.run_rules``)."""
    return [
        RuleRequest(
            field=name,
            css=rule.css,
            xpath=rule.xpath,
            attr=rule.attr,
            all=_take_all(compiled, name, rule),
        )
        for name, rule in rules.items()
    ]


def rule_value(
    compiled: CompiledSchema, name: str, rule: SelectorRule, found: Found | None
) -> tuple[Any | None, bool]:
    """The field's value from what ``rule`` ``found`` (None: nothing, or the rule did not run)
    and whether it was cut to the field's limits."""
    if found is None:
        return None, False
    values, truncated = found
    if not values:
        return None, truncated
    raw = values if _take_all(compiled, name, rule) else values[0]
    return _cut(coerce(compiled, name, raw)), truncated


def dom_stage(
    html: str,
    final_url: str,
    options: StructuredOptions,
    metadata: PageMetadata | None,
    items: StructuredItems,
    *,
    learn: bool,
    learn_max_chars: int,
    rules: Mapping[str, SelectorRule] | None = None,
    found: Mapping[str, Found] | None = None,
    warnings: list[str] | None = None,
) -> StructuredResult:
    """The DOM strategies of ``options`` on the page ``html`` (final URL ``final_url``).
    ``metadata`` are the page's ``PageMetadata`` (the last source of ``metadata``; None: not
    used), ``items`` its ``StructuredItems`` (for ``metadata``: with the syntaxes read),
    ``rules`` the selector rules that ran (``selector_rules``) and ``found`` what they found (by
    field, from a selector child). With ``learn`` the result carries ``learn_html`` (cut to
    ``learn_max_chars``). The stage's warnings (``structured_truncated``, ``tables_truncated``)
    are appended to ``warnings``. Never raises for a rule."""
    noted: list[str] = warnings if warnings is not None else []
    compiled = options.compiled
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    if compiled is not None:
        ran = rules or {}
        answers = found or {}
        caller = options.selectors if options.strategy in ("selectors", "auto") else {}
        uses_metadata = reads_metadata(options)
        truncated = False
        for name in compiled.fields:
            value: Any = None
            source: Source | None = None
            if name in ran:
                value, cut = rule_value(compiled, name, ran[name], answers.get(name))
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


def page_too_large(html: str) -> bool:
    """Whether the page ``html`` has more than ``STRUCTURED_MAX_ELEMENTS`` elements: then no
    rule runs on it (``learn_selectors`` checks ``learn_html`` with it)."""
    return _too_large(page_tree(html))


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
        text = bounded(cell.itertext())
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
        caption_text = bounded(caption.itertext()) if caption is not None else ""
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
