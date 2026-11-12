"""Apache Iceberg client — read-only SQL over an Iceberg catalog.

The catalog (Iceberg REST — Polaris, Nessie, Gravitino, Lakekeeper, Unity,
Snowflake Open Catalog … — AWS Glue, or Amazon S3 Tables) is the source of
truth: it lists namespaces and tables, their schemas, partition specs and
snapshots, and (for REST catalogs) hands out short-lived storage credentials.

Queries are DuckDB SQL. For each statement we:
  1. parse it (sqlglot, duckdb dialect) and find the Iceberg tables it names;
  2. plan an Iceberg scan per table — projecting only the columns the query
     mentions and, for single-table queries, pushing simple WHERE conjuncts
     down so partition / file pruning applies;
  3. refuse the query if the planned files hold more than `max_scan_rows`
     rows (the agent should add filters instead of pulling a whole lake);
  4. load the scans as Arrow into an in-memory DuckDB and run the SQL there.

Nothing needs DuckDB's network extensions, so it works air-gapped, and the
same path serves every catalog type PyIceberg supports. Read-only by
construction: there is no write path, and statements are checked by the SQL
guard (duckdb dialect) before planning.

Time travel: `execute_query(sql, as_of="2025-06-01T00:00:00Z")` or
`snapshot_id=…` reads every referenced table as of that point.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress

logger = logging.getLogger(__name__)

MAX_RESULT_ROWS = 200_000


class IcebergScanTooLarge(ValueError):
    """The planned scan exceeds max_scan_rows."""


class IcebergClient(DataSourceClient):

    # DuckDB SQL underneath: '' is the only quote escape (sqlglot duckdb
    # tokenizer).
    sql_literal_style = "standard"

    relative_date_hint = "Relative dates (DuckDB SQL): current_date, current_date - INTERVAL 7 DAY, date_trunc('month', current_date)."

    def __init__(
        self,
        catalog_type: str = "rest",
        uri: str | None = None,
        warehouse: str | None = None,
        region: str | None = None,
        namespaces: str | None = None,
        vended_credentials: bool = True,
        s3_endpoint: str | None = None,
        max_scan_rows: int = 20_000_000,
        extra_properties: str | None = None,
        # credentials (one variant's worth)
        token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        oauth2_server_uri: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        session_token: str | None = None,
        **_ignored,
    ):
        super().__init__()
        self.catalog_type = (catalog_type or "rest").strip().lower()
        self.uri = (uri or "").strip().rstrip("/") or None
        self.warehouse = (warehouse or "").strip() or None
        self.region = (region or "").strip() or None
        self.namespace_allowlist = [n.strip() for n in (namespaces or "").replace("\n", ",").split(",") if n.strip()]
        self.vended_credentials = bool(vended_credentials)
        self.s3_endpoint = (s3_endpoint or "").strip() or None
        self.max_scan_rows = max(1, int(max_scan_rows or 20_000_000))
        self.extra_properties = extra_properties
        self.token = token
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = (scope or "").strip() or None
        self.oauth2_server_uri = (oauth2_server_uri or "").strip() or None
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self._catalog = None

    # ---------------------------------------------------------------- catalog

    def _properties(self) -> dict[str, str]:
        props: dict[str, str] = {}
        aws: dict[str, str] = {}
        if self.region:
            aws["client.region"] = self.region
        if self.access_key and self.secret_key:
            aws["client.access-key-id"] = self.access_key
            aws["client.secret-access-key"] = self.secret_key
            if self.session_token:
                aws["client.session-token"] = self.session_token

        if self.catalog_type == "glue":
            props.update({"type": "glue", **aws})
            if self.warehouse:
                props["warehouse"] = self.warehouse
        elif self.catalog_type == "s3tables":
            if not self.region:
                raise ValueError("AWS Region is required for an S3 Tables catalog.")
            if not self.warehouse:
                raise ValueError("Warehouse must be the S3 table bucket ARN for an S3 Tables catalog.")
            props.update({
                "type": "rest",
                "uri": self.uri or f"https://s3tables.{self.region}.amazonaws.com/iceberg",
                "warehouse": self.warehouse,
                "rest.sigv4-enabled": "true",
                "rest.signing-name": "s3tables",
                "rest.signing-region": self.region,
                **aws,
            })
        elif self.catalog_type == "rest":
            if not self.uri:
                raise ValueError("Catalog URI is required for a REST catalog.")
            props.update({"type": "rest", "uri": self.uri})
            if self.warehouse:
                props["warehouse"] = self.warehouse
            if self.token:
                props["token"] = self.token
            elif self.client_id and self.client_secret:
                props["credential"] = f"{self.client_id}:{self.client_secret}"
                if self.scope:
                    props["scope"] = self.scope
                if self.oauth2_server_uri:
                    props["oauth2-server-uri"] = self.oauth2_server_uri
            if self.vended_credentials:
                props["header.X-Iceberg-Access-Delegation"] = "vended-credentials"
            props.update(aws)
        else:
            raise ValueError(f"Unknown Iceberg catalog type '{self.catalog_type}' (use rest, glue or s3tables).")

        if self.s3_endpoint:
            props["s3.endpoint"] = self.s3_endpoint
            props.setdefault("s3.path-style-access", "true")
        if self.extra_properties:
            try:
                extra = json.loads(self.extra_properties)
            except json.JSONDecodeError as e:
                raise ValueError(f"Extra catalog properties must be a JSON object: {e}") from e
            if not isinstance(extra, dict):
                raise ValueError("Extra catalog properties must be a JSON object.")
            props.update({str(k): str(v) for k, v in extra.items()})
        return props

    def _cat(self):
        if self._catalog is None:
            from pyiceberg.catalog import load_catalog

            self._catalog = load_catalog("bow", **self._properties())
        return self._catalog

    def connect(self):
        return self._cat()

    def _namespaces(self) -> list[tuple[str, ...]]:
        cat = self._cat()
        out: list[tuple[str, ...]] = []

        def walk(parent: tuple[str, ...], depth: int):
            try:
                children = cat.list_namespaces(parent) if parent else cat.list_namespaces()
            except IndexingCancelled:
                raise
            except Exception:
                if not parent:
                    raise
                return  # catalogs without nested namespaces reject a parent
            for ns in children:
                ns = tuple(ns)
                if parent and ns[: len(parent)] != parent:
                    ns = parent + ns  # some catalogs return the child segment only
                if ns in out:
                    continue
                out.append(ns)
                if depth < 3 and self.catalog_type == "rest":
                    walk(ns, depth + 1)

        walk((), 0)
        if self.namespace_allowlist:
            allowed = set(self.namespace_allowlist)
            out = [ns for ns in out if ".".join(ns) in allowed]
        return sorted(out)

    @staticmethod
    def _sql_name(identifier: tuple[str, ...]) -> str:
        """Catalog table name as the agent writes it in SQL: `namespace.table`
        (nested namespaces joined with `_` into one DuckDB schema)."""
        *ns, table = identifier
        return f"{'_'.join(ns)}.{table}"

    def _table_meta(self, identifier: tuple[str, ...]) -> Table:
        tbl = self._cat().load_table(identifier)
        schema = tbl.schema()
        cols = [
            TableColumn(name=f.name, dtype=str(f.field_type), description=f.doc or None)
            for f in schema.fields
        ]
        spec = tbl.spec()
        parts = []
        for pf in spec.fields:
            try:
                src = schema.find_field(pf.source_id).name
            except Exception:
                src = str(pf.source_id)
            parts.append(f"{pf.transform}({src})" if str(pf.transform) != "identity" else src)
        snap = tbl.current_snapshot()
        summary = dict(snap.summary.additional_properties) if snap and snap.summary else {}
        total = summary.get("total-records")
        desc_bits = []
        comment = (tbl.properties or {}).get("comment")
        if comment:
            desc_bits.append(comment.strip().rstrip("."))
        desc_bits.append(f"Partitioned by {', '.join(parts)}" if parts else "Unpartitioned")
        if total is not None:
            desc_bits.append(f"{int(total):,} rows")
        if snap:
            desc_bits.append(f"snapshot {snap.snapshot_id} at {datetime.fromtimestamp(snap.timestamp_ms / 1000, UTC):%Y-%m-%d %H:%M} UTC")
        return Table(
            name=self._sql_name(identifier),
            description=". ".join(desc_bits) + ".",
            columns=cols,
            pks=[],
            fks=[],
            metadata_json={"iceberg": {
                "identifier": list(identifier),
                "partition": parts,
                "total_records": int(total) if total is not None else None,
                "snapshot_id": snap.snapshot_id if snap else None,
                "location": tbl.location(),
                "format_version": tbl.format_version,
            }},
        )

    def _table_identifiers(self) -> list[tuple[str, ...]]:
        cat = self._cat()
        ids: list[tuple[str, ...]] = []
        for ns in self._namespaces():
            for ident in cat.list_tables(ns):
                ids.append(tuple(ident))
        return ids

    def get_tables(self) -> list[Table]:
        tables: list[Table] = []
        for ident in discovery_items(self._table_identifiers(), "tables", label=lambda i: ".".join(i)):
            try:
                tables.append(self._table_meta(ident))
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.warning("iceberg: could not load %s: %s", ".".join(ident), e)
                self.mark_discovery_incomplete(".".join(ident))
        return tables

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Table | None:
        ident = self._resolve_identifier(table_name)
        return self._table_meta(ident) if ident else None

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    # ----------------------------------------------------------------- query

    def _resolve_identifier(self, name: str, known: dict[str, tuple[str, ...]] | None = None) -> tuple[str, ...] | None:
        known = known if known is not None else {self._sql_name(i).lower(): i for i in self._table_identifiers()}
        return known.get(name.lower())

    def execute_query(
        self,
        sql: str,
        as_of: str | None = None,
        snapshot_id: int | None = None,
    ) -> pd.DataFrame:
        """Run one read-only DuckDB SQL statement over Iceberg tables → DataFrame."""
        import duckdb
        import sqlglot
        from sqlglot import exp

        from app.ai.code_execution.sql_guard import check_sql

        check_sql(sql, "duckdb")
        tree = sqlglot.parse_one(sql, read="duckdb")
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        refs: dict[str, exp.Table] = {}
        for t in tree.find_all(exp.Table):
            if not t.name or (not t.db and t.name.lower() in cte_names):
                continue
            key = f"{t.db}.{t.name}" if t.db else t.name
            refs.setdefault(key.lower(), t)
        if not refs:
            raise ValueError("The query names no Iceberg table. Use namespace.table, e.g. SELECT * FROM sales.orders LIMIT 10.")

        known = {self._sql_name(i).lower(): i for i in self._table_identifiers()}
        unknown = [k for k in refs if k not in known]
        if unknown:
            raise ValueError(
                f"Unknown Iceberg table(s): {', '.join(unknown)}. Name tables as namespace.table; available: "
                + ", ".join(sorted(known)[:30])
            )

        star = any(True for _ in tree.find_all(exp.Star))
        referenced_cols = {c.name.lower() for c in tree.find_all(exp.Column) if c.name}
        single_table = len(refs) == 1 and not any(True for _ in tree.find_all(exp.Join))

        con = duckdb.connect(":memory:")
        try:
            for i, key in enumerate(refs):
                ident = known[key]
                tbl = self._cat().load_table(ident)
                field_names = [f.name for f in tbl.schema().fields]
                if star:
                    selected: tuple[str, ...] = ("*",)
                else:
                    keep = [f for f in field_names if f.lower() in referenced_cols]
                    selected = tuple(keep or field_names[:1])
                row_filter = _pushdown_filter(tree, tbl.schema().fields) if single_table else None
                snap_id = self._snapshot_for(tbl, as_of, snapshot_id)
                kwargs: dict[str, Any] = {"selected_fields": selected, "case_sensitive": False}
                if snap_id is not None:
                    kwargs["snapshot_id"] = snap_id
                scan, tasks = None, None
                if row_filter is not None:
                    try:
                        scan = tbl.scan(row_filter=row_filter, **kwargs)
                        tasks = list(scan.plan_files())
                    except Exception as e:  # a literal that doesn't bind to the column type
                        logger.info("iceberg: pushdown skipped for %s: %s", key, e)
                        scan = None
                if scan is None:
                    scan = tbl.scan(**kwargs)
                    tasks = list(scan.plan_files())
                planned = sum(task.file.record_count for task in tasks)
                if planned > self.max_scan_rows:
                    raise IcebergScanTooLarge(
                        f"{key} would scan {planned:,} rows (limit {self.max_scan_rows:,}). Add a filter on a "
                        f"partition column ({', '.join(self._partition_sources(tbl)) or 'none'}) or a narrower WHERE."
                    )
                arrow = scan.to_arrow()
                schema_name, table_name = key.split(".", 1)
                view = f"__iceberg_scan_{i}"
                con.register(view, arrow)
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
                con.execute(f'CREATE OR REPLACE VIEW "{schema_name}"."{table_name}" AS SELECT * FROM {view}')
            result = con.execute(sql)
            rows = result.fetchmany(MAX_RESULT_ROWS + 1)
            cols = [d[0] for d in result.description]
            truncated = len(rows) > MAX_RESULT_ROWS
            df = pd.DataFrame(rows[:MAX_RESULT_ROWS], columns=cols)
            df.attrs["truncated"] = truncated
            return df
        finally:
            con.close()

    @staticmethod
    def _partition_sources(tbl) -> list[str]:
        schema = tbl.schema()
        out = []
        for pf in tbl.spec().fields:
            try:
                out.append(schema.find_field(pf.source_id).name)
            except Exception:
                pass
        return out

    @staticmethod
    def _snapshot_for(tbl, as_of: str | None, snapshot_id: int | None) -> int | None:
        if snapshot_id is not None:
            if tbl.snapshot_by_id(int(snapshot_id)) is None:
                raise ValueError(f"Snapshot {snapshot_id} does not exist for this table.")
            return int(snapshot_id)
        if as_of:
            ts = pd.Timestamp(as_of)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            snap = tbl.snapshot_as_of_timestamp(int(ts.timestamp() * 1000))
            if snap is None:
                raise ValueError(f"The table has no snapshot at or before {as_of}.")
            return snap.snapshot_id
        return None

    # ------------------------------------------------------------ lifecycle

    def test_connection(self):
        try:
            ns = self._namespaces()
            n_tables = sum(len(self._cat().list_tables(n)) for n in ns)
            return {"success": True, "message": f"Connected to the Iceberg catalog; {len(ns)} namespace(s), {n_tables} table(s)"}
        except Exception as e:
            return {"success": False, "message": str(e).split("\n")[0]}

    @property
    def description(self) -> str:
        where = {"glue": "AWS Glue", "s3tables": "Amazon S3 Tables"}.get(self.catalog_type, self.uri or "REST catalog")
        return f"""Apache Iceberg tables ({where}) — read-only DuckDB SQL.

