from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, ServiceFormatter
from typing import List, Dict, Optional, Tuple
import requests
import pandas as pd
import re
from urllib.parse import unquote


# Internal Vertipaq columns leaked by COLUMNSTATISTICS(): every table carries a
# hidden 'RowNumber-<GUID>' column that can never be referenced in DAX. If it
# reaches the indexed schema, the LLM sees it as a real column and generates
# queries the engine rejects ("cannot be found or may not be used in this
# expression").
_INTERNAL_COLUMN_RE = re.compile(
    r"^RowNumber-[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$", re.IGNORECASE
)


def _is_internal_column(column_name: str) -> bool:
    return bool(_INTERNAL_COLUMN_RE.match((column_name or "").strip()))


def _clean_table_display_name(table_name: str) -> str:
    """
    Clean up Power BI table names for display.

    SharePoint-connected tables have ugly URL-based names like:
    'https://tenant-my sharepoint com/personal/user/Documents/file xlsx'

    This extracts a cleaner display name (e.g., 'file' or 'Documents_file').
    """
    if not table_name:
        return table_name

    # Detect SharePoint/OneDrive URL patterns (dots already replaced with spaces by Power BI)
    if "sharepoint" in table_name.lower() or table_name.startswith("http"):
        # Try to extract the last meaningful segment from the path
        # Replace spaces back to dots for URL parsing, then decode
        normalized = table_name.replace(" ", ".")

        # Remove protocol and domain
        path = re.sub(r'^https?://[^/]+/', '', normalized)

        # Split by / and get meaningful segments
        segments = [s for s in path.split('/') if s and s.lower() not in ('personal', 'documents', 'sites')]

        if segments:
            # Get the last segment (usually the file name)
            last = segments[-1]
            # Remove file extension if present
            last = re.sub(r'\.(xlsx|xls|csv|txt)$', '', last, flags=re.IGNORECASE)
            # Clean up any remaining encoded chars
            last = unquote(last)
            # Replace dots/underscores with spaces, then clean up
            last = re.sub(r'[._]+', ' ', last).strip()

            if last:
                return last

    return table_name


