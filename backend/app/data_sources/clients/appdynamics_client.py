"""AppDynamics (Cisco / Splunk AppDynamics) data source client.

Talks to the Controller REST API (`https://<controller>/controller/rest/...`).
Target deployment is on-premises controllers (validated against the 21.x API
surface, which has been stable since the 4.x era); SaaS controllers use the
same API. There is no free-form query language: queries are JSON specs (see
`system_prompt`) that map to a fixed catalog of virtual tables — topology
(applications, tiers, nodes, backends, business transactions, service flows)
plus time-bounded activity (metric data, events, health-rule violations,
snapshots).

The service map: AppDynamics has no official flow-map endpoint (the UI uses
the unsupported session-auth `/controller/restui/*` API, which this client
deliberately avoids). The `service_flows` virtual table derives the edge list
by enumerating metric *names* (not data) under
`Overall Application Performance|<tier>|External Calls|` — each child name
encodes an outbound call edge such as `Call-HTTP to Discovered backend call
"PaymentsGateway"`. One cheap metrics-browse call per tier; no metric data is
fetched, so it stays under Controller rate limits.

Auth (Controller-native, both work on 21.4):
  - `userpass`   → HTTP Basic. The Controller expects the login qualified with
                   the account name: `user@account` (on-prem single-tenant is
                   `customer1`). The account is appended automatically unless
                   the username already contains "@".
  - `api_client` → OAuth 2.0 client-credentials against
                   `/controller/api/oauth/access_token` with
                   `client_id=<clientName>@<accountName>`. Tokens are short-
                   lived (default 5 min) — cached and refreshed on expiry/401.

Every request passes `output=JSON` (the API defaults to XML without it).
"""
import json
import re
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, ServiceFormatter


MAX_ROWS = 50_000            # hard cap per execute_query
DEFAULT_LIMIT = 500          # when the query spec omits `limit`
DEFAULT_DURATION_MINS = 60   # default time window for activity tables
HTTP_TIMEOUT = 120           # seconds per request
FLOW_TIER_LIMIT = 200        # max tiers browsed per app when deriving flows
SUMMARY_APP_LIMIT = 25       # apps named in catalog descriptions
SUMMARY_EDGE_LIMIT = 60      # flow edges embedded in the catalog description
TOKEN_EXPIRY_SLACK = 30      # refresh OAuth tokens this many seconds early
APP_CACHE_TTL = 30           # seconds to reuse the applications list — every
                             # execute_query resolves apps first, and topology
                             # does not churn at sub-minute granularity

# Matches "Call-HTTP to Discovered backend call \"X\"", "Call-JDBC to X", etc.
_EXTERNAL_CALL_RE = re.compile(
    r"^(?:Call-)?(?P<exit>[A-Za-z0-9_. ]+?)\s+to\s+"
    r"(?:Discovered backend call\s+)?"
    r"[\"']?(?P<target>.+?)[\"']?$"
)

# Event types that exist on every 21.x controller and cover the common
# "what happened" questions. The API rejects calls without event-types.
# NB: verified against a live 26.7 controller — the deploy-marker type is
# APPLICATION_DEPLOYMENT; plain "DEPLOYMENT" is a 400.
DEFAULT_EVENT_TYPES = (
    "APPLICATION_ERROR,APPLICATION_CONFIG_CHANGE,APP_SERVER_RESTART,"
    "DIAGNOSTIC_SESSION,POLICY_OPEN_WARNING,POLICY_OPEN_CRITICAL,"
    "POLICY_CLOSE_WARNING,POLICY_CLOSE_CRITICAL,APPLICATION_DEPLOYMENT"
)
DEFAULT_EVENT_SEVERITIES = "INFO,WARN,ERROR"


