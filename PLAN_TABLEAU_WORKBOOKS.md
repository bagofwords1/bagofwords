# Tableau Workbooks & Views Enhancement Plan

## Overview

Extend the existing Tableau client to discover workbooks and views (dashboards/worksheets) and attach them to datasource metadata for navigation purposes.

## Current State

The Tableau client currently supports:
- Published datasource discovery via REST API
- Field metadata via VizQL + Metadata GraphQL API
- Query execution via VizQL Data Service

## Goal

Add workbook/view metadata to each datasource's `metadata_json` so users can navigate from a datasource to the dashboards that use it.

## Architecture

```
Tableau Server/Cloud
├── Site
│   ├── Project(s)
│   │   ├── Published Datasource ──► Table object (current)
│   │   │   └── metadata_json.tableau.workbooks[] ──► NEW
│   │   │   └── metadata_json.tableau.views[] ──► NEW
│   │   ├── Workbook
│   │   │   └── Views (worksheets, dashboards)
```

## Files to Modify

### 1. Modify: `backend/app/data_sources/clients/tableau_client.py`

Add new methods and update `get_schemas()`.

#### Extend Existing GraphQL Query

Update `_metadata_fields_for_datasource()` to include downstream content:

```python
query = """
query datasourceFieldInfo {
  publishedDatasources(filter: { luid: "%s" }) {
    name
    description
    fields {
      name
      description
      __typename
      ... on ColumnField { dataType role }
      ... on CalculatedField { dataType role formula }
      ... on BinField { dataType }
      ... on GroupField { dataType role }
    }
    # NEW: downstream content
    downstreamWorkbooks {
      luid
      name
      projectName
    }
    downstreamDashboards {
      luid
      name
      path
    }
    downstreamSheets {
      luid
      name
      path
    }
  }
}
"""
```

Return type changes from `tuple` to include workbooks/views data.

#### Updated Schema Output

```python
Table(
    name="Project/Datasource",
    # ... existing fields ...
    metadata_json={
        "tableau": {
            "datasourceLuid": "...",
            "projectId": "...",
            "projectName": "...",
            "name": "...",
            "siteName": "...",
            # NEW
            "workbooks": [
                {
                    "id": "abc-123",
                    "name": "Sales Dashboard",
                    "projectName": "Analytics",
                    "webUrl": "https://tableau.company.com/#/views/SalesDashboard"
                }
            ],
            "views": [
                {
                    "id": "def-456",
                    "name": "Q4 Summary",
                    "viewType": "dashboard",  # or "worksheet"
                    "workbookName": "Sales Dashboard",
                    "webUrl": "https://tableau.company.com/#/views/SalesDashboard/Q4Summary"
                }
            ]
        }
    }
)
```

## API Used

Same Metadata GraphQL endpoint already used for field discovery: `POST /api/metadata/graphql`

No additional REST API calls needed.

## Implementation Steps

### Phase 1: Extend GraphQL Query
- [ ] Update `_metadata_fields_for_datasource()` to query `downstreamWorkbooks`, `downstreamDashboards`, `downstreamSheets`
- [ ] Update return type to include downstream content
- [ ] Build web URLs from path data

### Phase 2: Schema Integration
- [ ] Extend `_metadata_fields_for_datasource()` GraphQL query to include `downstreamWorkbooks` and `downstreamDashboards`
- [ ] Update `get_schemas()` to include workbooks/views in `metadata_json`
- [ ] Update `get_schema()` similarly

### Phase 3: Testing
- [ ] Unit tests for new methods
- [ ] Integration test with real Tableau Server/Cloud
- [ ] Verify navigation URLs are correct

## URL Construction

Tableau view URLs follow this pattern:

```
# Tableau Server
https://{server}/#/site/{siteName}/views/{workbookName}/{viewName}

# Tableau Cloud
https://{pod}.online.tableau.com/#/site/{siteName}/views/{workbookName}/{viewName}

# Default site (no site segment)
https://{server}/#/views/{workbookName}/{viewName}
```

The REST API returns `contentUrl` for workbooks and views which can be used to construct these URLs.

## Considerations

### Many-to-Many Relationship

Unlike Power BI (where a report belongs to one dataset), Tableau has many-to-many:
- A workbook can use multiple datasources
- A datasource can feed multiple workbooks

**Decision**: Attach all downstream workbooks/views to each datasource. Some duplication is acceptable for navigation purposes.

### Performance

No additional API calls - we extend the existing GraphQL query in `_metadata_fields_for_datasource()` to include downstream content. Minimal overhead.

### Permissions

The service account/PAT needs:
- `Read` permission on workbooks to list them
- Metadata API access (enabled by default on Tableau Cloud, may need enabling on Server)

## Open Questions

1. **Dashboards vs sheets?** The GraphQL API separates `downstreamDashboards` and `downstreamSheets`. Include both, or just dashboards?

2. **Embedded datasources?** Some workbooks have embedded (non-published) datasources. These won't appear in our datasource list. Ignore for now.
