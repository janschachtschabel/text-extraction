"""The ``DocumentPool`` target of the selector children: ``DocumentPool(target=
"text_extraction.structured.selector_child")`` (ruling R19).

The child imports this module (and with it ``structured.rules``: BeautifulSoup, soupsieve, lxml)
once, after its memory limit is set; no settings and no keys. ``FUNCTIONS`` is the allowlist of
what a parent may call by name. Logging is silenced first (``child_logging``): nothing a child
logs may quote a page or a selector.
"""

from collections.abc import Callable
from typing import Any

from text_extraction.documents.child_logging import silence_logging

silence_logging()

from text_extraction.structured import rules  # noqa: E402 - after the silencing

FUNCTIONS: dict[str, Callable[[Any], Any]] = {
    "rules": rules.run_rules_payload,
}
