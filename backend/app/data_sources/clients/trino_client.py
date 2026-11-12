"""Trino / Starburst (Galaxy and Enterprise) client.

One client serves three registry entries — ``trino``, ``starburst`` and the
legacy ``presto`` — because Starburst Galaxy and Starburst Enterprise *are*
Trino: same wire protocol, same ``trino`` driver, same SQL. What differs
between deployments is only the connection shape, and every one of those
differences is a constructor argument here:

* **TLS.** The driver infers HTTPS from port 443 and nothing else, so an
  Enterprise coordinator on 8443 (its default TLS port) silently got plain
  HTTP — with basic-auth credentials in the clear — until ``http_scheme`` was
  passed explicitly. Since driver 0.339 that is a hard ``TrinoAuthError``.
* **Certificate trust.** On-prem Enterprise behind a private CA needs a CA
  bundle (``ca_certificate``) or, for a lab, verification off.
* **Role.** Galaxy and Enterprise built-in access control pick what a login
  can see by *role*; a service account whose default role is restricted sees
  no tables unless the session role is set (``roles={"system": role}``).
* **Token auth.** Galaxy and Enterprise issue JWTs / personal tokens for
  programmatic access; the driver takes them as ``access_token``.
* **Federation.** A Starburst cluster fronts many catalogs; discovery can
  index more than one (``additional_catalogs``) and the agent joins across
  them with three-part names.
"""

from app.data_sources.clients.progress import (
    discovery_progress,
    discovery_items,
    discovery_phase,
    IndexingCancelled,
)
from app.data_sources.clients.base import DataSourceClient
import pandas as pd
import sqlalchemy

from app.data_sources.engine_pool import get_engine
from app.data_sources.query_cancellation import track
from sqlalchemy import text
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
from urllib.parse import quote_plus
import hashlib
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Sent as X-Trino-Source so Starburst's query history / audit attributes our
# traffic to us (the driver default, "trino-sqlalchemy", hides it among every
# other SQLAlchemy app).
QUERY_SOURCE = "bagofwords"

# Trino's per-catalog metadata schema; never a user table.
_SYSTEM_SCHEMAS = ("information_schema",)


def _split_list(value) -> List[str]:
    """Comma-separated config value -> ordered, de-duplicated list."""
    if not isinstance(value, str) or not value.strip():
        return []
    out: List[str] = []
    seen = set()
    for part in value.split(","):
        p = part.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _quote_ident(name: str) -> str:
    """Double-quote a Trino identifier (catalog / schema names come from the
    connection form, so they are quoted rather than trusted)."""
    return '"' + str(name).replace('"', '""') + '"'


