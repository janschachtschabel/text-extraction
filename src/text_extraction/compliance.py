"""Respect for the target site's wishes: robots.txt, TDMRep, ai.txt and ``noai``.

The pipeline calls this module three times per request:

1. ``resolve_mode`` turns the caller's ``compliance`` option into the mode that applies.
2. ``ComplianceChecker.before_fetch`` reads the three files of the target's origin (robots.txt,
   ``/.well-known/tdmrep.json``, ai.txt) and decides for the URL, before the page is fetched.
3. ``ComplianceChecker.after_fetch`` adds what only the page itself can tell: the
   ``tdm-reservation`` and ``tdm-policy`` headers, the same two ``<meta>`` tags and
   ``noai``/``noml`` in ``X-Robots-Tag`` or ``<meta name="robots">``.

``blocking_code`` is asked after both steps; it names the error to answer with, and only in
``enforce`` mode. In ``report`` mode nothing blocks, but the report says what would have. In
``ignore`` mode nothing is fetched or checked at all.

robots.txt follows RFC 9309 (via protego) with the product token ``text-extraction``:

* 2xx: the rules apply;
* 4xx: no rules, everything is allowed. 429 is the exception: it means "try later", so it is
  treated like a server error;
* 5xx, any other status and any fetch error: ``unavailable``, which ``enforce`` treats as
  disallowed. The body of a robots.txt over the 512 KB fetch limit is not available to us
  (the fetcher raises ``too_large`` without a body), so that case is ``unavailable`` too.

``llms.txt`` is a hint for models, not a restriction, and is never fetched.

State lives in Valkey, one key per origin and file: ``cmp:{kind}:{<origin>}`` (the braces are a
literal cluster hash tag) holding JSON ``{"state", "body"}``. The state is ``found`` (2xx, with
the raw text), ``absent`` (4xx) or ``unavailable`` (5xx, 429 or a fetch error). The first two live
``compliance_cache_seconds``; ``unavailable`` lives at most five minutes, so a short outage of a
site does not block it for a whole cache period.
"""

import asyncio
import contextlib
import json
import math
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit

import structlog
from protego import Protego
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.auth import Principal
from text_extraction.envelope import ErrorCode
from text_extraction.errors import ServiceError
from text_extraction.fetch import Fetcher, FetchError, FetchResult
from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

ComplianceMode = Literal["enforce", "report", "ignore"]

PRODUCT_TOKEN = "text-extraction"  # noqa: S105 - the robots.txt user-agent token, not a secret

_FILE_MAX_BYTES = 512_000
_FILE_TIMEOUT_SECONDS = 10
# A cached "unavailable" answer is kept at most this long.
_UNAVAILABLE_TTL_SECONDS = 300
# Only the head of a page can carry the meta tags we read.
_META_SCAN_CHARS = 262_144
_MAX_POLICY_CHARS = 2048
_DEFAULT_PORTS = {"http": 80, "https": 443}

_NOAI_DIRECTIVES = frozenset({"noai", "noml"})
# X-Robots-Tag directives that take a value and are not the name of a bot.
_VALUE_DIRECTIVES = frozenset(
    {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}
)
_BOT_NAME = re.compile(r"[a-z0-9_.*-]+")

_CacheState = Literal["found", "absent", "unavailable"]
_CACHE_STATES: frozenset[str] = frozenset({"found", "absent", "unavailable"})


class ComplianceReport(BaseModel):
    """What the compliance checks found for one URL. Goes into the result envelope."""

    mode: ComplianceMode
    decision: Literal["allowed", "blocked", "reported", "skipped"] = "allowed"
    robots: Literal["allowed", "disallowed", "unavailable", "not_checked"] = "not_checked"
    crawl_delay: float | None = None  # seconds, from robots.txt
    ai_txt: Literal["allowed", "disallowed", "absent", "not_checked"] = "not_checked"
    tdm_reserved: bool | None = None  # None: no TDM statement found
    tdm_policy: str | None = None
    tdm_source: Literal["tdmrep.json", "header", "meta"] | None = None
    noai: bool = False
    reasons: list[str] = Field(default_factory=list)


def resolve_mode(
    requested: ComplianceMode | None, principal: Principal, settings: Settings
) -> ComplianceMode:
    """The mode that applies: the caller's choice if they may make it, else a 403.

    Admins may choose any mode. Others get the default, may ask for it explicitly, and may ask
    for another mode only while ``compliance_allow_override`` is on. ``ignore`` is for admins
    (or an operator who made it the default).
    """
    default = settings.compliance_default
    if requested is None:
        return default
    if requested == default or principal.role == "admin":
        return requested
    if requested == "ignore":
        raise ServiceError(
            ErrorCode.FORBIDDEN, "Only administrators may switch the compliance checks off.", 403
        )
    if not settings.compliance_allow_override:
        raise ServiceError(ErrorCode.FORBIDDEN, "Changing the compliance mode is not allowed.", 403)
    return requested


