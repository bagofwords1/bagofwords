# Power BI Integration Plan

## Overview

Add Power BI as a data source to discover semantic models (datasets), fetch table/column/measure metadata, and execute DAX queries.

## Architecture

```
Power BI Service
├── Workspace(s)
│   ├── Semantic Model (Dataset) ──► Table object with columns/measures
│   │   ├── Tables
│   │   ├── Columns
│   │   ├── Measures
│   │   └── Relationships
│   └── Reports ──► Stored in metadata_json for navigation
```

## Files to Create/Modify

### 1. New File: `backend/app/data_sources/clients/powerbi_client.py`

The main client implementation.

```python
class PowerBIClient(DataSourceClient):
    def __init__(self, tenant_id, client_id, client_secret, workspace_ids, include_reports=True):
        ...

    # Auth
    def _get_access_token(self) -> str
    def _build_headers(self) -> dict

    # Connection
    def connect(self)
    def test_connection(self) -> dict

    # Discovery
    def list_datasets(self) -> List[dict]
    def list_reports(self) -> List[dict]
    def _get_dataset_tables(self, dataset_id: str) -> List[dict]
    def _get_dataset_measures(self, dataset_id: str) -> List[dict]

    # Schema (returns Table objects)
    def get_schemas(self) -> List[Table]
    def get_schema(self, table_name: str) -> Table

    # Query
    def execute_query(self, dax: str, dataset_id: str) -> pd.DataFrame

    # LLM support
    def prompt_schema(self) -> str
    def system_prompt(self) -> str
    @property
    def description(self) -> str
```

### 2. Modify: `backend/app/schemas/data_sources/configs.py`

Add config and credentials classes.

```python
# Power BI Credentials
class PowerBICredentials(BaseModel):
    tenant_id: str = Field(..., title="Tenant ID", description="Azure AD Tenant ID", json_schema_extra={"ui:type": "string"})
    client_id: str = Field(..., title="Client ID", description="Azure AD App Client ID", json_schema_extra={"ui:type": "string"})
    client_secret: str = Field(..., title="Client Secret", description="Azure AD App Secret", json_schema_extra={"ui:type": "password"})

# Power BI Config - empty, auto-discovers all workspaces/datasets
class PowerBIConfig(BaseModel):
    pass
```

### 3. Modify: `backend/app/schemas/data_source_registry.py`

Add registry entry.

```python
"powerbi": DataSourceRegistryEntry(
    type="powerbi",
    title="Power BI",
    description="Query Power BI semantic models via DAX and discover reports.",
    config_schema=PowerBIConfig,
    credentials_auth=AuthOptions(
        default="service_principal",
        by_auth={
            "service_principal": AuthVariant(
                title="Service Principal",
                schema=PowerBICredentials,
                scopes=["system", "user"]
            )
        }
    ),
    client_path="app.data_sources.clients.powerbi_client.PowerBIClient",
)
```

## API Endpoints Used

| Purpose | Method | Endpoint |
|---------|--------|----------|
| Auth | POST | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` |
| List datasets | GET | `https://api.powerbi.com/v1.0/myorg/groups/{workspaceId}/datasets` |
| List reports | GET | `https://api.powerbi.com/v1.0/myorg/groups/{workspaceId}/reports` |
| Get tables | GET | `https://api.powerbi.com/v1.0/myorg/datasets/{datasetId}/tables` |
| Execute DAX | POST | `https://api.powerbi.com/v1.0/myorg/datasets/{datasetId}/executeQueries` |

Note: For richer metadata (measures, relationships), may need to use:
- XMLA endpoint with DMV queries (`$SYSTEM.TMSCHEMA_*`)
- Or Metadata scanning APIs (Admin APIs)

## Data Mapping

### Semantic Model → Table

```python
Table(
    name="{workspace_name}/{dataset_name}",
    description=dataset.get("description"),
    columns=[
        TableColumn(
            name=column["name"],
            dtype=column["dataType"],
            description=column.get("description"),
            metadata={
                "table": internal_table_name,  # e.g., "Sales"
                "role": "column" | "measure",
                "expression": measure_expression,  # for measures only
            }
        )
        for column in all_columns_and_measures
    ],
    pks=[],
    fks=[],
    is_active=True,
    metadata_json={
        "powerbi": {
            "datasetId": dataset["id"],
            "workspaceId": workspace_id,
            "workspaceName": workspace_name,
            "configuredBy": dataset.get("configuredBy"),
            "refreshedAt": dataset.get("refreshSchedule"),
            "tables": ["Sales", "Products", ...],  # internal table names
            "relationships": [
                {"from": "Sales.ProductID", "to": "Products.ProductID", "type": "manyToOne"}
            ],
            "reports": [
                {"id": "...", "name": "Q4 Report", "webUrl": "https://..."}
            ]
        }
    }
)
```

