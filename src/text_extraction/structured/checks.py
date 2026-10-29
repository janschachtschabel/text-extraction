"""Request checks of ``structured`` that need more than the options themselves (ruling R4).

The options are validated when the request is parsed (``structured/types.py``); what depends on
the caller is checked where the request is handled, by the extract, file and batch routes:

* An XPath rule needs an admin key: XPath 1.0 has no cost limit lxml could enforce, and an
  expression such as ``//*//*//*`` runs for minutes on a large page (rule 5: no evaluation of
  caller code unless the caller is admin). Other keys use CSS.
* A CSS rule of a non-admin key may use ``:has(`` at most once, which bounds the nesting of
  relational selectors. A selector the LLM proposes for ``learn_selectors`` keeps to the same
  limit (``css_within_limits``), since a learned rule later runs for that key.

The messages never repeat a selector.
"""

import re

from text_extraction.auth import Principal
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.structured.types import StructuredOptions

XPATH_NEEDS_ADMIN_MESSAGE = "XPath selectors need an admin key; use CSS."
HAS_LIMIT_MESSAGE = "A CSS selector may use :has() at most once without an admin key."
MAX_HAS = 1
_HAS = re.compile(r":has\s*\(", re.IGNORECASE)


def css_within_limits(css: str) -> bool:
    """Whether a CSS selector keeps to the limits of a non-admin key: ``:has(`` at most
    ``MAX_HAS`` times."""
    return len(_HAS.findall(css)) <= MAX_HAS


def check_selector_access(structured: StructuredOptions | None, principal: Principal) -> None:
    """422 ``invalid_request`` for a non-admin key's XPath rule or a CSS rule with more than
    one ``:has(``."""
    if structured is None or principal.role == "admin":
        return
    for rule in structured.selectors.values():
        if rule.xpath is not None:
            raise ServiceError(ErrorCode.INVALID_REQUEST, XPATH_NEEDS_ADMIN_MESSAGE, 422)
        if rule.css is not None and not css_within_limits(rule.css):
            raise ServiceError(ErrorCode.INVALID_REQUEST, HAS_LIMIT_MESSAGE, 422)
