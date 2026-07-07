from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter

import json
import pandas as pd
import requests
from typing import Any, Dict, List, Optional


class OpenSearchClient(DataSourceClient):
    """OpenSearch client (REST over HTTP, no engine SDK).

    Indices map to catalog tables; the index *mapping* is the schema, so
    discovery is a single ``GET /_mapping`` call — no document sampling.
    Queries are the native query DSL wrapped in a JSON envelope (same
    contract style as the MongoDB client), with an optional ``sql`` escape
    hatch that posts to the bundled SQL plugin (``/_plugins/_sql``).

    Auth is HTTP basic (the OpenSearch security plugin's default) or none
    (security disabled / network-gated dev clusters).
    """

    # Keys the query envelope may pass through to POST /{index}/_search.
    SEARCH_KEYS = {"query", "aggs", "aggregations", "size", "sort", "_source",
                   "from", "search_after", "timeout"}

    # The engine's default max_result_window: size + from must stay under it.
    MAX_RESULT_WINDOW = 10_000

    # Bucket-level keys that are not sub-aggregations.
    _BUCKET_META_KEYS = {"key", "key_as_string", "doc_count", "from", "to",
                         "from_as_string", "to_as_string",
                         "doc_count_error_upper_bound"}

    # (connect, read) timeouts: 5s to connect (Mongo parity), and a read
    # timeout just above the 60s query timeout sent to the engine.
    TIMEOUTS = (5, 65)

    def __init__(
        self,
        host: str,
        port: int = 9200,
        user: Optional[str] = None,
        password: Optional[str] = None,
        secure: bool = False,
        verify_certs: bool = True,
        index_pattern: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.secure = secure
        self.verify_certs = verify_certs
        self.index_pattern = index_pattern

        # A full URL in `host` wins over port/secure (managed endpoints have
        # no separate host/port).
        h = (host or "").strip().rstrip("/")
        if h.startswith("http://") or h.startswith("https://"):
            self.base_url = h
        else:
            scheme = "https" if secure else "http"
            self.base_url = f"{scheme}://{h}:{port}"

        # Optional comma-separated index globs to narrow discovery.
        self._patterns: List[str] = []
        if isinstance(index_pattern, str) and index_pattern.strip():
            seen = set()
            for part in index_pattern.split(","):
                p = part.strip()
                if p and p not in seen:
                    seen.add(p)
                    self._patterns.append(p)

    # ---------- transport ---------- #

    def _request(self, method: str, path: str, json_body: Any = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        auth = (self.user, self.password or "") if self.user else None
        resp = requests.request(
            method,
            f"{self.base_url}{path}",
            json=json_body,
            params=params,
            auth=auth,
            verify=self.verify_certs,
            timeout=self.TIMEOUTS,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenSearch request {method} {path} failed "
                f"[{resp.status_code}]: {resp.text.strip()[:2000]}"
            )
        return resp.json()

    # ---------- schema discovery ---------- #

    @staticmethod
    def _dtype_for(mapping_type: str) -> str:
        if mapping_type in ("keyword", "text", "ip", "wildcard", "constant_keyword",
                            "match_only_text", "search_as_you_type"):
            return "string"
        if mapping_type in ("long", "integer", "short", "byte", "unsigned_long"):
            return "integer"
        if mapping_type in ("double", "float", "half_float", "scaled_float"):
            return "number"
        if mapping_type == "boolean":
            return "boolean"
        if mapping_type in ("date", "date_nanos"):
            return "datetime"
        if mapping_type == "nested":
            return "array"
        return "object"

    def _flatten_properties(self, props: Dict[str, Any], prefix: str = "",
                            raw_types: Optional[Dict[str, str]] = None) -> List[TableColumn]:
        """Flatten a mapping's `properties` tree into dot-path columns.

        Conventions match the MongoDB client: `object` fields recurse with a
        dot path (`customer.tier`); `nested` fields (arrays of objects) get an
        `array` column plus their children under the `[]` marker. Multi-fields
        (e.g. a `text` field's `.keyword` subfield) surface as columns too, so
        the coder can see the aggregatable variant.
        """
        columns: List[TableColumn] = []
        for name, defn in (props or {}).items():
            full = f"{prefix}.{name}" if prefix else name
            if not isinstance(defn, dict):
                continue
            child_props = defn.get("properties")
            mtype = defn.get("type")
            if child_props and (mtype is None or mtype == "object"):
                columns.extend(self._flatten_properties(child_props, full, raw_types))
                continue
            if mtype == "nested":
                columns.append(TableColumn(name=full, dtype="array"))
                if raw_types is not None:
                    raw_types[full] = "nested"
                if child_props:
                    columns.extend(self._flatten_properties(child_props, f"{full}[]", raw_types))
                continue
            dtype = self._dtype_for(mtype or "object")
            columns.append(TableColumn(name=full, dtype=dtype))
            if raw_types is not None and mtype:
                raw_types[full] = mtype
            for sub_name, sub_defn in (defn.get("fields") or {}).items():
                sub_full = f"{full}.{sub_name}"
                sub_type = (sub_defn or {}).get("type", "keyword")
                columns.append(TableColumn(name=sub_full, dtype=self._dtype_for(sub_type)))
                if raw_types is not None:
                    raw_types[sub_full] = sub_type
        return columns

    def _table_from_mapping(self, name: str, mappings: Dict[str, Any],
                            kind: str) -> Table:
        raw_types: Dict[str, str] = {}
        columns = self._flatten_properties(mappings.get("properties") or {}, "", raw_types)
        return Table(
            name=name,
            columns=columns,
            pks=[TableColumn(name="_id", dtype="string")],
            fks=[],
            metadata_json={"type": kind, "raw_types": raw_types},
        )

    @staticmethod
    def _union_table(name: str, members: List[Table], kind: str,
                     member_names: List[str]) -> Table:
        """A table whose columns are the union of several member tables'
        columns (aliases and data streams span multiple backing indices)."""
        seen: Dict[str, TableColumn] = {}
        raw_types: Dict[str, str] = {}
        for member in members:
            for col in member.columns:
                seen.setdefault(col.name, col)
            raw_types.update((member.metadata_json or {}).get("raw_types") or {})
        return Table(
            name=name,
            columns=list(seen.values()),
            pks=[TableColumn(name="_id", dtype="string")],
            fks=[],
            metadata_json={"type": kind, "indices": member_names, "raw_types": raw_types},
        )

    def _discover_data_streams(self) -> List[Dict[str, Any]]:
        """Data streams visible to the connection.

        Streams write to hidden `.ds-*` backing indices, so they never surface
        through the plain index scan — they need their own discovery call.
        Patterns are queried one by one because `GET /_data_stream/<name>`
        404s for a pattern that only matches plain indices; one bad pattern
        must not hide the streams matched by the others. Failures (pre-stream
        clusters, missing permission) degrade to "no streams", never an error.
        """
        patterns = self._patterns or [None]
        streams: Dict[str, Dict[str, Any]] = {}
        for pattern in patterns:
            path = f"/_data_stream/{pattern}" if pattern else "/_data_stream"
            try:
                for s in (self._request("GET", path) or {}).get("data_streams") or []:
                    if s.get("name"):
                        streams.setdefault(s["name"], s)
            except Exception:
                continue
        return list(streams.values())

    def get_tables(self) -> List[Table]:
        """Discover indices, aliases, and data streams with their mapped fields.

        One bulk `GET /_mapping` call; system/hidden indices (`.`-prefixed,
        incl. data-stream backing indices) are excluded unless an explicit
        `index_pattern` targets them. Aliases and data streams surface as
        tables of their own — columns are the union of their backing
        indices' fields.
        """
        try:
            path = f"/{','.join(self._patterns)}/_mapping" if self._patterns else "/_mapping"
            mappings_by_index = self._request("GET", path)
            aliases_by_index = self._request("GET", "/_alias")
        except Exception as e:
            print(f"Error retrieving OpenSearch mappings: {e}")
            return []

        streams = self._discover_data_streams()
        stream_backing = {
            (i or {}).get("index_name")
            for s in streams for i in (s.get("indices") or [])
        }

        tables: List[Table] = []
        alias_members: Dict[str, List[str]] = {}
        for index_name, body in sorted(mappings_by_index.items()):
            # Backing indices are queried through their stream, never directly.
            if index_name in stream_backing:
                continue
            if index_name.startswith(".") and not self._patterns:
                continue
            tables.append(self._table_from_mapping(
                index_name, (body or {}).get("mappings") or {}, "index"))
            for alias in ((aliases_by_index.get(index_name) or {}).get("aliases") or {}):
                alias_members.setdefault(alias, []).append(index_name)

        by_name = {t.name: t for t in tables}
        for alias, members in sorted(alias_members.items()):
            if alias.startswith("."):
                continue
            tables.append(self._union_table(
                alias, [by_name[m] for m in members if m in by_name], "alias", members))

        for s in sorted(streams, key=lambda s: s["name"]):
            name = s["name"]
            backing = [(i or {}).get("index_name") for i in (s.get("indices") or [])]
            try:
                # Resolves to the stream's backing indices' mappings.
                stream_mappings = self._request("GET", f"/{name}/_mapping")
            except Exception:
                continue
            members = [
                self._table_from_mapping(idx, (body or {}).get("mappings") or {}, "index")
                for idx, body in sorted(stream_mappings.items())
            ]
            tables.append(self._union_table(name, members, "data_stream", backing))
        return tables

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_schema(self, index_name: str) -> Table:
        """Schema for a single index (or alias/pattern resolving to one)."""
        body = self._request("GET", f"/{index_name}/_mapping")
        # The response is keyed by concrete index name, which for an alias
        # differs from what was asked for; take the first entry.
        first = next(iter(body.values()), {})
        return self._table_from_mapping(index_name, (first or {}).get("mappings") or {}, "index")

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    # ---------- query execution ---------- #

    def execute_query(self, query: str) -> pd.DataFrame:
        """Execute a JSON query envelope and return a DataFrame.

        Envelope (JSON string):
        {
            "index": "orders",                  # required
            "query": {...},                     # query DSL
            "aggs": {...},                      # aggregations
            "size": 100, "sort": [...], "_source": [...],
            "sql": "SELECT ..."                 # alternative: SQL plugin
        }
        """
        try:
            envelope = json.loads(query)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON query: {e}")
        if not isinstance(envelope, dict):
            raise ValueError("Query must be a JSON object")

        if "sql" in envelope:
            return self._execute_sql(envelope["sql"])

        index = envelope.get("index")
        if not index or not isinstance(index, str):
            raise ValueError("Query must specify 'index' (or use the 'sql' key)")

        body = {k: v for k, v in envelope.items() if k in self.SEARCH_KEYS}
        has_aggs = "aggs" in body or "aggregations" in body
        if "size" not in body:
            body["size"] = 0 if has_aggs else 100
        window = int(body.get("size") or 0) + int(body.get("from") or 0)
        if window > self.MAX_RESULT_WINDOW:
            raise ValueError(
                f"size + from must be <= {self.MAX_RESULT_WINDOW}; "
                "narrow the query or aggregate instead"
            )
        body.setdefault("timeout", "60s")

        result = self._request("POST", f"/{index}/_search", json_body=body)

        if has_aggs:
            rows = self._flatten_aggregations(result.get("aggregations") or {})
            return pd.DataFrame(rows)

        hits = (result.get("hits") or {}).get("hits") or []
        if not hits:
            return pd.DataFrame()
        df = pd.json_normalize([h.get("_source") or {} for h in hits], sep=".")
        df.insert(0, "_id", [h.get("_id") for h in hits])
        return df

    def _execute_sql(self, sql: str) -> pd.DataFrame:
        """Run a statement via the bundled SQL plugin (`POST /_plugins/_sql`)."""
        result = self._request("POST", "/_plugins/_sql", json_body={"query": sql})
        schema = result.get("schema") or []
        cols = [c.get("alias") or c.get("name") for c in schema]
        return pd.DataFrame(result.get("datarows") or [], columns=cols or None)

    # ---------- aggregation flattening ---------- #

    @classmethod
    def _metric_columns(cls, name: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Columns for a metric-agg result (`value`, stats dicts, percentiles)."""
        if "value" in body and not isinstance(body["value"], (dict, list)):
            return {name: body.get("value_as_string", body["value"])}
        if "values" in body and isinstance(body["values"], dict):
            return {f"{name}.{k}": v for k, v in body["values"].items()}
        scalars = {k: v for k, v in body.items()
                   if not isinstance(v, (dict, list)) and not k.endswith("_as_string")}
        if scalars:
            return {f"{name}.{k}": v for k, v in scalars.items()}
        # Unrecognized shape — keep the raw JSON rather than dropping data.
        return {name: json.dumps(body)}

    @classmethod
    def _flatten_aggregations(cls, aggs: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten an aggregations result tree into rows.

        Each bucket level contributes a key column named after the agg;
        metric leaves and `doc_count` become value columns. Sibling bucket
        aggs each produce their own row group (concatenated).
        """
        bucket_rows: List[Dict[str, Any]] = []
        metrics: Dict[str, Any] = {}
        for name, body in aggs.items():
            if not isinstance(body, dict):
                metrics[name] = body
                continue
            if "buckets" in body:
                buckets = body["buckets"]
                # The `filters` agg returns a dict keyed by bucket name.
                if isinstance(buckets, dict):
                    buckets = [{**b, "key": k} for k, b in buckets.items()]
                for bucket in buckets or []:
                    key = bucket.get("key_as_string", bucket.get("key"))
                    sub = {k: v for k, v in bucket.items()
                           if k not in cls._BUCKET_META_KEYS and isinstance(v, dict)}
                    base = {name: key, "doc_count": bucket.get("doc_count")}
                    if sub:
                        for sub_row in cls._flatten_aggregations(sub):
                            row = dict(base)
                            # Inner levels own doc_count when present.
                            if "doc_count" in sub_row:
                                row.pop("doc_count", None)
                            row.update(sub_row)
                            bucket_rows.append(row)
                    else:
                        for k, v in bucket.items():
                            if k in cls._BUCKET_META_KEYS or not isinstance(v, dict):
                                continue
                            base.update(cls._metric_columns(k, v))
                        bucket_rows.append(base)
                continue
            metrics.update(cls._metric_columns(name, body))

        if bucket_rows and metrics:
            return [{**metrics, **row} for row in bucket_rows]
        if bucket_rows:
            return bucket_rows
        return [metrics] if metrics else []

    # ---------- connection / description ---------- #

    def test_connection(self):
        try:
            info = self._request("GET", "/")
            version = (info.get("version") or {})
            distribution = version.get("distribution", "opensearch")
            number = version.get("number", "?")
            return {"success": True,
                    "message": f"Connected to {distribution} {number}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        """Return description for LLM code generation."""
        return f"""
OpenSearch cluster at {self.base_url}

CRITICAL RULES:
1. Only use fields that EXIST in the schema - never assume fields exist
2. Use valid JSON: true/false/null (NOT Python True/False/None)
3. Aggregate, sort and filter exactly on KEYWORD fields, never on "text" fields.
   When the schema shows both "title" (string) and "title.keyword", use
   "title.keyword" for terms aggs / sorting and "title" for full-text "match"
4. Fields marked "array" with children under "name[]" are NESTED - queries on
   them must be wrapped: {{"nested": {{"path": "items", "query": {{...}}}}}}
5. When only aggregations matter, "size" defaults to 0 automatically
6. If the tool inspect_data is available and you are unsure about the data,
   use it to sample documents before creating data/widgets

Use execute_query() with a JSON envelope string.

**Example - filter + fetch documents:**
```python
df = client.execute_query('''{{
    "index": "orders",
    "query": {{"bool": {{"filter": [
        {{"term": {{"status": "active"}}}},
        {{"range": {{"created_at": {{"gte": "2026-01-01"}}}}}}
    ]}}}},
    "sort": [{{"created_at": "desc"}}],
    "size": 100,
    "_source": ["order_id", "total", "customer.name"]
}}''')
```

**Example - aggregation (group + metric):**
```python
df = client.execute_query('''{{
    "index": "orders",
    "aggs": {{"by_tier": {{
        "terms": {{"field": "customer.tier", "size": 20}},
        "aggs": {{"revenue": {{"sum": {{"field": "total"}}}}}}
    }}}}
}}''')
# -> columns: by_tier, doc_count, revenue
```

**Example - SQL escape hatch (simple flat queries only):**
```python
df = client.execute_query('''{{
    "sql": "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
}}''')
```

WRONG - Do NOT do this:
```
{{"terms": {{"field": "title"}}}}       // text field - fails; use title.keyword
{{"term": {{"items.sku": "X"}}}}        // nested path without nested wrapper - matches nothing
```
"""


# Alias for the dynamic type->class naming convention ("opensearch" ->
# "OpensearchClient") used by the registry fallback and the integration-test
# harness. The registry's explicit client_path points at OpenSearchClient.
OpensearchClient = OpenSearchClient
