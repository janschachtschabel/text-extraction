import asyncio
import base64
import time
import tracemalloc
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis.asyncio import Redis
from structlog.testing import capture_logs

from text_extraction import fingerprint
from text_extraction.fingerprint import (
    MAX_SHINGLES,
    SHINGLE_WORDS,
    Fingerprint,
    FingerprintStore,
    compute_fingerprint,
    normalize_text,
    normalize_url,
    similarity,
)
from text_extraction.settings import Settings

RAW = "a" * 64

ARTICLE = " ".join(
    f"Sentence {i} talks about topic {i % 7} and mentions the river bank number {i * 3}."
    for i in range(120)
)
OTHER = " ".join(
    f"Completely unrelated paragraph {i} describes zebra migration pattern {i * 5} in savanna."
    for i in range(120)
)


def make_settings(**overrides: Any) -> Settings:
    return Settings(auth_mode="none", **overrides)


def test_normalize_strips_markdown_and_case() -> None:
    markdown = (
        "# Title Here\n\n"
        "Some **bold** and _emphasis_ with `code` and ~~gone~~.\n\n"
        "- first item\n"
        "* second item\n"
        "1. numbered item\n"
        "2) other item\n"
        "> quoted\n\n"
        '[a link](https://example.org/x?y=1 "tip") and ![an image](pic.png)\n\n'
        "| Name | Value |\n| --- | :---: |\n| Ärger | 5 |\n"
    )
    assert normalize_text(markdown) == (
        "title here some bold and emphasis with code and gone. "
        "first item second item numbered item other item quoted "
        "a link and an image name value ärger 5"
    )


def test_whitespace_collapse_is_the_same_across_chunk_borders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = " \t".join(["word", "\u00a0", "two\n\nthree", "  ", "x" * 50, "\u2003y"] * 40) + "  "
    expected = " ".join(text.split())
    for chunk in (1, 2, 7, 64, 1 << 20):
        monkeypatch.setattr(fingerprint, "_COLLAPSE_CHUNK", chunk)
        assert fingerprint._collapse_whitespace(text) == expected
    assert fingerprint._collapse_whitespace("") == ""
    assert fingerprint._collapse_whitespace(" \n ") == ""


def test_normalize_casefolds_and_collapses_whitespace() -> None:
    assert normalize_text("  Straße\t\tMASSE \n\n\n x  ") == "strasse masse x"
    assert normalize_text("") == ""


def test_same_text_same_hash_regardless_of_markdown() -> None:
    plain = "Release notes\n\nThe new version is faster and fixes the login bug."
    markdown = "## Release notes\n\nThe **new version** is _faster_ and fixes the [login](/l) bug."
    a = compute_fingerprint(RAW, plain)
    b = compute_fingerprint(RAW, markdown)
    assert a.text_sha256 == b.text_sha256
    assert a.minhash == b.minhash
    assert similarity(a.minhash, b.minhash) == 1.0


def test_fingerprint_fields_and_minhash_encoding() -> None:
    fp = compute_fingerprint(RAW, ARTICLE)
    assert fp.raw_sha256 == RAW
    assert len(fp.text_sha256) == 64
    assert len(base64.b64decode(fp.minhash, validate=True)) == 128 * 4
    assert fp.similarity_to_previous is None
    assert fp.changed is None


