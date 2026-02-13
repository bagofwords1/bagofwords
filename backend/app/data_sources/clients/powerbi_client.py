from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional
import requests
import pandas as pd


class PowerBIClient(DataSourceClient):
    """
    Power BI client for discovering semantic models and executing DAX queries.

    Auto-discovers all workspaces, datasets (semantic models), and reports
    that the service principal has access to.
    """

    BASE_URL = "https://api.powerbi.com/v1.0/myorg"
    AUTH_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    SCOPE = "https://analysis.windows.net/powerbi/api/.default"

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret

        self._access_token: Optional[str] = None
        self._http: Optional[requests.Session] = None

    def connect(self):
        """
        Authenticate with Azure AD and obtain an access token for Power BI API.
        Reuses cached token if already authenticated.
        """
        if self._http and self._access_token:
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
        self.connect()
        workspaces = self.list_workspaces()

        dataset_count = 0
        for ws in workspaces:
            dataset_count += len(self.list_datasets(ws["id"]))

        return {
            "success": True,
            "message": f"Connected to Power BI. Found {len(workspaces)} workspace(s), {dataset_count} dataset(s).",
            "workspaces": len(workspaces),
            "datasets": dataset_count,
        }

    def list_workspaces(self) -> List[Dict]:
        """
        List all workspaces (groups) the service principal has access to.
        """
        self.connect()
        url = f"{self.BASE_URL}/groups"
        headers = self._build_headers()

        results: List[Dict] = []
        while url:
            resp = self._http.get(url, headers=headers, timeout=30)
            if resp.status_code >= 300:
                raise RuntimeError(f"Failed to list workspaces: HTTP {resp.status_code} {resp.text}")

            payload = resp.json() or {}
            items = payload.get("value") or []
            for ws in items:
                results.append({
                    "id": ws.get("id"),
                    "name": ws.get("name"),
                    "type": ws.get("type"),
                    "isOnDedicatedCapacity": ws.get("isOnDedicatedCapacity"),
                })
            url = payload.get("@odata.nextLink")

        return results

    def list_datasets(self, workspace_id: str) -> List[Dict]:
        """
        List all datasets (semantic models) in a workspace.
        """
        self.connect()
        url = f"{self.BASE_URL}/groups/{workspace_id}/datasets"
        headers = self._build_headers()

        results: List[Dict] = []
        while url:
            resp = self._http.get(url, headers=headers, timeout=30)
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
        headers = self._build_headers()

        results: List[Dict] = []
        while url:
            resp = self._http.get(url, headers=headers, timeout=30)
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

    def get_dataset_tables(self, workspace_id: str, dataset_id: str) -> List[Dict]:
        """
        Get tables and columns for a dataset.
        Tries multiple approaches in order:
        1. REST API /tables endpoint (works for Push datasets)
        2. DAX COLUMNSTATISTICS() function (works for most datasets)
        3. Admin Scanner API (works for all datasets with admin permissions)
        """
        self.connect()
        headers = self._build_headers()

        # Try REST API first (only works for Push datasets)
        url = f"{self.BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/tables"
        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code < 300:
            tables = (resp.json() or {}).get("value") or []
            if tables and any(t.get("columns") for t in tables):
                return tables

        # Try DAX COLUMNSTATISTICS() - works for most datasets
        tables = self._get_tables_via_column_stats(workspace_id, dataset_id)
        if tables:
            return tables

        # Fallback: Use Admin Scanner API for schema discovery (needs admin perms)
        return self._get_tables_via_admin_scan(workspace_id, dataset_id)

    def _get_tables_via_column_stats(self, workspace_id: str, dataset_id: str) -> List[Dict]:
        """
        Get table/column metadata using DAX COLUMNSTATISTICS() function.
        Works for most imported and DirectQuery datasets.
        """
        import logging

        try:
            # COLUMNSTATISTICS() returns: Table Name, Column Name, Min, Max, Cardinality, Max Length
            stats_dax = "EVALUATE COLUMNSTATISTICS()"
            stats_df = self._execute_dax_internal(workspace_id, dataset_id, stats_dax)

            if stats_df.empty:
                return []

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

            return list(tables_dict.values())

        except Exception as e:
            logging.debug(f"COLUMNSTATISTICS failed for dataset {dataset_id}: {e}")
            return []

    def _get_tables_via_admin_scan(self, workspace_id: str, dataset_id: str) -> List[Dict]:
        """
        Get table/column metadata using the Admin Scanner API.
        Requires the service principal to have admin permissions.
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
                return []

            scan_data = resp.json() or {}
            scan_id = scan_data.get("id")
            if not scan_id:
                logging.warning("Admin scan did not return scan ID")
                return []

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
                return []

            # Step 3: Get scan results
            result_url = f"{self.BASE_URL}/admin/workspaces/scanResult/{scan_id}"
            result_resp = self._http.get(result_url, headers=headers, timeout=60)
            if result_resp.status_code >= 300:
                logging.warning(f"Failed to get scan results: HTTP {result_resp.status_code}")
                return []

            result_data = result_resp.json() or {}
            workspaces = result_data.get("workspaces") or []

            # Find the dataset in the scan results
            for ws in workspaces:
                for ds in ws.get("datasets") or []:
                    if ds.get("id") == dataset_id:
                        return self._parse_admin_scan_tables(ds)

            return []

        except Exception as e:
            logging.warning(f"Failed to get tables via admin scan for dataset {dataset_id}: {e}")
            return []

    def _parse_admin_scan_tables(self, dataset: Dict) -> List[Dict]:
        """Parse tables/columns/measures from Admin Scanner API response."""
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

        return list(tables_dict.values())

    def get_schemas(self) -> List[Table]:
        """
        Build Table objects representing all datasets across all workspaces.
        Each dataset becomes one Table with columns from all its internal tables.
        """
        workspaces = self.list_workspaces()
        tables: List[Table] = []

        for ws in workspaces:
            ws_id = ws.get("id")
            ws_name = ws.get("name") or ws_id

            # Get datasets and reports for this workspace
            datasets = self.list_datasets(ws_id)
            reports = self.list_reports(ws_id)

            # Build a map of datasetId -> list of reports
            reports_by_dataset: Dict[str, List[Dict]] = {}
            for rpt in reports:
                ds_id = rpt.get("datasetId")
                if ds_id:
                    if ds_id not in reports_by_dataset:
                        reports_by_dataset[ds_id] = []
                    reports_by_dataset[ds_id].append({
                        "id": rpt.get("id"),
                        "name": rpt.get("name"),
                        "webUrl": rpt.get("webUrl"),
                    })

            for ds in datasets:
                ds_id = ds.get("id")
                ds_name = ds.get("name") or ds_id

                # Get tables/columns for this dataset
                ds_tables = self.get_dataset_tables(ws_id, ds_id)

                # Flatten all columns from all internal tables
                columns: List[TableColumn] = []
                internal_table_names: List[str] = []

                for tbl in ds_tables:
                    tbl_name = tbl.get("name") or ""
                    internal_table_names.append(tbl_name)

                    for col in tbl.get("columns") or []:
                        col_name = col.get("name") or ""
                        col_type = col.get("dataType") or "unknown"

                        columns.append(TableColumn(
                            name=col_name,
                            dtype=col_type,
                            description=None,
                            metadata={
                                "table": tbl_name,
                                "role": "column",
                            }
                        ))

                    # Include measures if available
                    for measure in tbl.get("measures") or []:
                        measure_name = measure.get("name") or ""
                        expression = measure.get("expression") or ""

                        columns.append(TableColumn(
                            name=measure_name,
                            dtype="measure",
                            description=expression[:200] if expression else None,
                            metadata={
                                "table": tbl_name,
                                "role": "measure",
                                "expression": expression,
                            }
                        ))

                # Build table name as workspace/dataset
                table_name = f"{ws_name}/{ds_name}"

                metadata_json = {
                    "powerbi": {
                        "datasetId": ds_id,
                        "workspaceId": ws_id,
                        "workspaceName": ws_name,
                        "datasetName": ds_name,
                        "configuredBy": ds.get("configuredBy"),
                        "webUrl": ds.get("webUrl"),
                        "tables": internal_table_names,
                        "reports": reports_by_dataset.get(ds_id, []),
                    }
                }

                tables.append(Table(
                    name=table_name,
                    description=None,
                    columns=columns,
                    pks=[],
                    fks=[],
                    is_active=True,
                    metadata_json=metadata_json,
                ))
        return tables

    def get_schema(self, table_name: str) -> Table:
        """
        Get schema for a single dataset by name or ID.

        Accepts:
          - dataset ID (exact match)
          - "workspace/dataset" name path
          - dataset display name (first match)
        """
        all_tables = self.get_schemas()

        # Try exact name match
        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl

        # Try by dataset ID
        for tbl in all_tables:
            metadata = tbl.metadata_json or {}
            pbi = metadata.get("powerbi") or {}
            if pbi.get("datasetId") == table_name:
                return tbl

        # Try by dataset name only (first match)
        for tbl in all_tables:
            metadata = tbl.metadata_json or {}
            pbi = metadata.get("powerbi") or {}
            if pbi.get("datasetName") == table_name:
                return tbl

        raise RuntimeError(f"Dataset not found for '{table_name}'")

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
            table_name: Table name (e.g., "BOW/deals2") - will look up dataset_id/workspace_id
            dataset_id: Power BI dataset ID (alternative to table_name)
            workspace_id: Power BI workspace ID
            max_rows: Maximum rows to return

        Example:
            df = client.execute_query("EVALUATE Sales", "BOW/deals2")
            # or with explicit IDs:
            df = client.execute_query("EVALUATE Sales", dataset_id="abc", workspace_id="xyz")
        """
        if not query:
            raise ValueError("DAX query is required")

        # If table_name provided (but not dataset_id), look up the IDs
        if table_name and not dataset_id:
            try:
                table = self.get_schema(table_name)
                pbi = (table.metadata_json or {}).get("powerbi") or {}
                dataset_id = pbi.get("datasetId")
                workspace_id = workspace_id or pbi.get("workspaceId")
            except Exception:
                pass

        if not dataset_id:
            raise ValueError("dataset_id is required (pass table_name or dataset_id)")

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
        headers = self._build_headers()

        body = {
            "queries": [{"query": dax}],
            "serializerSettings": {"includeNulls": True},
        }

        resp = self._http.post(url, json=body, headers=headers, timeout=120)
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

        # Clean column names (remove brackets like [ColumnName])
        df = pd.DataFrame(rows)
        df.columns = [col.strip("[]") for col in df.columns]

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