Use client.execute_query(sql, as_of=None, snapshot_id=None) → pandas DataFrame.
Rules:
1. Name tables namespace.table exactly as in the schema (e.g. SELECT region, SUM(amount) FROM sales.orders GROUP BY region).
2. Write DuckDB SQL: date_trunc('month', ts), strftime, ILIKE, QUALIFY, list/struct functions all work; JOINs across Iceberg tables work.
3. Filter on partition columns (listed in each table's description) whenever you can: only matching files are read.
   A query that would read more than {self.max_scan_rows:,} rows is refused — narrow it with WHERE.
4. Select only the columns you need; SELECT * reads every column of every file.
5. Time travel: execute_query(sql, as_of='2025-06-01T00:00:00Z') or snapshot_id=… reads the tables as of then.
Only SELECT is allowed.
"""


# ------------------------------------------------------------ pushdown

_OPS = {
    "EQ": "EqualTo", "NEQ": "NotEqualTo", "GT": "GreaterThan", "GTE": "GreaterThanOrEqual",
    "LT": "LessThan", "LTE": "LessThanOrEqual",
}
_FLIP = {"GT": "LT", "GTE": "LTE", "LT": "GT", "LTE": "GTE", "EQ": "EQ", "NEQ": "NEQ"}


def _literal(node) -> tuple[bool, Any]:
    from sqlglot import exp

    if isinstance(node, exp.Literal):
        if node.is_string:
            return True, node.this
        try:
            v = float(node.this)
            return True, int(v) if v.is_integer() and "." not in node.this else v
        except ValueError:
            return False, None
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        ok, v = _literal(node.this)
        return (ok, -v) if ok else (False, None)
    if isinstance(node, exp.Boolean):
        return True, bool(node.this)
    return False, None


def _coerce(value: Any, field_type) -> Any:
    """SQL literal → the form PyIceberg binds for the column type. Agents
    write `ts >= '2025-03-01'`; Iceberg wants a full ISO-8601 timestamp."""
    from pyiceberg.types import DateType, TimestampType, TimestamptzType

    if isinstance(value, str) and isinstance(field_type, (DateType, TimestampType, TimestamptzType)):
        ts = pd.Timestamp(value)
        if isinstance(field_type, DateType):
            return ts.date().isoformat()
        if isinstance(field_type, TimestamptzType):
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
            return ts.isoformat()
        return (ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts).isoformat()
    return value


def _pushdown_filter(tree, fields):
    """AND of the WHERE conjuncts that compare a column of this table to a
    literal (=, <>, <, <=, >, >=, IN, BETWEEN) as a PyIceberg expression —
    used only to prune files; DuckDB re-applies the full WHERE afterwards, so
    skipping a conjunct is always safe."""
    from pyiceberg import expressions as ix
    from sqlglot import exp

    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None or select is not tree:
        return None  # only the outermost SELECT's WHERE is a filter on the table
    where = select.args.get("where")
    if where is None:
        return None
    lookup = {f.name.lower(): f.name for f in fields}
    types = {f.name: f.field_type for f in fields}

    def col(node) -> str | None:
        if isinstance(node, exp.Column):
            return lookup.get(node.name.lower())
        return None

    def lit(node, name):
        ok, val = _literal(node)
        if not ok or name is None:
            return False, None
        return True, _coerce(val, types[name])

    def conjuncts(node):
        if isinstance(node, exp.And):
            yield from conjuncts(node.left)
            yield from conjuncts(node.right)
        elif isinstance(node, exp.Paren):
            yield from conjuncts(node.this)
        else:
            yield node

    preds = []
    for c in conjuncts(where.this):
        try:
            key = type(c).__name__.upper()
            if key in _OPS:
                left, right = c.left, c.right
                name = col(left)
                ok, val = lit(right, name)
                op = key
                if not (name and ok):
                    name = col(right)
                    (ok, val), op = lit(left, name), _FLIP[key]
                if name and ok:
                    preds.append(getattr(ix, _OPS[op])(name, val))
            elif isinstance(c, exp.In) and not c.args.get("query"):
                name = col(c.this)
                vals = [lit(v, name) for v in c.expressions]
                if name and vals and all(ok for ok, _ in vals):
                    preds.append(ix.In(name, {v for _, v in vals}))
            elif isinstance(c, exp.Between):
                name = col(c.this)
                (ok1, lo), (ok2, hi) = lit(c.args["low"], name), lit(c.args["high"], name)
                if name and ok1 and ok2:
                    preds.append(ix.And(ix.GreaterThanOrEqual(name, lo), ix.LessThanOrEqual(name, hi)))
        except Exception:  # an unpushable conjunct is just not pushed
            continue
    if not preds:
        return None
    out = preds[0]
    for p in preds[1:]:
        out = ix.And(out, p)
    return out
