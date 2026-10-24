"""Respect for the target site's wishes: robots.txt, TDMRep, ai.txt and ``noai``.

The pipeline calls this module three times per request:

1. ``resolve_mode`` turns the caller's ``compliance`` option into the mode that applies.
2. ``ComplianceChecker.before_fetch`` reads the three files of the target's origin (robots.txt,
   ``/.well-known/tdmrep.json``, ai.txt) and decides for the URL, before the page is fetched.
   The files are fetched one after another, never in parallel: the pipeline calls this inside
   the outbound gate turn of the page, so every request to the host counts against its limits.
   ``cached_before_fetch`` decides from the cache alone (no request), or answers None while a
   file is not cached; the pipeline asks it first and takes the turn only for what is missing.
3. ``ComplianceChecker.after_fetch`` adds what only the page itself can tell: the
   ``tdm-reservation`` and ``tdm-policy`` headers, the same two ``<meta>`` tags and
   ``noai``/``noml`` in ``X-Robots-Tag`` or ``<meta name="robots">``.

Every redirect target is checked like the first URL (``before_fetch`` again; the files are
cached per origin) and merged into the report with ``with_redirect_findings``.

``blocking_code`` is asked after both steps; it names the error to answer with, and only in
``enforce`` mode. In ``report`` mode nothing blocks, but the report says what would have. In
``ignore`` mode nothing is fetched or checked at all.

robots.txt follows RFC 9309 (via protego) with the product token ``text-extraction``:

* 2xx: the rules apply;
* 4xx: no rules, everything is allowed. 429 is the exception: it means "try later", so it is
  treated like a server error. A final 3xx (no usable Location, too many redirects) also means
  no rules. The fetcher raises ``too_large`` or similar for some of these answers; their
  ``upstream_status`` decides, so a 404 page over the size limit still means "no file";
* 5xx, any other status and any fetch error: ``unavailable``, which ``enforce`` treats as
  disallowed.

A robots.txt over 500 KiB is parsed from its first 500 KiB (RFC 9309 section 2.5), without the
line the cut went through. tdmrep.json and ai.txt over that size count as not retrievable.

``llms.txt`` is a hint for models, not a restriction, and is never fetched.

State lives in Valkey, one key per origin and file: ``cmp:{kind}:{<origin>}`` (the braces are a
literal cluster hash tag) holding JSON ``{"state", "body"}``. The state is ``found`` (2xx, with
the raw text), ``absent`` (4xx) or ``unavailable`` (5xx, 429 or a fetch error). The first two live
``compliance_cache_seconds``; ``unavailable`` lives at most five minutes, so a short outage of a
site does not block it for a whole cache period. Concurrent checks of one origin in this process
share one load of each file (cache read, fetch, cache write); other pods load their own. A load
whose last waiting check is cancelled is cancelled too, so no request outlives the gate turn it
ran in.
Parsing a rules file runs in a worker thread, off the event loop.
"""

import asyncio
import contextlib
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
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

_FILE_MAX_BYTES = 512_000  # 500 KiB, RFC 9309's least parsing limit for robots.txt
_FILE_TIMEOUT_SECONDS = 10  # for the three files of an origin together
# A cached "unavailable" answer is kept at most this long.
_UNAVAILABLE_TTL_SECONDS = 300
# Only the head of a page can carry the meta tags we read.
_META_SCAN_CHARS = 262_144
_MAX_POLICY_CHARS = 2048
# Parsed robots.txt / ai.txt files kept in this process.
_RULES_CACHE_SIZE = 256
_ROBOTS_PATH = "/robots.txt"
_TDMREP_PATH = "/.well-known/tdmrep.json"
_AI_TXT_PATH = "/ai.txt"
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


def with_page_findings(report: ComplianceReport, previous: ComplianceReport) -> ComplianceReport:
    """``report`` plus the TDM statement and ``noai`` that a stored copy of the page carried.

    For a 304 answer: it has no body and often leaves out headers, but it says the page is the
    stored one, so what that page said about itself (``tdm-reservation`` header or meta tag,
    ``noai``) still holds. Findings of the origin files (robots.txt, tdmrep.json, ai.txt) are not
    carried; ``report`` has fresh ones. Pass the result to ``after_fetch`` with the 304 so its own
    headers count too. Returns ``report`` itself when there is nothing to carry.
    """
    update: dict[str, object] = {}
    reasons = list(report.reasons)
    source = previous.tdm_source
    carry_tdm = (
        source in ("header", "meta")
        and previous.tdm_reserved is not None
        and report.tdm_reserved is not True
        and (previous.tdm_reserved or report.tdm_reserved is None)
    )
    if carry_tdm:
        update.update(
            tdm_reserved=previous.tdm_reserved, tdm_policy=previous.tdm_policy, tdm_source=source
        )
        if previous.tdm_reserved:
            reasons.append(f"The page reserves text and data mining rights ({source}).")
    if previous.noai and not report.noai:
        update["noai"] = True
        reasons.append("The page asks not to be used for AI (noai or noml).")
    if not update:
        return report
    update["reasons"] = reasons
    return _decided(report.model_copy(update=update))


