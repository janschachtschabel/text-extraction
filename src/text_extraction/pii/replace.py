"""The replacement modes: what an entity becomes in the text, and the key of ``hash``.

Replacements are plain brackets in every format, so that Markdown does not read them as HTML.
Pure and free of Presidio and spaCy.
"""

import hashlib
import hmac
import os
from collections.abc import Sequence

from text_extraction.pii.types import HashScope, PiiMode, Span
from text_extraction.settings import Settings

HASH_HEX_CHARS = 12
_KEY_BYTES = 32


def hash_key(settings: Settings) -> tuple[bytes, HashScope]:
    """The key to hash one result with, and whose it is (decision D3).

    With ``PII_HASH_KEY`` the same value gets the same hash in every result of the service
    (scope ``service``), so documents can be linked without the value. Without it the key is
    random and used for this result only (scope ``result``): equal values match inside the
    result, and nobody can try candidates against the hashes afterwards. Call it once per
    result.
    """
    if settings.pii_hash_key is not None:
        return settings.pii_hash_key.get_secret_value().encode(), "service"
    return os.urandom(_KEY_BYTES), "result"


def _normalised(value: str) -> str:
    """Case-folded, every run of whitespace one space, no blanks at the ends: ``Anna  Müller``
    and ``anna müller`` are the same person."""
    return " ".join(value.casefold().split())


class Replacer:
    """Turns found entities into their replacement text for one mode and one key."""

    def __init__(self, mode: PiiMode, key: bytes) -> None:
        self._mode = mode
        self._key = key

    def placeholder(self, span: Span, value: str) -> str:
        """The text that stands for ``value``, the entity ``span`` found."""
        match self._mode:
            case "mask":
                return f"[{span.type}]"
            case "redact":
                return "[REDACTED]"
            case "hash":
                message = f"{span.type}:{_normalised(value)}".encode()
                digest = hmac.new(self._key, message, hashlib.sha256).hexdigest()
                return f"[{span.type}:{digest[:HASH_HEX_CHARS]}]"
            case _:
                raise ValueError("annotate leaves the text as it is: it has no placeholder")

    def apply(self, text: str, spans: Sequence[Span]) -> str:
        """``text`` with each span replaced. The spans are sorted and do not overlap, as
        ``spans.resolve`` returns them; their offsets are code points (Python string indices).
        ``annotate`` returns the text unchanged."""
        if self._mode == "annotate":
            return text
        # One pass over the spans (the same text as replacing them right to left, without
        # copying a long text once per span).
        parts: list[str] = []
        position = 0
        for span in spans:
            parts.append(text[position : span.start])
            parts.append(self.placeholder(span, text[span.start : span.end]))
            position = span.end
        parts.append(text[position:])
        return "".join(parts)
