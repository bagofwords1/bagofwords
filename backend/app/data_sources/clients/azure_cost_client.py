"""Azure Cost Management data source client — the Azure counterpart of AWS Cost Explorer.

Wraps three read-only Cost Management REST operations against one configured
**scope** (subscription, resource group, management group, or an EA / MCA
billing scope):

    costs              POST {scope}/providers/Microsoft.CostManagement/query
    forecast           POST {scope}/providers/Microsoft.CostManagement/forecast
    dimension_values   GET  {scope}/providers/Microsoft.CostManagement/dimensions

The Query API is an aggregation API, not a row store: every call names a time
window, an optional granularity (Daily / Monthly / none), at most two group-by
columns (dimensions or tag keys) and at most one or two summed cost columns.
The client accepts a small dict envelope (``group_by``, ``filter``, ``start``…)
and builds the REST body, so the agent never hand-writes the nested filter
grammar; a raw ``body`` is passed through for anything the envelope can't say.

Throttling is the main operational hazard. Microsoft documents a per-tenant
quota in "query processing units" (12 QPU / 10 s, 60 / min, 600 / hour; one
QPU ≈ one month of data queried) and signals throttling with a 429 plus one of
several ``x-ms-ratelimit-microsoft.*-retry-after`` headers. The client honours
the longest of those headers (falling back to ``Retry-After``), retries a few
times, and otherwise fails with a message that says to narrow the window.

Auth: an Entra ID service principal (tenant / client id / secret), or
azure-identity's DefaultAzureCredential (managed identity, workload identity,
env vars) when the app runs inside Azure. The identity needs *Cost Management
Reader* on the scope (or the EA / MCA billing role for billing scopes).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.progress import (
    IndexingCancelled,
    ProgressCallback,
    discovery_progress,
)

logger = logging.getLogger(__name__)

API_VERSION = "2025-03-01"
ARM_ENDPOINT = "https://management.azure.com"
ARM_TOKEN_SCOPE = "https://management.azure.com/.default"

COSTS_TABLE = "costs"
FORECAST_TABLE = "forecast"
DIMENSION_VALUES_TABLE = "dimension_values"

# Hard limits documented for the Query / Forecast APIs.
MAX_GROUPINGS = 2
MAX_AGGREGATIONS = 2

COST_TYPES = ("ActualCost", "AmortizedCost", "Usage")
COST_METRICS = ("Cost", "PreTaxCost", "CostUSD", "PreTaxCostUSD")
TIMEFRAMES = (
    "MonthToDate", "BillingMonthToDate", "TheLastMonth", "TheLastBillingMonth",
    "WeekToDate", "TheCurrentMonth", "Custom",
)
GRANULARITIES = {"daily": "Daily", "monthly": "Monthly", "none": None, "total": None}

# Client-side limits.
MAX_PAGES = 50
MAX_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 120
DEFAULT_LOOKBACK = "-30d"

# Every header Cost Management has been seen to use to say "back off N seconds".
# Microsoft's docs name the qpu one; the forecast/query specs name the
# consumption one; entity/tenant/client variants are seen in the wild.
_RETRY_AFTER_HEADERS = (
    "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
    "x-ms-ratelimit-microsoft.consumption-retry-after",
    "retry-after",
)

# Used when the Dimensions API is unavailable for the scope (it returns 204
# for some account types). Names are the ones the Query API accepts in
# `grouping` / `filter.dimensions`.
FALLBACK_DIMENSIONS = (
    "ResourceGroupName",
    "ResourceLocation",
    "ResourceType",
    "ResourceId",
    "ServiceName",
    "ServiceFamily",
    "ServiceTier",
    "MeterCategory",
    "MeterSubCategory",
    "Meter",
    "SubscriptionId",
    "SubscriptionName",
    "ChargeType",
    "PublisherType",
    "PricingModel",
    "ReservationName",
    "Frequency",
)

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RELATIVE_RE = re.compile(r"^-(\d+)\s*([dwmy])$", re.IGNORECASE)
_TAG_PREFIX = "tag:"


class AzureCostError(RuntimeError):
    """A Cost Management API call failed; the message is safe to show a user."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def normalize_scope(scope: str) -> str:
    """Turn what an admin pastes into the ARM scope path the API expects.

    Accepts a bare subscription GUID, a path with or without the leading
    slash, or a full portal/ARM URL; strips any trailing slash.
    """
    s = (scope or "").strip()
    if not s:
        raise ValueError("Azure Cost Management scope is required (e.g. /subscriptions/<subscription-id>).")
    if s.lower().startswith(ARM_ENDPOINT):
        s = s[len(ARM_ENDPOINT):]
    s = s.split("?", 1)[0].strip()
    if _GUID_RE.match(s):
        return f"/subscriptions/{s}"
    if not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


