"""IBM Db2 client: Db2 LUW, Db2 for z/OS and Db2 for IBM i over DRDA.

Uses IBM's `ibm_db` driver (Apache-2.0, bundles the IBM CLI driver). Db2 LUW
needs nothing else. Db2 for z/OS and Db2 for IBM i additionally need a Db2
Connect entitlement: either activated on the server (`db2connectactivate`, the
usual case on z/OS) or a client license file in the driver's `license` folder.
Without one the server answers SQL1598N, which `test_connection` explains.

IBM ships no `ibm_db` build for Linux ARM64, so the connector is hidden from the
catalog where the driver is missing (`requires_module` in the registry).

The product is detected from the driver's DBMS name on first connect (or forced
with `platform`), and picks the matching system catalog (see `_db2_catalog`).
"""

import hashlib
import logging
import os
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from typing import Optional

import pandas as pd
import sqlalchemy
from sqlalchemy.engine import URL

from app.ai.prompt_formatters import Table, TableFormatter
from app.data_sources import db2_dialect
from app.data_sources.clients import _db2_catalog as catalog
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_progress
from app.data_sources.engine_pool import get_engine
from app.data_sources.query_cancellation import track

logger = logging.getLogger(__name__)

_CA_DIR = os.path.join(tempfile.gettempdir(), "bow-db2-ca")

# "[IBM][CLI Driver][DB2/LINUXX8664] SQL0204N  ..." -> "SQL0204N  ..."
_DRIVER_PREFIX = re.compile(r"^(?:.*?Failed:\s*)?(?:\[[^\]]*\])+\s*")


def short_error(e: Exception) -> str:
    msg = str(getattr(e, "orig", None) or e).strip()
    # SQLAlchemy appends "[SQL: ...]" and a docs link; keep the driver message.
    msg = msg.split("\n[SQL:", 1)[0].split("\n(Background on", 1)[0].strip()
    msg = _DRIVER_PREFIX.sub("", msg)
    if "SQL1598N" in msg:
        msg += (
            " — Db2 for z/OS and Db2 for IBM i require a Db2 Connect license: activate "
            "it on the server (db2connectactivate) or install the client license file "
            "in the driver's clidriver/license folder."
        )
    return msg[:1000]


