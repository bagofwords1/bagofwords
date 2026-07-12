from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter

import json
import pandas as pd
import requests
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Union

from app.data_sources.clients.progress import ProgressCallback


# The fixed, virtual tables this connector exposes. Jaeger has no query
# language — it is a parameterized trace search — so instead of discovering
# tables we present a stable schema that mirrors the Query API surface.
_SERVICES_TABLE = "services"
_OPERATIONS_TABLE = "operations"
_SPANS_TABLE = "spans"
_DEPENDENCIES_TABLE = "dependencies"

# Core columns every flattened span row carries, on top of the span's own
# (dynamic) tag set. Ordered so the tidy DataFrame reads left-to-right from
# identity → topology → timing → status.
_SPAN_CORE_COLUMNS = [
    ("trace_id", "string"),
    ("span_id", "string"),
    ("parent_span_id", "string"),
    ("service", "string"),
    ("operation", "string"),
    ("start_time", "datetime"),
    ("duration_ms", "float"),
    ("status_code", "string"),
    ("error", "bool"),
]

# Tag keys we promote into the fixed `status_code` column (first hit wins).
_STATUS_TAG_KEYS = ("http.status_code", "otel.status_code", "rpc.grpc.status_code")


class JaegerClient(DataSourceClient):
    """Jaeger distributed-tracing client (over the Query JSON HTTP API).

    Jaeger is not a SQL database and has no query language: it stores spans
    grouped into traces, searched via a parameterized HTTP API
    (``/api/services``, ``/api/services/{svc}/operations``, ``/api/traces``,
    ``/api/dependencies``). We present a **fixed set of virtual tables** so the
    schema catalog and the agent's table-oriented planner work unchanged:

      - ``services``     — one row per instrumented service
      - ``operations``   — service × operation names
      - ``spans``        — the analytical workhorse: one row per span, with
                           core identity/timing columns plus a column per tag
      - ``dependencies`` — the service dependency graph (parent → child counts)

    ``execute_query`` takes a **JSON object** (string or dict) of search
    parameters rather than a query string, e.g.::

        {"table": "spans", "service": "frontend", "operation": "HTTP GET",
         "tags": {"error": "true"}, "lookback": "1h", "limit": 20,
         "min_duration": "100ms"}

    It routes to the matching endpoint and flattens the response into a tidy
    DataFrame. ``table`` defaults to ``spans``; pass ``trace_id`` to fetch a
    single trace by id.

    Auth mirrors the registry variants: none (network-gated / behind a proxy),
    HTTP Basic (``username``/``password``), or a bearer ``token``.
    """

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        verify_ssl: bool = True,
        default_lookback: str = "1h",
        default_limit: int = 20,
        timeout: int = 30,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.verify_ssl = verify_ssl
        self.default_lookback = (default_lookback or "1h").strip() or "1h"
        try:
            self.default_limit = int(default_limit)
        except (TypeError, ValueError):
            self.default_limit = 20
        self.timeout = timeout

    # ── HTTP plumbing ────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _auth(self):
        # Basic auth only when a token is not supplied (they are mutually
        # exclusive; token takes precedence).
        if not self.token and self.username:
            return (self.username, self.password or "")
        return None

    @contextmanager
    def connect(self):
        session = requests.Session()
        session.headers.update(self._headers())
        auth = self._auth()
        if auth is not None:
            session.auth = auth
        session.verify = self.verify_ssl
        try:
            yield session
        finally:
            session.close()

    def _api(self, session: requests.Session, path: str, params=None) -> Any:
        """Call a Jaeger Query API endpoint and return its ``data`` payload.

        Jaeger wraps successful responses as
        ``{"data": [...], "total": N, "errors": null}`` and failures as
        ``{"data": null, "errors": [{"code": ..., "msg": "..."}]}`` — often with
        an HTTP 4xx/5xx and a JSON body. We surface the ``errors`` message
        rather than a bare status code so the agent (and the Test-connection
        button) see the actual Jaeger error.
        """
        url = f"{self.base_url}{path}"
        resp = session.get(url, params=params or {}, timeout=self.timeout)
        try:
            payload = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"Non-JSON response from {url}: {resp.text[:300]}")
        if isinstance(payload, dict) and payload.get("errors"):
            errs = payload["errors"]
            msg = "; ".join(
                str(e.get("msg", e)) if isinstance(e, dict) else str(e) for e in errs
            )
            raise RuntimeError(f"Jaeger API error: {msg}")
        if resp.status_code >= 400:
            resp.raise_for_status()
        if isinstance(payload, dict):
            return payload.get("data")
        return payload

    # ── Discovery ────────────────────────────────────────────────────────────

    def _services(self, session: requests.Session) -> List[str]:
        data = self._api(session, "/api/services") or []
        return sorted([s for s in data if s])

    def _operations(self, session: requests.Session, service: str) -> List[str]:
        # /api/services/{service}/operations returns either a list of strings
        # (older Jaeger) or a list of {name, spanKind} objects (newer). Handle
        # both so the connector spans versions.
        data = self._api(session, f"/api/services/{service}/operations") or []
        out: List[str] = []
        for item in data:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    out.append(name)
            elif item:
                out.append(item)
        return sorted(set(out))

    def get_tables(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """Return the fixed virtual-table catalog.

        The tables themselves are static; we only reach the API to enrich the
        `services`/`operations` descriptions with the live service list, which
        also doubles as a cheap connectivity check during indexing.
        """
        services: List[str] = []
        try:
            with self.connect() as session:
                services = self._services(session)
        except Exception:
            # Discovery is best-effort — the schema is fixed regardless, and a
            # transient API hiccup shouldn't blank the catalog.
            services = []

        if progress_callback:
            try:
                progress_callback(1, 1, f"Discovered {len(services)} services")
            except Exception:
                pass

        svc_hint = ""
        if services:
            preview = ", ".join(services[:12])
            more = "" if len(services) <= 12 else f", … (+{len(services) - 12} more)"
            svc_hint = f" Known services: {preview}{more}."

        tables: List[Table] = [
            Table(
                name=_SERVICES_TABLE,
                description="Instrumented services reporting traces to Jaeger." + svc_hint,
                columns=[TableColumn(name="service", dtype="string")],
                pks=[], fks=[],
                metadata_json={"source": "jaeger", "endpoint": "/api/services"},
            ),
            Table(
                name=_OPERATIONS_TABLE,
                description=(
                    "Operation (span) names per service. Query with "
                    '{"table": "operations", "service": "<name>"}.'
                ),
                columns=[
                    TableColumn(name="service", dtype="string"),
                    TableColumn(name="operation", dtype="string"),
                ],
                pks=[], fks=[],
                metadata_json={"source": "jaeger", "endpoint": "/api/services/{service}/operations"},
            ),
            Table(
                name=_SPANS_TABLE,
                description=(
                    "Individual spans from matching traces (the main analytical "
                    "table). Core columns below; every span tag also appears as "
                    "its own column. Search with service/operation/tags/lookback/"
                    "min_duration/limit." + svc_hint
                ),
                columns=[TableColumn(name=n, dtype=d) for n, d in _SPAN_CORE_COLUMNS],
                pks=[], fks=[],
                metadata_json={"source": "jaeger", "endpoint": "/api/traces"},
            ),
            Table(
                name=_DEPENDENCIES_TABLE,
                description=(
                    "Service dependency graph over a time window — one row per "
                    "caller→callee edge with the observed call count."
                ),
                columns=[
                    TableColumn(name="parent", dtype="string"),
                    TableColumn(name="child", dtype="string"),
                    TableColumn(name="call_count", dtype="int"),
                ],
                pks=[], fks=[],
                metadata_json={"source": "jaeger", "endpoint": "/api/dependencies"},
            ),
        ]
        return tables

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None):
        return self.get_tables(progress_callback=progress_callback)

    def get_schema(self, table_name: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_schemas() instead.")

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    # ── Querying ─────────────────────────────────────────────────────────────

    @staticmethod
    def _coerce_query(query: Union[str, dict, None], kwargs: dict) -> dict:
        """Normalise the query argument into a params dict.

        Accepts a JSON string, a dict, or ``None`` (params supplied purely via
        kwargs). Model-generated code reaches for all three, so we accept all
        three rather than failing on the shape.
        """
        params: Dict[str, Any] = {}
        if isinstance(query, dict):
            params.update(query)
        elif isinstance(query, str) and query.strip():
            try:
                parsed = json.loads(query)
            except (ValueError, TypeError):
                raise RuntimeError(
                    "Jaeger execute_query expects a JSON object of search params "
                    '(e.g. {"table": "spans", "service": "frontend", '
                    '"lookback": "1h"}), not a query string.'
                )
            if not isinstance(parsed, dict):
                raise RuntimeError("Jaeger query JSON must decode to an object.")
            params.update(parsed)
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return params

    def execute_query(self, query: Union[str, dict, None] = None, **kwargs) -> pd.DataFrame:
        """Run a Jaeger search described by a JSON params object.

        Routing on the ``table`` key (default ``spans``):
          - ``services``     → ``/api/services``
          - ``operations``   → ``/api/services/{service}/operations`` (needs ``service``)
          - ``dependencies`` → ``/api/dependencies`` (window via ``lookback``)
          - ``spans``        → ``/api/traces`` search, or ``/api/traces/{id}``
                               when ``trace_id`` is given

        Span search keys: ``service``, ``operation``, ``tags`` (dict or Jaeger
        logfmt string), ``lookback`` (e.g. ``"1h"``, ``"2d"``), ``start``/``end``
        (unix microseconds), ``min_duration``/``max_duration`` (e.g. ``"100ms"``),
        ``limit``.
        """
        params = self._coerce_query(query, kwargs)
        table = (params.get("table") or _SPANS_TABLE).lower()

        with self.connect() as session:
            if table == _SERVICES_TABLE:
                return pd.DataFrame({"service": self._services(session)})

            if table == _OPERATIONS_TABLE:
                service = params.get("service")
                if not service:
                    raise RuntimeError('The "operations" table requires a "service".')
                ops = self._operations(session, service)
                return pd.DataFrame({"service": [service] * len(ops), "operation": ops})

            if table == _DEPENDENCIES_TABLE:
                return self._query_dependencies(session, params)

            # Default: spans.
            trace_id = params.get("trace_id") or params.get("traceID")
            if trace_id:
                data = self._api(session, f"/api/traces/{trace_id}") or []
            else:
                data = self._api(session, "/api/traces", params=self._trace_search_params(params)) or []
            return self._flatten_traces(data)

    def _trace_search_params(self, params: dict) -> List[tuple]:
        """Translate our param dict into Jaeger ``/api/traces`` query params."""
        out: List[tuple] = []
        service = params.get("service")
        if not service:
            raise RuntimeError(
                'Span search requires a "service" (or a "trace_id"). '
                'Use {"table": "services"} to list them.'
            )
        out.append(("service", service))
        if params.get("operation"):
            out.append(("operation", params["operation"]))

        # Tags: accept a dict (preferred) or a raw Jaeger logfmt/JSON string.
        tags = params.get("tags")
        if isinstance(tags, dict) and tags:
            out.append(("tags", json.dumps({k: str(v) for k, v in tags.items()})))
        elif isinstance(tags, str) and tags.strip():
            out.append(("tags", tags))

        # Time window: explicit start/end (unix micros) win; else lookback.
        if params.get("start") is not None and params.get("end") is not None:
            out.append(("start", str(params["start"])))
            out.append(("end", str(params["end"])))
        else:
            out.append(("lookback", str(params.get("lookback") or self.default_lookback)))

        for key, api_key in (("min_duration", "minDuration"), ("max_duration", "maxDuration")):
            if params.get(key):
                out.append((api_key, str(params[key])))

        limit = params.get("limit", self.default_limit)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = self.default_limit
        out.append(("limit", str(limit)))
        return out

    def _query_dependencies(self, session: requests.Session, params: dict) -> pd.DataFrame:
        # Jaeger's dependencies endpoint takes endTs (ms) + lookback (ms). We
        # accept a friendly `end` (unix ms) and `lookback_ms`, defaulting to a
        # 24h window ending "now" is impossible without a clock — so require an
        # explicit endTs, else pass through whatever the caller provides.
        q: Dict[str, Any] = {}
        if params.get("end_ts") is not None:
            q["endTs"] = str(params["end_ts"])
        elif params.get("endTs") is not None:
            q["endTs"] = str(params["endTs"])
        lookback_ms = params.get("lookback_ms") or params.get("lookback")
        if lookback_ms is not None:
            q["lookback"] = str(lookback_ms)
        data = self._api(session, "/api/dependencies", params=q) or []
        rows = [
            {
                "parent": d.get("parent"),
                "child": d.get("child"),
                "call_count": d.get("callCount"),
            }
            for d in data
        ]
        return pd.DataFrame(rows, columns=["parent", "child", "call_count"])

    @staticmethod
    def _flatten_traces(traces: List[dict]) -> pd.DataFrame:
        """Flatten Jaeger traces into a tidy one-row-per-span DataFrame.

        Each trace carries ``spans`` and a ``processes`` map (processID →
        {serviceName, tags}). We resolve every span's service via its
        processID, convert Jaeger's microsecond ``startTime``/``duration`` to
        datetime/milliseconds, promote a couple of common tags into fixed
        columns (``status_code``, ``error``), and spread the remaining tags out
        as their own columns (union across all rows), mirroring how the
        Prometheus connector spreads labels.
        """
        core_names = [n for n, _ in _SPAN_CORE_COLUMNS]
        rows: List[dict] = []

        for trace in traces or []:
            processes = trace.get("processes", {}) or {}
            for span in trace.get("spans", []) or []:
                tags = _tags_to_dict(span.get("tags", []))

                parent = None
                for ref in span.get("references", []) or []:
                    if ref.get("refType") in ("CHILD_OF", "FOLLOWS_FROM"):
                        parent = ref.get("spanID")
                        break

                proc = processes.get(span.get("processID"), {}) or {}
                start_us = span.get("startTime")
                duration_us = span.get("duration")

                # Keep status as a string so a numeric HTTP code (e.g. 500)
                # doesn't upcast the whole column to float once null rows
                # (spans without the tag) are mixed in — and so gRPC/otel
                # string codes ("OK"/"ERROR") sit in the same column cleanly.
                status_code = None
                for k in _STATUS_TAG_KEYS:
                    if k in tags:
                        status_code = str(tags[k])
                        break

                error_tag = tags.get("error")
                is_error = error_tag in (True, "true", "True", 1, "1")

                core = {
                    "trace_id": span.get("traceID"),
                    "span_id": span.get("spanID"),
                    "parent_span_id": parent,
                    "service": proc.get("serviceName"),
                    "operation": span.get("operationName"),
                    "start_time": start_us,
                    "duration_ms": (duration_us / 1000.0) if duration_us is not None else None,
                    "status_code": status_code,
                    "error": is_error,
                }
                # Tags first so core columns always take precedence on collision.
                rows.append({**tags, **core})

        tag_keys = sorted(
            {k for r in rows for k in r.keys() if k not in core_names}
        )
        columns = core_names + tag_keys
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            # Jaeger startTime is unix microseconds.
            df["start_time"] = pd.to_datetime(
                pd.to_numeric(df["start_time"], errors="coerce"), unit="us"
            )
            df["duration_ms"] = pd.to_numeric(df["duration_ms"], errors="coerce")
        return df

    # ── Connection test ──────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        try:
            with self.connect() as session:
                # /api/services exercises auth + the query path and works on an
                # empty backend (returns []), unlike a trace search that needs a
                # known service.
                services = self._services(session)
                n = len(services)
                return {
                    "success": True,
                    "message": f"Successfully connected to Jaeger ({n} service{'s' if n != 1 else ''} found)",
                }
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Connection error: {e}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── Agent-facing description / system prompt ─────────────────────────────

    def system_prompt(self) -> str:
        return """
## Jaeger (distributed tracing) Integration

This connector queries a Jaeger backend through its Query JSON API. Jaeger has
**no query language** — you search traces by parameters. The schema exposes four
virtual tables: `services`, `operations`, `spans`, and `dependencies`.

### How to query
Call `execute_query` with a **JSON object** of search parameters (a dict or a
JSON string). The `table` key selects what you get back (default: `spans`).

```python
# 1. List instrumented services
df = client.execute_query({"table": "services"})

# 2. List operations for a service
df = client.execute_query({"table": "operations", "service": "frontend"})

# 3. Search spans (the main analytical table) — one row per span
df = client.execute_query({
    "table": "spans",
    "service": "frontend",
    "operation": "HTTP GET /cart",   # optional
    "tags": {"error": "true"},        # optional; matches span tags
    "lookback": "1h",                 # or start/end in unix microseconds
    "min_duration": "100ms",          # optional; also max_duration
    "limit": 20,
})

# 4. Fetch a single trace by id
df = client.execute_query({"trace_id": "abc123..."})

# 5. Service dependency graph (parent -> child call counts)
df = client.execute_query({"table": "dependencies", "endTs": 1719792000000, "lookback": 3600000})
```

### The `spans` result
Core columns: `trace_id`, `span_id`, `parent_span_id`, `service`, `operation`,
`start_time` (datetime), `duration_ms` (float), `status_code`, `error` (bool).
Every span tag also appears as its own column (e.g. `http.method`,
`http.url`, `otel.status_code`).

### Rules of thumb
- Span search **requires** a `service` (or a `trace_id`). Call
  `{"table": "services"}` first if you don't know the name.
- Latency questions: sort/filter by `duration_ms`; errors: `error == True` or a
  `status_code` >= 400 / rpc status.
- `tags` is an AND-match over span tags; pass a dict like `{"http.status_code": "500"}`.
- Time defaults to the last `lookback` window; widen it (e.g. `"6h"`, `"2d"`)
  if a search returns nothing.
- `dependencies` needs an `endTs` (unix **milliseconds**) and `lookback`
  (milliseconds) — it reads a precomputed graph, not live spans.
"""

    @property
    def description(self) -> str:
        return (
            f"Jaeger distributed-tracing backend at {self.base_url}.\n\n"
            + self.system_prompt()
        )


def _tags_to_dict(tags: List[dict]) -> Dict[str, Any]:
    """Convert Jaeger's ``[{key, type, value}, ...]`` tag list to ``{key: value}``.

    Later duplicates win (matches how most viewers render repeated keys). Values
    keep their JSON-decoded Python type (str/int/float/bool).
    """
    out: Dict[str, Any] = {}
    for t in tags or []:
        key = t.get("key")
        if key is not None:
            out[key] = t.get("value")
    return out