def _violation(report: ComplianceReport) -> ErrorCode | None:
    """The error this report would block with, whatever the mode."""
    if report.robots in ("disallowed", "unavailable"):
        return ErrorCode.BLOCKED_BY_ROBOTS
    if report.tdm_reserved is True or report.noai or report.ai_txt == "disallowed":
        return ErrorCode.TDM_RESERVED
    return None


def blocking_code(report: ComplianceReport) -> ErrorCode | None:
    """The error code to fail with, or ``None``. Only ``enforce`` blocks."""
    return _violation(report) if report.mode == "enforce" else None


def _decided(report: ComplianceReport) -> ComplianceReport:
    """The report with ``decision`` set from its findings and mode."""
    if report.mode == "ignore":
        decision = "skipped"
    elif _violation(report) is None:
        decision = "allowed"
    else:
        decision = "blocked" if report.mode == "enforce" else "reported"
    return report.model_copy(update={"decision": decision})


@dataclass(frozen=True)
class _Origin:
    """A site's origin. ``key`` always has the port; ``base`` leaves out a default port."""

    key: str
    base: str


@dataclass(frozen=True)
class _Entry:
    """One origin file as cached: its state and, when found, its text."""

    state: _CacheState
    body: str = ""


class ComplianceChecker:
    """Reads and caches the compliance files of an origin and judges URLs against them."""

    def __init__(self, fetcher: Fetcher, valkey: Redis, settings: Settings) -> None:
        self._fetcher = fetcher
        self._valkey = valkey
        self._settings = settings

    async def before_fetch(self, url: str, mode: ComplianceMode) -> ComplianceReport:
        """Check robots.txt, TDMRep and ai.txt for ``url``; fetches nothing in ``ignore`` mode."""
        if mode == "ignore":
            return ComplianceReport(mode=mode, decision="skipped")

        origin = _origin(url)
        async with asyncio.TaskGroup() as group:
            robots_task = group.create_task(self._load("robots", origin, "/robots.txt"))
            tdmrep_task = group.create_task(
                self._load("tdmrep", origin, "/.well-known/tdmrep.json")
            )
            ai_task = group.create_task(self._load("ai", origin, "/ai.txt"))

        report = ComplianceReport(mode=mode)
        report = self._apply_robots(report, robots_task.result(), url)
        report = self._apply_tdmrep(report, tdmrep_task.result(), url)
        report = self._apply_ai_txt(report, ai_task.result(), url)
        return _decided(report)

    def after_fetch(
        self, report: ComplianceReport, fetched: FetchResult, html: str | None
    ) -> ComplianceReport:
        """Add the page's own TDM statements and ``noai``/``noml`` to ``report``.

        Returns a new report; ``report`` is not changed. A reservation found earlier (tdmrep.json)
        stays; a reservation of the page is added to an open or missing one, since a TDM opt-out
        from any source counts.
        """
        if report.mode == "ignore":
            return report

        metas = _head_metas(html)
        found: list[tuple[bool, str | None, Literal["header", "meta"]]] = []
        header_value = _parse_reservation(fetched.headers.get("tdm-reservation"))
        if header_value is not None:
            found.append((header_value, _policy(fetched.headers.get("tdm-policy")), "header"))
        meta_value = _parse_reservation(_first_content(metas, "tdm-reservation"))
        if meta_value is not None:
            found.append((meta_value, _policy(_first_content(metas, "tdm-policy")), "meta"))

        reasons = list(report.reasons)
        update: dict[str, object] = {}
        reserving = next((item for item in found if item[0]), None)
        if report.tdm_reserved is not True and reserving is not None:
            _, policy, source = reserving
            update.update(tdm_reserved=True, tdm_policy=policy, tdm_source=source)
            reasons.append(f"The page reserves text and data mining rights ({source}).")
        elif report.tdm_reserved is None and found:
            _, policy, source = found[0]
            update.update(tdm_reserved=False, tdm_policy=policy, tdm_source=source)

        if not report.noai and _has_noai(fetched.headers.get("x-robots-tag"), metas):
            update["noai"] = True
            reasons.append("The page asks not to be used for AI (noai or noml).")

        update["reasons"] = reasons
        return _decided(report.model_copy(update=update))

    # --- applying one file to the report -------------------------------------------------------

    def _apply_robots(self, report: ComplianceReport, entry: _Entry, url: str) -> ComplianceReport:
        if entry.state == "absent":
            return report.model_copy(update={"robots": "allowed"})
        reasons = list(report.reasons)
        rules = _parse_rules(entry.body) if entry.state == "found" else None
        if rules is None:
            reasons.append("robots.txt could not be retrieved, so access cannot be confirmed.")
            return report.model_copy(update={"robots": "unavailable", "reasons": reasons})
        allowed = rules.can_fetch(url, PRODUCT_TOKEN)
        if not allowed:
            reasons.append(f"robots.txt disallows this URL for {PRODUCT_TOKEN}.")
        return report.model_copy(
            update={
                "robots": "allowed" if allowed else "disallowed",
                "crawl_delay": _crawl_delay(rules),
                "reasons": reasons,
            }
        )

    def _apply_tdmrep(self, report: ComplianceReport, entry: _Entry, url: str) -> ComplianceReport:
        if entry.state == "absent":
            return report
        reasons = list(report.reasons)
        matched = _match_tdmrep(entry.body, url) if entry.state == "found" else None
        if matched is None:
            if entry.state == "unavailable":
                reasons.append("tdmrep.json could not be retrieved.")
            return report.model_copy(update={"reasons": reasons})
        if matched.reserved:
            reasons.append("tdmrep.json reserves text and data mining rights for this URL.")
        return report.model_copy(
            update={
                "tdm_reserved": matched.reserved,
                "tdm_policy": matched.policy,
                "tdm_source": "tdmrep.json",
                "reasons": reasons,
            }
        )

    def _apply_ai_txt(self, report: ComplianceReport, entry: _Entry, url: str) -> ComplianceReport:
        reasons = list(report.reasons)
        rules = _parse_rules(entry.body) if entry.state == "found" else None
        if rules is None:
            if entry.state == "unavailable":
                reasons.append("ai.txt could not be retrieved.")
            return report.model_copy(update={"ai_txt": "absent", "reasons": reasons})
        if rules.can_fetch(url, PRODUCT_TOKEN):
            return report.model_copy(update={"ai_txt": "allowed"})
        reasons.append(f"ai.txt disallows this URL for {PRODUCT_TOKEN}.")
        return report.model_copy(update={"ai_txt": "disallowed", "reasons": reasons})

    # --- reading the origin files --------------------------------------------------------------

    async def _load(self, kind: str, origin: _Origin, path: str) -> _Entry:
        key = f"cmp:{kind}:{{{origin.key}}}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        entry = await self._fetch_file(origin.base + path)
        await self._cache_set(key, entry)
        return entry

    async def _fetch_file(self, file_url: str) -> _Entry:
        try:
            fetched = await self._fetcher.fetch(
                file_url, max_bytes=_FILE_MAX_BYTES, timeout_seconds=_FILE_TIMEOUT_SECONDS
            )
        except FetchError as error:
            _log.info("compliance_file_unavailable", code=str(error.code))
            return _Entry("unavailable")
        if 200 <= fetched.status < 300:
            return _Entry("found", fetched.content.decode("utf-8-sig", errors="replace"))
        if 400 <= fetched.status < 500 and fetched.status != 429:
            return _Entry("absent")
        return _Entry("unavailable")

    async def _cache_get(self, key: str) -> _Entry | None:
        try:
            raw = await self._valkey.get(key)
        except (RedisError, OSError) as error:
            _log.warning("compliance_cache_read_failed", error=type(error).__name__)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None  # unreadable entry: fetch again and overwrite it
        if not isinstance(data, dict) or data.get("state") not in _CACHE_STATES:
            return None
        body = data.get("body", "")
        return _Entry(data["state"], body) if isinstance(body, str) else None

    async def _cache_set(self, key: str, entry: _Entry) -> None:
        ttl = self._settings.compliance_cache_seconds
        if entry.state == "unavailable":
            ttl = min(ttl, _UNAVAILABLE_TTL_SECONDS)
        if ttl <= 0:
            return
        payload = json.dumps({"state": entry.state, "body": entry.body})
        try:
            await self._valkey.set(key, payload, ex=ttl)
        except (RedisError, OSError) as error:
            _log.warning("compliance_cache_write_failed", error=type(error).__name__)


