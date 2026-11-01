"""Valkey (Redis protocol) client. All shared state of the service lives there."""

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ConstantBackoff
from redis.exceptions import ConnectionError, RedisError

# Short timeouts: a Valkey that is down must fail a request fast, not hang it.
_SOCKET_TIMEOUT_SECONDS = 2
# One retry on a broken connection: a pooled connection goes stale when Valkey restarts or
# fails over, and the first command on it fails although Valkey is back. A command that
# timed out is not retried, since it may have run (a rate-limit count, a job claim).
_RETRY_BACKOFF_SECONDS = 0.05


def create_valkey(url: str) -> Redis:
    """Build an async client for ``url``. It connects lazily, on the first command."""
    return Redis.from_url(
        url,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        retry=Retry(ConstantBackoff(_RETRY_BACKOFF_SECONDS), 1, (ConnectionError,)),
    )


def reply_text(value: bytes | str) -> str:
    """A Valkey reply as text: the client hands out bytes, since it does not decode replies."""
    return value if isinstance(value, str) else value.decode()


async def valkey_ping(client: Redis) -> bool:
    """True when Valkey answers. Any connection problem is False; this never raises."""
    try:
        return bool(await client.ping())
    except (RedisError, OSError):
        return False