## DAX Query Execution

### Request
```python
def execute_query(self, dax: str, dataset_id: str) -> pd.DataFrame:
    url = f"{self.base_url}/datasets/{dataset_id}/executeQueries"
    body = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True}
    }
    response = self._http.post(url, json=body, headers=self._build_headers())
    return self._parse_dax_response(response.json())
```

### Response Parsing
```json
{
  "results": [
    {
      "tables": [
        {
          "rows": [
            {"[ProductName]": "Widget", "[Total Revenue]": 1500.00},
            {"[ProductName]": "Gadget", "[Total Revenue]": 2300.00}
          ]
        }
      ]
    }
  ]
}
```

## System Prompt for LLM

```python
def system_prompt(self):
    return """
## Power BI DAX Query Guide

Execute DAX queries against Power BI semantic models to answer business questions.

### Basic Query Pattern
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

-- Top N
EVALUATE
TOPN(
    10,
    SUMMARIZECOLUMNS(
        Customers[CustomerName],
        "Total", SUM(Sales[Revenue])
    ),
    [Total], DESC
)

-- Use existing measures (referenced without table prefix)
EVALUATE
SUMMARIZECOLUMNS(
    'Date'[Month],
    "Revenue", [Total Revenue]  -- measure from model
)
```

### Key Differences from SQL
- No SELECT/FROM/WHERE - use EVALUATE with table functions
- Measures are pre-defined calculations - use them when available
- Table names with spaces need single quotes: 'Date'[Column]
- Column references: TableName[ColumnName]
- Measure references: [MeasureName] (no table prefix)

### Best Practices
- Use existing measures when available (they contain business logic)
- Prefer SUMMARIZECOLUMNS for aggregations
- Use FILTER for row-level filtering
- Check metadata.role to identify measures vs columns
"""
```

## Implementation Steps

### Phase 1: Foundation
- [ ] Add `PowerBIConfig` and `PowerBICredentials` to configs.py
- [ ] Add imports and `__all__` exports in configs.py
- [ ] Add registry entry to data_source_registry.py

### Phase 2: Client Core
- [ ] Create powerbi_client.py with class structure
- [ ] Implement `_get_access_token()` - OAuth2 client credentials flow
- [ ] Implement `connect()` and `test_connection()`
- [ ] Implement `_build_headers()`

### Phase 3: Discovery
- [ ] Implement `list_datasets()` - paginated
- [ ] Implement `list_reports()` - paginated
- [ ] Implement `_get_dataset_tables()` - via REST API
- [ ] Implement `_get_dataset_measures()` - via DMV or REST

### Phase 4: Schema
- [ ] Implement `get_schemas()` - returns List[Table]
- [ ] Implement `get_schema(table_name)` - returns single Table
- [ ] Map columns and measures to TableColumn objects
- [ ] Build metadata_json with relationships and reports

### Phase 5: Query
- [ ] Implement `execute_query(dax, dataset_id)`
- [ ] Parse DAX response into DataFrame
- [ ] Handle errors and edge cases

### Phase 6: LLM Support
- [ ] Implement `system_prompt()` with DAX examples
- [ ] Implement `prompt_schema()`
- [ ] Implement `description` property

### Phase 7: Testing
- [ ] Unit tests for client methods
- [ ] Integration test with real Power BI workspace
- [ ] Test connection form in UI

## Azure AD App Setup (Documentation for Users)

1. Go to Azure Portal → Azure Active Directory → App registrations
2. New registration → Name: "BagOfWords Power BI"
3. Note the **Application (client) ID** and **Directory (tenant) ID**
4. Certificates & secrets → New client secret → Copy value
5. API permissions → Add:
   - `Power BI Service` → `Dataset.Read.All`
   - `Power BI Service` → `Report.Read.All`
   - `Power BI Service` → `Workspace.Read.All`
6. Grant admin consent
7. In Power BI Admin Portal → Tenant settings → Enable "Service principals can use Power BI APIs"
8. Add service principal to workspace as Member or higher

## Open Questions

1. **Measures discovery**: REST API `/tables` doesn't return measures. Options:
   - Use XMLA endpoint with DMV query `SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES`
   - Use Admin API metadata scanning
   - Accept limitation and only show columns (not ideal)

2. **Relationships**: Not available via simple REST API. Options:
   - XMLA/DMV: `SELECT * FROM $SYSTEM.TMSCHEMA_RELATIONSHIPS`
   - Skip for MVP, add later

3. **Workspace discovery**: Should we auto-discover all workspaces the service principal has access to, or require explicit workspace IDs?

## Dependencies

```
requests  # Already in project
pandas    # Already in project
msal      # Microsoft Authentication Library (optional, can use raw requests)
```
