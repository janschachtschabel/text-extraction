"""Text fingerprints and change detection.

After an extraction the content is normalised (no Markdown syntax, no case, single spaces) and
gets two fingerprints: the SHA-256 of the normalised text, which says *whether* the text changed,
and a 128-permutation MinHash over word 5-shingles, which says *how much*. ``FingerprintStore``
keeps the last fingerprint of each URL in Valkey and compares a new one with it.

The MinHash is implemented here (numpy only) instead of using ``datasketch``: that library pulls
in scipy (80 MB installed) for features we do not use, and its default hash scheme changed
between releases, which would silently turn every stored fingerprint into a "change". The
algorithm below is fixed and equals datasketch 2.x ``MinHash(num_perm=128, seed=1,
scheme="affine32")``: SHA-1 (first four bytes, little-endian) of each shingle, permuted by
``h -> a*h + b mod 2^32`` with ``a`` odd (after a MurmurHash3 finalizer that spreads the bits),
minimum per permutation. Near-duplicate search with
datasketch's LSH can read the stored values later.

Valkey keys: ``fp:{<sha256>}`` (the braces are a literal cluster hash tag). The hash is taken over
the normalised URL and, when given, a variant (the pipeline passes the output format, since
Markdown, text and HTML of one page normalise to different texts): ``sha256(url)`` without a
variant, ``sha256(url + NUL + variant)`` with one. The URL is normalised by ``normalize_url``:
lower-case scheme and host, no fragment, no default port; path and query stay as they are.
"""

import base64
import hashlib
import re
from functools import lru_cache
from itertools import islice
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import structlog
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from text_extraction.settings import Settings

_log = structlog.get_logger(__name__)

NUM_PERM = 128
SEED = 1
SHINGLE_WORDS = 5
# At most this many shingles are hashed. Longer texts (about 1.3 MB of prose and more) enter the
# MinHash with their first MAX_SHINGLES shingles only, which bounds the cost of a 20 MB page to
# a fraction of a second. ``text_sha256`` still covers the whole text, so a later change is
# reported as ``changed`` even when ``similarity_to_previous`` stays at 1.0.
MAX_SHINGLES = 200_000
# Shingles are permuted in blocks of this size: 4096 x 128 x 4 bytes = 2 MB at a time.
_BLOCK = 4096
_MAX_HASH = (1 << 32) - 1


_WHITESPACE_CHAR = re.compile(r"\s")
# Whitespace is collapsed in pieces of about this many characters (see _collapse_whitespace).
_COLLAPSE_CHUNK = 1 << 20
_WORD = re.compile(r"\S+")

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Markdown syntax that carries no text. Every pattern is linear: lines are anchored with ``^`` and
# only use horizontal whitespace, bracket classes exclude their own opener, and runs that may
# fail to match are possessive.
_MD_RULE_LINE = re.compile(r"^[-=:| \t]{3,}+$", re.MULTILINE)  # table separators, ---, ===
_MD_LINE_PREFIX = re.compile(
    r"^[ \t]*+(?:#{1,6}(?=[ \t]|$)|>+|[-*+](?=[ \t])|\d{1,9}[.)](?=[ \t]))[ \t]*+", re.MULTILINE
)
_MD_LINK = re.compile(r"!?\[([^\[\]]*+)\]\([^()\n]*+\)")  # [text](target) and ![alt](target)
_MD_EMPHASIS = re.compile(r"[*_~`]")  # removed: "**bold**." must give the words of "bold."
_MD_TABLE_PIPE = re.compile(r"\|")  # a separator: "|a|b|" has two words


def normalize_text(content: str) -> str:
    """Reduce ``content`` to its words: no Markdown syntax, case-folded, single spaces."""
    text = _MD_RULE_LINE.sub("", content)
    text = _MD_LINE_PREFIX.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_EMPHASIS.sub("", text)
    text = _MD_TABLE_PIPE.sub(" ", text)
    return _collapse_whitespace(text.casefold())


def _collapse_whitespace(text: str) -> str:
    """``" ".join(text.split())`` without holding every word of a huge text in memory at once.

    ``split()`` makes a list of all words (about 90 MB per 10 MB of prose) and ``re.sub`` a list of
    all matches, so the text is cut at whitespace into pieces of ``_COLLAPSE_CHUNK`` characters.
    """
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start + _COLLAPSE_CHUNK
        if end < len(text):
            cut = _WHITESPACE_CHAR.search(text, end)  # never inside a word
            end = cut.start() if cut else len(text)
        piece = " ".join(text[start:end].split())
        if piece:
            pieces.append(piece)
        start = end
    return " ".join(pieces)


