"""Apache Cassandra client (read-only CQL) — also ScyllaDB and Amazon Keyspaces.

Cassandra has a real schema, so the catalog comes from `system_schema`: every
table with typed columns, its partition key and clustering columns. Those keys
are spelled out in each table's description because CQL can only filter
efficiently on them — anything else needs `ALLOW FILTERING` (a cluster-wide
scan), and CQL has no JOIN and only partition-scoped aggregation.

Queries are single CQL `SELECT`s, paged by the driver and capped at
`max_rows`; the DataFrame is flagged `truncated` when the cap stops it.
Writes, DDL and batches are refused before anything is sent.
"""
from __future__ import annotations

import logging
import ssl as _ssl
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients._document_schema import ensure_single_select, to_plain
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress

logger = logging.getLogger(__name__)

_SYSTEM_KEYSPACES = {
    "system", "system_auth", "system_schema", "system_distributed", "system_traces",
    "system_views", "system_virtual_schema", "dse_system", "dse_security", "dse_auth",
    "dse_insights", "dse_insights_local", "dse_leases", "dse_perf", "dse_system_local",
    "solr_admin", "system_distributed_everywhere", "system_replicated_keys",
    # Amazon Keyspaces' own tables
    "system_multiregion_info",
}


class CassandraClient(DataSourceClient):

    # CQL: a quote in a string constant is written '', a backslash is ordinary
    # (CQL reference, Constants).
    sql_literal_style = "standard"

    def __init__(
        self,
        contact_points: str,
        port: int = 9042,
        keyspaces: str | None = None,
        local_datacenter: str | None = None,
        ssl: bool = False,
        verify_ssl: bool = True,
        max_rows: int = 5000,
        username: str | None = None,
        password: str | None = None,
        connect_timeout: int = 15,
        request_timeout: int = 60,
        **_ignored,
    ):
        super().__init__()
        self.contact_points = [h.strip() for h in str(contact_points or "").replace("\n", ",").split(",") if h.strip()]
        self.port = int(port or 9042)
        self.keyspaces = [k.strip() for k in (keyspaces or "").replace("\n", ",").split(",") if k.strip()]
        self.local_datacenter = (local_datacenter or "").strip() or None
        self.ssl = bool(ssl)
        self.verify_ssl = bool(verify_ssl)
        self.max_rows = max(1, int(max_rows or 5000))
        self.username = username or None
        self.password = password or None
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._cluster = None
        self._session = None

    # ---------------------------------------------------------------- session

    def _connect(self):
        if self._session is not None:
            return self._session
        from cassandra.auth import PlainTextAuthProvider
        from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile
        from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
        from cassandra.query import dict_factory

        policy = TokenAwarePolicy(DCAwareRoundRobinPolicy(local_dc=self.local_datacenter))
        profile = ExecutionProfile(
            load_balancing_policy=policy,
            request_timeout=self.request_timeout,
            row_factory=dict_factory,
        )
        kwargs: dict[str, Any] = dict(
            contact_points=self.contact_points,
            port=self.port,
            execution_profiles={EXEC_PROFILE_DEFAULT: profile},
            connect_timeout=self.connect_timeout,
        )
        if self.username:
            kwargs["auth_provider"] = PlainTextAuthProvider(username=self.username, password=self.password or "")
        if self.ssl:
            ctx = _ssl.create_default_context()
            if not self.verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
            kwargs["ssl_context"] = ctx
        self._cluster = Cluster(**kwargs)
        self._session = self._cluster.connect()
        return self._session

    def connect(self):
        return self._connect()

    def close(self):
        if self._cluster is not None:
            try:
                self._cluster.shutdown()
            finally:
                self._cluster = None
                self._session = None

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- catalog

    def _keyspace_names(self, session) -> list[str]:
        rows = session.execute("SELECT keyspace_name FROM system_schema.keyspaces")
        names = sorted(r["keyspace_name"] for r in rows if r["keyspace_name"] not in _SYSTEM_KEYSPACES)
        if self.keyspaces:
            wanted = set(self.keyspaces)
            names = [n for n in names if n in wanted]
        return names

    def get_tables(self) -> list[Table]:
        session = self._connect()
        tables: list[Table] = []
        for ks in discovery_items(self._keyspace_names(session), "keyspaces", label=str):
            try:
                tmeta = {
                    r["table_name"]: r
                    for r in session.execute(
                        "SELECT table_name, comment FROM system_schema.tables WHERE keyspace_name = %s", (ks,)
                    )
                }
                cols: dict[str, list[dict[str, Any]]] = {}
                for r in session.execute(
                    "SELECT table_name, column_name, type, kind, position, clustering_order "
                    "FROM system_schema.columns WHERE keyspace_name = %s",
                    (ks,),
                ):
                    cols.setdefault(r["table_name"], []).append(r)
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.warning("cassandra: could not read keyspace %s: %s", ks, e)
                self.mark_discovery_incomplete(ks)
                continue
            for tname in sorted(cols):
                tables.append(self._table(ks, tname, cols[tname], (tmeta.get(tname) or {}).get("comment")))
        return tables

    @staticmethod
    def _table(ks: str, tname: str, rows: list[dict[str, Any]], comment: str | None) -> Table:
        def order(r):
            kind_rank = {"partition_key": 0, "clustering": 1, "static": 2, "regular": 3}
            return (kind_rank.get(r["kind"], 4), r["position"] if r["position"] >= 0 else 0, r["column_name"])

        rows = sorted(rows, key=order)
        pk = [r["column_name"] for r in rows if r["kind"] == "partition_key"]
        ck = [(r["column_name"], (r.get("clustering_order") or "asc").upper()) for r in rows if r["kind"] == "clustering"]
        columns = []
        for r in rows:
            note = {"partition_key": "partition key", "clustering": "clustering column", "static": "static"}.get(r["kind"])
            columns.append(TableColumn(name=r["column_name"], dtype=r["type"], description=note))
        desc = f"Partition key: ({', '.join(pk)})"
        if ck:
            desc += "; clustering: " + ", ".join(f"{c} {o}" for c, o in ck)
        if comment:
            desc = f"{comment.strip()}. {desc}"
        return Table(
            name=f"{ks}.{tname}",
            description=desc + ".",
            columns=columns,
            pks=[TableColumn(name=c, dtype="") for c in pk] + [TableColumn(name=c, dtype="") for c, _ in ck],
            fks=[],
            metadata_json={"cassandra": {"keyspace": ks, "partition_key": pk, "clustering": [c for c, _ in ck]}},
        )

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Table | None:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        return None

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    # ----------------------------------------------------------------- query

    def execute_query(self, query: str, parameters: Any | None = None, limit: int | None = None) -> pd.DataFrame:
        """Run one CQL SELECT and return rows as a DataFrame (capped at max_rows)."""
        from cassandra.query import SimpleStatement

        query = ensure_single_select(query, "CQL")
        cap = max(1, min(int(limit or self.max_rows), self.max_rows))
        session = self._connect()
        stmt = SimpleStatement(query, fetch_size=min(1000, cap))
        result = session.execute(stmt, parameters)
        rows: list[dict[str, Any]] = []
        truncated = False
        for row in result:
            if len(rows) >= cap:
                truncated = True
                break
            rows.append({k: _cql_value(v) for k, v in row.items()})
        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=list(getattr(result, "column_names", None) or []))
        df.attrs["truncated"] = truncated
        return df

    # ------------------------------------------------------------ lifecycle

    def test_connection(self):
        try:
            session = self._connect()
            row = session.execute("SELECT release_version, cluster_name, data_center FROM system.local").one()
            n = len(self._keyspace_names(session))
            ver = (row or {}).get("release_version", "?")
            return {"success": True, "message": f"Connected to Cassandra {ver} ({(row or {}).get('data_center') or 'dc'}); {n} keyspace(s) visible"}
        except Exception as e:
            return {"success": False, "message": str(e)}
        finally:
            self.close()

    @property
    def description(self) -> str:
        where = ", ".join(self.contact_points) or "cluster"
        return f"""Apache Cassandra (CQL) at {where} — read-only.

Use client.execute_query(cql, parameters=None, limit=None) → pandas DataFrame (capped at {self.max_rows} rows;
df.attrs['truncated'] is True when the cap cut it). Table names are keyspace.table.
Rules — CQL is NOT SQL:
1. WHERE must restrict the FULL partition key (every partition-key column, with = or IN). Clustering columns may then be
   filtered in order (=, then a range on the next one). Each table's description lists its partition key and clustering columns.
2. Filtering on any other column needs ALLOW FILTERING, which scans the whole table — only on small tables, and always with LIMIT.
3. No JOIN, no subqueries, no OR. GROUP BY only on partition/clustering columns. Select the rows, then aggregate/join in pandas.
4. Use %s placeholders with parameters: execute_query("SELECT * FROM shop.orders WHERE customer_id = %s", parameters=["c-17"]).
5. Text literals use single quotes; uuid/timeuuid literals are unquoted.
Only SELECT is allowed.
"""


def _cql_value(v: Any) -> Any:
    """Driver types (UUID, Decimal, Date, SortedSet, OrderedMapSerializedKey, …)
    → plain Python a DataFrame and JSON can carry."""
    import uuid

    try:
        from cassandra.util import Date, Duration, OrderedMap, SortedSet, Time
    except Exception:  # pragma: no cover
        Date = Duration = OrderedMap = SortedSet = Time = ()  # type: ignore
    if isinstance(v, uuid.UUID):
        return str(v)
    if Date and isinstance(v, Date):
        return str(v)
    if Time and isinstance(v, Time):
        return str(v)
    if Duration and isinstance(v, Duration):
        return str(v)
    if OrderedMap and isinstance(v, OrderedMap):
        return {str(_cql_value(k)): _cql_value(x) for k, x in v.items()}
    if SortedSet and isinstance(v, SortedSet):
        return [_cql_value(x) for x in v]
    if isinstance(v, dict):
        return {str(_cql_value(k)): _cql_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_cql_value(x) for x in v]
    return to_plain(v)
