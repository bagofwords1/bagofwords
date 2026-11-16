"""How many queries BOW may run against one connection at the same time.

Rate limiting already bounds *how often* a connection is queried, and the
timeout bounds *how long* any one query may take. Neither bounds how many run
at once, and that is the number a struggling on-prem box actually feels: ten
concurrent analytical scans is ten sessions competing for the same buffer pool,
and on Oracle ten forked server processes. An agent naturally produces bursts —
a planner step that inspects six tables fires six queries with nothing between
them — so the burst is the normal case, not the pathological one.

This module only resolves the limit. Enforcement lives in
``app.data_sources.query_executions``: every query holds a row in
``query_executions`` while it runs, and a query may start only while fewer than
the limit are running on its connection — counted in the shared database, so
the limit is **app-wide** (all workers, all replicas), not per process. Queries
over the limit wait their turn (first come, first served) instead of failing; only
a query that cannot get a slot within its wait budget fails, with
``ConnectionBusyError``.
"""

import logging

# Re-exported: callers and tests import the error from here.
from app.data_sources.query_executions import ConnectionBusyError  # noqa: F401

logger = logging.getLogger(__name__)

# App-wide per connection (every worker and replica together). 12 keeps what a
# typical multi-worker install effectively had when the limit was per process
# (4 x 3 workers). It must not exceed one worker's engine pool
# (engine_pool.DEFAULT_POOL_SIZE + DEFAULT_MAX_OVERFLOW = 12): all of a
# connection's slots can land on one worker, and the limit, not the pool, must
# be what a burst hits — queueing here is explicit and reported, whereas pool
# exhaustion surfaces as an opaque SQLAlchemy TimeoutError after pool_timeout.
DEFAULT_MAX_CONCURRENT_QUERIES = 12


def effective_limit(client, organization_settings) -> int:
    """Resolve the app-wide concurrency limit for `client`'s connection.

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