class TrinoClient(DataSourceClient):

    # Trino: '' only (sqlglot trino).
    sql_literal_style = "standard"

    # The custom-query extractor bounds rows with LIMIT for this dialect.
    EXTRACTION_DIALECT = "trino"

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = (
        "Relative dates (Trino): current_date, current_date - interval '7' day, "
        "date_trunc('month', current_date), date_add('day', -30, current_date); "
        "the clock follows the session timezone (the connection's Timezone "
        "setting, otherwise the cluster default)."
    )

    def __init__(
        self,
        host,
        port=443,
        catalog=None,
        schema=None,
        user=None,
        password=None,
        protocol="https",
        verify_ssl: bool = True,
        ca_certificate: Optional[str] = None,
        role: Optional[str] = None,
        additional_catalogs: Optional[str] = None,
        timezone: Optional[str] = None,
        access_token: Optional[str] = None,
    ):
        self.host = host
        self.port = int(port) if port else (443 if str(protocol).lower() == "https" else 8080)
        self.catalog = (catalog or "").strip() or None
        # Optional schema or comma-separated list of schemas. Kept as the raw
        # string on `self.schema` for the legacy single-schema callers; the
        # parsed list drives discovery.
        self.schema = schema
        self._schemas = _split_list(schema)
        self.user = (user or "").strip() or None
        self.password = password or None
        self.protocol = (protocol or "https").strip().lower()
        if self.protocol not in ("http", "https"):
            raise ValueError("Protocol must be http or https")
        self.verify_ssl = bool(verify_ssl) if verify_ssl is not None else True
        self.ca_certificate = (ca_certificate or "").strip() or None
        self.role = (role or "").strip() or None
        self.additional_catalogs = [
            c for c in _split_list(additional_catalogs) if c != self.catalog
        ]
        self.timezone = (timezone or "").strip() or None
        self.access_token = (access_token or "").strip() or None
        if not self.catalog:
            raise ValueError("Catalog is required")
        if self.access_token and self.protocol != "https":
            raise ValueError("Token authentication requires HTTPS")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @property
    def catalogs(self) -> List[str]:
        """Primary catalog first, then the extra ones discovery also indexes."""
        return [self.catalog] + list(self.additional_catalogs)

    @cached_property
    def trino_uri(self):
        """SQLAlchemy URL. Identity (user / password / token) lives here so the
        shared engine pool keys engines per identity; everything about *how*
        to connect (scheme, trust, role, source, timezone) goes through
        `connect_args`, which the pool keys on too."""
        auth = ""
        if self.access_token:
            # A token may stand alone; `user` is then only an optional session
            # user the server may ignore.
            if self.user:
                auth = f"{quote_plus(self.user)}@"
        elif self.user:
            auth = quote_plus(self.user)
            # Credentials only travel over TLS. Over plain HTTP the driver
            # (0.339+) refuses a password outright, and before that it sent it
            # in the clear; a bare user is what an unauthenticated coordinator
            # expects anyway.
            if self.password and self.protocol == "https":
                auth += f":{quote_plus(self.password)}"
            auth += "@"
        path = quote_plus(self.catalog)
        if self._schemas:
            path += "/" + quote_plus(self._schemas[0])
        uri = f"trino://{auth}{self.host}:{self.port}/{path}"
        if self.access_token:
            uri += f"?access_token={quote_plus(self.access_token)}"
        return uri

    def _ca_bundle_path(self) -> Optional[str]:
        """Materialise the pasted CA PEM once per content hash. The driver
        (requests) wants a path; a deterministic name keeps the engine-pool key
        stable across client instances built from the same connection."""
        if not self.ca_certificate:
            return None
        digest = hashlib.sha256(self.ca_certificate.encode("utf-8")).hexdigest()[:16]
        path = os.path.join(tempfile.gettempdir(), f"bow-trino-ca-{digest}.pem")
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(self.ca_certificate.rstrip() + "\n")
        return path

    def _connect_args(self) -> Dict[str, Any]:
        """DBAPI kwargs the SQLAlchemy URL cannot carry (`http_scheme` and
        `timezone` are not URL parameters) or that are clearer here."""
        args: Dict[str, Any] = {
            "http_scheme": self.protocol,
            "source": QUERY_SOURCE,
        }
        if self.protocol == "https":
            if not self.verify_ssl:
                args["verify"] = False
            else:
                ca = self._ca_bundle_path()
                if ca:
                    args["verify"] = ca
        if self.role:
            args["roles"] = {"system": self.role}
        if self.timezone:
            args["timezone"] = self.timezone
        return args

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        conn = None
        try:
            engine = get_engine(self.trino_uri, connect_args=self._connect_args())
            conn = engine.connect()
        except IndexingCancelled:
            raise
        except Exception as e:
            logger.error(f"Error connecting to Trino: {e}")
            if conn is not None:
                conn.close()
            raise RuntimeError(f"{e}")
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
        # NB: no engine.dispose() — the engine is pooled and shared
        # (engine_pool). conn.close() above returns the connection.

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Run `sql` on a driver cursor we hold a handle to.

        The cursor is parked on the pooled connection's `info` for the
        duration of the statement so a timeout on another thread can
        `cancel()` it (see `query_cancellation`): the driver's cancel is a
        DELETE on the query's nextUri, safe to issue cross-thread, and the
        cluster then stops the query instead of running it to completion
        after we have stopped listening. Going through the raw cursor also
        means the SQL is sent verbatim — no SQLAlchemy `:name` parameter
        parsing, so `'12:30'` and `::` are never mistaken for bind markers.
        """
        try:
            with self.connect() as conn:
                raw = conn.connection.dbapi_connection
                cur = raw.cursor()
                conn.info["bow_trino_cursor"] = cur
                try:
                    cur.execute(sql)
                    rows = cur.fetchall()
                    columns = [d[0] for d in (cur.description or [])]
                finally:
                    conn.info.pop("bow_trino_cursor", None)
                    try:
                        cur.close()
                    except Exception:
                        pass
                return pd.DataFrame(rows, columns=columns)
        except Exception as e:
            logger.error(f"Error executing SQL query: {e}")
            raise RuntimeError(f"{e}")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _schema_filter(self, column: str, params: Dict[str, Any]) -> str:
        """WHERE fragment restricting to the configured schemas, or to every
        non-system schema when none is configured."""
        clauses = [f"{column} NOT IN ({', '.join(repr(s) for s in _SYSTEM_SCHEMAS)})"]
        if self._schemas:
            keys = []
            for idx, sch in enumerate(self._schemas):
                key = f"s{idx}"
                params[key] = sch
                keys.append(f":{key}")
            clauses.append(f"{column} IN ({', '.join(keys)})")
        return " AND ".join(clauses)

    def _table_name(self, catalog: str, schema: str, table: str) -> str:
        """`schema.table` in the primary catalog (the session catalog, so the
        name resolves as written); `catalog.schema.table` elsewhere."""
        if catalog == self.catalog:
            return f"{schema}.{table}"
        return f"{catalog}.{schema}.{table}"

    def get_tables(self) -> List[Table]:
        """Tables with columns, comments and view flags; falls back to the
        plain column listing if the enriched reads fail (older Presto without
        a `comment` column, or a role without access to system.metadata)."""
        try:
            return self._get_tables_enriched()
        except IndexingCancelled:
            raise
        except Exception:
            logger.exception("Trino enriched table introspection failed; falling back to basic query")
            return self._get_tables_basic()

    def _get_tables_enriched(self) -> List[Table]:
        discovery_phase('reading_columns')
        with self.connect() as conn:
            tables: Dict[tuple, Table] = {}
            for catalog in self.catalogs:
                params: Dict[str, Any] = {}
                where_sql = self._schema_filter("table_schema", params)
                cat = _quote_ident(catalog)

                # View flag per table.
                table_types: Dict[tuple, str] = {}
                for schema_name, table_name, table_type in conn.execute(text(f"""
                    SELECT table_schema, table_name, table_type
                    FROM {cat}.information_schema.tables
                    WHERE {where_sql}
                """), params).fetchall():
                    table_types[(schema_name, table_name)] = table_type

                # Table comments live in the system catalog, not information_schema.
                tbl_comments: Dict[tuple, str] = {}
                try:
                    c_params = dict(params)
                    c_params["catalog"] = catalog
                    c_where = self._schema_filter("schema_name", c_params)
                    for schema_name, table_name, comment in conn.execute(text(f"""
                        SELECT schema_name, table_name, comment
                        FROM system.metadata.table_comments
                        WHERE catalog_name = :catalog AND {c_where} AND comment IS NOT NULL
                    """), c_params).fetchall():
                        tbl_comments[(schema_name, table_name)] = comment
                except IndexingCancelled:
                    raise
                except Exception as e:
                    logger.info("Trino table comments unavailable for %s: %s", catalog, e)

                result = conn.execute(text(f"""
                    SELECT table_schema, table_name, column_name, data_type, comment
                    FROM {cat}.information_schema.columns
                    WHERE {where_sql}
                    ORDER BY table_schema, table_name, ordinal_position
                """), params).fetchall()

                for row in discovery_items(
                    result, 'columns',
                    label=lambda row, _c=catalog: '.'.join([_c] + [str(v) for v in row[:3]]),
                ):
                    schema_name, table_name, column_name, data_type, col_comment = row
                    key = (catalog, schema_name, table_name)
                    if key not in tables:
                        tbl_comment = tbl_comments.get((schema_name, table_name))
                        tables[key] = Table(
                            name=self._table_name(catalog, schema_name, table_name),
                            description=tbl_comment if tbl_comment else None,
                            columns=[],
                            pks=None,
                            fks=None,
                            metadata_json={
                                "catalog": catalog,
                                "schema": schema_name,
                                "type": table_types.get((schema_name, table_name)),
                            },
                        )
                    tables[key].columns.append(TableColumn(
                        name=column_name,
                        dtype=data_type,
                        description=col_comment if col_comment else None,
                    ))
            return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        discovery_phase('metadata_fallback')
        try:
            with self.connect() as conn:
                tables: Dict[tuple, Table] = {}
                for catalog in self.catalogs:
                    params: Dict[str, Any] = {}
                    where_sql = self._schema_filter("table_schema", params)
                    result = conn.execute(text(f"""
                        SELECT table_schema, table_name, column_name, data_type
                        FROM {_quote_ident(catalog)}.information_schema.columns
                        WHERE {where_sql}
                        ORDER BY table_schema, table_name, ordinal_position
                    """), params).fetchall()
                    for row in discovery_items(
                        result, 'columns',
                        label=lambda row, _c=catalog: '.'.join([_c] + [str(v) for v in row[:3]]),
                    ):
                        schema_name, table_name, column_name, data_type = row
                        key = (catalog, schema_name, table_name)
                        if key not in tables:
                            tables[key] = Table(
                                name=self._table_name(catalog, schema_name, table_name),
                                columns=[], pks=None, fks=None,
                                metadata_json={"catalog": catalog, "schema": schema_name},
                            )
                        tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))
                return list(tables.values())
        except IndexingCancelled:
            raise
        except Exception as e:
            self.mark_discovery_incomplete("trino catalog")
            logger.error(f"Error retrieving tables: {e}")
            return []

    def count_tables(self) -> int:
        """Cheap table + view count for the connection test (one row per
        object from information_schema.tables, not every column of every
        table — a federated catalog can be large and slow to introspect)."""
        total = 0
        with self.connect() as conn:
            for catalog in self.catalogs:
                params: Dict[str, Any] = {}
                where_sql = self._schema_filter("table_schema", params)
                total += int(conn.execute(text(f"""
                    SELECT count(*) FROM {_quote_ident(catalog)}.information_schema.tables
                    WHERE {where_sql}
                """), params).scalar() or 0)
        return total

    def get_schema(self, table_id: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_tables() instead.")

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    def prompt_schema(self):
        return TableFormatter(self.get_tables()).table_str

    def test_connection(self):
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {"success": True, "message": "Successfully connected to Trino"}
        except Exception as e:
            logger.error(f"Error testing Trino connection: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------

    @property
    def description(self):
        catalogs = ", ".join(self.catalogs)
        system_prompt = f"""
        You can call the execute_query method to run SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM {self._schemas[0] if self._schemas else 'schema'}.users LIMIT 100")
        ```

        Trino SQL rules (Starburst Galaxy / Enterprise speak the same dialect):
          - Table names in the schema listing are `schema.table` for the session
            catalog `{self.catalog}` and `catalog.schema.table` for the other
            indexed catalogs ({catalogs}). Use them exactly as listed; joins across
            catalogs are allowed and use the three-part names.
          - Quote identifiers with double quotes only (no backticks, no square
            brackets); identifiers are case-insensitive.
          - Integer division truncates: write CAST(x AS double) / y (or x * 1.0 / y)
            for a ratio.
          - Paginate with LIMIT n (or OFFSET n LIMIT m). There is no TOP.
          - Dates: date '2024-01-31', interval '7' day, date_trunc('month', d),
            date_add('day', -7, d), date_diff('day', d1, d2), date_format(ts, '%Y-%m').
          - approx_distinct(x) and approx_percentile(x, 0.5) are cheap on large tables.
          - There are no primary or foreign keys; infer joins from column names.
        """
        return (
            f"Trino database at {self.host}:{self.port}, catalog {self.catalog}"
            f"{', schemas ' + ', '.join(self._schemas) if self._schemas else ''}"
            f"{', also indexing catalogs ' + ', '.join(self.additional_catalogs) if self.additional_catalogs else ''}"
            f"\n\n{system_prompt}"
        )
