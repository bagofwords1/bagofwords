from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, ServiceFormatter
from typing import List, Dict, Optional, Tuple
import requests
import pandas as pd
import re
from urllib.parse import unquote


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

        # Persisted schema metadata injected via attach_table_metadata():
        # schema table name ("Dataset/Table") -> the table's `powerbi` metadata
        # dict ({datasetId, workspaceId, ...}). Lets execute_query resolve the
        # dataset GUID as a dict lookup instead of re-crawling the tenant.
        self._table_metadata_map: Dict[str, Dict] = {}
        # Live-discovery cache: get_schemas() is a full tenant crawl (workspaces,
        # datasets, admin scan, COLUMNSTATISTICS) — run it at most once per
        # client instance.
        self._schemas_cache: Optional[List[Table]] = None

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
            if self._workspace_filter:
                return {
                    "success": False,
                    "message": (
                        f"Connected, but none of the configured workspaces ({self.workspaces}) were found. "
                        "Check the names/IDs and ensure the service principal is a Member/Contributor of those workspaces."
                    ),
                }
            return {
                "success": False,
                "message": "Connected but no workspaces found. Ensure the service principal has access to at least one workspace.",
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
            return {
                "success": False,
                "message": (
                    f"Connected but not authorized to query {permission_error}. "
                    "Ensure the service principal is a Member or Contributor of the workspace "
                    "(Viewer is not enough), and that 'Allow service principals to use Power BI APIs' "
                    "is enabled in the tenant settings."
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
        self.connect()
        headers = self._build_headers()

        # Try COLUMNSTATISTICS first (works for most datasets without admin perms)
        tables, _ = self._get_tables_via_column_stats(workspace_id, dataset_id)
        if tables:
            return tables, []

        # Fallback: REST API /tables (only works for Push datasets)
        url = f"{self.BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/tables"
        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code < 300:
            rest_tables = (resp.json() or {}).get("value") or []
            if rest_tables and any(t.get("columns") for t in rest_tables):
                return rest_tables, []

        return [], []

    def _get_tables_via_column_stats(self, workspace_id: str, dataset_id: str) -> tuple:
        """
        Get table/column metadata using DAX COLUMNSTATISTICS() function.
        Works for most imported and DirectQuery datasets.

        Returns:
            tuple: (tables_list, relationships_list) - relationships always empty for this method
        """
        import logging

        try:
            # COLUMNSTATISTICS() returns: Table Name, Column Name, Min, Max, Cardinality, Max Length
            stats_dax = "EVALUATE COLUMNSTATISTICS()"
            stats_df = self._execute_dax_internal(workspace_id, dataset_id, stats_dax)
            if stats_df.empty:
                return [], []

            # Build tables structure from column stats
            tables_dict: Dict[str, Dict] = {}

            for _, row in stats_df.iterrows():
                table_name = str(row.get("Table Name", ""))
                col_name = str(row.get("Column Name", ""))

                if not table_name or not col_name:
                    continue

                # Skip internal/system tables
                if table_name.startswith("DateTableTemplate") or table_name.startswith("LocalDateTable"):
                    continue

                if table_name not in tables_dict:
                    tables_dict[table_name] = {"name": table_name, "columns": [], "measures": []}

                tables_dict[table_name]["columns"].append({
                    "name": col_name,
                    "dataType": "unknown",  # COLUMNSTATISTICS doesn't return data type
                })

            # No relationships available via COLUMNSTATISTICS
            return list(tables_dict.values()), []

        except Exception as e:
            logging.warning(f"COLUMNSTATISTICS failed for dataset {dataset_id}: {e}")
            return [], []

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

            # Add columns
            for col in tbl.get("columns") or []:
                col_name = col.get("name") or ""
                if col_name and not col.get("isHidden"):
                    tables_dict[tbl_name]["columns"].append({
                        "name": col_name,
                        "dataType": col.get("dataType") or "unknown",
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

    def get_schemas(self, force_refresh: bool = False) -> List[Table]:
        """
        Build Table objects representing all internal tables across all datasets.
        Each internal Power BI table becomes one BOW Table named "{Dataset}/{Table}".

        The result is cached on the instance: this is a full tenant crawl
        (workspaces, datasets, admin scan, COLUMNSTATISTICS fallbacks), far too
        expensive to repeat per query. Pass force_refresh=True to re-discover.

        Strategy:
        1. Fetch datasets and reports for all workspaces in parallel
        2. Try batch admin scan (up to 100 workspaces per call) — gets tables + relationships
        3. For datasets not covered by admin scan, fall back to parallel COLUMNSTATISTICS
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import logging

        if self._schemas_cache is not None and not force_refresh:
            return self._schemas_cache

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

        # Collect all (workspace, dataset) pairs
        all_ds_tasks: List[Tuple[Dict, Dict, str]] = []
        for ws in workspaces:
            ws_id = ws.get("id")
            for ds in ws_datasets.get(ws_id, []):
                all_ds_tasks.append((ws, ds, ws_id))

        # Phase 2: Try batch admin scan for all workspaces (tables + relationships in bulk)
        ws_ids = [ws["id"] for ws in workspaces]
        admin_scan_results: Dict[str, tuple] = {}  # ds_id -> (tables, relationships)
        try:
            admin_scan_results = self._batch_admin_scan(ws_ids)
        except Exception as e:
            logging.debug(f"Batch admin scan unavailable, falling back to COLUMNSTATISTICS: {e}")

        # Phase 3: For datasets not covered by admin scan, use parallel COLUMNSTATISTICS
        ds_table_results: Dict[str, tuple] = {}  # "ws_id:ds_id" -> (tables, relationships)
        fallback_tasks = []

        for ws, ds, ws_id in all_ds_tasks:
            ds_id = ds.get("id")
            key = f"{ws_id}:{ds_id}"
            if ds_id in admin_scan_results:
                ds_table_results[key] = admin_scan_results[ds_id]
            else:
                fallback_tasks.append((ws, ds, ws_id, key))

        if fallback_tasks:
            with ThreadPoolExecutor(max_workers=10) as pool:
                tbl_futures = {}
                for ws, ds, ws_id, key in fallback_tasks:
                    ds_id = ds.get("id")
                    tbl_futures[pool.submit(self.get_dataset_tables, ws_id, ds_id)] = key

                for fut in as_completed(tbl_futures):
                    key = tbl_futures[fut]
                    try:
                        ds_table_results[key] = fut.result()
                    except Exception:
                        ds_table_results[key] = ([], [])

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

            ds_tables, ds_relationships = ds_table_results.get(key, ([], []))

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
                        columns.append(TableColumn(
                            name=col_name,
                            dtype=col_type,
                            description=None,
                            metadata={"role": "column"},
                        ))

                # Measures for this table
                for measure in tbl.get("measures") or []:
                    measure_name = measure.get("name") or ""
                    expression = measure.get("expression") or ""
                    if measure_name:
                        columns.append(TableColumn(
                            name=measure_name,
                            dtype="measure",
                            description=expression[:200] if expression else None,
                            metadata={
                                "role": "measure",
                                "expression": expression,
                            },
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

                tables.append(Table(
                    name=full_name,
                    description=None,
                    columns=columns,
                    pks=[],
                    fks=fks if fks else [],
                    is_active=True,
                    metadata_json=metadata_json,
                ))

        self._schemas_cache = tables
        return tables

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

    def _execute_dax_internal(
        self,
        workspace_id: Optional[str],
        dataset_id: str,
        dax: str,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Internal DAX execution."""
        self.connect()
        # Use workspace-scoped endpoint if workspace_id provided
        if workspace_id:
            url = f"{self.BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
        else:
            url = f"{self.BASE_URL}/datasets/{dataset_id}/executeQueries"

        body = {
            "queries": [{"query": dax}],
            "serializerSettings": {"includeNulls": True},
        }

        resp = self._request("POST", url, json_body=body, timeout=120)
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
- INFO.TABLES() and INFO.COLUMNS() do NOT work via REST API - use the schema metadata instead
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
