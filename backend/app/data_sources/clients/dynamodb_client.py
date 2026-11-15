"""Amazon DynamoDB client (read-only PartiQL).

Each DynamoDB table is a catalog table. Its columns are the key attributes
(declared, typed) plus the attributes found in a bounded sample of items; its
description carries the key schema and the secondary indexes, because the
single biggest cost/latency lever in DynamoDB is querying by key.

Queries are PartiQL `SELECT` statements run through ExecuteStatement, paged up
to `max_items`. A SELECT whose WHERE does not pin the partition key becomes a
full table Scan (billed per item read), so results are capped and the
DataFrame is flagged `truncated` when the cap stops it. Writes are refused
before anything is sent.

Auth mirrors the S3 connector: static keys, keys + STS assume-role, or boto3's
default credential chain. `endpoint_url` targets DynamoDB Local / VPC endpoints.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients._document_schema import (
    ensure_single_select,
    infer_columns,
    rows_to_frame,
)
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress

logger = logging.getLogger(__name__)

_DDB_TYPES = {"S": "string", "N": "number", "B": "binary"}


class DynamodbClient(DataSourceClient):

    # PartiQL: '' is a quote inside a string (PartiQL specification,
    # Literals); doubling the backslash too keeps one literal whether or not a
    # backslash escapes.
    sql_literal_style = "doubled"

    def __init__(
        self,
        region: str | None = None,
        endpoint_url: str | None = None,
        tables: str | None = None,
        sample_size: int = 100,
        max_items: int = 5000,
        access_key: str | None = None,
        secret_key: str | None = None,
        session_token: str | None = None,
        role_arn: str | None = None,
        **_ignored,
    ):
        super().__init__()
        self.region = (region or "").strip() or None
        self.endpoint_url = (endpoint_url or "").strip() or None
        self.table_allowlist = [t.strip() for t in (tables or "").replace("\n", ",").split(",") if t.strip()]
        self.sample_size = max(1, min(int(sample_size or 100), 1000))
        self.max_items = max(1, int(max_items or 5000))
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.role_arn = role_arn
        self._ddb = None

    # ---------------------------------------------------------------- client

    def _session(self):
        import boto3

        if self.role_arn:
            sts_kwargs: dict[str, Any] = {"region_name": self.region}
            if self.access_key and self.secret_key:
                sts_kwargs["aws_access_key_id"] = self.access_key
                sts_kwargs["aws_secret_access_key"] = self.secret_key
            c = boto3.client("sts", **sts_kwargs).assume_role(
                RoleArn=self.role_arn, RoleSessionName="bow-dynamodb"
            )["Credentials"]
            return boto3.Session(
                aws_access_key_id=c["AccessKeyId"],
                aws_secret_access_key=c["SecretAccessKey"],
                aws_session_token=c["SessionToken"],
                region_name=self.region,
            )
        if self.access_key and self.secret_key:
            return boto3.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                aws_session_token=self.session_token,
                region_name=self.region,
            )
        return boto3.Session(region_name=self.region)

    def _client(self):
        if self._ddb is None:
            from botocore.config import Config

            self._ddb = self._session().client(
                "dynamodb",
                endpoint_url=self.endpoint_url,
                config=Config(retries={"max_attempts": 5, "mode": "adaptive"}, read_timeout=60),
            )
        return self._ddb

    def connect(self):
        return self._client()

    # ---------------------------------------------------------------- values

    @staticmethod
    def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeDeserializer

        d = TypeDeserializer()
        return {k: d.deserialize(v) for k, v in item.items()}

    # ---------------------------------------------------------------- catalog

    def _table_names(self) -> list[str]:
        names: list[str] = []
        for page in self._client().get_paginator("list_tables").paginate():
            names.extend(page.get("TableNames", []))
        if self.table_allowlist:
            allowed = set(self.table_allowlist)
            names = [n for n in names if n in allowed]
        return sorted(names)

    @staticmethod
    def _key_names(key_schema: list[dict[str, str]]) -> list[str]:
        order = {"HASH": 0, "RANGE": 1}
        return [k["AttributeName"] for k in sorted(key_schema, key=lambda k: order.get(k["KeyType"], 2))]

    def _describe(self, name: str) -> Table:
        ddb = self._client()
        desc = ddb.describe_table(TableName=name)["Table"]
        attr_types = {a["AttributeName"]: _DDB_TYPES.get(a["AttributeType"], a["AttributeType"])
                      for a in desc.get("AttributeDefinitions", [])}
        keys = self._key_names(desc.get("KeySchema", []))
        sample = ddb.scan(TableName=name, Limit=self.sample_size).get("Items", [])
        items = [self._deserialize(i) for i in sample]

        inferred = {c.name: c for c in infer_columns(items, key_order=keys)}
        columns: list[TableColumn] = []
        for k in keys:
            role = "partition key" if k == keys[0] else "sort key"
            columns.append(TableColumn(name=k, dtype=attr_types.get(k, "string"), description=role))
            inferred.pop(k, None)
        columns.extend(inferred.values())

        indexes = []
        for kind, label in (("GlobalSecondaryIndexes", "GSI"), ("LocalSecondaryIndexes", "LSI")):
            for ix in desc.get(kind, []) or []:
                ik = self._key_names(ix.get("KeySchema", []))
                indexes.append({"name": ix["IndexName"], "type": label, "keys": ik})

        parts = [f"Partition key: {keys[0]}" if keys else "No key schema"]
        if len(keys) > 1:
            parts.append(f"sort key: {keys[1]}")
        if indexes:
            table_name = desc["TableName"]
            parts.append("indexes: " + "; ".join(
                f'"{table_name}"."{ix["name"]}" ({ix["type"]} on {", ".join(ix["keys"])})' for ix in indexes
            ))
        # ItemCount is refreshed by DynamoDB roughly every six hours, so a 0 on
        # a young table means "unknown", not "empty".
        if desc.get("ItemCount"):
            parts.append(f"~{desc['ItemCount']} items")
        return Table(
            name=desc["TableName"],
            description=". ".join(parts) + ".",
            columns=columns,
            pks=[TableColumn(name=k, dtype=attr_types.get(k, "string")) for k in keys],
            fks=[],
            metadata_json={"dynamodb": {
                "keys": keys,
                "indexes": indexes,
                "item_count": desc.get("ItemCount"),
                "size_bytes": desc.get("TableSizeBytes"),
                "billing_mode": (desc.get("BillingModeSummary") or {}).get("BillingMode"),
                "sampled_items": len(items),
            }},
        )

    def get_tables(self) -> list[Table]:
        tables: list[Table] = []
        for name in discovery_items(self._table_names(), "tables", label=str):
            try:
                tables.append(self._describe(name))
            except IndexingCancelled:
                raise
            except Exception as e:
                logger.warning("dynamodb: could not describe %s: %s", name, e)
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

    def execute_query(self, statement: str, parameters: list[Any] | None = None, limit: int | None = None) -> pd.DataFrame:
        """Run one PartiQL SELECT and return the items as a DataFrame.

        `parameters` binds `?` placeholders (plain Python values). Pages are
        followed until `limit` (default: the connection's max_items) items.
        """
        statement = ensure_single_select(statement, "PartiQL")
        cap = max(1, min(int(limit or self.max_items), self.max_items))
        kwargs: dict[str, Any] = {"Statement": statement}
        if parameters:
            from boto3.dynamodb.types import TypeSerializer

            ser = TypeSerializer()
            kwargs["Parameters"] = [ser.serialize(_to_ddb(p)) for p in parameters]

        rows: list[dict[str, Any]] = []
        truncated = False
        ddb = self._client()
        while True:
            kwargs["Limit"] = min(1000, cap - len(rows))
            resp = ddb.execute_statement(**kwargs)
            rows.extend(self._deserialize(i) for i in resp.get("Items", []))
            token = resp.get("NextToken")
            if len(rows) >= cap:
                truncated = bool(token) or len(rows) > cap
                rows = rows[:cap]
                break
            if not token:
                break
            kwargs["NextToken"] = token

        keys = self._keys_for_statement(statement)
        df = rows_to_frame(rows, key_order=keys)
        df.attrs["truncated"] = truncated
        if truncated:
            logger.info("dynamodb: result capped at %d items", cap)
        return df

    def _keys_for_statement(self, statement: str) -> list[str]:
        import re

        m = re.search(r'\bFROM\s+"([^"]+)"', statement, re.IGNORECASE)
        if not m:
            return []
        try:
            desc = self._client().describe_table(TableName=m.group(1))["Table"]
            return self._key_names(desc.get("KeySchema", []))
        except Exception:
            return []

    # ------------------------------------------------------------ lifecycle

    def test_connection(self):
        try:
            ddb = self._client()
            if self.table_allowlist:
                ddb.describe_table(TableName=self.table_allowlist[0])
                n = len(self.table_allowlist)
            else:
                n = len(ddb.list_tables(Limit=100).get("TableNames", []))
            where = self.endpoint_url or self.region or "default region"
            return {"success": True, "message": f"Connected to DynamoDB ({where}); {n} table(s) visible"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        where = self.endpoint_url or self.region or "the default region"
        return f"""Amazon DynamoDB ({where}) — query with PartiQL, read-only.

Use client.execute_query(statement, parameters=None, limit=None) → pandas DataFrame.
Rules:
1. Table names are double-quoted, strings single-quoted: SELECT * FROM "Orders" WHERE "customer_id" = 'c-17'
2. ALWAYS filter on the partition key (and optionally the sort key) when you can — that is a cheap Query.
   A WHERE that does not pin the partition key is a full-table Scan: slow, billed per item, capped at {self.max_items} items
   (df.attrs['truncated'] is True when the cap cut it).
3. Query a secondary index with "Table"."IndexName": SELECT * FROM "Orders"."by_status" WHERE "status" = 'OPEN'
4. Sort-key ranges: WHERE "pk" = 'x' AND "sk" BETWEEN 'a' AND 'm', or begins_with("sk", '2025-').
5. PartiQL on DynamoDB has NO JOIN, GROUP BY, COUNT/SUM/AVG, or DISTINCT — select the items, then aggregate/join in pandas.
6. Nested attributes: "address"."city"; list element: "tags"[0]. Missing attributes come back as NaN.
7. Use parameters for values: execute_query('SELECT * FROM "Orders" WHERE "pk" = ?', parameters=['c-17']).
Only SELECT is allowed.
"""


def _to_ddb(value: Any) -> Any:
    """Plain Python → values the DynamoDB serializer accepts (floats → Decimal)."""
    import decimal

    if isinstance(value, float):
        return decimal.Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value