class AzureCostClient(DataSourceClient):
    """Azure Cost Management (Query / Forecast / Dimensions) as a table-shaped source."""

    capabilities = {Capability.QUERY}

    relative_date_hint = (
        "Relative dates (Azure Cost Management): pass `start`/`end` in the query "
        "envelope as relative offsets ('-7d', '-3m', '-1y'), 'today', "
        "'start_of_month', 'start_of_last_month', 'end_of_last_month', or ISO dates "
        "(YYYY-MM-DD) — or a named `timeframe` such as 'MonthToDate' / 'TheLastMonth'. "
        "Never bake absolute dates into a saved query. Dates are UTC."
    )

    def __init__(
        self,
        scope: str,
        cost_type: str = "ActualCost",
        # Credentials — the flat union of every auth variant, because
        # Connection.get_client() splats config + decrypted credentials together.
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        _session: Any = None,
        _sleep: Callable[[float], None] = time.sleep,
        _today: Callable[[], dt.date] | None = None,
        **kwargs: Any,
    ):
        self.scope = normalize_scope(scope)
        self.cost_type = cost_type if cost_type in COST_TYPES else "ActualCost"
        self.tenant_id = (tenant_id or "").strip() or None
        self.client_id = (client_id or "").strip() or None
        self.client_secret = client_secret or None
        self._http = _session
        self._sleep = _sleep
        self._today = _today or (lambda: dt.datetime.now(dt.UTC).date())
        self._credential = None
        self._dimensions_cache: list[dict] | None = None

    # ── auth + HTTP ──────────────────────────────────────────────────────────

    def _get_credential(self):
        if self._credential is None:
            if self.client_id and self.client_secret and self.tenant_id:
                from azure.identity import ClientSecretCredential

                self._credential = ClientSecretCredential(
                    tenant_id=self.tenant_id,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                )
            else:
                from azure.identity import DefaultAzureCredential

                self._credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True,
                )
        return self._credential

    def _token(self) -> str:
        try:
            return self._get_credential().get_token(ARM_TOKEN_SCOPE).token
        except IndexingCancelled:
            raise
        except Exception as e:
            raise AzureCostError(
                f"Could not get an Azure access token — check the tenant ID, client ID and secret. {e}",
                status=401,
                code="AuthenticationFailed",
            )

    def _session(self):
        if self._http is None:
            import requests

            self._http = requests.Session()
        return self._http

    @contextmanager
    def connect(self):
        """Yield an authenticated requests-like session.

        Only token acquisition is wrapped; errors raised by the caller inside
        the block keep their own type and message.
        """
        self._token()
        yield self._session()

    @staticmethod
    def _retry_after(headers: Any) -> float | None:
        """Longest back-off any Cost Management throttling header asks for."""
        if not headers:
            return None
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
        waits = []
        for name in _RETRY_AFTER_HEADERS:
            raw = lowered.get(name)
            if raw is None:
                continue
            try:
                waits.append(float(str(raw).strip()))
            except ValueError:
                continue
        return max(waits) if waits else None

    @staticmethod
    def _error_detail(resp: Any) -> tuple[str | None, str]:
        try:
            body = resp.json()
        except Exception:
            return None, (getattr(resp, "text", "") or "")[:500]
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return err.get("code"), err.get("message") or ""
        return None, json.dumps(body)[:500]

    def _request(self, method: str, url: str, body: dict | None = None) -> dict | None:
        """One ARM call with throttling-aware retries. Returns parsed JSON, or
        None for a 204 (no data for this scope)."""
        session = self._session()
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
                # Without a ClientType, callers share one anonymous rate-limit bucket.
                "ClientType": "bagofwords",
            }
            resp = session.request(
                method, url, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS
            )
            status = resp.status_code
            if status == 204:
                return None
            if 200 <= status < 300:
                return resp.json()

            code, message = self._error_detail(resp)
            if status in (429, 503) and attempt < MAX_RETRIES:
                wait = self._retry_after(resp.headers)
                if wait is None:
                    wait = min(2 ** (attempt + 1), MAX_RETRY_WAIT_SECONDS)
                if wait <= MAX_RETRY_WAIT_SECONDS:
                    attempt += 1
                    logger.info("Azure Cost Management throttled (%s); retrying in %ss", status, wait)
                    self._sleep(wait)
                    continue
            raise AzureCostError(self._explain(status, code, message), status=status, code=code)

    def _explain(self, status: int, code: str | None, message: str) -> str:
        prefix = f"Azure Cost Management returned {status}"
        if code:
            prefix += f" ({code})"
        hint = ""
        if status in (401, 403):
            hint = (
                f" The identity needs the 'Cost Management Reader' role on {self.scope} "
                "(or an EA / MCA billing reader role for billing scopes). For EA "
                "subscriptions, 'Account owners can view charges' must also be enabled."
            )
        elif status == 404:
            hint = f" Check that the scope '{self.scope}' exists and is spelled correctly."
        elif status == 429:
            hint = (
                " The tenant's Cost Management query quota is exhausted. Narrow the date "
                "range (each month queried costs quota) or try again in a few minutes."
            )
        return f"{prefix}: {message}{hint}".strip()

    def _url(self, operation: str, params: str = "") -> str:
        return (
            f"{ARM_ENDPOINT}{self.scope}/providers/Microsoft.CostManagement/"
            f"{operation}?api-version={API_VERSION}{params}"
        )

    # ── dates ────────────────────────────────────────────────────────────────

    def _resolve_date(self, value: Any, default: dt.date) -> dt.date:
        if value is None or value == "":
            return default
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        s = str(value).strip().lower()
        today = self._today()
        if s in ("today", "now"):
            return today
        if s == "yesterday":
            return today - dt.timedelta(days=1)
        if s == "start_of_month":
            return today.replace(day=1)
        if s == "start_of_last_month":
            return (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        if s == "end_of_last_month":
            return today.replace(day=1) - dt.timedelta(days=1)
        if s == "start_of_year":
            return today.replace(month=1, day=1)
        m = _RELATIVE_RE.match(s)
        if m:
            n, unit = int(m.group(1)), m.group(2).lower()
            if unit == "d":
                return today - dt.timedelta(days=n)
            if unit == "w":
                return today - dt.timedelta(weeks=n)
            if unit == "m":
                month_index = today.year * 12 + (today.month - 1) - n
                year, month = divmod(month_index, 12)
                return today.replace(year=year, month=month + 1, day=1)
            if unit == "y":
                return today.replace(year=today.year - n, month=1, day=1)
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            raise ValueError(
                f"Unrecognized date '{value}'. Use YYYY-MM-DD, a relative offset like "
                "'-30d' / '-3m', or 'today' / 'start_of_month' / 'start_of_last_month'."
            )

    def _time_fields(self, spec: dict[str, Any]) -> dict[str, Any]:
        timeframe = spec.get("timeframe")
        if timeframe and str(timeframe) != "Custom" and not (spec.get("start") or spec.get("end")):
            match = next((t for t in TIMEFRAMES if t.lower() == str(timeframe).lower()), None)
            if not match:
                raise ValueError(f"Unknown timeframe '{timeframe}'. Use one of: {', '.join(TIMEFRAMES)}.")
            return {"timeframe": match}
        today = self._today()
        start = self._resolve_date(spec.get("start"), self._resolve_date(DEFAULT_LOOKBACK, today))
        end = self._resolve_date(spec.get("end"), today)
        if end < start:
            raise ValueError(f"end ({end}) is before start ({start}).")
        return {
            "timeframe": "Custom",
            "timePeriod": {
                "from": f"{start.isoformat()}T00:00:00+00:00",
                "to": f"{end.isoformat()}T23:59:59+00:00",
            },
        }

    # ── body construction ────────────────────────────────────────────────────

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    @classmethod
    def _grouping(cls, group_by: Any) -> list[dict]:
        groups = []
        for g in cls._as_list(group_by):
            if isinstance(g, dict):
                groups.append({"type": g.get("type", "Dimension"), "name": g["name"]})
                continue
            name = str(g).strip()
            if name.lower().startswith(_TAG_PREFIX):
                groups.append({"type": "TagKey", "name": name[len(_TAG_PREFIX):]})
            else:
                groups.append({"type": "Dimension", "name": name})
        if len(groups) > MAX_GROUPINGS:
            raise ValueError(
                f"Azure Cost Management allows at most {MAX_GROUPINGS} group_by columns per query "
                f"(got {len(groups)}). Run separate queries or filter instead."
            )
        return groups

    @classmethod
    def _filter(cls, flt: Any) -> dict | None:
        """Accept either the native filter grammar or a flat
        ``{"ResourceGroupName": ["rg1"], "tag:env": ["prod"]}`` mapping."""
        if not flt:
            return None
        if not isinstance(flt, dict):
            raise ValueError("filter must be a dict.")
        native_keys = {"and", "or", "dimensions", "tags"}
        if set(flt.keys()) & native_keys:
            return flt
        clauses = []
        for name, values in flt.items():
            vals = [str(v) for v in cls._as_list(values)]
            if not vals:
                continue
            key = str(name)
            if key.lower().startswith(_TAG_PREFIX):
                clauses.append({"tags": {"name": key[len(_TAG_PREFIX):], "operator": "In", "values": vals}})
            else:
                clauses.append({"dimensions": {"name": key, "operator": "In", "values": vals}})
        if not clauses:
            return None
        # The API rejects an `and` with fewer than two members.
        return clauses[0] if len(clauses) == 1 else {"and": clauses}

    def _aggregation(self, spec: dict[str, Any]) -> dict[str, dict]:
        metrics = self._as_list(spec.get("metrics") or spec.get("metric") or "Cost")
        if len(metrics) > MAX_AGGREGATIONS:
            raise ValueError(f"At most {MAX_AGGREGATIONS} metrics per query.")
        agg = {}
        for m in metrics:
            match = next((c for c in COST_METRICS if c.lower() == str(m).lower()), None)
            if not match:
                raise ValueError(f"Unknown metric '{m}'. Use one of: {', '.join(COST_METRICS)}.")
            agg[match] = {"name": match, "function": "Sum"}
        return agg

    def build_body(self, spec: dict[str, Any], forecast: bool = False) -> dict[str, Any]:
        if isinstance(spec.get("body"), dict):
            return spec["body"]
        cost_type = spec.get("type") or spec.get("cost_type") or self.cost_type
        match = next((c for c in COST_TYPES if c.lower() == str(cost_type).lower()), None)
        if not match:
            raise ValueError(f"Unknown cost type '{cost_type}'. Use one of: {', '.join(COST_TYPES)}.")

        granularity_raw = spec.get("granularity", "Daily" if forecast else "None")
        gkey = str(granularity_raw or "none").lower()
        if gkey not in GRANULARITIES:
            raise ValueError("granularity must be 'Daily', 'Monthly' or 'None'.")
        granularity = GRANULARITIES[gkey]

        dataset: dict[str, Any] = {"aggregation": self._aggregation(spec)}
        if granularity:
            dataset["granularity"] = granularity
        flt = self._filter(spec.get("filter"))
        if flt:
            dataset["filter"] = flt

        if forecast:
            if not granularity:
                dataset["granularity"] = "Daily"
            today = self._today()
            spec = dict(spec)
            spec.setdefault("start", today.isoformat())
            if not spec.get("end"):
                nm = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
                spec["end"] = (nm - dt.timedelta(days=1)).isoformat()
            spec.pop("timeframe", None)
            body = {"type": match, "dataset": dataset, **self._time_fields(spec)}
            body["includeActualCost"] = bool(spec.get("include_actual", False))
            body["includeFreshPartialCost"] = bool(spec.get("include_actual", False))
            return body

        groups = self._grouping(spec.get("group_by"))
        if groups:
            dataset["grouping"] = groups
        return {"type": match, "dataset": dataset, **self._time_fields(spec)}

    # ── response shaping ─────────────────────────────────────────────────────

    def _run_paged(self, operation: str, body: dict, max_rows: int | None) -> pd.DataFrame:
        url = self._url(operation)
        columns: list[str] = []
        rows: list[list] = []
        pages = 0
        while url and pages < MAX_PAGES:
            payload = self._request("POST", url, body)
            pages += 1
            if not payload:
                break
            props = payload.get("properties") or {}
            if not columns:
                columns = [c.get("name") for c in props.get("columns") or []]
            rows.extend(props.get("rows") or [])
            if max_rows and len(rows) >= max_rows:
                rows = rows[:max_rows]
                break
            # The next page is fetched by POSTing the same body to nextLink.
            url = props.get("nextLink")
        df = pd.DataFrame(rows, columns=columns or None)
        return self._normalize_frame(df)

    @staticmethod
    def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        if "UsageDate" in df.columns:
            df["UsageDate"] = pd.to_datetime(df["UsageDate"].astype(str), format="%Y%m%d", errors="coerce").dt.date
        if "BillingMonth" in df.columns:
            df["BillingMonth"] = pd.to_datetime(df["BillingMonth"], errors="coerce").dt.date
        return df

    @staticmethod
    def _align_cost_column(df: pd.DataFrame, requested: list[str]) -> pd.DataFrame:
        """With one requested metric, expose it under its requested name.

        Documented responses name the summed column after the source column
        (e.g. `PreTaxCost`) even when a different metric/alias was asked for,
        so code written against `Cost` would otherwise break per account type.
        """
        if len(requested) != 1 or requested[0] in df.columns:
            return df
        present = [c for c in df.columns if c in COST_METRICS or c == "totalCost"]
        if len(present) == 1:
            df = df.rename(columns={present[0]: requested[0]})
        return df

    def _query(self, operation: str, spec: dict[str, Any]) -> pd.DataFrame:
        forecast = operation == "forecast"
        body = self.build_body(spec, forecast=forecast)
        max_rows = spec.get("limit")
        user_chose_metric = bool(spec.get("metric") or spec.get("metrics") or spec.get("body"))
        try:
            df = self._run_paged(operation, body, max_rows)
        except AzureCostError as e:
            # Some account types only accept PreTaxCost (EA / pay-as-you-go
            # legacy) or only Cost (MCA). When the caller left the metric to
            # us, retry once with the other spelling.
            if e.status == 400 and not user_chose_metric:
                body = json.loads(json.dumps(body))
                body["dataset"]["aggregation"] = {"PreTaxCost": {"name": "PreTaxCost", "function": "Sum"}}
                df = self._run_paged(operation, body, max_rows)
            else:
                raise
        requested = [] if spec.get("body") else list(self._aggregation(spec).keys())
        return self._align_cost_column(df, requested)

    def _dimension_values(self, spec: dict[str, Any]) -> pd.DataFrame:
        name = spec.get("dimension_values") or spec.get("dimension")
        top = int(spec.get("limit") or 1000)
        params = f"&$expand=properties/data&$top={min(max(top, 1), 1000)}"
        if name:
            params += f"&$filter=properties/category eq '{name}'"
        payload = self._request("GET", self._url("dimensions", params)) or {}
        rows = []
        for item in payload.get("value") or []:
            props = item.get("properties") or {}
            category = props.get("category")
            for v in props.get("data") or []:
                rows.append({"dimension": category, "value": v})
        return pd.DataFrame(rows, columns=["dimension", "value"])

    # ── discovery ────────────────────────────────────────────────────────────

    def _list_dimensions(self) -> list[dict]:
        if self._dimensions_cache is not None:
            return self._dimensions_cache
        try:
            payload = self._request("GET", self._url("dimensions")) or {}
            dims = []
            for item in payload.get("value") or []:
                props = item.get("properties") or {}
                category = props.get("category")
                if category and (props.get("groupingEnabled") or props.get("filterEnabled")):
                    dims.append({
                        "name": category,
                        "description": props.get("description") or "",
                        "groupable": bool(props.get("groupingEnabled")),
                    })
        except IndexingCancelled:
            raise
        except Exception as e:
            logger.info("Azure Cost Management dimensions listing failed; using defaults: %s", e)
            dims = []
        if not dims:
            dims = [{"name": d, "description": "", "groupable": True} for d in FALLBACK_DIMENSIONS]
        self._dimensions_cache = dims
        return dims

    def _tables(self) -> list[Table]:
        dims = self._list_dimensions()
        dim_cols = [
            TableColumn(
                name=d["name"],
                dtype="dimension",
                description=(d["description"] or f"Cost dimension '{d['name']}'.")
                + ("" if d["groupable"] else " Filter only — cannot be used in group_by."),
            )
            for d in dims
        ]
        common = [
            TableColumn(name="Cost", dtype="number", description="Summed cost for the row, in Currency."),
            TableColumn(name="Currency", dtype="string", description="Billing currency of Cost."),
        ]
        costs = Table(
            name=COSTS_TABLE,
            columns=common
            + [
                TableColumn(name="UsageDate", dtype="date", description="Day of usage (granularity='Daily')."),
                TableColumn(name="BillingMonth", dtype="date", description="First day of the month (granularity='Monthly')."),
            ]
            + dim_cols,
            pks=[],
            fks=[],
            description=(
                f"Aggregated Azure spend for scope {self.scope}. Not a row store: each query "
                "sums cost over a time window, optionally per day/month and per up to "
                "two dimensions or tag keys."
            ),
        )
        forecast = Table(
            name=FORECAST_TABLE,
            columns=common
            + [
                TableColumn(name="UsageDate", dtype="date", description="Forecast day."),
                TableColumn(name="CostStatus", dtype="string", description="'Forecast' or 'Actual'."),
            ],
            pks=[],
            fks=[],
            description="Azure Cost Management spend forecast for future dates.",
        )
        values = Table(
            name=DIMENSION_VALUES_TABLE,
            columns=[
                TableColumn(name="dimension", dtype="string", description="Dimension name, e.g. ResourceGroupName."),
                TableColumn(name="value", dtype="string", description="A value seen for that dimension."),
            ],
            pks=[],
            fks=[],
            description="Distinct values per cost dimension — use to find exact filter values.",
        )
        return [costs, forecast, values]

    @discovery_progress
    def get_schemas(self, progress_callback: ProgressCallback | None = None) -> list[Table]:
        return self._tables()

    def get_tables(self, progress_callback: ProgressCallback | None = None) -> list[Table]:
        return self._tables()

    def get_schema(self, table_name: str) -> Table:
        for t in self._tables():
            if t.name == table_name:
                return t
        raise ValueError(f"Unknown table '{table_name}'.")

    def prompt_schema(self) -> str:
        return ServiceFormatter(self.get_schemas()).table_str

    # ── query entry point ────────────────────────────────────────────────────

    @staticmethod
    def _coerce_spec(query: Any) -> dict[str, Any]:
        if query is None:
            return {}
        if isinstance(query, dict):
            return dict(query)
        s = str(query).strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            raise ValueError(
                "Azure Cost Management queries are dict envelopes, e.g. "
                "{'start': '-30d', 'group_by': ['ServiceName']}, not SQL."
            )
        if not isinstance(parsed, dict):
            raise ValueError("Azure Cost Management query must be a JSON object.")
        return parsed

    def execute_query(self, query: Any = None, **kwargs: Any) -> pd.DataFrame:
        """Run a Cost Management query and return a DataFrame.

        Spend by service, last 30 days:
            {"start": "-30d", "group_by": ["ServiceName"]}
        Daily cost for one resource group, amortized:
            {"type": "AmortizedCost", "start": "start_of_month", "granularity": "Daily",
             "filter": {"ResourceGroupName": ["rg-prod"]}}
        Monthly cost by a tag:
            {"start": "-6m", "granularity": "Monthly", "group_by": ["tag:Environment"]}
        Forecast to month end:
            {"table": "forecast"}
        Values of a dimension:
            {"table": "dimension_values", "dimension": "ResourceGroupName"}
        """
        spec = self._coerce_spec(query)
        for key, value in kwargs.items():
            spec.setdefault(key, value)
        table = str(spec.get("table") or "").strip().lower()
        if spec.get("forecast") is True:
            table = FORECAST_TABLE
        if spec.get("dimension_values"):
            table = DIMENSION_VALUES_TABLE

        with self.connect():
            if table == DIMENSION_VALUES_TABLE:
                return self._dimension_values(spec)
            if table == FORECAST_TABLE:
                return self._query("forecast", spec)
            if table in ("", COSTS_TABLE):
                return self._query("query", spec)
        raise ValueError(
            f"Unknown table '{table}'. Use '{COSTS_TABLE}', '{FORECAST_TABLE}' or '{DIMENSION_VALUES_TABLE}'."
        )

    # ── connection test ──────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """Run the cheapest real query (month-to-date total, ~1 QPU)."""
        try:
            with self.connect():
                df = self._query("query", {"timeframe": "MonthToDate"})
        except IndexingCancelled:
            raise
        except Exception as e:
            return {"success": False, "message": str(e)}
        msg = f"Connected to Azure Cost Management for {self.scope}."
        if not df.empty and "Cost" in df.columns:
            total = pd.to_numeric(df["Cost"], errors="coerce").sum()
            currency = df["Currency"].iloc[0] if "Currency" in df.columns else ""
            msg += f" Month-to-date {self.cost_type}: {total:,.2f} {currency}".rstrip() + "."
        else:
            msg += " No cost recorded month-to-date."
        return {"success": True, "message": msg}

    # ── agent-facing prompt ──────────────────────────────────────────────────

    def system_prompt(self) -> str:
        return f"""
## Azure Cost Management Integration

Scope: `{self.scope}` · default cost type: `{self.cost_type}`.

This is an **aggregation API, not SQL**. Call `execute_query` with a dict envelope;
every query sums cost over a time window.

Tables:
- `{COSTS_TABLE}` — summed cost, optionally per day/month and per up to {MAX_GROUPINGS}
  dimensions or tag keys. Columns: `Cost`, `Currency`, `UsageDate` (Daily) or
  `BillingMonth` (Monthly), plus each `group_by` column.
- `{FORECAST_TABLE}` — projected cost per day (`UsageDate`, `Cost`, `CostStatus`).
- `{DIMENSION_VALUES_TABLE}` — known values of a dimension; use it to find exact filter values.

```python
# Spend by service, last 30 days
df = client.execute_query({{"start": "-30d", "group_by": ["ServiceName"]}})

# Daily cost trend this month for one resource group
df = client.execute_query({{
    "start": "start_of_month", "granularity": "Daily",
    "filter": {{"ResourceGroupName": ["rg-prod"]}},
}})

# Monthly cost by a tag for the last 6 months (tag keys are prefixed 'tag:')
df = client.execute_query({{"start": "-6m", "granularity": "Monthly", "group_by": ["tag:Environment"]}})

# Top resources last month, amortized (reservations spread over their term)
df = client.execute_query({{"type": "AmortizedCost", "timeframe": "TheLastMonth", "group_by": ["ResourceId"]}})

# Forecast through the end of this month
df = client.execute_query({{"table": "forecast"}})

# Which resource groups exist (exact names for filters)
df = client.execute_query({{"table": "dimension_values", "dimension": "ResourceGroupName"}})
```

Envelope keys:
- `start` / `end`: 'YYYY-MM-DD', '-30d', '-3m', '-1y', 'today', 'start_of_month',
  'start_of_last_month', 'end_of_last_month'. Default: last 30 days.
  Or `timeframe`: {', '.join(t for t in TIMEFRAMES if t != 'Custom')}.
- `granularity`: 'Daily', 'Monthly' or 'None' (default — one total per group).
- `group_by`: up to {MAX_GROUPINGS} dimension names, or `tag:<key>`.
- `filter`: {{"<Dimension>": [values], "tag:<key>": [values]}} — values are ANDed across keys,
  ORed within a key. The native Cost Management filter grammar (and/or/dimensions/tags) is also accepted.
- `type`: 'ActualCost' (invoice view), 'AmortizedCost' (reservations/savings plans spread daily).
- `metric`: 'Cost' (default, billing currency) or 'CostUSD'.
- `limit`: cap the rows returned.

Rules of thumb:
- Keep windows as tight as the question allows: the tenant's query quota is charged per
  month of data, and heavy use is throttled. Prefer one grouped query over many small ones.
- Keep a single query's window to 12 months or less; split longer ranges by year.
- More than {MAX_GROUPINGS} breakdowns: filter on one dimension and group by the others.
- Cost data lags usage by several hours; today's numbers are partial.
- Rows can span several currencies at billing-account scope — group or filter by Currency
  before summing.
"""

    @property
    def description(self) -> str:
        head = f"Azure Cost Management for {self.scope} — Azure spend by service, resource group, resource, tag and time, plus forecasts."
        return head + "\n\n" + self.system_prompt()