class PowerBIClient(DataSourceClient):
    """
    Power BI client for discovering semantic models and executing DAX queries.

    Auto-discovers all workspaces, datasets (semantic models), and reports
    that the service principal has access to.
    """

    BASE_URL = "https://api.powerbi.com/v1.0/myorg"
    AUTH_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    SCOPE = "https://analysis.windows.net/powerbi/api/.default"

    # Connection-test probe budget: enough to skip a few empty/system models
    # without hammering large tenants.
    MAX_PROBE_WORKSPACES = 5
    MAX_PROBE_DATASETS = 5

    def __init__(
        self,
        tenant_id: str = None,
        client_id: str = None,
        client_secret: str = None,
        access_token: str = None,
        workspaces: str = None,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        # Optional comma-separated workspace names or IDs limiting discovery
        # (mirrors the schema filter on the Fabric connector).
        self.workspaces = workspaces
        self._workspace_filter = {
            w.strip().lower() for w in (workspaces or "").split(",") if w.strip()
        }

        self._access_token: Optional[str] = access_token
        self._http: Optional[requests.Session] = None
        # A delegated (per-user OBO) identity was handed a token at construction;
        # the service principal is built from client_id/secret and mints its own.
        # Only a delegated identity has user-cached permissions worth flushing.
        self._delegated: bool = access_token is not None
        # RefreshUserPermissions is account-wide and idempotent — fire it at most
        # once per client instance (guarded here, invoked from get_schemas).
        self._perms_refreshed: bool = False
        # Whether this deployment's executeQueries endpoint accepts DAX INFO
        # functions (used to read model relationships without admin scope).
        # None = untried; False = the endpoint rejected them, so stop paying a
        # request per dataset to rediscover that. Support is deployment-
        # dependent, so this is measured once per client rather than assumed.
        self._info_functions_supported: Optional[bool] = None

        # Persisted schema metadata injected via attach_table_metadata():
        # schema table name ("Dataset/Table") -> the table's `powerbi` metadata
        # dict ({datasetId, workspaceId, ...}). Lets execute_query resolve the
        # dataset GUID as a dict lookup instead of re-crawling the tenant.
        self._table_metadata_map: Dict[str, Dict] = {}
        # Live-discovery cache: get_schemas() is a full tenant crawl (workspaces,
        # datasets, admin scan, COLUMNSTATISTICS) — run it at most once per
        # client instance.
        self._schemas_cache: Optional[List[Table]] = None
        # Per-run diagnostics: datasets that were listed but produced no schema
        # (no Build permission, RLS, DirectLake, ...). Populated by get_schemas()
        # and surfaced via index_stats() so the indexing job can report which
        # semantic models were found-but-unreadable instead of dropping them
        # silently. Each entry: {datasetId, datasetName, workspaceId,
        # workspaceName, reason}.
        self.discovery_diagnostics: List[Dict] = []
        # Workspaces this identity has no role in, but whose datasets it can
        # still query item-level. Populated lazily on a 401/403 from the
        # workspace-scoped endpoint; makes the tenant-level URL sticky so the
        # fallback is paid once per workspace, not once per query.
        self._tenant_scoped_workspaces: set = set()

    def attach_table_metadata(self, tables: List[Dict]) -> None:
        """Inject persisted table metadata (from the indexed schema catalog).

        `tables` is a list of {"name": <schema table name>, "metadata_json": {...}}
        entries. Only entries carrying `powerbi.datasetId` are kept. Called by
        DataSourceService.construct_clients so query-time table_name resolution
        needs zero API calls.
        """
        mapping: Dict[str, Dict] = {}
        for t in tables or []:
            try:
                name = (t.get("name") or "").strip()
                meta = t.get("metadata_json") or {}
                pbi = meta.get("powerbi") if isinstance(meta, dict) else None
                if name and isinstance(pbi, dict) and pbi.get("datasetId"):
                    mapping[name] = pbi
            except Exception:
                continue
        self._table_metadata_map = mapping

    def _resolve_ids_from_metadata(self, table_name: str) -> Optional[Dict]:
        """Resolve a table reference to its `powerbi` metadata using the
        attached map. Matches the exact schema name first, then falls back to
        case-insensitive and tableName/datasetName/datasetId matches."""
        if not table_name or not self._table_metadata_map:
            return None
        pbi = self._table_metadata_map.get(table_name)
        if pbi:
            return pbi
        lowered = table_name.strip().lower()
        for name, meta in self._table_metadata_map.items():
            if name.strip().lower() == lowered:
                return meta
        for meta in self._table_metadata_map.values():
            candidates = (
                str(meta.get("tableName") or "").strip().lower(),
                str(meta.get("datasetName") or "").strip().lower(),
                str(meta.get("datasetId") or "").strip().lower(),
            )
            if lowered in candidates and lowered:
                return meta
        return None

    def connect(self):
        """
        Authenticate with Azure AD and obtain an access token for Power BI API.
        Reuses cached token if already authenticated.
        """
        if self._http and self._access_token:
            return

        # If a delegated access_token was provided, just set up the session
        if self._access_token:
            self._http = requests.Session()
            return

        auth_url = self.AUTH_URL.format(tenant_id=self.tenant_id)
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.SCOPE,
        }

        resp = requests.post(auth_url, data=payload, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(f"Failed to authenticate with Azure AD: HTTP {resp.status_code} {resp.text}")

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError("Authentication did not return access token")

        self._access_token = token
        self._http = requests.Session()

    def refresh_user_permissions(self) -> bool:
        """Force Power BI to re-evaluate THIS user's cached permissions.

        Power BI serves workspace/dataset permissions from a replicated cache,
        so a grant or revocation made in the portal lags by an unbounded window
        — the Get Groups reference warns "user permissions for workspaces take
        time to get updated and may not be immediately available". During that
        window a just-revoked user still reads rows they should not, and a
        just-granted user sees nothing. This is Microsoft's documented flush.

        Fired once per client, only for a delegated (per-user) identity: a
        service principal has no user permission cache, and the effect is
        account-wide so repeating it is waste. Best-effort — a failure here must
        never break discovery, so the caller proceeds regardless. Note the flush
        is asynchronous on Microsoft's side; it reliably freshens the user's
        NEXT queries (the security-critical path) and usually the crawl that
        follows it in this same request.

        SINGLE ATTEMPT on purpose: this endpoint is aggressively rate-limited
        (429 with a ~30s Retry-After). Letting the shared `_request` backoff loop
        retry would block the overlay sync — and thus interactive sign-in /
        reload — for up to a minute on a call we do not even need to succeed. A
        429 here just means the cache was flushed recently, which is fine; move
        on immediately.
        """
        if not self._delegated or self._perms_refreshed:
            return False
        self._perms_refreshed = True
        try:
            self.connect()
            resp = self._request(
                "POST", f"{self.BASE_URL}/RefreshUserPermissions",
                timeout=30, max_attempts=1,
            )
            return resp.status_code < 300
        except Exception:
            return False

    def test_connection(self) -> Dict:
        """
        Validate credentials and API access.

        The DAX probe classifies failures by which layer answered, not by
        message text: a 401/403 is a real permission problem, while ANY
        response from the Analysis Services engine (including "model has no
        tables") proves auth, routing, and query access all work. Probes
        several datasets so one empty/system model can't fail the test.
        """
        # Phase 1: Authenticate
        try:
            self.connect()
        except Exception as e:
            return {
                "success": False,
                "message": f"Authentication failed: {e}",
            }

        # Phase 2: List workspaces (applies the configured workspace filter).
        # Without a filter, only the first page is fetched to stay fast.
        try:
            workspaces = self.list_workspaces(first_page_only=not self._workspace_filter)
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to list workspaces: {e}",
            }

        if not workspaces:
            # No workspace ROLE anywhere is normal for an end user under RLS —
            # they are kept out of workspaces on purpose. Before failing them,
            # check the known catalog for a model they can query item-level.
            ok, detail = self._probe_known_catalog()
            if ok:
                return {
                    "success": True,
                    "message": (
                        "Connected to Power BI. You have no workspace role, but query access "
                        f"was verified on {detail}."
                    ),
                    "workspaces": 0,
                }
            if self._workspace_filter:
                return {
                    "success": False,
                    "message": (
                        f"Connected, but none of the configured workspaces ({self.workspaces}) were found. "
                        "Check the names/IDs and ensure the service principal is a Member/Contributor of those workspaces."
                    ),
                    "connectivity": True,
                }
            return {
                "success": False,
                "message": (
                    "Connected, but no workspace or semantic model was reachable with this identity. "
                    "For a service principal, ensure it is a Member/Contributor of at least one workspace. "
                    "For a personal sign-in, ask an admin for Build permission on the semantic models you need."
                ),
                "connectivity": True,
            }

        # Phase 3: Probe datasets across workspaces until one query reaches the engine
        probed = 0
        datasets_seen = 0
        engine_details: List[str] = []   # engine answered, model unqueryable (empty, RLS, ...)
        permission_error: Optional[str] = None
        last_error: Optional[str] = None

        for ws in workspaces[: self.MAX_PROBE_WORKSPACES]:
            if probed >= self.MAX_PROBE_DATASETS:
                break
            ws_id = ws.get("id")
            ws_name = ws.get("name") or ws_id
            try:
                ds_list = self.list_datasets(ws_id)
            except Exception as e:
                last_error = f"Failed to list datasets in workspace '{ws_name}': {e}"
                continue
            datasets_seen += len(ds_list)

            for ds in ds_list:
                if probed >= self.MAX_PROBE_DATASETS:
                    break
                probed += 1
                ds_name = ds.get("name") or ds.get("id")
                outcome, detail = self._probe_dataset_query(ws_id, ds.get("id"))

                if outcome == "ok":
                    return {
                        "success": True,
                        "message": (
                            f"Connected to Power BI. Verified query access on dataset "
                            f"'{ds_name}' in workspace '{ws_name}'."
                        ),
                        "workspaces": len(workspaces),
                        "datasets": datasets_seen,
                    }
                if outcome == "engine":
                    # The semantic engine answered — credentials and query
                    # access are proven; only this particular model is
                    # unqueryable (empty, OLS-hidden tables, RLS, ...).
                    engine_details.append(f"'{ds_name}' ({ws_name}): {detail}")
                elif outcome == "forbidden":
                    permission_error = f"dataset '{ds_name}' in workspace '{ws_name}': {detail}"
                elif outcome == "error":
                    last_error = f"dataset '{ds_name}' in workspace '{ws_name}': {detail}"
                # outcome == "skip" (404/stale) → try the next dataset

        if engine_details:
            # Query access verified — every probed model just had nothing to query.
            return {
                "success": True,
                "message": (
                    f"Connected to Power BI ({len(workspaces)} workspace(s), {datasets_seen} dataset(s)). "
                    f"Query access verified, but the {len(engine_details)} probed model(s) were empty or "
                    f"not queryable: {'; '.join(engine_details[:3])}"
                ),
                "workspaces": len(workspaces),
                "datasets": datasets_seen,
            }

        if permission_error:
            # The probed workspaces were all forbidden — but the identity may
            # still hold Build on a model elsewhere in the known catalog. That
            # is the whole point of a delegated connection, so check before
            # reporting a permission failure.
            ok, detail = self._probe_known_catalog()
            if ok:
                return {
                    "success": True,
                    "message": f"Connected to Power BI. Verified query access on {detail}.",
                }
            return {
                "success": False,
                "message": (
                    f"Connected but not authorized to query {permission_error}. "
                    "Ensure the service principal is a Member or Contributor of the workspace "
                    "(Viewer is not enough), and that 'Allow service principals to use Power BI APIs' "
                    "is enabled in the tenant settings. If this is a personal sign-in, you need "
                    "Build permission on the semantic model (and, on an RLS model, membership of an RLS role)."
                ),
                "connectivity": True,
            }

        if datasets_seen == 0:
            return {
                "success": False,
                "message": (
                    f"Connected to {len(workspaces)} workspace(s) but found no datasets. "
                    "Ensure the service principal is a Member/Contributor of workspaces that contain semantic models."
                ),
                "connectivity": True,
            }

        return {
            "success": False,
            "message": f"Connected but could not verify query access: {last_error or 'no dataset could be probed'}",
            "connectivity": True,
        }

    def _probe_known_catalog(self, limit: int = 10) -> Tuple[bool, str]:
        """Is ANY model in the indexed catalog queryable by this identity?

        The workspace crawl only sees workspaces the caller holds a role in, so
        it cannot speak for an identity whose access is item-level — the normal
        shape under RLS. The stored catalog (attached via attach_table_metadata)
        supplies the dataset IDs to try; a single success proves query access.
        Returns (ok, human-readable detail).
        """
        seen: Dict[str, str] = {}
        for meta in (self._table_metadata_map or {}).values():
            ds_id = meta.get("datasetId")
            if ds_id and ds_id not in seen:
                seen[ds_id] = meta.get("datasetName") or ds_id
            if len(seen) >= limit:
                break
        for ds_id, ds_name in seen.items():
            if self._can_query_dataset(ds_id):
                return True, f"semantic model '{ds_name}'"
        return False, ""

    def _probe_dataset_query(self, workspace_id: str, dataset_id: str) -> Tuple[str, str]:
        """
        Run a minimal DAX query against one dataset and classify the outcome
        by which layer answered:

          - "ok":        query succeeded
          - "engine":    the AS engine answered with a model-level error
                         (empty model, OLS/RLS, invalid state) — query access
                         itself is proven
          - "forbidden": 401/403 — a real permission problem
          - "skip":      404 — stale/deleted dataset, try another
          - "error":     anything else (5xx, network, ...)
        """
        try:
            url = f"{self.BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
            body = {
                "queries": [{"query": "EVALUATE ROW(\"test\", 1)"}],
                "serializerSettings": {"includeNulls": True},
            }
            resp = self._request("POST", url, json_body=body, timeout=30)

            if resp.status_code < 300:
                return "ok", ""
            if resp.status_code in (401, 403):
                return "forbidden", self._extract_pbi_error(resp) or f"HTTP {resp.status_code}"
            if resp.status_code == 404:
                return "skip", "HTTP 404"

            detail = self._extract_pbi_error(resp)
            if detail:
                # A structured pbi.error means the request authenticated and
                # reached the semantic engine; the model itself is the problem.
                return "engine", detail
            return "error", f"HTTP {resp.status_code} {resp.text[:300]}"
        except Exception as e:
            return "error", str(e)

    @staticmethod
    def _extract_pbi_error(resp) -> str:
        """Pull the human-readable detail out of a Power BI error response."""
        try:
            err = (resp.json() or {}).get("error", {})
            pbi_err = err.get("pbi.error", {})
            for d in pbi_err.get("details", []):
                val = (d.get("detail") or {}).get("value", "")
                if val:
                    return val
            return err.get("message") or ""
        except Exception:
            return ""

    def list_workspaces(self, first_page_only: bool = False) -> List[Dict]:
        """
        List workspaces (groups) the service principal has access to,
        restricted to the configured `workspaces` filter when one is set
        (matches on workspace name or ID, case-insensitive).
        """
        self.connect()
        url = f"{self.BASE_URL}/groups"

        results: List[Dict] = []
        while url:
            resp = self._request("GET", url, timeout=30)
            if resp.status_code >= 300:
                raise RuntimeError(f"Failed to list workspaces: HTTP {resp.status_code} {resp.text}")

            payload = resp.json() or {}
            items = payload.get("value") or []
            for ws in items:
                if not self._workspace_allowed(ws):
                    continue
                results.append({
                    "id": ws.get("id"),
                    "name": ws.get("name"),
                    "type": ws.get("type"),
                    "isOnDedicatedCapacity": ws.get("isOnDedicatedCapacity"),
                })
            # With a filter, stop early once every configured workspace is found
            if self._workspace_filter and len(results) >= len(self._workspace_filter):
                break
            if first_page_only:
                break
            url = payload.get("@odata.nextLink")

        return results

    def _workspace_allowed(self, ws: Dict) -> bool:
        """Check a workspace against the configured filter (name or ID)."""
        if not self._workspace_filter:
            return True
        ws_id = (ws.get("id") or "").strip().lower()
        ws_name = (ws.get("name") or "").strip().lower()
        return ws_id in self._workspace_filter or ws_name in self._workspace_filter

    def list_datasets(self, workspace_id: str) -> List[Dict]:
        """
        List all datasets (semantic models) in a workspace.
        """
        self.connect()
        url = f"{self.BASE_URL}/groups/{workspace_id}/datasets"

        results: List[Dict] = []
        while url:
            resp = self._request("GET", url, timeout=30)
            if resp.status_code >= 300:
                raise RuntimeError(f"Failed to list datasets: HTTP {resp.status_code} {resp.text}")

            payload = resp.json() or {}
            items = payload.get("value") or []
            for ds in items:
                results.append({
                    "id": ds.get("id"),
                    "name": ds.get("name"),
                    "configuredBy": ds.get("configuredBy"),
                    "isRefreshable": ds.get("isRefreshable"),
                    "isOnPremGatewayRequired": ds.get("isOnPremGatewayRequired"),
                    "webUrl": ds.get("webUrl"),
                    # Row-level security markers. These come free with the
                    # listing we already make — no admin scope, no extra call —
                    # and they are the ONLY reliable signal that results may be
                    # row-filtered. RLS filtering itself is undetectable: a
                    # filtered query returns HTTP 200 with fewer rows, which is
                    # indistinguishable from a genuinely small result.
                    "isEffectiveIdentityRequired": ds.get("isEffectiveIdentityRequired"),
                    "isEffectiveIdentityRolesRequired": ds.get("isEffectiveIdentityRolesRequired"),
                })
            url = payload.get("@odata.nextLink")

        return results

    def list_reports(self, workspace_id: str) -> List[Dict]:
        """
        List all reports in a workspace.
        """
        self.connect()
        url = f"{self.BASE_URL}/groups/{workspace_id}/reports"

        results: List[Dict] = []
        while url:
            resp = self._request("GET", url, timeout=30)
            if resp.status_code >= 300:
                raise RuntimeError(f"Failed to list reports: HTTP {resp.status_code} {resp.text}")

            payload = resp.json() or {}
            items = payload.get("value") or []
            for rpt in items:
                results.append({
                    "id": rpt.get("id"),
                    "name": rpt.get("name"),
                    "datasetId": rpt.get("datasetId"),
                    "webUrl": rpt.get("webUrl"),
                    "reportType": rpt.get("reportType"),
                })
            url = payload.get("@odata.nextLink")

        return results

    def get_dataset_tables(self, workspace_id: str, dataset_id: str) -> tuple:
        """
        Get tables and columns for a single dataset.
        Uses COLUMNSTATISTICS (no relationships) with REST API fallback.
        For bulk discovery with relationships, use _batch_admin_scan() instead.

        Returns:
            tuple: (tables_list, relationships_list)
        """
        tables, rels, _ = self.get_dataset_tables_with_reason(workspace_id, dataset_id)
        return tables, rels

    def get_dataset_tables_with_reason(self, workspace_id: str, dataset_id: str) -> tuple:
        """
        Like get_dataset_tables, but also returns a human-readable reason when
        no tables could be introspected. Lets discovery record *why* a semantic
        model produced no schema (no Build permission, RLS, DirectLake, ...)
        instead of dropping it silently.

        Returns:
            tuple: (tables_list, relationships_list, reason_or_None)
                   reason is None on success, else a short diagnostic string.
        """
        self.connect()
        headers = self._build_headers()

        # Primary: one request for columns + types + measures + relationships.
        tables, rels, meta_reason = self._get_model_metadata_via_dax(workspace_id, dataset_id)
        if tables:
            self._add_relationship_key_columns(tables, rels)
            return tables, rels, None

        # Fallback for endpoints that reject DAX INFO functions: COLUMNSTATISTICS
        # gives column names only (no types, no measures), so relationships need
        # their own request here.
        tables, _, stats_reason = self._get_tables_via_column_stats_with_reason(
            workspace_id, dataset_id
        )
        if tables:
            rels = self._get_relationships_via_dax(workspace_id, dataset_id)
            self._add_relationship_key_columns(tables, rels)
            return tables, rels, None

        # Fallback: REST API /tables (only works for Push datasets)
        url = f"{self.BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/tables"
        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code < 300:
            rest_tables = (resp.json() or {}).get("value") or []
            if rest_tables and any(t.get("columns") for t in rest_tables):
                rels = self._get_relationships_via_dax(workspace_id, dataset_id)
                self._add_relationship_key_columns(rest_tables, rels)
                return rest_tables, rels, None

        # Nothing worked. Prefer the COLUMNSTATISTICS reason (most informative),
        # then the metadata-query reason; fall back to describing the REST attempt.
        reason = stats_reason or meta_reason or (
            f"table introspection returned no columns (REST /tables HTTP {resp.status_code})"
        )
        return [], [], reason

    def _get_tables_via_column_stats(self, workspace_id: str, dataset_id: str) -> tuple:
        """Back-compat wrapper: (tables, relationships) without the reason."""
        tables, rels, _ = self._get_tables_via_column_stats_with_reason(
            workspace_id, dataset_id
        )
        return tables, rels

    def _get_tables_via_column_stats_with_reason(self, workspace_id: str, dataset_id: str) -> tuple:
        """
        Get table/column metadata using DAX COLUMNSTATISTICS() function.
        Works for most imported and DirectQuery datasets.

        Returns:
            tuple: (tables_list, relationships_list, reason_or_None) - relationships
                   always empty for this method; reason is None on success, else a
                   short diagnostic (the Power BI error, or "empty result").
        """
        import logging

        try:
            # COLUMNSTATISTICS() returns: Table Name, Column Name, Min, Max, Cardinality, Max Length
            stats_dax = "EVALUATE COLUMNSTATISTICS()"
            stats_df = self._execute_dax_internal(workspace_id, dataset_id, stats_dax)
            if stats_df.empty:
                return [], [], "COLUMNSTATISTICS returned no rows"

            # Build tables structure from column stats
            tables_dict: Dict[str, Dict] = {}

            for _, row in stats_df.iterrows():
                table_name = str(row.get("Table Name", ""))
                col_name = str(row.get("Column Name", ""))

                if not table_name or not col_name:
                    continue

                # Skip internal/system tables
                if self._is_system_table(table_name):
                    continue

                # Skip internal engine columns (RowNumber-<GUID>): not
                # queryable in DAX, must not reach the indexed schema.
                if _is_internal_column(col_name):
                    continue

                if table_name not in tables_dict:
                    tables_dict[table_name] = {"name": table_name, "columns": [], "measures": []}

                tables_dict[table_name]["columns"].append({
                    "name": col_name,
                    "dataType": "unknown",  # COLUMNSTATISTICS doesn't return data type
                })

            # No relationships available via COLUMNSTATISTICS
            if not tables_dict:
                return [], [], "COLUMNSTATISTICS returned only system tables"
            return list(tables_dict.values()), [], None

        except Exception as e:
            logging.warning(f"COLUMNSTATISTICS failed for dataset {dataset_id}: {e}")
            return [], [], f"COLUMNSTATISTICS failed: {self._short_error(e)}"

    @staticmethod
    def _short_error(e: Exception) -> str:
        """Condense a raised error into a short, log-safe reason.

        Power BI distinguishes the failure modes by error code, and the
        distinction is actionable — "join an RLS role" and "get Build
        permission" are different requests to a different person. Collapsing
        them into one message sent every denied user down the wrong path.
        """
        msg = str(e)
        if "RLSNotAuthorizedForModel" in msg:
            return ("not a member of any row-level-security role on this model "
                    "(Build permission alone is not sufficient)")
        if "PowerBIEntityNotFound" in msg:
            return "no access to this semantic model (not shared with this identity)"
        if "HTTP 401" in msg or "HTTP 403" in msg:
            return "not authorized to query (Build permission required, or RLS with no effective identity)"
        return msg[:200]

    # Relationships, expressed as a DAX projection over the model's own
    # metadata. SELECTCOLUMNS keeps the payload to the six fields we map,
    # instead of the ~15 INFO.VIEW.RELATIONSHIPS returns.
    _RELATIONSHIPS_DAX = """EVALUATE
SELECTCOLUMNS(
    INFO.VIEW.RELATIONSHIPS(),
    "FromTable", [FromTable],
    "FromColumn", [FromColumn],
    "ToTable", [ToTable],
    "ToColumn", [ToColumn],
    "IsActive", [IsActive],
    "CrossFilteringBehavior", [CrossFilteringBehavior]
)"""

    # The whole model's metadata in ONE request: columns (with real data types,
    # hidden flags and data categories), measures, and relationships, UNIONed
    # into a single projection. executeQueries accepts only one query per call,
    # so the alternative is three round-trips per dataset — and discovery is
    # rate-limited (~120 requests/min/user, shared with the user's real
    # queries), which on a tenant with thousands of semantic models is the
    # difference between minutes and hours. Same call budget as the
    # COLUMNSTATISTICS-only discovery it replaces.
    _MODEL_METADATA_DAX = """EVALUATE
UNION(
    SELECTCOLUMNS(INFO.VIEW.COLUMNS(),
        "Kind", "C", "Tbl", [Table], "Name", [Name],
        "Info1", [DataType], "Info2", [DataCategory], "Flag", [IsHidden]),
    SELECTCOLUMNS(INFO.VIEW.MEASURES(),
        "Kind", "M", "Tbl", [Table], "Name", [Name],
        "Info1", [DataType], "Info2", "", "Flag", [IsHidden]),
    SELECTCOLUMNS(INFO.VIEW.RELATIONSHIPS(),
        "Kind", "R", "Tbl", [FromTable], "Name", [FromColumn],
        "Info1", [ToTable], "Info2", [ToColumn], "Flag", [IsActive])
)"""

    @staticmethod
    def _truthy(value) -> bool:
        """executeQueries serializes booleans inconsistently across models."""
        if isinstance(value, str):
            return value.strip().lower() not in ("", "false", "0", "no")
        return bool(value)

    def _get_model_metadata_via_dax(self, workspace_id: str, dataset_id: str) -> tuple:
        """Read columns + data types + measures + relationships in one request.

        This is the primary discovery path. It supersedes COLUMNSTATISTICS,
        which returns neither data types (every column indexed as "unknown"),
        nor measures (a semantic model's actual business logic), nor
        relationships — leaving the agent to write DAX against an untyped,
        join-less, measure-less schema.

        Verified against a live tenant: on an RLS-protected model this is
        refused with exactly the same 401 as COLUMNSTATISTICS, so replacing the
        older probe does not risk indexing a model the identity cannot query.

        Returns:
            tuple: (tables_list, relationships_list, reason_or_None)
        """
        import logging

        if self._info_functions_supported is False:
            return [], [], "DAX INFO functions unsupported on this endpoint"

        try:
            df = self._execute_dax_internal(workspace_id, dataset_id, self._MODEL_METADATA_DAX)
        except Exception as e:
            msg = str(e)
            per_dataset = any(code in msg for code in ("HTTP 401", "HTTP 403", "HTTP 404"))
            if not per_dataset:
                self._info_functions_supported = False
                logging.info(
                    "PowerBI: executeQueries rejected DAX INFO functions (%s) — "
                    "falling back to COLUMNSTATISTICS (no types, measures or relationships)",
                    self._short_error(e),
                )
            return [], [], f"model metadata query failed: {self._short_error(e)}"

        self._info_functions_supported = True
        if df.empty:
            return [], [], "model metadata query returned no rows"

        tables_dict: Dict[str, Dict] = {}
        relationships: List[Dict] = []

        def _table(name: str) -> Optional[Dict]:
            if not name or self._is_system_table(name):
                return None
            if name not in tables_dict:
                tables_dict[name] = {"name": name, "columns": [], "measures": []}
            return tables_dict[name]

        for _, row in df.iterrows():
            kind = str(row.get("Kind") or "")
            tbl_name = str(row.get("Tbl") or "")
            name = str(row.get("Name") or "")
            info1 = row.get("Info1")
            info2 = row.get("Info2")
            flag = row.get("Flag")

            if kind == "C":
                tbl = _table(tbl_name)
                if tbl is None or not name:
                    continue
                # RowNumber-<GUID> internal columns: identified by the model's
                # own DataCategory here rather than by matching the name, with
                # the name check kept as a backstop for models that don't set it.
                if str(info2 or "") == "RowNumber" or _is_internal_column(name):
                    continue
                tbl["columns"].append({
                    "name": name,
                    "dataType": str(info1) if info1 else "unknown",
                    "isHidden": self._truthy(flag),
                })
            elif kind == "M":
                tbl = _table(tbl_name)
                if tbl is None or not name:
                    continue
                # INFO.VIEW.MEASURES does not expose [Expression] — measured, not
                # assumed. That is survivable: DAX invokes a measure by NAME, so
                # the agent can use it without seeing its definition. Expressions
                # remain available only through the admin scan.
                tbl["measures"].append({
                    "name": name,
                    "expression": "",
                    "dataType": str(info1) if info1 else "unknown",
                    "isHidden": self._truthy(flag),
                })
            elif kind == "R":
                to_table = str(info1 or "")
                to_column = str(info2 or "")
                if not (tbl_name and name and to_table and to_column):
                    continue
                # The table list above drops auto-generated date tables via
                # `_table()`; without the same test here an edge survives to a
                # table that was never emitted, and the agent is handed
                # `references {dataset}/LocalDateTable_<guid>(Date)` for
                # something it can neither see nor query in DAX.
                if self._is_system_table(tbl_name) or self._is_system_table(to_table):
                    continue
                # Inactive relationships are ignored by the engine unless a query
                # opts in with USERELATIONSHIP; presenting them as joinable would
                # invite silently wrong results.
                if not self._truthy(flag):
                    continue
                relationships.append({
                    "fromTable": tbl_name,
                    "fromColumn": name,
                    "toTable": to_table,
                    "toColumn": to_column,
                    "crossFilteringBehavior": None,
                })

        if not tables_dict:
            return [], [], "model metadata query returned only system tables"
        return list(tables_dict.values()), relationships, None

    @staticmethod
    def _is_system_table(name: str) -> bool:
        """True for the tables Power BI generates rather than the author.

        The engine creates a hidden `LocalDateTable_<guid>` per date column (and
        `DateTableTemplate_<guid>` behind the auto date/time feature). They are
        excluded from the indexed catalog because they are not tables an author
        modelled or an agent can usefully query.

        Shared rather than inlined because the rule has to hold in two places at
        once: the table list AND every relationship list. Filtering only the
        former is what let edges reference tables that were never emitted.
        """
        return bool(name) and (
            name.startswith("DateTableTemplate") or name.startswith("LocalDateTable")
        )

    def _get_relationships_via_dax(self, workspace_id: str, dataset_id: str) -> List[Dict]:
        """Read a model's relationships with the querying identity's own token.

        The Admin Scanner API is the only other source we have for them, and it
        needs tenant-admin scope — which a delegated (OBO) identity never holds
        and a service principal only holds when two Fabric admin-portal settings
        are enabled. Without this, every non-admin deployment indexes semantic
        models with ZERO relationships, the agent sees empty `fks`, and it tells
        users the tables cannot be joined (they can — the engine applies the
        relationships at query time regardless of what we discovered).

        `INFO.VIEW.RELATIONSHIPS()` is documented as unsupported on the JSON
        `executeQueries` endpoint, so this is best-effort: the first rejection
        flips `_info_functions_supported` and every later dataset in the crawl
        skips the call. That caps the cost of an unsupported deployment at ONE
        wasted request per client, while a deployment that does accept it gets
        relationships for free on the non-admin path.

        Returns the same relationship shape as `_parse_admin_scan_tables`;
        empty on any failure (never raises — discovery must not die over this).
        """
        import logging

        if self._info_functions_supported is False:
            return []

        try:
            df = self._execute_dax_internal(workspace_id, dataset_id, self._RELATIONSHIPS_DAX)
        except Exception as e:
            msg = str(e)
            # 401/403/404 are about THIS dataset (no Build permission, RLS,
            # deleted model) — other datasets may still answer, so don't let one
            # of them disable the whole feature. Anything else (typically a 400
            # "function not supported") is a property of the endpoint itself.
            per_dataset = any(code in msg for code in ("HTTP 401", "HTTP 403", "HTTP 404"))
            if not per_dataset:
                self._info_functions_supported = False
                logging.info(
                    "PowerBI: executeQueries rejected DAX INFO functions (%s) — "
                    "relationship discovery unavailable on this endpoint; models "
                    "will index without relationships unless the admin scan covers them",
                    self._short_error(e),
                )
            return []

        self._info_functions_supported = True
        if df.empty:
            return []

        relationships: List[Dict] = []
        for _, row in df.iterrows():
            from_table = str(row.get("FromTable") or "")
            from_column = str(row.get("FromColumn") or "")
            to_table = str(row.get("ToTable") or "")
            to_column = str(row.get("ToColumn") or "")
            if not (from_table and from_column and to_table and to_column):
                continue
            # Same reason as the model-metadata path: an edge to a generated
            # date table names something the catalog never emits.
            if self._is_system_table(from_table) or self._is_system_table(to_table):
                continue
            # Inactive relationships are NOT applied by the engine unless a query
            # opts in via USERELATIONSHIP. Presenting them like active ones would
            # invite silently wrong joins, so they are dropped.
            is_active = row.get("IsActive")
            if isinstance(is_active, str):
                is_active = is_active.strip().lower() not in ("false", "0", "no")
            if is_active is not None and not is_active:
                continue
            relationships.append({
                "fromTable": from_table,
                "fromColumn": from_column,
                "toTable": to_table,
                "toColumn": to_column,
                "crossFilteringBehavior": row.get("CrossFilteringBehavior"),
            })
        return relationships

    @staticmethod
    def _add_relationship_key_columns(tables: List[Dict], relationships: List[Dict]) -> None:
        """Ensure every column a relationship joins on exists in the table's
        column list, adding it when it doesn't. Mutates `tables` in place.

        Join keys are routinely marked hidden in a semantic model — hiding the
        surrogate key is the standard convention once a relationship handles the
        join — and hidden is a report-authoring flag, not a permission: the
        column is fully queryable in DAX. But the admin scan drops hidden
        columns, so exactly the columns needed to join arrive missing, and the
        agent reports that the fact table has no field identifying the entity.

        A foreign key pointing at a column absent from the schema is worse than
        useless, so re-add it wherever a relationship proves it exists.
        """
        if not relationships:
            return
        by_name = {t.get("name"): t for t in tables if t.get("name")}
        for rel in relationships:
            for tbl_name, col_name in (
                (rel.get("fromTable"), rel.get("fromColumn")),
                (rel.get("toTable"), rel.get("toColumn")),
            ):
                tbl = by_name.get(tbl_name)
                if not tbl or not col_name:
                    continue
                cols = tbl.setdefault("columns", [])
                if any((c.get("name") or "") == col_name for c in cols):
                    continue
                cols.append({
                    "name": col_name,
                    "dataType": "unknown",
                    "isHidden": True,
                    "isRelationshipKey": True,
                })

    def _get_tables_via_admin_scan(self, workspace_id: str, dataset_id: str) -> tuple:
        """
        Get table/column metadata using the Admin Scanner API.
        Requires the service principal to have admin permissions.

        Returns:
            tuple: (tables_list, relationships_list)
        """
        import time
        import logging

        try:
            headers = self._build_headers()

            # Step 1: Initiate workspace scan with datasetSchema=true
            scan_url = f"{self.BASE_URL}/admin/workspaces/getInfo?datasetSchema=true"
            body = {"workspaces": [workspace_id]}

            resp = self._http.post(scan_url, json=body, headers=headers, timeout=30)
            if resp.status_code >= 300:
                logging.warning(f"Admin scan initiation failed: HTTP {resp.status_code} {resp.text}")
                return [], []

            scan_data = resp.json() or {}
            scan_id = scan_data.get("id")
            if not scan_id:
                logging.warning("Admin scan did not return scan ID")
                return [], []

            # Step 2: Poll for scan completion (max 30 seconds)
            status_url = f"{self.BASE_URL}/admin/workspaces/scanStatus/{scan_id}"
            for _ in range(15):
                time.sleep(2)
                status_resp = self._http.get(status_url, headers=headers, timeout=30)
                if status_resp.status_code >= 300:
                    continue
                status_data = status_resp.json() or {}
                if status_data.get("status") == "Succeeded":
                    break
            else:
                logging.warning(f"Admin scan timed out for workspace {workspace_id}")
                return [], []

            # Step 3: Get scan results
            result_url = f"{self.BASE_URL}/admin/workspaces/scanResult/{scan_id}"
            result_resp = self._http.get(result_url, headers=headers, timeout=60)
            if result_resp.status_code >= 300:
                logging.warning(f"Failed to get scan results: HTTP {result_resp.status_code}")
                return [], []

            result_data = result_resp.json() or {}
            workspaces = result_data.get("workspaces") or []

            # Find the dataset in the scan results
            for ws in workspaces:
                for ds in ws.get("datasets") or []:
                    if ds.get("id") == dataset_id:
                        return self._parse_admin_scan_tables(ds)

            return [], []

        except Exception as e:
            logging.warning(f"Failed to get tables via admin scan for dataset {dataset_id}: {e}")
            return [], []

    def _parse_admin_scan_tables(self, dataset: Dict) -> tuple:
        """Parse tables/columns/measures/relationships from Admin Scanner API response.

        Returns:
            tuple: (tables_list, relationships_list)
        """
        tables_dict: Dict[str, Dict] = {}

        for tbl in dataset.get("tables") or []:
            tbl_name = tbl.get("name") or ""
            if not tbl_name or tbl.get("isHidden"):
                continue

            if tbl_name not in tables_dict:
                tables_dict[tbl_name] = {"name": tbl_name, "columns": [], "measures": []}

            # Add columns. Hidden columns are KEPT (flagged, not dropped): in a
            # semantic model `isHidden` means "don't offer this to report
            # authors", not "inaccessible" — it is fully queryable in DAX, and
            # hiding surrogate/foreign keys once a relationship covers the join
            # is the standard convention. Dropping them removed precisely the
            # columns needed to join, leaving the agent to conclude the fact
            # table had no key to the dimension.
            for col in tbl.get("columns") or []:
                col_name = col.get("name") or ""
                if col_name and not _is_internal_column(col_name):
                    tables_dict[tbl_name]["columns"].append({
                        "name": col_name,
                        "dataType": col.get("dataType") or "unknown",
                        "isHidden": bool(col.get("isHidden")),
                    })

            # Add measures
            for measure in tbl.get("measures") or []:
                measure_name = measure.get("name") or ""
                if measure_name and not measure.get("isHidden"):
                    tables_dict[tbl_name]["measures"].append({
                        "name": measure_name,
                        "expression": measure.get("expression") or "",
                    })

        # Extract relationships
        relationships = []
        for rel in dataset.get("relationships") or []:
            from_table = rel.get("fromTable") or ""
            from_column = rel.get("fromColumn") or ""
            to_table = rel.get("toTable") or ""
            to_column = rel.get("toColumn") or ""
            if self._is_system_table(from_table) or self._is_system_table(to_table):
                continue
            if from_table and from_column and to_table and to_column:
                relationships.append({
                    "fromTable": from_table,
                    "fromColumn": from_column,
                    "toTable": to_table,
                    "toColumn": to_column,
                    "crossFilteringBehavior": rel.get("crossFilteringBehavior"),
                })

        return list(tables_dict.values()), relationships

    def _batch_admin_scan(self, workspace_ids: List[str]) -> Dict[str, Dict]:
        """
        Batch admin scan: up to 100 workspaces per request.
        Returns dict keyed by dataset_id -> (tables, relationships) from _parse_admin_scan_tables.
        """
        import time
        import logging

        self.connect()
        headers = self._build_headers()
        # ds_id -> (tables, relationships)
        results: Dict[str, tuple] = {}

        # Batch in chunks of 100 (API limit)
        for i in range(0, len(workspace_ids), 100):
            batch = workspace_ids[i:i + 100]

            try:
                scan_url = f"{self.BASE_URL}/admin/workspaces/getInfo?datasetSchema=true"
                resp = self._http.post(scan_url, json={"workspaces": batch}, headers=headers, timeout=30)
                if resp.status_code >= 300:
                    logging.debug(f"Batch admin scan failed: HTTP {resp.status_code}")
                    continue

                scan_id = (resp.json() or {}).get("id")
                if not scan_id:
                    continue

                # Poll for completion (max 60s for batch)
                status_url = f"{self.BASE_URL}/admin/workspaces/scanStatus/{scan_id}"
                succeeded = False
                for _ in range(30):
                    time.sleep(2)
                    status_resp = self._http.get(status_url, headers=headers, timeout=30)
                    if status_resp.status_code < 300:
                        if (status_resp.json() or {}).get("status") == "Succeeded":
                            succeeded = True
                            break
                if not succeeded:
                    continue

                # Fetch results
                result_url = f"{self.BASE_URL}/admin/workspaces/scanResult/{scan_id}"
                result_resp = self._http.get(result_url, headers=headers, timeout=60)
                if result_resp.status_code >= 300:
                    continue

                for ws in (result_resp.json() or {}).get("workspaces") or []:
                    for ds in ws.get("datasets") or []:
                        ds_id = ds.get("id")
                        if ds_id:
                            results[ds_id] = self._parse_admin_scan_tables(ds)

            except Exception as e:
                logging.debug(f"Batch admin scan error: {e}")
                continue

        return results

    def get_schemas(
        self,
        force_refresh: bool = False,
        prior_tables: Optional[Dict[str, Dict]] = None,
    ) -> List[Table]:
        """
        Build Table objects representing all internal tables across all datasets.
        Each internal Power BI table becomes one BOW Table named "{Dataset}/{Table}".

        The result is cached on the instance: this is a full tenant crawl
        (workspaces, datasets, admin scan, COLUMNSTATISTICS fallbacks), far too
        expensive to repeat per query. Pass force_refresh=True to re-discover.

        `prior_tables` enables INCREMENTAL discovery: a mapping of previously
        indexed schema tables ({schema_table_name: {"columns": [...], "pks":
        [...], "fks": [...], "metadata_json": {...}}}). Datasets already present
        in it (matched by powerbi.datasetId, with non-empty columns) are rebuilt
        from the stored definition instead of being introspected — dataset
        listing is identity-scoped and takes seconds, while per-dataset
        COLUMNSTATISTICS is executeQueries-rate-limited (~120/user/min, i.e.
        minutes-scale on large tenants). Only NEW datasets pay the introspection
        cost; datasets that vanished from the listing are dropped as usual.
        Callers that must detect column-level drift in known models (scheduled/
        background reindexing) should NOT pass prior_tables.

        Strategy:
        1. Fetch datasets and reports for all workspaces in parallel
        2. Try batch admin scan (up to 100 workspaces per call) — gets tables + relationships
        3. For datasets not covered by admin scan, fall back to parallel COLUMNSTATISTICS
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import logging

        if self._schemas_cache is not None and not force_refresh:
            return self._schemas_cache

        # A delegated crawl only happens on OBO sign-in and manual reload — the
        # two moments a user's access may just have changed. Flush Power BI's
        # permission cache first so this crawl (and the queries that follow) see
        # the current grants, not a stale snapshot. No-op for the service
        # principal and fires at most once per client. The query path never
        # reaches here (it resolves dataset IDs from attached metadata), so this
        # never adds a RefreshUserPermissions call to a hot query.
        self.refresh_user_permissions()

        # Fresh crawl → reset per-run diagnostics.
        self.discovery_diagnostics = []

        # datasetId -> [(schema_table_name, prior_entry), ...] for reuse.
        # Entries without columns are ignored so a previously-unreadable
        # dataset still gets a real introspection attempt.
        prior_by_dataset: Dict[str, List[Tuple[str, Dict]]] = {}
        for prior_name, entry in (prior_tables or {}).items():
            try:
                meta = (entry.get("metadata_json") or {}).get("powerbi") or {}
                ds_id = meta.get("datasetId")
                if ds_id and (entry.get("columns") or []):
                    prior_by_dataset.setdefault(str(ds_id), []).append((prior_name, entry))
            except Exception:
                continue

        workspaces = self.list_workspaces()
        tables: List[Table] = []

        # Phase 1: Fetch datasets and reports for all workspaces in parallel
        ws_datasets: Dict[str, List[Dict]] = {}  # ws_id -> datasets
        ws_reports: Dict[str, List[Dict]] = {}    # ws_id -> reports

        with ThreadPoolExecutor(max_workers=10) as pool:
            ds_futures = {pool.submit(self.list_datasets, ws["id"]): ws for ws in workspaces}
            rpt_futures = {pool.submit(self.list_reports, ws["id"]): ws for ws in workspaces}

            for fut in as_completed(ds_futures):
                ws = ds_futures[fut]
                try:
                    ws_datasets[ws["id"]] = fut.result()
                except Exception:
                    ws_datasets[ws["id"]] = []

            for fut in as_completed(rpt_futures):
                ws = rpt_futures[fut]
                try:
                    ws_reports[ws["id"]] = fut.result()
                except Exception:
                    ws_reports[ws["id"]] = []

        # Collect all (workspace, dataset) pairs. Every semantic model the
        # identity can list is discovered — including Fabric default semantic
        # models and Microsoft's built-in usage-metrics models. We do NOT hide
        # any of them: hiding is a product decision, and in tenants where the
        # usage-metrics models are the only ones currently visible, dropping
        # them would make the catalog look emptier, not cleaner.
        all_ds_tasks: List[Tuple[Dict, Dict, str]] = []
        for ws in workspaces:
            ws_id = ws.get("id")
            for ds in ws_datasets.get(ws_id, []):
                all_ds_tasks.append((ws, ds, ws_id))

        # Item-shared models the workspace crawl cannot reach. `GET /groups`
        # returns only workspaces the identity holds a ROLE in, and
        # `GET /myorg/datasets` is My-workspace-only — so a model granted to
        # this user via item-level sharing (the standard RLS setup, where end
        # users are kept out of the workspace so RLS actually applies) appears
        # in NEITHER listing, and the user's catalog silently loses it.
        #
        # There is no delegated "list models shared with me" API, so the known
        # catalog is the candidate list: probe each prior dataset the listing
        # missed and keep the ones this identity can actually query.
        all_ds_tasks.extend(self._probe_unlisted_prior_datasets(prior_by_dataset, all_ds_tasks))

        # Datasets already known from prior_tables skip introspection entirely —
        # they are rebuilt from the stored definitions in Phase 4.
        known_dataset_ids = set(prior_by_dataset)
        introspect_tasks = [
            t for t in all_ds_tasks if str(t[1].get("id")) not in known_dataset_ids
        ]
        if prior_by_dataset:
            logging.info(
                "PowerBI incremental discovery: %d dataset(s) reused from prior catalog, "
                "%d introspected live",
                len(all_ds_tasks) - len(introspect_tasks), len(introspect_tasks),
            )

        # Phase 2: Try batch admin scan (tables + relationships in bulk), only
        # for workspaces that still have datasets needing introspection.
        ws_ids = sorted({ws_id for _, _, ws_id in introspect_tasks})
        admin_scan_results: Dict[str, tuple] = {}  # ds_id -> (tables, relationships)
        try:
            if ws_ids:
                admin_scan_results = self._batch_admin_scan(ws_ids)
        except Exception as e:
            logging.debug(f"Batch admin scan unavailable, falling back to COLUMNSTATISTICS: {e}")

        # Phase 3: For datasets the admin scan did not cover WITH TABLES, use
        # parallel COLUMNSTATISTICS. A dataset can appear in the admin-scan
        # results with an EMPTY table list (model not refreshed since enhanced
        # metadata scanning was enabled, all-hidden tables, some DirectLake
        # models). Treating "present in scan" as final would shadow the DAX
        # fallback that often *can* read it — so fall through on an empty scan
        # result, not just a missing one.
        ds_table_results: Dict[str, tuple] = {}  # "ws_id:ds_id" -> (tables, relationships)
        ds_reasons: Dict[str, str] = {}          # "ws_id:ds_id" -> why no tables
        fallback_tasks = []

        # Datasets the scan described but gave no relationships for. The scan is
        # all-or-nothing per tenant setting, so this is common; ask the model
        # directly rather than indexing a join-less schema.
        rel_only_tasks: List[Tuple[str, str, str]] = []  # (ws_id, ds_id, key)

        for ws, ds, ws_id in introspect_tasks:
            ds_id = ds.get("id")
            key = f"{ws_id}:{ds_id}"
            scan_tables, scan_rels = admin_scan_results.get(ds_id, ([], []))
            if scan_tables:
                ds_table_results[key] = (scan_tables, scan_rels)
                if not scan_rels:
                    rel_only_tasks.append((ws_id, ds_id, key))
            else:
                fallback_tasks.append((ws, ds, ws_id, key))

        if fallback_tasks:
            with ThreadPoolExecutor(max_workers=10) as pool:
                tbl_futures = {}
                for ws, ds, ws_id, key in fallback_tasks:
                    ds_id = ds.get("id")
                    tbl_futures[pool.submit(self.get_dataset_tables_with_reason, ws_id, ds_id)] = key

                for fut in as_completed(tbl_futures):
                    key = tbl_futures[fut]
                    try:
                        tbls, rels, reason = fut.result()
                        ds_table_results[key] = (tbls, rels)
                        if not tbls and reason:
                            ds_reasons[key] = reason
                    except Exception as e:
                        ds_table_results[key] = ([], [])
                        ds_reasons[key] = f"introspection error: {self._short_error(e)}"

        if rel_only_tasks:
            # Serial, and stops early: the FIRST dataset settles whether this
            # endpoint accepts INFO functions at all, and if it doesn't there is
            # nothing to gain from asking the rest (see
            # `_get_relationships_via_dax`). Costs one request per dataset when
            # supported, one request total when not.
            for ws_id, ds_id, key in rel_only_tasks:
                if self._info_functions_supported is False:
                    break
                rels = self._get_relationships_via_dax(ws_id, ds_id)
                if rels:
                    tbls, _ = ds_table_results[key]
                    self._add_relationship_key_columns(tbls, rels)
                    ds_table_results[key] = (tbls, rels)

        # Phase 4: Assemble Table objects (CPU-only, no I/O)
        for ws, ds, ws_id in all_ds_tasks:
            ws_name = ws.get("name") or ws_id
            ds_id = ds.get("id")
            ds_name = ds.get("name") or ds_id
            key = f"{ws_id}:{ds_id}"

            # Build reports map for this workspace
            reports_by_dataset: Dict[str, List[Dict]] = {}
            for rpt in ws_reports.get(ws_id, []):
                rpt_ds_id = rpt.get("datasetId")
                if rpt_ds_id:
                    if rpt_ds_id not in reports_by_dataset:
                        reports_by_dataset[rpt_ds_id] = []
                    reports_by_dataset[rpt_ds_id].append({
                        "id": rpt.get("id"),
                        "name": rpt.get("name"),
                        "webUrl": rpt.get("webUrl"),
                    })

            # Incremental reuse: this dataset was not introspected — rebuild its
            # tables from the prior catalog, refreshed with the listing's
            # current dataset/workspace names and reports.
            prior_entries = prior_by_dataset.get(str(ds_id))
            if prior_entries is not None:
                tables.extend(self._tables_from_prior(
                    prior_entries, ds, ws_id, ws_name,
                    reports_by_dataset.get(ds_id, []),
                ))
                continue

            ds_tables, ds_relationships = ds_table_results.get(key, ([], []))

            # Found-but-unreadable: the dataset was listed but no introspection
            # path produced tables. Record it as a diagnostic (surfaced on the
            # indexing job) instead of silently dropping the semantic model.
            # We deliberately do NOT emit a phantom table — a column-less table
            # is not queryable and would just move the failure downstream.
            if not ds_tables:
                self.discovery_diagnostics.append({
                    "datasetId": ds_id,
                    "datasetName": ds_name,
                    "workspaceId": ws_id,
                    "workspaceName": ws_name,
                    "reason": ds_reasons.get(key, "no tables discovered"),
                })
                continue

            # Create one BOW Table per internal Power BI table
            for tbl in ds_tables:
                tbl_name = tbl.get("name") or ""
                if not tbl_name:
                    continue

                # Clean up display name for SharePoint URL tables
                tbl_display_name = _clean_table_display_name(tbl_name)

                # 2-level naming: Dataset/Table (like Snowflake's schema.table)
                full_name = f"{ds_name}/{tbl_display_name}"

                # Columns for this table only
                columns: List[TableColumn] = []
                for col in tbl.get("columns") or []:
                    col_name = col.get("name") or ""
                    col_type = col.get("dataType") or "unknown"
                    if col_name:
                        col_meta = {"role": "column"}
                        # Queryable, but not meant for display — let the agent
                        # join on it without offering it as a report field.
                        if col.get("isHidden"):
                            col_meta["hidden"] = True
                        if col.get("isRelationshipKey"):
                            col_meta["relationship_key"] = True
                        columns.append(TableColumn(
                            name=col_name,
                            dtype=col_type,
                            description=None,
                            metadata=col_meta,
                        ))

                # Measures for this table. A measure is the model's own business
                # logic — the agent should invoke it by name rather than
                # re-deriving it from raw columns, which will not reproduce the
                # measure's filter context.
                for measure in tbl.get("measures") or []:
                    measure_name = measure.get("name") or ""
                    expression = measure.get("expression") or ""
                    if measure_name:
                        meas_meta = {"role": "measure", "expression": expression}
                        if measure.get("dataType"):
                            meas_meta["returns"] = measure["dataType"]
                        if measure.get("isHidden"):
                            meas_meta["hidden"] = True
                        columns.append(TableColumn(
                            name=measure_name,
                            dtype="measure",
                            description=expression[:200] if expression else None,
                            metadata=meas_meta,
                        ))

                # Build FKs for relationships FROM this table
                fks: List[ForeignKey] = []
                for rel in ds_relationships:
                    if rel.get("fromTable") == tbl_name:
                        to_table = rel.get("toTable") or ""
                        to_table_display = _clean_table_display_name(to_table)
                        fks.append(ForeignKey(
                            column=TableColumn(
                                name=rel.get("fromColumn") or "",
                                dtype="unknown",
                            ),
                            references_name=f"{ds_name}/{to_table_display}",
                            references_column=TableColumn(
                                name=rel.get("toColumn") or "",
                                dtype="unknown",
                            ),
                        ))

                # Metadata for query execution (workspace at connection level)
                metadata_json = {
                    "powerbi": {
                        "datasetId": ds_id,
                        "workspaceId": ws_id,
                        "workspaceName": ws_name,
                        "datasetName": ds_name,
                        "tableName": tbl_name,
                        "configuredBy": ds.get("configuredBy"),
                        "webUrl": ds.get("webUrl"),
                        "reports": reports_by_dataset.get(ds_id, []),
                    }
                }
                if ds.get("isEffectiveIdentityRequired"):
                    metadata_json["powerbi"]["rowLevelSecurity"] = True

                tables.append(Table(
                    name=full_name,
                    description=None,
                    columns=columns,
                    pks=[],
                    fks=fks if fks else [],
                    is_active=True,
                    metadata_json=metadata_json,
                ))

        # Relationship coverage is the difference between "the agent can join
        # these models" and "the agent tells users they can't", and it is
        # invisible in the table count — so state it explicitly.
        total_fks = sum(len(t.fks or []) for t in tables)
        if not total_fks and tables:
            logging.warning(
                "PowerBI discovery: %d table(s) indexed with NO relationships "
                "(admin scan covered %d dataset(s), INFO functions supported: %s). "
                "The agent will not know these tables can be joined.",
                len(tables), len(admin_scan_results), self._info_functions_supported,
            )
        else:
            logging.info(
                "PowerBI discovery: %d table(s), %d relationship(s)", len(tables), total_fks
            )

        self._schemas_cache = tables
        return tables

    # Cap on how many unlisted datasets we probe per discovery run.
    # executeQueries is rate-limited to ~120 requests/min/user and that budget is
    # shared with the user's real queries, so this stays bounded and is reported
    # (not silently truncated) when it bites.
    MAX_UNLISTED_PROBES = 60

    def _probe_unlisted_prior_datasets(
        self,
        prior_by_dataset: Dict[str, List[Tuple[str, Dict]]],
        listed_tasks: List[Tuple[Dict, Dict, str]],
    ) -> List[Tuple[Dict, Dict, str]]:
        """Probe known datasets the workspace listing didn't return.

        Returns synthetic (workspace, dataset, workspace_id) tuples for the ones
        this identity can query, shaped exactly like listed tasks so Phase 4
        rebuilds them from the prior catalog like any other known dataset.
        """
        import logging
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not prior_by_dataset:
            return []
        listed_ids = {str(ds.get("id")) for _, ds, _ in listed_tasks}
        unlisted = [d for d in prior_by_dataset if str(d) not in listed_ids]
        if not unlisted:
            return []

        probed = unlisted[: self.MAX_UNLISTED_PROBES]
        if len(unlisted) > len(probed):
            logging.warning(
                "PowerBI discovery: %d unlisted dataset(s) known to the catalog, probing "
                "only the first %d (executeQueries rate limit); the rest are omitted "
                "from this identity's catalog.",
                len(unlisted), len(probed),
            )

        def _probe(ds_id: str):
            meta = (prior_by_dataset[ds_id][0][1].get("metadata_json") or {}).get("powerbi") or {}
            return ds_id, meta, self._can_query_dataset(ds_id)

        out: List[Tuple[Dict, Dict, str]] = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(_probe, d) for d in probed]
            for fut in as_completed(futures):
                try:
                    ds_id, meta, ok = fut.result()
                except Exception:
                    continue
                if not ok:
                    continue
                ws_id = meta.get("workspaceId")
                out.append((
                    {"id": ws_id, "name": meta.get("workspaceName") or ws_id},
                    {"id": ds_id, "name": meta.get("datasetName") or ds_id,
                     "configuredBy": meta.get("configuredBy"), "webUrl": meta.get("webUrl")},
                    ws_id,
                ))
        if out:
            logging.info(
                "PowerBI discovery: %d/%d unlisted dataset(s) reachable item-level for this identity",
                len(out), len(probed),
            )
        return out

    def _can_query_dataset(self, dataset_id: str) -> bool:
        """Can this identity execute DAX against the dataset right now?

        Deliberately tenant-level: the workspace-scoped endpoint needs a
        workspace role, which is exactly what these datasets lack. 200 means
        yes; 401 (no permission / RLS with no role) and 404 (invisible to this
        identity) both mean no.
        """
        try:
            resp = self._request(
                "POST", f"{self.BASE_URL}/datasets/{dataset_id}/executeQueries",
                json_body={"queries": [{"query": 'EVALUATE ROW("t",1)'}],
                           "serializerSettings": {"includeNulls": True}},
                timeout=30,
            )
            return resp.status_code < 300
        except Exception:
            return False

    def _tables_from_prior(
        self,
        prior_entries: List[Tuple[str, Dict]],
        ds: Dict,
        ws_id: str,
        ws_name: str,
        reports: List[Dict],
    ) -> List[Table]:
        """Rebuild a known dataset's Table objects from stored definitions
        (incremental discovery). Columns/pks/fks come from the prior catalog;
        dataset/workspace names, webUrl, and reports are refreshed from the
        live listing so renames propagate without introspection."""
        ds_id = ds.get("id")
        ds_name = ds.get("name") or ds_id
        out: List[Table] = []
        for prior_name, entry in prior_entries:
            prior_pbi = ((entry.get("metadata_json") or {}).get("powerbi")) or {}
            tbl_name = prior_pbi.get("tableName") or prior_name.split("/", 1)[-1]
            full_name = f"{ds_name}/{_clean_table_display_name(tbl_name)}"

            def _cols(items):
                """Rebuild columns from the persisted definition.

                `description` and `metadata` are carried through, not just
                name/dtype. The metadata is what marks a column as a measure
                (with its return type) or as a hidden join key, and the schema
                renderer reads exactly those keys — so reducing a column to
                {name, dtype} here silently downgrades every model on an
                incremental reload: measures reach the agent as untyped columns
                it can no longer tell to invoke by name, and hidden keys look
                like ordinary report fields. `normalize_indexed_columns` stores
                both for precisely this reason; dropping them on the way back
                out undoes that.
                """
                cols = []
                for c in items or []:
                    is_dict = isinstance(c, dict)
                    name = c.get("name") if is_dict else getattr(c, "name", None)
                    if not name:
                        continue
                    dtype = c.get("dtype") if is_dict else getattr(c, "dtype", None)
                    description = (c.get("description") if is_dict
                                   else getattr(c, "description", None))
                    meta = c.get("metadata") if is_dict else getattr(c, "metadata", None)
                    # Only a real dict: on a SQLAlchemy ORM instance `.metadata`
                    # is the declarative MetaData registry, which would fail
                    # TableColumn validation and abort the whole rebuild.
                    if not isinstance(meta, dict) or not meta:
                        meta = None
                    cols.append(TableColumn(
                        name=name,
                        dtype=dtype or "unknown",
                        description=description or None,
                        metadata=meta,
                    ))
                return cols

            fks: List[ForeignKey] = []
            for fk in entry.get("fks") or []:
                try:
                    fks.append(fk if isinstance(fk, ForeignKey) else ForeignKey(**fk))
                except Exception:
                    continue

            out.append(Table(
                name=full_name,
                description=None,
                columns=_cols(entry.get("columns")),
                pks=_cols(entry.get("pks")),
                fks=fks,
                is_active=True,
                metadata_json={
                    "powerbi": {
                        "datasetId": ds_id,
                        "workspaceId": ws_id,
                        "workspaceName": ws_name,
                        "datasetName": ds_name,
                        "tableName": tbl_name,
                        "configuredBy": ds.get("configuredBy"),
                        "webUrl": ds.get("webUrl"),
                        "reports": reports,
                    }
                },
            ))
        return out

    def index_stats(self) -> dict:
        """Fold discovery diagnostics into the indexing row so the job can
        report semantic models that were found-but-unreadable (no Build
        permission, RLS, DirectLake, ...) instead of them vanishing silently.

        Empty when every listed dataset was introspected successfully.
        """
        diags = self.discovery_diagnostics or []
        if not diags:
            return {}
        return {
            "unreadable_datasets": diags,
            "unreadable_dataset_count": len(diags),
        }

    def get_schema(self, table_name: str) -> Table:
        """
        Get schema for a single table by name.

        Accepts:
          - "Dataset/Table" name path (exact match)
          - Internal table name only (first match)
          - Dataset ID (returns first table in that dataset)
        """
        all_tables = self.get_schemas()

        # Try exact name match (Dataset/Table)
        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl

        # Try by internal table name only (first match)
        for tbl in all_tables:
            metadata = tbl.metadata_json or {}
            pbi = metadata.get("powerbi") or {}
            if pbi.get("tableName") == table_name:
                return tbl

        # Try by dataset ID (returns first table in that dataset)
        for tbl in all_tables:
            metadata = tbl.metadata_json or {}
            pbi = metadata.get("powerbi") or {}
            if pbi.get("datasetId") == table_name:
                return tbl

        raise RuntimeError(f"Table not found for '{table_name}'")

    def execute_query(
        self,
        query: str,
        table_name: Optional[str] = None,
        dataset_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Execute a DAX query against a dataset and return results as DataFrame.

        Args:
            query: DAX query string (must start with EVALUATE)
            table_name: Table name (e.g., "SalesModel/Customers") - will look up dataset_id/workspace_id
            dataset_id: Power BI dataset ID (alternative to table_name)
            workspace_id: Power BI workspace ID
            max_rows: Maximum rows to return

        Example:
            df = client.execute_query("EVALUATE Customers", "SalesModel/Customers")
            # or with explicit IDs:
            df = client.execute_query("EVALUATE Customers", dataset_id="abc", workspace_id="xyz")
        """
        if not query:
            raise ValueError("DAX query is required")

        # If table_name provided (but not dataset_id), resolve the IDs:
        # 1. From the attached persisted metadata map — zero API calls.
        # 2. Fallback: live discovery (cached on the instance after first use).
        lookup_error: Optional[str] = None
        if table_name and not dataset_id:
            meta = self._resolve_ids_from_metadata(table_name)
            if meta:
                dataset_id = meta.get("datasetId")
                workspace_id = workspace_id or meta.get("workspaceId")
            else:
                try:
                    table = self.get_schema(table_name)
                    pbi = (table.metadata_json or {}).get("powerbi") or {}
                    dataset_id = pbi.get("datasetId")
                    workspace_id = workspace_id or pbi.get("workspaceId")
                except Exception as e:
                    lookup_error = str(e)

        if not dataset_id:
            known = sorted(self._table_metadata_map.keys())
            hint = (
                f" Known schema tables include: {', '.join(known[:10])}"
                f"{', ...' if len(known) > 10 else ''}."
                if known else ""
            )
            if table_name:
                detail = f" Lookup failed: {lookup_error}" if lookup_error else ""
                raise ValueError(
                    f"Could not resolve Power BI dataset for table '{table_name}'.{detail} "
                    "Pass the schema table name EXACTLY as shown in the schema context "
                    "(format 'Dataset/Table'), or pass explicit dataset_id=/workspace_id= "
                    f"from the table's powerbi metadata.{hint}"
                )
            raise ValueError(
                "execute_query needs a target dataset: pass the schema table name as the "
                "second argument (format 'Dataset/Table', exactly as shown in the schema "
                "context), or explicit dataset_id=/workspace_id= from the table's powerbi "
                f"metadata. Do not ask the user for these IDs.{hint}"
            )

        return self._execute_dax_internal(workspace_id, dataset_id, query, max_rows=max_rows)

    def _dataset_query_url(self, workspace_id: Optional[str], dataset_id: str) -> str:
        """The executeQueries URL to use for this dataset.

        Workspace-scoped by default, but the tenant-level form once we've learned
        the caller has no role in that workspace (see `_execute_dax_internal`).
        """
        if workspace_id and workspace_id not in self._tenant_scoped_workspaces:
            return f"{self.BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
        return f"{self.BASE_URL}/datasets/{dataset_id}/executeQueries"

    def _execute_dax_internal(
        self,
        workspace_id: Optional[str],
        dataset_id: str,
        dax: str,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Internal DAX execution.

        The workspace-scoped endpoint requires a workspace ROLE, not just access
        to the model: a user granted Build on a semantic model via item-level
        sharing gets 401 there, while the tenant-level
        `/datasets/{id}/executeQueries` answers 200 for the same identity and
        query. That topology is the norm under row-level security, where end
        users are deliberately kept out of the workspace (Contributor and above
        BYPASS RLS), so falling back is the difference between "works" and
        "every query 401s". The fallback result is remembered per workspace so
        each workspace costs at most one wasted request per client instance.
        """
        self.connect()
        url = self._dataset_query_url(workspace_id, dataset_id)

        body = {
            "queries": [{"query": dax}],
            "serializerSettings": {"includeNulls": True},
        }

        resp = self._request("POST", url, json_body=body, timeout=120)
        if resp.status_code in (401, 403) and workspace_id and workspace_id not in self._tenant_scoped_workspaces:
            fallback = f"{self.BASE_URL}/datasets/{dataset_id}/executeQueries"
            retry = self._request("POST", fallback, json_body=body, timeout=120)
            if retry.status_code < 300:
                self._tenant_scoped_workspaces.add(workspace_id)
                resp = retry
        if resp.status_code >= 300:
            raise RuntimeError(f"DAX query failed: HTTP {resp.status_code} {resp.text}")

        payload = resp.json() or {}
        results = payload.get("results") or []

        if not results:
            return pd.DataFrame()

        first_result = results[0]
        tables = first_result.get("tables") or []

        if not tables:
            return pd.DataFrame()

        rows = tables[0].get("rows") or []

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df.columns = self._clean_dax_columns(list(df.columns))

        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)

        return df

    def prompt_schema(self) -> str:
        """Format schemas for LLM prompt."""
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str

    @property
    def description(self) -> str:
        text = "Power BI Client: discover semantic models and execute DAX queries."
        text += self.system_prompt()
        return text

    def system_prompt(self) -> str:
        return """
## Power BI DAX Query Guide

Execute DAX queries against Power BI semantic models.

### Schema Structure

Each Power BI table is exposed as a separate schema table named `Dataset/Table`:
- `SalesModel/Customers` - Customers table in SalesModel dataset
- `SalesModel/Orders` - Orders table in SalesModel dataset

Tables in the same dataset share the same `metadata.powerbi.datasetId` and can be joined via relationships (see `fks` field).

### Table Name vs DAX Table Name

- **Schema table name** (e.g., `SalesModel/Customers`) - Pass as second argument to `execute_query()`
- **DAX table name** - The part after `/` (e.g., `Customers`) - Use inside DAX queries

The DAX table name is also available in `metadata.powerbi.tableName`.

### How to Execute Queries

**Signature**: `execute_query(dax_query, table_name)` - BOTH arguments are REQUIRED!

```python
# Schema table name as 2nd arg, DAX table name in query
df = db_clients['powerbi'].execute_query(
    "EVALUATE Customers",           # DAX uses the table name (after /)
    "SalesModel/Customers"          # Schema table name (REQUIRED)
)

# Or with explicit IDs from the table's <powerbi datasetId=... workspaceId=.../> metadata:
df = db_clients['powerbi'].execute_query(
    "EVALUATE Customers",
    dataset_id="<datasetId>",
    workspace_id="<workspaceId>",
)
```

Every table's `datasetId`/`workspaceId` are shown in the schema context — NEVER ask the user for them.

### DAX Query Pattern

```dax
EVALUATE <table_expression>
```

### Examples

```dax
-- Get all rows (quote table name if it has spaces)
EVALUATE Customers
EVALUATE 'Order Details'

-- Aggregate with grouping
EVALUATE
SUMMARIZECOLUMNS(
    Orders[Category],
    "Total", SUM(Orders[Amount])
)

-- Filter data
EVALUATE
FILTER(
    Customers,
    Customers[Status] = "Active"
)

-- Top N results
EVALUATE
TOPN(10,
    SUMMARIZECOLUMNS(Customers[Name], "Total", SUM(Orders[Value])),
    [Total], DESC
)
```

### Key DAX Syntax Rules
- Table names with spaces MUST use single quotes: 'Order Details'[Column]
- Column references: TableName[ColumnName] or 'Table Name'[ColumnName]
- Measure references: [MeasureName] (no table prefix)
- String literals use double quotes: "value"
- Relationships between tables are in `fks` - use RELATED() to traverse them
- An EMPTY `fks` list means the relationships could not be READ during indexing
  (that needs tenant-admin scope we may not have), NOT that the model has none.
  Never tell the user that tables cannot be joined, or that a table has no key
  to another, on the basis of missing `fks` - you cannot see that from here.
  The model's relationships are enforced by the DAX engine at query time
  regardless of what we indexed, so cross-table aggregation just works:
  `EVALUATE SUMMARIZECOLUMNS(Dim[Attr], "Total", SUM(Fact[Value]))` resolves the
  join itself. Try the query; a wrong-grain result is the signal there is no
  usable relationship, and a `[hidden]` column is still fully queryable.
- Measures are the model's OWN business logic. When one exists for what is being
  asked (e.g. a total, a rate, an average), invoke it by name - `[Measure Name]`
  - instead of re-deriving it from raw columns with SUM/DIVIDE. A hand-rolled
  equivalent will not reproduce the measure's filter context and will disagree
  with the customer's own reports. `[measure -> Number]` shows what it returns;
  the definition is not always readable, and you do not need it to call it.
- Row-level security may be filtering your results and you CANNOT tell. A
  row-filtered query returns HTTP 200 with fewer rows - indistinguishable from a
  genuinely small result - and whether a model is row-secured is not readable
  through the API with a normal user's token. A `rowLevelSecurity` marker in a
  table's Power BI metadata confirms RLS when present, but its ABSENCE proves
  nothing. So never describe a Power BI total as organization-wide, company-wide
  or complete: report it as the data visible to the current user. If the
  distinction matters for the answer, say so explicitly.
- Bare INFO.TABLES() / INFO.COLUMNS() / INFO.RELATIONSHIPS() do NOT work via the
  REST API (HTTP 400). The INFO.VIEW.* family DOES work - INFO.VIEW.TABLES(),
  INFO.VIEW.COLUMNS(), INFO.VIEW.MEASURES(), INFO.VIEW.RELATIONSHIPS() - so use
  those to inspect the model when the indexed schema looks incomplete.
- NEVER reference columns named `RowNumber-<GUID>` even if they appear in the
  schema - they are internal engine columns and any query using them fails
- In expression slots of SUMMARIZECOLUMNS / ADDCOLUMNS / ROW, a bare column
  reference fails with "A single value for column ... cannot be determined".
  Wrap it in an aggregation (MIN/MAX/COUNTROWS/...) or, for distinct values,
  group by the column instead: `EVALUATE SUMMARIZE(Users, Users[UserId])` or
  `EVALUATE DISTINCT(Users[UserId])`
"""

    # ----------------------------
    # Internal helpers
    # ----------------------------

    @staticmethod
    def _clean_dax_columns(columns: List[str]) -> List[str]:
        """
        Unwrap executeQueries column names to bare column names.

        The REST API returns '[Measure]' for measures/aliases and
        'Table[Column]' for table columns. str.strip("[]") only handles the
        first form — 'Sales[Region]' became 'Sales[Region'. Unwrap both forms,
        but keep the qualified 'Table[Column]' name whenever unwrapping would
        collide with another column in the same result set (e.g. both
        Customers[Name] and Products[Name] selected).
        """
        unwrapped = []
        for col in columns:
            m = re.match(r"^(?:[^\[\]]*\[)?([^\[\]]+)\]$", col or "")
            unwrapped.append(m.group(1) if m else col)

        counts = {}
        for name in unwrapped:
            counts[name] = counts.get(name, 0) + 1

        cleaned = []
        for original, bare in zip(columns, unwrapped):
            if counts[bare] > 1 and original != f"[{bare}]":
                # Ambiguous bare name: keep the table-qualified form, just
                # drop the trailing bracket noise ('Table[Column]' -> 'Table.Column').
                cleaned.append(original.rstrip("]").replace("[", "."))
            else:
                cleaned.append(bare)
        return cleaned

    def _build_headers(self) -> Dict[str, str]:
        if not self._access_token:
            raise RuntimeError("Not authenticated")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, json_body: Optional[Dict] = None,
                 timeout: int = 30, max_attempts: int = 3):
        """
        HTTP request with retry/backoff on 429 (throttling) and 5xx.
        Respects Retry-After when Power BI provides it. Returns the final
        response (does not raise on HTTP error status).
        """
        import time

        resp = None
        for attempt in range(1, max_attempts + 1):
            resp = self._http.request(
                method, url, json=json_body, headers=self._build_headers(), timeout=timeout
            )
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            if attempt >= max_attempts:
                return resp
            try:
                delay = float(resp.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                delay = 0
            if delay <= 0:
                delay = 2 ** attempt  # 2s, 4s
            time.sleep(min(delay, 30))
        return resp


# Compatibility alias for dynamic resolver expecting 'PowerbiClient'
PowerbiClient = PowerBIClient
