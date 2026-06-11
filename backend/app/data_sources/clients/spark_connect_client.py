from app.data_sources.clients.base import DataSourceClient

import re
import pandas as pd
from contextlib import contextmanager
from typing import Generator, List, Optional
from urllib.parse import quote

from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter


class SparkQueryGuardError(RuntimeError):
    """Raised when a query is rejected by the pre-flight EXPLAIN gate.

    The message is intentionally actionable so the agent/LLM can self-correct
    (e.g. add a partition filter or narrow the scanned range).
    """


# IEC byte units as printed by Spark's Utils.bytesToString (binary, 1024-based).
_SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
               "TiB": 1024 ** 4, "PiB": 1024 ** 5, "EiB": 1024 ** 6}
_SIZE_RE = re.compile(r"sizeInBytes=([\d.]+(?:E[+-]?\d+)?)\s*(B|KiB|MiB|GiB|TiB|PiB|EiB)")

# Spark uses Long.MaxValue (~8.0 EiB) as the default size when a relation has no
# stats. Treat anything at/above 1 EiB as "unknown" so the size gate fails open
# instead of rejecting every unstatted scan.
_UNKNOWN_SIZE_FLOOR = _SIZE_UNITS["EiB"]


def _parse_size_to_bytes(value: str, unit: str) -> int:
    return int(float(value) * _SIZE_UNITS[unit])


def _max_scan_bytes_from_plan(plan: str) -> Optional[int]:
    """Largest *known* sizeInBytes in an EXPLAIN COST plan, in bytes.

    Returns None when no usable estimate exists (all sizes are the unknown
    Long.MaxValue default), so callers can fail open on the size gate.
    """
    known = [
        b for v, u in _SIZE_RE.findall(plan)
        if (b := _parse_size_to_bytes(v, u)) < _UNKNOWN_SIZE_FLOOR
    ]
    return max(known) if known else None


def _has_unfiltered_partition_scan(plan: str) -> bool:
    """True if the plan scans a partitioned table with no partition predicate.

    Structural (not statistical): Spark emits `PartitionFilters: []` on a file
    scan only when the relation is partitioned but nothing pruned it. A scan
    with pushed predicates shows them inside the brackets.
    """
    return bool(re.search(r"PartitionFilters:\s*\[\s*\]", plan))


