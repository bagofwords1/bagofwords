"""Cap how many queries BOW runs against one connection at the same time.

Rate limiting already bounds *how often* a connection is queried, and the
timeout bounds *how long* any one query may take. Neither bounds how many run
at once, and that is the number a struggling on-prem box actually feels: ten
concurrent analytical scans is ten sessions competing for the same buffer pool,
and on Oracle ten forked server processes. An agent naturally produces bursts —
a planner step that inspects six tables fires six queries with nothing between
them — so the burst is the normal case, not the pathological one.

The cap is a semaphore per connection, acquired for the whole query and
released when it returns. Queries over the cap **wait** rather than fail:
serialising a burst is the desired behaviour, and an error would just make the
agent retry and add load. Only when a query cannot get a slot within the same
wall-clock budget it would have been given to run does it fail — at that point
waiting longer buys nothing.

**Scope: per replica.** Like the SQLAlchemy pool sizes it sits in front of,
this is process-local; N replicas admit N × cap. Making it global would need
DB-backed leases with expiry (the counters that back rate limiting cannot
express "in flight"), and a per-replica cap already turns an unbounded burst
into a bounded one, which is the property that matters. The org-level default
should be read as "per replica" — `effective_limit` documents that at the point
of use.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Chosen to be smaller than the engine pool (5 + 5 overflow) so the semaphore,
# not the pool, is what a burst hits — queueing here is explicit and reported,
# whereas pool exhaustion surfaces as an opaque SQLAlchemy TimeoutError after
# pool_timeout has already been burned.
DEFAULT_MAX_CONCURRENT_QUERIES = 4

_lock = threading.Lock()
# connection_id -> (limit the semaphore was built for, semaphore)
_semaphores: Dict[str, Tuple[int, threading.BoundedSemaphore]] = {}
_in_flight: Dict[str, int] = {}


class ConnectionBusyError(Exception):
    """Raised when a query cannot get a concurrency slot in time.

    Surfaces to the agent like any other query error. The message names the cap
    so the model can react by running fewer queries at once rather than
    retrying the same burst.
    """

    def __init__(self, connection_name: Optional[str], limit: int, waited: float):
        target = connection_name or "this connection"
        super().__init__(
            f"Too many queries are already running against {target} "
            f"(limit {limit} concurrent). Waited {waited:.0f}s for a slot. "
            f"Run fewer queries at once — combine them into a single query, or "
            f"query sequentially instead of in parallel."
        )
        self.connection_name = connection_name
        self.limit = limit
        self.waited = waited


def effective_limit(client, organization_settings) -> int:
    """Resolve the per-replica concurrency cap for `client`'s connection.

    Same precedence as the query timeout: the connection's own config wins,
    then the org default, then the built-in. A connection setting can only be a
    positive integer; anything else is ignored rather than treated as
    "unlimited", because an accidental 0 must not remove the protection.
    """
    conn_value = getattr(client, "_bow_connection_max_concurrent_queries", None)
    if isinstance(conn_value, (int, float)) and conn_value > 0:
        return int(conn_value)
    if organization_settings is not None:
        try:
            cfg = organization_settings.get_config("max_concurrent_queries_per_connection")
            value = cfg.value if hasattr(cfg, "value") else cfg
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        except Exception:
            pass
    return DEFAULT_MAX_CONCURRENT_QUERIES


def _semaphore_for(connection_id: str, limit: int) -> threading.BoundedSemaphore:
    with _lock:
        entry = _semaphores.get(connection_id)
        if entry is not None and entry[0] == limit:
            return entry[1]
        # First use, or the admin changed the cap. Replacing the semaphore
        # means queries already holding the old one are not counted against the
        # new one, so the cap can be briefly exceeded after a change. That is
        # the accepted trade for not having to drain in-flight queries.
        sem = threading.BoundedSemaphore(limit)
        _semaphores[connection_id] = (limit, sem)
        return sem


@contextmanager
def slot(connection_id: Optional[str], limit: int, wait_seconds: float,
         connection_name: Optional[str] = None):
    """Hold a concurrency slot for `connection_id` for the duration of a query.

    A missing `connection_id` (indexing, connection tests, anything built
    outside the agent path) is a no-op — those paths are not the burst source
    and gating them would let a slow agent query block a health check.
    """
    if not connection_id:
        yield False
        return

    sem = _semaphore_for(str(connection_id), limit)
    import time
    t0 = time.monotonic()
    acquired = sem.acquire(timeout=max(1.0, float(wait_seconds)))
    waited = time.monotonic() - t0
    if not acquired:
        raise ConnectionBusyError(connection_name, limit, waited)
    if waited > 0.5:
        logger.info(
            "Query queued %.1fs for a slot on connection %s (limit %d)",
            waited, connection_id, limit,
        )
    with _lock:
        _in_flight[str(connection_id)] = _in_flight.get(str(connection_id), 0) + 1
    try:
        yield True
    finally:
        with _lock:
            remaining = _in_flight.get(str(connection_id), 1) - 1
            if remaining > 0:
                _in_flight[str(connection_id)] = remaining
            else:
                _in_flight.pop(str(connection_id), None)
        try:
            sem.release()
        except ValueError:
            # BoundedSemaphore guards against over-release, which can happen
            # once if the cap was changed mid-query and the semaphore swapped.
            logger.debug("Concurrency slot released after a cap change")


def in_flight(connection_id: str) -> int:
    """Test/observability helper: queries currently holding a slot."""
    with _lock:
        return _in_flight.get(str(connection_id), 0)


def reset() -> None:
    """Test helper — drop all semaphores."""
    with _lock:
        _semaphores.clear()
        _in_flight.clear()