# Worse answers rank higher; a redirect chain reports the worst answer of its URLs.
_ROBOTS_RANK = {"not_checked": 0, "allowed": 1, "unavailable": 2, "disallowed": 3}
_AI_TXT_RANK = {"not_checked": 0, "absent": 1, "allowed": 2, "disallowed": 3}


def with_redirect_findings(report: ComplianceReport, hop: ComplianceReport) -> ComplianceReport:
    """``report`` plus the findings of ``before_fetch`` for a redirect target, ``hop``.

    The target is fetched too, so its findings count as much as the first URL's: the worse
    robots.txt and ai.txt answer, a TDM reservation from either, and the reasons of both.
    ``crawl_delay`` becomes the target's, since its host is the one fetched next.
    """
    update: dict[str, object] = {
        "robots": max(report.robots, hop.robots, key=_ROBOTS_RANK.__getitem__),
        "ai_txt": max(report.ai_txt, hop.ai_txt, key=_AI_TXT_RANK.__getitem__),
        "crawl_delay": hop.crawl_delay,
        "noai": report.noai or hop.noai,
        "reasons": [*report.reasons, *(r for r in hop.reasons if r not in report.reasons)],
    }
    takes_hop_tdm = hop.tdm_reserved is True and report.tdm_reserved is not True
    if takes_hop_tdm or (report.tdm_reserved is None and hop.tdm_reserved is not None):
        update.update(
            tdm_reserved=hop.tdm_reserved, tdm_policy=hop.tdm_policy, tdm_source=hop.tdm_source
        )
    return _decided(report.model_copy(update=update))


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
        # Parsed rules by SHA-256 of their text: a 500 KB file takes a while to parse, and the
        # same text from the Valkey cache must not be parsed again on every request.
        self._rules_cache: OrderedDict[str, Protego | None] = OrderedDict()
        # Loads in flight by cache key; concurrent checks of one origin wait for the same one.
        self._loading: dict[str, asyncio.Task[_Entry]] = {}
        # Checks waiting for each load in flight.
        self._waiters: dict[asyncio.Task[_Entry], int] = {}

    async def before_fetch(self, url: str, mode: ComplianceMode) -> ComplianceReport:
        """Check robots.txt, TDMRep and ai.txt for ``url``; fetches nothing in ``ignore`` mode."""
        if mode == "ignore":
            return ComplianceReport(mode=mode, decision="skipped")

        origin = _origin(url)
        # One time budget for the three files together (R23): a host where all of them hang
        # costs _FILE_TIMEOUT_SECONDS, not three times that.
        deadline = time.monotonic() + _FILE_TIMEOUT_SECONDS
        robots = await self._load("robots", origin, _ROBOTS_PATH, deadline)
        tdmrep = await self._load("tdmrep", origin, _TDMREP_PATH, deadline)
        ai = await self._load("ai", origin, _AI_TXT_PATH, deadline)
        return await self._judge(mode, url, robots, tdmrep, ai)

    async def cached_before_fetch(self, url: str, mode: ComplianceMode) -> ComplianceReport | None:
        """``before_fetch`` from the cache alone: None while any of the three files is not
        cached (or the cache cannot be read). Fetches nothing."""
        if mode == "ignore":
            return ComplianceReport(mode=mode, decision="skipped")
        origin = _origin(url)
        # One round trip: the keys of an origin share a hash tag.
        keys = [_cache_key(kind, origin) for kind in ("robots", "tdmrep", "ai")]
        try:
            raws = await self._valkey.mget(keys)
        except (RedisError, OSError) as error:
            _log.warning("compliance_cache_read_failed", error=type(error).__name__)
            return None
        robots, tdmrep, ai = (_parse_entry(raw) for raw in raws)
        if robots is None or tdmrep is None or ai is None:
            return None
        return await self._judge(mode, url, robots, tdmrep, ai)

    async def _judge(
        self, mode: ComplianceMode, url: str, robots: _Entry, tdmrep: _Entry, ai: _Entry
    ) -> ComplianceReport:
        report = ComplianceReport(mode=mode)
        report = self._apply_robots(report, robots, await self._rules(robots), url)
        report = self._apply_tdmrep(report, tdmrep, url)
        report = self._apply_ai_txt(report, ai, await self._rules(ai), url)
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
        # Repeated headers arrive joined with ", "; the first value counts.
        header_value = _parse_reservation(fetched.headers.get("tdm-reservation", "").split(",")[0])
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

    def _apply_robots(
        self, report: ComplianceReport, entry: _Entry, rules: Protego | None, url: str
    ) -> ComplianceReport:
        if entry.state == "absent":
            return report.model_copy(update={"robots": "allowed"})
        reasons = list(report.reasons)
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

    def _apply_ai_txt(
        self, report: ComplianceReport, entry: _Entry, rules: Protego | None, url: str
    ) -> ComplianceReport:
        reasons = list(report.reasons)
        if rules is None:
            if entry.state == "unavailable":
                reasons.append("ai.txt could not be retrieved.")
            return report.model_copy(update={"ai_txt": "absent", "reasons": reasons})
        if rules.can_fetch(url, PRODUCT_TOKEN):
            return report.model_copy(update={"ai_txt": "allowed"})
        reasons.append(f"ai.txt disallows this URL for {PRODUCT_TOKEN}.")
        return report.model_copy(update={"ai_txt": "disallowed", "reasons": reasons})

    async def _rules(self, entry: _Entry) -> Protego | None:
        """The parsed rules of a found robots.txt or ai.txt; None for any other entry."""
        if entry.state != "found":
            return None
        digest = hashlib.sha256(entry.body.encode()).hexdigest()
        if digest in self._rules_cache:
            self._rules_cache.move_to_end(digest)
            return self._rules_cache[digest]
        rules = await asyncio.to_thread(_parse_rules, entry.body)
        self._rules_cache[digest] = rules
        if len(self._rules_cache) > _RULES_CACHE_SIZE:
            self._rules_cache.popitem(last=False)
        return rules

    # --- reading the origin files --------------------------------------------------------------

    async def _load(self, kind: str, origin: _Origin, path: str, deadline: float) -> _Entry:
        """The file's entry. Concurrent callers share one load, within the time budget of the
        caller that started it (``deadline``, monotonic); its error reaches them all."""
        key = _cache_key(kind, origin)
        task = self._loading.get(key)
        if task is None:
            task = asyncio.create_task(self._load_now(key, kind, origin.base + path, deadline))
            self._loading[key] = task
            task.add_done_callback(lambda done: self._forget_load(key, done))
        self._waiters[task] = self._waiters.get(task, 0) + 1
        try:
            # A cancelled caller must not cancel the load the others wait for ...
            return await asyncio.shield(task)
        finally:
            self._waiters[task] -= 1
            if not self._waiters[task]:
                del self._waiters[task]
                # ... but the last one does: its request ran in that caller's gate turn. A
                # check that comes after starts a load of its own.
                if not task.done():
                    task.cancel()
                    self._forget_load(key, task)
                    # Wait until its request has ended: the caller gives its turn back next.
                    # Nobody waits for its outcome any more, an error included.
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    def _forget_load(self, key: str, task: asyncio.Task[_Entry]) -> None:
        if self._loading.get(key) is task:
            del self._loading[key]

    async def _load_now(self, key: str, kind: str, file_url: str, deadline: float) -> _Entry:
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        left = deadline - time.monotonic()
        if left > 0:
            entry = await self._fetch_file(file_url, truncate=kind == "robots", seconds=left)
        else:
            # The budget went to the files before: nothing is sent, as if this one timed out.
            _log.info(
                "compliance_file_unavailable", host=urlsplit(file_url).hostname, code="timeout"
            )
            entry = _Entry("unavailable")
        await self._cache_set(key, entry)
        return entry

    async def _fetch_file(self, file_url: str, *, truncate: bool, seconds: float) -> _Entry:
        try:
            fetched = await self._fetcher.fetch(
                file_url,
                max_bytes=_FILE_MAX_BYTES,
                timeout_seconds=min(seconds, _FILE_TIMEOUT_SECONDS),
                truncate=truncate,
            )
        except FetchError as error:
            # The fetcher also raises for some answers it did read: a 404 page over the size limit,
            # too many redirects. Their status says "no file here", not "site down".
            if error.upstream_status is not None and _says_absent(error.upstream_status):
                return _Entry("absent")
            _log.info(
                "compliance_file_unavailable",
                host=urlsplit(file_url).hostname,
                code=str(error.code),
            )
            return _Entry("unavailable")
        if 200 <= fetched.status < 300:
            text = fetched.content.decode("utf-8-sig", errors="replace")
            if fetched.truncated:
                # The cut may have gone through a rule; "Disallow: /d" must not stand for more.
                text = text[: text.rfind("\n") + 1]
                _log.info("compliance_file_truncated", host=urlsplit(file_url).hostname)
            return _Entry("found", text)
        if _says_absent(fetched.status):
            return _Entry("absent")
        return _Entry("unavailable")

    async def _cache_get(self, key: str) -> _Entry | None:
        try:
            raw = await self._valkey.get(key)
        except (RedisError, OSError) as error:
            _log.warning("compliance_cache_read_failed", error=type(error).__name__)
            return None
        return _parse_entry(raw)

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


def _cache_key(kind: str, origin: _Origin) -> str:
    return f"cmp:{kind}:{{{origin.key}}}"


def _parse_entry(raw: bytes | str | None) -> _Entry | None:
    """A cached entry; None when there is none or it cannot be read (fetch again)."""
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


def _says_absent(status: int) -> bool:
    """Whether an HTTP status means "no such file": 4xx (not 429) and a 3xx that went nowhere.

    RFC 9309 section 2.3.1.3 treats 4xx as "unavailable", which allows everything. Section 2.3.1.2
    lets a crawler treat redirects that cannot be followed the same way.
    """
    return 300 <= status < 400 or (400 <= status < 500 and status != 429)


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