class Db2Client(DataSourceClient):

    # Db2 SQL: a quote in a character constant is written '' and a backslash
    # is ordinary (IBM Db2 SQL Reference, Constants).
    sql_literal_style = "standard"

    relative_date_hint = catalog.RELATIVE_DATE_HINT

    # Catalog schema override, per platform. Not user-configurable: the test
    # simulation of z/OS points this at stand-in views because SYSIBM cannot
    # hold user objects on a Db2 LUW test server.
    catalog_schemas: dict = {}

    def __init__(self, host: str, port: int = 50000, database: str = "", user: str = "",
                 password: str = "", schema: Optional[str] = None, platform: str = "auto",
                 ssl: bool = False, ssl_ca: Optional[str] = None):
        self.host = host
        self.port = int(port or 50000)
        self.database = (database or "").strip()
        self.user = user
        self.password = password
        self.schema = schema
        self._schemas = catalog.parse_schemas(schema)
        self.platform = platform if platform in catalog.PLATFORMS else "auto"
        self.ssl = bool(ssl)
        self.ssl_ca = (ssl_ca or "").strip() or None
        self._detected: Optional[str] = None
        self._server_version: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @property
    def db2_uri(self) -> str:
        """Pool identity (host, port, database, user, password).

        Also what `connection_service._invalidate_engine_pool` disposes when the
        connection is edited or deleted.
        """
        return URL.create(
            db2_dialect.DRIVERNAME,
            username=self.user or None,
            password=self.password or None,
            host=self.host,
            port=self.port,
            database=self.database or None,
        ).render_as_string(hide_password=False)

    def _ca_file(self) -> str:
        """The CLI driver reads the server CA from a file; write the PEM once."""
        digest = hashlib.sha256(self.ssl_ca.encode()).hexdigest()[:32]
        os.makedirs(_CA_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(_CA_DIR, f"{digest}.pem")
        if not os.path.exists(path):
            fd, tmp = tempfile.mkstemp(dir=_CA_DIR)
            with os.fdopen(fd, "w") as f:
                f.write(self.ssl_ca + "\n")
            os.replace(tmp, path)
        return path

    def _connect_args(self) -> dict:
        extra: dict = {}
        if self.ssl:
            extra["SECURITY"] = "SSL"
            if self.ssl_ca:
                extra["SSLServerCertificate"] = self._ca_file()
        if self._schemas:
            extra["CURRENTSCHEMA"] = self._schemas[0]
        return {"dsn_extra": extra} if extra else {}

    @staticmethod
    def _require_driver():
        try:
            import ibm_db_dbi  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "The IBM Db2 driver (ibm_db) is not installed on this server. IBM does not "
                "publish it for Linux ARM64; run BOW on an x86_64 host to use this connector."
            ) from None

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.Connection, None, None]:
        """Yield a pooled SQLAlchemy connection to Db2."""
        self._require_driver()
        conn = None
        try:
            engine = get_engine(self.db2_uri, connect_args=self._connect_args())
            conn = engine.connect()
            if self._detected is None:
                self._detect(conn)
        except IndexingCancelled:
            raise
        except Exception as e:
            if conn is not None:
                conn.close()
            logger.debug("Db2 connect failed: %s", e)
            raise RuntimeError(short_error(e)) from None
        # The yield is outside the try above so exceptions raised by the
        # caller's `with` body keep their type (see postgresql_client.connect).
        try:
            with track(self, conn):
                yield conn
        finally:
            conn.close()

    def _detect(self, conn) -> None:
        raw = conn.connection.dbapi_connection
        name = getattr(raw, "dbms_name", None)
        self._server_version = getattr(raw, "dbms_ver", None)
        self._detected = catalog.platform_from_dbms_name(name)
        logger.info("Db2 server %s (%s) detected as %s", name, self._server_version, self._detected)

    @property
    def effective_platform(self) -> str:
        if self.platform != "auto":
            return self.platform
        return self._detected or catalog.LUW

    def _run(self, conn, sql: str, params=()) -> list:
        return [tuple(r) for r in conn.exec_driver_sql(sql, tuple(params)).fetchall()]

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute a SQL statement and return the result as a DataFrame."""
        with self.connect() as conn:
            try:
                # A str goes to exec_driver_sql: the text reaches Db2 as written.
                return pd.read_sql(sql, conn)
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.debug("Db2 query failed: %s", e)
                raise RuntimeError(short_error(e)) from None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def get_tables(self) -> list[Table]:
        with self.connect() as conn:
            return catalog.build_tables(
                lambda sql, params: self._run(conn, sql, params),
                self.effective_platform,
                self._schemas,
                catalog.queries_for(self.effective_platform,
                                    self.catalog_schemas.get(self.effective_platform)),
            )

    def get_schema(self, table_id: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_tables() instead.")

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    def prompt_schema(self):
        return TableFormatter(self.get_schemas()).table_str

    def test_connection(self):
        try:
            with self.connect() as conn:
                self._run(conn, "SELECT 1 FROM SYSIBM.SYSDUMMY1")
        except Exception as e:
            return {"success": False, "message": short_error(e)}
        label = catalog.PLATFORM_LABELS[self.effective_platform]
        version = f" {self._server_version}" if self._server_version else ""
        return {"success": True, "message": f"Successfully connected to {label}{version}"}

    @property
    def description(self):
        platform = self.effective_platform
        target = f"{catalog.PLATFORM_LABELS[platform]} database"
        if self.database:
            target += f" {self.database}"
        target += f" at {self.host}:{self.port}"
        if self._schemas:
            target += f" (schemas: {', '.join(self._schemas)})"
        extra = ""
        if platform == catalog.ZOS:
            extra = (
                "- z/OS sorts in EBCDIC order (lowercase < uppercase < digits); keep that "
                "in mind for ORDER BY and range filters on text.\n"
            )
        elif platform == catalog.IBMI:
            extra = (
                "- IBM i: use SQL naming LIBRARY.TABLE (not LIBRARY/TABLE). Libraries are schemas.\n"
            )
        return f"""{target}

You can call the execute_query method to run SQL queries (IBM Db2 SQL dialect).

```python
df = client.execute_query('SELECT * FROM SALES.ORDERS FETCH FIRST 100 ROWS ONLY')
```
or:
```python
df = client.execute_query("SELECT REGION, SUM(REVENUE) AS REVENUE FROM SALES.ORDERS WHERE ORDER_DATE >= CURRENT DATE - 30 DAYS GROUP BY REGION ORDER BY REVENUE DESC")
```
{catalog.DIALECT_NOTES}{extra}"""
