"""``learn_selectors`` (rulings S7, R4; Review Focus 1): CSS selectors the LLM proposes for the
values it found, kept only when they reproduce those values.

One LLM call shows the model the values (``<values>``) and the cleaned page (``learn_html``,
``<document>``) and asks for one ``{"css": str, "attr": str | null}`` per field, nullable
(strict schema). Each proposal must be a valid ``SelectorRule`` (CSS only: learned rules are
never XPath), keep to the limits of a non-admin key (``checks.css_within_limits``), not be
positional, and, run on ``learn_html`` exactly as the DOM stage runs rules (``rules.run_rules``,
read with ``dom.rule_value``), give a value equal to the LLM's: strings compared with whitespace
collapsed and case folded, arrays element by element, objects field by field. A selector such as
``*`` that a page talks the model into selects the whole page, which is not the value: it is
rejected. Nothing from the answer is fetched or executed; a proposal that fails a check is rejected
without a repair call. Storing the kept rules (for the caller's own key, S8) is the post stage's
part. Selectors and values are never logged.

The proposals are selectors the LLM chose, run on HTML a page shaped: they run in a selector
child (``selector_pool``, ruling R19), all in one run, killed at
``STRUCTURED_SELECTOR_TIMEOUT_SECONDS``; a run that does not finish (or whose child fails) keeps
none of them (``LearnCheckFailedError``, the post stage's ``selector_learning_failed``). Counting
``learn_html``'s elements and comparing the values is CPU work on the post pool
(``POST_CONCURRENCY``, R21), never on the event loop; the proposals run only on a page of at most
``STRUCTURED_MAX_ELEMENTS`` elements, like every other selector run. A bigger page raises
``LearnPageTooLargeError`` before the LLM call (the post stage reports
``selectors_skipped_large_page``). Positional proposals (``checks.positional``, M-d) are
rejected: ``learn_html`` lacks the page's scripts, styles, SVG and comments, so sibling
positions there are not the page's.
"""

from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, TypeAdapter, ValidationError

from text_extraction.conversion_pool import ConversionPool
from text_extraction.llm.usage import LlmUsage
from text_extraction.structured.checks import css_within_limits, positional
from text_extraction.structured.dom import page_too_large, rule_requests, rule_value
from text_extraction.structured.prompts import LEARN_SYSTEM, learn_message
from text_extraction.structured.rules import Found
from text_extraction.structured.schema import CompiledSchema
from text_extraction.structured.selector_pool import SelectorRunner
from text_extraction.structured.types import LearnReport, SelectorRule

if TYPE_CHECKING:  # the OpenAI SDK only where a client is made (``llm/basics.py``)
    from text_extraction.llm.client import LlmClient

__all__ = ["LearnCheckFailedError", "LearnPageTooLargeError", "learn"]


class LearnPageTooLargeError(Exception):
    """``learn_html`` has more than ``STRUCTURED_MAX_ELEMENTS`` elements: nothing is learned."""


class LearnCheckFailedError(Exception):
    """The proposals' run did not finish in time, or its child failed: nothing is learned.
    ``usage`` is the learning call's."""

    def __init__(self, usage: LlmUsage) -> None:
        super().__init__("the learned selectors could not be checked")
        self.usage = usage


class _Proposal(BaseModel, extra="ignore"):
    css: str
    attr: str | None = None


_PROPOSALS: Final = TypeAdapter(dict[str, _Proposal | None])
_PROPOSAL_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {"css": {"type": "string"}, "attr": {"type": ["string", "null"]}},
    "required": ["css", "attr"],
    "additionalProperties": False,
}


def _schema(fields: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: dict(_PROPOSAL_SCHEMA) for name in fields},
        "required": fields,
        "additionalProperties": False,
    }


def _proposals(fields: list[str], parsed: Any) -> dict[str, _Proposal | None]:
    """The answer's proposal for each field (None: none); raises ``ValueError`` (a
    ``ValidationError`` included) for an answer of another shape, which gets the repair call."""
    if not isinstance(parsed, dict):
        raise ValueError("The answer must be a JSON object.")
    return _PROPOSALS.validate_python({name: parsed.get(name) for name in fields})


def _normal(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if isinstance(value, list):
        return [_normal(item) for item in value]
    if isinstance(value, dict):
        return {key: _normal(item) for key, item in value.items()}
    return value


def _rule(proposal: _Proposal | None) -> SelectorRule | None:
    """The proposal as a rule; None for one that is invalid, over a non-admin key's limits or
    positional (M-d: ``learn_html`` lacks the scripts, styles, SVG and comments of the page,
    so sibling positions there are not the page's)."""
    if proposal is None or not css_within_limits(proposal.css):
        return None
    try:
        rule = SelectorRule(css=proposal.css, attr=proposal.attr)
    except ValidationError:
        return None
    return None if positional(rule) else rule


def _kept(
    compiled: CompiledSchema,
    asked: Mapping[str, Any],
    rules: Mapping[str, SelectorRule],
    found: Mapping[str, Found],
) -> tuple[dict[str, SelectorRule], list[str]]:
    """The rules whose value (from what they ``found`` on the page) is the one asked, and the
    fields rejected."""
    kept: dict[str, SelectorRule] = {}
    rejected: list[str] = []
    for name, value in asked.items():
        rule = rules.get(name)
        got = rule_value(compiled, name, rule, found.get(name))[0] if rule is not None else None
        if rule is not None and got is not None and _normal(got) == _normal(value):
            kept[name] = rule
        else:
            rejected.append(name)
    return kept, rejected


async def learn(
    client: "LlmClient",
    html: str,
    compiled: CompiledSchema,
    values: Mapping[str, Any],
    *,
    base_url: str | None = None,
    selectors: SelectorRunner,
    threads: ConversionPool,
) -> tuple[dict[str, SelectorRule], LearnReport, LlmUsage]:
    """The rules that reproduce ``values`` (the LLM's, by field; None: no value) on ``html``
    (``learn_html``), the report (``stored`` names the kept fields, which the post stage still
    has to store; ``rejected`` the others) and the LLM's usage. ``base_url`` is the page's
    final URL, against which ``href`` and ``src`` values are made absolute; ``selectors`` runs
    the proposals, ``threads`` (the post pool) the rest of the CPU work. No call without a
    value. Raises ``LearnPageTooLargeError`` (before any call) for a page over
    ``STRUCTURED_MAX_ELEMENTS`` elements, ``LlmError`` when the call fails and
    ``LearnCheckFailedError`` when the proposals' run fails."""
    fields = [name for name in compiled.fields if values.get(name) is not None]
    if not fields:
        empty = LlmUsage(model="", calls=0, prompt_tokens=0, completion_tokens=0)
        return {}, LearnReport(stored=[], rejected=[]), empty
    asked = {name: values[name] for name in fields}
    if await threads.run(page_too_large, html):
        raise LearnPageTooLargeError
    proposals, usage = await client.chat_json(
        LEARN_SYSTEM,
        learn_message(html, asked),
        _schema(fields),
        validate=partial(_proposals, fields),
    )
    rules = {name: rule for name in fields if (rule := _rule(proposals.get(name))) is not None}
    outcome = await selectors.run(html, base_url or "", rule_requests(compiled, rules))
    if outcome.warning is not None:
        raise LearnCheckFailedError(usage)
    kept, rejected = await threads.run(_kept, compiled, asked, rules, outcome.found)
    return kept, LearnReport(stored=list(kept), rejected=rejected), usage
