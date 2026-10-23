"""Valkey (Redis protocol) client. All shared state of the service lives there."""

from redis.asyncio import Redis
from redis.exceptions import RedisError

# Short timeouts: a Valkey that is down must fail a request fast, not hang it.
_SOCKET_TIMEOUT_SECONDS = 2


def create_valkey(url: str) -> Redis:
    """Build an async client for ``url``. It connects lazily, on the first command."""
    return Redis.from_url(
        url,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


async def valkey_ping(client: Redis) -> bool:
    """True when Valkey answers. Any connection problem is False; this never raises."""
    try:
        return bool(await client.ping())
    except (RedisError, OSError):
        return False
