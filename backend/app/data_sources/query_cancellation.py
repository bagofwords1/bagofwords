"""Cancel a running query on the *source* when BOW gives up on it.

`QueryCapturingClientWrapper` enforces a per-query wall-clock timeout by
running `execute_query` in a daemon thread and abandoning it on expiry. That
protects BOW, but it does nothing for the database: the statement keeps burning
CPU and I/O on the source until it finishes on its own. On the legacy on-prem
boxes this whole feature exists for, an abandoned 20-minute scan is exactly the
load we promised to stop generating — and an agent that retries a timing-out
query three times leaves three of them running.

So on timeout we also ask the source to stop. What "ask" means is per driver:

  * **Postgres** (psycopg2) — `connection.cancel()`. Documented as safe to call
    from another thread; sends a cancel request on a side channel. The statement
    aborts, the session survives.
  * **Oracle** (python-oracledb) — `connection.cancel()`, the OCI break. Same
    shape: statement aborts, session survives. This one matters most; Oracle
    forks a server process per session and an abandoned full scan pins it.
  * **MySQL / MariaDB** (pymysql) — no client-side cancel. `KILL QUERY <id>`
    from a side connection, which is what a DBA would do: it stops the statement
    and leaves the session alive. pymysql exposes the thread id locally, so
    there is no round trip to learn it.
  * **SQL Server** (pyodbc) — no cancel on the connection object (only on a
    cursor, which pandas owns). `KILL <spid>` from a side connection. Unlike the
    MySQL case this kills the *session*, not just the statement — noted in the
    outcome string so it is visible rather than surprising. The SPID is read
    once per physical connection and cached on it, so the cost is per pooled
    connection and not per query.
  * Everything else (Trino, Presto, Snowflake, …) — reported as unsupported.
    We deliberately do NOT close the connection out from under the driver: for
    the C-extension drivers that is a segfault risk, and the orphan thread ends
    up returning the connection to the pool by itself anyway.

The registry is keyed by (client, thread) rather than by client alone. One
client instance can have several queries in flight — an agent query and a
scheduled extraction, say — and a timeout on one must not shoot down the other.
The wrapper knows which thread it abandoned, so it can name it.

Cancellation is best effort by construction: the query may finish, the cancel
may lose a race, the side connection may fail. Every path returns a description
of what happened instead of raising, and callers record it.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# (id(client), thread ident) -> live SQLAlchemy Connection
_active: Dict[Tuple[int, int], Any] = {}


@contextmanager
def track(client: Any, sa_conn: Any):
    """Register `sa_conn` as this thread's live connection for `client`.

    Wraps the `yield` in every pooled client's `connect()`. Registration is
    cheap (a dict write) and unconditional; the per-driver work only happens if
    somebody actually calls `cancel_thread`.
    """
    key = (id(client), threading.get_ident())
    with _lock:
        _active[key] = sa_conn
    try:
        yield sa_conn
    finally:
        with _lock:
            _active.pop(key, None)


def cancel_thread(client: Any, thread_ident: int) -> str:
    """Cancel whatever `thread_ident` is running on `client`. Never raises.

    Returns a short human-readable outcome, which the caller logs and stores in
    the query timing record — "we timed out" and "the source stopped" are
    different facts and both belong in the trace.
    """
    with _lock:
        sa_conn = _active.get((id(client), thread_ident))
    if sa_conn is None:
        # The query finished between the timeout firing and this call.
        return "not_running"
    try:
        return _cancel(sa_conn)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Query cancellation failed: %s", e)
        return f"failed: {type(e).__name__}"


def active_count() -> int:
    """Test/observability helper."""
    with _lock:
        return len(_active)


# --------------------------------------------------------------------------
# Per-driver dispatch
# --------------------------------------------------------------------------

def _dialect_name(sa_conn: Any) -> str:
    try:
        return (sa_conn.engine.dialect.name or "").lower()
    except Exception:
        return ""


def _dbapi_connection(sa_conn: Any):
    """The raw driver connection under a SQLAlchemy Connection (2.0 API)."""
    fairy = getattr(sa_conn, "connection", None)
    if fairy is None:
        return None
    return getattr(fairy, "dbapi_connection", None) or fairy


def _cancel(sa_conn: Any) -> str:
    dialect = _dialect_name(sa_conn)
    raw = _dbapi_connection(sa_conn)
    if raw is None:
        return "unsupported: no driver connection"

    if dialect in ("postgresql", "oracle"):
        # Both drivers document cancel() as callable from another thread while
        # the owning thread blocks in execute().
        raw.cancel()
        return f"cancelled ({dialect} cancel)"

    if dialect in ("mysql", "mariadb"):
        thread_id = _pymysql_thread_id(raw)
        if thread_id is None:
            return "unsupported: no thread id"
        return _kill_via_side_connection(
            sa_conn, f"KILL QUERY {int(thread_id)}", f"statement killed (thread {thread_id})"
        )

    if dialect in ("mssql", "sql server"):
        spid = _mssql_spid(sa_conn)
        if spid is None:
            return "unsupported: no spid"
        return _kill_via_side_connection(
            sa_conn, f"KILL {int(spid)}", f"session killed (spid {spid})"
        )

    return f"unsupported: {dialect or 'unknown'}"


def _pymysql_thread_id(raw: Any) -> Optional[int]:
    """pymysql caches the server thread id from the handshake — local read."""
    try:
        getter = getattr(raw, "thread_id", None)
        if callable(getter):
            return getter()
        return getattr(raw, "server_thread_id", None)
    except Exception:
        return None


def _mssql_spid(sa_conn: Any) -> Optional[int]:
    """Read @@SPID once per physical connection and cache it there.

    `Connection.info` belongs to the pooled DBAPI connection, so this survives
    check-in/check-out and costs one round trip per connection lifetime rather
    than one per query. We cannot read it now — the connection is mid-query —
    so it has to have been captured at checkout by `capture_identity`.
    """
    try:
        return sa_conn.info.get("bow_spid")
    except Exception:
        return None


def capture_identity(sa_conn: Any) -> None:
    """Learn whatever a later KILL will need, while the connection is idle.

    Only SQL Server needs this (its SPID is server-side); everything else either
    cancels client-side or reads the id locally from the driver. Called once per
    physical connection from the SQL Server client's `connect()`.
    """
    try:
        if sa_conn.info.get("bow_spid") is not None:
            return
        from sqlalchemy import text
        spid = sa_conn.execute(text("SELECT @@SPID")).scalar()
        if spid is not None:
            sa_conn.info["bow_spid"] = int(spid)
    except Exception as e:
        # Losing the SPID costs us cancellation, not correctness.
        logger.debug("Could not capture SQL Server SPID: %s", e)


def _kill_via_side_connection(sa_conn: Any, statement: str, success: str) -> str:
    """Issue `statement` on a fresh connection to the same server.

    Not from the pool: the pool may be saturated (that is often *why* we are
    cancelling), and waiting `pool_timeout` seconds to deliver a KILL defeats
    the point. One short-lived connection on an exceptional path is the cheaper
    trade.
    """
    import sqlalchemy
    from sqlalchemy.pool import NullPool

    side = None
    try:
        # The URL carries the credentials for both dialects that reach here
        # (MySQL/MariaDB in the DSN, SQL Server inside odbc_connect), so it is
        # enough on its own — no connect_args to reconstruct.
        #
        # AUTOCOMMIT is not optional. SQLAlchemy opens a transaction on connect,
        # and SQL Server refuses KILL inside one:
        #   "KILL command cannot be used inside user transactions." (6115)
        # Verified against SQL Server 2022 — every cancellation failed this way
        # until the isolation level was set.
        side = sqlalchemy.create_engine(
            sa_conn.engine.url, poolclass=NullPool
        ).execution_options(isolation_level="AUTOCOMMIT")
        with side.connect() as c:
            c.exec_driver_sql(statement)
        return success
    except Exception as e:
        logger.warning("KILL failed (%s): %s", statement, e)
        return f"failed: {type(e).__name__}"
    finally:
        if side is not None:
            try:
                side.dispose()
            except Exception:
                pass
