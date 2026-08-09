from app.data_sources.clients.base import DataSourceClient
from app.data_sources.query_cancellation import track

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import Dict, Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from snowflake.sqlalchemy import URL
import base64
import json
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend


class SnowflakeClient(DataSourceClient):

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (Snowflake): CURRENT_DATE (session timezone), DATEADD(day, -7, CURRENT_DATE), DATE_TRUNC('month', CURRENT_DATE)."

    def __init__(
        self,
        account,
        warehouse,
        database,
        # Optional: blank means "discover across the whole database". The
        # service layer strips empty config values before constructing the
        # client, so a required `schema` blew up as a missing positional arg
        # whenever the (optional) Schema field was left empty in the UI.
        schema: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        private_key_passphrase: Optional[str] = None,
        role: Optional[str] = None,
        access_token: Optional[str] = None,
    ):
        self.account = account
        self.user = user
        self.password = password
        self.private_key_pem = private_key_pem
        self.private_key_passphrase = private_key_passphrase
        self.role = role
        # Delegated per-user OAuth token (Snowflake OAuth security integration).
        # When set, the token carries the identity — no user/password/keypair.
        self.access_token = access_token
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

    # Declared rather than sniffed from a URI: the engine is built from a URL
    # *plus* connect_args, because a keypair private key cannot live in a URL.
    # There is no single string to detect, so the client says what it speaks.
    EXTRACTION_DIALECT = "snowflake"

    @cached_property
    def snowflake_engine(self):
        """Return a SQLAlchemy engine configured for OAuth, keypair, or password auth."""
        connect_args = {
            "account": self.account,
            "warehouse": self.warehouse,
            "database": self.database,
        }
        if self.role:
            connect_args["role"] = self.role

        # Delegated per-user OAuth token takes precedence: the token IS the
        # identity, and Snowflake rejects the login when a `user` that differs
        # from the token's subject is also sent — so `user` is only included
        # for the password/keypair paths below.
        if self.access_token:
            connect_args["authenticator"] = "oauth"
            # The token goes through connect_args rather than the URL: JWTs are
            # long and URL() would embed the secret in the engine's repr/logs.
            return sqlalchemy.create_engine(
                URL(**connect_args),
                connect_args={"token": self.access_token},
            )

        connect_args["user"] = self.user

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
        conn = None
        try:
            engine = self.snowflake_engine
            conn = engine.connect()
        except Exception as e:
            if conn is not None:
                conn.close()
            raise RuntimeError(f"Error while connecting to Snowflake: {e}")
        # The yield is deliberately OUTSIDE the try/except above. With it
        # inside, this contextmanager caught whatever the *caller* raised in
        # its `with client.connect()` body and re-raised it as a bare
        # RuntimeError, erasing the type: an extraction abort came back
        # indistinguishable from a connection failure. The except clause is
        # meant to wrap connect-time errors, and now only does.
        try:
            with track(self, conn):
                yield conn
        finally:
            conn.close()
            # NB: no engine.dispose(). `snowflake_engine` is a cached_property,
            # so disposing here tore down the pool on every query and made the
            # cache pointless — each call paid a fresh Snowflake session.

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

    @staticmethod
    def _key_list(raw) -> List[str]:
        """`DESC SEMANTIC VIEW` returns key columns as a JSON array in a string
        (e.g. '["DEPOT_KEY"]'). Fall back to the raw value if it isn't JSON."""
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(k) for k in parsed]
            return [str(parsed)]
        except Exception:
            return [str(raw)]

    @classmethod
    def _build_semantic_model_meta(cls, logical_tables: Dict, relationships: Dict) -> Dict:
        """Shape the logical tables and joins of a semantic view for the prompt.

        A semantic view is ONE queryable object whose columns span several base
        tables, so its joins are internal — the agent never writes them, but it
        does need to know they exist to understand that a dimension on one
        logical table can slice a metric on another (the whole point of
        `SEMANTIC_VIEW(... DIMENSIONS ... METRICS ...)`).
        """
        tables_out = []
        for alias, props in sorted(logical_tables.items()):
            entry = {"alias": alias}
            base = props.get("BASE_TABLE_NAME")
            if base:
                schema = props.get("BASE_TABLE_SCHEMA_NAME")
                entry["base_table"] = f"{schema}.{base}" if schema else base
            pk = cls._key_list(props.get("PRIMARY_KEY"))
            if pk:
                entry["primary_key"] = pk
            tables_out.append(entry)

        rels_out = []
        for name, props in sorted(relationships.items()):
            from_alias = props.get("TABLE") or props.get("FROM")
            to_alias = props.get("REF_TABLE")
            if not (from_alias and to_alias):
                continue
            rels_out.append({
                "name": name,
                "from_table": from_alias,
                "from_columns": cls._key_list(props.get("FOREIGN_KEY")),
                "to_table": to_alias,
                "to_columns": cls._key_list(props.get("REF_KEY")),
            })

        model = {}
        if tables_out:
            model["tables"] = tables_out
        if rels_out:
            model["relationships"] = rels_out
        return model

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

                    # Group properties by (object_kind, object_name).
                    #
                    # A semantic view describes MORE than its dimensions and
                    # metrics. `DESC` also returns TABLE rows (the logical alias
                    # -> base table mapping, plus primary keys) and RELATIONSHIP
                    # rows (how those logical tables join). Reading only
                    # DIMENSION/FACT/METRIC discarded both: the agent could not
                    # tell which base table a dimension came from, and had no
                    # idea the view's tables were related at all.
                    objects = {}          # (kind, name) -> {property: value}
                    logical_tables = {}   # alias -> {property: value}
                    relationships = {}    # rel name -> {property: value, from: alias}
                    for row in desc_results:
                        obj_kind = row[0]       # TABLE, DIMENSION, FACT, METRIC, RELATIONSHIP
                        obj_name = row[1]       # name of the object
                        parent_entity = row[2] if len(row) > 2 else None
                        prop_name = row[3] if len(row) > 3 else None
                        prop_value = row[4] if len(row) > 4 else None

                        # Semantic view-level comment (object_kind is NULL)
                        if obj_kind is None and prop_name == "COMMENT" and prop_value:
                            description = prop_value
                            continue

                        if obj_kind == "TABLE":
                            entry = logical_tables.setdefault(obj_name, {})
                            if prop_name and prop_value is not None:
                                entry[prop_name] = prop_value
                        elif obj_kind == "RELATIONSHIP":
                            entry = relationships.setdefault(obj_name, {"FROM": parent_entity})
                            if prop_name and prop_value is not None:
                                entry[prop_name] = prop_value
                        elif obj_kind in ("DIMENSION", "FACT", "METRIC", "DERIVED_METRIC"):
                            key = (obj_kind, obj_name)
                            if key not in objects:
                                objects[key] = {}
                            # The logical table this column belongs to. Without
                            # it every column looks like it comes from one flat
                            # object, which is what a semantic view is NOT.
                            if parent_entity:
                                objects[key]["_PARENT"] = parent_entity
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
                        if props.get("_PARENT"):
                            col_metadata["table"] = props["_PARENT"]
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

                sv_meta = {"schema": sv_schema, "type": "semantic_view"}
                model = self._build_semantic_model_meta(logical_tables, relationships)
                if model:
                    sv_meta["semantic_model"] = model

                tables.append(Table(
                    name=fqn,
                    description=description,
                    columns=columns,
                    pks=None,
                    # NOT fks: a foreign key renders as a reference to another
                    # INDEXED table, and these joins are internal to this single
                    # semantic view. They are carried as model metadata instead.
                    fks=None,
                    metadata_json=sv_meta,
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
