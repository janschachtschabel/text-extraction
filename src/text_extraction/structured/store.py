"""Learned selectors in Valkey (ruling S8).

``learn_selectors`` (S7) keeps the CSS selectors that reproduced the LLM's values, per API key
(the owner, D2 a: no key can change another's results), host and schema: the key is
``sel:{<owner>}:<host>:<schema_id>`` (the braces are a literal cluster hash tag), the value a
JSON object of ``SelectorRule`` objects by field name. Each write renews the TTL
(``SELECTOR_LEARN_TTL_SECONDS``, 30 days); reads do not. A later write merges per field.

The host is the request URL's (``selector_host``), lower-cased: the pipeline reads with it
before the page is fetched, and the post stage writes with it.

A Valkey problem on a read raises ``LearnedSelectorsUnavailableError``, which the pipeline
turns into no learned rules and the warning ``learned_selectors_unavailable``; on a write the
``RedisError`` (or ``OSError``) propagates, and the post stage reports ``learned.stored: []``.
A stored value that cannot be read any more (another shape) counts as no rules. Owners, hosts,
selectors and values are never logged.
"""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from text_extraction.structured.types import SelectorRule

_log = structlog.get_logger(__name__)

LEARNED_SELECTORS_UNAVAILABLE_WARNING = "learned_selectors_unavailable"
_WRITE_ATTEMPTS = 5  # optimistic merges; another writer of the same key makes one retry


class LearnedSelectorsUnavailableError(Exception):
    """The learned selectors could not be read now (Valkey failed)."""


def selector_key(owner: str, host: str, schema_id: str) -> str:
    return f"sel:{{{owner}}}:{host.lower()}:{schema_id}"


def selector_host(url: str) -> str | None:
    """The host learned selectors of ``url`` are filed under: its lower-case host name, or
    None (an upload, or a URL without a host)."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _decode(raw: Any) -> dict[str, SelectorRule]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    rules: dict[str, SelectorRule] = {}
    for name, value in data.items():
        try:
            rules[str(name)] = SelectorRule.model_validate(value)
        except ValidationError:
            continue  # a rule this release cannot read: as if it were not there
    return rules


def _encode(rules: Mapping[str, SelectorRule]) -> str:
    return json.dumps(
        {name: rule.model_dump(mode="json", exclude_none=True) for name, rule in rules.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class SelectorStore:
    """Read and merge learned ``SelectorRule`` objects; see the module docstring."""

    def __init__(self, valkey: Redis, ttl_seconds: int) -> None:
        self._valkey = valkey
        self._ttl_seconds = ttl_seconds

    async def get(self, owner: str, host: str, schema_id: str) -> dict[str, SelectorRule]:
        """The rules ``owner`` learned for ``host`` and the schema; empty when there are none.
        Raises ``LearnedSelectorsUnavailableError`` when Valkey fails."""
        try:
            raw = await self._valkey.get(selector_key(owner, host, schema_id))
        except (RedisError, OSError) as error:
            _log.warning("learned_selectors_read_failed", error=type(error).__name__)
            raise LearnedSelectorsUnavailableError from None
        return {} if raw is None else _decode(raw)

    async def put(
        self, owner: str, host: str, schema_id: str, rules: Mapping[str, SelectorRule]
    ) -> None:
        """Merge ``rules`` into the stored ones (a field's new rule replaces its old one) and
        renew the TTL. Raises ``RedisError`` or ``OSError`` when Valkey fails."""
        if not rules:
            return
        key = selector_key(owner, host, schema_id)
        async with self._valkey.pipeline(transaction=True) as pipe:
            for attempt in range(_WRITE_ATTEMPTS):
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    merged = {**(_decode(raw) if raw is not None else {}), **rules}
                    pipe.multi()  # type: ignore[no-untyped-call]
                    pipe.set(key, _encode(merged), ex=self._ttl_seconds)
                    await pipe.execute()
                    return
                except WatchError:
                    if attempt == _WRITE_ATTEMPTS - 1:
                        raise
                    continue
