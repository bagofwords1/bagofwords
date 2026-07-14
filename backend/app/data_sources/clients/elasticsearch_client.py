"""Elasticsearch data source client (REST over HTTP, no engine SDK).

Forked from `opensearch_client.py` — OpenSearch is a fork of Elasticsearch
7.10, so the shape is identical: indices/data streams map to catalog tables,
the index *mapping* is the schema (so discovery is a single bulk
``GET /_mapping`` call — no document sampling, no cost), and queries are the
native query DSL wrapped in a JSON envelope.

Differences from the OpenSearch client, all localized:
  - **Auth** — Elasticsearch 8.x is secured by default. Three variants:
    an **API key** (`Authorization: ApiKey <base64(id:key)>`, the recommended
    path), HTTP **basic** (`elastic` superuser / a role user), or **none**
    (security disabled / network-gated dev clusters).
  - **SQL escape hatch** — `POST /_sql?format=json` (Elastic's endpoint),
    whose response is `{columns:[{name,type}], rows:[[...]]}` — not
    OpenSearch's `/_plugins/_sql` with `{schema, datarows}`.
  - **ES|QL** — the piped query language (ES 8.11+): `POST /_query` with
    `{query: "FROM logs-* | STATS count() BY level"}`, response
    `{columns:[...], values:[[...]]}`.
  - **Pattern collapsing** — daily/rolling time-series indices
    (``logs-app-2026.07.10``, ``…07.09``, …) are collapsed into a single
    ``logs-app-*`` table (the union of their fields) so the catalog stays a
    handful of *patterns* instead of exploding to one table per day. This is
    what lets the agent search ``logs-app-*`` the way an analyst does. Data
    streams and aliases collapse the same way (inherited from OpenSearch).
"""
import base64
import json
import re
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter


# Trailing date/rollover suffix on a time-series index, e.g.
#   logs-app-2026.07.10   metrics-2026-07-10   filebeat-000042
# The leading group is the stable pattern base; the suffix rolls over.
_DATE_SUFFIX = re.compile(
    r"^(?P<base>.+?)[-.](?:"
    r"\d{4}[.\-/]\d{2}[.\-/]\d{2}"        # 2026.07.10 / 2026-07-10
    r"|\d{4}[.\-/]\d{2}"                   # 2026.07 (monthly)
    r"|\d{8}"                             # 20260710
    r"|\d{6,}"                            # 000042 rollover counter
    r")$"
)


