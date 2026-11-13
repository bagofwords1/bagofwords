"""Make a Postgres session read literals the way the read-only guard did.

The guard reads Postgres text with `standard_conforming_strings` on: a
backslash in a plain literal is just a backslash. A server with the setting
off reads it as an escape, so the same text would hold other values, or end
a literal where the guard saw none. Before a read, the session's reported
setting is checked (psycopg2 keeps what the server reports, so this costs no
round trip) and switched on for the read when it is off; a text whose
literals hold a backslash is refused when that cannot be confirmed.

Servers older than 9.1, and Redshift, read backslashes in plain literals as
escapes whatever they report, so there the Redshift rule applies: no
backslash in a literal and no E-string at all.

Every SET runs as its own statement inside the read's transaction, which
ends in a rollback, so the session keeps the server's own setting.
"""

import logging

from app.ai.code_execution.sql_guard import ReadOnlyViolation

logger = logging.getLogger(__name__)

_SCS = "standard_conforming_strings"
# Set on the connection by `prepare_pg_session` when the read starts with
# the setting confirmed on.
_CONFIRMED = "bow_pg_standard_strings_on"
_FIRST_STANDARD_VERSION = 90100


def _reported(conn, name: str):
    return conn.connection.dbapi_connection.get_parameter_status(name)


def prepare_pg_session(conn, sql: str) -> None:
    """Get the session on `conn` ready to run `sql`, or refuse `sql`
    (`ReadOnlyViolation`). Runs before `sql`, inside its transaction."""
    from app.ai.code_execution.pg_lexer import lex_postgres

    dbapi = conn.connection.dbapi_connection
    version_text = str(dbapi.get_parameter_status("server_version") or "")
    if int(getattr(dbapi, "server_version", 0) or 0) < _FIRST_STANDARD_VERSION or "redshift" in version_text.lower():
        # No backslash survives this rule, so the setting cannot change
        # what the text holds.
        lex_postgres(sql, mode="redshift")
        conn.info[_CONFIRMED] = False
        return
    status = _reported(conn, _SCS)
    if status == "off":
        conn.exec_driver_sql(f"SET {_SCS} = on")
        status = _reported(conn, _SCS)
    conn.info[_CONFIRMED] = status == "on"
    if status != "on" and lex_postgres(sql).has_backslash_literal:
        raise ReadOnlyViolation(
            f"This server could not be confirmed to read string literals as written ({_SCS} is "
            f"{status or 'not reported'}), so a literal holding a backslash is not allowed. "
            "Write the value without a backslash, for example with chr(92)."
        )


def verify_pg_session_after(conn) -> None:
    """After the read: a session that no longer reads literals as written
    is invalidated, so the pool never hands it out again."""
    if conn.invalidated:
        # Already discarded (a read stopped early is cancelled that way).
        return
    if not conn.info.get(_CONFIRMED):
        return
    status = _reported(conn, _SCS)
    if status != "on":
        logger.warning("Postgres session changed %s to %s during a read; discarding the connection",
                       _SCS, status)
        conn.invalidate()
