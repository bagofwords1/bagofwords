from app.data_sources.clients.base import DataSourceClient

import pandas as pd
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter

# Schemas that belong to HANA itself (or SAP-delivered content), never to user
# data. Filtered out of introspection unless the user scopes schemas explicitly.
SYSTEM_SCHEMAS = ("SYS", "SYSTEMDB", "PUBLIC", "UIS", "HANA_XS_BASE", "SAP_XS_LM")
SYSTEM_SCHEMA_PREFIXES = ("_SYS", "SAP_", "XSSQLCC", "HDI_", "BROKER_")


class SapHanaClient(DataSourceClient):
    """SAP HANA / HANA Cloud / SAP Datasphere (Open SQL schema) client.

    Speaks plain SQL to the HANA SQL port via the official `hdbcli` DBAPI
    driver. For SAP Datasphere, connect with a space database user
    (SPACE#NAME) against the tenant's HANA Cloud endpoint (port 443, TLS) —
    views marked "Expose for Consumption" appear in the space schema.
    """

    def __init__(self, host, port: int = 443, user: str = None, password: str = None,
                 database: Optional[str] = None, schema: Optional[str] = None,
                 encrypt: bool = True, verify_ssl: bool = True):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.encrypt = encrypt
        self.verify_ssl = verify_ssl
        # Optional schema or comma-separated list of schemas
        self.schema = schema
        self._schemas = []
        if isinstance(self.schema, str) and self.schema.strip():
            seen = set()
            for p in [s.strip() for s in self.schema.split(",") if s.strip()]:
                if p not in seen:
                    seen.add(p)
                    self._schemas.append(p)

    @property
    def _connect_kwargs(self) -> dict:
        kwargs = {
            "address": self.host,
            "port": int(self.port),
            "user": self.user,
            "password": self.password,
            "encrypt": self.encrypt,
            "sslValidateCertificate": self.verify_ssl if self.encrypt else False,
        }
        if self.database:
            # Multitenant: route via the nameserver to a tenant by name.
            kwargs["databaseName"] = self.database
        if self._schemas:
            kwargs["currentSchema"] = self._schemas[0]
        return kwargs

    @contextmanager
    def connect(self) -> Generator["object", None, None]:
        """Yield an hdbcli DBAPI connection to SAP HANA."""
        from hdbcli import dbapi

        conn = None
        try:
            conn = dbapi.connect(**self._connect_kwargs)
            yield conn
        except Exception as e:
            raise RuntimeError(f"{e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(sql)
                    rows = cursor.fetchall()
                    columns = [d[0] for d in cursor.description] if cursor.description else []
                finally:
                    cursor.close()
            return pd.DataFrame([tuple(r) for r in rows], columns=columns)
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def _schema_filter(self, column: str) -> tuple:
        """WHERE fragment + params restricting to user schemas.

        With an explicit schema list, filter to it; otherwise exclude HANA
        system/SAP-delivered schemas so introspection returns user data only.
        """
        if self._schemas:
            placeholders = ", ".join(["?"] * len(self._schemas))
            return f"{column} IN ({placeholders})", list(self._schemas)
        clauses = [f"{column} NOT IN ({', '.join(['?'] * len(SYSTEM_SCHEMAS))})"]
        params = list(SYSTEM_SCHEMAS)
        for prefix in SYSTEM_SCHEMA_PREFIXES:
            clauses.append(f"{column} NOT LIKE ? ESCAPE '\\'")
            params.append(prefix.replace("_", "\\_") + "%")
        return " AND ".join(clauses), params

    def get_tables(self) -> List[Table]:
        """Get tables and views (Datasphere exposes views) with graceful fallback."""
        try:
            return self._get_tables_enriched()
        except Exception:
            return self._get_tables_basic()

    def _primary_keys(self, conn) -> dict:
        """Map (schema, table) -> ordered PK column names. Best-effort."""
        try:
            where_sql, params = self._schema_filter("SCHEMA_NAME")
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT SCHEMA_NAME, TABLE_NAME, COLUMN_NAME
                    FROM SYS.CONSTRAINTS
                    WHERE IS_PRIMARY_KEY = 'TRUE' AND {where_sql}
                    ORDER BY SCHEMA_NAME, TABLE_NAME, POSITION
                    """,
                    params,
                )
                pks: dict = {}
                for schema_name, table_name, column_name in cursor.fetchall():
                    pks.setdefault((schema_name, table_name), []).append(column_name)
                return pks
            finally:
                cursor.close()
        except Exception:
            return {}

    # Enriched and basic introspection share this shape: one row per column,
    # covering both tables and views — SAP Datasphere exposes only views
    # ("Expose for Consumption"), so views are first-class here.
    _ENRICHED_SQL = """
        SELECT
            c.SCHEMA_NAME, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE_NAME,
            c.COMMENTS AS COLUMN_COMMENT, t.COMMENTS AS OBJECT_COMMENT,
            'TABLE' AS OBJECT_TYPE, c.POSITION
        FROM SYS.TABLE_COLUMNS c
        LEFT JOIN SYS.TABLES t
            ON c.SCHEMA_NAME = t.SCHEMA_NAME AND c.TABLE_NAME = t.TABLE_NAME
        WHERE {table_filter}
        UNION ALL
        SELECT
            vc.SCHEMA_NAME, vc.VIEW_NAME, vc.COLUMN_NAME, vc.DATA_TYPE_NAME,
            vc.COMMENTS, v.COMMENTS, 'VIEW', vc.POSITION
        FROM SYS.VIEW_COLUMNS vc
        LEFT JOIN SYS.VIEWS v
            ON vc.SCHEMA_NAME = v.SCHEMA_NAME AND vc.VIEW_NAME = v.VIEW_NAME
        WHERE {view_filter}
        ORDER BY 1, 2, 8
    """

    _BASIC_SQL = """
        SELECT c.SCHEMA_NAME, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE_NAME,
               NULL AS COLUMN_COMMENT, NULL AS OBJECT_COMMENT,
               'TABLE' AS OBJECT_TYPE, c.POSITION
        FROM SYS.TABLE_COLUMNS c
        WHERE {table_filter}
        UNION ALL
        SELECT vc.SCHEMA_NAME, vc.VIEW_NAME, vc.COLUMN_NAME, vc.DATA_TYPE_NAME,
               NULL, NULL, 'VIEW', vc.POSITION
        FROM SYS.VIEW_COLUMNS vc
        WHERE {view_filter}
        ORDER BY 1, 2, 8
    """

    def _fetch_tables(self, sql_template: str, with_pks: bool) -> List[Table]:
        table_filter, table_params = self._schema_filter("c.SCHEMA_NAME")
        view_filter, view_params = self._schema_filter("vc.SCHEMA_NAME")
        sql = sql_template.format(table_filter=table_filter, view_filter=view_filter)
        with self.connect() as conn:
            pks = self._primary_keys(conn) if with_pks else {}
            cursor = conn.cursor()
            try:
                cursor.execute(sql, table_params + view_params)
                rows = cursor.fetchall()
            finally:
                cursor.close()

        tables: dict = {}
        for schema_name, table_name, column_name, dtype, col_comment, obj_comment, obj_type, _pos in rows:
            key = (schema_name, table_name)
            if key not in tables:
                tables[key] = Table(
                    name=f"{schema_name}.{table_name}",
                    description=obj_comment if obj_comment else None,
                    columns=[],
                    pks=[TableColumn(name=c, dtype="") for c in pks.get(key, [])],
                    fks=[],
                    metadata_json={"schema": schema_name, "object_type": obj_type},
                )
            tables[key].columns.append(TableColumn(
                name=column_name,
                dtype=dtype,
                description=col_comment if col_comment else None,
            ))
        return list(tables.values())

    def _get_tables_enriched(self) -> List[Table]:
        """Tables + views with comments and primary keys."""
        return self._fetch_tables(self._ENRICHED_SQL, with_pks=True)

    def _get_tables_basic(self) -> List[Table]:
        """Tables + views without comments (minimal privileges — always works)."""
        try:
            return self._fetch_tables(self._BASIC_SQL, with_pks=False)
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        """Get schemas for all tables in the specified database."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to SAP HANA and return status information."""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT 1 FROM DUMMY")
                    cursor.fetchall()
                finally:
                    cursor.close()
            return {
                "success": True,
                "message": "Successfully connected to SAP HANA"
            }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        system_prompt = """
        You can call the execute_query method to run SQL queries (SAP HANA SQL dialect).

        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query('SELECT * FROM "SALES"."ORDERS" LIMIT 100')
        ```
        or:
        ```python
        df = client.execute_query('SELECT REGION, SUM(REVENUE) AS REVENUE FROM "SALES"."V_REVENUE" GROUP BY REGION ORDER BY REVENUE DESC')
        ```

        SAP HANA SQL notes:
        - Identifiers: unquoted names fold to UPPERCASE. Mixed-case or dotted
          object names (common for SAP Datasphere views) MUST be double-quoted
          exactly as they appear in the schema: "MySpace"."Sales Orders View".
        - Row limits: use LIMIT n [OFFSET m] (or TOP n directly after SELECT).
        - Strings: single quotes for literals, || for concatenation.
        - Dates: CURRENT_DATE / CURRENT_TIMESTAMP, ADD_DAYS(date, n),
          ADD_MONTHS(date, n), TO_DATE('2026-01-31'), TO_VARCHAR(ts, 'YYYY-MM').
        - Nulls: IFNULL(x, y) or COALESCE(...).
        - The dummy table is DUMMY: SELECT CURRENT_DATE FROM DUMMY.
        - SAP Datasphere: only views marked "Expose for Consumption" in the
          space schema are readable — query them like any table/view.
        """
        target = f"SAP HANA database at {self.host}:{self.port}"
        if self.database:
            target += f" (tenant '{self.database}')"
        description = target + "\n\n" + system_prompt
        return description
