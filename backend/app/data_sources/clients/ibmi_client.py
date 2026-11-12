"""Db2 for IBM i (AS/400) client over Mapepire.

Mapepire is IBM's open-source database access layer for IBM i: a small server
on the IBM i (``yum install mapepire-server``, port 8076) and a pure-Python
PEP 249 client (``mapepire-python``) over a TLS WebSocket. Unlike `ibm_db`, it
needs no Db2 Connect license, no ODBC driver and runs on every architecture.

Every connection opens with SQL naming (``LIB.TABLE``) and ``access=read only``,
so the IBM i job itself refuses writes on top of the SQL read-only guard.
Catalog discovery is shared with the Db2 client (`_db2_catalog`, IBM i flavour).
"""

import logging
import os
import ssl
from collections.abc import Generator
from contextlib import contextmanager
from typing import Optional

import pandas as pd

from app.ai.prompt_formatters import Table, TableFormatter
from app.data_sources.clients import _db2_catalog as catalog
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_progress

logger = logging.getLogger(__name__)

# JDBC properties for the IBM i job behind the Mapepire connection.
JOB_PROPERTIES = {"naming": "sql", "access": "read only"}


def _system_ca_bundle() -> Optional[str]:
    """PEM text of the system trust store, for certificate verification.

    Mapepire's client, given no CA, downloads the server's own certificate and
    trusts it, which verifies nothing. Passing the system bundle makes it check
    the chain and host name like any TLS client.
    """
    for path in (os.environ.get("SSL_CERT_FILE"), ssl.get_default_verify_paths().cafile):
        if path and os.path.isfile(path):
            with open(path) as f:
                return f.read()
    try:
        import certifi

        with open(certifi.where()) as f:
            return f.read()
    except Exception:
        return None


def short_error(e: Exception) -> str:
    msg = str(e).strip()
    # Mapepire raises RuntimeError({'error': ..., 'sql_state': ..., 'sql_rc': ...}).
    if msg.startswith("{") and "'error'" in msg:
        try:
            import ast

            d = ast.literal_eval(msg)
            msg = d.get("error") or msg
            if d.get("sql_state"):
                msg += f" (SQLSTATE {d['sql_state']})"
        except Exception:
            pass
    if "TCP connection failed" in msg or "Connection refused" in msg:
        msg += " — is the Mapepire server running on the IBM i (sc start mapepire) and the port reachable?"
    return msg[:1000]


class IbmiClient(DataSourceClient):

    # Db2 for i SQL: '' only, a backslash is ordinary (IBM Db2 for i SQL
    # Reference, Constants).
    sql_literal_style = "standard"

    relative_date_hint = catalog.RELATIVE_DATE_HINT

    def __init__(self, host: str, port: int = 8076, user: str = "", password: str = "",
                 schema: Optional[str] = None, verify_ssl: bool = True,
                 ssl_ca: Optional[str] = None):
        self.host = host
        self.port = int(port or 8076)
        self.user = user
        self.password = password
        self.schema = schema
        self._schemas = catalog.parse_schemas(schema)
        self.verify_ssl = bool(verify_ssl)
        self.ssl_ca = (ssl_ca or "").strip() or None

    def _server(self):
        from mapepire_python.data_types import DaemonServer

        if self.verify_ssl:
            ca = self.ssl_ca or _system_ca_bundle()
            if not ca:
                raise RuntimeError("No trusted CA certificates found; paste the server certificate or turn off verification.")
            return DaemonServer(host=self.host, port=self.port, user=self.user,
                                password=self.password, ignoreUnauthorized=False, ca=ca)
        return DaemonServer(host=self.host, port=self.port, user=self.user,
                            password=self.password, ignoreUnauthorized=True)

    def _job_properties(self) -> dict:
        props = dict(JOB_PROPERTIES)
        if self._schemas:
            # Unqualified names resolve against the first listed library.
            props["libraries"] = self._schemas[0]
        return props

    @contextmanager
    def connect(self) -> Generator["object", None, None]:
        """Yield a Mapepire SQL job (one IBM i database job)."""
        from mapepire_python.client.sql_job import SQLJob

        job = None
        try:
            job = SQLJob(options=self._job_properties())
            job.connect(self._server())
        except IndexingCancelled:
            raise
        except Exception as e:
            if job is not None:
                try:
                    job.close()
                except Exception:
                    pass
            logger.debug("IBM i connect failed: %s", e)
            raise RuntimeError(short_error(e)) from None
        try:
            yield job
        finally:
            try:
                job.close()
            except Exception:
                pass

    @staticmethod
    def _run(job, sql: str, params=(), batch: int = 1000) -> tuple[list, list]:
        """Run one statement and return (column names, row tuples).

        Uses terse results (rows as arrays): Mapepire's default keys each row
        by column name, which silently drops one of two same-named columns
        (SELECT a.ID, b.ID). The DBAPI cursor cannot ask for terse rows on a
        prepared statement, so this drives the query API directly.
        """
        from mapepire_python.data_types import QueryOptions

        opts = QueryOptions(isTerseResults=True, parameters=list(params) if params else None)
        query = job.query(sql, opts)
        try:
            result = query.run(rows_to_fetch=batch)
            meta = result.metadata
            cols = [c.name for c in meta.columns] if meta and meta.columns else []
            rows = [tuple(r) for r in (result.data or [])]
            done = result.is_done
            while cols and not done:
                more = query.fetch_more(rows_to_fetch=batch)
                rows.extend(tuple(r) for r in (more.data or []))
                done = more.is_done
            return cols, rows
        finally:
            try:
                query.close()
            except Exception:
                pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        with self.connect() as conn:
            try:
                cols, rows = self._run(conn, sql)
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.debug("IBM i query failed: %s", e)
                raise RuntimeError(short_error(e)) from None
        return pd.DataFrame(rows, columns=cols)

    def get_tables(self) -> list[Table]:
        with self.connect() as conn:
            return catalog.build_tables(
                lambda sql, params: self._run(conn, sql, params)[1],
                catalog.IBMI,
                self._schemas,
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
        return {"success": True, "message": "Successfully connected to Db2 for IBM i"}

    @property
    def description(self):
        target = f"Db2 for IBM i at {self.host}"
        if self._schemas:
            target += f" (libraries: {', '.join(self._schemas)})"
        return f"""{target}

You can call the execute_query method to run SQL queries (Db2 for IBM i SQL dialect).

```python
df = client.execute_query('SELECT * FROM MYLIB.CUSTOMERS FETCH FIRST 100 ROWS ONLY')
```
{catalog.DIALECT_NOTES}- IBM i: use SQL naming LIBRARY.TABLE (not LIBRARY/TABLE). Libraries are schemas.
- Older files may have CHAR columns padded with blanks: TRIM() before grouping or comparing.
"""
