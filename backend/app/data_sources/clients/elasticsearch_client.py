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

**Least privilege.** Locked-down deployments hand out API keys carrying index
privileges only — no cluster privileges at all — often one key per index
pattern (``eksa*``, ``ekpb*``, …). Everything this client does fits in
``read`` + ``view_index_metadata`` on those patterns:

===========================  ===================================
call                         privilege
===========================  ===================================
``GET /{pattern}/_mapping``  ``view_index_metadata``
``GET /{pattern}/_alias``    ``view_index_metadata``
``GET /_data_stream/{p}``    ``view_index_metadata``
``POST /{index}/_search``    ``read``
===========================  ===================================

The one exception is ``GET /`` (the version banner), which maps to
``cluster:monitor/main`` and needs cluster ``monitor``/``manage``/``all`` — so
``test_connection`` treats it as optional and falls back to probes that need no
cluster privilege (see there). Discovery is likewise per-pattern and lenient:
one unreadable or currently-empty glob degrades to "that pattern contributed
nothing" instead of zeroing the whole catalog.
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


class ElasticsearchHttpError(RuntimeError):
    """The cluster answered with an HTTP error.

    Distinct from a transport failure (DNS/TLS/refused): the host is *reachable*
    and it is the request — usually the credentials' privileges — that was
    rejected. Callers branch on that, so a 403 is never mistaken for a bad
    endpoint. Subclasses RuntimeError so existing `except Exception` paths and
    error strings are unchanged.
    """

    def __init__(self, message: str, status_code: int, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


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

    # Sent on every index-targeted metadata/search call so a multi-pattern
    # target ("a-*,b-*") does not fail as a whole when one glob currently
    # matches nothing — or matches nothing *this key is allowed to see*.
    LENIENT_TARGET = {"ignore_unavailable": "true", "allow_no_indices": "true"}

    # The index privileges this client needs. Used to explain a failed probe
    # ("missing view_index_metadata on eksa*") instead of echoing a raw 403.
    REQUIRED_INDEX_PRIVILEGES = ("read", "view_index_metadata")

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

        # System/hidden `.`-indices are catalog noise, so they are surfaced only
        # when a pattern names them (`.ds-*`). Keyed off the pattern text rather
        # than "any pattern is set", so `index_pattern = "*"` no longer drags
        # `.security-7` and friends into the catalog.
        self._targets_system = any(p.startswith(".") for p in self._patterns)

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
            raise ElasticsearchHttpError(
                f"Elasticsearch request {method} {path} failed "
                f"[{resp.status_code}]: {resp.text.strip()[:2000]}"
                f"{self._privilege_hint(resp.status_code)}",
                status_code=resp.status_code,
                body=resp.text.strip()[:2000],
            )
        return resp.json()

    @staticmethod
    def _privilege_hint(status_code: int) -> str:
        """Turn an auth failure into an actionable next step.

        Reaches both the admin (connection status) and the agent (tool error
        text). The wildcard note is the practical half: verified against 8.15,
        a target naming an index the key may not see is rejected outright,
        while a wildcard resolves to the authorized subset instead.
        """
        if status_code == 403:
            return (
                " — the credentials lack the privilege for this call. This "
                "connector needs `read` + `view_index_metadata` on the indices "
                "it should expose; note that a target naming an index the key "
                "cannot see is rejected outright, while a wildcard (e.g. "
                "`eksa*`) resolves to the authorized subset, so prefer the "
                "patterns listed in the schema."
            )
        if status_code == 401:
            return " — authentication failed; the API key or password is invalid or expired."
        return ""

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

    def _target_params(self, target: Optional[str]) -> Dict[str, Any]:
        """Query params for a metadata call against `target`.

        An explicitly configured pattern means "expose whatever this matches",
        so hidden indices are expanded too — wildcards skip them by default,
        which would silently drop a hidden index named `eksa-archive` from a
        connection scoped to `eksa*`. A bare `*` is left alone: expanding it
        would fetch every system index's mapping only to filter it back out.
        """
        params = dict(self.LENIENT_TARGET)
        if target and target not in ("*", "_all"):
            params["expand_wildcards"] = "open,hidden"
        return params

    def _fetch_mappings(self) -> tuple:
        """Bulk mappings, fetched **per configured pattern**.

        One request per pattern rather than one comma-joined request, because
        the joined form fails as a unit: with `index_pattern = "eksa*,ekpb*"`,
        a key that may not see `ekpb*` loses the perfectly readable `eksa*`
        mappings too. Returns (mappings_by_index, errors) so the caller can
        tell "nothing readable" from "some patterns contributed nothing".
        """
        mappings: Dict[str, Any] = {}
        errors: List[str] = []
        for target in (self._patterns or [None]):
            path = f"/{target}/_mapping" if target else "/_mapping"
            try:
                mappings.update(self._request("GET", path,
                                              params=self._target_params(target)) or {})
            except Exception as e:
                errors.append(f"{target or '_all'}: {e}")
        return mappings, errors

    def _fetch_aliases(self) -> Dict[str, Any]:
        """Aliases for the configured patterns; `{}` if unreadable.

        Scoped and isolated on purpose: aliases are an enrichment, so a key
        without alias visibility must not cost us the mappings we already hold
        (they used to share one try block, where an alias failure returned an
        empty catalog).
        """
        aliases: Dict[str, Any] = {}
        for target in (self._patterns or [None]):
            path = f"/{target}/_alias" if target else "/_alias"
            try:
                aliases.update(self._request("GET", path,
                                             params=self._target_params(target)) or {})
            except Exception:
                continue
        return aliases

    def get_tables(self) -> List[Table]:
        """Discover indices, patterns, aliases, and data streams with their
        mapped fields.

        One bulk `GET /_mapping` call per configured pattern. Time-series
        indices sharing a date/rollover suffix collapse into a single
        `<base>-*` pattern table (union of fields). System indices
        (`.`-prefixed, incl. data-stream backing indices) are excluded unless
        an `index_pattern` explicitly targets `.`-names. Aliases and data
        streams surface as their own union tables.

        Raises when *every* target failed — an empty catalog with a swallowed
        403 shows the admin "0 tables" and no reason, while the raised message
        lands on the indexing row and in the connection test.
        """
        mappings_by_index, errors = self._fetch_mappings()
        if errors and not mappings_by_index:
            raise RuntimeError(
                "Elasticsearch schema discovery failed for every configured "
                "index pattern — " + "; ".join(errors)
            )
        aliases_by_index = self._fetch_aliases()

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
            if index_name.startswith(".") and not self._targets_system:
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

    # Fallback `GET /{a,b,c}/_mapping` batching: at most 50 names per call,
    # and never past ~3000 chars of joined names — stream names may run to
    # 255 bytes and the engine's request line tops out at 4kB by default.
    _STREAM_MAPPING_BATCH = 50
    _STREAM_MAPPING_MAX_CHARS = 3000

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
        chunk: List[str] = []
        chunks: List[List[str]] = []
        for name in missing:
            if chunk and (len(chunk) >= self._STREAM_MAPPING_BATCH
                          or len(",".join(chunk)) + len(name) + 1 > self._STREAM_MAPPING_MAX_CHARS):
                chunks.append(chunk)
                chunk = []
            chunk.append(name)
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
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
        body = self._request("GET", f"/{index_name}/_mapping",
                             params=self._target_params(index_name))
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
            params=dict(self.LENIENT_TARGET),
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

    def _privilege_report(self) -> Dict[str, Any]:
        """What these credentials may actually do, in their own words.

        `_has_privileges` is usable by ANY authenticated user for their *own*
        privileges, so it answers exactly where the other probes 403 — turning
        an opaque rejection into "missing view_index_metadata on eksa*".
        """
        names = self._patterns or ["*"]
        try:
            result = self._request(
                "POST", "/_security/user/_has_privileges",
                json_body={"index": [{"names": names,
                                      "privileges": list(self.REQUIRED_INDEX_PRIVILEGES)}]},
            ) or {}
        except Exception:
            return {}
        missing = [f"`{priv}` on `{name}`"
                   for name, granted in sorted((result.get("index") or {}).items())
                   for priv, ok in sorted(granted.items()) if not ok]
        return {"has_all_requested": bool(result.get("has_all_requested")),
                "missing_privileges": missing}

    @staticmethod
    def _privilege_warning(report: Dict[str, Any]) -> str:
        """Say what a half-granted key will break, while the test still passes.

        `view_index_metadata` alone is the trap: the connection tests green AND
        indexes a full catalog, then every query 403s. Named here so the admin
        sees it now rather than in the first session.

        A heads-up, never a failure: the check runs against the *configured*
        patterns, which may be wider than the key's actual grant (pattern
        `eksa*` against a key scoped to `eksa-app*` reads as missing), and only
        the real probes decide success.
        """
        missing = report.get("missing_privileges") or []
        if not missing:
            return ""
        breaks = []
        if any("`read`" in m for m in missing):
            breaks.append("queries will fail")
        if any("view_index_metadata" in m for m in missing):
            breaks.append("schema discovery will fail")
        return (f" Warning: the credentials appear to be missing "
                f"{', '.join(missing)} — {' and '.join(breaks)}.")

    def test_connection(self):
        """Confirm endpoint + credentials WITHOUT requiring cluster privileges.

        `GET /` is the informative probe — it carries the version — but it maps
        to `cluster:monitor/main`, granted only by cluster `monitor`/`manage`/
        `all`. Deployments that hand out index-scoped API keys grant none of
        those, and a 403 there says nothing about whether this connector can
        work: it blocks the save path (which hard-blocks on `reachable: False`)
        and flips `is_active` off on every status sweep.

        So the probe degrades instead of failing:
          1. `GET /`                         — best case, reports the version.
          2. `GET /_security/_authenticate`  — any authenticated user, no privilege.
          3. `GET /{patterns}/_mapping`      — needs exactly the
             `view_index_metadata` the connector already requires.

        Only a transport failure is `reachable: False`. Any HTTP answer proves
        the host is there, so the result carries `reachable: True` and the save
        path lets the admin through to the schema check — which reports what is
        actually missing.
        """
        notes: List[str] = []

        try:
            info = self._request("GET", "/") or {}
            number = (info.get("version") or {}).get("number", "?")
            return {"success": True, "reachable": True,
                    "message": f"Connected to Elasticsearch {number}"}
        except ElasticsearchHttpError as e:
            notes.append(f"GET / -> HTTP {e.status_code}")
        except Exception as e:
            return {"success": False, "reachable": False, "message": str(e)}

        try:
            who = self._request("GET", "/_security/_authenticate") or {}
            username = who.get("username")
            # `username` on an API key is the key's *owner*, so name the key
            # itself when there is one — an admin comparing several per-pattern
            # keys needs to know which one this connection holds.
            key_name = ((who.get("api_key") or {}).get("name")
                        if who.get("authentication_type") == "api_key" else None)
            identity = (f"API key '{key_name}' (owner: {username})" if key_name
                        else username or "the supplied credentials")
            report = self._privilege_report()
            return {
                "success": True, "reachable": True,
                "message": (
                    f"Connected to Elasticsearch as {identity}. The cluster "
                    f"`monitor` privilege is not granted, so the version is "
                    f"unavailable — index access is unaffected."
                    + self._privilege_warning(report)
                ),
                "details": {"username": username, "roles": who.get("roles") or [],
                            "cluster_monitor": False, **report},
            }
        except ElasticsearchHttpError as e:
            notes.append(f"GET /_security/_authenticate -> HTTP {e.status_code}")
        except Exception as e:
            return {"success": False, "reachable": False, "message": str(e)}

        target = ",".join(self._patterns) if self._patterns else "*"
        try:
            self._request("GET", f"/{target}/_mapping",
                          params=self._target_params(target if self._patterns else None))
            report = self._privilege_report()
            return {
                "success": True, "reachable": True,
                "message": (f"Connected to Elasticsearch — index metadata readable "
                            f"for `{target}`. No cluster privileges are granted, so "
                            f"the version is unavailable."
                            + self._privilege_warning(report)),
                "details": {"cluster_monitor": False, **report},
            }
        except ElasticsearchHttpError as e:
            notes.append(f"GET /{target}/_mapping -> HTTP {e.status_code}")
            message = str(e)
        except Exception as e:
            return {"success": False, "reachable": False, "message": str(e)}

        report = self._privilege_report()
        if report.get("missing_privileges"):
            message = ("Connected, but the credentials are missing "
                       + ", ".join(report["missing_privileges"])
                       + ". Grant `read` + `view_index_metadata` on the indices "
                         "this connection should expose.")
        return {"success": False, "reachable": True, "message": message,
                "details": {"probes": notes, **report}}

    @property
    def description(self) -> str:
        # A scoped connection's credentials often see ONLY these patterns, so
        # the agent has to know the boundary — targeting anything else is a 403,
        # not an empty result.
        scope = (f"\nSCOPE: this connection is restricted to {', '.join(self._patterns)} — "
                 f"every query must target these patterns.\n" if self._patterns else "")
        return f"""
Elasticsearch cluster at {self.base_url}
{scope}
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
8. Target the names shown in the schema, preferring the wildcard patterns. The
   credentials may be scoped to a subset of the cluster, and a name they cannot
   see is rejected while a wildcard resolves to whatever is permitted. If
   "sql"/"esql" fails with 403 or "Unknown index", the index exists but is
   outside this connection's grant — re-target a pattern from the schema, or
   ask the same question with the query DSL envelope.

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
