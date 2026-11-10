from app.data_sources.clients.progress import discovery_progress, discovery_items, discovery_phase, IndexingCancelled
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.fk_reflection import attach_foreign_keys
from app.data_sources.query_cancellation import track
from app.data_sources.query_tag import (
    SOURCE_QUERY_ID_ATTR,
    current_query_tag,
    render_query_tag,
)

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager, suppress
from typing import Dict, Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from snowflake.sqlalchemy import URL
import base64
import json
import logging
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)

# Per pooled DBAPI connection (`Connection.info` survives check-in/out):
# the QUERY_TAG this session currently has, and the id of the last statement.
_TAG_INFO_KEY = "bow_query_tag"
_LAST_QUERY_ID_KEY = "bow_last_query_id"


def _remember_query_id(conn, cursor, statement, parameters, context, executemany):
    """SQLAlchemy `after_cursor_execute` hook: keep Snowflake's query id.

    The id is on the driver cursor (`sfqid`), which pandas owns and discards,
    so it is captured here, without an extra round trip.
    """
    qid = getattr(cursor, "sfqid", None)
    if qid:
        conn.info[_LAST_QUERY_ID_KEY] = str(qid)


def _identifier_key(name: str) -> str:
    """An identifier the way Snowflake stores it: unquoted names are upper
    case, a double-quoted name keeps its case (minus the quotes)."""
    name = name.strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name.upper()


def _split_list(raw) -> List[str]:
    """Comma-separated config value -> stripped entries, first spelling of
    each identifier kept."""
    if not isinstance(raw, str):
        return []
    out, seen = [], set()
    for part in raw.split(","):
        part = part.strip()
        if part and _identifier_key(part) not in seen:
            seen.add(_identifier_key(part))
            out.append(part)
    return out


