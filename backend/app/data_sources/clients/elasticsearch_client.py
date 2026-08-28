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
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter

logger = logging.getLogger(__name__)

DASHBOARD_PREFIX = "dashboard::"
SAVED_SEARCH_PREFIX = "saved_search::"
MAX_KIBANA_OBJECTS = 300     # cap on dashboards / saved searches cataloged
_FIND_PAGE = 100             # saved-objects _find page size


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
        kibana_url: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.api_key = api_key or None
        self.user = user
        self.password = password
        self.secure = secure
        self.verify_certs = verify_certs
        self.index_pattern = index_pattern
        # Optional Kibana base URL: enables the dashboard / saved-search
        # catalog. Kibana authenticates through Elasticsearch security, so the
        # connection's existing credentials (API key or basic) are reused —
        # no separate Kibana credential.
        k = (kibana_url or "").strip().rstrip("/")
        if k and not (k.startswith("http://") or k.startswith("https://")):
            k = f"http://{k}" if not secure else f"https://{k}"
        self.kibana_url = k or None

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
            # Get Mapping is not supported by cross-cluster search. Field Caps
            # is, so synthesize the small mapping shape used by get_tables().
            if target and ":" in target:
                try:
                    result = self._request(
                        "GET", f"/{target}/_field_caps",
                        params={"fields": "*", **self.LENIENT_TARGET},
                    ) or {}
                    properties: Dict[str, Any] = {}
                    for field, variants in (result.get("fields") or {}).items():
                        if not isinstance(variants, dict):
                            continue
                        variant = next(
                            (value for value in variants.values()
                             if isinstance(value, dict)),
                            None,
                        )
                        if variant and variant.get("type"):
                            properties[field] = {"type": variant["type"]}
                    if properties:
                        mappings[target] = {"mappings": {"properties": properties}}
                except Exception as e:
                    errors.append(f"{target}: {e}")
                continue
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

    # ---------- Kibana dashboards & saved searches ---------- #
    #
    # Kibana stores the operators' investigation knowledge as saved objects:
    # dashboards (panels referencing Lens/legacy visualizations and Discover
    # sessions) and saved searches. Cataloging them lets the agent replay a
    # manual RCA: find the dashboard for the affected service, read what each
    # panel queries, re-run it scoped to the incident window. ES|QL panels and
    # Discover searches carry directly runnable queries; Lens/legacy panels
    # are config-not-query, so they surface as a structured summary (index
    # pattern + fields + operations) the agent composes DSL/ES|QL from.
    # Everything here is best-effort: no kibana_url, an unreachable Kibana, or
    # an unparseable object never breaks index discovery.

    def _kibana_request(self, method: str, path: str,
                        params: Optional[Dict[str, Any]] = None,
                        json_body: Any = None) -> Any:
        auth, extra_headers = self._auth()
        headers = {"Content-Type": "application/json", "kbn-xsrf": "true"}
        headers.update(extra_headers)
        resp = requests.request(
            method, f"{self.kibana_url}{path}", params=params, json=json_body,
            auth=auth, verify=self.verify_certs, timeout=self.TIMEOUTS,
            headers=headers,
        )
        if resp.status_code >= 400:
            raise ElasticsearchHttpError(
                f"Kibana request {method} {path} failed "
                f"({resp.status_code}): {resp.text[:300]}",
                resp.status_code, resp.text[:2000])
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"Kibana returned non-JSON on {path}: {resp.text[:200]}")

    def _kibana_spaces(self) -> List[str]:
        """Space ids visible to the credentials; ['default'] when the spaces
        API is unavailable (basic license, missing privilege)."""
        try:
            spaces = self._kibana_request("GET", "/api/spaces/space")
            ids = [s.get("id") for s in spaces if s.get("id")]
            return ids or ["default"]
        except Exception as e:
            logger.warning(f"Kibana spaces enumeration failed ({e}); using default space")
            return ["default"]

    @staticmethod
    def _space_path(space: str) -> str:
        return "" if space in ("default", "", None) else f"/s/{space}"

    def _find_saved_objects(self, space: str, so_type: str) -> List[Dict[str, Any]]:
        """All saved objects of one type in one space, paginated."""
        out: List[Dict[str, Any]] = []
        page = 1
        while len(out) < MAX_KIBANA_OBJECTS:
            body = self._kibana_request(
                "GET", f"{self._space_path(space)}/api/saved_objects/_find",
                params={"type": so_type, "per_page": _FIND_PAGE, "page": page})
            objs = body.get("saved_objects") or []
            out.extend(objs)
            if len(objs) < _FIND_PAGE:
                break
            page += 1
        return out[:MAX_KIBANA_OBJECTS]

    def _bulk_get(self, space: str, refs: List[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict]:
        """Resolve (type, id) references via _bulk_get; failures resolve to {}."""
        if not refs:
            return {}
        try:
            body = self._kibana_request(
                "POST", f"{self._space_path(space)}/api/saved_objects/_bulk_get",
                json_body=[{"type": t, "id": i} for t, i in refs])
            return {(o.get("type"), o.get("id")): o
                    for o in (body.get("saved_objects") or []) if not o.get("error")}
        except Exception as e:
            logger.warning(f"Kibana _bulk_get failed: {e}")
            return {}

    # -- panel query extraction (per dialect) --

    @staticmethod
    def _parse_json_attr(raw) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw or "null")
        except (ValueError, TypeError):
            return None

    @classmethod
    def _lens_panel(cls, attrs: Dict[str, Any],
                    pattern_titles: Dict[str, str]) -> Dict[str, Any]:
        """Summarize a Lens visualization's query intent.

        `textBased` (ES|QL) layers carry a runnable query. `formBased` /
        `indexpattern` layers are agg configs — summarized as
        field/operation pairs over the referenced index pattern."""
        state = attrs.get("state") or {}
        ds_states = state.get("datasourceStates") or {}
        # ES|QL layers → runnable.
        text_based = ds_states.get("textBased") or {}
        for layer in (text_based.get("layers") or {}).values():
            q = ((layer.get("query") or {}).get("esql")
                 if isinstance(layer.get("query"), dict) else None)
            if q:
                return {"kind": "lens_esql", "query": q, "runnable": True,
                        "language": "esql", "index": None, "summary": q}
        # Form-based layers → summary of operations.
        ops: List[str] = []
        form = ds_states.get("formBased") or ds_states.get("indexpattern") or {}
        for layer in (form.get("layers") or {}).values():
            for col in (layer.get("columns") or {}).values():
                op = col.get("operationType") or "?"
                field = col.get("sourceField") or col.get("label") or ""
                ops.append(f"{op}({field})" if field else op)
        bar_query = ((state.get("query") or {}).get("query")
                     if isinstance(state.get("query"), dict) else None)
        index = None
        for r in attrs.get("references") or []:
            if r.get("type") == "index-pattern":
                index = pattern_titles.get(r.get("id")) or index
        summary = ", ".join(ops[:12]) or "lens visualization"
        if bar_query:
            summary += f' [query: {bar_query}]'
        return {"kind": "lens", "query": bar_query, "runnable": False,
                "language": "kuery", "index": index, "summary": summary}

    @classmethod
    def _search_source(cls, attrs: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """(query_string, index_pattern_ref_id) from kibanaSavedObjectMeta."""
        meta = attrs.get("kibanaSavedObjectMeta") or {}
        ss = cls._parse_json_attr(meta.get("searchSourceJSON")) or {}
        q = ss.get("query") or {}
        query = q.get("query") if isinstance(q, dict) else None
        index_ref = ss.get("indexRefName") or ss.get("index")
        return (query or None), index_ref

    @classmethod
    def _viz_panel(cls, attrs: Dict[str, Any],
                   pattern_titles: Dict[str, str],
                   ref_index: Optional[str]) -> Dict[str, Any]:
        """Summarize a legacy (visState) visualization."""
        vis = cls._parse_json_attr(attrs.get("visState")) or {}
        aggs = []
        for a in vis.get("aggs") or []:
            p = a.get("params") or {}
            f = p.get("field")
            aggs.append(f"{a.get('type')}({f})" if f else str(a.get("type")))
        query, _ = cls._search_source(attrs)
        summary = f"{vis.get('type') or 'viz'}: " + (", ".join(aggs) or "no aggs")
        if query:
            summary += f' [query: {query}]'
        return {"kind": "visualization", "query": query, "runnable": False,
                "language": "kuery", "index": ref_index, "summary": summary}

    @classmethod
    def _discover_panel(cls, attrs: Dict[str, Any],
                        ref_index: Optional[str]) -> Dict[str, Any]:
        """A Discover session / saved search — runnable as a query_string."""
        query, _ = cls._search_source(attrs)
        cols = attrs.get("columns") or []
        summary = f"saved search: {query or '*'}"
        if cols:
            summary += f" | columns: {', '.join(cols[:8])}"
        return {"kind": "search", "query": query or "*", "runnable": True,
                "language": "query_string", "index": ref_index, "summary": summary}

    def _resolve_dashboard_panels(self, space: str,
                                  dash: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract panels (title, kind, index, query/summary, runnable) from a
        dashboard saved object, resolving by-reference panels via _bulk_get."""
        attrs = dash.get("attributes") or {}
        panels = self._parse_json_attr(attrs.get("panelsJSON")) or []
        refs = {r.get("name"): r for r in (dash.get("references") or [])}

        # Collect referenced objects (panels + index patterns) for one bulk call.
        wanted: List[Tuple[str, str]] = []
        for r in (dash.get("references") or []):
            if r.get("type") in ("lens", "visualization", "search", "map"):
                wanted.append((r["type"], r["id"]))
        resolved = self._bulk_get(space, sorted(set(wanted)))

        # Index-pattern titles: from dashboard refs + refs of resolved objects.
        ip_refs: List[Tuple[str, str]] = [
            ("index-pattern", r["id"]) for r in (dash.get("references") or [])
            if r.get("type") == "index-pattern"]
        for o in resolved.values():
            for r in (o.get("references") or []):
                if r.get("type") == "index-pattern":
                    ip_refs.append(("index-pattern", r["id"]))
        ip_objs = self._bulk_get(space, sorted(set(ip_refs)))
        pattern_titles = {i: (o.get("attributes") or {}).get("title")
                          for (_, i), o in ip_objs.items()}

        def ref_index_for(obj) -> Optional[str]:
            for r in (obj or {}).get("references") or []:
                if r.get("type") == "index-pattern":
                    return pattern_titles.get(r.get("id"))
            return None

        out: List[Dict[str, Any]] = []
        for p in panels if isinstance(panels, list) else []:
            try:
                cfg = p.get("embeddableConfig") or {}
                title = (p.get("title") or cfg.get("title")
                         or cfg.get("savedObjectId") or "").strip()
                ptype = p.get("type") or ""
                ref = None
                ref_name = p.get("panelRefName")
                if ref_name:
                    key = f"{p.get('panelIndex')}:{ref_name}"
                    ref = refs.get(key) or refs.get(ref_name)
                inline_attrs = cfg.get("attributes") if isinstance(cfg.get("attributes"), dict) else None
                obj = None
                if ref:
                    obj = resolved.get((ref.get("type"), ref.get("id")))
                    ptype = ref.get("type") or ptype
                attrs_src = inline_attrs or ((obj or {}).get("attributes") or {})
                if not attrs_src:
                    continue
                title = title or (attrs_src.get("title") or "").strip() or ptype
                if ptype == "lens":
                    # By-value lens: index-pattern refs live inside the config.
                    local_titles = dict(pattern_titles)
                    if inline_attrs:
                        extra = [("index-pattern", r["id"])
                                 for r in (inline_attrs.get("references") or [])
                                 if r.get("type") == "index-pattern"]
                        for (_, i), o in self._bulk_get(space, extra).items():
                            local_titles[i] = (o.get("attributes") or {}).get("title")
                        attrs_src = dict(attrs_src)
                        attrs_src.setdefault("references", inline_attrs.get("references") or [])
                    panel = self._lens_panel(attrs_src, local_titles)
                    if panel["index"] is None:
                        panel["index"] = ref_index_for(obj or {"references": (inline_attrs or {}).get("references")})
                elif ptype == "search":
                    panel = self._discover_panel(attrs_src, ref_index_for(obj))
                elif ptype == "visualization":
                    panel = self._viz_panel(attrs_src, pattern_titles, ref_index_for(obj))
                else:
                    continue  # maps, links, images — no query content
                panel["title"] = title
                out.append(panel)
            except Exception as e:
                logger.warning(f"Kibana panel parse failed on dashboard "
                               f"'{(attrs.get('title') or '?')}': {e}")
        return out

    def _panel_column_desc(self, p: Dict[str, Any]) -> str:
        idx = f" over {p['index']}" if p.get("index") else ""
        if p.get("runnable") and p.get("language") == "esql":
            return f"ES|QL{idx}: {p['query']}"
        if p.get("runnable"):
            return f"query_string{idx}: {p['query']}"
        return f"{p['kind']}{idx}: {p['summary']}"

    def _dashboard_table(self, space: str, dash: Dict[str, Any],
                         panels: List[Dict[str, Any]]) -> Table:
        attrs = dash.get("attributes") or {}
        title = (attrs.get("title") or dash.get("id") or "").strip()
        columns = [TableColumn(name=p["title"], dtype="panel",
                               description=self._panel_column_desc(p))
                   for p in panels]
        titles = "; ".join(p["title"] for p in panels[:8])
        user_desc = (attrs.get("description") or "").strip()
        desc = (f"Kibana dashboard '{title}' (space: {space})."
                + (f" {user_desc}" if user_desc else "")
                + (f" Panels: {titles}." if titles else "")
                + " Each panel column's description carries its query (ES|QL /"
                  " query_string panels are directly runnable) or its Lens/viz"
                  " aggregation summary — reuse them for incident analysis,"
                  " re-scoped to the incident time window. Run a panel with"
                  " execute_query('{\"dashboard\": \"" + f"{space}/{title}" + "\","
                  " \"panel\": \"<title>\", \"earliest\": \"now-4h\"}') or adapt"
                  " its query directly against the index it names.")
        return Table(
            name=f"{DASHBOARD_PREFIX}{space}/{title}", description=desc,
            columns=columns, pks=[], fks=[],
            metadata_json={"kibana": {
                "kind": "dashboard", "space": space, "dashboard_id": dash.get("id"),
                "panel_count": len(panels), "managed": bool(dash.get("managed")),
                "panels": panels,
            }},
        )

    def _kibana_dashboard_tables(self) -> List[Table]:
        tables: List[Table] = []
        for space in self._kibana_spaces():
            for dash in self._find_saved_objects(space, "dashboard"):
                if len(tables) >= MAX_KIBANA_OBJECTS:
                    return tables
                try:
                    panels = self._resolve_dashboard_panels(space, dash)
                    tables.append(self._dashboard_table(space, dash, panels))
                except Exception as e:
                    logger.warning(
                        f"Kibana dashboard catalog skipped "
                        f"{space}/{(dash.get('attributes') or {}).get('title')}: {e}")
        return tables

    def _kibana_saved_search_tables(self) -> List[Table]:
        tables: List[Table] = []
        for space in self._kibana_spaces():
            for so in self._find_saved_objects(space, "search"):
                if len(tables) >= MAX_KIBANA_OBJECTS:
                    return tables
                attrs = so.get("attributes") or {}
                title = (attrs.get("title") or "").strip()
                if not title:
                    continue
                ip_refs = [("index-pattern", r["id"])
                           for r in (so.get("references") or [])
                           if r.get("type") == "index-pattern"]
                ip = self._bulk_get(space, ip_refs)
                index = next(((o.get("attributes") or {}).get("title")
                              for o in ip.values()), None)
                query, _ = self._search_source(attrs)
                user_desc = (attrs.get("description") or "").strip()
                desc = (f"Kibana saved search (space: {space})."
                        + (f" {user_desc}" if user_desc else "")
                        + (f" Query over {index}: " if index else " Query: ")
                        + f"`{query or '*'}`."
                        + " Run it with execute_query('{\"saved_search\": \""
                        + f"{space}/{title}" + "\", \"earliest\": \"now-4h\"}')"
                        " or adapt the query_string against the index.")
                tables.append(Table(
                    name=f"{SAVED_SEARCH_PREFIX}{space}/{title}", description=desc,
                    columns=[], pks=[], fks=[],
                    metadata_json={"kibana": {
                        "kind": "saved_search", "space": space,
                        "search_id": so.get("id"), "index": index,
                        "query": query or "*", "columns": attrs.get("columns") or [],
                    }},
                ))
        return tables

    def _kibana_knowledge_tables(self) -> List[Table]:
        """Dashboards + saved searches, best-effort — never fails discovery."""
        if not self.kibana_url:
            return []
        tables: List[Table] = []
        try:
            tables.extend(self._kibana_dashboard_tables())
        except Exception as e:
            logger.warning(f"Kibana dashboard catalog failed: {e}")
        try:
            tables.extend(self._kibana_saved_search_tables())
        except Exception as e:
            logger.warning(f"Kibana saved-search catalog failed: {e}")
        return tables

    def _find_dashboard(self, space: str, name: str) -> Dict[str, Any]:
        """A dashboard saved object by title (exact, case-insensitive) or id."""
        want = name.strip().lower()
        matches = []
        for dash in self._find_saved_objects(space, "dashboard"):
            title = ((dash.get("attributes") or {}).get("title") or "").strip()
            if dash.get("id") == name or title.lower() == want:
                matches.append(dash)
        if not matches:
            raise ValueError(f"Kibana dashboard not found in space '{space}': {name}")
        return matches[0]

    def get_schemas(self) -> List[Table]:
        tables = self.get_tables()
        tables.extend(self._kibana_knowledge_tables())
        return tables

    @staticmethod
    def _split_kibana_ref(ref: str, prefix: str) -> Tuple[str, str]:
        """`dashboard::default/Checkout Health` (or bare `default/…`, or just a
        title/id) → (space, name). Space defaults to 'default'."""
        r = (ref or "").strip()
        if r.startswith(prefix):
            r = r[len(prefix):]
        if "/" in r:
            space, name = r.split("/", 1)
            return (space or "default"), name
        return "default", r

    def get_schema(self, index_name: str) -> Table:
        """Schema for a single index (or alias/pattern resolving to one or
        more; a pattern like `logs-app-*` unions every matching index) — or
        the live panel set for a `dashboard::space/title` /
        `saved_search::space/title` Kibana entry."""
        if index_name.startswith(DASHBOARD_PREFIX):
            space, name = self._split_kibana_ref(index_name, DASHBOARD_PREFIX)
            dash = self._find_dashboard(space, name)
            panels = self._resolve_dashboard_panels(space, dash)
            return self._dashboard_table(space, dash, panels)
        if index_name.startswith(SAVED_SEARCH_PREFIX):
            space, name = self._split_kibana_ref(index_name, SAVED_SEARCH_PREFIX)
            want = name.strip().lower()
            for t in self._kibana_saved_search_tables():
                if t.name.lower() == f"{SAVED_SEARCH_PREFIX}{space}/{want}":
                    return t
            raise ValueError(f"Kibana saved search not found: {space}/{name}")
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
            envelope = json.loads(query) if isinstance(query, str) else query
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON query: {e}")
        if not isinstance(envelope, dict):
            raise ValueError("Query must be a JSON object")

        if envelope.get("dashboard"):
            return self._execute_dashboard(envelope)
        if envelope.get("saved_search"):
            return self._execute_saved_search(envelope)
        if "esql" in envelope:
            return self._execute_esql(envelope["esql"])
        if "sql" in envelope:
            return self._execute_sql(envelope["sql"])

        index = envelope.get("index")
        if not index or not isinstance(index, str):
            raise ValueError(
                "Query must specify 'index' (or use 'sql'/'esql', or a Kibana "
                "knowledge envelope: {\"dashboard\": \"space/title\"} / "
                "{\"saved_search\": \"space/title\"})")

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

    def _run_query_string(self, index: Optional[str], query: str,
                          spec: Dict[str, Any]) -> pd.DataFrame:
        """Run a Kibana query_string panel/search as a DSL search, scoped to
        the incident window (`earliest`/`latest`, default now-24h .. now)."""
        if not index:
            raise ValueError(
                "This panel does not name an index pattern — compose a query "
                "against one of the catalog's index tables instead.")
        body: Dict[str, Any] = {
            "index": index,
            "query": {"bool": {
                "must": ([{"query_string": {"query": query}}]
                         if query and query != "*" else [{"match_all": {}}]),
                "filter": [{"range": {"@timestamp": {
                    "gte": spec.get("earliest") or "now-24h",
                    "lte": spec.get("latest") or "now"}}}],
            }},
            "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
            "size": int(spec.get("limit") or spec.get("size") or 100),
        }
        return self.execute_query(json.dumps(body))

    def _panels_inventory(self, panels: List[Dict[str, Any]]) -> pd.DataFrame:
        return pd.DataFrame([{
            "panel": p.get("title"), "kind": p.get("kind"),
            "index": p.get("index"), "runnable": bool(p.get("runnable")),
            "query": p.get("query") if p.get("runnable") else p.get("summary"),
        } for p in panels])

    def _execute_dashboard(self, spec: Dict[str, Any]) -> pd.DataFrame:
        if not self.kibana_url:
            raise ValueError("This connection has no Kibana URL configured.")
        space, name = self._split_kibana_ref(str(spec["dashboard"]), DASHBOARD_PREFIX)
        dash = self._find_dashboard(space, name)
        panels = self._resolve_dashboard_panels(space, dash)
        panel_ref = spec.get("panel")
        if panel_ref is None or panel_ref == "":
            # No panel selected → the inventory (title, kind, query/summary),
            # so the agent can read the queries and pick.
            return self._panels_inventory(panels)
        panel = None
        if isinstance(panel_ref, int) or (isinstance(panel_ref, str) and panel_ref.isdigit()):
            i = int(panel_ref)
            if 0 <= i < len(panels):
                panel = panels[i]
        if panel is None:
            want = str(panel_ref).strip().lower()
            panel = next((p for p in panels
                          if (p.get("title") or "").strip().lower() == want), None)
        if panel is None:
            titles = "; ".join(p.get("title") or "?" for p in panels)
            raise ValueError(f"Panel '{panel_ref}' not found on {space}/{name}. "
                             f"Panels: {titles}")
        if panel.get("runnable") and panel.get("language") == "esql":
            # ES|QL runs as stored — its FROM/WHERE already encode the scope;
            # adapt the query text itself to change the window.
            return self._execute_esql(panel["query"])
        if panel.get("runnable"):
            return self._run_query_string(panel.get("index"), panel.get("query") or "*", spec)
        # Config-not-query panel (Lens aggs / legacy viz): return its recipe so
        # the agent can compose the equivalent DSL/ES|QL — never a dead end.
        return pd.DataFrame([{
            "panel": panel.get("title"), "kind": panel.get("kind"),
            "index": panel.get("index"), "runnable": False,
            "recipe": panel.get("summary"),
            "hint": ("Not directly executable. Compose the equivalent query "
                     "against the index above using this recipe (operations "
                     "are Lens/viz aggregations)."),
        }])

    def _execute_saved_search(self, spec: Dict[str, Any]) -> pd.DataFrame:
        if not self.kibana_url:
            raise ValueError("This connection has no Kibana URL configured.")
        space, name = self._split_kibana_ref(str(spec["saved_search"]), SAVED_SEARCH_PREFIX)
        want = name.strip().lower()
        for so in self._find_saved_objects(space, "search"):
            attrs = so.get("attributes") or {}
            title = (attrs.get("title") or "").strip()
            if so.get("id") != name and title.lower() != want:
                continue
            query, _ = self._search_source(attrs)
            ip_refs = [("index-pattern", r["id"]) for r in (so.get("references") or [])
                       if r.get("type") == "index-pattern"]
            ip = self._bulk_get(space, ip_refs)
            index = next(((o.get("attributes") or {}).get("title")
                          for o in ip.values()), None)
            return self._run_query_string(index, query or "*", spec)
        raise ValueError(f"Kibana saved search not found: {space}/{name}")

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
        kibana_note = ""
        if self.kibana_url:
            kibana_note = (
                "KIBANA KNOWLEDGE (dashboards & saved searches): tables named "
                "`dashboard::<space>/<title>` are the deployment's Kibana dashboards — "
                "the SAME dashboards operators stare at during a manual investigation. "
                "Each panel is a column whose description carries its query: "
                "`ES|QL:` / `query_string:` panels are DIRECTLY runnable; `lens` / "
                "`visualization` panels show the aggregation recipe (operations over an "
                "index pattern) to compose the equivalent DSL/ES|QL from. "
                "`saved_search::<space>/<title>` tables are Discover saved searches "
                "(runnable query_string). For an incident/RCA question PREFER these over "
                "writing queries from scratch — they encode the fields, indices, and "
                "thresholds the team actually uses. Envelopes: "
                '{"dashboard": "<space>/<title>"} -> panel inventory; '
                '{"dashboard": "<space>/<title>", "panel": "<title>", "earliest": "now-4h"} '
                "-> run one panel (query_string panels get the time window applied; ES|QL "
                "panels run as stored — edit the ES|QL text to change the window); "
                '{"saved_search": "<space>/<title>", "earliest": "now-4h"} -> run a saved '
                "search. Never paste `dashboard::...` into the `index` field."
            )
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

{kibana_note}

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
