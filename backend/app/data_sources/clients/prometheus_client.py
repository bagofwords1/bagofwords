from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter

import pandas as pd
import requests
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from app.data_sources.clients.progress import ProgressCallback


# Synthetic columns every metric "table" exposes on top of its label set.
_TIMESTAMP_COL = "timestamp"
_VALUE_COL = "value"

# Prometheus metric-type -> a short, human dtype for the value column, so the
# schema hints whether a metric is a counter (use rate()) vs a gauge.
_TYPE_HINT = {
    "counter": "counter (use rate/increase)",
    "gauge": "gauge",
    "histogram": "histogram (use histogram_quantile)",
    "summary": "summary",
    "untyped": "untyped",
    "unknown": "unknown",
}

# A histogram/summary is exposed as three *suffixed* series — `_bucket`, `_sum`
# and `_count` — but `/api/v1/metadata` only carries an entry under the BASE
# name (`http_request_duration_seconds`), which is itself never a series and so
# never appears in `/api/v1/label/__name__/values`. Looking metadata up by the
# exact series name therefore misses every component: on a stock Prometheus
# ~20% of metrics come back `unknown` and NOTHING is ever typed `histogram`.
# The suffix decides the component's own type: `_bucket` is the histogram body
# (needs histogram_quantile), while `_sum`/`_count` are plain counters (need
# rate/increase) — querying those raw returns meaningless monotonic totals.
_COMPONENT_SUFFIX_TYPE = {
    "_bucket": "histogram",
    "_sum": "counter",
    "_count": "counter",
}


