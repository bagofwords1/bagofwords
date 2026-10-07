"""VMware Aria Operations (formerly vRealize Operations / vROps; VCF Operations
in 9.x) data source client.

Talks to the Suite API (`https://<appliance>/suite-api/api/...`). Validated
against the official OpenAPI spec (`vmware/vcf-api-specs`, VCF Operations 9.1
— the same surface Aria Operations 8.18 serves) and the 8.18 API Programming
Guide. There is no free-form query language: queries are JSON specs (see
`system_prompt`) that map to a catalog of virtual tables.

Two kinds of table:
  - FIXED tables declared in code (`_CATALOG`): the dictionary (adapter kinds,
    resource kinds, stat keys), inventory (resources, properties,
    relationships), time series (metrics, metrics_latest, metrics_topn) and
    the incident timeline (alerts, symptoms, contributing_symptoms,
    alert_definitions, custom_groups).
  - DISCOVERED tables, one per *populated* resource kind, named
    `metrics::<AdapterKind>/<ResourceKind>` with the kind's stat keys as
    columns. This is how a storage management pack (Hitachi, NetApp, IBM …)
    becomes queryable with zero vendor-specific code: whatever adapters the
    customer installed show up as wide, per-kind metric tables.

Why no `events` table: the public API only lets you PUSH events
(`POST /events`); there is no read endpoint. Alerts + symptoms (with start /
update / cancel timestamps) ARE the incident timeline.

RCA signal worth knowing: `stats/query` accepts `dt=true` and then returns
Aria's learned dynamic-threshold band (`minThresholdData` /
`maxThresholdData`) next to each series — "was this abnormal?" becomes a
column (`dt_min`, `dt_max`) rather than a judgement call.

Auth is Aria-native (8.18 guide, "Acquire an Authentication Token"):
  POST /suite-api/api/auth/token/acquire {username, password, authSource}
  → `Authorization: OpsToken <token>` (the legacy `vRealizeOpsToken` header
  is still accepted; we send the current one). Tokens live six hours,
  sliding; cached here and re-acquired on 401. `authSource` is `LOCAL` or
  the name of a configured LDAP/AD/vIDM source. Basic auth is deprecated and
  disabled by default on 8.18, so it is not offered.

Every request sends `Accept: application/json` — the API's historic default
is XML, and XML is deprecated for the next major release.
"""
from app.data_sources.clients.progress import discovery_progress, IndexingCancelled
import json
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd
import requests

from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, ServiceFormatter


_QUERY_DEADLINE = ContextVar("aria_query_deadline", default=None)
QUERY_BUDGET_SECONDS = 150

MAX_ROWS = 50_000             # hard cap per execute_query
DEFAULT_LIMIT = 500           # when the query spec omits `limit`
DEFAULT_DURATION_MINS = 60    # default time window for activity tables
HTTP_TIMEOUT = 120            # seconds per request
PAGE_SIZE = 1000              # the API's maximum page size
ID_BATCH = 1000               # max resourceIds per stats call (API limit)
MAX_PAGES = 200               # safety cap on pagination loops
TOKEN_EXPIRY_SLACK = 60       # re-acquire this many seconds before expiry
TOKEN_TTL_FALLBACK = 6 * 3600 # documented token lifetime when the response lacks `validity`
MAX_DEPTH = 4                 # relationship BFS cap
DEFAULT_METRIC_TABLES = 40    # discovered metrics:: tables emitted by default
SUMMARY_KIND_LIMIT = 30       # resource kinds named in catalog descriptions
METRIC_TABLE_PREFIX = "metrics::"

# Adapter kinds that exist on every instance and only describe Aria itself —
# never worth a metrics:: table; they would crowd out the customer's estate.
_INTERNAL_ADAPTER_KINDS = {
    "VMWARE_ARIA_OPERATIONS", "vRealizeOpsMgrAPI", "Container", "VirtualAndPhysicalSANAdapter",
    "vCenter Operations Adapter", "Alert Adapter", "OPENAPI", "SDDCHealthAdapter",
}
# Resource-kind types that are containers/adapter instances, not monitored objects.
_SKIP_RESOURCE_KIND_TYPES = {"ADAPTER_INSTANCE", "GROUP", "TAG", "BUSINESS_SERVICE", "TIER"}