class ElasticsearchClient(DataSourceClient):

    # Keys the query envelope may pass through to POST /{index}/_search.
    SEARCH_KEYS = {"query", "aggs", "aggregations", "size", "sort", "_source",
                   "from", "search_after", "timeout", "runtime_mappings"}

    # The engine's default max_result_window: size + from must stay under it.
    MAX_RESULT_WINDOW = 10_000

    # Bucket-level keys that are not sub-aggregations.
    _BUCKET_META_KEYS = {"key", "key_as_string", "doc_count", "from", "to",
                         "from_as_string", "to_as_string",
                         "doc_count_error_upper_bound"}

    # (connect, read) timeouts: 5s to connect, read just above the 60s query
    # timeout sent to the engine.
    TIMEOUTS = (5, 65)

    # Analyzed full-text types: never valid in terms aggs / sort. Serverless
    # (logsdb) clusters map message fields as `match_only_text` with NO
    # `.keyword` subfield, so the schema must say so or the coder will write
    # aggregations that 400 with "match_only_text fields do not support
    # sorting and aggregations".
    _ANALYZED_TEXT_TYPES = {"text", "match_only_text", "search_as_you_type"}

    def __init__(
        self,
        host: str,
        port: int = 9200,
        api_key: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        secure: bool = True,
        verify_certs: bool = True,
        index_pattern: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.api_key = api_key or None
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

    def _auth(self):
        """Return (auth_tuple, extra_headers). API key takes precedence over
        basic; both may be absent (security disabled)."""
        if self.api_key:
            # Accept either a raw `id:key` pair or an already-base64'd token.
            token = self.api_key
            if ":" in token and "=" not in token:
                token = base64.b64encode(token.encode()).decode()
            return None, {"Authorization": f"ApiKey {token}"}
        if self.user:
            return (self.user, self.password or ""), {}
        return None, {}

    def _request(self, method: str, path: str, json_body: Any = None,
                 params: Optional[Dict[str, Any]] = None) -> Any:
        auth, extra_headers = self._auth()
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers)
        resp = requests.request(
            method,
            f"{self.base_url}{path}",
            json=json_body,
            params=params,
            auth=auth,
            verify=self.verify_certs,
            timeout=self.TIMEOUTS,
            headers=headers,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Elasticsearch request {method} {path} failed "
                f"[{resp.status_code}]: {resp.text.strip()[:2000]}"
            )
        return resp.json()

    # ---------- schema discovery ---------- #

    @staticmethod
    def _dtype_for(mapping_type: str) -> str:
        if mapping_type in ("keyword", "text", "ip", "wildcard", "constant_keyword",
                            "match_only_text", "search_as_you_type", "version"):
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

        `object` fields recurse with a dot path (`customer.tier`); `nested`
        fields (arrays of objects) get an `array` column plus their children
        under the `[]` marker. Multi-fields (e.g. a `text` field's `.keyword`
        subfield) surface as columns too, so the coder can see the
        aggregatable variant.
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
            if mtype in self._ANALYZED_TEXT_TYPES:
                kw = next(
                    (f"{full}.{sub}" for sub, sub_defn in (defn.get("fields") or {}).items()
                     if (sub_defn or {}).get("type") == "keyword"),
                    None,
                )
                dtype = (f"string (full-text; aggregate/sort on {kw})" if kw
                         else "string (full-text; NOT aggregatable/sortable)")
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
        columns (patterns, aliases and data streams span multiple backing
        indices)."""
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

    @staticmethod
    def _pattern_base(index_name: str) -> Optional[str]:
        """If `index_name` is a date/rollover-suffixed member of a time-series
        pattern, return the pattern base (`logs-app` for `logs-app-2026.07.10`);
        else None."""
        m = _DATE_SUFFIX.match(index_name)
        return m.group("base") if m else None

    def _discover_data_streams(self) -> List[Dict[str, Any]]:
        """Data streams visible to the connection.

        Streams write to hidden `.ds-*` backing indices, so they never surface
        through the plain index scan — they need their own discovery call.
        Failures (pre-stream clusters, missing permission) degrade to "no
        streams", never an error.
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
        """Discover indices, patterns, aliases, and data streams with their
        mapped fields.

        One bulk `GET /_mapping` call. Time-series indices sharing a
        date/rollover suffix collapse into a single `<base>-*` pattern table
        (union of fields). System/hidden indices (`.`-prefixed, incl.
        data-stream backing indices) are excluded unless an explicit
        `index_pattern` targets them. Aliases and data streams surface as
        their own union tables.
        """
        try:
            path = f"/{','.join(self._patterns)}/_mapping" if self._patterns else "/_mapping"
            mappings_by_index = self._request("GET", path)
            aliases_by_index = self._request("GET", "/_alias")
        except Exception as e:
            print(f"Error retrieving Elasticsearch mappings: {e}")
            return []

        streams = self._discover_data_streams()
        stream_backing = {
            (i or {}).get("index_name")
            for s in streams for i in (s.get("indices") or [])
        }

        # First pass: build a table per concrete (non-backing, non-system)
        # index, and bucket date-suffixed indices by their pattern base.
        concrete: Dict[str, Table] = {}
        pattern_members: Dict[str, List[str]] = {}
        alias_members: Dict[str, List[str]] = {}
        for index_name, body in sorted(mappings_by_index.items()):
            if index_name in stream_backing:
                continue
            if index_name.startswith(".") and not self._patterns:
                continue
            concrete[index_name] = self._table_from_mapping(
                index_name, (body or {}).get("mappings") or {}, "index")
            base = self._pattern_base(index_name)
            if base:
                pattern_members.setdefault(base, []).append(index_name)
            for alias in ((aliases_by_index.get(index_name) or {}).get("aliases") or {}):
                alias_members.setdefault(alias, []).append(index_name)

        tables: List[Table] = []
        collapsed: set = set()
        # Collapse each multi-member pattern into one `<base>-*` union table.
        # A base with a single member is left as its own concrete index (no
        # value in aliasing `foo-2026.07.10` to `foo-*` when there's one day).
        for base, members in sorted(pattern_members.items()):
            if len(members) < 2:
                continue
            tables.append(self._union_table(
                f"{base}-*", [concrete[m] for m in members], "pattern", members))
            collapsed.update(members)

        # Emit the remaining concrete indices that weren't collapsed.
        for name, table in concrete.items():
            if name not in collapsed:
                tables.append(table)

        by_name = {t.name: t for t in tables}
        for alias, members in sorted(alias_members.items()):
            if alias.startswith("."):
                continue
            member_tables = [concrete[m] for m in members if m in concrete]
            tables.append(self._union_table(alias, member_tables, "alias", members))

        tables.extend(self._stream_tables(streams, mappings_by_index))

        _ = by_name  # retained for parity/debugging; alias union uses concrete
        return tables

    # How many stream names to resolve per fallback `GET /{a,b,c}/_mapping`
    # call. Bounded by URL length (~4k): stream names run ~20-60 chars.
    _STREAM_MAPPING_BATCH = 50

    def _stream_tables(self, streams: List[Dict[str, Any]],
                       mappings_by_index: Dict[str, Any]) -> List[Table]:
        """Union tables for data streams, WITHOUT one mapping call per stream.

        The bulk ``GET /_mapping`` usually already contains the streams'
        hidden ``.ds-*`` backing indices (always, on serverless), so each
        stream's members are assembled from that response. Only streams whose
        backing indices are missing (stateful clusters that hide ``.ds-*``
        from the bulk call, or an `index_pattern` that skipped them) fall back
        to the network — batched as ``GET /{a,b,c}/_mapping`` instead of one
        call per stream, so a 2,000-stream estate costs a handful of requests,
        not 2,000. Failures still degrade per-batch to "no table", never an
        error, matching the previous per-stream behavior.
        """
        fetched: Dict[str, Any] = {}
        missing = [
            s["name"] for s in streams
            if not all((i or {}).get("index_name") in mappings_by_index
                       for i in (s.get("indices") or []))
        ]
        for start in range(0, len(missing), self._STREAM_MAPPING_BATCH):
            chunk = missing[start:start + self._STREAM_MAPPING_BATCH]
            try:
                fetched.update(self._request("GET", f"/{','.join(chunk)}/_mapping") or {})
            except Exception:
                continue

        tables: List[Table] = []
        for s in sorted(streams, key=lambda s: s["name"]):
            name = s["name"]
            backing = [(i or {}).get("index_name") for i in (s.get("indices") or [])]
            members = [
                self._table_from_mapping(
                    idx, ((mappings_by_index.get(idx) or fetched.get(idx)) or {}).get("mappings") or {}, "index")
                for idx in sorted(backing)
                if idx and (idx in mappings_by_index or idx in fetched)
            ]
            if not members:
                continue
            tables.append(self._union_table(name, members, "data_stream", backing))
        return tables

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_schema(self, index_name: str) -> Table:
        """Schema for a single index (or alias/pattern resolving to one or
        more). A pattern like `logs-app-*` unions every matching index."""
        body = self._request("GET", f"/{index_name}/_mapping")
        members = [
            self._table_from_mapping(idx, (m or {}).get("mappings") or {}, "index")
            for idx, m in sorted(body.items())
        ]
        if len(members) == 1:
            members[0].name = index_name
            return members[0]
        return self._union_table(index_name, members, "pattern", list(body.keys()))

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    # ---------- query execution ---------- #

    def execute_query(self, query: str) -> pd.DataFrame:
        """Execute a JSON query envelope and return a DataFrame.

        Envelope (JSON string):
        {
            "index": "logs-app-*",              # required (may be multi: "a-*,b-*")
            "query": {...},                     # query DSL
            "aggs": {...},                      # aggregations
            "size": 100, "sort": [...], "_source": [...],
            "sql":  "SELECT ...",               # alternative: Elasticsearch SQL
            "esql": "FROM logs-* | STATS ..."   # alternative: ES|QL (8.11+)
        }
        """
        try:
            envelope = json.loads(query)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON query: {e}")
        if not isinstance(envelope, dict):
            raise ValueError("Query must be a JSON object")

        if "esql" in envelope:
            return self._execute_esql(envelope["esql"])
        if "sql" in envelope:
            return self._execute_sql(envelope["sql"])

        index = envelope.get("index")
        if not index or not isinstance(index, str):
            raise ValueError("Query must specify 'index' (or use 'sql'/'esql')")

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

        # ignore_unavailable + allow_no_indices so a multi-pattern target like
        # "a-*,b-*" doesn't 404 when one pattern currently matches nothing.
        result = self._request(
            "POST", f"/{index}/_search", json_body=body,
            params={"ignore_unavailable": "true", "allow_no_indices": "true"},
        )

        if has_aggs:
            rows = self._flatten_aggregations(result.get("aggregations") or {})
            return pd.DataFrame(rows)

        hits = (result.get("hits") or {}).get("hits") or []
        if not hits:
            return pd.DataFrame()
        df = pd.json_normalize([h.get("_source") or {} for h in hits], sep=".")
        df.insert(0, "_id", [h.get("_id") for h in hits])
        df.insert(1, "_index", [h.get("_index") for h in hits])
        return df

    def _execute_sql(self, sql: str) -> pd.DataFrame:
        """Run a statement via Elasticsearch SQL (`POST /_sql?format=json`).

        Response shape: {columns:[{name,type}], rows:[[...]]}.
        """
        result = self._request("POST", "/_sql", params={"format": "json"},
                               json_body={"query": sql})
        cols = [c.get("name") for c in (result.get("columns") or [])]
        return pd.DataFrame(result.get("rows") or [], columns=cols or None)

    def _execute_esql(self, esql: str) -> pd.DataFrame:
        """Run a piped ES|QL query (`POST /_query`, ES 8.11+).

        Response shape: {columns:[{name,type}], values:[[...]]}.
        """
        result = self._request("POST", "/_query", json_body={"query": esql})
        cols = [c.get("name") for c in (result.get("columns") or [])]
        return pd.DataFrame(result.get("values") or [], columns=cols or None)

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
            number = version.get("number", "?")
            return {"success": True,
                    "message": f"Connected to Elasticsearch {number}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        return f"""
Elasticsearch cluster at {self.base_url}

This is a LOG / OBSERVABILITY search source. Tables are indices, patterns
(`logs-app-*` — the collapsed set of rolling daily indices), aliases, and data
streams. The index *mapping* is the schema, so every field below is real.

CRITICAL RULES:
1. Only use fields that EXIST in the schema - never assume fields exist. An
   unknown field is NOT an error in Elasticsearch: it silently matches nothing,
   which yields a wrong "0 results" answer. Check the schema first.
2. Use valid JSON: true/false/null (NOT Python True/False/None)
3. Aggregate, sort and filter on KEYWORD fields, never on "text" fields.
   When the schema shows both "message" (string/text) and "message.keyword",
   use "message.keyword" for terms aggs / sorting and "message" for full-text
   "match" / "query_string". Fields marked "NOT aggregatable/sortable"
   (full-text with no keyword subfield — the serverless default for message
   fields) can NEVER appear in terms aggs or sort: aggregate on a keyword
   field instead (e.g. error.type rather than error.message), or define a
   keyword copy via "runtime_mappings" first.
4. Fields marked "array" with children under "name[]" are NESTED - queries on
   them must be wrapped: {{"nested": {{"path": "items", "query": {{...}}}}}}
5. When only aggregations matter, "size" defaults to 0 automatically.
6. You may target MULTIPLE patterns at once: "index": "logs-app-*,logs-security-*".
7. Bound log searches by time using the @timestamp range filter.

Use execute_query() with a JSON envelope string.

**Example - log investigation (errors across services, last 24h):**
```python
df = client.execute_query('''{{
    "index": "logs-app-*,logs-security-*",
    "query": {{"bool": {{
        "must": [{{"query_string": {{"query": "level:(error OR fatal) OR status:>=500"}}}}],
        "filter": [{{"range": {{"@timestamp": {{"gte": "now-24h"}}}}}}],
        "must_not": [{{"match_phrase": {{"message": "healthcheck"}}}}]
    }}}},
    "sort": [{{"@timestamp": "desc"}}],
    "size": 100,
    "_source": ["@timestamp", "service", "level", "message"]
}}''')
```

**Example - count errors per service (aggregation):**
```python
df = client.execute_query('''{{
    "index": "logs-app-*",
    "query": {{"bool": {{"filter": [
        {{"term": {{"level": "error"}}}},
        {{"range": {{"@timestamp": {{"gte": "now-7d"}}}}}}
    ]}}}},
    "aggs": {{"by_service": {{"terms": {{"field": "service", "size": 20}}}}}}
}}''')
# -> columns: by_service, doc_count
```

**Example - SQL / ES|QL escape hatches (simple flat queries):**
```python
df = client.execute_query('{{"sql": "SELECT level, COUNT(*) n FROM \\"logs-app-*\\" GROUP BY level"}}')
df = client.execute_query('{{"esql": "FROM logs-app-* | STATS n = COUNT(*) BY level"}}')
```

WRONG - Do NOT do this:
```
{{"terms": {{"field": "message"}}}}   // text field - fails; use message.keyword
{{"term": {{"items.sku": "X"}}}}      // nested path without nested wrapper - matches nothing
```
"""


# Alias for the dynamic type->class naming convention. The registry's explicit
# client_path points at ElasticsearchClient.
ElasticSearchClient = ElasticsearchClient