class PrometheusClient(DataSourceClient):
    """Prometheus time-series client (PromQL over the HTTP API).

    Prometheus is not a SQL database: it stores labelled time series queried
    with **PromQL** over an HTTP API (``/api/v1/query``, ``/api/v1/query_range``,
    ``/api/v1/metadata``, ``/api/v1/label/__name__/values``, ``/api/v1/series``).
    We model each **metric name as a table** whose columns are the metric's
    label set plus synthetic ``timestamp`` and ``value`` columns, so the schema
    catalog and the agent's table-oriented planner work unchanged.

    ``execute_query`` takes a PromQL string. With no time window it runs an
    *instant* query; pass ``start``/``end`` (and optional ``step``) for a
    *range* query. Results — instant vectors or range matrices — are flattened
    into a tidy DataFrame: one column per label, plus ``timestamp`` and
    ``value``, one row per (series, sample).

    Auth mirrors the registry variants: none (network-gated), HTTP Basic
    (``username``/``password``), or a bearer ``token``. ``org_id`` is sent as
    the ``X-Scope-OrgID`` header for multi-tenant back-ends (Thanos, Cortex,
    Mimir).
    """

    # PromQL's own relative windows are the range selectors inside the query;
    # the instant/range boundary lives in execute_query's start/end args, which
    # are Python-side and so must be computed from `now` rather than frozen as
    # literals that go stale on the next dashboard refresh.
    relative_date_hint = (
        "Relative dates (PromQL): windows inside the query are relative already "
        "— rate(m[5m]), m offset 1h, avg_over_time(m[24h]). For a range query, "
        "compute execute_query's start/end from datetime.now(timezone.utc) "
        "(e.g. end - timedelta(hours=1)); never hard-code a date literal."
    )

    # Discovering label sets series-by-series is the slow part on large
    # instances, so metric names are matched in batches of this size per
    # /api/v1/series request.
    _SERIES_BATCH = 40

    # `/api/v1/series` with no start/end scans the FULL retention window and
    # returns one JSON object per matching series — on a churning instance
    # (pods, jobs, build ids) that is every series ever seen, not the live ones,
    # so the response can be orders of magnitude larger than the live label set
    # it is being read for. We only need which label KEYS a metric currently
    # carries, so discovery is bounded to a recent window. Overridable per
    # connection for an instance scraped less often than this.
    _DISCOVERY_LOOKBACK_S = 3600

    # Hard ceiling on series returned per discovery request, so one
    # high-cardinality metric cannot pin the response size. Prometheus caps
    # server-side (2.45+/3.x); older servers ignore it and stay bounded by the
    # lookback window alone.
    _SERIES_LIMIT = 5000

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        verify_ssl: bool = True,
        org_id: Optional[str] = None,
        metric_prefix: Optional[str] = None,
        discovery_lookback_hours: Optional[float] = None,
        timeout: int = 30,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.verify_ssl = verify_ssl
        self.org_id = org_id
        self.metric_prefix = (metric_prefix or "").strip() or None
        # Optional numeric config arrives from the connect form as a STRING —
        # and as "" when the admin leaves the field blank, which `requests`
        # rejects with "Timeout value connect was , but it must be an int,
        # float or None". Coerce both here so a blank field means "default".
        self.timeout = self._as_number(timeout, int, 30)
        hours = self._as_number(discovery_lookback_hours, float, 0)
        self.discovery_lookback_s = (
            int(hours * 3600) if hours and hours > 0 else self._DISCOVERY_LOOKBACK_S
        )

    @staticmethod
    def _as_number(value, cast, default):
        """Cast an optional config value, falling back on blank/garbage input."""
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    # ── HTTP plumbing ────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.org_id:
            headers["X-Scope-OrgID"] = self.org_id
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

    def _api(self, session: requests.Session, path: str, params=None, method: str = "GET") -> Any:
        """Call a Prometheus API endpoint and return its ``data`` payload.

        Prometheus wraps successful responses as ``{"status":"success","data":…}``
        and errors as ``{"status":"error","errorType":…,"error":…}`` — often with
        an HTTP 4xx/5xx and a JSON body. We surface the ``error`` message rather
        than a bare status code so the agent (and the Test-connection button) see
        the PromQL parse/exec error.

        ``method`` is POST for ``/query``, ``/query_range`` and ``/series`` (which
        form-encode potentially large query/match[] payloads, avoiding URL-length
        limits) and GET for the read-only discovery endpoints
        (``/label/.../values``, ``/metadata``, ``/status/buildinfo``), which
        reject POST with 405.
        """
        url = f"{self.base_url}{path}"
        if method == "POST":
            resp = session.post(url, data=params or {}, timeout=self.timeout)
        else:
            resp = session.get(url, params=params or {}, timeout=self.timeout)
        try:
            payload = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"Non-JSON response from {url}: {resp.text[:300]}")
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise RuntimeError(
                f"Prometheus API error ({payload.get('errorType', 'unknown')}): "
                f"{payload.get('error', resp.text[:300])}"
            )
        if resp.status_code >= 400:
            resp.raise_for_status()
        return payload.get("data") if isinstance(payload, dict) else payload

    # ── Discovery ────────────────────────────────────────────────────────────

    def _metric_names(self, session: requests.Session) -> List[str]:
        names = self._api(session, "/api/v1/label/__name__/values") or []
        if self.metric_prefix:
            names = [n for n in names if n.startswith(self.metric_prefix)]
        return sorted(names)

    def _metadata(self, session: requests.Session) -> Dict[str, dict]:
        """metric name -> {type, help, unit} (first entry wins per metric)."""
        raw = self._api(session, "/api/v1/metadata") or {}
        out: Dict[str, dict] = {}
        if isinstance(raw, dict):
            for name, entries in raw.items():
                if entries:
                    out[name] = entries[0]
        return out

    @staticmethod
    def _metadata_for(metadata: Dict[str, dict], name: str) -> dict:
        """Metadata for one series name, resolving histogram/summary components.

        Exact name first. Failing that, a `_bucket`/`_sum`/`_count` series is
        looked up under its base name and re-typed for what that component
        actually is (see _COMPONENT_SUFFIX_TYPE) — the base entry says
        `histogram`, but only `_bucket` is the histogram body; `_sum`/`_count`
        are counters. `help` and `unit` carry over from the base metric, which
        is exactly what they describe.
        """
        meta = metadata.get(name)
        if meta:
            return meta
        for suffix, component_type in _COMPONENT_SUFFIX_TYPE.items():
            if not name.endswith(suffix):
                continue
            base = metadata.get(name[: -len(suffix)])
            if not base:
                continue
            base_type = (base.get("type") or "").lower()
            # Only re-type when the base really is a histogram/summary; a plain
            # counter that happens to end in `_count` keeps its own type.
            if base_type not in ("histogram", "summary"):
                return base
            return {**base, "type": component_type}
        return {}

    def _labels_for(self, session: requests.Session, names: List[str]) -> Dict[str, set]:
        """Map each metric name to the set of label keys seen on its series.

        Uses ``/api/v1/series`` with one ``match[]=<metric>`` selector per name,
        batched to keep each request bounded. ``__name__`` is dropped from the
        label set (it is the metric/table name itself).

        Every request is bounded by ``start``/``end`` (a recent window) and a
        ``limit`` — without them Prometheus scans the whole retention period and
        materialises one object per series ever seen, which on a churning
        instance dwarfs the live label set this is actually reading.
        """
        labels: Dict[str, set] = {n: set() for n in names}
        now = int(time.time())
        window = [
            ("start", str(now - self.discovery_lookback_s)),
            ("end", str(now)),
            ("limit", str(self._SERIES_LIMIT)),
        ]
        for i in range(0, len(names), self._SERIES_BATCH):
            batch = names[i : i + self._SERIES_BATCH]
            series = self._api(
                session, "/api/v1/series",
                params=[("match[]", n) for n in batch] + window, method="POST",
            ) or []
            for s in series:
                name = s.get("__name__")
                if name is None:
                    continue
                bucket = labels.setdefault(name, set())
                for key in s.keys():
                    if key != "__name__":
                        bucket.add(key)
        return labels

    def get_tables(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """Build one Table per metric: columns = labels + timestamp + value."""
        with self.connect() as session:
            names = self._metric_names(session)
            metadata = self._metadata(session)

            # Label discovery, batched, with progress reporting.
            labels: Dict[str, set] = {n: set() for n in names}
            total = len(names)
            for i in range(0, total, self._SERIES_BATCH):
                batch = names[i : i + self._SERIES_BATCH]
                batch_labels = self._labels_for(session, batch)
                for k, v in batch_labels.items():
                    labels.setdefault(k, set()).update(v)
                if progress_callback:
                    done = min(i + self._SERIES_BATCH, total)
                    try:
                        progress_callback(done, total, f"Indexed {done}/{total} metrics")
                    except Exception:
                        pass

            tables: List[Table] = []
            for name in names:
                meta = self._metadata_for(metadata, name)
                mtype = (meta.get("type") or "unknown").lower()
                help_text = meta.get("help") or None
                unit = meta.get("unit") or None

                columns = [
                    TableColumn(name=lbl, dtype="label")
                    for lbl in sorted(labels.get(name, set()))
                ]
                columns.append(TableColumn(name=_TIMESTAMP_COL, dtype="datetime"))
                columns.append(
                    TableColumn(name=_VALUE_COL, dtype=_TYPE_HINT.get(mtype, mtype))
                )
                tables.append(
                    Table(
                        name=name,
                        description=help_text,
                        columns=columns,
                        pks=[],
                        fks=[],
                        metadata_json={
                            "metric_type": mtype,
                            "unit": unit,
                            "source": "prometheus",
                        },
                    )
                )
            return tables

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None):
        return self.get_tables(progress_callback=progress_callback)

    def get_schema(self, table_name: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_schemas() instead.")

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    # ── Querying ─────────────────────────────────────────────────────────────

    @staticmethod
    def _flatten(result: dict) -> pd.DataFrame:
        """Flatten a Prometheus query result into a tidy DataFrame.

        Handles all four result types:
          - ``vector``  (instant): one sample per series -> one row per series
          - ``matrix``  (range):   many samples per series -> one row per sample
          - ``scalar``  / ``string``: a single value -> one row
        Columns: every label key seen (sorted), then ``timestamp`` + ``value``.
        """
        rtype = result.get("resultType")
        payload = result.get("result")
        rows: List[dict] = []

        if rtype == "vector":
            for item in payload or []:
                metric = item.get("metric", {})
                ts, val = item.get("value", [None, None])
                rows.append({**metric, _TIMESTAMP_COL: ts, _VALUE_COL: val})
        elif rtype == "matrix":
            for item in payload or []:
                metric = item.get("metric", {})
                for ts, val in item.get("values", []) or []:
                    rows.append({**metric, _TIMESTAMP_COL: ts, _VALUE_COL: val})
        elif rtype in ("scalar", "string"):
            ts, val = payload if isinstance(payload, list) else (None, payload)
            rows.append({_TIMESTAMP_COL: ts, _VALUE_COL: val})
        else:
            raise RuntimeError(f"Unexpected Prometheus resultType: {rtype!r}")

        label_keys = sorted(
            {k for r in rows for k in r.keys() if k not in (_TIMESTAMP_COL, _VALUE_COL)}
        )
        columns = label_keys + [_TIMESTAMP_COL, _VALUE_COL]
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            # Prometheus timestamps are float unix seconds; values are strings.
            df[_TIMESTAMP_COL] = pd.to_datetime(
                pd.to_numeric(df[_TIMESTAMP_COL], errors="coerce"), unit="s"
            )
            df[_VALUE_COL] = pd.to_numeric(df[_VALUE_COL], errors="coerce")
        return df

    def execute_query(
        self,
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        step: Optional[str] = None,
        time: Optional[str] = None,
    ) -> pd.DataFrame:
        """Run a PromQL query and return a DataFrame.

        Instant query (default): ``execute_query("up")`` ->
        ``/api/v1/query``. Optional ``time`` (RFC3339 or unix seconds) evaluates
        at a past instant.

        Range query: ``execute_query("rate(http_requests_total[5m])",
        start="2024-01-01T00:00:00Z", end="2024-01-01T01:00:00Z", step="60s")``
        -> ``/api/v1/query_range``. ``step`` defaults to ``60s``.
        """
        with self.connect() as session:
            if start is not None and end is not None:
                data = self._api(
                    session,
                    "/api/v1/query_range",
                    params={
                        "query": query,
                        "start": start,
                        "end": end,
                        "step": step or "60s",
                    },
                    method="POST",
                )
            else:
                params = {"query": query}
                if time is not None:
                    params["time"] = time
                data = self._api(session, "/api/v1/query", params=params, method="POST")
            return self._flatten(data or {})

    # ── Connection test ──────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        try:
            with self.connect() as session:
                # A trivial PromQL eval exercises auth + the query engine (more
                # meaningful than /-/healthy, which some proxies leave open).
                self._api(session, "/api/v1/query", params={"query": "1"})
                version = None
                try:
                    info = self._api(session, "/api/v1/status/buildinfo") or {}
                    version = info.get("version")
                except Exception:
                    pass
                msg = "Successfully connected to Prometheus"
                if version:
                    msg += f" {version}"
                return {"success": True, "message": msg}
        except requests.exceptions.RequestException as e:
            return {"success": False, "message": f"Connection error: {e}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── Agent-facing description / system prompt ─────────────────────────────

    def system_prompt(self) -> str:
        return """
## Prometheus (PromQL) Integration

This connector queries a Prometheus time-series database using **PromQL** (NOT
SQL). Each "table" in the schema is a metric name; its columns are the metric's
labels plus synthetic `timestamp` and `value` columns.

### How to query
Call `execute_query` with a PromQL string.

```python
# Instant query — current value of every series of a metric
df = client.execute_query('up')

# Instant query with a filter on labels
df = client.execute_query('up{job="prometheus"}')

# Counters must be wrapped in rate()/increase() over a range selector
df = client.execute_query('sum by (job) (rate(prometheus_http_requests_total[5m]))')

# Histograms: use histogram_quantile over the _bucket series
df = client.execute_query('histogram_quantile(0.95, sum by (le) (rate(prometheus_http_request_duration_seconds_bucket[5m])))')

# Range query — a time series over a window (returns one row per sample).
# Compute start/end RELATIVE to now: this code is re-executed on dashboard
# refreshes and scheduled runs, and a hard-coded date would pin every future
# run to the same stale window.
from datetime import datetime, timedelta, timezone
end = datetime.now(timezone.utc)
start = end - timedelta(hours=1)
df = client.execute_query(
    'sum(rate(prometheus_http_requests_total[5m]))',
    start=start.isoformat(), end=end.isoformat(), step='60s')
```

### Alerts
Firing/pending alerts are queryable via the synthetic `ALERTS` series:

```python
df = client.execute_query('ALERTS{alertstate="firing"}')
df = client.execute_query('count by (alertname) (ALERTS{alertstate="firing"})')
```

### Rules of thumb
- PromQL, not SQL: no SELECT/FROM/JOIN. Filter with `{label="value"}` selectors.
- `counter` metrics only make sense wrapped in `rate()`/`increase()[window]`.
- `gauge` metrics can be read directly.
- Aggregate with `sum`/`avg`/`max`/`min`/`count` + `by (label)` / `without (label)`.
- The result DataFrame has one column per label plus `timestamp` and `value`.
"""

    @property
    def description(self) -> str:
        head = f"Prometheus time-series database at {self.base_url}."
        if self.metric_prefix:
            head += f" Metric discovery is filtered to names starting with '{self.metric_prefix}'."
        return head + "\n\n" + self.system_prompt()