### CRITICAL: Schema Name vs DAX Table Name

There are TWO different names you need to understand:

1. **Schema name** (e.g., "BOW/leads") - Used ONLY as the second argument to `execute_query()`
2. **DAX table name** (e.g., "Mock_Salesforce_Leads 2") - Used INSIDE the DAX query itself

The schema name identifies which Power BI dataset to query.
The DAX table name is the actual table name inside that dataset.

**Find the DAX table name**: Look at each column's `metadata.table` field - that's the internal table name to use in DAX.

### How to Execute Queries

**Signature**: `execute_query(dax_query, schema_name)` - BOTH arguments are REQUIRED!

```python
# CORRECT: Schema name as 2nd arg, DAX table name in query
df = db_clients['powerbi'].execute_query(
    "EVALUATE 'Mock_Salesforce_Leads 2'",  # DAX uses internal table name
    "BOW/leads"                             # Schema name (REQUIRED)
)
```

**IMPORTANT**: You MUST always pass the schema name (e.g., "BOW/leads") as the second argument. Without it, the query will fail.

### DAX Query Pattern

```dax
EVALUATE <table_expression>
```

### Examples

```dax
-- Get all rows (use internal table name, quote if has spaces)
EVALUATE 'Mock_Salesforce_Leads 2'

-- Aggregate with grouping
EVALUATE
SUMMARIZECOLUMNS(
    'My Table'[Category],
    "Total", SUM('My Table'[Amount])
)

-- Filter data
EVALUATE
FILTER(
    'My Table',
    'My Table'[Status] = "Active"
)

-- Top N results
EVALUATE
TOPN(10,
    SUMMARIZECOLUMNS('My Table'[Name], "Total", SUM('My Table'[Value])),
    [Total], DESC
)
```

### Key DAX Syntax Rules
- Table names with spaces MUST use single quotes: 'My Table'[Column]
- Column references: TableName[ColumnName] or 'Table Name'[ColumnName]
- Measure references: [MeasureName] (no table prefix)
- String literals use double quotes: "value"
- NEVER use the schema name (like "BOW/leads") in DAX - use the internal table name from metadata.table
- INFO.TABLES() and INFO.COLUMNS() do NOT work via REST API - use the schema metadata instead
"""

    # ----------------------------
    # Internal helpers
    # ----------------------------

    def _build_headers(self) -> Dict[str, str]:
        if not self._access_token:
            raise RuntimeError("Not authenticated")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }


# Compatibility alias for dynamic resolver expecting 'PowerbiClient'
PowerbiClient = PowerBIClient
