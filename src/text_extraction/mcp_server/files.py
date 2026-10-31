"""Files over MCP (D3 a, ruling P10-S5): ``extract_file`` takes the file as base64, optionally
behind a ``data:<type>;base64,`` prefix, within the upload route's limit."""

import base64
import binascii
import re

from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError

INVALID_BASE64_MESSAGE = "content_base64 is not valid base64."
# The longest content type a ``data:`` prefix may carry, as for the ``content_type`` argument.
MAX_CONTENT_TYPE_CHARS = 255

_WHITESPACE = re.compile(r"\s+")
_DATA_PREFIX = re.compile(r"data:([^,]*);base64,", re.IGNORECASE)


def _invalid() -> ServiceError:
    return ServiceError(ErrorCode.INVALID_REQUEST, INVALID_BASE64_MESSAGE, 422)


def _too_large(limit: int) -> ServiceError:
    return ServiceError(ErrorCode.INVALID_REQUEST, f"The file is larger than {limit} bytes.", 413)


def decode_file(value: str, limit: int) -> tuple[bytes, str | None]:
    """The bytes of a base64 file and the content type of its ``data:`` prefix (None without
    one, or with an empty type). Whitespace (line breaks) is ignored. ``invalid_request`` for
    anything that is not base64, and for a file over ``limit`` bytes, which is refused from its
    length before it is decoded."""
    content_type: str | None = None
    if value[:5].lower() == "data:":
        prefix = _DATA_PREFIX.match(value)
        if prefix is None:
            raise _invalid()
        content_type = prefix.group(1).strip() or None
        if content_type is not None and len(content_type) > MAX_CONTENT_TYPE_CHARS:
            raise _invalid()
        value = value[prefix.end() :]
    text = _WHITESPACE.sub("", value)
    if len(text) % 4:
        raise _invalid()
    padding = len(text) - len(text.rstrip("="))
    if (len(text) // 4) * 3 - padding > limit:
        raise _too_large(limit)
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid() from None
    return data, content_type