# ── virtual table catalog ─────────────────────────────────────────────────────
#
# Topology tables map to entity-list endpoints (cheap, enumerable). Activity
# tables are time-bounded and only fetched at query time. `application` names
# thread the graph together (AppD scopes almost everything per application).
_CATALOG = {
    "applications": {
        "pk": "id",
        "columns": [("id", "int"), ("name", "str"), ("description", "str")],
        "fks": [],
        "desc": "Monitored business applications (the top-level unit in AppDynamics).",
    },
    "tiers": {
        "pk": "id",
        "columns": [
            ("id", "int"), ("name", "str"), ("type", "str"),
            ("agentType", "str"), ("numberOfNodes", "int"),
            ("application_id", "int"), ("application_name", "str"),
        ],
        "fks": [("application_id", "applications", "id")],
        "desc": "Service tiers (deployment units) within each application — the nodes of the service map.",
    },
    "nodes": {
        "pk": "id",
        "columns": [
            ("id", "int"), ("name", "str"), ("tierId", "int"), ("tierName", "str"),
            ("machineName", "str"), ("agentType", "str"), ("appAgentVersion", "str"),
            ("application_id", "int"), ("application_name", "str"),
        ],
        "fks": [("tierId", "tiers", "id"), ("application_id", "applications", "id")],
        "desc": "Agent-instrumented processes (JVMs/CLRs/...) grouped under tiers.",
    },
    "backends": {
        "pk": "id",
        "columns": [
            ("id", "int"), ("name", "str"), ("exitPointType", "str"),
            ("application_id", "int"), ("application_name", "str"),
        ],
        "fks": [("application_id", "applications", "id")],
        "desc": "Discovered external dependencies (databases, queues, remote HTTP services) — exit points of the service map.",
    },
    "business_transactions": {
        "pk": "id",
        "columns": [
            ("id", "int"), ("name", "str"), ("tierName", "str"),
            ("entryPointType", "str"), ("internalName", "str"),
            ("application_id", "int"), ("application_name", "str"),
        ],
        "fks": [("application_id", "applications", "id")],
        "desc": "Named user-facing operations (e.g. /checkout) with the tier that serves them.",
    },
    "service_flows": {
        "pk": None,
        "columns": [
            ("from_tier", "str"), ("to_target", "str"), ("to_type", "str"),
            ("exit_type", "str"), ("metric_name", "str"),
            ("application_id", "int"), ("application_name", "str"),
        ],
        "fks": [("application_id", "applications", "id")],
        "desc": ("THE SERVICE MAP as an edge list: which tier calls which tier/backend, "
                 "derived from External Calls metric names. to_type is 'tier' when "
                 "to_target matches a tier name in the same application, else 'backend'. "
                 "Join from_tier/to_target with tiers and backends to build the full topology graph."),
    },
    "metric_data": {
        "pk": None,
        "columns": [
            ("metricId", "int"), ("metricName", "str"), ("metricPath", "str"),
            ("frequency", "str"), ("startTimeInMillis", "int"),
            ("value", "float"), ("min", "float"), ("max", "float"),
            ("sum", "float"), ("count", "int"), ("application_name", "str"),
        ],
        "fks": [],
        "desc": ("Time-series metric datapoints. Query with a metric_path (wildcards '*' "
                 "allowed per pipe-delimited segment) and a time range. rollup=true returns "
                 "one aggregated row per metric instead of the series."),
    },
    "events": {
        "pk": "id",
        "columns": [
            ("id", "int"), ("type", "str"), ("severity", "str"),
            ("summary", "str"), ("eventTime", "int"),
            ("application_name", "str"),
        ],
        "fks": [],
        "desc": ("Controller events (errors, config changes, restarts, policy/health-rule "
                 "state changes, deployments). eventTime is epoch millis."),
    },
    "health_rule_violations": {
        "pk": "id",
        "columns": [
            ("id", "int"), ("name", "str"), ("severity", "str"),
            ("incidentStatus", "str"), ("affectedEntityType", "str"),
            ("affectedEntityName", "str"), ("startTimeInMillis", "int"),
            ("endTimeInMillis", "int"), ("application_name", "str"),
        ],
        "fks": [],
        "desc": ("Health-rule violations (open and resolved). endTimeInMillis is -1 while "
                 "the violation is still open."),
    },
    "snapshots": {
        "pk": None,
        "columns": [
            ("requestGUID", "str"), ("businessTransaction", "str"),
            ("applicationComponent", "str"), ("applicationComponentNode", "str"),
            ("userExperience", "str"), ("timeTakenInMilliSecs", "int"),
            ("errorOccured", "bool"), ("localStartTime", "int"),
            ("application_name", "str"),
        ],
        "fks": [],
        "desc": ("Captured request snapshots for slow/error transactions. userExperience is "
                 "NORMAL/SLOW/VERY_SLOW/ERROR/STALL. applicationComponent is the tier. "
                 "NB the API spells errorOccured with one 'r'."),
    },
}