class SnowflakeClient(DataSourceClient):

    # A backslash escapes inside string literals here and '' is a quote too:
    # parameter values double both (app/ai/code_execution/query_params.py).
    sql_literal_style = "doubled"

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
        query_tag_enabled: bool = True,
        query_tag_label: Optional[str] = None,
    ):
        self.account = account
        # Tag every session with a JSON QUERY_TAG naming the BOW report, run
        # and user behind its queries (see app/data_sources/query_tag.py).
        # Off for customers whose own QUERY_TAG scheme must not be replaced.
        self.query_tag_enabled = query_tag_enabled is not False
        self.query_tag_label = query_tag_label
        self.user = user
        self.password = password
        self.private_key_pem = private_key_pem
        self.private_key_passphrase = private_key_passphrase
        self.role = role
        # Delegated per-user OAuth token (Snowflake OAuth security integration).
        # When set, the token carries the identity — no user/password/keypair.
        self.access_token = access_token
        # `database` may list several databases, comma-separated. The first is
        # the session's default database; the agent can join tables across all
        # of them in one query, because they share this account, role and
        # warehouse. As typed (used in SQL) and as Snowflake names them (used in
        # table names).
        self._databases = _split_list(database) or ([database] if database else [])
        self._database_names = [_identifier_key(d) for d in self._databases]
        self.database = self._databases[0] if self._databases else database
        # One database: tables are `SCHEMA.TABLE`, exactly as before. Several:
        # every table is `DATABASE.SCHEMA.TABLE`, so no name depends on which
        # database happens to be the default.
        self._multi_database = len(self._databases) > 1
        # Accept comma-separated schemas in the existing `schema` field.
        # `DB.SCHEMA` limits that database; a plain `SCHEMA` belongs to the
        # default database. A database with no entry is read in full.
        # Normalize to uppercase per Snowflake INFORMATION_SCHEMA behavior
        self.schema = schema
        self._schema_filter: Dict[str, List[str]] = {}
        default_name = self._database_names[0] if self._database_names else None
        for entry in _split_list(schema):
            db_part, dot, schema_part = entry.partition(".")
            if dot and _identifier_key(db_part) in self._database_names and schema_part.strip():
                target, schema_name = _identifier_key(db_part), schema_part.strip().upper()
            else:
                target, schema_name = default_name, entry.upper()
            bucket = self._schema_filter.setdefault(target, [])
            if schema_name not in bucket:
                bucket.append(schema_name)
        self._schemas = list(self._schema_filter.get(default_name, []))
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
            return self._instrument(sqlalchemy.create_engine(
                URL(**connect_args),
                connect_args={"token": self.access_token},
            ))

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

        return self._instrument(sqlalchemy.create_engine(URL(**connect_args)))

    def _instrument(self, engine):
        # Best effort: losing these hooks costs the query id and the tag on a
        # new session's first statements (checkout still tags it), never the
        # connection itself.
        try:
            sqlalchemy.event.listen(engine, "after_cursor_execute", _remember_query_id)
            sqlalchemy.event.listen(engine, "do_connect", self._tag_new_session)
        except Exception as e:
            logger.warning("Snowflake query tagging hooks not installed: %s", e)
        return engine

    def _tag_new_session(self, dialect, conn_rec, cargs, cparams) -> None:
        """SQLAlchemy `do_connect` hook: open new sessions already tagged.

        A fresh session runs the dialect's own setup statements before any
        checkout code sees it, so an ALTER issued at checkout would leave
        those untagged. Passing QUERY_TAG as a connect-time session parameter
        (snowflake.connector's `session_parameters`) tags them too.
        """
        if not self.query_tag_enabled:
            return
        tag = self._query_tag()
        params = dict(cparams.get("session_parameters") or {})
        params["QUERY_TAG"] = tag
        cparams["session_parameters"] = params
        # Same dict as Connection.info on every later checkout.
        conn_rec.info[_TAG_INFO_KEY] = tag

    def _query_tag(self) -> str:
        """The QUERY_TAG for a statement issued now, from this thread's scope."""
        return render_query_tag(current_query_tag(
            connection_id=getattr(self, "_bow_connection_id", None),
            data_source_id=getattr(self, "_bow_data_source_id", None),
            label=self.query_tag_label,
        ))

    def _apply_query_tag(self, conn) -> None:
        """Give this checkout's session the current tag.

        Runs on every checkout, not once per connection: the pool hands the
        same session to the next caller, and a tag left over from the previous
        one would attribute its queries to the wrong report. The ALTER is only
        sent when the tag changed, so a run's queries pay for it once per
        pooled session. Best effort: a failed ALTER never blocks the query.
        """
        if not self.query_tag_enabled:
            return
        tag = self._query_tag()
        if conn.info.get(_TAG_INFO_KEY) == tag:
            return
        try:
            # Tag values are restricted to [A-Za-z0-9 _.:/@-] (query_tag._clean),
            # so the JSON holds no quote or backslash; escape anyway.
            literal = tag.replace("\\", "\\\\").replace("'", "''")
            conn.exec_driver_sql(f"ALTER SESSION SET QUERY_TAG = '{literal}'")
            conn.info[_TAG_INFO_KEY] = tag
        except Exception as e:
            conn.info.pop(_TAG_INFO_KEY, None)
            logger.warning("Could not set Snowflake QUERY_TAG: %s", e)
            # Leave the connection clean for the query that follows.
            with suppress(Exception):
                conn.rollback()

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a Snowflake database."""
        conn = None
        try:
            engine = self.snowflake_engine
            conn = engine.connect()
        except IndexingCancelled:
            raise
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
            self._apply_query_tag(conn)
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
                conn.info.pop(_LAST_QUERY_ID_KEY, None)
                try:
                    # Wrap SQL query with text() to handle complex SQL
                    df = pd.read_sql(text(sql), conn)
                except Exception as e:
                    # A failed statement has a Snowflake query id too, and it
                    # is the one an admin needs to look the failure up.
                    qid = getattr(getattr(e, "orig", None), "sfqid", None) or conn.info.get(_LAST_QUERY_ID_KEY)
                    if qid:
                        with suppress(Exception):
                            setattr(e, SOURCE_QUERY_ID_ATTR, str(qid))
                    raise
                qid = conn.info.pop(_LAST_QUERY_ID_KEY, None)
            if qid:
                df.attrs[SOURCE_QUERY_ID_ATTR] = qid
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails, plus semantic views."""
        tables: List[Table] = []
        for db_sql, db_name in zip(self._databases, self._database_names, strict=True):
            try:
                tables.extend(self._get_tables_enriched(db_sql, db_name))
            except IndexingCancelled:
                raise
            except Exception:
                # Fallback to basic query without comments
                tables.extend(self._get_tables_basic(db_sql, db_name))

        # Append semantic views (failures here should not affect regular tables)
        try:
            tables.extend(self._get_semantic_views())
        except IndexingCancelled:
            raise
        except Exception:
            self.mark_discovery_incomplete("snowflake catalog")
            pass

        return tables

    @property
    def table_scope_default_database(self) -> Optional[str]:
        """Two-part names (stored or in agent SQL) resolve in this database."""
        return self._database_names[0] if self._database_names else None

    @property
    def table_scope_enforce_databases(self) -> bool:
        """Refuse tables from databases outside this connection. Connections
        with several databases are new, so nothing saved depends on the old
        lenient matching; single-database connections log it for now."""
        return self._multi_database

    def _table_name(self, db_name: str, schema: str, table: str) -> str:
        if self._multi_database:
            return f"{db_name}.{schema}.{table}"
        return f"{schema}.{table}"

    def _table_meta(self, db_name: str, schema: str) -> Dict:
        return {"schema": schema, "database": db_name}

    def table_name_aliases(self, name: str) -> List[str]:
        """The name this table had with the other naming style.

        Adding a second database renames `SCHEMA.TABLE` to
        `DEFAULT_DB.SCHEMA.TABLE` (and removing it renames it back). The schema
        refresh uses this to rename the stored table in place, so agents keep
        their table selection, instructions and history.
        """
        default = self.table_scope_default_database
        if not default:
            return []
        prefix = f"{default}."
        if self._multi_database:
            return [name[len(prefix):]] if name.startswith(prefix) and name.count(".") >= 2 else []
        return [prefix + name]

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
        discovery_phase('reading_semantic_views')
        tables = []
        for db_sql, db_name in zip(self._databases, self._database_names, strict=True):
            tables.extend(self._get_semantic_views_in(db_sql, db_name))
        return tables

    def _get_semantic_views_in(self, db_sql: str, db_name: str) -> List[Table]:
        """Semantic views of one database."""
        tables = []
        schemas = self._schema_filter.get(db_name, [])

        with self.connect() as conn:
            # Collect all semantic view rows
            sv_results = []
            if not schemas:
                # No schema filter — discover at database level
                try:
                    sv_results = conn.execute(
                        text(f"SHOW SEMANTIC VIEWS IN DATABASE {db_sql}")
                    ).fetchall()
                except IndexingCancelled:
                    raise
                except Exception:
                    self.mark_discovery_incomplete("snowflake catalog")
                    return tables
            else:
                for schema in schemas:
                    try:
                        rows = conn.execute(
                            text(f"SHOW SEMANTIC VIEWS IN SCHEMA {db_sql}.{schema}")
                        ).fetchall()
                        sv_results.extend(rows)
                    except IndexingCancelled:
                        raise
                    except Exception:
                        self.mark_discovery_incomplete("snowflake catalog")
                        continue

            for sv_row in discovery_items(sv_results, 'semantic_views', label=lambda row: str(row[0])):
                view_name = sv_row[1]  # name
                sv_schema = sv_row[3]  # schema_name
                fqn = self._table_name(db_name, sv_schema, view_name)

                # DESC SEMANTIC VIEW returns property rows with columns:
                #   (object_kind, object_name, parent_entity, property, property_value)
                # object_kind: NULL, TABLE, DIMENSION, FACT, METRIC, DERIVED_METRIC, etc.
                # We group by (object_kind, object_name) and collect properties.
                columns = []
                description = None
                try:
                    desc_results = conn.execute(
                        text(f"DESC SEMANTIC VIEW {db_sql}.{sv_schema}.{view_name}")
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
                except IndexingCancelled:
                    raise
                except Exception:
                    pass

                sv_meta = {**self._table_meta(db_name, sv_schema), "type": "semantic_view"}
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

    def _schema_where(self, db_name: str, column: str, params: Dict) -> str:
        """`WHERE` clause limiting one database to its configured schemas."""
        schemas = self._schema_filter.get(db_name, [])
        if not schemas:
            return ""
        in_keys = []
        for idx, sch in enumerate(schemas):
            key = f"s{idx}"
            in_keys.append(f":{key}")
            params[key] = sch
        return f" WHERE {column} IN ({', '.join(in_keys)})"

    def _get_tables_enriched(self, db_sql: str, db_name: str) -> List[Table]:
        """Get one database's tables with column/table comments. May fail on
        older Snowflake versions."""
        discovery_phase('reading_columns')
        tables = {}
        with self.connect() as conn:
            params = {}
            where_sql = self._schema_where(db_name, "c.table_schema", params)
            sql = text(f"""
                SELECT
                    c.table_schema,
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.comment AS column_comment,
                    t.comment AS table_comment
                FROM {db_sql}.INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN {db_sql}.INFORMATION_SCHEMA.TABLES t
                    ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                {where_sql}
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """)

            results = conn.execute(sql, params).fetchall()

            for row in discovery_items(results, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
                table_schema, table_name, column_name, data_type, col_comment, tbl_comment = row
                key = (table_schema, table_name)
                if key not in tables:
                    tables[key] = Table(
                        name=self._table_name(db_name, table_schema, table_name),
                        description=tbl_comment,
                        columns=[],
                        pks=None,
                        fks=None,
                        metadata_json=self._table_meta(db_name, table_schema),
                    )
                tables[key].columns.append(TableColumn(
                    name=column_name,
                    dtype=data_type,
                    description=col_comment
                ))

            self._attach_foreign_keys(conn, tables, db_name)

        return list(tables.values())

    def _get_tables_basic(self, db_sql: str, db_name: str) -> List[Table]:
        """Get one database's tables without comments (original query - always works)."""
        discovery_phase('metadata_fallback')
        tables = {}
        with self.connect() as conn:
            params = {}
            where_sql = self._schema_where(db_name, "table_schema", params)
            sql = text(f"""
                SELECT table_schema, table_name, column_name, data_type
                FROM {db_sql}.INFORMATION_SCHEMA.COLUMNS
                {where_sql}
                ORDER BY table_schema, table_name, ordinal_position
            """)

            results = conn.execute(sql, params).fetchall()

            for row in discovery_items(results, 'columns', label=lambda row: '.'.join(str(v) for v in row[:3])):
                table_schema, table_name, column_name, data_type = row
                key = (table_schema, table_name)
                if key not in tables:
                    tables[key] = Table(
                        name=self._table_name(db_name, table_schema, table_name), columns=[], pks=None, fks=None,
                        metadata_json=self._table_meta(db_name, table_schema),
                    )
                tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

            self._attach_foreign_keys(conn, tables, db_name)

        return list(tables.values())

    def _attach_foreign_keys(self, conn, tables, db_name: str) -> None:
        """Populate `fks` on one database's tables just collected.

        `name_fn` mirrors `_table_name` — the two must agree or downstream
        resolution, which is an exact string match, drops every edge without
        complaining.

        Snowflake declares foreign keys but does not enforce them, so most
        warehouses have none to find. Reflecting costs one call per schema and
        returns whatever was actually declared.
        """
        if not tables:
            return
        # The dialect reflects `DB.SCHEMA` (so a database other than the
        # session's default reads its own keys) and reports table names the
        # SQLAlchemy way, lower-cased, while INFORMATION_SCHEMA gives them upper
        # case. Match on the upper-cased (schema, table) pair, then name the
        # edge after the table actually collected.
        db_sql = self._databases[self._database_names.index(db_name)]

        def key_fn(schema, table):
            return (str(schema or "").split(".")[-1].upper(), str(table or "").upper())

        by_key = {key_fn(schema, table): t for (schema, table), t in tables.items()}

        def name_fn(schema, table):
            hit = by_key.get(key_fn(schema, table))
            if hit is not None:
                return hit.name
            return self._table_name(db_name, str(schema).split(".")[-1].upper(), str(table).upper())

        schemas = self._schema_filter.get(db_name) or sorted({schema for schema, _ in tables})
        attach_foreign_keys(
            conn,
            by_key,
            [f"{db_sql}.{schema}" for schema in schemas],
            name_fn,
            key_fn=key_fn,
        )
        # Column names come back lower-cased too: spell them (and type them)
        # as the collected columns do.
        by_name = {t.name: t for t in tables.values()}
        for table in tables.values():
            for fk in table.fks or []:
                self._match_column(fk.column, table)
                self._match_column(fk.references_column, by_name.get(fk.references_name))

    @staticmethod
    def _match_column(column: TableColumn, table: Optional[Table]) -> None:
        if table is None or column is None:
            return
        for col in table.columns or []:
            if col.name.upper() == str(column.name).upper():
                column.name = col.name
                column.dtype = column.dtype or col.dtype
                return
    def get_schema(self, table: str, schema: str) -> Table:
        """Return Table."""
        with self.connect() as conn:
            columns = []
            sql = text(f"SHOW COLUMNS IN {schema}.{table}")
            schema_list = conn.execute(sql).fetchall()

            for row in schema_list:
                columns.append(TableColumn(name=row[0], dtype=row[1]))

            return Table(name=f"{schema}.{table}", columns=columns, pks=None, fks=None, metadata_json={"schema": schema})

    @discovery_progress
    def get_schemas(self, progress_callback=None):
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
                if self._multi_database:
                    # A join needs this role to read every listed database, so
                    # a missing grant shows up now rather than mid-chat.
                    unreadable = []
                    for db_sql in self._databases:
                        try:
                            conn.execute(text(f"SELECT 1 FROM {db_sql}.INFORMATION_SCHEMA.SCHEMATA LIMIT 1"))
                        except Exception:
                            unreadable.append(db_sql)
                    if unreadable:
                        return {
                            "success": False,
                            "message": (
                                "Connected, but this role cannot read database(s): "
                                f"{', '.join(unreadable)}. Grant USAGE on them, or remove "
                                "them from the Database field."
                            ),
                        }
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
        if self._multi_database:
            where = (
                f"Snowflake databases {', '.join(self._database_names)} on account {self.account} "
                f"(default database: {self._database_names[0]}).\n"
                "Every table here is named DATABASE.SCHEMA.TABLE. Always write that full name in SQL. "
                "Tables from these databases can be joined in ONE query on this client; Snowflake runs "
                "the join, so do not query them separately and merge in pandas.\n"
            )
        else:
            where = f"Snowflake database {self.database} on account {self.account}.\n"
        return (
            where +
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