class SparkConnectClient(DataSourceClient):
    """Client for Spark Connect servers.

    Spark Connect lets us run queries against a remote Spark cluster using a
    thin, pure-Python gRPC client — no local JVM. BOW only sends the SQL string
    and receives the result; all scanning/joining/aggregation happens on the
    remote cluster. This is the resource-friendly alternative to running an
    in-process engine (e.g. DuckDB) on the BOW server itself.
    """

    def __init__(
        self,
        host: str,
        port: int = 15002,
        token: Optional[str] = None,
        use_ssl: bool = False,
        catalog: Optional[str] = None,
        database: Optional[str] = None,
        require_partition_filter: bool = False,
        max_scan_bytes: Optional[int] = None,
    ):
        self.host = host
        self.port = int(port) if port is not None else 15002
        self.token = token
        # config values may arrive as strings from JSON; coerce truthy strings
        self.use_ssl = use_ssl if isinstance(use_ssl, bool) else str(use_ssl).lower() in ("true", "1", "yes")
        self.catalog = catalog or None
        # Pre-flight EXPLAIN gate knobs
        self.require_partition_filter = (
            require_partition_filter if isinstance(require_partition_filter, bool)
            else str(require_partition_filter).lower() in ("true", "1", "yes")
        )
        self.max_scan_bytes = int(max_scan_bytes) if max_scan_bytes else None

        # Parse comma-separated databases (schemas) if provided
        self._databases: List[str] = []
        if isinstance(database, str) and database.strip():
            seen = set()
            for p in (s.strip() for s in database.split(",")):
                if p and p not in seen:
                    seen.add(p)
                    self._databases.append(p)

    @property
    def remote_url(self) -> str:
        """Build the sc:// connection string.

        Auth and transport options are appended as `;key=value` params after the
        path separator, per the Spark Connect connection-string spec.
        """
        url = f"sc://{self.host}:{self.port}/"
        params = []
        if self.use_ssl:
            params.append("use_ssl=true")
        if self.token:
            # token is opaque; quote to keep the connection string well-formed
            params.append(f"token={quote(str(self.token), safe='')}")
        if params:
            url += ";" + ";".join(params)
        return url

    @contextmanager
    def connect(self) -> Generator:
        """Yield a Spark Connect session, stopped when the block exits."""
        from pyspark.sql import SparkSession

        spark = None
        # Only connection setup is wrapped here; exceptions raised by the `with`
        # body (e.g. the pre-flight guard) must propagate untouched.
        try:
            builder = SparkSession.builder.remote(self.remote_url)
            # `create()` forces a fresh session (avoids reusing a cached global
            # session that may point at a different remote); fall back to
            # getOrCreate() on older clients that don't expose create().
            if hasattr(builder, "create"):
                spark = builder.create()
            else:
                spark = builder.getOrCreate()
            if self.catalog:
                try:
                    spark.catalog.setCurrentCatalog(self.catalog)
                except Exception:
                    # Not all catalog providers support setCurrentCatalog; schema
                    # discovery still scopes by catalog where it can.
                    pass
        except Exception as e:
            raise RuntimeError(f"Error connecting to Spark Connect: {e}")
        try:
            yield spark
        finally:
            if spark is not None:
                try:
                    spark.stop()
                except Exception:
                    pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL on the remote cluster and return the result as a DataFrame.

        Runs the pre-flight EXPLAIN gate (if enabled) in the *same* session so
        the planner's partition/file listing is cached and not paid twice.
        """
        try:
            with self.connect() as spark:
                self._run_guard(spark, sql)
                return spark.sql(sql).toPandas()
        except SparkQueryGuardError:
            raise
        except Exception as e:
            raise RuntimeError(f"Error executing SQL on Spark Connect: {e}")

    @property
    def _guard_enabled(self) -> bool:
        return bool(self.require_partition_filter or self.max_scan_bytes)

    def explain_cost(self, sql: str, spark=None) -> str:
        """Return the `EXPLAIN COST` plan text for a query (planning only, no scan).

        Uses `EXPLAIN COST <sql>` rather than DataFrame.explain() so the plan is
        returned as a string over Spark Connect (explain() prints to stdout).
        """
        def _do(s):
            rows = s.sql(f"EXPLAIN COST {sql}").collect()
            return rows[0][0] if rows else ""
        if spark is not None:
            return _do(spark)
        with self.connect() as s:
            return _do(s)

    def _run_guard(self, spark, sql: str) -> None:
        """Reject the query before execution if it violates the configured gates.

        Fails open if EXPLAIN itself errors (e.g. non-explainable statement): we
        only block on a *positive* violation, never on an inability to estimate.
        """
        if not self._guard_enabled:
            return
        try:
            plan = self.explain_cost(sql, spark=spark)
        except Exception:
            return  # can't explain -> don't block

        if self.require_partition_filter and _has_unfiltered_partition_scan(plan):
            raise SparkQueryGuardError(
                "Query rejected: it scans a partitioned table without filtering on a "
                "partition column, which would read all partitions. Add a predicate on "
                "the table's partition column(s) and retry."
            )

        if self.max_scan_bytes:
            est = _max_scan_bytes_from_plan(plan)
            if est is not None and est > self.max_scan_bytes:
                raise SparkQueryGuardError(
                    f"Query rejected: estimated scan size {est:,} bytes exceeds the "
                    f"configured limit of {self.max_scan_bytes:,} bytes. Narrow the query "
                    "(filter partitions, select fewer columns, or add a WHERE clause) and retry."
                )

    def _target_databases(self, spark) -> List[str]:
        """Resolve which databases to introspect."""
        if self._databases:
            return self._databases
        try:
            return [db.name for db in spark.catalog.listDatabases()]
        except Exception:
            # Fall back to the current database only
            try:
                return [spark.catalog.currentDatabase()]
            except Exception:
                return []

    def get_tables(self) -> List[Table]:
        """Discover tables/columns via the Spark catalog API.

        Uses catalog.listTables/listColumns rather than information_schema since
        the latter is not available on all catalog providers (e.g. Hive).
        """
        tables: List[Table] = []
        with self.connect() as spark:
            for db in self._target_databases(spark):
                try:
                    catalog_tables = spark.catalog.listTables(db)
                except Exception:
                    continue
                for t in catalog_tables:
                    cols: List[TableColumn] = []
                    try:
                        for c in spark.catalog.listColumns(t.name, db):
                            cols.append(TableColumn(
                                name=c.name,
                                dtype=getattr(c, "dataType", None) or "unknown",
                                description=getattr(c, "description", None) or None,
                            ))
                    except Exception:
                        # Skip columns we can't introspect; keep the table listed
                        pass
                    fqn = f"{db}.{t.name}" if db else t.name
                    tables.append(Table(
                        name=fqn,
                        description=getattr(t, "description", None) or None,
                        columns=cols,
                        pks=[],
                        fks=[],
                        metadata_json={"schema": db, "catalog": self.catalog} if db else {},
                    ))
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Deprecated — use get_tables() / get_schemas() instead."""
        raise NotImplementedError("get_schema() is deprecated. Use get_tables() instead.")

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    def test_connection(self) -> dict:
        try:
            with self.connect() as spark:
                spark.sql("SELECT 1").collect()
            return {"success": True, "message": "Successfully connected to Spark Connect"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        catalog_info = self.catalog or "default catalog"
        db_info = ", ".join(self._databases) if self._databases else "all databases"
        return f"""Spark Connect cluster
Host: {self.host}:{self.port}
Catalog: {catalog_info}
Databases: {db_info}

You can execute SQL queries using the execute_query method. Queries run on the
remote Spark cluster (Spark SQL syntax):
```python
df = client.execute_query("SELECT * FROM database.table_name LIMIT 10")
```
or:
```python
df = client.execute_query("SELECT product, SUM(amount) AS total FROM sales GROUP BY product")
```

Tables are addressed as `database.table` (or `catalog.database.table`).
"""