PINNED_MINHASH = (
    "HEawOiHPtDHZ95NFQucyR0gWMG8JbcCI+vF0hybdFYTgLWm+VwxuGJEe25c/Dle5I3HPTJEz2AmswfmY1yiCKpjJIRwv"
    "xPdwAMFOQOo2RzSw7lk4iydJKvNe+Uj5qdo06HgGrcXWxGEcDN4ZiBk6Eza0KlQcNuAE+SP1blY1zjhrEwEK0IEWltXq"
    "szzLfd+MzRCrGtQJIKFG5kq5WMRpVqjF3h4kuSJGEPsMIAVqMx9yLGAS0D/JiLW5o21MvmILxowbU1/reyP9L2RZduGR"
    "1ijY0yaYIoJPe1FlQkYNQX2eKRBrEvaKaRQrEyOV9qdC3qbnMhq1t286DH9ZvoeIkRsY1IPbTdKHtCOh8ppf2GtrrAPl"
    "QbUR2dXzegbe3cmCIOjSAlLKCjJ8S2Qggogto8lkVgSB4S5fdYWMC47Vnbr1u2u7MQ/jK6DMQQ0O1CeE9L5ebyPuXkmR"
    "a5MvfN+iroWyZE5JtAf5I7xvgdxsaRl9tBlPR+2lH10ig9t9ou8LUohaMSHYG4Ew0IpUGItEIU+9hB46rOVMsT3No7Q8"
    "AYH39f5Ae3K6b4TNgk1XrmgTTbFZHMbiWiHLtnm8+ekmZvlrKhHo2DNGSWU9GoTY0AFx0DBNtPeXCVkn9RBX5p9I3Xw8"
    "cTVity/zfI+sQSt8nOxvWFyy3eAukRfTUNPl0ww="
)


def test_minhash_matches_the_datasketch_affine32_layout() -> None:
    # Known value for a fixed text (equal to datasketch 2.x MinHash(128, seed=1, "affine32")):
    # guards against accidental changes of the algorithm, which would silently make every
    # stored fingerprint "changed".
    fp = compute_fingerprint(RAW, "one two three four five six")
    assert fp.minhash == PINNED_MINHASH
    assert fp.text_sha256 == compute_fingerprint(RAW, "ONE two  three four five six").text_sha256


def test_words_beyond_the_shingle_cap_do_not_enter_the_minhash() -> None:
    head = " ".join(f"w{i}" for i in range(MAX_SHINGLES + SHINGLE_WORDS - 1))
    a = compute_fingerprint(RAW, head)
    b = compute_fingerprint(RAW, head + " extra words after the cap")
    assert a.minhash == b.minhash  # the cap is on shingles: the first MAX_SHINGLES of them
    assert a.text_sha256 != b.text_sha256  # but the text hash still sees everything


