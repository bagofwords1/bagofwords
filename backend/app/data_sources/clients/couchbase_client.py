"""Couchbase client (read-only SQL++ / N1QL) — self-managed clusters and Capella.

Each collection of the bucket (all scopes, or one) is a catalog table named
`scope.collection`. Collections are schemaless, so columns are inferred from a
key-value sampling scan — that needs no query index, so the catalog works
even on collections nobody has indexed yet.

Queries are single SQL++ `SELECT`s sent with the server-side `readonly`
flag, so the query service itself refuses anything that would write, on top
of our own statement check. Results are capped at `max_rows` (an outer LIMIT
is not injected; the cap is applied while streaming rows).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients._document_schema import ensure_single_select, infer_columns, rows_to_frame
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress

logger = logging.getLogger(__name__)


class CouchbaseClient(DataSourceClient):

    # SQL++ (N1QL): a backslash starts an escape (\' \" \\ \n \r ...) and ''
    # is not documented as a quote (Couchbase SQL++ reference, Literals).
    sql_literal_style = "backslash"

    def __init__(
        self,
        connection_string: str,
        bucket: str,
        scope: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: bool = True,
        sample_size: int = 100,
        max_rows: int = 5000,
        timeout_seconds: int = 75,
        **_ignored,
    ):
        super().__init__()
        self.connection_string = (connection_string or "").strip()
        self.bucket = (bucket or "").strip()
        self.scope = (scope or "").strip() or None
        self.username = username
        self.password = password
        self.verify_ssl = bool(verify_ssl)
        self.sample_size = max(1, min(int(sample_size or 100), 1000))
        self.max_rows = max(1, int(max_rows or 5000))
        self.timeout = timedelta(seconds=int(timeout_seconds or 75))
        self._cluster = None

    # ---------------------------------------------------------------- cluster

    def _connect(self):
        if self._cluster is not None:
            return self._cluster
        from couchbase.auth import PasswordAuthenticator
        from couchbase.cluster import Cluster
        from couchbase.options import ClusterOptions, ClusterTimeoutOptions

        conn = self.connection_string
        if not self.verify_ssl and conn.startswith("couchbases://") and "ssl=no_verify" not in conn:
            conn += ("&" if "?" in conn else "?") + "ssl=no_verify"
        opts = ClusterOptions(
            PasswordAuthenticator(self.username or "", self.password or ""),
            timeout_options=ClusterTimeoutOptions(
                connect_timeout=timedelta(seconds=15),
                query_timeout=self.timeout,
                kv_timeout=timedelta(seconds=10),
                kv_scan_timeout=timedelta(seconds=30),
            ),
        )
        if conn.startswith("couchbases://") and ".cloud.couchbase.com" in conn:
            # Capella: WAN-friendly defaults recommended by the SDK.
            opts.apply_profile("wan_development")
        cluster = Cluster(conn, opts)
        cluster.wait_until_ready(timedelta(seconds=20))
        self._cluster = cluster
        return cluster

    def connect(self):
        return self._connect()

    def close(self):
        if self._cluster is not None:
            try:
                self._cluster.close()
            except Exception:
                pass
            self._cluster = None

    # ---------------------------------------------------------------- catalog

    def _collections(self) -> list[tuple]:
        bucket = self._connect().bucket(self.bucket)
        out = []
        for scope in bucket.collections().get_all_scopes():
            if self.scope and scope.name != self.scope:
                continue
            if not self.scope and scope.name == "_system":
                continue
            for coll in scope.collections:
                out.append((scope.name, coll.name))
        return sorted(out)

    def _sample(self, scope: str, collection: str) -> list[dict[str, Any]]:
        from couchbase.kv_range_scan import SamplingScan

        coll = self._connect().bucket(self.bucket).scope(scope).collection(collection)
        docs: list[dict[str, Any]] = []
        try:
            for item in coll.scan(SamplingScan(self.sample_size)):
                body = item.content_as[dict]
                if isinstance(body, dict):
                    docs.append(body)
        except IndexingCancelled:
            raise
        except Exception as e:
            # Older servers (< 7.6) have no KV range scan: fall back to a
            # query, which needs a primary/secondary index to exist.
            logger.info("couchbase: sampling scan unavailable on %s.%s (%s); using a query", scope, collection, e)
            ks = self._keyspace(scope, collection)
            res = self._connect().query(f"SELECT RAW d FROM {ks} AS d LIMIT {int(self.sample_size)}")
            docs = [r for r in res.rows() if isinstance(r, dict)]
        return docs

    def _keyspace(self, scope: str, collection: str) -> str:
        return f"`{self.bucket}`.`{scope}`.`{collection}`"

    def get_tables(self) -> list[Table]:
        tables: list[Table] = []
        for scope, coll in discovery_items(self._collections(), "collections", label=lambda sc: f"{sc[0]}.{sc[1]}"):
            try:
                docs = self._sample(scope, coll)
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.warning("couchbase: could not sample %s.%s: %s", scope, coll, e)
                self.mark_discovery_incomplete(f"{scope}.{coll}")
                docs = []
            if (scope, coll) == ("_default", "_default") and not docs:
                continue  # every bucket has it; empty, it is only noise
            columns = [TableColumn(name="meta().id", dtype="string", description="document key")]
            columns.extend(infer_columns(docs))
            tables.append(Table(
                name=f"{scope}.{coll}",
                description=f"Collection — query it as {self._keyspace(scope, coll)}. Sampled {len(docs)} documents.",
                columns=columns,
                pks=[TableColumn(name="meta().id", dtype="string")],
                fks=[],
                metadata_json={"couchbase": {"bucket": self.bucket, "scope": scope, "collection": coll,
                                             "keyspace": self._keyspace(scope, coll), "sampled": len(docs)}},
            ))
        return tables

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

    def execute_query(self, statement: str, parameters: Any | None = None, limit: int | None = None) -> pd.DataFrame:
        """Run one SQL++ SELECT (read-only on the server) → DataFrame.

        `parameters`: a dict for $named placeholders or a list for $1, $2 ….
        """
        from couchbase.options import QueryOptions

        statement = ensure_single_select(statement, "SQL++", extra_read_verbs=("WITH", "EXPLAIN", "INFER"))
        cap = max(1, min(int(limit or self.max_rows), self.max_rows))
        kwargs: dict[str, Any] = {"read_only": True, "timeout": self.timeout, "metrics": False}
        if isinstance(parameters, dict):
            kwargs["named_parameters"] = parameters
        elif parameters:
            kwargs["positional_parameters"] = list(parameters)
        result = self._connect().query(statement, QueryOptions(**kwargs))
        rows: list[Any] = []
        truncated = False
        for row in result.rows():
            if len(rows) >= cap:
                truncated = True
                break
            rows.append(row)
        if rows and not isinstance(rows[0], dict):
            rows = [{"value": r} for r in rows]  # SELECT RAW scalar
        df = rows_to_frame(rows)
        df.attrs["truncated"] = truncated
        return df

    # ------------------------------------------------------------ lifecycle

    def test_connection(self):
        try:
            colls = self._collections()
            list(self._connect().query("SELECT 1 AS ok").rows())
            return {"success": True, "message": f"Connected to Couchbase bucket '{self.bucket}'; {len(colls)} collection(s) visible"}
        except Exception as e:
            return {"success": False, "message": str(e)}
        finally:
            self.close()

    @property
    def description(self) -> str:
        return f"""Couchbase bucket `{self.bucket}` — query with SQL++ (N1QL), read-only.

Use client.execute_query(statement, parameters=None, limit=None) → pandas DataFrame (capped at {self.max_rows} rows;
df.attrs['truncated'] is True when the cap cut it). Each catalog table is scope.collection; query it by its full keyspace:
  SELECT o.status, COUNT(*) AS n FROM `{self.bucket}`.`inventory`.`orders` AS o GROUP BY o.status
Rules:
1. Always alias the keyspace and select fields through the alias (o.total) — SELECT * wraps each document in an object named after the alias.
2. Backtick identifiers with special characters; strings use single or double quotes.
3. The document key is META(o).id. Nested fields: o.address.city; arrays: ANY i IN o.items SATISFIES i.qty > 2 END, or UNNEST o.items AS i.
4. SQL++ supports JOIN (ON keys or ANSI), GROUP BY, aggregates, subqueries and window functions.
5. A query needs an index on the collection. An error like "No index available" means only key lookups (USE KEYS) work there.
6. Parameters: execute_query("SELECT o.* FROM `b`.`s`.`orders` o WHERE o.status = $status", parameters={{"status": "OPEN"}}).
Only SELECT is allowed (the server enforces read-only too).
"""
