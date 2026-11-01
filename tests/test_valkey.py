"""``valkey.py``: the client, its health check and its helpers.

The client tests run against a tiny RESP server on a local socket. It answers the
handshake and a few commands, counts what it receives, and can drop a connection on
purpose, the way a Valkey restart or a lost peer does.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from redis.asyncio import SSLConnection, UnixDomainSocketConnection
from redis.asyncio.connection import AbstractConnection, Connection
from redis.exceptions import ConnectionError as ValkeyConnectionError

from text_extraction import valkey as valkey_module
from text_extraction.valkey import create_valkey, reply_text


@dataclass
class FakeValkey:
    """What the fake server saw, and how it misbehaves."""

    received: Counter[str] = field(default_factory=Counter)
    connections: int = 0
    # the next command with this name closes the connection without an answer
    drop_on: str | None = None
    writers: list[asyncio.StreamWriter] = field(default_factory=list)

    def close_all(self) -> None:
        """Close every open connection, as a Valkey restart does."""
        for writer in self.writers:
            writer.close()
        self.writers.clear()


async def _read_command(reader: asyncio.StreamReader) -> list[str] | None:
    line = await reader.readline()
    if not line:
        return None
    count = int(line[1:])
    parts = []
    for _ in range(count):
        size = int((await reader.readline())[1:])
        parts.append((await reader.readexactly(size + 2))[:-2].decode())
    return parts


def _answer(command: list[str]) -> bytes:
    name = command[0].upper()
    if name == "PING":
        return b"+PONG\r\n"
    if name == "GET":
        return b"$5\r\nvalue\r\n"
    if name == "INCRBY":
        return b":1\r\n"
    if name == "HELLO":  # the client speaks RESP3: a map of server facts
        return b"%2\r\n$6\r\nserver\r\n$6\r\nvalkey\r\n$5\r\nproto\r\n:3\r\n"
    return b"+OK\r\n"  # CLIENT SETINFO and the rest


@asynccontextmanager
async def fake_valkey() -> AsyncIterator[tuple[str, FakeValkey]]:
    state = FakeValkey()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state.connections += 1
        state.writers.append(writer)
        try:
            while (command := await _read_command(reader)) is not None:
                name = command[0].upper()
                state.received[name] += 1
                if name == state.drop_on:
                    state.drop_on = None
                    writer.close()
                    return
                writer.write(_answer(command))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"redis://127.0.0.1:{port}/0", state
    finally:
        state.close_all()
        server.close()
        await server.wait_closed()


@pytest.fixture
def quick_health_check(monkeypatch: pytest.MonkeyPatch) -> float:
    """A health check after 50 ms idle instead of 5 s, so the tests stay fast."""
    monkeypatch.setattr(valkey_module, "HEALTH_CHECK_SECONDS", 0.05)
    return 0.05


def test_a_connection_dropped_while_idle_recovers_on_the_next_command(
    quick_health_check: float,
) -> None:
    async def scenario() -> None:
        async with fake_valkey() as (url, server):
            client = create_valkey(url)
            try:
                assert await client.get("k") == b"value"
                # Valkey restarts: the pooled connection is gone, the client has not noticed
                server.close_all()
                await asyncio.sleep(quick_health_check * 2)

                assert await client.get("k") == b"value"
                assert server.connections == 2
                assert server.received["GET"] == 2
            finally:
                await client.aclose()

    asyncio.run(scenario())


def test_a_failed_health_check_reconnects_once() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        class Probe(valkey_module._CheckedConnection):
            async def _send_ping(self) -> None:
                calls.append("ping")
                if len(calls) == 1:
                    raise ValkeyConnectionError("stale")

            async def disconnect(self, *args: object, **kwargs: object) -> None:
                calls.append("disconnect")

        connection = Probe(health_check_interval=1)
        await connection.check_health()

        assert calls == ["ping", "disconnect", "ping"]

    asyncio.run(scenario())


def test_a_second_failed_health_check_raises() -> None:
    async def scenario() -> None:
        class Dead(valkey_module._CheckedConnection):
            async def _send_ping(self) -> None:
                raise ValkeyConnectionError("down")

            async def disconnect(self, *args: object, **kwargs: object) -> None:
                pass

        with pytest.raises(ValkeyConnectionError):
            await Dead(health_check_interval=1).check_health()

    asyncio.run(scenario())


def test_no_health_check_on_a_connection_in_use() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        class Busy(valkey_module._CheckedConnection):
            async def _send_ping(self) -> None:
                calls.append("ping")

        connection = Busy(health_check_interval=60)
        connection.next_health_check = asyncio.get_running_loop().time() + 60
        await connection.check_health()

        assert calls == []

    asyncio.run(scenario())


def test_a_command_lost_in_flight_is_not_sent_again() -> None:
    async def scenario() -> None:
        async with fake_valkey() as (url, server):
            client = create_valkey(url)
            try:
                assert await client.get("k") == b"value"
                server.drop_on = "INCRBY"

                with pytest.raises(ValkeyConnectionError):
                    await client.incr("counter")

                # a retry could apply the command twice (a Lua script, MULTI, a job claim)
                assert server.received["INCRBY"] == 1
            finally:
                await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("url", "plain", "checked"),
    [
        ("redis://localhost:6379/0", Connection, valkey_module._CheckedConnection),
        ("rediss://localhost:6380/0", SSLConnection, valkey_module._CheckedSSLConnection),
        (
            "unix:///run/valkey.sock",
            UnixDomainSocketConnection,
            valkey_module._CheckedUnixConnection,
        ),
    ],
)
def test_every_url_scheme_gets_the_health_check(
    url: str, plain: type[AbstractConnection], checked: type[AbstractConnection]
) -> None:
    async def scenario() -> None:
        client = create_valkey(url)
        try:
            pool = client.connection_pool
            assert pool.connection_class is checked
            assert issubclass(checked, plain)
            assert (
                pool.connection_kwargs["health_check_interval"]
                == valkey_module.HEALTH_CHECK_SECONDS
            )
            assert pool.connection_kwargs["retry"].get_retries() == 0
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_reply_text_decodes_bytes_and_keeps_text() -> None:
    assert reply_text(b"gr\xc3\xbc\xc3\x9fe") == "grüße"
    assert reply_text("plain") == "plain"
