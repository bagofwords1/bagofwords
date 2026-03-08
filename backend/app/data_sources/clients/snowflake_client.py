from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from snowflake.sqlalchemy import URL
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend


class SnowflakeClient(DataSourceClient):
    def __init__(
        self,
        account,
        user,
        warehouse,
        database,
        schema,
        password: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        private_key_passphrase: Optional[str] = None,
        role: Optional[str] = None,
    ):
        self.account = account
        self.user = user
        self.password = password
        self.private_key_pem = private_key_pem
        self.private_key_passphrase = private_key_passphrase
        self.role = role
        self.database = database
        # Accept comma-separated schemas in the existing `schema` field
        # Normalize to uppercase per Snowflake INFORMATION_SCHEMA behavior
        self.schema = schema
        self._schemas = []
        if isinstance(self.schema, str) and self.schema.strip():
            parts = [s.strip() for s in self.schema.split(",") if s.strip()]
            # Dedupe while preserving order
            seen = set()
            for p in parts:
                up = p.upper()
                if up not in seen:
                    seen.add(up)
                    self._schemas.append(up)
        # Primary schema for connection string (fall back to provided single schema)
        self._primary_schema = (
            self._schemas[0]
            if self._schemas
            else (self.schema.upper() if isinstance(self.schema, str) and self.schema else None)
        )
        self.warehouse = warehouse

    @cached_property
    def snowflake_engine(self):
        """Return a SQLAlchemy engine configured for either password or keypair auth."""
        connect_args = {
            "user": self.user,
            "account": self.account,
            "warehouse": self.warehouse,
            "database": self.database,
        }
        if self.role:
            connect_args["role"] = self.role

        # Prefer keypair auth when private key is provided
        if self.private_key_pem:
            pem_bytes = self.private_key_pem.encode("utf-8")
            password_bytes = (
                self.private_key_passphrase.encode("utf-8") if self.private_key_passphrase else None
            )
            try:
                private_key = serialization.load_pem_private_key(
                    pem_bytes,
                    password=password_bytes,
                    backend=default_backend(),
                )
            except Exception as e:
                raise RuntimeError(f"Invalid Snowflake private key: {e}")

            private_key_der = private_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            # Snowflake expects a base64-encoded DER string for keypair auth
            connect_args["private_key"] = base64.b64encode(private_key_der).decode("utf-8")
        else:
            # Fallback to password-based auth
            connect_args["password"] = self.password

        engine = sqlalchemy.create_engine(URL(**connect_args))
        return engine

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a Snowflake database."""
        engine = None
        conn = None

        try:
            engine = self.snowflake_engine
            conn = engine.connect()
            yield conn
        except Exception as e:
            raise RuntimeError(f"Error while connecting to Snowflake: {e}")

        finally:
            if conn is not None:
                conn.close()
            if engine is not None:
                engine.dispose()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Run SQL statement."""
        try:
            with self.connect() as conn:
                # Wrap SQL query with text() to handle complex SQL
                df = pd.read_sql(text(sql), conn)
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails, plus semantic views."""
        try:
            tables = self._get_tables_enriched()
        except Exception:
            # Fallback to basic query without comments
            tables = self._get_tables_basic()

        # Append semantic views (failures here should not affect regular tables)
        try:
            tables.extend(self._get_semantic_views())
        except Exception:
            pass

        return tables

    def _get_semantic_views(self) -> List[Table]:
        """Discover Snowflake semantic views and their columns/measures/dimensions."""
        tables = []
        schemas = self._schemas if self._schemas else ([self._primary_schema] if self._primary_schema else [])

        with self.connect() as conn:
            # Collect all semantic view rows
            sv_results = []
            if not schemas:
                # No schema filter — discover at database level
                try:
                    sv_results = conn.execute(
                        text(f"SHOW SEMANTIC VIEWS IN DATABASE {self.database}")
                    ).fetchall()
                except Exception:
                    return tables
            else:
                for schema in schemas:
                    try:
                        rows = conn.execute(
                            text(f"SHOW SEMANTIC VIEWS IN SCHEMA {self.database}.{schema}")
                        ).fetchall()
                        sv_results.extend(rows)
                    except Exception:
                        continue

            for sv_row in sv_results:
                view_name = sv_row[1]  # name
                sv_schema = sv_row[3]  # schema_name
                fqn = f"{sv_schema}.{view_name}"

                # DESC SEMANTIC VIEW returns property rows with columns:
                #   (object_kind, object_name, parent_entity, property, property_value)
                # object_kind: NULL, TABLE, DIMENSION, FACT, METRIC, DERIVED_METRIC, etc.
                # We group by (object_kind, object_name) and collect properties.
                columns = []
                description = None
                try:
                    desc_results = conn.execute(
                        text(f"DESC SEMANTIC VIEW {self.database}.{sv_schema}.{view_name}")
                    ).fetchall()

                    # Group properties by (object_kind, object_name)
                    objects = {}  # (kind, name) -> {property: value, ...}
                    for row in desc_results:
                        obj_kind = row[0]       # TABLE, DIMENSION, FACT, METRIC, etc.
                        obj_name = row[1]       # name of the object
                        # row[2] = parent_entity
                        prop_name = row[3] if len(row) > 3 else None
                        prop_value = row[4] if len(row) > 4 else None

                        # Semantic view-level comment (object_kind is NULL)
                        if obj_kind is None and prop_name == "COMMENT" and prop_value:
                            description = prop_value
                            continue

                        if obj_kind in ("DIMENSION", "FACT", "METRIC", "DERIVED_METRIC"):
                            key = (obj_kind, obj_name)
                            if key not in objects:
                                objects[key] = {}
                            if prop_name and prop_value is not None:
                                objects[key][prop_name] = prop_value

                    # Build TableColumn for each dimension/fact/metric
                    for (obj_kind, obj_name), props in objects.items():
                        kind = obj_kind.lower()  # dimension, fact, metric
                        # Map kind to simpler labels
                        if kind == "fact":
                            kind = "measure"
                        elif kind == "derived_metric":
                            kind = "metric"

                        col_metadata = {"kind": kind}
                        if props.get("EXPRESSION"):
                            col_metadata["expression"] = props["EXPRESSION"]
                        if props.get("SYNONYMS"):
                            col_metadata["synonyms"] = props["SYNONYMS"]

                        columns.append(TableColumn(
                            name=obj_name,
                            dtype=props.get("DATA_TYPE"),
                            description=props.get("COMMENT"),
                            metadata=col_metadata,
                        ))
                except Exception:
                    pass

                tables.append(Table(
                    name=fqn,
                    description=description,
                    columns=columns,
                    pks=None,
                    fks=None,
                    metadata_json={"schema": sv_schema, "type": "semantic_view"},
                ))

        return tables

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table comments. May fail on older Snowflake versions."""
        tables = {}
        with self.connect() as conn:
            params = {}
            where_clauses = []
            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    in_keys.append(f":{key}")
                    params[key] = sch
                where_clauses.append(f"c.table_schema IN ({', '.join(in_keys)})")
            elif self._primary_schema:
                params["schema"] = self._primary_schema
                where_clauses.append("c.table_schema = :schema")

            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            sql = text(f"""
                SELECT
                    c.table_schema,
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.comment AS column_comment,
                    t.comment AS table_comment
                FROM {self.database}.INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN {self.database}.INFORMATION_SCHEMA.TABLES t
                    ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                {where_sql}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """)

            results = conn.execute(sql, params).fetchall()

            for row in results:
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn,
                        description=tbl_comment,
                        columns=[],
                        pks=None,
                        fks=None,
                        metadata_json={"schema": table_schema}
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment
                ))

        return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        tables = {}
        with self.connect() as conn:
            params = {}
            where_clauses = []
            if self._schemas:
                in_keys = []
                for idx, sch in enumerate(self._schemas):
                    key = f"s{idx}"
                    in_keys.append(f":{key}")
                    params[key] = sch
                where_clauses.append(f"table_schema IN ({', '.join(in_keys)})")
            elif self._primary_schema:
                params["schema"] = self._primary_schema
                where_clauses.append("table_schema = :schema")

            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            sql = text(f"""
                SELECT table_schema, table_name, column_name, data_type
                FROM {self.database}.INFORMATION_SCHEMA.COLUMNS
                {where_sql}
                ORDER BY table_schema, table_name, ordinal_position
            """)

            results = conn.execute(sql, params).fetchall()

            for row in results:
                table_schema, table_name, column_name, data_type = row
                key = (table_schema, table_name)
                fqn = f"{table_schema}.{table_name}"
                if key not in tables:
                    tables[key] = Table(
                        name=fqn, columns=[], pks=None, fks=None, metadata_json={"schema": table_schema}
                    )
                tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

        return list(tables.values())

    def get_schema(self, table: str, schema: str) -> Table:
        """Return Table."""
        with self.connect() as conn:
            columns = []
            sql = text(f"SHOW COLUMNS IN {schema}.{table}")
            schema_list = conn.execute(sql).fetchall()

            for row in schema_list:
                columns.append(TableColumn(name=row[0], dtype=row[1]))

            return Table(name=f"{schema}.{table}", columns=columns, pks=None, fks=None, metadata_json={"schema": schema})

    def get_schemas(self):
        tables = self.get_tables()
        return tables

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test database connection and return status information."""
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {
                    "success": True,
                    "message": "Successfully connected to database"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        return (
            f"Snowflake database {self.database} on account {self.account}.\n"
            "This database may contain Snowflake Semantic Views. "
            "Semantic views use the SEMANTIC_VIEW() function instead of regular SQL:\n"
            "- Query with: SELECT * FROM SEMANTIC_VIEW(view_name DIMENSIONS ... METRICS ...)\n"
            "- DIMENSIONS: columns to group by (role=dimension)\n"
            "- METRICS: aggregated values (role=measure/metric)\n"
            "- Cannot combine FACTS and METRICS in the same query\n"
            "- Use WHERE inside SEMANTIC_VIEW() for filtering\n"
            "Examples:\n"
            '  df = client.execute_query("SELECT * FROM SEMANTIC_VIEW(schema.view_name DIMENSIONS customer_name METRICS total_revenue)")\n'
            '  df = client.execute_query("SELECT * FROM SEMANTIC_VIEW(schema.view_name DIMENSIONS region, order_date METRICS order_count, total_revenue WHERE region = \'US\')")'
        )