class AppDynamicsClient(DataSourceClient):

    def __init__(
        self,
        controller_url: str,
        account_name: str = "customer1",
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_name: Optional[str] = None,
        client_secret: Optional[str] = None,
        verify_ssl: bool = True,
    ):
        # Accept a bare host, host:port, or a full URL, with or without the
        # trailing /controller. Normalize to "https://host[:port]".
        base = (controller_url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        if base.endswith("/controller"):
            base = base[: -len("/controller")]
        self.base_url = base
        self.account_name = (account_name or "customer1").strip()
        self.username = username or None
        self.password = password or None
        self.client_name = client_name or None
        self.client_secret = client_secret or None
        self.verify_ssl = verify_ssl
        # OAuth token cache (api_client auth only).
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        # Shared session for HTTP keep-alive: schema discovery makes many
        # sequential calls, and without connection reuse each one pays a full
        # TCP+TLS handshake (~700ms against a SaaS controller).
        self._session: Optional[requests.Session] = None
        # Short-TTL applications-list cache (see APP_CACHE_TTL).
        self._apps_cache: Optional[List[dict]] = None
        self._apps_cache_at: float = 0.0

    def _http(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    @property
    def description(self):
        text = ("AppDynamics client — query APM topology (applications, tiers, nodes, "
                "backends, business transactions), the service map (service_flows), "
                "metrics, events, health-rule violations and request snapshots via the "
                "Controller REST API.")
        return text + "\n\n" + self.system_prompt()

    # ── auth ──────────────────────────────────────────────────────────────────

    @property
    def _basic_login(self) -> Optional[str]:
        """Effective Basic-auth login: append @account unless the username
        already carries an account qualifier."""
        if not self.username:
            return None
        if "@" in self.username:
            return self.username
        return f"{self.username}@{self.account_name}"

    def _fetch_token(self) -> str:
        url = f"{self.base_url}/controller/api/oauth/access_token"
        try:
            resp = self._http().post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": f"{self.client_name}@{self.account_name}",
                    "client_secret": self.client_secret or "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                verify=self.verify_ssl,
                timeout=HTTP_TIMEOUT,
            )
        except requests.exceptions.SSLError as e:
            raise RuntimeError(
                f"AppDynamics TLS error: {e}. Enable 'Skip TLS verification' for "
                "self-signed or private-CA controllers."
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"AppDynamics connection failed to {url}: {e}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"AppDynamics OAuth token request failed ({resp.status_code}): "
                f"{resp.text[:300]}. Check the API client name/secret and account name."
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("AppDynamics OAuth response had no access_token.")
        self._token = token
        self._token_expiry = time.time() + int(body.get("expires_in", 300)) - TOKEN_EXPIRY_SLACK
        return token

    def _request_kwargs(self) -> dict:
        """Auth material for one request: Basic tuple or Bearer header."""
        if self.username and self.password:
            return {"auth": (self._basic_login, self.password)}
        if self.client_name and self.client_secret:
            if not self._token or time.time() >= self._token_expiry:
                self._fetch_token()
            return {"headers": {"Authorization": f"Bearer {self._token}"}}
        raise RuntimeError(
            "AppDynamics requires either username/password or an API client "
            "name/secret. None were provided."
        )

    # ── transport ─────────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> object:
        """One authenticated GET, JSON-decoded. Retries once on 401 for the
        OAuth variant (expired token); never retries Basic auth (a bad
        password must not trip the controller's lockout)."""
        url = f"{self.base_url}/controller/{path.lstrip('/')}"
        q = dict(params or {})
        q["output"] = "JSON"
        for attempt in (0, 1):
            kwargs = self._request_kwargs()
            try:
                resp = self._http().get(url, params=q, verify=self.verify_ssl,
                                        timeout=HTTP_TIMEOUT, **kwargs)
            except requests.exceptions.SSLError as e:
                raise RuntimeError(
                    f"AppDynamics TLS error: {e}. Enable 'Skip TLS verification' "
                    "for self-signed or private-CA controllers."
                )
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"AppDynamics connection failed to {url}: {e}")

            if resp.status_code == 401:
                # OAuth: token may have just expired — refresh once. Basic:
                # fail immediately (retrying a wrong password risks lockout).
                if attempt == 0 and self.client_name and not self.username:
                    self._token = None
                    continue
                raise RuntimeError(
                    "AppDynamics authentication failed (401). Check the username "
                    f"('{self._basic_login}' — the account name is appended "
                    "automatically unless the username contains '@') and password."
                    if self.username else
                    "AppDynamics authentication failed (401) for the API client."
                )
            if resp.status_code == 403:
                raise RuntimeError(
                    "AppDynamics authorization failed (403): the account lacks "
                    "view permission on this resource. Grant a read-only role "
                    "such as Applications & Dashboards Viewer."
                )
            if resp.status_code == 404:
                raise RuntimeError(
                    f"AppDynamics resource not found (404) at /controller/{path}: "
                    "check the Controller URL and that the application exists."
                )
            if resp.status_code == 429:
                raise RuntimeError(
                    "AppDynamics rate limit hit (429). Narrow the metric path or "
                    "time range, or retry later."
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"AppDynamics HTTP error ({resp.status_code}): {resp.text[:300]}"
                )
            try:
                return resp.json()
            except ValueError:
                raise RuntimeError(
                    f"AppDynamics returned non-JSON (is output=JSON being dropped "
                    f"by a proxy?): {resp.text[:300]}"
                )

    def test_connection(self):
        try:
            apps = self._get("rest/applications") or []
            # Visibility check: an account with no application grants
            # authenticates fine but sees an empty world — say so here
            # instead of failing silently at query time.
            if not apps:
                return {
                    "success": True,
                    "message": (
                        "Connected to the AppDynamics controller, but 0 applications "
                        "are visible to this account — all queries will return empty "
                        "results. Grant the account a view role (e.g. Applications & "
                        "Dashboards Viewer) on the relevant applications."
                    ),
                }
            return {
                "success": True,
                "message": (
                    f"Connected to the AppDynamics controller "
                    f"({len(apps)} applications visible)"
                ),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── schema discovery ──────────────────────────────────────────────────────

    def get_schemas(self) -> List[Table]:
        tables = [self._build_table(name) for name in _CATALOG]
        # Best-effort enrichment: put the real estate into the catalog so the
        # agent sees actual app/tier names and the observed service map without
        # issuing a single query. Never fail discovery on this.
        try:
            apps = self._list_applications()
            by_name = {t.name: t for t in tables}
            if apps:
                names = ", ".join(a["name"] for a in apps[:SUMMARY_APP_LIMIT])
                more = f" (+{len(apps) - SUMMARY_APP_LIMIT} more)" if len(apps) > SUMMARY_APP_LIMIT else ""
                by_name["applications"].description += f" Present: {names}{more}."
            edges: List[dict] = []
            tier_names: List[str] = []
            for app in apps:
                tiers = self._get(f"rest/applications/{app['id']}/tiers") or []
                tier_names.extend(f"{app['name']}/{t['name']}" for t in tiers)
                edges.extend(self._flows_for_app(app, tiers))
            if tier_names:
                shown = ", ".join(tier_names[:SUMMARY_APP_LIMIT])
                more = f" (+{len(tier_names) - SUMMARY_APP_LIMIT} more)" if len(tier_names) > SUMMARY_APP_LIMIT else ""
                by_name["tiers"].description += f" Present (app/tier): {shown}{more}."
            if edges:
                lines = [
                    f"{e['application_name']}: {e['from_tier']} -[{e['exit_type']}]-> {e['to_target']}"
                    for e in edges[:SUMMARY_EDGE_LIMIT]
                ]
                more = f" (+{len(edges) - SUMMARY_EDGE_LIMIT} more edges)" if len(edges) > SUMMARY_EDGE_LIMIT else ""
                by_name["service_flows"].description += (
                    " Observed flows: " + "; ".join(lines) + f".{more}"
                )
        except Exception:
            pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        if table_name not in _CATALOG:
            raise ValueError(
                f"Unknown AppDynamics table '{table_name}'. Available: {', '.join(_CATALOG)}."
            )
        return self._build_table(table_name)

    def _build_table(self, name: str) -> Table:
        spec = _CATALOG[name]
        columns = [TableColumn(name=c, dtype=d) for c, d in spec["columns"]]
        by_name = {c.name: c for c in columns}
        fks = [
            ForeignKey(
                column=by_name[col],
                references_name=ref_table,
                references_column=TableColumn(name=ref_col, dtype="int"),
            )
            for col, ref_table, ref_col in spec["fks"]
            if col in by_name
        ]
        pks = [by_name[spec["pk"]]] if spec.get("pk") and spec["pk"] in by_name else []
        return Table(name=name, description=spec["desc"], columns=columns, pks=pks, fks=fks)

    # ── querying ──────────────────────────────────────────────────────────────

    def execute_query(self, query) -> pd.DataFrame:
        """Execute an AppDynamics query spec and return a DataFrame.

        `query` is a JSON string (or dict):
            {"table": "metric_data",                       (required)
             "application": "retail-banking",              (name or id; omit to span all apps)
             "metric_path": "Business Transaction Performance|*|*|Average Response Time (ms)",
             "duration_in_mins": 60,                       (or start_time/end_time epoch ms)
             "rollup": false,
             "params": {...},                              (extra query params, passed through)
             "limit": 500}
        """
        spec = self._parse_spec(query)
        table = spec["table"]
        limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)

        if table == "applications":
            rows = self._list_applications()
            return pd.DataFrame(rows[:limit])

        apps = self._resolve_apps(spec.get("application"))

        if table in ("tiers", "nodes", "backends", "business_transactions"):
            path = {"tiers": "tiers", "nodes": "nodes", "backends": "backends",
                    "business_transactions": "business-transactions"}[table]
            rows = []
            for app in apps:
                for row in (self._get(f"rest/applications/{app['id']}/{path}") or []):
                    row["application_id"] = app["id"]
                    row["application_name"] = app["name"]
                    rows.append(row)
            return pd.DataFrame(rows[:limit])

        if table == "service_flows":
            rows = []
            for app in apps:
                tiers = self._get(f"rest/applications/{app['id']}/tiers") or []
                rows.extend(self._flows_for_app(app, tiers))
            return pd.DataFrame(rows[:limit])

        if table == "metric_browse":
            rows = []
            for app in apps:
                params = {"metric-path": spec.get("metric_path") or ""}
                for row in (self._get(f"rest/applications/{app['id']}/metrics", params) or []):
                    row["application_name"] = app["name"]
                    rows.append(row)
            return pd.DataFrame(rows[:limit])

        time_params = self._time_params(spec)

        if table == "metric_data":
            metric_path = spec.get("metric_path")
            if not metric_path:
                raise ValueError(
                    'metric_data requires "metric_path", e.g. '
                    '"Business Transaction Performance|*|*|Average Response Time (ms)".'
                )
            rows = []
            for app in apps:
                params = {
                    "metric-path": metric_path,
                    "rollup": "true" if spec.get("rollup") else "false",
                    **time_params,
                    **(spec.get("params") or {}),
                }
                for series in (self._get(f"rest/applications/{app['id']}/metric-data", params) or []):
                    values = series.get("metricValues") or []
                    meta = {
                        "metricId": series.get("metricId"),
                        "metricName": series.get("metricName"),
                        "metricPath": series.get("metricPath"),
                        "frequency": series.get("frequency"),
                        "application_name": app["name"],
                    }
                    for v in values:
                        rows.append({**meta, **v})
            df = pd.DataFrame(rows[:limit])
            return df

        if table == "events":
            rows = []
            for app in apps:
                params = {
                    "event-types": spec.get("event_types") or DEFAULT_EVENT_TYPES,
                    "severities": spec.get("severities") or DEFAULT_EVENT_SEVERITIES,
                    **time_params,
                    **(spec.get("params") or {}),
                }
                for row in (self._get(f"rest/applications/{app['id']}/events", params) or []):
                    row["application_name"] = app["name"]
                    rows.append(row)
            return pd.DataFrame(rows[:limit])

        if table == "health_rule_violations":
            rows = []
            for app in apps:
                params = {**time_params, **(spec.get("params") or {})}
                for row in (self._get(
                    f"rest/applications/{app['id']}/problems/healthrule-violations", params
                ) or []):
                    row["application_name"] = app["name"]
                    rows.append(row)
            return pd.DataFrame(rows[:limit])

        if table == "snapshots":
            rows = []
            for app in apps:
                params = {**time_params, **(spec.get("params") or {})}
                if spec.get("first_in_chain") is not False:
                    params.setdefault("first-in-chain", "true")
                for row in (self._get(
                    f"rest/applications/{app['id']}/request-snapshots", params
                ) or []):
                    row["application_name"] = app["name"]
                    rows.append(row)
            return pd.DataFrame(rows[:limit])

        raise ValueError(f"Unhandled AppDynamics table '{table}'.")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _list_applications(self) -> List[dict]:
        if self._apps_cache is not None and time.time() - self._apps_cache_at < APP_CACHE_TTL:
            return self._apps_cache
        apps = list(self._get("rest/applications") or [])
        self._apps_cache = apps
        self._apps_cache_at = time.time()
        return apps

    def _resolve_apps(self, application) -> List[dict]:
        """Resolve the spec's `application` (name or id) to app dicts; when
        omitted, span every visible application (the all-services case)."""
        apps = self._list_applications()
        if application in (None, "", "*", "all"):
            return apps
        wanted = str(application)
        for app in apps:
            if str(app.get("id")) == wanted or app.get("name") == wanted:
                return [app]
        raise ValueError(
            f"AppDynamics application '{application}' not found. Visible: "
            f"{', '.join(a['name'] for a in apps[:25])}"
        )

    def _time_params(self, spec: dict) -> Dict[str, str]:
        start = spec.get("start_time")
        end = spec.get("end_time")
        if start and end:
            return {
                "time-range-type": "BETWEEN_TIMES",
                "start-time": str(int(start)),
                "end-time": str(int(end)),
            }
        duration = int(spec.get("duration_in_mins") or DEFAULT_DURATION_MINS)
        return {
            "time-range-type": "BEFORE_NOW",
            "duration-in-mins": str(duration),
        }

    def _flows_for_app(self, app: dict, tiers: List[dict]) -> List[dict]:
        """Derive service-map edges for one app from External Calls metric names."""
        edges: List[dict] = []
        tier_names = {t["name"] for t in tiers}
        for tier in tiers[:FLOW_TIER_LIMIT]:
            path = f"Overall Application Performance|{tier['name']}|External Calls"
            try:
                children = self._get(
                    f"rest/applications/{app['id']}/metrics", {"metric-path": path}
                ) or []
            except RuntimeError:
                continue  # tier without external calls (or no branch) — skip
            for child in children:
                name = child.get("name") or ""
                exit_type, target = self._parse_external_call(name)
                if not target:
                    continue
                edges.append({
                    "from_tier": tier["name"],
                    "to_target": target,
                    "to_type": "tier" if target in tier_names else "backend",
                    "exit_type": exit_type,
                    "metric_name": name,
                    "application_id": app["id"],
                    "application_name": app["name"],
                })
        return edges

    @staticmethod
    def _parse_external_call(name: str) -> Tuple[str, Optional[str]]:
        """'Call-HTTP to Discovered backend call "X"' → ("HTTP", "X")."""
        m = _EXTERNAL_CALL_RE.match(name.strip())
        if not m:
            return ("", None)
        exit_type = m.group("exit").strip()
        if exit_type.lower().startswith("call-"):
            exit_type = exit_type[5:]
        return (exit_type, m.group("target").strip())

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = query
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError(
                    "AppDynamics query must be a JSON object like "
                    '{"table": "metric_data", "application": "myapp", '
                    '"metric_path": "...", "duration_in_mins": 60} — got: '
                    f"{str(query)[:200]}"
                )
        if not isinstance(spec, dict) or not spec.get("table"):
            raise ValueError('AppDynamics query spec must include a "table" key.')
        known = set(_CATALOG) | {"metric_browse"}
        if spec["table"] not in known:
            raise ValueError(
                f"Unknown AppDynamics table '{spec['table']}'. Available: "
                f"{', '.join(sorted(known))}."
            )
        return spec

    # ── prompts ───────────────────────────────────────────────────────────────

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self):
        text = """
        ## AppDynamics Integration
        Query AppDynamics via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "metric_data",
         "application": "retail-banking",
         "metric_path": "Business Transaction Performance|*|*|Average Response Time (ms)",
         "duration_in_mins": 60,
         "rollup": false,
         "limit": 500}
        ```

        - `table` (required): one of applications, tiers, nodes, backends,
          business_transactions, service_flows, metric_data, events,
          health_rule_violations, snapshots — plus metric_browse to explore
          the metric tree.
        - `application` (optional): app name or id. OMIT it to span every
          visible application (use this for estate-wide questions and the
          full service map).
        - Time ranges: `duration_in_mins` (default 60, relative to now) or
          `start_time`/`end_time` in EPOCH MILLISECONDS. Timestamps in
          results (eventTime, startTimeInMillis, ...) are epoch millis:
          convert with `pd.to_datetime(df[col], unit="ms")`.
        - `limit` (optional): max rows, default 500.

        THE SERVICE MAP / topology:
        - `service_flows` returns the dependency edge list: from_tier →
          to_target with exit_type (HTTP/JDBC/JMS/...) and to_type
          ('tier' = internal call to another tier, 'backend' = external
          dependency). Build the topology by combining `tiers` (graph nodes)
          + `backends` (leaf nodes) + `service_flows` (edges); aggregate and
          join in pandas. Omit `application` to map the whole estate.
        - An edge appears only if calls actually occurred in the metric
          retention window (observed topology, like AppD's own flow map).

        METRICS (`metric_data`):
        - `metric_path` is pipe-delimited; '*' wildcards one segment. The
          most useful roots:
          - `Business Transaction Performance|Business Transactions|<tier>|<bt>|<metric>`
            with metrics like `Average Response Time (ms)`, `Calls per Minute`,
            `Errors per Minute` (wildcard tier/bt: `...|Business Transactions|*|*|Calls per Minute`)
          - `Overall Application Performance|<tier>|<metric>` — per-tier totals
          - `Overall Application Performance|<tier>|External Calls|<edge>|<metric>` —
            per-dependency traffic (edge weights for the service map)
          - `Application Infrastructure Performance|<tier>|Hardware Resources|CPU|%Busy`
        - `rollup=true` → one aggregated row per metric; `rollup=false` →
          the time series.
        - Use `metric_browse` with a partial `metric_path` to discover what
          exists under a branch.

        EVENTS / HEALTH:
        - `events` needs `event_types` + `severities` (sensible defaults are
          applied). severity values: INFO, WARN, ERROR.
        - `health_rule_violations` returns open + resolved incidents in the
          window; endTimeInMillis is -1 while still open.
        - `snapshots` returns slow/error request captures (userExperience:
          NORMAL/SLOW/VERY_SLOW/ERROR/STALL; the error flag is spelled
          `errorOccured`).

        Examples:
        ```python
        # Full-estate service map (all applications)
        df = client.execute_query('{"table": "service_flows"}')
        # Slowest business transactions in one app, last hour
        df = client.execute_query('{"table": "metric_data", "application": "retail-banking", "metric_path": "Business Transaction Performance|Business Transactions|*|*|Average Response Time (ms)", "duration_in_mins": 60, "rollup": true}')
        # Open health-rule violations across the estate, last 24h
        df = client.execute_query('{"table": "health_rule_violations", "duration_in_mins": 1440}')
        ```
        Aggregate by fetching rows and grouping in pandas.
        """
        return text


# Alias so dynamic naming and the explicit client_path both resolve.
AppDynamicsClient = AppDynamicsClient
