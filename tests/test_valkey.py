"""Helpers of ``valkey.py``."""

import asyncio
import contextlib

import pytest
from redis.exceptions import TimeoutError

from text_extraction.valkey import create_valkey, reply_text


def test_reply_text_decodes_bytes_and_keeps_text() -> None:
    assert reply_text(b"gr\xc3\xbc\xc3\x9fe") == "grüße"
    assert reply_text("plain") == "plain"


async def _serve_resp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer each command once (``PING`` with ``PONG``, the rest with ``OK``), then hang up:
    every connection goes stale after its first reply, like one to a Valkey that restarted."""
    with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
        while True:
            count = int((await reader.readline())[1:])
            words = []
            for _ in range(count):
                size = int((await reader.readline())[1:])
                words.append((await reader.readexactly(size + 2))[:-2].upper())
            writer.write(b"+PONG\r\n" if words[0] == b"PING" else b"+OK\r\n")
            await writer.drain()
            if words[0] == b"PING":
                break
    writer.close()


def test_a_stale_connection_is_retried_once() -> None:
    asyncio.run(_ping_twice())


async def _ping_twice() -> None:
    server = await asyncio.start_server(_serve_resp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = create_valkey(f"redis://127.0.0.1:{port}/0")
    try:
        assert await client.ping()
        assert await client.ping()  # the pooled connection was closed by the server
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()


def test_a_timeout_is_not_retried() -> None:
    # A command that timed out may have run; running it again could count or claim twice.
    retry = create_valkey("redis://127.0.0.1:1/0").connection_pool.connection_kwargs["retry"]
    assert retry.get_retries() == 1
    with pytest.raises(TimeoutError):
        asyncio.run(retry.call_with_retry(_times_out, _ignore))


async def _times_out() -> None:
    raise TimeoutError("read timed out")


async def _ignore(error: Exception) -> None:
    del error
