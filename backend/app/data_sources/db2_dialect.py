"""Minimal SQLAlchemy dialect over IBM's `ibm_db` DBAPI (`ibm_db_dbi`).

It exists so the Db2 client can use the shared pooled engines in
`engine_pool` (pre-ping, recycle, idle reaper, ephemeral health probes) like the
other SQL clients: a Db2 connect is a full DRDA handshake (and on z/OS often a
RACF check), so a new connection per agent query is expensive.

SQL is never compiled through it: queries go to the driver as text
(`exec_driver_sql`, which is what pandas uses for a string), so no Db2 SQL
compiler or type map is needed.

The URL carries the pool identity; the driver itself takes one CLI connection
string, which `create_connect_args` assembles from the URL plus the
`connect_args` the client passes (TLS settings, current schema).
"""

from typing import Any

from sqlalchemy.dialects import registry
from sqlalchemy.engine import default

DRIVERNAME = "db2_bow"


def _escape(value: str) -> str:
    # CLI connection-string values containing ';' must be wrapped in braces.
    value = str(value)
    if ";" in value or value.startswith("{"):
        return "{" + value.replace("}", "}}") + "}"
    return value


def build_dsn(params: dict[str, Any]) -> str:
    return "".join(f"{k}={_escape(v)};" for k, v in params.items() if v not in (None, ""))


class BowDb2Dialect(default.DefaultDialect):
    name = "db2"
    driver = "ibm_db"
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
        import ibm_db_dbi

        return ibm_db_dbi

    def create_connect_args(self, url):
        params: dict[str, Any] = {
            "DATABASE": url.database,
            "HOSTNAME": url.host,
            "PORT": url.port,
            "PROTOCOL": "TCPIP",
            "UID": url.username,
            "PWD": url.password,
        }
        for k, v in url.query.items():
            params[k] = v
        return [], {"dsn": build_dsn(params)}

    def connect(self, *cargs, **cparams):
        extra = cparams.pop("dsn_extra", None) or {}
        dsn = cparams.pop("dsn")
        if extra:
            dsn += build_dsn(extra)
        return self.loaded_dbapi.connect(dsn, "", "")

    def _get_server_version_info(self, connection):
        return None

    def _get_default_schema_name(self, connection):
        return None

    def get_isolation_level(self, dbapi_connection):
        raise NotImplementedError()

    def _check_max_identifier_length(self, connection):
        return None

    def do_ping(self, dbapi_connection) -> bool:
        # Returning False makes the pool discard the connection and open a new
        # one instead of raising to the caller.
        try:
            cur = dbapi_connection.cursor()
            try:
                cur.execute("SELECT 1 FROM SYSIBM.SYSDUMMY1")
                cur.fetchall()
            finally:
                cur.close()
            return True
        except Exception:
            return False

    def do_rollback(self, dbapi_connection) -> None:
        # ibm_db runs with autocommit on (its default) and the read-only guard
        # refuses anything that writes, so there is never an open transaction
        # when a connection returns to the pool. Skipping it saves a round trip.
        return None

    def do_commit(self, dbapi_connection) -> None:
        return None


registry.register(DRIVERNAME, __name__, "BowDb2Dialect")