# --- helpers -------------------------------------------------------------------------------------


def _origin(url: str) -> _Origin:
    """The origin of ``url``, lower-case, with IPv6 literals in brackets."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host:
        raise ValueError("compliance checks need an http(s) URL with a host")
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal
    port = parts.port or _DEFAULT_PORTS[scheme]
    base = f"{scheme}://{host}" + ("" if port == _DEFAULT_PORTS[scheme] else f":{port}")
    return _Origin(key=f"{scheme}://{host}:{port}", base=base)


def _parse_rules(text: str) -> Protego | None:
    """Parse robots.txt-style rules. protego is lenient; a parser failure counts as unusable."""
    try:
        return Protego.parse(text)
    except Exception:  # untrusted input: any parser failure means the file is unusable
        _log.warning("compliance_rules_unparsable")
        return None


def _crawl_delay(rules: Protego) -> float | None:
    delay = rules.crawl_delay(PRODUCT_TOKEN)
    if delay is None or not math.isfinite(delay) or delay < 0:
        return None
    return float(delay)


@dataclass(frozen=True)
class _TdmMatch:
    reserved: bool
    policy: str | None


def _parse_reservation(value: object) -> bool | None:
    """TDMRep reservation value: ``1`` or ``0`` as number or string; anything else is no answer."""
    if isinstance(value, bool):
        return value
    text = str(value).strip() if isinstance(value, int | str) else ""
    return {"1": True, "0": False}.get(text)


def _policy(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:_MAX_POLICY_CHARS] or None


def _location_matches(pattern: str, target: str) -> bool:
    """robots.txt-style path pattern: prefix match, ``*`` any text, a final ``$`` anchors the end.

    Done by hand instead of with a regular expression: the pattern comes from the target site, and
    a pattern full of ``*`` must not be able to make the match slow.
    """
    anchored = pattern.endswith("$")
    parts = (pattern[:-1] if anchored else pattern).split("*")
    if not target.startswith(parts[0]):
        return False
    position = len(parts[0])
    if len(parts) == 1:
        return target == parts[0] if anchored else True
    for part in parts[1:-1]:
        found = target.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    last = parts[-1]
    if anchored:
        return target.endswith(last) and len(target) - len(last) >= position
    return target.find(last, position) >= 0


def _match_tdmrep(body: str, url: str) -> _TdmMatch | None:
    """The first tdmrep.json entry that covers ``url`` and states a reservation."""
    try:
        entries = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(entries, list):
        return None
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        location = entry.get("location")
        reserved = _parse_reservation(entry.get("tdm-reservation"))
        if not isinstance(location, str) or reserved is None:
            continue
        if _location_matches(location, target):
            return _TdmMatch(reserved, _policy(entry.get("tdm-policy")))
    return None


class _StopParsing(Exception):  # noqa: N818 - control flow, not an error
    """Raised to stop reading a page once its head is over."""


class _MetaCollector(HTMLParser):
    """Collects ``(name, content)`` of ``<meta>`` tags up to the start of the body."""

    def __init__(self) -> None:
        super().__init__()
        self.metas: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            raise _StopParsing
        if tag == "meta":
            values = dict(attrs)
            name, content = values.get("name"), values.get("content")
            if name and content is not None:
                self.metas.append((name.strip().lower(), content))


def _head_metas(html: str | None) -> list[tuple[str, str]]:
    if not html:
        return []
    collector = _MetaCollector()
    with contextlib.suppress(_StopParsing):
        collector.feed(html[:_META_SCAN_CHARS])
    return collector.metas


def _first_content(metas: list[tuple[str, str]], name: str) -> str | None:
    return next((content for meta_name, content in metas if meta_name == name), None)


def _x_robots_noai(value: str) -> bool:
    """``noai``/``noml`` in an ``X-Robots-Tag`` that addresses everyone or this service.

    A directive list may be introduced by a bot name (``googlebot: noindex, noai``); it then
    holds for that bot until the next name.
    """
    scope: str | None = None
    for raw in value.lower().split(","):
        part = raw.strip()
        head, separator, rest = part.partition(":")
        head = head.strip()
        if separator and _BOT_NAME.fullmatch(head) and head not in _VALUE_DIRECTIVES:
            scope, part = head, rest.strip()
        if scope in (None, "*", "robots", PRODUCT_TOKEN) and part in _NOAI_DIRECTIVES:
            return True
    return False


def _has_noai(x_robots_tag: str | None, metas: list[tuple[str, str]]) -> bool:
    if x_robots_tag and _x_robots_noai(x_robots_tag):
        return True
    return any(
        directive.strip().lower() in _NOAI_DIRECTIVES
        for name, content in metas
        if name in ("robots", PRODUCT_TOKEN)
        for directive in content.split(",")
    )
