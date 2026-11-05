"""Minimal SQLAlchemy dialect over the official `teradatasql` driver.

It exists so the Teradata client can use the shared pooled engines in
`engine_pool` (pre-ping, recycle, idle reaper, ephemeral health probes) and the
per-thread registry in `query_cancellation`, like the other SQL clients. A
Teradata logon is slow and audited, so a new session per agent query is the
logon storm `engine_pool` was written to stop.

We never compile SQL through it: queries go to the driver as text
(`exec_driver_sql`, which is what pandas uses for a string), so no Teradata SQL
compiler or type map is needed. Teradata's own `teradatasqlalchemy` package was
not used because it adds telemetry query bands to the session and a much larger
surface than pooling needs.
"""

from typing import Any

from sqlalchemy.dialects import registry
from sqlalchemy.engine import default

DRIVERNAME = "teradatasql_bow"


class BowTeradataDialect(default.DefaultDialect):
    name = "teradata"
    driver = "teradatasql"
    paramstyle = "qmark"
    default_paramstyle = "qmark"
    positional = True
    supports_statement_cache = True
    supports_native_decimal = True
    supports_native_boolean = False
    supports_sane_rowcount = False
    postfetch_lastrowid = False
    implicit_returning = False
    max_identifier_length = 128

    @classmethod
    def import_dbapi(cls):
        import teradatasql

        return teradatasql

    def create_connect_args(self, url):
        # The driver takes every connection parameter as a string.
        params: dict[str, Any] = {}
        if url.host:
            params["host"] = url.host
        if url.port:
            params["dbs_port"] = str(url.port)
        if url.username:
            params["user"] = url.username
        if url.password is not None:
            params["password"] = url.password
        if url.database:
            params["database"] = url.database
        for k, v in url.query.items():
            params[k] = v if isinstance(v, str) else str(v)
        return [], params

    def _get_server_version_info(self, connection):
        return None

    def _get_default_schema_name(self, connection):
        return None

    def get_isolation_level(self, dbapi_connection):
        raise NotImplementedError()

    def _check_max_identifier_length(self, connection):
        return None

    def do_ping(self, dbapi_connection) -> bool:
        # Any failure means the session is unusable; returning False makes the
        # pool discard it and open a new one instead of raising to the caller.
        try:
            cur = dbapi_connection.cursor()
            try:
                cur.execute("SELECT 1")
                cur.fetchall()
            finally:
                cur.close()
            return True
        except Exception:
            return False

    def do_rollback(self, dbapi_connection) -> None:
        # The driver runs with autocommit on (its default), so there is never
        # an open transaction to roll back when a connection returns to the
        # pool. Skipping it saves a round trip per checkin. The read-only guard
        # refuses BT/ET/BEGIN, so nothing can open one.
        return None

    def do_commit(self, dbapi_connection) -> None:
        return None


registry.register(DRIVERNAME, __name__, "BowTeradataDialect")
