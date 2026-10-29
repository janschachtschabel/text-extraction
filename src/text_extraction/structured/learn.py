"""``learn_selectors`` (rulings S7, R4; Review Focus 1): CSS selectors the LLM proposes for the
values it found, kept only when they reproduce those values.

One LLM call shows the model the values (``<values>``) and the cleaned page (``learn_html``,
``<document>``) and asks for one ``{"css": str, "attr": str | null}`` per field, nullable
(strict schema). Each proposal must be a valid ``SelectorRule`` (CSS only: learned rules are
never XPath), keep to the limits of a non-admin key (``checks.css_within_limits``), and, run on
``learn_html`` exactly as the DOM stage runs rules (``dom.RulePage``), give a value equal to the
LLM's: strings compared with whitespace collapsed and case folded, arrays element by element,
objects field by field. A selector such as ``*`` that a page talks the model into selects the
whole page, which is not the value: it is rejected. Nothing from the answer is fetched or
executed; a proposal that fails a check is rejected without a repair call. Storing the kept
rules (for the caller's own key, S8) is the post stage's part. Selectors and values are never
logged.
"""

from collections.abc import Mapping
from functools import partial
from typing import Any, Final

from pydantic import BaseModel, TypeAdapter, ValidationError

from text_extraction.llm.client import LlmClient
from text_extraction.llm.usage import LlmUsage
from text_extraction.structured.checks import css_within_limits
from text_extraction.structured.dom import RulePage
from text_extraction.structured.prompts import LEARN_SYSTEM, learn_message
from text_extraction.structured.schema import CompiledSchema
from text_extraction.structured.types import LearnReport, SelectorRule

__all__ = ["learn"]


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
    if proposal is None or not css_within_limits(proposal.css):
        return None
    try:
        return SelectorRule(css=proposal.css, attr=proposal.attr)
    except ValidationError:
        return None


async def learn(
    client: LlmClient,
    html: str,
    compiled: CompiledSchema,
    values: Mapping[str, Any],
    *,
    base_url: str | None = None,
) -> tuple[dict[str, SelectorRule], LearnReport, LlmUsage]:
    """The rules that reproduce ``values`` (the LLM's, by field; None: no value) on ``html``
    (``learn_html``), the report (``stored`` names the kept fields, which the post stage still
    has to store; ``rejected`` the others) and the LLM's usage. ``base_url`` is the page's
    final URL, against which ``href`` and ``src`` values are made absolute. No call without a
    value. Raises ``LlmError`` when the call fails."""
    fields = [name for name in compiled.fields if values.get(name) is not None]
    if not fields:
        empty = LlmUsage(model="", calls=0, prompt_tokens=0, completion_tokens=0)
        return {}, LearnReport(stored=[], rejected=[]), empty
    asked = {name: values[name] for name in fields}
    proposals, usage = await client.chat_json(
        LEARN_SYSTEM,
        learn_message(html, asked),
        _schema(fields),
        validate=partial(_proposals, fields),
    )
    page = RulePage(html, base_url or "")
    kept: dict[str, SelectorRule] = {}
    rejected: list[str] = []
    for name in fields:
        rule = _rule(proposals.get(name))
        found = page.value(compiled, name, rule) if rule is not None else None
        if rule is not None and found is not None and _normal(found) == _normal(asked[name]):
            kept[name] = rule
        else:
            rejected.append(name)
    return kept, LearnReport(stored=list(kept), rejected=rejected), usage