def normalize_url(url: str) -> str:
    """Key form of ``url``: lower-case scheme and host, no fragment, no default port.

    The path and the query are kept as they are. A string that does not parse as a URL is only
    stripped of its fragment.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url.split("#", 1)[0]
    scheme = parts.scheme.lower()
    host = parts.hostname
    if host is None:
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = f"{userinfo}@{host}" if userinfo else host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def url_hash(url: str) -> str:
    """SHA-256 (hex) of the normalised URL; the per-URL part of every Valkey key."""
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


class Fingerprint(BaseModel):
    """What identifies a text. The last two fields are filled by ``FingerprintStore``."""

    raw_sha256: str  # of the fetched bytes
    text_sha256: str  # of the normalised text
    minhash: str  # base64 of 128 little-endian uint32 values
    similarity_to_previous: float | None = None  # estimated Jaccard similarity, 0..1
    changed: bool | None = None  # text_sha256 differs from the previous one; None = no previous


@lru_cache(maxsize=1)
def _permutations() -> tuple[np.ndarray, np.ndarray]:
    generator = np.random.RandomState(SEED)
    a = generator.randint(0, 1 << 31, NUM_PERM, dtype=np.uint32) * np.uint32(2) + np.uint32(1)
    b = generator.randint(0, 1 << 32, NUM_PERM, dtype=np.uint32)
    return a, b


def _hash32(shingle: str) -> int:
    return int.from_bytes(
        hashlib.sha1(shingle.encode(), usedforsecurity=False).digest()[:4], "little"
    )


def _fmix32(h: np.ndarray) -> np.ndarray:
    """MurmurHash3 finalizer: a bijection on uint32 that avalanches every input bit."""
    h = h ^ (h >> np.uint32(16))
    h = h * np.uint32(0x85EBCA6B)
    h = h ^ (h >> np.uint32(13))
    h = h * np.uint32(0xC2B2AE35)
    h = h ^ (h >> np.uint32(16))
    return h


def _minhash(words: list[str]) -> bytes:
    """MinHash of the shingles of ``words`` as 128 little-endian uint32 values."""
    if len(words) < SHINGLE_WORDS:
        shingles = set(words)
    else:
        count = min(len(words) - SHINGLE_WORDS + 1, MAX_SHINGLES)
        shingles = {" ".join(words[i : i + SHINGLE_WORDS]) for i in range(count)}
    a, b = _permutations()
    values = np.full(NUM_PERM, _MAX_HASH, dtype=np.uint32)
    hashes = np.fromiter((_hash32(s) for s in shingles), dtype=np.uint32, count=len(shingles))
    with np.errstate(over="ignore"):  # uint32 wrap-around is the arithmetic mod 2^32
        hashes = _fmix32(hashes)
        for start in range(0, len(hashes), _BLOCK):
            block = hashes[start : start + _BLOCK, None]
            np.minimum(values, (block * a + b).min(axis=0), out=values)
    return values.astype("<u4").tobytes()


def compute_fingerprint(raw_sha256: str, content: str) -> Fingerprint:
    """Fingerprint ``content``; ``raw_sha256`` is the hash of the fetched bytes."""
    normalized = normalize_text(content)
    # Only the words the capped shingles need are materialised, not every word of a huge text.
    words = [
        m.group() for m in islice(_WORD.finditer(normalized), MAX_SHINGLES + SHINGLE_WORDS - 1)
    ]
    return Fingerprint(
        raw_sha256=raw_sha256,
        text_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
        minhash=base64.b64encode(_minhash(words)).decode("ascii"),
    )


def _decode(minhash: str) -> np.ndarray:
    try:
        raw = base64.b64decode(minhash, validate=True)
    except ValueError:
        raw = b""
    if len(raw) != NUM_PERM * 4:
        raise ValueError("minhash must be base64 of 128 uint32 values")
    return np.frombuffer(raw, dtype="<u4")


def similarity(a: str, b: str) -> float:
    """Estimated Jaccard similarity (0..1) of the two texts behind two encoded MinHashes."""
    return float(np.mean(_decode(a) == _decode(b)))


class FingerprintStore:
    """The last fingerprint of each URL, in Valkey, to tell whether and how much a page changed."""

    def __init__(self, valkey: Redis, settings: Settings) -> None:
        self._valkey = valkey
        self._ttl = settings.fingerprint_ttl_seconds

    async def compare_and_save(
        self, url: str, fp: Fingerprint, *, variant: str = ""
    ) -> Fingerprint:
        """Store ``fp`` for ``url`` and ``variant`` and return it with ``changed`` and
        ``similarity_to_previous``. Each variant keeps its own previous fingerprint.

        Both stay ``None`` when there is no usable previous fingerprint: first sight, expired,
        unreadable, or Valkey unavailable (that is logged and never raised). With
        ``FINGERPRINT_TTL_SECONDS=0`` nothing is stored. Replacing and reading the old value is
        one atomic ``SET ... GET``, so two pods never compare against the same predecessor.
        """
        if self._ttl <= 0:
            return fp
        stored = fp.model_copy(update={"similarity_to_previous": None, "changed": None})
        key = _store_key(url, variant)
        try:
            previous_raw = await self._valkey.set(
                key, stored.model_dump_json(), ex=self._ttl, get=True
            )
        except (RedisError, OSError) as error:
            _log.warning("fingerprint_store_unavailable", error=type(error).__name__)
            return stored
        previous = _parse(previous_raw if isinstance(previous_raw, bytes | str) else None)
        if previous is None:
            return stored
        try:
            score = similarity(previous.minhash, fp.minhash)
        except ValueError:
            return stored
        return stored.model_copy(
            update={
                "similarity_to_previous": score,
                "changed": previous.text_sha256 != fp.text_sha256,
            }
        )

    async def touch(self, url: str, *, variant: str = "") -> None:
        """Renew the TTL of the stored fingerprint, if there is one: a page that is only ever
        revalidated (and so never compared again) keeps its baseline. Never raises."""
        if self._ttl <= 0:
            return
        try:
            await self._valkey.expire(_store_key(url, variant), self._ttl)
        except (RedisError, OSError) as error:
            _log.warning("fingerprint_store_unavailable", error=type(error).__name__)


def _store_key(url: str, variant: str) -> str:
    if not variant:
        return f"fp:{{{url_hash(url)}}}"
    digest = hashlib.sha256(f"{normalize_url(url)}\0{variant}".encode()).hexdigest()
    return f"fp:{{{digest}}}"


def _parse(raw: bytes | str | None) -> Fingerprint | None:
    if raw is None:
        return None
    try:
        return Fingerprint.model_validate_json(raw)
    except ValidationError:
        return None  # unreadable: treated as first sight and overwritten
