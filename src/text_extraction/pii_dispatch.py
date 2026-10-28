"""The ``nlp`` pool's names: its pool and its queue (rulings N11, N13).

The nlp worker (``nlp_worker.py``) and the ``Worker`` of every pool need to name the pool; the hop
that sends results to it (``PiiHandoff``, ``PiiMove``) is added to this module with the requests'
side of the package.

**The ``nlp`` pool's items (the contract of the nlp worker).**

- Queue: ``nlp_queue(valkey, settings)``, pool ``nlp``, without the per-domain lease cap (nothing
  is fetched). Items are ``<job id>:<index>:<attempts>``. Claims are leased for
  ``jobs.pii_lease_seconds`` (``2 * PII_TIMEOUT_SECONDS + 10``).
- Job: ``JobStore.get``: its ``options.pii`` says what to do.
- Draft: ``JobStore.draft(job, index)``: the unprocessed envelope of the item. None when it
  expired with its job: the item then answers ``pii_failed``, retryable. The ``Worker`` deletes
  the draft once the item's result is written (also of an exhausted item). There are no input
  bytes.
- Result: the processed envelope, or ``pii_failed`` / ``too_large`` with the content withheld.
  An item that is lost too often (``JOB_ITEM_MAX_ATTEMPTS``) answers ``pii_failed``, retryable,
  never its draft.
"""

from typing import Final

from redis.asyncio import Redis

from text_extraction.scheduler import FairQueue
from text_extraction.settings import Settings

PII_POOL: Final = "nlp"
_PII_DOMAIN_CAP: Final = 1_000_000


def nlp_queue(valkey: Redis, settings: Settings) -> FairQueue:
    """The ``nlp`` pool's queue, as the API pods, the other workers and the nlp worker use it:
    its claims ignore the per-domain lease cap."""
    return FairQueue(valkey, settings, PII_POOL, domain_cap=_PII_DOMAIN_CAP)
