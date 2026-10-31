"""Microsoft Intune data source client.

Backs the `intune` data source type. Intune has no SQL endpoint — its data
lives behind Microsoft Graph (`/deviceManagement/*`, `/deviceAppManagement/*`).
Like the Zabbix connector, the catalog is a *fixed* set of virtual tables whose
columns are declared in code, because Intune's data model is stable and Graph
offers no schema introspection worth crawling:

  managed_devices                    /deviceManagement/managedDevices
  compliance_policies                /deviceManagement/deviceCompliancePolicies
  compliance_policy_device_statuses  /deviceManagement/deviceCompliancePolicies/{id}/deviceStatuses
  configuration_profiles             /deviceManagement/deviceConfigurations
  mobile_apps                        /deviceAppManagement/mobileApps
  detected_apps                      /deviceManagement/detectedApps
  autopilot_devices                  /deviceManagement/windowsAutopilotDeviceIdentities

`execute_query()` takes a JSON spec (see `system_prompt`) that maps 1:1 to a
Graph list call with OData `$filter` / `$select` / `$orderby`, paged through
`@odata.nextLink`. Column names are Graph's own camelCase property names, so a
filter is passed to Graph verbatim — no translation layer to get wrong.

Auth is the admin's Entra app registration with *application* permissions
(client-credentials). That is the standard shape for Intune reporting: the
data is tenant-wide device inventory, not per-user content. Each table needs
one read permission; a table whose permission was not granted is left out of
the catalog instead of failing every query (see `TABLES[...]["permission"]`).

HTTP, token minting and Graph throttling (429 / Retry-After, shared per-app
pause) are inherited from GraphDriveClient, so Intune traffic follows the same
discipline as the SharePoint / OneDrive connectors.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.base import Capability
from app.data_sources.clients.graph_drive_client import GraphDriveClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_progress

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 1000
MAX_ROWS = 50_000
# Rows per Graph page. Intune list endpoints accept $top; 500 keeps a page well
# inside the gateway timeout on large tenants while bounding round-trips.
PAGE_SIZE = 500
# Fan-out bound for the per-policy device-status table.
MAX_POLICIES = 200

# Intune reports "never" / "not set" as sentinel timestamps instead of null
# (e.g. a device that never synced with Exchange has
# exchangeLastSuccessfulSyncDateTime = 0001-01-01T00:00:00Z, and a device with
# no compliance grace period has complianceGracePeriodExpirationDateTime =
# 9999-12-31T23:59:59Z). Left in place they break pd.to_datetime (out of
# bounds) and silently skew min/max aggregates, so they are returned as null.
_SENTINEL_PREFIXES = ("0001-01-01", "9999-12-31")


def _col(name: str, dtype: str) -> tuple:
    return (name, dtype)


# ── virtual table catalog ─────────────────────────────────────────────────────
#
# `path`        Graph collection the table reads.
# `permission`  the least-privileged Graph application permission that reads it
#               (per the Microsoft Graph v1.0 API reference for each endpoint).
# `columns`     (graph_property, dtype). Order = DataFrame column order.
#               `odataType` is derived from each row's `@odata.type` (the
#               concrete subtype: platform of a policy, kind of an app).
# Deliberately NOT exposed: managedDevice.activationLockBypassCode and
# windowsAutopilotDeviceIdentity.productKey — secrets, not analytics.
TABLES: dict[str, dict[str, Any]] = {
    "managed_devices": {
        "path": "/deviceManagement/managedDevices",
        "permission": "DeviceManagementManagedDevices.Read.All",
        "pk": "id",
        "columns": [
            _col("id", "string"), _col("deviceName", "string"),
            _col("userPrincipalName", "string"), _col("userDisplayName", "string"),
            _col("emailAddress", "string"), _col("userId", "string"),
            _col("operatingSystem", "string"), _col("osVersion", "string"),
            _col("complianceState", "string"), _col("managementState", "string"),
            _col("managementAgent", "string"), _col("managedDeviceOwnerType", "string"),
            _col("deviceEnrollmentType", "string"), _col("deviceRegistrationState", "string"),
            _col("enrolledDateTime", "datetime"), _col("lastSyncDateTime", "datetime"),
            _col("complianceGracePeriodExpirationDateTime", "datetime"),
            _col("manufacturer", "string"), _col("model", "string"),
            _col("serialNumber", "string"), _col("imei", "string"),
            _col("wiFiMacAddress", "string"),
            _col("isEncrypted", "boolean"), _col("isSupervised", "boolean"),
            _col("jailBroken", "string"), _col("azureADRegistered", "boolean"),
            _col("azureADDeviceId", "string"), _col("deviceCategoryDisplayName", "string"),
            _col("partnerReportedThreatState", "string"),
            _col("exchangeAccessState", "string"),
            _col("totalStorageSpaceInBytes", "integer"), _col("freeStorageSpaceInBytes", "integer"),
            _col("physicalMemoryInBytes", "integer"),
            _col("managementCertificateExpirationDate", "datetime"),
            _col("enrollmentProfileName", "string"),
        ],
        "fks": [],
        "desc": (
            "Devices enrolled in Intune (one row per device). complianceState: "
            "compliant, noncompliant, inGracePeriod, conflict, error, unknown, configManager. "
            "operatingSystem e.g. Windows, iOS, Android, macOS. managedDeviceOwnerType: "
            "company, personal, unknown. lastSyncDateTime = last check-in with Intune."
        ),
    },
    "compliance_policies": {
        "path": "/deviceManagement/deviceCompliancePolicies",
        "permission": "DeviceManagementConfiguration.Read.All",
        "pk": "id",
        "columns": [
            _col("id", "string"), _col("displayName", "string"), _col("description", "string"),
            _col("odataType", "string"), _col("createdDateTime", "datetime"),
            _col("lastModifiedDateTime", "datetime"), _col("version", "integer"),
        ],
        "fks": [],
        "desc": (
            "Device compliance policies. odataType is the platform, e.g. "
            "windows10CompliancePolicy, iosCompliancePolicy, androidCompliancePolicy, "
            "macOSCompliancePolicy."
        ),
    },
    "compliance_policy_device_statuses": {
        # Fan-out: one Graph collection per policy, unioned (see _fetch_statuses).
        "path": "/deviceManagement/deviceCompliancePolicies/{policyId}/deviceStatuses",
        "permission": "DeviceManagementConfiguration.Read.All",
        "pk": "id",
        "columns": [
            _col("policyId", "string"), _col("policyName", "string"),
            _col("id", "string"), _col("deviceDisplayName", "string"),
            _col("userPrincipalName", "string"), _col("userName", "string"),
            _col("deviceModel", "string"), _col("status", "string"),
            _col("lastReportedDateTime", "datetime"),
            _col("complianceGracePeriodExpirationDateTime", "datetime"),
        ],
        "fks": [("policyId", "compliance_policies", "id")],
        "desc": (
            "Per-device result of each compliance policy (one row per policy x device). "
            "status: compliant, nonCompliant, inGracePeriod, notApplicable, notAssigned, "
            "remediated, conflict, error, unknown. Pass \"policy\" in the query spec to "
            "read one policy instead of all."
        ),
    },
    "configuration_profiles": {
        "path": "/deviceManagement/deviceConfigurations",
        "permission": "DeviceManagementConfiguration.Read.All",
        "pk": "id",
        "columns": [
            _col("id", "string"), _col("displayName", "string"), _col("description", "string"),
            _col("odataType", "string"), _col("createdDateTime", "datetime"),
            _col("lastModifiedDateTime", "datetime"), _col("version", "integer"),
        ],
        "fks": [],
        "desc": (
            "Device configuration profiles. odataType is the profile kind/platform, e.g. "
            "windows10GeneralConfiguration, iosGeneralDeviceConfiguration."
        ),
    },
    "mobile_apps": {
        "path": "/deviceAppManagement/mobileApps",
        "permission": "DeviceManagementApps.Read.All",
        "pk": "id",
        "columns": [
            _col("id", "string"), _col("displayName", "string"), _col("publisher", "string"),
            _col("odataType", "string"), _col("description", "string"),
            _col("developer", "string"), _col("owner", "string"),
            _col("publishingState", "string"), _col("isFeatured", "boolean"),
            _col("createdDateTime", "datetime"), _col("lastModifiedDateTime", "datetime"),
        ],
        "fks": [],
        "desc": (
            "Apps published/managed through Intune. odataType is the app kind, e.g. "
            "win32LobApp, iosStoreApp, androidManagedStoreApp, webApp, microsoftStoreForBusinessApp."
        ),
    },
    "detected_apps": {
        "path": "/deviceManagement/detectedApps",
        "permission": "DeviceManagementManagedDevices.Read.All",
        "pk": "id",
        "columns": [
            _col("id", "string"), _col("displayName", "string"), _col("version", "string"),
            _col("publisher", "string"), _col("platform", "string"),
            _col("deviceCount", "integer"), _col("sizeInByte", "integer"),
        ],
        "fks": [],
        "desc": (
            "Software inventory discovered on managed devices, aggregated per app+version. "
            "deviceCount = devices it was found on. platform e.g. windows, ios, androidOSP, macOS."
        ),
    },
    "autopilot_devices": {
        "path": "/deviceManagement/windowsAutopilotDeviceIdentities",
        "permission": "DeviceManagementServiceConfig.Read.All",
        "pk": "id",
        "columns": [
            _col("id", "string"), _col("serialNumber", "string"), _col("displayName", "string"),
            _col("manufacturer", "string"), _col("model", "string"), _col("groupTag", "string"),
            _col("enrollmentState", "string"), _col("lastContactedDateTime", "datetime"),
            _col("userPrincipalName", "string"), _col("purchaseOrderIdentifier", "string"),
            _col("managedDeviceId", "string"), _col("azureActiveDirectoryDeviceId", "string"),
            _col("systemFamily", "string"), _col("skuNumber", "string"),
        ],
        "fks": [("managedDeviceId", "managed_devices", "id")],
        "desc": (
            "Windows Autopilot device registrations (hardware hashes uploaded for zero-touch "
            "provisioning). enrollmentState: enrolled, notContacted, pendingReset, failed, "
            "blocked, unknown. managedDeviceId links to managed_devices once enrolled."
        ),
    },
}

# Derived (not a Graph property) — never sent in $select.
_DERIVED = {"odataType", "policyId", "policyName"}


class IntuneAccessDenied(ValueError):
    """Graph answered 401/403 for a table: the app lacks its permission."""


class IntuneClient(GraphDriveClient):
    """Microsoft Intune over Microsoft Graph, shaped as a fixed table catalog."""

    capabilities = {Capability.QUERY}

    relative_date_hint = (
        "OData datetime literals are absolute and unquoted ISO-8601 UTC (e.g. "
        "lastSyncDateTime le 2026-01-01T00:00:00Z); compute relative dates in Python "
        "(datetime.now(timezone.utc) - timedelta(days=30)) and inline the value."
    )

    def __init__(self, max_rows: int = MAX_ROWS, **kwargs):
        # Intune is tenant-wide inventory read with application permissions;
        # a delegated user token is never passed for this type.
        kwargs["mode"] = "intune"
        super().__init__(**kwargs)
        try:
            self.max_rows = max(1, min(int(max_rows or MAX_ROWS), MAX_ROWS))
        except (TypeError, ValueError):
            self.max_rows = MAX_ROWS
        # table -> True (readable) / False (permission missing); filled lazily.
        self._access: dict[str, bool] = {}

    # ------------------------------------------------------------- identity

    @property
    def is_document_based(self) -> bool:
        return False

    @property
    def description(self) -> str:
        return (
            "Microsoft Intune — managed devices, compliance, configuration profiles, "
            "apps and Autopilot as tables; query via execute_query with a JSON spec "
            "(OData filters).\n\n" + self.system_prompt()
        )

    # ---------------------------------------------------------------- http

    def _get_checked(self, url: str, params: dict[str, str] | None = None) -> dict:
        resp = self._request("GET", url, params=params, timeout=60.0)
        if resp.status_code in (401, 403):
            raise IntuneAccessDenied(f"Graph {url} → {resp.status_code} {resp.text[:300]}")
        if resp.status_code >= 400:
            raise ValueError(f"Graph {url} → {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def _probe(self, table: str) -> bool:
        """One `$top=1` read: is this table's permission granted?

        Only a definitive 401/403 counts as "no access". Anything else (a
        throttle, a 5xx) propagates — a transient failure must not quietly
        drop a table from the catalog.
        """
        if table in self._access:
            return self._access[table]
        path = TABLES["compliance_policies"]["path"] if table == "compliance_policy_device_statuses" else TABLES[table]["path"]
        try:
            self._get_checked(path, {"$top": "1", "$select": "id"})
            ok = True
        except IntuneAccessDenied:
            ok = False
        self._access[table] = ok
        return ok

    # ------------------------------------------------------------- catalog

    @staticmethod
    def _build_table(name: str) -> Table:
        spec = TABLES[name]
        return Table(
            name=name,
            description=f"{spec['desc']} (Graph {spec['path']})",
            columns=[TableColumn(name=c, dtype=t) for c, t in spec["columns"]],
            pks=[TableColumn(name=spec["pk"], dtype="string")],
            fks=[
                ForeignKey(
                    column=TableColumn(name=col, dtype="string"),
                    references_name=ref_table,
                    references_column=TableColumn(name=ref_col, dtype="string"),
                )
                for col, ref_table, ref_col in spec["fks"]
            ],
            metadata_json={"intune": {"graph_path": spec["path"], "permission": spec["permission"]}},
        )

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        tables: list[Table] = []
        for name in TABLES:
            if not self._probe(name):
                logger.info(
                    "intune: %s omitted from catalog — app lacks %s",
                    name, TABLES[name]["permission"],
                )
                continue
            tables.append(self._build_table(name))
        return tables

    def get_schema(self, table_name: str) -> Table | None:
        return self._build_table(table_name) if table_name in TABLES else None

    def prompt_schema(self) -> str:
        from app.ai.prompt_formatters import ServiceFormatter
        return ServiceFormatter(self.get_schemas()).table_str

    # ------------------------------------------------------------- querying

    @staticmethod
    def _parse_spec(query) -> dict:
        if isinstance(query, dict):
            return dict(query)
        text = str(query or "").strip()
        if text.startswith("{"):
            try:
                spec = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON query spec: {e}")
            if not isinstance(spec, dict):
                raise ValueError("Query spec must be a JSON object.")
            return spec
        # A bare string names a table — fetch it with default options.
        return {"table": text}

    @staticmethod
    def _clean(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(_SENTINEL_PREFIXES):
            return None
        return value

    def _row(self, item: dict, columns: list[str], extra: dict | None = None) -> dict:
        row: dict[str, Any] = {}
        for c in columns:
            if c == "odataType":
                row[c] = (item.get("@odata.type") or "").replace("#microsoft.graph.", "") or None
            elif extra and c in extra:
                row[c] = extra[c]
            else:
                row[c] = self._clean(item.get(c))
        return row

    def _page(
        self,
        url: str,
        params: dict[str, str],
        limit: int,
        columns: list[str],
        extra: dict | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        next_url: str | None = url
        first = True
        while next_url and len(rows) < limit:
            data = self._get_checked(next_url, params if first else None)
            for item in data.get("value") or []:
                rows.append(self._row(item, columns, extra))
                if len(rows) >= limit:
                    break
            next_url = data.get("@odata.nextLink")
            first = False
        return rows

    def _resolve_policies(self, ref: str | None) -> list[dict]:
        data_rows = self._page(
            TABLES["compliance_policies"]["path"],
            {"$select": "id,displayName", "$top": str(PAGE_SIZE)},
            MAX_POLICIES,
            ["id", "displayName"],
        )
        if not ref:
            return data_rows
        want = str(ref).strip().lower()
        hits = [p for p in data_rows if want in ((p["id"] or "").lower(), (p["displayName"] or "").lower())]
        if not hits:
            names = ", ".join(sorted(p["displayName"] or p["id"] for p in data_rows)) or "(none)"
            raise ValueError(f"Compliance policy {ref!r} not found. Policies: {names}")
        return hits

    def execute_query(self, query: str | None = None, table_name: str | None = None, **kwargs) -> pd.DataFrame:
        """Run an Intune query spec and return a DataFrame.

        `query` is a JSON string (or dict):
            {"table": "managed_devices",                       (required)
             "filter": "complianceState eq 'noncompliant'",    (optional OData $filter)
             "select": ["deviceName", "userPrincipalName"],    (optional columns)
             "orderby": "lastSyncDateTime desc",               (optional)
             "limit": 1000,                                     (optional)
             "policy": "Win10 baseline"}                        (compliance_policy_device_statuses only)
        """
        spec = self._parse_spec(query if query is not None else table_name)
        if table_name and "table" not in spec:
            spec["table"] = table_name
        table = str(spec.get("table") or "").strip()
        if table not in TABLES:
            raise ValueError(f"Unknown Intune table {table!r}. Tables: {', '.join(TABLES)}")
        meta = TABLES[table]
        all_cols = [c for c, _ in meta["columns"]]

        if spec.get("select"):
            sel = spec["select"]
            if isinstance(sel, str):
                sel = [s.strip() for s in sel.split(",") if s.strip()]
            unknown = [c for c in sel if c not in all_cols]
            if unknown:
                raise ValueError(
                    f"Unknown column(s) {unknown} on {table}. Columns: {', '.join(all_cols)}"
                )
            columns = list(dict.fromkeys(sel))
        else:
            columns = all_cols

        try:
            limit = int(spec.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = max(1, min(limit, self.max_rows))

        # $select is always sent: it bounds payload size (mobileApps would
        # otherwise inline a base64 icon per app) and Intune only returns some
        # managedDevice properties (e.g. physicalMemoryInBytes) when selected.
        graph_cols = [c for c in columns if c not in _DERIVED]
        if "id" not in graph_cols:
            graph_cols.insert(0, "id")
        params: dict[str, str] = {"$select": ",".join(graph_cols), "$top": str(min(PAGE_SIZE, limit))}
        if spec.get("filter"):
            params["$filter"] = str(spec["filter"])
        if spec.get("orderby"):
            params["$orderby"] = str(spec["orderby"])

        started = time.perf_counter()
        try:
            if table == "compliance_policy_device_statuses":
                rows: list[dict] = []
                for p in self._resolve_policies(spec.get("policy")):
                    if len(rows) >= limit:
                        break
                    pid = urllib.parse.quote(str(p["id"]), safe="")
                    rows.extend(self._page(
                        meta["path"].format(policyId=pid), params, limit - len(rows), columns,
                        extra={"policyId": p["id"], "policyName": p["displayName"]},
                    ))
            else:
                rows = self._page(meta["path"], params, limit, columns)
        except IntuneAccessDenied as e:
            raise ValueError(
                f"Access denied reading {table}: the Entra app needs the "
                f"'{meta['permission']}' application permission (with admin consent). {e}"
            )
        except ValueError as e:
            msg = str(e)
            if (params.get("$filter") or params.get("$orderby")) and "→ 400" in msg:
                raise ValueError(
                    f"Intune rejected the OData filter/orderby ({msg[:240]}). Intune only "
                    "supports $filter/$orderby on some properties. Retry WITHOUT 'filter' "
                    "and 'orderby', then filter/sort the DataFrame in pandas."
                )
            raise

        df = pd.DataFrame(rows).reindex(columns=columns)
        logger.info(
            "intune.execute_query: table=%s filter=%r rows=%d in %.1fs",
            table, spec.get("filter"), len(df), time.perf_counter() - started,
        )
        return df

    # ------------------------------------------------------------- testing

    def test_connection(self) -> dict:
        """Token + one `$top=1` probe per table. Reports missing permissions."""
        t0 = time.perf_counter()
        details: dict[str, Any] = {"mode": "intune", "auth": "service_principal"}

        def _result(success: bool, message: str) -> dict:
            details["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return {"success": success, "message": message, "details": details}

        try:
            self._token()
        except Exception as e:
            return _result(False, f"Could not get a Microsoft Graph token: {e}")
        try:
            readable, missing = [], {}
            for name, meta in TABLES.items():
                if self._probe(name):
                    readable.append(name)
                else:
                    missing.setdefault(meta["permission"], []).append(name)
        except IndexingCancelled:
            raise
        except Exception as e:
            text = str(e)
            if "not applicable to target tenant" in text.lower():
                return _result(False, "Connected to Microsoft Graph, but this tenant has no active Intune license.")
            return _result(False, text)
        details["readable_tables"] = readable
        details["missing_permissions"] = missing
        if not readable:
            perms = ", ".join(sorted(missing))
            return _result(False, f"Token works but no Intune data is readable. Grant these application permissions (with admin consent): {perms}.")
        msg = f"Connected to Intune. {len(readable)} of {len(TABLES)} tables readable."
        if missing:
            msg += " Missing permissions: " + "; ".join(
                f"{perm} ({', '.join(tables)})" for perm, tables in sorted(missing.items())
            ) + "."
        return _result(True, msg)

    # ------------------------------------------------------------- prompts

    def system_prompt(self) -> str:
        # Table notes carry the enum vocabularies a filter must match exactly
        # (Intune is inconsistent: managed_devices says 'noncompliant', device
        # statuses say 'nonCompliant'). Catalog table descriptions are not
        # rendered to the agent, so they ride here.
        notes = "\n".join(f"        - `{name}`: {meta['desc']}" for name, meta in TABLES.items())
        return self._GUIDE.replace("{table_notes}", notes)

    _GUIDE = """
        ## Microsoft Intune Integration
        Query Intune via `client.execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "managed_devices",
         "filter": "complianceState eq 'noncompliant' and operatingSystem eq 'Windows'",
         "select": ["deviceName", "userPrincipalName", "osVersion", "lastSyncDateTime"],
         "orderby": "lastSyncDateTime desc",
         "limit": 1000}
        ```

        - `table` (required): one of the cataloged tables.
        - `filter` (optional): OData $filter using the cataloged camelCase column names.
          Strings and enum values in single quotes ('noncompliant'); datetimes unquoted
          ISO-8601 UTC (lastSyncDateTime le 2026-01-01T00:00:00Z). Intune supports
          filtering only on some properties — if a filter is rejected, re-run without
          `filter`/`orderby` and filter in pandas.
        - `select` (optional): columns to return; omit for all cataloged columns.
        - `orderby` (optional), `limit` (optional, default 1000).
        - `policy` (compliance_policy_device_statuses only): a policy name or id;
          omit to read every policy's device statuses.

        Returns a pandas DataFrame with exactly the requested columns. Datetimes are
        ISO strings (parse with pd.to_datetime(..., utc=True)); Intune's "never"
        sentinels (0001-01-01 / 9999-12-31) are returned as null. Aggregate in pandas
        (Graph has no server-side group-by).

        Tables (use the enum values exactly as written — Intune spells them differently per table):
{table_notes}

        Examples:
        ```python
        df = client.execute_query('{"table": "managed_devices", "filter": "complianceState eq \\'noncompliant\\'"}')
        df = client.execute_query('{"table": "compliance_policy_device_statuses", "filter": "status eq \\'nonCompliant\\'"}')
        df = client.execute_query('{"table": "detected_apps", "orderby": "deviceCount desc", "limit": 50}')
        ```
        """