def test_fingerprint_does_not_materialise_every_word() -> None:
    text = " ".join(f"w{i}" for i in range(1_500_000))  # 1.5 M words, ~10 MB
    tracemalloc.start()
    try:
        compute_fingerprint(RAW, text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Holding every word costs ~90 MB on top of the ~50 MB the capped shingles need.
    assert peak < 80_000_000, f"peak {peak / 1e6:.0f} MB"


def test_minhash_similarity_close_for_small_edit() -> None:
    edited = ARTICLE.replace("topic 3 and", "subject 3 and", 1) + " One extra closing sentence."
    a = compute_fingerprint(RAW, ARTICLE)
    b = compute_fingerprint(RAW, edited)
    assert a.text_sha256 != b.text_sha256
    assert similarity(a.minhash, b.minhash) > 0.8


def test_minhash_similarity_low_for_different_text() -> None:
    a = compute_fingerprint(RAW, ARTICLE)
    b = compute_fingerprint(RAW, OTHER)
    assert similarity(a.minhash, b.minhash) < 0.2


def test_short_text_fingerprint() -> None:
    # Fewer than five words: the word set is the shingle set.
    a = compute_fingerprint(RAW, "Hello brave world")
    b = compute_fingerprint(RAW, "hello world brave")
    c = compute_fingerprint(RAW, "Hello brave there")
    assert similarity(a.minhash, b.minhash) == 1.0  # same word set
    assert a.text_sha256 != b.text_sha256  # but the text differs
    assert 0.0 < similarity(a.minhash, c.minhash) < 1.0
    # Empty text still gives a valid fingerprint.
    empty = compute_fingerprint(RAW, "  ")
    assert len(base64.b64decode(empty.minhash)) == 128 * 4


def test_similarity_rejects_a_malformed_minhash() -> None:
    good = compute_fingerprint(RAW, ARTICLE).minhash
    with pytest.raises(ValueError, match="minhash"):
        similarity(good, "not base64!")
    with pytest.raises(ValueError, match="minhash"):
        similarity(good, base64.b64encode(b"short").decode())


def test_normalize_url() -> None:
    assert (
        normalize_url("HTTPS://Example.ORG:443/Path/A?Q=1#frag") == "https://example.org/Path/A?Q=1"
    )
    assert normalize_url("http://example.org:80/") == "http://example.org/"
    assert normalize_url("http://example.org:8080/x") == "http://example.org:8080/x"
    assert normalize_url("https://example.org:80/") == "https://example.org:80/"
    assert normalize_url("https://[::1]:443/a") == "https://[::1]/a"
    assert normalize_url("https://User@Example.org/A") == "https://User@example.org/A"
    assert normalize_url("not a url") == "not a url"
    assert normalize_url("http://[broken/x#f") == "http://[broken/x"


def test_normalization_and_fingerprint_are_fast_on_huge_text() -> None:
    # Pathological shapes for regexes must stay linear: many "[" or "](" without a closing
    # bracket, long whitespace and dash runs.
    nasty = ("[" * 200_000) + ("](" * 100_000) + (" " * 200_000) + "-" * 3 + " x" + "\n" * 100_000
    start = time.perf_counter()
    normalize_text(nasty)
    assert time.perf_counter() - start < 2.0

    big = ("Paragraph with **markdown** and [links](http://e.org/a) and plain words. " * 280_000)[
        :20_000_000
    ]
    start = time.perf_counter()
    fp = compute_fingerprint(RAW, big)
    elapsed = time.perf_counter() - start
    assert elapsed < 4.0, f"fingerprinting 20 MB took {elapsed:.1f}s"
    assert len(fp.text_sha256) == 64


def store_for(valkey: Redis, **overrides: Any) -> FingerprintStore:
    return FingerprintStore(valkey, make_settings(**overrides))


def test_first_fingerprint_has_no_previous() -> None:
    async def scenario() -> Fingerprint:
        store = store_for(FakeAsyncRedis())
        return await store.compare_and_save(
            "https://example.org/a", compute_fingerprint(RAW, ARTICLE)
        )

    saved = asyncio.run(scenario())
    assert saved.similarity_to_previous is None
    assert saved.changed is None


def test_store_reports_changed_and_similarity() -> None:
    async def scenario() -> tuple[Fingerprint, Fingerprint, Fingerprint]:
        store = store_for(FakeAsyncRedis())
        url = "https://example.org/a"
        await store.compare_and_save(url, compute_fingerprint(RAW, ARTICLE))
        same = await store.compare_and_save(url, compute_fingerprint("b" * 64, f"**{ARTICLE}**"))
        edited = ARTICLE.replace("topic 3 and", "subject 3 and", 1)
        small = await store.compare_and_save(url, compute_fingerprint(RAW, edited))
        other = await store.compare_and_save(url, compute_fingerprint(RAW, OTHER))
        return same, small, other

    same, small, other = asyncio.run(scenario())
    # Markdown-only differences and a different raw hash do not count as a text change.
    assert same.changed is False
    assert same.similarity_to_previous == 1.0
    assert small.changed is True
    assert small.similarity_to_previous is not None
    assert small.similarity_to_previous > 0.8
    # Each call compares with the one saved by the call before.
    assert other.changed is True
    assert other.similarity_to_previous is not None
    assert other.similarity_to_previous < 0.2


def test_store_key_is_normalised_hash_tagged_and_expires() -> None:
    async def scenario() -> tuple[list[bytes | str], int, Fingerprint]:
        valkey = FakeAsyncRedis()
        store = store_for(valkey, fingerprint_ttl_seconds=100)
        await store.compare_and_save(
            "HTTPS://Example.org:443/a?x=1#top", compute_fingerprint(RAW, ARTICLE)
        )
        keys = await valkey.keys("*")
        ttl = await valkey.ttl(keys[0])
        again = await store.compare_and_save(
            "https://example.org/a?x=1", compute_fingerprint(RAW, ARTICLE)
        )
        return keys, ttl, again

    keys, ttl, again = asyncio.run(scenario())
    (raw_key,) = keys
    key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
    assert key.startswith("fp:{")
    assert key.endswith("}")
    assert len(key) == len("fp:{}") + 64
    assert "example" not in key
    assert 0 < ttl <= 100
    assert again.changed is False  # same key although spelled differently


def test_store_ignores_an_unreadable_previous_entry() -> None:
    async def scenario() -> Fingerprint:
        valkey = FakeAsyncRedis()
        store = store_for(valkey)
        fp = compute_fingerprint(RAW, ARTICLE)
        await store.compare_and_save("https://example.org/a", fp)
        (key,) = await valkey.keys("*")
        await valkey.set(key, b"{not json")
        return await store.compare_and_save("https://example.org/a", fp)

    result = asyncio.run(scenario())
    assert result.changed is None
    assert result.similarity_to_previous is None


def test_store_ttl_zero_stores_nothing() -> None:
    async def scenario() -> tuple[list[bytes | str], Fingerprint]:
        valkey = FakeAsyncRedis()
        store = store_for(valkey, fingerprint_ttl_seconds=0)
        fp = await store.compare_and_save(
            "https://example.org/a", compute_fingerprint(RAW, ARTICLE)
        )
        return await valkey.keys("*"), fp

    keys, fp = asyncio.run(scenario())
    assert keys == []
    assert fp.changed is None


def test_store_survives_valkey_errors() -> None:
    async def scenario() -> Fingerprint:
        store = store_for(FakeAsyncRedis(connected=False))
        return await store.compare_and_save(
            "https://example.org/secret/path?token=abc", compute_fingerprint(RAW, ARTICLE)
        )

    with capture_logs() as logs:
        fp = asyncio.run(scenario())
    assert fp.raw_sha256 == RAW
    assert fp.changed is None
    assert fp.similarity_to_previous is None
    (warning,) = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warning["event"] == "fingerprint_store_unavailable"
    assert "example.org" not in repr(logs)
    assert "token" not in repr(logs)


def test_store_keeps_one_baseline_per_variant() -> None:
    url = "https://example.org/a"

    async def scenario() -> tuple[list[Fingerprint], int]:
        valkey = FakeAsyncRedis()
        store = store_for(valkey)
        markdown = compute_fingerprint(RAW, f"# Title\n\n{ARTICLE}")
        html = compute_fingerprint(RAW, f"<h1>Title</h1><p>{ARTICLE}</p>")
        results = [
            await store.compare_and_save(url, markdown, variant="markdown"),
            await store.compare_and_save(url, html, variant="html"),
            await store.compare_and_save(url, markdown, variant="markdown"),
            await store.compare_and_save(url, html, variant="html"),
        ]
        return results, len(await valkey.keys("fp:*"))

    results, key_count = asyncio.run(scenario())
    assert [fp.changed for fp in results] == [None, None, False, False]
    assert key_count == 2


def test_default_variant_keeps_the_url_only_key() -> None:
    async def scenario() -> list[bytes | str]:
        valkey = FakeAsyncRedis()
        await store_for(valkey).compare_and_save(
            "https://example.org/a", compute_fingerprint(RAW, ARTICLE)
        )
        return list(await valkey.keys("*"))

    (key,) = asyncio.run(scenario())
    assert key == f"fp:{{{fingerprint.url_hash('https://example.org/a')}}}".encode()


def test_touch_renews_the_baseline_ttl() -> None:
    url = "https://example.org/a"

    async def scenario() -> tuple[int, int]:
        valkey = FakeAsyncRedis()
        store = store_for(valkey, fingerprint_ttl_seconds=1000)
        await store.compare_and_save(url, compute_fingerprint(RAW, ARTICLE), variant="text")
        (key,) = await valkey.keys("fp:*")
        await valkey.expire(key, 5)
        await store.touch(url, variant="text")
        await store.touch("https://example.org/never-seen", variant="text")
        return int(await valkey.ttl(key)), len(await valkey.keys("fp:*"))

    ttl, key_count = asyncio.run(scenario())
    assert ttl > 900
    assert key_count == 1  # touching an unknown URL creates nothing


def test_touch_survives_valkey_errors() -> None:
    store = store_for(FakeAsyncRedis(connected=False))

    asyncio.run(store.touch("https://example.org/a", variant="text"))
