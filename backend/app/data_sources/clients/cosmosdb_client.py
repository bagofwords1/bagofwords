"""Azure Cosmos DB client (NoSQL API, read-only).

Each container of the configured database is a catalog table. Columns are
inferred from a bounded `SELECT TOP n` sample (system properties dropped);
the description names the partition key, which decides whether a query is a
cheap single-partition read or a fan-out across every partition (billed in
request units).

Queries are single Cosmos SQL `SELECT`s against one container. Cosmos SQL's
FROM names an alias, not a container, so the container comes from the
`container` argument — or, when omitted, from the FROM identifier when it
matches a container (`SELECT o.id FROM orders o`). Results are capped at
`max_items`. The data-plane query API cannot write; with a read-only key or a
reader role the account can't be written to at all.

Auth: account key (read-only key recommended), service principal, or the
deployment's managed identity.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients._document_schema import ensure_single_select, infer_columns, rows_to_frame
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress

logger = logging.getLogger(__name__)

_SYSTEM_PROPS = {"_rid", "_self", "_etag", "_attachments", "_lsn"}
_FROM_IDENT = re.compile(r"\bFROM\s+([A-Za-z_][\w-]*)", re.IGNORECASE)


class CosmosdbClient(DataSourceClient):

    # Cosmos DB query language: string escapes are \' \" \\ \n \r ... and ''
    # is not one (Cosmos DB query language reference, Constants).
    sql_literal_style = "backslash"

    def __init__(
        self,
        endpoint: str,
        database: str,
        containers: str | None = None,
        verify_ssl: bool = True,
        sample_size: int = 100,
        max_items: int = 5000,
        account_key: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        **_ignored,
    ):
        super().__init__()
        self.endpoint = (endpoint or "").strip()
        self.database = (database or "").strip()
        self.container_allowlist = [c.strip() for c in (containers or "").replace("\n", ",").split(",") if c.strip()]
        self.verify_ssl = bool(verify_ssl)
        self.sample_size = max(1, min(int(sample_size or 100), 1000))
        self.max_items = max(1, int(max_items or 5000))
        self.account_key = account_key
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._db = None

    # ---------------------------------------------------------------- client

    def _credential(self):
        if self.account_key:
            return self.account_key
        if self.tenant_id and self.client_id and self.client_secret:
            from azure.identity import ClientSecretCredential

            return ClientSecretCredential(self.tenant_id, self.client_id, self.client_secret)
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential(exclude_interactive_browser_credential=True)

    def _database(self):
        if self._db is not None:
            return self._db
        from azure.cosmos import CosmosClient

        kwargs: dict[str, Any] = {"connection_timeout": 30}
        if not self.verify_ssl:
            kwargs["connection_verify"] = False
            # The emulator advertises its own endpoints; don't hop off the
            # configured host to discover regions.
            kwargs["enable_endpoint_discovery"] = False
        client = CosmosClient(self.endpoint, credential=self._credential(), **kwargs)
        self._db = client.get_database_client(self.database)
        return self._db

    def connect(self):
        return self._database()

    # ---------------------------------------------------------------- catalog

    def _container_names(self) -> list[str]:
        names = sorted(c["id"] for c in self._database().list_containers())
        if self.container_allowlist:
            wanted = set(self.container_allowlist)
            names = [n for n in names if n in wanted]
        return names

    @staticmethod
    def _clean(doc: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in doc.items() if k not in _SYSTEM_PROPS}

    def _describe(self, name: str) -> Table:
        container = self._database().get_container_client(name)
        props = container.read()
        pk_paths = (props.get("partitionKey") or {}).get("paths") or []
        pk_fields = [p.lstrip("/").replace("/", ".") for p in pk_paths]
        docs = [
            self._clean(d) for d in container.query_items(
                f"SELECT TOP {int(self.sample_size)} * FROM c",
                enable_cross_partition_query=True,
            )
        ]
        cols = infer_columns(docs, key_order=["id", *pk_fields])
        names = {c.name for c in cols}
        for f in reversed(["id", *pk_fields]):
            if f not in names:
                cols.insert(0, TableColumn(name=f, dtype="string"))
        for c in cols:
            if c.name in pk_fields:
                c.description = "partition key"
            elif c.name == "id":
                c.description = "document id (unique within a partition)"
        desc = f"Partition key: {', '.join('/' + p.replace('.', '/') for p in pk_fields) or '(none)'}. Sampled {len(docs)} documents."
        return Table(
            name=name,
            description=desc,
            columns=cols,
            pks=[TableColumn(name="id", dtype="string")],
            fks=[],
            metadata_json={"cosmosdb": {"database": self.database, "partition_key": pk_paths, "sampled": len(docs)}},
        )

    def get_tables(self) -> list[Table]:
        tables: list[Table] = []
        for name in discovery_items(self._container_names(), "containers", label=str):
            try:
                tables.append(self._describe(name))
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.warning("cosmosdb: could not describe %s: %s", name, e)
                self.mark_discovery_incomplete(name)
        return tables

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Table:
        return self._describe(table_name)

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    # ----------------------------------------------------------------- query

    def _resolve_container(self, query: str, container: str | None) -> str:
        if container:
            return container
        m = _FROM_IDENT.search(query)
        if m:
            names = self._container_names()
            if m.group(1) in names:
                return m.group(1)
            lowered = {n.lower(): n for n in names}
            if m.group(1).lower() in lowered:
                return lowered[m.group(1).lower()]
        raise ValueError(
            "Name the container: execute_query(query, container='orders'), or write FROM <container> "
            "(e.g. SELECT o.id FROM orders o)."
        )

    def execute_query(
        self,
        query: str,
        container: str | None = None,
        parameters: Any | None = None,
        partition_key: Any | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Run one Cosmos SQL SELECT against a container → DataFrame.

        `parameters`: dict {"@status": "OPEN"} or list [{"name": "@status", "value": "OPEN"}].
        `partition_key`: scope the query to one partition (cheapest).
        """
        query = ensure_single_select(query, "Cosmos SQL")
        cap = max(1, min(int(limit or self.max_items), self.max_items))
        name = self._resolve_container(query, container)
        if isinstance(parameters, dict):
            parameters = [{"name": k if k.startswith("@") else f"@{k}", "value": v} for k, v in parameters.items()]
        kwargs: dict[str, Any] = {"parameters": parameters or None, "max_item_count": min(1000, cap)}
        if partition_key is not None:
            kwargs["partition_key"] = partition_key
        else:
            kwargs["enable_cross_partition_query"] = True
        items = self._database().get_container_client(name).query_items(query, **kwargs)
        rows: list[Any] = []
        truncated = False
        for item in items:
            if len(rows) >= cap:
                truncated = True
                break
            rows.append(self._clean(item) if isinstance(item, dict) else {"value": item})
        df = rows_to_frame(rows)
        df.attrs["truncated"] = truncated
        return df

    # ------------------------------------------------------------ lifecycle

    def test_connection(self):
        try:
            self._database().read()
            n = len(self._container_names())
            return {"success": True, "message": f"Connected to Cosmos DB database '{self.database}'; {n} container(s) visible"}
        except Exception as e:
            msg = str(e).split("\n")[0]
            return {"success": False, "message": msg}

    @property
    def description(self) -> str:
        return f"""Azure Cosmos DB (NoSQL API) database '{self.database}' — read-only.

Use client.execute_query(query, container=None, parameters=None, partition_key=None, limit=None) → pandas DataFrame
(capped at {self.max_items} items; df.attrs['truncated'] is True when the cap cut it).
Each catalog table is a container. A query runs against ONE container: write FROM <container> <alias>
(SELECT o.id, o.total FROM orders o WHERE o.status = 'OPEN'), or pass container='orders'.
Rules:
1. Filter on the partition key (named in each table's description) whenever possible — or pass partition_key=... —
   otherwise the query fans out to every partition (more request units, slower).
2. Project fields through the alias; nested: o.address.city; arrays: JOIN i IN o.items, or ARRAY_CONTAINS / EXISTS.
3. No joins ACROSS containers. Aggregates: SELECT VALUE COUNT(1) FROM orders o; GROUP BY works on fields.
4. TOP n (not LIMIT) for the first n rows; OFFSET x LIMIT y for paging.
5. Parameters: execute_query("SELECT * FROM orders o WHERE o.status = @s", parameters={{"@s": "OPEN"}}).
Only SELECT is allowed.
"""