# ── fixed virtual-table catalog ───────────────────────────────────────────────
#
# Columns are the flattened API fields (camelCase kept where the API uses it
# so results match the vendor docs). `fks` wire the graph so the planner
# understands resource ⇄ alert ⇄ definition relationships.
_CATALOG: Dict[str, dict] = {
    "adapter_kinds": {
        "pk": "key",
        "columns": [("key", "str"), ("name", "str"), ("description", "str"),
                    ("adapterKindType", "str"), ("resourceKindCount", "int")],
        "fks": [],
        "desc": ("Installed adapters / management packs (VMWARE = vCenter; storage vendors, NSX, "
                 "etc. appear here when their pack is installed). Start here to learn what this "
                 "instance monitors."),
    },
    "resource_kinds": {
        "pk": "key",
        "columns": [("adapterKind", "str"), ("key", "str"), ("name", "str"),
                    ("resourceKindType", "str"), ("resourceCount", "int")],
        "fks": [("adapterKind", "adapter_kinds", "key")],
        "desc": ("Object types per adapter (VirtualMachine, HostSystem, Datastore, storage Pool, LDEV …) "
                 "with how many objects of each exist. Filter with `adapter_kind`."),
    },
    "stat_keys": {
        "pk": None,
        "columns": [("adapterKind", "str"), ("resourceKind", "str"), ("key", "str"), ("name", "str"),
                    ("unit", "str"), ("rollupType", "str"), ("dataType", "str"), ("description", "str")],
        "fks": [("adapterKind", "adapter_kinds", "key")],
        "desc": ("The metric dictionary: every stat key an object type can report, with unit. "
                 "Requires `adapter_kind` + `resource_kind`. Use it to find the exact `stat_key` "
                 "strings for `metrics` (e.g. VMWARE/VirtualMachine → virtualDisk|totalLatency)."),
    },
    "resources": {
        "pk": "id",
        "columns": [("id", "str"), ("name", "str"), ("adapterKind", "str"), ("resourceKind", "str"),
                    ("health", "str"), ("healthValue", "float"), ("resourceStatus", "str"),
                    ("resourceState", "str"), ("badges", "str"), ("creationTime", "int")],
        "fks": [("resourceKind", "resource_kinds", "key")],
        "desc": ("Inventory of monitored objects. Filter by `name` (exact, list ok), `regex`, "
                 "`adapter_kind`, `resource_kind`, `parent_id`, `health` (GREEN/YELLOW/ORANGE/RED/GREY) "
                 "or `property` {key, operator, value}. Returns the resource `id` needed everywhere else."),
    },
    "properties": {
        "pk": None,
        "columns": [("resourceId", "str"), ("resourceName", "str"), ("name", "str"), ("value", "str")],
        "fks": [("resourceId", "resources", "id")],
        "desc": ("Static attributes of objects (VM instance UUID `config|instanceUuid`, MoRef `summary|MOID`, "
                 "datastore NAA id, array serial, LDEV/pool ids …). These are the JOIN KEYS to a CMDB "
                 "(ServiceNow) and to native storage connectors."),
    },
    "relationships": {
        "pk": None,
        "columns": [("parentId", "str"), ("parentName", "str"), ("parentKind", "str"),
                    ("childId", "str"), ("childName", "str"), ("childKind", "str"), ("depth", "int")],
        "fks": [("parentId", "resources", "id"), ("childId", "resources", "id")],
        "desc": ("THE TOPOLOGY as an edge list around one object: vCenter → Datacenter → Cluster → Host → VM, "
                 "VM/Host → Datastore, and (with a storage pack) Datastore → LUN/LDEV → Pool → Array. "
                 "`depth` walks further (default 1, max 4). This is the RCA graph."),
    },
    "metrics": {
        "pk": None,
        "columns": [("resourceId", "str"), ("resourceName", "str"), ("resourceKind", "str"),
                    ("statKey", "str"), ("timestamp", "int"), ("value", "float"),
                    ("dt_min", "float"), ("dt_max", "float")],
        "fks": [("resourceId", "resources", "id")],
        "desc": ("Time series for any objects × stat keys over a window (epoch ms). `rollup` "
                 "(AVG/MIN/MAX/SUM/LATEST/COUNT) + `interval`/`interval_quantifier` downsample. "
                 "`dt: true` adds Aria's dynamic-threshold NORMAL BAND as dt_min/dt_max — a value "
                 "outside the band is abnormal by Aria's own baseline."),
    },
    "metrics_latest": {
        "pk": None,
        "columns": [("resourceId", "str"), ("resourceName", "str"), ("statKey", "str"),
                    ("timestamp", "int"), ("value", "float")],
        "fks": [("resourceId", "resources", "id")],
        "desc": "Most recent sample(s) per object × stat key. For 'what is X right now'.",
    },
    "metrics_topn": {
        "pk": None,
        "columns": [("rank", "int"), ("statKey", "str"), ("resourceId", "str"), ("resourceName", "str"),
                    ("value", "float"), ("timestamp", "int")],
        "fks": [("resourceId", "resources", "id")],
        "desc": ("Top-N objects by a stat over a window (e.g. the 10 datastores with the worst latency). "
                 "Give `stat_key` + a scope (`resource_kind`, or `resource_id` list) + `top_n`."),
    },
    "alerts": {
        "pk": "alertId",
        "columns": [("alertId", "str"), ("resourceId", "str"), ("resourceName", "str"), ("resourceKind", "str"),
                    ("alertDefinitionId", "str"), ("alertDefinitionName", "str"), ("alertLevel", "str"),
                    ("status", "str"), ("controlState", "str"), ("alertImpact", "str"),
                    ("startTimeUTC", "int"), ("updateTimeUTC", "int"), ("cancelTimeUTC", "int")],
        "fks": [("resourceId", "resources", "id"), ("alertDefinitionId", "alert_definitions", "id")],
        "desc": ("Alerts (open and historical) — THE incident timeline. alertLevel INFORMATION/WARNING/"
                 "IMMEDIATE/CRITICAL; status NEW/ACTIVE/UPDATED/CANCELED; cancelTimeUTC is 0 while open. "
                 "Windows default to start time; use time_mode=overlap for incidents opened earlier. "
                 "`active_only` without a window means open now."),
    },
    "contributing_symptoms": {
        "pk": None,
        "columns": [("alertId", "str"), ("symptomId", "str"), ("symptomDefinitionId", "str"),
                    ("severity", "str"), ("symptomDefinitionsIds", "str"), ("alertConditions", "str")],
        "fks": [("alertId", "alerts", "alertId"), ("symptomId", "symptoms", "id")],
        "desc": "SELF-defined contributing symptoms (give `alert_id` list). JSON columns preserve all definitions and conditions; scalar fields summarize the first. Empty results do not exclude non-SELF contributors.",
    },
    "symptoms": {
        "pk": "id",
        "columns": [("id", "str"), ("resourceId", "str"), ("resourceName", "str"), ("symptomDefinitionId", "str"),
                    ("symptomCriticality", "str"), ("message", "str"), ("statKey", "str"), ("kpi", "bool"),
                    ("startTimeUTC", "int"), ("updateTimeUTC", "int"), ("cancelTimeUTC", "int")],
        "fks": [("resourceId", "resources", "id")],
        "desc": ("Triggered symptoms (finer than alerts): the exact condition, the metric (`statKey`) and "
                 "an event message when available. Numeric threshold details are not guaranteed."),
    },
    "alert_definitions": {
        "pk": "id",
        "columns": [("id", "str"), ("name", "str"), ("description", "str"), ("adapterKindKey", "str"),
                    ("resourceKindKey", "str"), ("severity", "str"), ("waitCycles", "int"), ("cancelCycles", "int"), ("states", "str")],
        "fks": [("adapterKindKey", "adapter_kinds", "key")],
        "desc": ("The operators' own alert rules per object type — what THEY consider abnormal. Read these "
                 "before judging a metric. Filter with `adapter_kind` / `resource_kind`."),
    },
    "custom_groups": {
        "pk": None,
        "columns": [("groupId", "str"), ("groupName", "str"), ("policy", "str"),
                    ("memberId", "str"), ("memberName", "str"), ("memberKind", "str")],
        "fks": [("memberId", "resources", "id")],
        "desc": "Operator-defined groups (e.g. 'Production Databases') and their member objects.",
    },
}

_ACTIVITY_TABLES = {"metrics", "metrics_topn", "alerts", "symptoms"}


