"""Valkey (Redis protocol) client. All shared state of the service lives there."""

import asyncio
from typing import Final

from redis.asyncio import ConnectionPool, Redis, SSLConnection, UnixDomainSocketConnection
from redis.asyncio.connection import AbstractConnection, Connection
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as ValkeyConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as ValkeyTimeoutError

# Short timeouts: a Valkey that is down must fail a request fast, not hang it.
_SOCKET_TIMEOUT_SECONDS = 2
# A pooled connection idle for longer than this is checked with PING before its next command.
HEALTH_CHECK_SECONDS: Final = 5.0


async def _check_health(connection: AbstractConnection) -> None:
    """PING a connection that has been idle; a stale one is replaced once, then PINGed again.

    A Valkey restart or a lost peer leaves dead connections in the pool. The PING finds
    them before a command is sent. Commands themselves are never retried: a command lost
    in flight may have run already, and running a Lua script, a MULTI or a job claim twice
    is worse than one failed request.
    """
    if not connection.health_check_interval:
        return
    if asyncio.get_running_loop().time() <= connection.next_health_check:
        return
    try:
        await connection._send_ping()  # type: ignore[no-untyped-call]  # redis-py leaves it untyped
    except (ValkeyConnectionError, ValkeyTimeoutError):
        await connection.disconnect()
        # it connects again; a second failure is raised
        await connection._send_ping()  # type: ignore[no-untyped-call]


class _CheckedConnection(Connection):
    async def check_health(self) -> None:
        await _check_health(self)


class _CheckedSSLConnection(SSLConnection):
    async def check_health(self) -> None:
        await _check_health(self)


class _CheckedUnixConnection(UnixDomainSocketConnection):
    async def check_health(self) -> None:
        await _check_health(self)


_CHECKED: Final[dict[type[AbstractConnection], type[AbstractConnection]]] = {
    Connection: _CheckedConnection,
    SSLConnection: _CheckedSSLConnection,
    UnixDomainSocketConnection: _CheckedUnixConnection,
}


def create_valkey(url: str) -> Redis:
    """Build an async client for ``url``. It connects lazily, on the first command.

    The URL scheme (``redis://``, ``rediss://``, ``unix://``) picks the connection class;
    each gets the health check of ``_check_health`` and no command retries.
    """
    pool = ConnectionPool.from_url(
        url,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        health_check_interval=HEALTH_CHECK_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )
    pool.connection_class = _CHECKED.get(pool.connection_class, pool.connection_class)
    return Redis.from_pool(pool)


def reply_text(value: bytes | str) -> str:
    """A Valkey reply as text: the client hands out bytes, since it does not decode replies."""
    return value if isinstance(value, str) else value.decode()


async def valkey_ping(client: Redis) -> bool:
    """True when Valkey answers. Any connection problem is False; this never raises."""
    try:
        return bool(await client.ping())
    except (RedisError, OSError):
        return False
