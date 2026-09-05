"""AWS CloudWatch data source client (Logs Insights + Metrics).

CloudWatch is two products behind one brand, and this client catalogs both
under a single connection because an investigation crosses them: the Lambda
that is erroring in **Logs** is the one whose Duration/Throttles live in
**Metrics**. Splitting them into two connection types would put them on
separate connections with no way for the planner to correlate.

Tables are therefore namespaced by prefix, the way the Splunk client
namespaces `index::sourcetype` alongside `dashboard::`/`saved_search::`:

    log_group::/aws/lambda/checkout   columns: @timestamp, @message, @logStream
                                               + JSON keys sampled from events
    metric::AWS/EC2/CPUUtilization    columns: <dimension names> + timestamp + value

**Logs discovery is schema-on-read.** CloudWatch has no field catalog, so
columns for a log group come from sampling recent events and parsing JSON out
of `@message`. Only the top-K groups by stored bytes are sampled (ranked, then
capped); the rest stay *thin* — they carry the `@`-builtins only and the agent
discovers fields on demand with a `| limit 5` peek. That is safe because an
unknown field in Insights matches nothing rather than erroring.

**Metrics discovery is a catalog read.** `list_metrics` returns one entry per
(metric, dimension-set) combination — `AWS/RDS/VolumeReadIOPs` comes back
separately for `[DBClusterIdentifier]`, `[DbClusterIdentifier, EngineName]`
and `[]` — so entries are grouped by (namespace, metric name) and their
dimension names unioned into one table's columns.

**Cost.** Logs Insights bills on bytes *scanned*, and a `limit` clause does not
reduce the scan: a `| limit 8` over three log groups measured here scanned
126 MB / 31,620 records. The time window and the log-group selection are the
only real cost controls, which is why the window is bounded by config, why
queries default to a short lookback rather than the discovery window, and why
`system_prompt()` tells the agent to filter before it aggregates.

Auth mirrors the S3/Athena idiom so boto3 session construction is familiar:
static keys, keys + STS assume-role, or boto3's default chain (env vars,
instance profile, EKS IRSA).
"""
import json
import logging
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from app.ai.prompt_formatters import ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.progress import ProgressCallback

logger = logging.getLogger(__name__)

LOG_PREFIX = "log_group::"
METRIC_PREFIX = "metric::"

# CloudWatch API limits (hard, server-side).
MAX_LOG_GROUPS_PER_QUERY = 50   # StartQuery rejects more: "Too many log groups specified"
MAX_INSIGHTS_ROWS = 10_000      # GetQueryResults never returns more than this
MAX_METRIC_QUERIES = 500        # MetricDataQueries per GetMetricData call

# Client-side defaults.
DEFAULT_ROW_LIMIT = 1_000
DEFAULT_QUERY_WINDOW = "-1h"    # when a query omits start/end — deliberately short (cost)
DEFAULT_WINDOW_SECONDS = 3600
QUERY_TIMEOUT_SECONDS = 180     # give up on an Insights query after this
POLL_INITIAL = 0.5
POLL_MAX = 3.0
SAMPLE_EVENTS = 50              # events sampled per log group for field discovery

# `@ptr` is an opaque pagination cursor Insights adds to every row. It is noise
# in a DataFrame and blows up token cost in previews, so it is dropped unless
# explicitly selected.
_NOISE_FIELDS = frozenset({"@ptr"})

_BUILTIN_LOG_COLUMNS = (
    ("@timestamp", "datetime", "Event timestamp (UTC), set by CloudWatch on ingestion."),
    ("@message", "string", "The raw log line. JSON payloads are also exposed as parsed columns."),
    ("@logStream", "string", "Log stream the event came from."),
    ("@ingestionTime", "datetime", "When CloudWatch received the event."),
)

# The literal format Insights emits for a `bin(...)` bucket column.
_INSIGHTS_BIN_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

