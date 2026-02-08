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
        """
        Validate authentication and API access by listing workspaces.
        """
        try:
            self.connect()
            workspaces = self.list_workspaces()
            return {
                "success": True,
                "message": f"Connected to Power BI. Found {len(workspaces)} workspace(s).",
                "workspaces": len(workspaces),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

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

    def get_dataset_tables(self, dataset_id: str) -> List[Dict]:
        """
        Get tables and columns for a dataset.
        Note: This endpoint may not return measures - only physical tables/columns.
        """
        self.connect()
        url = f"{self.BASE_URL}/datasets/{dataset_id}/tables"
        headers = self._build_headers()

        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            # Dataset may not support this endpoint (e.g., live connection)
            return []
        if resp.status_code >= 300:
            raise RuntimeError(f"Failed to get dataset tables: HTTP {resp.status_code} {resp.text}")

        payload = resp.json() or {}
        tables = payload.get("value") or []
        return tables

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
                ds_tables = self.get_dataset_tables(ds_id)

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
        *,
        dax: str,
        dataset_id: str,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Execute a DAX query against a dataset and return results as DataFrame.
        """
        self.connect()
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

Execute DAX queries against Power BI semantic models to answer business questions.

### Query Pattern
All DAX queries must start with EVALUATE:
```dax
EVALUATE
<table_expression>
```

### Examples

```dax
-- Get all rows from a table
EVALUATE Sales

-- Aggregate with grouping
EVALUATE
SUMMARIZECOLUMNS(
    Products[Category],
    "Total Revenue", SUM(Sales[Revenue]),
    "Order Count", COUNTROWS(Sales)
)

-- Filter data
EVALUATE
FILTER(
    Sales,
    Sales[OrderDate] >= DATE(2024, 1, 1)
)

-- Top N results
EVALUATE
TOPN(
    10,
    SUMMARIZECOLUMNS(
        Customers[CustomerName],
        "Total", SUM(Sales[Revenue])
    ),
    [Total], DESC
)

-- Use existing measures (no table prefix for measures)
EVALUATE
SUMMARIZECOLUMNS(
    'Date'[Month],
    "Revenue", [Total Revenue]
)

-- Date filtering with relative dates
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        Products[Category],
        "Sales", [Total Sales]
    ),
    DATESINPERIOD('Date'[Date], TODAY(), -30, DAY)
)
```

### Key DAX Syntax Rules
- Always start with EVALUATE
- Table names with spaces need single quotes: 'Date'[Column]
- Column references: TableName[ColumnName]
- Measure references: [MeasureName] (no table prefix)
- String literals use double quotes: "value"

### Best Practices
- Use existing measures when available (they contain business logic)
- Prefer SUMMARIZECOLUMNS for aggregations with grouping
- Use FILTER for row-level filtering
- Use TOPN for ranking queries
- Check metadata.role to identify measures vs columns
- Measures are pre-aggregated - don't wrap them in SUM/COUNT

### Data Volume Management
- Always prefer aggregation over raw row retrieval
- Use TOPN to limit results
- Profile with COUNTROWS first if unsure about data volume
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