class AriaOperationsClient(DataSourceClient):

    def __init__(
        self,
        url: str,
        auth_source: str = "LOCAL",
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        history_window_days: int = 7,
        max_metric_tables: int = DEFAULT_METRIC_TABLES,
    ):
        base = (url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        for suffix in ("/suite-api/api", "/suite-api", "/api"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.base_url = base
        self.auth_source = (auth_source or "LOCAL").strip() or "LOCAL"
        self.username = username or None
        self.password = password or None
        self.verify_ssl = verify_ssl
        self.ca_bundle = (ca_bundle or "").strip() or None
        self.history_window_days = int(history_window_days or 7)
        self.max_metric_tables = int(max_metric_tables or 0)
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._session: Optional[requests.Session] = None
        # Per-instance caches (dictionary calls are cheap but repeated).
        self._kinds_cache: Optional[List[dict]] = None
        self._resource_kinds_cache: Dict[str, List[dict]] = {}
        self._statkeys_cache: Dict[Tuple[str, str], List[dict]] = {}
        self._name_cache: Dict[str, Tuple[str, str]] = {}   # id -> (name, kind)

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _http(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    @property
    def _verify(self):
        # verify_ssl decides whether to verify; a CA bundle path is only the
        # trust store used when it is on.
        if not self.verify_ssl:
            return False
        return self.ca_bundle or True

    @property
    def description(self):
        text = ("VMware Aria Operations client — query the vSphere estate (VMs, hosts, clusters, "
                "datastores) and every installed management pack (storage arrays, NSX …): "
                "inventory, topology/relationships, metrics with history and dynamic thresholds, "
                "alerts and symptoms via the Suite API.")
        return text + "\n\n" + self.system_prompt()

    # ── auth ──────────────────────────────────────────────────────────────────

    def _acquire_token(self) -> str:
        if not (self.username and self.password):
            raise RuntimeError("Aria Operations requires a username and password (plus the auth source name).")
        url = f"{self.base_url}/suite-api/api/auth/token/acquire"
        body = {"username": self.username, "password": self.password, "authSource": self.auth_source}
        try:
            resp = self._http().post(url, json=body, headers={"Accept": "application/json",
                                                             "Content-Type": "application/json"},
                                     verify=self._verify, timeout=self._request_timeout())
        except requests.exceptions.SSLError as e:
            raise RuntimeError(
                f"Aria Operations TLS error: {e}. Provide the CA bundle path or disable 'Verify SSL' "
                "for self-signed / private-CA appliances."
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Aria Operations connection failed to {url}: {e}")
        if resp.status_code == 401:
            raise RuntimeError(
                f"Aria Operations authentication failed (401) for '{self.username}' on auth source "
                f"'{self.auth_source}'. For LDAP/AD accounts set the auth source to the name configured "
                "in Administration → Authentication Sources (default LOCAL)."
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Aria Operations token request failed ({resp.status_code}): {resp.text[:300]}"
            )
        try:
            payload = resp.json()
        except ValueError:
            raise RuntimeError(
                f"Aria Operations token endpoint returned non-JSON: {resp.text[:200]}"
            )
        token = payload.get("token")
        if not token:
            raise RuntimeError("Aria Operations token response had no 'token'.")
        # `validity` is an epoch-ms expiry; fall back to the documented 6h.
        validity = payload.get("validity")
        try:
            expiry = float(validity) / 1000.0 if validity else time.time() + TOKEN_TTL_FALLBACK
        except (TypeError, ValueError):
            expiry = time.time() + TOKEN_TTL_FALLBACK
        if expiry <= time.time():
            expiry = time.time() + TOKEN_TTL_FALLBACK
        self._token = token
        self._token_expiry = expiry - TOKEN_EXPIRY_SLACK
        return token

    def _headers(self) -> dict:
        if not self._token or time.time() >= self._token_expiry:
            self._acquire_token()
        return {"Authorization": f"OpsToken {self._token}",
                "Accept": "application/json", "Content-Type": "application/json"}

    # ── transport ─────────────────────────────────────────────────────────────

    def _request_timeout(self):
        deadline = _QUERY_DEADLINE.get()
        if deadline is None:
            return HTTP_TIMEOUT
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Aria Operations query budget exhausted; narrow the window or resource scope.")
        return min(HTTP_TIMEOUT, remaining)

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None) -> Any:
        """One authenticated call, JSON-decoded. Re-acquires the token once on 401."""
        url = f"{self.base_url}/suite-api/api/{path.lstrip('/')}"
        for attempt in (0, 1):
            headers = self._headers()
            try:
                resp = self._http().request(
                    method, url, params=params, json=body, headers=headers,
                    verify=self._verify, timeout=self._request_timeout(),
                )
            except requests.exceptions.SSLError as e:
                raise RuntimeError(
                    f"Aria Operations TLS error: {e}. Provide the CA bundle path or disable "
                    "'Verify SSL' for self-signed / private-CA appliances."
                )
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"Aria Operations connection failed to {url}: {e}")

            if resp.status_code == 401:
                if attempt == 0:
                    self._token = None       # expired or revoked — one re-acquire
                    continue
                raise RuntimeError("Aria Operations authentication failed (401) after re-acquiring the token.")
            if resp.status_code == 403:
                raise RuntimeError(
                    f"Aria Operations authorization failed (403) for {method} {path}. "
                    "Verify the connection identity and permissions for this endpoint and object scope."
                )
            if resp.status_code == 404:
                raise RuntimeError(
                    f"Aria Operations resource not found (404) at /suite-api/api/{path}: check the "
                    "appliance URL, or the object id."
                )
            if resp.status_code == 429:
                raise RuntimeError(
                    "Aria Operations rate limit hit (429). Narrow the window or the object list and retry."
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"Aria Operations HTTP error ({resp.status_code}): {resp.text[:300]}")
            ctype = (resp.headers.get("Content-Type") or "").lower() if hasattr(resp, "headers") else ""
            if "xml" in ctype:
                raise RuntimeError(
                    "Aria Operations answered XML — a proxy is stripping the Accept: application/json header."
                )
            try:
                return resp.json()
            except ValueError:
                raise RuntimeError(f"Aria Operations returned non-JSON: {resp.text[:300]}")

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict, params: Optional[dict] = None) -> Any:
        return self._request("POST", path, params=params, body=body)

    def _paged(self, method: str, path: str, key: str, params: Optional[dict] = None,
               body: Optional[dict] = None, limit: Optional[int] = None, predicate=None) -> List[dict]:
        """Apply local filters before the result limit; count raw rows for pagination."""
        out: List[dict] = []
        seen = 0
        for page in range(MAX_PAGES):
            q = dict(params or {})
            q["page"] = page
            # Keep the page size stable: changing it changes the server's offset.
            q["pageSize"] = min(PAGE_SIZE, limit) if limit is not None and not predicate else PAGE_SIZE
            payload = self._request(method, path, params=q, body=body) or {}
            if key not in payload or not isinstance(payload[key], list):
                raise RuntimeError(f"Aria Operations {method} {path}: expected a '{key}' list.")
            rows = payload[key]
            seen += len(rows)
            out.extend(r for r in rows if predicate is None or predicate(r))
            info = payload.get("pageInfo") or {}
            total = info.get("totalCount")
            if limit is not None and len(out) >= limit:
                return out[:limit]
            if total is not None and seen >= int(total):
                return out
            if not rows:
                if total is not None and seen < int(total):
                    raise RuntimeError("Aria Operations pagination ended before totalCount was reached.")
                return out
            effective_size = int(info.get("pageSize") or q["pageSize"])
            if total is None and len(rows) < effective_size:
                return out
        raise RuntimeError("Aria Operations pagination safety limit reached; narrow the query scope.")

    # ── connection test ───────────────────────────────────────────────────────

    def test_connection(self):
        try:
            kinds = self._adapter_kinds()
            version = ""
            try:
                v = self._get("versions/current") or {}
                version = v.get("releaseName") or ""
            except IndexingCancelled:
                raise
            except Exception:
                pass
            names = ", ".join(k.get("key", "") for k in kinds[:8])
            more = f" (+{len(kinds) - 8} more)" if len(kinds) > 8 else ""
            return {
                "success": True,
                "message": (
                    f"Connected to Aria Operations{(' ' + version) if version else ''}: "
                    f"{len(kinds)} adapter kinds visible ({names}{more})."
                ),
            }
        except IndexingCancelled:
            raise
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── dictionary helpers ────────────────────────────────────────────────────

    def _adapter_kinds(self) -> List[dict]:
        if self._kinds_cache is None:
            payload = self._get("adapterkinds", {"retrieveResourceKindInfos": "true"}) or {}
            self._kinds_cache = list(payload.get("adapter-kind") or [])
        return self._kinds_cache

    def _resource_kinds(self, adapter_kind: str) -> List[dict]:
        if adapter_kind not in self._resource_kinds_cache:
            rows = self._paged("GET", f"adapterkinds/{adapter_kind}/resourcekinds", "resource-kind")
            self._resource_kinds_cache[adapter_kind] = rows
        return self._resource_kinds_cache[adapter_kind]

    def _stat_keys(self, adapter_kind: str, resource_kind: str) -> List[dict]:
        key = (adapter_kind, resource_kind)
        if key not in self._statkeys_cache:
            payload = self._get(f"adapterkinds/{adapter_kind}/resourcekinds/{resource_kind}/statkeys",
                                {"statOnly": "true"}) or {}
            self._statkeys_cache[key] = list(payload.get("resourceTypeAttributes") or [])
        return self._statkeys_cache[key]

    def _count_resources(self, adapter_kind: str, resource_kind: str) -> int:
        payload = self._post("resources/query", {"adapterKind": [adapter_kind], "resourceKind": [resource_kind]},
                             params={"page": 0, "pageSize": 1}) or {}
        return int(((payload.get("pageInfo") or {}).get("totalCount")) or len(payload.get("resourceList") or []))

    # ── schema discovery ──────────────────────────────────────────────────────

    @discovery_progress
    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        tables = [self._build_table(name) for name in _CATALOG]
        if self.max_metric_tables == 0:
            return tables
        by_name = {t.name: t for t in tables}
        # Best-effort enrichment + discovered metric tables. Never fail discovery
        # on this — the fixed catalog is always usable.
        try:
            kinds = self._adapter_kinds()
            populated: List[Tuple[str, str, str, int]] = []   # (ak, rk_key, rk_name, count)
            customer_kinds = [k for k in kinds if k.get("key") not in _INTERNAL_ADAPTER_KINDS]
            total = max(1, len(customer_kinds))
            for i, ak in enumerate(customer_kinds):
                ak_key = ak.get("key")
                if progress_callback:
                    progress_callback("adapter kinds", ak_key, i, total)
                for rk in self._resource_kinds(ak_key):
                    if rk.get("resourceKindType") in _SKIP_RESOURCE_KIND_TYPES:
                        continue
                    try:
                        n = self._count_resources(ak_key, rk.get("key"))
                    except IndexingCancelled:
                        raise
                    except Exception:
                        n = 0
                    if n > 0:
                        populated.append((ak_key, rk.get("key"), rk.get("name") or rk.get("key"), n))
            if kinds:
                shown = ", ".join(f"{k.get('key')} ({k.get('name')})" for k in kinds[:SUMMARY_KIND_LIMIT])
                by_name["adapter_kinds"].description += f" Installed: {shown}."
            if populated:
                lines = [f"{ak}/{rk} ×{n}" for ak, rk, _, n in populated[:SUMMARY_KIND_LIMIT]]
                more = f" (+{len(populated) - SUMMARY_KIND_LIMIT} more)" if len(populated) > SUMMARY_KIND_LIMIT else ""
                by_name["resource_kinds"].description += " Populated kinds: " + "; ".join(lines) + f".{more}"
                by_name["resources"].description += (
                    " Estate: " + "; ".join(lines[:12]) + "."
                )
            # Discovered wide metric tables, largest kinds first, capped.
            populated.sort(key=lambda t: -t[3])
            for ak, rk, rk_name, n in populated[: self.max_metric_tables]:
                try:
                    stats = self._stat_keys(ak, rk)
                except IndexingCancelled:
                    raise
                except Exception:
                    continue
                if not stats:
                    continue
                tables.append(self._build_metric_table(ak, rk, rk_name, n, stats))
        except IndexingCancelled:
            raise
        except Exception:
            pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        if table_name in _CATALOG:
            return self._build_table(table_name)
        if table_name.startswith(METRIC_TABLE_PREFIX):
            ak, rk = self._split_metric_table(table_name)
            stats = self._stat_keys(ak, rk)
            return self._build_metric_table(ak, rk, rk, 0, stats)
        raise ValueError(
            f"Unknown Aria Operations table '{table_name}'. Available: {', '.join(_CATALOG)} "
            f"plus discovered {METRIC_TABLE_PREFIX}<AdapterKind>/<ResourceKind> tables."
        )

    def _build_table(self, name: str) -> Table:
        spec = _CATALOG[name]
        columns = [TableColumn(name=c, dtype=d) for c, d in spec["columns"]]
        by_name = {c.name: c for c in columns}
        fks = [
            ForeignKey(column=by_name[col], references_name=ref_table,
                       references_column=TableColumn(name=ref_col, dtype="str"))
            for col, ref_table, ref_col in spec["fks"] if col in by_name
        ]
        pks = [by_name[spec["pk"]]] if spec.get("pk") and spec["pk"] in by_name else []
        return Table(name=name, description=spec["desc"], columns=columns, pks=pks, fks=fks)

    def _build_metric_table(self, ak: str, rk: str, rk_name: str, count: int, stats: List[dict]) -> Table:
        columns = [TableColumn(name="resourceId", dtype="str"),
                   TableColumn(name="resourceName", dtype="str"),
                   TableColumn(name="timestamp", dtype="int", description="epoch milliseconds")]
        for s in stats:
            key = s.get("key")
            if not key:
                continue
            unit = s.get("unit") or ""
            desc = " ".join(x for x in [s.get("name") or "", f"[{unit}]" if unit else "", s.get("description") or ""] if x)
            columns.append(TableColumn(name=key, dtype="float", description=desc.strip() or None))
        fk = ForeignKey(column=columns[0], references_name="resources",
                        references_column=TableColumn(name="id", dtype="str"))
        desc = (f"Wide time series for every {rk_name} ({ak}/{rk}, {count} objects): one row per object per "
                f"timestamp, one column per stat key. Query with a window (+ optional `name`/`regex`/"
                f"`resource_id`, `stat_keys` to select columns, `rollup`, `interval`). For threshold bands use the long `metrics` table with `dt: true`.")
        return Table(name=f"{METRIC_TABLE_PREFIX}{ak}/{rk}", description=desc, columns=columns,
                     pks=[], fks=[fk])

    @staticmethod
    def _split_metric_table(name: str) -> Tuple[str, str]:
        rest = name[len(METRIC_TABLE_PREFIX):]
        if "/" not in rest:
            raise ValueError(f"Metric table name must be '{METRIC_TABLE_PREFIX}<AdapterKind>/<ResourceKind>', got '{name}'.")
        ak, rk = rest.split("/", 1)
        return ak, rk

    # ── query dispatch ────────────────────────────────────────────────────────

    def execute_query(self, query) -> pd.DataFrame:
        """Execute an Aria Operations query spec and return a DataFrame.

        `query` is a JSON string (or dict):
            {"table": "metrics",                                   (required)
             "resource_id": ["<id>", ...] | "name": "prod-db-01" | "regex": "^prod-db",
             "resource_kind": "VirtualMachine", "adapter_kind": "VMWARE",
             "stat_key": ["virtualDisk|totalLatency"],
             "duration_in_mins": 60 | "start_time"/"end_time" epoch ms,
             "rollup": "AVG", "interval": "MINUTES", "interval_quantifier": 5, "dt": true,
             "limit": 500}
        """
        token = _QUERY_DEADLINE.set(time.monotonic() + QUERY_BUDGET_SECONDS)
        try:
            return self._execute_query(query)
        finally:
            _QUERY_DEADLINE.reset(token)

    def _execute_query(self, query):
        spec = self._parse_spec(query)
        table = spec["table"]
        limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)

        if table.startswith(METRIC_TABLE_PREFIX):
            return self._query_metric_table(table, spec, limit)

        handler = getattr(self, f"_q_{table}")
        df = handler(spec, limit)
        return df

    # dictionary ---------------------------------------------------------------

    def _q_adapter_kinds(self, spec: dict, limit: int) -> pd.DataFrame:
        rows = [{"key": k.get("key"), "name": k.get("name"), "description": k.get("description"),
                 "adapterKindType": k.get("adapterKindType"),
                 "resourceKindCount": len(k.get("resourceKinds") or [])} for k in self._adapter_kinds()]
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["adapter_kinds"]["columns"]])

    def _q_resource_kinds(self, spec: dict, limit: int) -> pd.DataFrame:
        aks = self._adapter_kind_list(spec)
        rows = []
        for ak in aks:
            if len(rows) >= limit:
                break
            for rk in self._resource_kinds(ak):
                if spec.get("resource_kind") and rk.get("key") not in self._as_list(spec["resource_kind"]):
                    continue
                count = None
                if spec.get("with_counts", True) and rk.get("resourceKindType") not in _SKIP_RESOURCE_KIND_TYPES:
                    try:
                        count = self._count_resources(ak, rk.get("key"))
                    except IndexingCancelled:
                        raise
                    except Exception:
                        count = None
                rows.append({"adapterKind": ak, "key": rk.get("key"), "name": rk.get("name"),
                             "resourceKindType": rk.get("resourceKindType"), "resourceCount": count})
                if len(rows) >= limit:
                    break
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["resource_kinds"]["columns"]])

    def _q_stat_keys(self, spec: dict, limit: int) -> pd.DataFrame:
        ak = spec.get("adapter_kind")
        rk = spec.get("resource_kind")
        if not ak or not rk:
            raise ValueError('stat_keys requires "adapter_kind" and "resource_kind", e.g. VMWARE / VirtualMachine.')
        rows = [{"adapterKind": ak, "resourceKind": rk, "key": s.get("key"), "name": s.get("name"),
                 "unit": s.get("unit"), "rollupType": s.get("rollupType"), "dataType": s.get("dataType2"),
                 "description": s.get("description")} for s in self._stat_keys(ak, rk)]
        needle = (spec.get("search") or "").lower()
        if needle:
            rows = [r for r in rows if needle in (str(r["key"]) + " " + str(r["name"]) + " " + str(r["description"])).lower()]
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["stat_keys"]["columns"]])

    # inventory ----------------------------------------------------------------

    def _q_resources(self, spec: dict, limit: int) -> pd.DataFrame:
        rows = [self._flatten_resource(r) for r in self._find_resources(spec, limit)]
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["resources"]["columns"]])

    def _q_properties(self, spec: dict, limit: int) -> pd.DataFrame:
        resources = self._resolve_resources(spec, required=True)
        wanted = self._as_list(spec.get("property_keys") or spec.get("property_key"))
        rows = []
        for r in resources:
            rid = r["identifier"]
            payload = self._get(f"resources/{rid}/properties") or {}
            for p in payload.get("property") or []:
                if wanted and p.get("name") not in wanted:
                    continue
                rows.append({"resourceId": rid, "resourceName": self._res_name(r),
                             "name": p.get("name"), "value": p.get("value")})
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["properties"]["columns"]])

    def _q_relationships(self, spec: dict, limit: int) -> pd.DataFrame:
        roots = self._resolve_resources(spec, required=True)
        depth = max(1, min(int(spec.get("depth") or 1), MAX_DEPTH))
        rel_type = (spec.get("relationship_type") or "ALL").upper()
        if rel_type not in ("PARENT", "CHILD", "ALL"):
            raise ValueError('relationship_type must be PARENT, CHILD or ALL.')
        directions = ["PARENT", "CHILD"] if rel_type == "ALL" else [rel_type]
        rows: List[dict] = []
        seen_edges: Set[Tuple[str, str]] = set()
        visited: Set[str] = set()
        frontier = deque((r["identifier"], 0) for r in roots)
        for r in roots:
            self._remember(r)
        while frontier and len(rows) < limit:
            rid, d = frontier.popleft()
            if rid in visited or d >= depth:
                continue
            visited.add(rid)
            for direction in directions:
                related = self._paged("GET", f"resources/{rid}/relationships", "resourceList",
                                      params={"relationshipType": direction})
                for rel in related:
                    self._remember(rel)
                    oid = rel.get("identifier")
                    if direction == "PARENT":
                        edge = (oid, rid)
                    else:
                        edge = (rid, oid)
                    if edge in seen_edges:
                        continue
                    seen_edges.add(edge)
                    pn, pk = self._name_cache.get(edge[0], ("", ""))
                    cn, ck = self._name_cache.get(edge[1], ("", ""))
                    rows.append({"parentId": edge[0], "parentName": pn, "parentKind": pk,
                                 "childId": edge[1], "childName": cn, "childKind": ck, "depth": d + 1})
                    if oid not in visited:
                        frontier.append((oid, d + 1))
                    if len(rows) >= limit:
                        break
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["relationships"]["columns"]])

    # time series ----------------------------------------------------------------

    def _q_metrics(self, spec: dict, limit: int) -> pd.DataFrame:
        resources = self._resolve_resources(spec, required=True)
        stat_keys = self._as_list(spec.get("stat_key") or spec.get("stat_keys"))
        if not stat_keys:
            raise ValueError(
                'metrics requires "stat_key" (string or list), e.g. "virtualDisk|totalLatency". '
                "Use the stat_keys table to discover keys for a resource kind."
            )
        body = self._stats_body(spec, stat_keys)
        rows: List[dict] = []
        for chunk in self._chunks([r["identifier"] for r in resources], ID_BATCH):
            payload = self._post("resources/stats/query", {**body, "resourceId": chunk}) or {}
            rows.extend(self._flatten_stats(payload, with_dt=bool(spec.get("dt"))))
            if len(rows) >= limit:
                break
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["metrics"]["columns"]])

    def _q_metrics_latest(self, spec: dict, limit: int) -> pd.DataFrame:
        resources = self._resolve_resources(spec, required=True)
        stat_keys = self._as_list(spec.get("stat_key") or spec.get("stat_keys"))
        max_samples = max(1, int(spec.get("max_samples") or 1))
        rows: List[dict] = []
        for chunk in self._chunks([r["identifier"] for r in resources], ID_BATCH):
            body = {"resourceId": chunk, "maxSamples": max_samples}
            if stat_keys:
                body["statKey"] = stat_keys
            payload = self._post("resources/stats/latest/query", body) or {}
            for row in self._flatten_stats(payload, with_dt=False):
                rows.append({k: row[k] for k in ("resourceId", "resourceName", "statKey", "timestamp", "value")})
            if len(rows) >= limit:
                break
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["metrics_latest"]["columns"]])

    def _q_metrics_topn(self, spec: dict, limit: int) -> pd.DataFrame:
        stat_keys = self._as_list(spec.get("stat_key") or spec.get("stat_keys"))
        if not stat_keys:
            raise ValueError('metrics_topn requires "stat_key".')
        resources = self._resolve_resources(spec, required=True)
        top_n = max(1, int(spec.get("top_n") or spec.get("topN") or 10))
        order = "ASCENDING" if str(spec.get("sort_order") or spec.get("order") or "desc").lower().startswith("asc") else "DESCENDING"
        body = self._stats_body(spec, stat_keys)
        params: Dict[str, Any] = {"topN": top_n, "groupBy": "STATKEY", "sortOrder": order,
                                  "statKey": stat_keys, "begin": body["begin"], "end": body["end"],
                                  "rollUpType": body["rollUpType"]}
        if "intervalType" in body:
            params["intervalType"] = body["intervalType"]
            params["intervalQuantifier"] = body["intervalQuantifier"]
        rows: List[dict] = []
        # The API ranks within one call; across chunks we merge and re-rank.
        for chunk in self._chunks([r["identifier"] for r in resources], ID_BATCH):
            payload = self._get("resources/stats/topn", {**params, "resourceId": chunk}) or {}
            for group in payload.get("resourceStatGroups") or []:
                for rs in group.get("resourceStats") or []:
                    rid = rs.get("resourceId")
                    for s in ([rs["stat"]] if rs.get("stat") else ((rs.get("stat-list") or {}).get("stat") or [])):
                        data = s.get("data") or []
                        ts = s.get("timestamps") or []
                        if not data:
                            continue
                        rows.append({"statKey": (s.get("statKey") or {}).get("key") or group.get("groupKey"),
                                     "resourceId": rid, "resourceName": self._name_cache.get(rid, ("", ""))[0],
                                     "value": data[-1], "timestamp": ts[-1] if ts else None})
        out: List[dict] = []
        by_key: Dict[str, List[dict]] = {}
        for r in rows:
            by_key.setdefault(r["statKey"], []).append(r)
        for key, items in by_key.items():
            items.sort(key=lambda x: (x["value"] is None, x["value"]), reverse=(order == "DESCENDING"))
            for i, item in enumerate(items[:top_n]):
                out.append({"rank": i + 1, **item})
        return pd.DataFrame(out[:limit], columns=[c for c, _ in _CATALOG["metrics_topn"]["columns"]])

    def _query_metric_table(self, table: str, spec: dict, limit: int) -> pd.DataFrame:
        ak, rk = self._split_metric_table(table)
        scoped = {**spec, "adapter_kind": ak, "resource_kind": rk}
        resources = self._resolve_resources(scoped, required=False)
        if not resources:
            return pd.DataFrame(columns=["resourceId", "resourceName", "timestamp"])
        stat_keys = self._as_list(spec.get("stat_key") or spec.get("stat_keys"))
        if not stat_keys:
            stat_keys = [s.get("key") for s in self._stat_keys(ak, rk) if s.get("key")]
        body = self._stats_body(spec, stat_keys)
        long_rows: List[dict] = []
        for chunk in self._chunks([r["identifier"] for r in resources], ID_BATCH):
            payload = self._post("resources/stats/query", {**body, "resourceId": chunk}) or {}
            long_rows.extend(self._flatten_stats(payload, with_dt=False))
        if not long_rows:
            return pd.DataFrame(columns=["resourceId", "resourceName", "timestamp"] + stat_keys)
        df = pd.DataFrame(long_rows)
        wide = df.pivot_table(index=["resourceId", "resourceName", "timestamp"], columns="statKey",
                              values="value", aggfunc="last").reset_index()
        wide.columns.name = None
        for k in stat_keys:
            if k not in wide.columns:
                wide[k] = None
        wide = wide[["resourceId", "resourceName", "timestamp"] + stat_keys]
        wide = wide.sort_values(["resourceName", "timestamp"]).reset_index(drop=True)
        return wide.head(limit)

    # incident timeline -----------------------------------------------------------

    def _incident_rows(self, table, key, spec, body, limit):
        explicit_window = any(k in spec for k in ("start_time", "end_time", "duration_in_mins"))
        predicate = None
        if not body["activeOnly"] or explicit_window:
            begin, end = self._window(spec)
            overlap = spec.get("time_mode", "started") == "overlap"
            body["startTimeRange"] = {"startTime": 0 if overlap else begin, "endTime": end}
            if overlap:
                predicate = lambda row: (row.get("startTimeUTC") or 0) <= end and (
                    not row.get("cancelTimeUTC") or row["cancelTimeUTC"] >= begin)
        return self._paged("POST", f"{table}/query", key, body=body, limit=limit, predicate=predicate)

    def _q_alerts(self, spec: dict, limit: int) -> pd.DataFrame:
        body: Dict[str, Any] = {"activeOnly": bool(spec.get("active_only", False))}
        if spec.get("level") or spec.get("criticality"):
            body["alertCriticality"] = [x.upper() for x in self._as_list(spec.get("level") or spec.get("criticality"))]
        if spec.get("status"):
            body["alertStatus"] = [x.upper() for x in self._as_list(spec["status"])]
        if spec.get("alert_definition_id"):
            body["alertDefinitionId"] = self._as_list(spec["alert_definition_id"])
        if spec.get("alert_id"):
            body["alertId"] = self._as_list(spec["alert_id"])
        if spec.get("name_contains"):
            body["alertName"] = str(spec["name_contains"])
        rq = self._resource_query(spec)
        if rq:
            body["resource-query"] = rq
            if spec.get("include_children"):
                body["includeChildrenResources"] = True
        rows = self._incident_rows("alerts", "alerts", spec, body, limit)
        self._hydrate_names([a.get("resourceId") for a in rows])
        out = []
        for a in rows[:limit]:
            name, kind = self._name_cache.get(a.get("resourceId"), ("", ""))
            out.append({"alertId": a.get("alertId"), "resourceId": a.get("resourceId"), "resourceName": name,
                        "resourceKind": kind, "alertDefinitionId": a.get("alertDefinitionId"),
                        "alertDefinitionName": a.get("alertDefinitionName"), "alertLevel": a.get("alertLevel"),
                        "status": a.get("status"), "controlState": a.get("controlState"),
                        "alertImpact": a.get("alertImpact"), "startTimeUTC": a.get("startTimeUTC"),
                        "updateTimeUTC": a.get("updateTimeUTC"), "cancelTimeUTC": a.get("cancelTimeUTC")})
        df = pd.DataFrame(out, columns=[c for c, _ in _CATALOG["alerts"]["columns"]])
        if len(df):
            df = df.sort_values("startTimeUTC").reset_index(drop=True)
        return df

    def _q_contributing_symptoms(self, spec: dict, limit: int) -> pd.DataFrame:
        alert_ids = self._as_list(spec.get("alert_id") or spec.get("alert_ids"))
        if not alert_ids:
            raise ValueError('contributing_symptoms requires "alert_id" (string or list).')
        rows: List[dict] = []
        for chunk in self._chunks(alert_ids, 100):
            payload = self._get("alerts/contributingsymptoms", {"id": chunk}) or {}
            for entry in payload.get("contributingSymptoms") or []:
                inner = ((entry.get("contributingSymptoms") or {}).get("contributingSymptoms") or [])
                for cs in inner:
                    sev = ""
                    conds = cs.get("alertConditions") or []
                    if conds:
                        sev = conds[0].get("severity") or ""
                    defs = cs.get("symptomDefinitionsIds") or []
                    rows.append({"alertId": entry.get("alertId"), "symptomId": cs.get("symptomId"),
                                 "symptomDefinitionId": defs[0] if defs else None, "severity": sev,
                                 "symptomDefinitionsIds": json.dumps(defs), "alertConditions": json.dumps(conds)})
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["contributing_symptoms"]["columns"]])

    def _q_symptoms(self, spec: dict, limit: int) -> pd.DataFrame:
        body: Dict[str, Any] = {"activeOnly": bool(spec.get("active_only", False))}
        if spec.get("level") or spec.get("criticality"):
            body["alarmCriticality"] = [x.upper() for x in self._as_list(spec.get("level") or spec.get("criticality"))]
        rq = self._resource_query(spec)
        if rq:
            body["resource-query"] = rq
            if spec.get("include_children"):
                body["includeChildrenResources"] = True
        rows = self._incident_rows("symptoms", "symptom", spec, body, limit)
        self._hydrate_names([s.get("resourceId") for s in rows])
        out = []
        for s in rows[:limit]:
            name, _ = self._name_cache.get(s.get("resourceId"), ("", ""))
            out.append({"id": s.get("id"), "resourceId": s.get("resourceId"), "resourceName": name,
                        "symptomDefinitionId": s.get("symptomDefinitionId"),
                        "symptomCriticality": s.get("symptomCriticality"), "message": s.get("message"),
                        "statKey": s.get("statKey"), "kpi": s.get("kpi"), "startTimeUTC": s.get("startTimeUTC"),
                        "updateTimeUTC": s.get("updateTimeUTC"), "cancelTimeUTC": s.get("cancelTimeUTC")})
        df = pd.DataFrame(out, columns=[c for c, _ in _CATALOG["symptoms"]["columns"]])
        if len(df):
            df = df.sort_values("startTimeUTC").reset_index(drop=True)
        return df

    def _q_alert_definitions(self, spec: dict, limit: int) -> pd.DataFrame:
        body: Dict[str, Any] = {}
        if spec.get("adapter_kind"):
            body["adapterKinds"] = self._as_list(spec["adapter_kind"])
        if spec.get("resource_kind"):
            body["resourceKinds"] = self._as_list(spec["resource_kind"])
        if spec.get("id") or spec.get("ids"):
            body["ids"] = self._as_list(spec.get("id") or spec.get("ids"))
        needle = str(spec.get("search") or "").lower()
        predicate = (lambda d: needle in f"{d.get('name', '')} {d.get('description', '')}".lower()) if needle else None
        rows = self._paged("POST", "alertdefinitions/query", "alertDefinitions", body=body, limit=limit, predicate=predicate)
        out = []
        for d in rows:
            states = d.get("states") or []
            sev = (states[0].get("severity") if states else None)
            out.append({"id": d.get("id"), "name": d.get("name"), "description": d.get("description"),
                        "adapterKindKey": d.get("adapterKindKey"), "resourceKindKey": d.get("resourceKindKey"),
                        "severity": sev, "waitCycles": d.get("waitCycles"), "cancelCycles": d.get("cancelCycles"),
                        "states": json.dumps(states)})
        return pd.DataFrame(out[:limit], columns=[c for c, _ in _CATALOG["alert_definitions"]["columns"]])

    def _q_custom_groups(self, spec: dict, limit: int) -> pd.DataFrame:
        payload = self._get("resources/groups", {"includePolicy": "true"}) or {}
        groups = payload.get("groups") or []
        wanted = (spec.get("name") or "").lower() if isinstance(spec.get("name"), str) else ""
        rows: List[dict] = []
        for g in groups:
            gname = ((g.get("resourceKey") or {}).get("name")) or ""
            if wanted and wanted not in gname.lower():
                continue
            gid = g.get("id")
            members = self._paged("GET", f"resources/groups/{gid}/members", "resourceList", limit=limit - len(rows))
            if not members:
                rows.append({"groupId": gid, "groupName": gname, "policy": g.get("policy"),
                             "memberId": None, "memberName": None, "memberKind": None})
            for m in members:
                self._remember(m)
                rows.append({"groupId": gid, "groupName": gname, "policy": g.get("policy"),
                             "memberId": m.get("identifier"), "memberName": self._res_name(m),
                             "memberKind": self._res_kind(m)})
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        return pd.DataFrame(rows[:limit], columns=[c for c, _ in _CATALOG["custom_groups"]["columns"]])

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _as_list(v) -> List[str]:
        if v is None or v == "":
            return []
        if isinstance(v, (list, tuple, set)):
            return [str(x) for x in v if x not in (None, "")]
        return [str(v)]

    @staticmethod
    def _chunks(items: List[str], n: int) -> Iterable[List[str]]:
        for i in range(0, len(items), n):
            yield items[i:i + n]

    @staticmethod
    def _res_name(r: dict) -> str:
        return ((r.get("resourceKey") or {}).get("name")) or ""

    @staticmethod
    def _res_kind(r: dict) -> str:
        return ((r.get("resourceKey") or {}).get("resourceKindKey")) or ""

    def _remember(self, r: dict) -> None:
        rid = r.get("identifier")
        if rid:
            self._name_cache[rid] = (self._res_name(r), self._res_kind(r))

    def _hydrate_names(self, ids: List[Optional[str]]) -> None:
        missing = [i for i in set(ids) if i and i not in self._name_cache]
        for chunk in self._chunks(missing, ID_BATCH):
            try:
                for r in self._paged("POST", "resources/query", "resourceList", body={"resourceId": chunk}):
                    self._remember(r)
            except RuntimeError:
                pass

    def _flatten_resource(self, r: dict) -> dict:
        self._remember(r)
        key = r.get("resourceKey") or {}
        states = r.get("resourceStatusStates") or []
        st = states[0] if states else {}
        badges = r.get("badges") or []
        return {
            "id": r.get("identifier"), "name": key.get("name"), "adapterKind": key.get("adapterKindKey"),
            "resourceKind": key.get("resourceKindKey"), "health": r.get("resourceHealth"),
            "healthValue": r.get("resourceHealthValue"), "resourceStatus": st.get("resourceStatus"),
            "resourceState": st.get("resourceState"),
            "badges": ", ".join(f"{b.get('type')}={b.get('color')}" for b in badges if b.get("type")),
            "creationTime": r.get("creationTime"),
        }

    def _adapter_kind_list(self, spec: dict) -> List[str]:
        aks = self._as_list(spec.get("adapter_kind"))
        if aks:
            return aks
        return [k.get("key") for k in self._adapter_kinds() if k.get("key")]

    def _resource_query(self, spec: dict) -> dict:
        """Translate the spec's resource filters into the API's `resource-query` body."""
        rq: Dict[str, Any] = {}
        ids = self._as_list(spec.get("resource_id") or spec.get("resource_ids") or spec.get("id") or spec.get("ids"))
        if ids:
            rq["resourceId"] = ids
        names = self._as_list(spec.get("name") or spec.get("names"))
        if names:
            rq["name"] = names
        regex = self._as_list(spec.get("regex"))
        if regex:
            rq["regex"] = regex
        if spec.get("adapter_kind"):
            rq["adapterKind"] = self._as_list(spec["adapter_kind"])
        if spec.get("resource_kind"):
            rq["resourceKind"] = self._as_list(spec["resource_kind"])
        if spec.get("parent_id"):
            rq["parentId"] = self._as_list(spec["parent_id"])
        if spec.get("health"):
            rq["resourceHealth"] = [h.upper() for h in self._as_list(spec["health"])]
        prop = spec.get("property")
        if isinstance(prop, dict) and prop.get("key"):
            rq["propertyConditions"] = {
                "conjunctionOperator": "AND",
                "conditions": [{"key": prop["key"], "operator": (prop.get("operator") or "EQ").upper(),
                                "stringValue": str(prop.get("value", ""))}],
            }
        return rq

    def _find_resources(self, spec: dict, limit: Optional[int] = None) -> List[dict]:
        rq = self._resource_query(spec)
        rows = self._paged("POST", "resources/query", "resourceList", body=rq, limit=limit)
        for r in rows:
            self._remember(r)
        return rows

    def _resolve_resources(self, spec: dict, required: bool) -> List[dict]:
        """Resources named by the spec (ids, names, regex, kind…). `required`
        rejects an unscoped spec — a metrics call over the whole estate is a
        mistake, not a query."""
        rq = self._resource_query(spec)
        if not rq:
            if required:
                raise ValueError(
                    "This table needs a resource scope: give \"resource_id\" (list), \"name\", "
                    "\"regex\", or \"resource_kind\" (optionally with \"adapter_kind\")."
                )
            return []
        if set(rq) == {"resourceId"}:
            # Already have ids; fetch names for output (cheap, one page per 1000).
            self._hydrate_names(rq["resourceId"])
            return [{"identifier": i, "resourceKey": {"name": self._name_cache.get(i, ("", ""))[0],
                                                       "resourceKindKey": self._name_cache.get(i, ("", ""))[1]}}
                    for i in rq["resourceId"]]
        rows = self._find_resources(spec)
        if not rows and required:
            raise ValueError(
                f"No Aria Operations resources match {json.dumps(rq)[:200]}. Check the name/kind with the "
                "resources table (names are exact; use \"regex\" for partial matches)."
            )
        return rows

    def _window(self, spec: dict) -> Tuple[int, int]:
        now_ms = int(time.time() * 1000)
        start, end = spec.get("start_time"), spec.get("end_time")
        if start is not None or end is not None:
            end_ms = int(end) if end is not None else now_ms
            start_ms = int(start) if start is not None else end_ms - DEFAULT_DURATION_MINS * 60_000
            if start_ms >= end_ms:
                raise ValueError("start_time must be before end_time (both epoch milliseconds).")
            return start_ms, end_ms
        mins = spec.get("duration_in_mins")
        if mins is None:
            mins = self.history_window_days * 24 * 60 if spec.get("table") in ("alerts", "symptoms") else DEFAULT_DURATION_MINS
        return now_ms - int(mins) * 60_000, now_ms

    def _stats_body(self, spec: dict, stat_keys: List[str]) -> dict:
        begin, end = self._window(spec)
        body: Dict[str, Any] = {"statKey": stat_keys, "begin": begin, "end": end,
                                "rollUpType": (spec.get("rollup") or "AVG").upper()}
        if body["rollUpType"] not in ("SUM", "AVG", "MIN", "MAX", "NONE", "LATEST", "COUNT"):
            raise ValueError("rollup must be one of SUM, AVG, MIN, MAX, NONE, LATEST, COUNT.")
        interval = spec.get("interval") or spec.get("interval_type")
        if interval:
            it = str(interval).upper()
            if it not in ("SECONDS", "MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS", "YEARS"):
                raise ValueError("interval must be SECONDS, MINUTES, HOURS, DAYS, WEEKS, MONTHS or YEARS.")
            body["intervalType"] = it
            body["intervalQuantifier"] = int(spec.get("interval_quantifier") or 1)
        if spec.get("dt"):
            body["dt"] = True
        return body

    def _flatten_stats(self, payload: dict, with_dt: bool) -> List[dict]:
        rows: List[dict] = []
        for v in payload.get("values") or []:
            rid = v.get("resourceId")
            name, kind = self._name_cache.get(rid, ("", ""))
            for s in ((v.get("stat-list") or {}).get("stat") or []):
                key = (s.get("statKey") or {}).get("key")
                ts = s.get("timestamps") or []
                data = s.get("data") or []
                if not data and s.get("values"):
                    data = s.get("values")
                dmin = s.get("minThresholdData") or []
                dmax = s.get("maxThresholdData") or []
                dt_times = s.get("dtTimestamps") or ts
                lower = dict(zip(dt_times, dmin))
                upper = dict(zip(dt_times, dmax))
                for i, t in enumerate(ts):
                    row = {"resourceId": rid, "resourceName": name, "resourceKind": kind, "statKey": key,
                           "timestamp": t, "value": data[i] if i < len(data) else None}
                    if with_dt:
                        row["dt_min"] = lower.get(t)
                        row["dt_max"] = upper.get(t)
                    else:
                        row["dt_min"] = None
                        row["dt_max"] = None
                    rows.append(row)
        return rows

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = dict(query)
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError(
                    "Aria Operations query must be a JSON object like "
                    '{"table": "metrics", "name": "prod-db-01", "resource_kind": "VirtualMachine", '
                    '"stat_key": ["virtualDisk|totalLatency"], "duration_in_mins": 60} — got: '
                    f"{str(query)[:200]}"
                )
        if not isinstance(spec, dict) or not spec.get("table"):
            raise ValueError('Aria Operations query spec must include a "table" key.')
        table = str(spec["table"])
        if table not in _CATALOG and not table.startswith(METRIC_TABLE_PREFIX):
            raise ValueError(
                f"Unknown Aria Operations table '{table}'. Available: {', '.join(_CATALOG)} plus "
                f"discovered {METRIC_TABLE_PREFIX}<AdapterKind>/<ResourceKind> tables."
            )
        scope = {"resource_id", "resource_ids", "id", "ids", "name", "names", "regex",
                 "resource_kind", "adapter_kind", "parent_id", "health", "property"}
        window = {"start_time", "end_time", "duration_in_mins"}
        stats = scope | window | {"stat_key", "stat_keys", "rollup", "interval", "interval_type", "interval_quantifier"}
        incident = scope | window | {"active_only", "level", "criticality", "include_children", "time_mode"}
        allowed = {
            "adapter_kinds": set(), "resource_kinds": {"adapter_kind", "resource_kind", "with_counts"},
            "stat_keys": {"adapter_kind", "resource_kind", "search"}, "resources": scope,
            "properties": scope | {"property_key", "property_keys"},
            "relationships": scope | {"depth", "relationship_type"},
            "metrics": stats | {"dt"}, "metrics_latest": scope | {"stat_key", "stat_keys", "max_samples"},
            "metrics_topn": stats | {"top_n", "topN", "sort_order", "order"},
            "alerts": incident | {"status", "alert_definition_id", "alert_id", "name_contains"},
            "symptoms": incident, "contributing_symptoms": {"alert_id", "alert_ids"},
            "alert_definitions": {"adapter_kind", "resource_kind", "id", "ids", "search"},
            "custom_groups": {"name"},
        }
        supported = stats | {"dt"} if table.startswith(METRIC_TABLE_PREFIX) else allowed[table]
        unknown = set(spec) - supported - {"table", "limit"}
        if unknown:
            raise ValueError(f"Unsupported fields for {table}: {', '.join(sorted(unknown))}.")
        for field in ("start_time", "end_time"):
            if field in spec and (isinstance(spec[field], bool) or not isinstance(spec[field], int) or spec[field] < 0):
                raise ValueError(f"{field} must be a non-negative epoch-millisecond integer.")
        for field in ("limit", "duration_in_mins", "interval_quantifier", "max_samples", "top_n", "topN"):
            if field in spec and (isinstance(spec[field], bool) or not isinstance(spec[field], int) or spec[field] <= 0):
                raise ValueError(f"{field} must be a positive integer.")
        for field in ("active_only", "dt", "include_children", "with_counts"):
            if field in spec and not isinstance(spec[field], bool):
                raise ValueError(f"{field} must be a JSON boolean.")
        if spec.get("time_mode", "started") not in ("started", "overlap"):
            raise ValueError("time_mode must be 'started' or 'overlap'.")
        if table.startswith(METRIC_TABLE_PREFIX) and spec.get("dt"):
            raise ValueError("Use the metrics table with dt=true for threshold bands; wide tables contain metric values only.")
        spec["table"] = table
        return spec

    # ── prompts ───────────────────────────────────────────────────────────────

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    def system_prompt(self):
        text = """
        ## VMware Aria Operations Integration
        Query Aria Operations via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "metrics",
         "name": "prod-db-01", "resource_kind": "VirtualMachine",
         "stat_key": ["virtualDisk|totalLatency", "virtualDisk|commandsAveraged_average"],
         "duration_in_mins": 120, "rollup": "AVG", "interval": "MINUTES", "interval_quantifier": 5,
         "dt": true, "limit": 2000}
        ```

        - `table` (required): adapter_kinds, resource_kinds, stat_keys, resources, properties,
          relationships, metrics, metrics_latest, metrics_topn, alerts, contributing_symptoms,
          symptoms, alert_definitions, custom_groups — or a discovered wide table
          `metrics::<AdapterKind>/<ResourceKind>` (one column per stat key).
        - Resource scope (any of): `resource_id` (list of ids), `name` (exact, list ok), `regex`,
          `resource_kind` (+ `adapter_kind`), `parent_id`, `health`, or
          `property` {"key": "summary|MOID", "operator": "EQ", "value": "vm-1001"}.
          Names are EXACT — use `regex` for partial matches. Never query metrics without a scope.
        - Time: `duration_in_mins` (relative to now) or `start_time`/`end_time` in EPOCH
          MILLISECONDS. All timestamps in results are epoch ms: `pd.to_datetime(df.timestamp, unit="ms")`.
          Collection intervals depend on the adapter and configuration; use `interval: "MINUTES", interval_quantifier: 5` or coarser.
        - `limit` (optional): max returned rows, default 500. A limited result is a sample,
          not proof of absence or a complete population for aggregation.
        - Alerts/symptoms default to `time_mode: "started"`: start time within the window,
          sent to the server as startTimeRange. `time_mode: "overlap"` explicitly includes
          incidents opened earlier and not cancelled before the window; it can scan history.
          `active_only: true` without a window means currently active, regardless of start time.
          Internal JSON names are start_time/end_time, not the raw Suite API body names.

        DISCOVERY (do this before guessing keys):
        - `adapter_kinds` → what is monitored (VMWARE = vCenter; storage/NSX/etc. packs).
        - `resource_kinds` with `adapter_kind` → object types + counts.
        - `stat_keys` with `adapter_kind` + `resource_kind` (+ `search`) → exact metric keys and units.
          Common VMWARE keys: VirtualMachine `cpu|usage_average`, `mem|usage_average`,
          `virtualDisk|totalLatency`, `virtualDisk|commandsAveraged_average`;
          Datastore `datastore|totalLatency`; HostSystem `cpu|usage_average`, `disk|totalLatency_average`.

        TOPOLOGY / RCA:
        - `relationships` with a resource scope and `depth` (1-4) returns the edge list around the
          object: VM → Datastore → (storage pack) LUN/LDEV → Pool → Array, and up to Host/Cluster.
          Walk it to find WHAT SHARES the same datastore/pool/host as the affected object.
        - `metrics` with `dt: true` returns dt_min/dt_max, Aria's learned normal band: a value above
          dt_max is abnormal by Aria's own baseline. Compare the SAME window across layers (VM disk
          latency, datastore latency, pool response time) and look at which layer deviated FIRST.
        - `alerts` / `symptoms` over the incident window are the timeline (startTimeUTC order);
          `symptoms.message` may contain event details (not guaranteed numeric thresholds); `contributing_symptoms` links
          them; `alert_definitions` are the operators' own rules — cite them as the yardstick.
        - `metrics_topn` ranks objects: e.g. the noisiest VMs on a datastore, worst datastores by latency.
        - `properties` hold the join keys to a CMDB/ServiceNow (`config|instanceUuid`, `summary|MOID`)
          and to storage (`summary|datastore|naa`, array `serialNumber`, LDEV `naaId`).

        Examples:
        ```python
        # 1. Locate the object and its neighbourhood
        vm  = client.execute_query('{"table": "resources", "name": "prod-db-01", "resource_kind": "VirtualMachine"}')
        rel = client.execute_query('{"table": "relationships", "name": "prod-db-01", "resource_kind": "VirtualMachine", "depth": 3, "limit": 2000}')
        # 2. Same window, three layers, with the normal band
        m = client.execute_query('{"table": "metrics", "resource_id": ["<vm-id>", "<datastore-id>", "<pool-id>"], "stat_key": ["virtualDisk|totalLatency", "datastore|totalLatency", "response_time"], "start_time": 1756865400000, "end_time": 1756869000000, "interval": "MINUTES", "interval_quantifier": 5, "dt": true, "limit": 5000}')
        # 3. What fired in the window, oldest first
        a = client.execute_query('{"table": "alerts", "start_time": 1756863600000, "end_time": 1756870800000, "limit": 500}')
        s = client.execute_query('{"table": "symptoms", "start_time": 1756863600000, "end_time": 1756870800000}')
        # 4. Who else is on that datastore / pool, and who is noisiest
        kids = client.execute_query('{"table": "relationships", "resource_id": "<datastore-id>", "relationship_type": "PARENT"}')
        top = client.execute_query('{"table": "metrics_topn", "resource_kind": "VirtualMachine", "stat_key": "virtualDisk|commandsAveraged_average", "top_n": 5, "duration_in_mins": 180}')
        # 5. A storage pack's wide table (discovered)
        pools = client.execute_query('{"table": "metrics::HitachiStorage/Pool", "duration_in_mins": 180, "limit": 5000}')
        ```
        Aggregate by fetching rows and grouping in pandas. Join across connections on names / properties.
        """
        return text


# Alias so dynamic naming ("aria_operations" → "AriaOperationsClient") and the
# explicit client_path both resolve to the same class.
AriaOperationsClient = AriaOperationsClient
