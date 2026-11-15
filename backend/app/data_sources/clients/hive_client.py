"""Apache Hive / Apache Impala / Spark Thrift Server client (HiveServer2 protocol).

One client for every engine that speaks HiveServer2: Hive (port 10000),
Impala (21050) and the Spark Thrift Server (10000). The `engine` setting only
changes the dialect hints the agent gets and the SQL guard's dialect.

The catalog is read with SHOW DATABASES / SHOW TABLES / DESCRIBE, so it works
on all three engines without metastore access. Partition columns are flagged,
because filtering on them is what keeps a Hive query from reading a whole
table. Queries are single read statements (SQL guard, hive/spark dialect),
fetched in batches and capped at `max_rows`.

Auth: none (optionally as a named user) or LDAP username/password. Kerberos
deployments can front HiveServer2 with LDAP or use Knox; GSSAPI is not
bundled.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress

logger = logging.getLogger(__name__)

_DIALECT = {"hive": "hive", "impala": "hive", "spark": "spark"}
_ENGINE_NAME = {"hive": "Apache Hive", "impala": "Apache Impala", "spark": "Spark Thrift Server"}
_SYSTEM_DBS = {"information_schema", "sys", "_impala_builtins"}


class HiveClient(DataSourceClient):

    # HiveQL / Impala / Spark SQL: a backslash escapes inside a string and ''
    # is not a quote (sqlglot hive and spark tokenizers).
    sql_literal_style = "backslash"

    def __init__(
        self,
        host: str,
        port: int = 10000,
        engine: str = "hive",
        databases: str | None = None,
        max_rows: int = 100_000,
        username: str | None = None,
        password: str | None = None,
        **_ignored,
    ):
        super().__init__()
        self.host = (host or "").strip()
        self.port = int(port or 10000)
        self.engine = (engine or "hive").strip().lower()
        if self.engine not in _DIALECT:
            self.engine = "hive"
        self.databases = [d.strip() for d in (databases or "").replace("\n", ",").split(",") if d.strip()]
        self.max_rows = max(1, int(max_rows or 100_000))
        self.username = (username or "").strip() or None
        self.password = password or None
        self._conn = None

    @property
    def relative_date_hint(self) -> str:  # type: ignore[override]
        if self.engine == "impala":
            return "Relative dates (Impala): now(), current_date(), date_sub(current_date(), 7), trunc(now(), 'MM')."
        return "Relative dates (HiveQL): current_date, date_sub(current_date, 7), trunc(current_date, 'MM')."

    # ------------------------------------------------------------ connection

    def _connect(self):
        if self._conn is not None:
            return self._conn
        from pyhive import hive

        kwargs: dict[str, Any] = {"host": self.host, "port": self.port}
        if self.password:
            kwargs.update(username=self.username, password=self.password, auth="LDAP")
        else:
            kwargs.update(username=self.username or "bow", auth="NONE")
        self._conn = hive.connect(**kwargs)
        return self._conn

    def connect(self):
        return self._connect()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _fetch(self, sql: str, max_rows: int | None = None):
        cur = self._connect().cursor()
        try:
            cur.execute(sql)
            cols = [d[0].split(".", 1)[-1] if "." in d[0] else d[0] for d in (cur.description or [])]
            rows: list[tuple] = []
            limit = max_rows or self.max_rows
            truncated = False
            while True:
                batch = cur.fetchmany(min(10_000, limit + 1 - len(rows)))
                if not batch:
                    break
                rows.extend(batch)
                if len(rows) > limit:
                    rows = rows[:limit]
                    truncated = True
                    break
            return cols, rows, truncated
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # ----------------------------------------------------------------- query

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Run one read-only statement → DataFrame (capped at max_rows)."""
        from app.ai.code_execution.sql_guard import check_sql

        check_sql(sql, _DIALECT[self.engine])
        cols, rows, truncated = self._fetch(sql.strip().rstrip(";"))
        df = pd.DataFrame(rows, columns=cols)
        df.attrs["truncated"] = truncated
        return df

    # ---------------------------------------------------------------- catalog

    def _database_names(self) -> list[str]:
        _, rows, _ = self._fetch("SHOW DATABASES", 100_000)
        names = sorted(str(r[0]) for r in rows if str(r[0]).lower() not in _SYSTEM_DBS)
        if self.databases:
            wanted = {d.lower() for d in self.databases}
            names = [n for n in names if n.lower() in wanted]
        return names

    def _describe(self, db: str, table: str) -> Table:
        _, rows, _ = self._fetch(f"DESCRIBE `{db}`.`{table}`", 100_000)
        columns: list[TableColumn] = []
        partition_cols: list[str] = []
        section = "columns"
        seen = set()
        for r in rows:
            name = (str(r[0]) if r and r[0] is not None else "").strip()
            dtype = (str(r[1]) if len(r) > 1 and r[1] is not None else "").strip()
            comment = (str(r[2]) if len(r) > 2 and r[2] is not None else "").strip() or None
            if not name:
                continue
            if name.startswith("#"):
                if "partition" in name.lower():
                    section = "partitions"
                elif name.lower().startswith("# col_name"):
                    pass
                else:
                    section = "other"
                continue
            if section == "columns" and name not in seen:
                seen.add(name)
                columns.append(TableColumn(name=name, dtype=dtype, description=comment))
            elif section == "partitions":
                partition_cols.append(name)
        for c in columns:
            if c.name in partition_cols:
                c.description = ((c.description + "; ") if c.description else "") + "partition column"
        desc = f"Partitioned by {', '.join(partition_cols)}" if partition_cols else "Unpartitioned"
        return Table(
            name=f"{db}.{table}",
            description=desc + ".",
            columns=columns,
            pks=[],
            fks=[],
            metadata_json={"hive": {"database": db, "partition_columns": partition_cols}},
        )

    def get_tables(self) -> list[Table]:
        out: list[Table] = []
        idents = []
        for db in self._database_names():
            try:
                _, rows, _ = self._fetch(f"SHOW TABLES IN `{db}`", 100_000)
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.warning("hive: cannot list %s: %s", db, e)
                self.mark_discovery_incomplete(db)
                continue
            # Hive returns (tab_name); Spark returns (namespace, tableName, isTemporary).
            for r in rows:
                tname = r[1] if len(r) >= 3 else r[0]
                if len(r) >= 3 and r[2] in (True, "true"):
                    continue  # Spark temp views are session-local
                idents.append((db, str(tname)))
        for db, tname in discovery_items(idents, "tables", label=lambda i: f"{i[0]}.{i[1]}"):
            try:
                out.append(self._describe(db, tname))
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.warning("hive: cannot describe %s.%s: %s", db, tname, e)
                self.mark_discovery_incomplete(f"{db}.{tname}")
        return out

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Table | None:
        db, _, t = table_name.partition(".")
        return self._describe(db, t) if t else None

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    # ------------------------------------------------------------ lifecycle

    def test_connection(self):
        try:
            _, rows, _ = self._fetch("SELECT 1", 1)
            n = len(self._database_names())
            return {"success": True, "message": f"Connected to {_ENGINE_NAME[self.engine]} at {self.host}:{self.port}; {n} database(s) visible"}
        except Exception as e:
            return {"success": False, "message": str(e).split("\n")[0][:500]}
        finally:
            self.close()

    @property
    def description(self) -> str:
        engine = _ENGINE_NAME[self.engine]
        dialect = {"hive": "HiveQL", "impala": "Impala SQL", "spark": "Spark SQL"}[self.engine]
        return f"""{engine} at {self.host}:{self.port} — read-only {dialect}.

Use client.execute_query(sql) → pandas DataFrame (capped at {self.max_rows} rows; df.attrs['truncated'] is True when the cap cut it).
Rules:
1. Name tables database.table (backtick identifiers with special characters: `db`.`my-table`).
2. ALWAYS filter on partition columns (flagged in each table's description) when the question allows — otherwise the
   engine reads every file of the table, which can take minutes on large tables.
3. Aggregate in SQL; add LIMIT when exploring. Avoid SELECT * on wide tables.
4. Dates: {self.relative_date_hint}
Only SELECT is allowed.
"""
