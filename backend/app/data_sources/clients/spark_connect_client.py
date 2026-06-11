from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from contextlib import contextmanager
from typing import Generator, List, Optional
from urllib.parse import quote

from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter


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
    ):
        self.host = host
        self.port = int(port) if port is not None else 15002
        self.token = token
        # config values may arrive as strings from JSON; coerce truthy strings
        self.use_ssl = use_ssl if isinstance(use_ssl, bool) else str(use_ssl).lower() in ("true", "1", "yes")
        self.catalog = catalog or None

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
            yield spark
        except Exception as e:
            raise RuntimeError(f"Error connecting to Spark Connect: {e}")
        finally:
            if spark is not None:
                try:
                    spark.stop()
                except Exception:
                    pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL on the remote cluster and return the result as a DataFrame."""
        try:
            with self.connect() as spark:
                return spark.sql(sql).toPandas()
        except Exception as e:
            raise RuntimeError(f"Error executing SQL on Spark Connect: {e}")

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