_RELATIVE_RE = re.compile(r"^-(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class AwsCloudWatchClient(DataSourceClient):
    """CloudWatch Logs Insights + Metrics as a table-shaped data source."""

    capabilities = {Capability.QUERY}

    relative_date_hint = (
        "Relative dates (CloudWatch): pass `start`/`end` in the query envelope as "
        "relative offsets ('-1h', '-24h', '-7d') or ISO-8601 UTC timestamps — never "
        "bake absolute dates into a saved query. Omitted, the window defaults to "
        f"'{DEFAULT_QUERY_WINDOW}'. Inside a Logs Insights string, `@timestamp` is UTC."
    )

    def __init__(
        self,
        region: str,
        log_group_prefix: Optional[str] = None,
        metric_namespaces: Optional[str] = None,
        discovery_window_hours: int = 24,
        max_sampled_log_groups: int = 25,
        # Credentials — the flat union of every auth variant, because
        # Connection.get_client() splats config + decrypted credentials together.
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        session_token: Optional[str] = None,
        role_arn: Optional[str] = None,
        **kwargs: Any,
    ):
        self.region = region
        self.log_group_prefix = (log_group_prefix or "").strip() or None
        self.metric_namespaces = [
            ns.strip() for ns in (metric_namespaces or "").split(",") if ns.strip()
        ]
        self.discovery_window_hours = int(discovery_window_hours or 24)
        self.max_sampled_log_groups = int(max_sampled_log_groups or 0)
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.role_arn = role_arn

    # ── boto3 session ────────────────────────────────────────────────────────

    def _session(self):
        """Build a boto3 Session from whichever auth variant was configured.

        Mirrors `s3_client._build_session`: assume-role first (optionally using
        static keys to make the STS call), then static keys, then the default
        chain.
        """
        import boto3

        if self.role_arn:
            sts_kwargs: Dict[str, Any] = {"region_name": self.region}
            if self.access_key and self.secret_key:
                sts_kwargs["aws_access_key_id"] = self.access_key
                sts_kwargs["aws_secret_access_key"] = self.secret_key
                if self.session_token:
                    sts_kwargs["aws_session_token"] = self.session_token
            sts = boto3.client("sts", **sts_kwargs)
            creds = sts.assume_role(
                RoleArn=self.role_arn, RoleSessionName="bow-cloudwatch"
            )["Credentials"]
            return boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
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

    @contextmanager
    def connect(self):
        """Yield a (logs, cloudwatch) client pair.

        Only *session construction* is wrapped — the `yield` sits outside the
        try so that a query error raised by the caller keeps its own type and
        message instead of being relabelled as a connection failure.
        """
        try:
            session = self._session()
            clients = (session.client("logs"), session.client("cloudwatch"))
        except Exception as e:
            raise RuntimeError(f"Error connecting to AWS CloudWatch ({self.region}): {e}")
        yield clients

    # ── time helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _to_epoch(value: Any, default: int) -> int:
        """Normalize '-24h' / ISO-8601 / epoch seconds to epoch seconds.

        The Insights and GetMetricData APIs take absolute times, so relative
        offsets must be resolved client-side at *execution* time — resolving
        them once and storing them would freeze a dashboard's window.
        """
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip()
        if not s or s.lower() == "now":
            return int(time.time())
        m = _RELATIVE_RE.match(s)
        if m:
            return int(time.time()) - int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
        try:
            ts = pd.Timestamp(s)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            return int(ts.timestamp())
        except Exception:
            raise ValueError(
                f"Unparseable time {value!r}. Use a relative offset ('-1h', '-7d'), "
                "'now', an ISO-8601 timestamp, or epoch seconds."
            )

    def _window(self, spec: Dict[str, Any]) -> Tuple[int, int]:
        end = self._to_epoch(spec.get("end"), int(time.time()))
        start = self._to_epoch(spec.get("start"), end - DEFAULT_WINDOW_SECONDS)
        if start >= end:
            raise ValueError(f"Query window is empty: start ({start}) >= end ({end}).")
        return start, end

    # ── discovery ────────────────────────────────────────────────────────────

    def _list_log_groups(self, logs) -> List[dict]:
        groups: List[dict] = []
        kwargs: Dict[str, Any] = {}
        if self.log_group_prefix:
            # Filter server-side rather than listing the account and discarding.
            kwargs["logGroupNamePrefix"] = self.log_group_prefix
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate(**kwargs):
            groups.extend(page.get("logGroups", []))
        return groups

    def _sample_fields(self, logs, group_name: str, window_start: int, window_end: int) -> Dict[str, str]:
        """Sample recent events from one log group to infer columns.

        Returns {column: dtype}. JSON objects in `@message` contribute their
        top-level keys; anything else contributes nothing beyond the builtins.
        Best-effort — a failure degrades this group to thin, never fails
        discovery.
        """
        fields: Dict[str, str] = {}
        try:
            rows = self._run_insights(
                logs,
                query=f"fields @timestamp, @message | limit {SAMPLE_EVENTS}",
                log_groups=[group_name],
                start=window_start,
                end=window_end,
            )
        except Exception as e:
            logger.info("CloudWatch: field sampling skipped for %s: %s", group_name, e)
            return fields

        for row in rows:
            raw = row.get("@message")
            if not raw:
                continue
            text = raw.strip()
            if not (text.startswith("{") and text.endswith("}")):
                continue
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            for key, value in payload.items():
                if key in fields:
                    continue
                if isinstance(value, bool):
                    fields[key] = "boolean"
                elif isinstance(value, (int, float)):
                    fields[key] = "number"
                elif isinstance(value, (dict, list)):
                    fields[key] = "json"
                else:
                    fields[key] = "string"
        return fields

    def _log_group_tables(
        self, logs, progress_callback: Optional[ProgressCallback] = None
    ) -> List[Table]:
        groups = self._list_log_groups(logs)
        if not groups:
            return []

        now = int(time.time())
        window_start = now - self.discovery_window_hours * 3600

        # Sample the biggest groups first — they are the ones an operator asks
        # about, and the cap has to fall somewhere.
        ranked = sorted(groups, key=lambda g: g.get("storedBytes") or 0, reverse=True)
        sampled = {g["logGroupName"] for g in ranked[: self.max_sampled_log_groups]}

        tables: List[Table] = []
        total = len(ranked)
        for i, group in enumerate(ranked):
            name = group["logGroupName"]
            columns = [
                TableColumn(name=c, dtype=d, description=desc)
                for c, d, desc in _BUILTIN_LOG_COLUMNS
            ]
            parsed: Dict[str, str] = {}
            if name in sampled:
                parsed = self._sample_fields(logs, name, window_start, now)
                for field, dtype in sorted(parsed.items()):
                    columns.append(
                        TableColumn(
                            name=field,
                            dtype=dtype,
                            description="Parsed from the JSON body of @message.",
                        )
                    )

            retention = group.get("retentionInDays")
            tables.append(
                Table(
                    name=f"{LOG_PREFIX}{name}",
                    description=(
                        f"CloudWatch log group. Stored bytes: {group.get('storedBytes', 0):,}. "
                        + (f"Retention: {retention} days. " if retention else "Retention: never expires. ")
                        + (
                            "Columns beyond the @-builtins were sampled from recent events."
                            if name in sampled
                            else "Not field-sampled (beyond the discovery cap) — run "
                            "`fields @message | limit 5` to see its shape."
                        )
                    ),
                    columns=columns,
                    pks=[],
                    fks=[],
                    metadata_json={
                        "source": "cloudwatch_logs",
                        "log_group": name,
                        "arn": group.get("arn"),
                        "stored_bytes": group.get("storedBytes"),
                        "retention_days": retention,
                        "field_sampled": name in sampled,
                    },
                )
            )
            if progress_callback:
                try:
                    progress_callback(i + 1, total, f"Indexed {i + 1}/{total} log groups")
                except Exception:
                    pass
        return tables

    def _metric_tables(self, cw, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        if not self.metric_namespaces:
            return []

        # (namespace, metric) -> set of dimension names, unioned across every
        # dimension-set variant list_metrics returns for that metric.
        grouped: Dict[Tuple[str, str], set] = {}
        for ns in self.metric_namespaces:
            try:
                for page in cw.get_paginator("list_metrics").paginate(Namespace=ns):
                    for m in page.get("Metrics", []):
                        key = (m["Namespace"], m["MetricName"])
                        bucket = grouped.setdefault(key, set())
                        for d in m.get("Dimensions", []):
                            bucket.add(d["Name"])
            except Exception as e:
                logger.info("CloudWatch: metric discovery failed for %s: %s", ns, e)

        tables: List[Table] = []
        total = len(grouped)
        for i, ((ns, metric), dims) in enumerate(sorted(grouped.items())):
            columns = [
                TableColumn(name=d, dtype="dimension", description=f"CloudWatch dimension '{d}'.")
                for d in sorted(dims)
            ]
            columns.append(TableColumn(name="timestamp", dtype="datetime", description="Sample time (UTC)."))
            columns.append(
                TableColumn(name="value", dtype="number", description="Aggregated statistic for the period.")
            )
            tables.append(
                Table(
                    name=f"{METRIC_PREFIX}{ns}/{metric}",
                    description=(
                        f"CloudWatch metric {metric} in namespace {ns}. "
                        f"Available dimensions: {', '.join(sorted(dims)) or 'none'}."
                    ),
                    columns=columns,
                    pks=[],
                    fks=[],
                    metadata_json={
                        "source": "cloudwatch_metrics",
                        "namespace": ns,
                        "metric_name": metric,
                        "dimensions": sorted(dims),
                    },
                )
            )
            if progress_callback:
                try:
                    progress_callback(i + 1, total, f"Indexed {i + 1}/{total} metrics")
                except Exception:
                    pass
        return tables

    def get_tables(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        with self.connect() as (logs, cw):
            tables = self._log_group_tables(logs, progress_callback=progress_callback)
            tables.extend(self._metric_tables(cw, progress_callback=progress_callback))
            return tables

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        return self.get_tables(progress_callback=progress_callback)

    def get_schema(self, table_name: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_schemas() instead.")

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    # ── Logs Insights execution ──────────────────────────────────────────────

    def _run_insights(
        self, logs, query: str, log_groups: List[str], start: int, end: int
    ) -> List[Dict[str, str]]:
        """Start an Insights query, poll to completion, return raw rows.

        Insights is asynchronous: StartQuery returns a queryId and the caller
        polls GetQueryResults until the status leaves the running states. On
        timeout the query is cancelled — an abandoned query keeps scanning (and
        keeps billing) until the service stops it.
        """
        if not log_groups:
            raise ValueError(
                "A Logs Insights query needs at least one log group. Pass "
                '`log_groups`: ["/aws/lambda/my-fn"] in the query envelope.'
            )
        if len(log_groups) > MAX_LOG_GROUPS_PER_QUERY:
            raise ValueError(
                f"CloudWatch accepts at most {MAX_LOG_GROUPS_PER_QUERY} log groups per query; "
                f"{len(log_groups)} were given. Narrow the selection or query in batches."
            )

        query_id = logs.start_query(
            logGroupNames=log_groups,
            startTime=start,
            endTime=end,
            queryString=query,
        )["queryId"]

        deadline = time.time() + QUERY_TIMEOUT_SECONDS
        delay = POLL_INITIAL
        while True:
            result = logs.get_query_results(queryId=query_id)
            status = result.get("status")
            if status == "Complete":
                return [
                    {f["field"]: f["value"] for f in row}
                    for row in result.get("results", [])
                ]
            if status in ("Failed", "Cancelled", "Timeout"):
                raise RuntimeError(
                    f"CloudWatch Logs Insights query ended with status {status}. "
                    f"Query: {query[:200]}"
                )
            if time.time() > deadline:
                self._stop_query(logs, query_id)
                raise TimeoutError(
                    f"CloudWatch Logs Insights query exceeded {QUERY_TIMEOUT_SECONDS}s and was "
                    "cancelled. Narrow the time window or the log-group selection."
                )
            time.sleep(delay)
            delay = min(delay * 1.5, POLL_MAX)

    @staticmethod
    def _stop_query(logs, query_id: str) -> None:
        """Best-effort cancel. StopQuery raises InvalidParameterException if the
        query already ended, which is not an error worth surfacing."""
        try:
            logs.stop_query(queryId=query_id)
        except Exception as e:
            logger.debug("CloudWatch: stop_query(%s) ignored: %s", query_id, e)

    def _execute_logs(self, logs, spec: Dict[str, Any]) -> pd.DataFrame:
        query = spec.get("insights") or spec.get("query") or spec.get("logs")
        groups = spec.get("log_groups") or spec.get("log_group") or []
        if isinstance(groups, str):
            groups = [groups]
        groups = [self._strip_prefix(g) for g in groups]

        start, end = self._window(spec)
        limit = spec.get("limit")
        if limit is not None:
            limit = min(int(limit), MAX_INSIGHTS_ROWS)
            if "| limit" not in query.lower():
                query = f"{query} | limit {limit}"

        rows = self._run_insights(logs, query=query, log_groups=groups, start=start, end=end)
        if not rows:
            return pd.DataFrame()

        keep_noise = any(f in query for f in _NOISE_FIELDS)
        if not keep_noise:
            rows = [{k: v for k, v in r.items() if k not in _NOISE_FIELDS} for r in rows]

        df = pd.DataFrame(rows)
        if "@timestamp" in df.columns:
            df["@timestamp"] = pd.to_datetime(df["@timestamp"], errors="coerce", utc=True)
        if "@ingestionTime" in df.columns:
            df["@ingestionTime"] = pd.to_datetime(df["@ingestionTime"], errors="coerce", utc=True)
        # Insights returns every value as a string. Recover numerics for the
        # aggregate columns (count(), avg(), sum()) so charts work, and parse
        # `bin()` buckets — which come back in this exact format — back into
        # datetimes, since a time bucket that stays a string plots as a
        # category rather than a time axis.
        for col in df.columns:
            if col.startswith("@") or df[col].dtype != object:
                continue
            converted = pd.to_numeric(df[col], errors="coerce")
            if converted.notna().all():
                df[col] = converted
                continue
            bucketed = pd.to_datetime(
                df[col], format=_INSIGHTS_BIN_FORMAT, errors="coerce", utc=True
            )
            if bucketed.notna().all():
                df[col] = bucketed
        return df

    # ── Metrics execution ────────────────────────────────────────────────────

    def _resolve_dimension_sets(self, cw, namespace: str, metric_name: str) -> List[List[dict]]:
        """The dimension-set variants CloudWatch actually publishes for a metric.

        A metric queried WITHOUT its dimensions returns nothing: `AWS/EC2`
        `CPUUtilization` only exists per `InstanceId`, so a dimensionless
        MetricStat matches no series and comes back empty. Rather than hand the
        agent an empty frame and let it conclude "you have no EC2 instances",
        resolve the real dimension sets and query each one.
        """
        variants: List[List[dict]] = []
        seen = set()
        try:
            pages = cw.get_paginator("list_metrics").paginate(
                Namespace=namespace, MetricName=metric_name
            )
            for page in pages:
                for m in page.get("Metrics", []):
                    dims = m.get("Dimensions", []) or []
                    key = tuple(sorted((d["Name"], d["Value"]) for d in dims))
                    if key in seen:
                        continue
                    seen.add(key)
                    variants.append(dims)
                    if len(variants) >= MAX_METRIC_QUERIES:
                        return variants
        except Exception as e:
            logger.info("CloudWatch: dimension resolution failed for %s/%s: %s",
                        namespace, metric_name, e)
        return variants

    def _execute_metric(self, cw, spec: Dict[str, Any]) -> pd.DataFrame:
        start, end = self._window(spec)
        period = int(spec.get("period") or 300)
        stat = self._normalize_stat(spec.get("stat") or "Average")

        # query id -> the dimension dict that produced it, so the result frame
        # can carry the dimension COLUMNS the catalog advertises for this table.
        dims_by_id: Dict[str, Dict[str, str]] = {}
        queries: List[dict] = []

        if spec.get("metric_math"):
            queries.append({"Id": "q0", "Expression": spec["metric_math"], "Period": period})
        else:
            target = self._strip_prefix(spec["metric"])
            if "/" not in target:
                raise ValueError(
                    f"Metric {target!r} must be '<Namespace>/<MetricName>', e.g. 'AWS/EC2/CPUUtilization'."
                )
            namespace, metric_name = target.rsplit("/", 1)
            requested = spec.get("dimensions") or {}

            if requested:
                variants = [[{"Name": k, "Value": str(v)} for k, v in requested.items()]]
            else:
                variants = self._resolve_dimension_sets(cw, namespace, metric_name) or [[]]

            for i, dims in enumerate(variants):
                qid = f"q{i}"
                dims_by_id[qid] = {d["Name"]: d["Value"] for d in dims}
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {"Namespace": namespace, "MetricName": metric_name,
                                   "Dimensions": dims},
                        "Period": period,
                        "Stat": stat,
                    },
                })

        rows: List[dict] = []
        next_token = None
        while True:
            kwargs: Dict[str, Any] = {
                "MetricDataQueries": queries,
                "StartTime": pd.Timestamp(start, unit="s", tz="UTC").to_pydatetime(),
                "EndTime": pd.Timestamp(end, unit="s", tz="UTC").to_pydatetime(),
            }
            if next_token:
                kwargs["NextToken"] = next_token
            response = cw.get_metric_data(**kwargs)
            for series in response.get("MetricDataResults", []):
                label = series.get("Label")
                dims = dims_by_id.get(series.get("Id"), {})
                for ts, value in zip(series.get("Timestamps", []), series.get("Values", [])):
                    rows.append({**dims, "series": label, "timestamp": ts, "value": value})
            next_token = response.get("NextToken")
            if not next_token:
                break

        dim_cols = sorted({k for d in dims_by_id.values() for k in d})
        columns = dim_cols + ["series", "timestamp", "value"]
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.sort_values((dim_cols or ["series"]) + ["timestamp"]).reset_index(drop=True)
        return df

    @staticmethod
    def _normalize_stat(stat: str) -> str:
        """Accept the friendly spellings an LLM reaches for ('avg', 'p95')."""
        s = str(stat).strip()
        aliases = {
            "avg": "Average", "average": "Average", "mean": "Average",
            "sum": "Sum", "total": "Sum",
            "min": "Minimum", "minimum": "Minimum",
            "max": "Maximum", "maximum": "Maximum",
            "count": "SampleCount", "samplecount": "SampleCount",
        }
        if s.lower() in aliases:
            return aliases[s.lower()]
        if re.fullmatch(r"p\d{1,2}(\.\d+)?", s.lower()):
            return s.lower()
        return s

    # ── dispatch ─────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_prefix(name: str) -> str:
        """Accept either the catalog table name or the bare AWS identifier."""
        s = str(name).strip()
        for prefix in (LOG_PREFIX, METRIC_PREFIX):
            if s.startswith(prefix):
                return s[len(prefix):]
        return s

    @staticmethod
    def _coerce_spec(query: Any) -> Dict[str, Any]:
        if isinstance(query, dict):
            return dict(query)
        s = str(query or "").strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return {"insights": s}
        return {"insights": s}

    def execute_query(self, query: Any = None, **kwargs: Any) -> pd.DataFrame:
        """Run a CloudWatch query and return a DataFrame.

        Logs Insights:
            {"insights": "fields @timestamp, @message | filter @message like /ERROR/",
             "log_groups": ["/aws/lambda/checkout"], "start": "-1h", "limit": 200}

        A metric time series:
            {"metric": "AWS/EC2/CPUUtilization", "stat": "Average", "period": 300,
             "dimensions": {"InstanceId": "i-0abc"}, "start": "-24h"}

        Metrics Insights (SQL-ish metric math across many series):
            {"metric_math": 'SELECT AVG(CPUUtilization) FROM "AWS/EC2" GROUP BY InstanceId',
             "start": "-6h", "period": 300}

        A bare string is treated as a Logs Insights query, which then needs
        `log_groups` supplied via kwargs.
        """
        spec = self._coerce_spec(query)
        # Allow kwargs form: execute_query(insights="...", log_groups=[...]).
        for key, value in kwargs.items():
            spec.setdefault(key, value)

        with self.connect() as (logs, cw):
            if spec.get("metric") or spec.get("metric_math"):
                return self._execute_metric(cw, spec)
            if spec.get("insights") or spec.get("query") or spec.get("logs"):
                return self._execute_logs(logs, spec)
            raise ValueError(
                "CloudWatch query must specify one of: 'insights' (Logs Insights query "
                "string + log_groups), 'metric' (Namespace/MetricName), or 'metric_math' "
                "(a Metrics Insights SELECT)."
            )

    # ── connection test ──────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """Probe both halves. Report which IAM action failed — an admin whose
        policy is missing one permission needs to know *which*."""
        try:
            with self.connect() as (logs, cw):
                try:
                    groups = self._list_log_groups(logs)
                except Exception as e:
                    return {
                        "success": False,
                        "message": f"logs:DescribeLogGroups failed — check the IAM policy. {e}",
                    }
                try:
                    cw.list_metrics(**({"Namespace": self.metric_namespaces[0]}
                                       if self.metric_namespaces else {}))
                except Exception as e:
                    return {
                        "success": False,
                        "message": f"cloudwatch:ListMetrics failed — check the IAM policy. {e}",
                    }

                msg = f"Connected to AWS CloudWatch ({self.region}). {len(groups)} log group(s) visible"
                if self.log_group_prefix:
                    msg += f" under prefix '{self.log_group_prefix}'"
                if self.metric_namespaces:
                    msg += f"; metric namespaces: {', '.join(self.metric_namespaces)}"
                if not groups and not self.metric_namespaces:
                    msg += ". No log groups found and no metric namespaces configured — this connection would index nothing."
                return {"success": True, "message": msg + "."}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── agent-facing prompt ──────────────────────────────────────────────────

    def system_prompt(self) -> str:
        return f"""
## AWS CloudWatch Integration

This connector exposes **two kinds of table**, distinguished by their prefix:

- `{LOG_PREFIX}<name>` — a CloudWatch **log group**, queried with **Logs Insights**
  (NOT SQL). Columns are `@timestamp`, `@message`, `@logStream` plus any JSON
  keys sampled out of recent events.
- `{METRIC_PREFIX}<Namespace>/<MetricName>` — a CloudWatch **metric**. Columns are
  its dimensions plus `timestamp` and `value`.

Call `execute_query` with a dict envelope.

### Logs Insights
```python
df = client.execute_query({{
    "insights": "fields @timestamp, @logStream, @message | filter @message like /ERROR/ | sort @timestamp desc",
    "log_groups": ["/aws/lambda/checkout"],
    "start": "-1h", "limit": 200,
}})

# Aggregate server-side — this is both cheaper and the only way past the row cap
df = client.execute_query({{
    "insights": "stats count() as errors by bin(5m) as t",
    "log_groups": ["/aws/lambda/checkout"], "start": "-6h",
}})

# Parse structured fields out of a JSON body
df = client.execute_query({{
    "insights": "fields @timestamp, level, msg | filter level = 'error' | stats count() by msg",
    "log_groups": ["/ecs/api"], "start": "-24h",
}})
```

### Metrics
```python
# Every instance/resource publishing this metric — dimensions are resolved for
# you, and each dimension comes back as its own column.
df = client.execute_query({{
    "metric": "AWS/EC2/CPUUtilization", "stat": "Average", "period": 300, "start": "-24h",
}})
# -> columns: InstanceId, series, timestamp, value

# Narrow to one resource
df = client.execute_query({{
    "metric": "AWS/EC2/CPUUtilization", "stat": "Average", "period": 300,
    "dimensions": {{"InstanceId": "i-0abc123"}}, "start": "-24h",
}})

# Across many series at once (Metrics Insights)
df = client.execute_query({{
    "metric_math": 'SELECT AVG(CPUUtilization) FROM "AWS/EC2" GROUP BY InstanceId',
    "start": "-6h", "period": 300,
}})
```

### Rules of thumb
- **Logs Insights is not SQL.** Pipe stages: `fields`, `filter`, `stats … by`,
  `sort`, `limit`, `parse`. No SELECT/FROM/JOIN.
- **`log_groups` is required for a logs query** — pass the log group's real
  name (the `{LOG_PREFIX}` prefix is stripped for you if you leave it on). At
  most {MAX_LOG_GROUPS_PER_QUERY} per query.
- **Cost: Insights bills on bytes scanned, and `limit` does NOT reduce it.**
  Only the time window and log-group selection do. Keep `start` as tight as the
  question allows; default is `{DEFAULT_QUERY_WINDOW}`.
- **A query returns at most {MAX_INSIGHTS_ROWS:,} rows.** For anything wider,
  aggregate with `stats` instead of pulling raw events.
- **A metric table's dimension columns are returned as columns.** Omit
  `dimensions` to get every resource publishing that metric (one row per
  resource per sample); pass them to narrow to one. An empty result means the
  metric genuinely has no data in the window — not that dimensions were missing.
- `stat` accepts `Average`/`Sum`/`Minimum`/`Maximum`/`SampleCount` or a
  percentile like `p95`; friendly spellings (`avg`, `max`) are normalized.
- Times: relative (`-15m`, `-24h`, `-7d`), `now`, ISO-8601, or epoch seconds.
"""

    @property
    def description(self) -> str:
        head = f"AWS CloudWatch (region {self.region}) — log groups queried with Logs Insights, plus CloudWatch metrics."
        if self.log_group_prefix:
            head += f" Log group discovery is scoped to names starting with '{self.log_group_prefix}'."
        if self.metric_namespaces:
            head += f" Metric namespaces: {', '.join(self.metric_namespaces)}."
        return head + "\n\n" + self.system_prompt()
