from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
from typing import List, Dict, Optional
import requests
import pandas as pd


class TableauClient(DataSourceClient):

    def __init__(
        self,
        server_url: str,
        site_name: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        pat_name: Optional[str] = None,
        pat_token: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_sec: int = 30,
        default_project_id: Optional[str] = None,
        include_datasource_ids: Optional[List[str]] = None,
    ):
        self.server_url = server_url
        self.site_name = site_name
        self.username = username
        self.password = password
        self.pat_name = pat_name
        self.pat_token = pat_token
        self.verify_ssl = verify_ssl
        self.timeout_sec = timeout_sec
        self.default_project_id = default_project_id
        self.include_datasource_ids = include_datasource_ids or []

        self._site_id = None
        self._auth_token = None
        self._http = None
        self._api_version = "3.21"  # REST API version

    def connect(self):
        """
        Sign in to Tableau via REST API to obtain X-Tableau-Auth and site id.
        Reuses a cached session if already authenticated.
        """
        if self._http and self._auth_token and self._site_id:
            return

        if not self.server_url:
            raise RuntimeError("server_url is required")

        session = requests.Session()

        signin_url = f"{self.server_url.rstrip('/')}/api/{self._api_version}/auth/signin"
        if self.pat_name and self.pat_token:
            payload = {
                "credentials": {
                    "personalAccessTokenName": self.pat_name,
                    "personalAccessTokenSecret": self.pat_token,
                    "site": {"contentUrl": self.site_name or ""},
                }
            }
        elif self.username and self.password:
            payload = {
                "credentials": {
                    "name": self.username,
                    "password": self.password,
                    "site": {"contentUrl": self.site_name or ""},
                }
            }
        else:
            raise RuntimeError("Either PAT (pat_name, pat_token) or username/password must be provided")

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        resp = session.post(signin_url, json=payload, headers=headers, timeout=self.timeout_sec, verify=self.verify_ssl)
        if resp.status_code >= 300:
            raise RuntimeError(f"Failed to sign in to Tableau: HTTP {resp.status_code} {resp.text}")
        data = (resp.json() or {}).get("credentials") or {}
        token = data.get("token")
        site = data.get("site") or {}
        site_id = site.get("id")
        if not token or not site_id:
            raise RuntimeError("Sign-in did not return token or site id")

        self._auth_token = token
        self._site_id = site_id
        self._http = session

    def test_connection(self):
        """
        Validate sign-in, VizQL health, and probe query-datasource availability.
        """
        try:
            self.connect()
            headers = self._build_headers()
            base_url = self.server_url.rstrip('/')
            site_prefix = self._vizql_site_prefix()

            # VizQL health probe
            health_url = f"{base_url}{site_prefix}/vizql-data-service/v1/simple-request"
            health_resp = self._http.get(health_url, headers=headers, timeout=self.timeout_sec, verify=self.verify_ssl)
            health_status = health_resp.status_code

            # Query-datasource probe (best-effort): use first datasource LUID if available
            query_url = f"{base_url}{site_prefix}/vizql-data-service/v1/query-datasource"
            datasource_id = None
            try:
                dss = self.list_published_datasources()
                if dss:
                    datasource_id = dss[0].get("id")
            except Exception:
                datasource_id = None

            query_status = None
            if datasource_id:
                probe_body = {
                    "datasource": {"datasourceLuid": datasource_id},
                    "query": {"fields": [], "filters": []},
                    "options": {"returnFormat": "OBJECTS", "debug": False, "disaggregate": False},
                }
                query_resp = self._http.post(query_url, json=probe_body, headers=headers, timeout=self.timeout_sec, verify=self.verify_ssl)
                query_status = query_resp.status_code

            # Interpret results
            details = {"vizql_health": health_status, "query_status": query_status}
            if health_status < 300 and query_status == 404:
                details["message"] = "VizQL reachable, but query endpoint returned 404 (likely feature/permission issue)."
                return {"success": True, **details}
            if health_status < 300 and (query_status is not None and query_status < 300):
                details["message"] = "Connected; VizQL query endpoint enabled."
                return {"success": True, **details}
            if health_status < 300:
                details["message"] = "Connected to Tableau; VizQL reachable."
                return {"success": True, **details}
            return {"success": True, "message": "Connected to Tableau", **details}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def list_published_datasources(self) -> List[Dict]:
        """
        List published datasources (id, name, project) via REST API.
        Respects include_datasource_ids if provided.
        """
        self.connect()
        results: List[Dict] = []
        page_size = 100
        page_number = 1
        while True:
            url = f"{self.server_url.rstrip('/')}/api/{self._api_version}/sites/{self._site_id}/datasources?pageNumber={page_number}&pageSize={page_size}&format=json"
            headers = self._build_headers()
            resp = self._http.get(url, headers=headers, timeout=self.timeout_sec, verify=self.verify_ssl)
            if resp.status_code >= 300:
                raise RuntimeError(f"Failed to list datasources: HTTP {resp.status_code} {resp.text}")
            payload = resp.json() or {}
            ds_container = (payload.get("datasources") or {})
            ds_items = ds_container.get("datasource") or []
            for ds in ds_items:
                item = {
                    "id": ds.get("id"),
                    "name": ds.get("name"),
                    "project_id": (ds.get("project") or {}).get("id"),
                    "project_name": (ds.get("project") or {}).get("name"),
                }
                results.append(item)
            pagination = (payload.get("pagination") or {})
            total_available = int(pagination.get("totalAvailable", len(results)))
            if len(results) >= total_available or not ds_items:
                break
            page_number += 1

        if self.include_datasource_ids:
            results = [r for r in results if r.get("id") in set(self.include_datasource_ids)]
        if self.default_project_id:
            results = [r for r in results if r.get("project_id") == self.default_project_id]
        return results

    def get_schemas(self) -> List[Table]:
        """
        Build Table objects representing published datasources with columns
        discovered by combining VizQL read-metadata with Metadata GraphQL API (publishedDatasources).
        """
        datasources = self.list_published_datasources()
        tables: List[Table] = []
        for ds in datasources:
            fields = self._combined_fields_for_datasource(ds["id"])  # list of dicts
            columns = [
                TableColumn(name=(f.get("fieldCaption") or f.get("fieldName") or ""), dtype=(f.get("dataType") or "unknown"))
                for f in fields
            ]
            table_name = f"{(ds.get('project_name') or '').strip()}/{ds.get('name') or ds.get('id')}".strip("/")
            tables.append(Table(name=table_name, columns=columns, pks=[], fks=[], is_active=True))
        return tables

    def get_schema(self, table_name: str) -> Table:
        """
        Resolve a single datasource by project/name or by id, then fetch fields via read-metadata.
        Accepts:
          - datasource LUID (exact id)
          - "project/datasource" name path
          - datasource display name (first match)
        """
        datasources = self.list_published_datasources()
        target = None
        # Direct id match
        for ds in datasources:
            if ds.get("id") == table_name:
                target = ds
                break
        if not target:
            # project/name path
            for ds in datasources:
                path = f"{(ds.get('project_name') or '').strip()}/{ds.get('name') or ''}".strip("/")
                if path == table_name:
                    target = ds
                    break
        if not target:
            # fallback: first name match
            for ds in datasources:
                if (ds.get("name") or "") == table_name:
                    target = ds
                    break
        if not target:
            raise RuntimeError(f"Datasource not found for '{table_name}'")

        fields = self._combined_fields_for_datasource(target["id"])  # list of dicts
        if not fields:
            raise RuntimeError(
                "No fields returned. Ensure Metadata API is enabled or VizQL Data Service (Headless BI) is available for your site."
            )
        columns = [
            TableColumn(name=(f.get("fieldCaption") or f.get("fieldName") or ""), dtype=(f.get("dataType") or "unknown"))
            for f in fields
        ]
        resolved_name = f"{(target.get('project_name') or '').strip()}/{target.get('name') or target.get('id')}".strip("/")
        return Table(name=resolved_name, columns=columns, pks=[], fks=[], is_active=True)

    def execute_query(
        self,
        *,
        datasource_id: str,
        fields: List[Dict],
        filters: Optional[List[Dict]] = None,
        return_format: str = "OBJECTS",
        disaggregate: bool = False,
        max_rows: Optional[int] = None,
    ):
        """
        Execute Headless BI query-datasource against a published datasource and return a DataFrame.
        """
        self.connect()
        url = f"{self.server_url.rstrip('/')}{self._vizql_site_prefix()}/vizql-data-service/v1/query-datasource"
        headers = self._build_headers()
        body = {
            "datasource": {
                "datasourceLuid": datasource_id,
            },
            "query": {
                "fields": fields or [],
                "filters": filters or [],
            },
            "options": {
                "returnFormat": return_format,
                "debug": False,
                "disaggregate": disaggregate,
            },
        }
        resp = self._http.post(url, json=body, headers=headers, timeout=self.timeout_sec, verify=self.verify_ssl)
        if resp.status_code >= 300:
            raise RuntimeError(f"query-datasource failed: HTTP {resp.status_code} {resp.text}")
        payload = resp.json() or {}
        data = payload.get("data")
        if data is None:
            return pd.DataFrame([])
        # Normalize to DataFrame
        if isinstance(data, list):
            if len(data) == 0:
                return pd.DataFrame([])
            if isinstance(data[0], dict):
                df = pd.DataFrame(data)
            elif isinstance(data[0], list):
                # Map columns from fields list if available
                col_names = []
                for f in fields or []:
                    alias = f.get("fieldAlias")
                    caption = f.get("fieldCaption")
                    col_names.append(alias or caption or "col")
                df = pd.DataFrame(data, columns=col_names if col_names else None)
            else:
                df = pd.DataFrame(data)
        elif isinstance(data, dict):
            # Single object
            df = pd.DataFrame([data])
        else:
            df = pd.DataFrame([])

        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)
        return df

    def prompt_schema(self) -> str:
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str

    @property
    def description(self):
        text = "Tableau Client: discover schemas via Metadata API and query published data sources via VizQL Data Service."
        text += self.system_prompt()

        return text
    
    def system_prompt(self):
        text = """
Executes VizQL queries against Tableau data sources to answer business questions from published data. This tool allows you to retrieve aggregated and filtered data with proper sorting and grouping.

## Examples

```python
# Quick profiling: record count
df = client.execute_query(
  datasource_id="abc123",
  fields=[{"fieldCaption": "Order ID", "function": "COUNT", "fieldAlias": "Total Records"}]
)
```

```python
# Top 10 customers by revenue (with current year filter)
df = client.execute_query(
  datasource_id="abc123",
  fields=[
    {"fieldCaption": "Customer Name"},
    {
      "fieldCaption": "Sales",
      "function": "SUM",
      "fieldAlias": "Total Revenue",
      "sortDirection": "DESC",
      "sortPriority": 1
    }
  ],
  filters=[
    {
      "field": {"fieldCaption": "Customer Name"},
      "filterType": "TOP",
      "howMany": 10,
      "direction": "TOP",
      "fieldToMeasure": {"fieldCaption": "Sales", "function": "SUM"}
    },
    {
      "field": {"fieldCaption": "Order Date"},
      "filterType": "DATE",
      "periodType": "YEARS",
      "dateRangeType": "CURRENT"
    }
  ]
)
```

```python
# Monthly sales trend (last 12 months)
df = client.execute_query(
  datasource_id="abc123",
  fields=[
    {
      "fieldCaption": "Order Date",
      "function": "TRUNC_MONTH",
      "fieldAlias": "Month",
      "sortDirection": "ASC",
      "sortPriority": 1
    },
    {"fieldCaption": "Sales", "function": "SUM", "fieldAlias": "Monthly Sales"},
    {"fieldCaption": "Order ID", "function": "COUNT", "fieldAlias": "Order Count"}
  ],
  filters=[
    {
      "field": {"fieldCaption": "Order Date"},
      "filterType": "DATE",
      "periodType": "MONTHS",
      "dateRangeType": "LAST",
      "amount": 12
    }
  ]
)
```

### Data Volume Management
- **Always prefer aggregation** - Use aggregated fields (SUM, COUNT, AVG, etc.) instead of raw row-level data to reduce response size
- **Profile data before querying** - When unsure about data volume, first run a COUNT query to understand the scale:
  ```json
  {
    "fields": [
      {
        "fieldCaption": "Order ID",
        "function": "COUNT",
        "fieldAlias": "Total Records"
      }
    ]
  }
  ```
- **Use TOP filters for rankings** - When users ask for "top N" results, use TOP filter type to limit results at the database level
- **Apply restrictive filters** - Use SET, QUANTITATIVE, or DATE filters to reduce data volume before processing
- **Avoid row-level queries when possible** - Only retrieve individual records when specifically requested and the business need is clear

### Field Usage Guidelines
- **Prefer existing fields** - Use fields already modeled in the data source rather than creating custom calculations
- **Use calculations sparingly** - Only create calculated fields when absolutely necessary and the calculation cannot be achieved through existing fields and aggregations
- **Validate field availability** - Always check field metadata before constructing queries

### Query Construction
- **Group by meaningful dimensions** - Ensure grouping supports the business question being asked
- **Order results logically** - Use sortDirection and sortPriority to present data in a meaningful way
- **Use appropriate date functions** - Choose the right date aggregation (YEAR, QUARTER, MONTH, WEEK, DAY, or TRUNC_* variants)
- **Leverage filter capabilities** - Use the extensive filter options to narrow results

## Data Profiling Strategy

When a query might return large amounts of data, follow this profiling approach:

**Step 1: Count total records**
```json
{
  "fields": [
    {
      "fieldCaption": "Primary_Key_Field",
      "function": "COUNT",
      "fieldAlias": "Total Records"
    }
  ]
}
```

**Step 2: Count by key dimensions**
```json
{
  "fields": [
    {
      "fieldCaption": "Category",
      "fieldAlias": "Category"
    },
    {
      "fieldCaption": "Order ID",
      "function": "COUNT",
      "fieldAlias": "Record Count"
    }
  ]
}
```

**Step 3: Apply appropriate aggregation or filtering based on counts**

## Filter Types and Usage

### SET Filters
Filter by specific values:
```json
{
  "field": {"fieldCaption": "Region"},
  "filterType": "SET",
  "values": ["North", "South", "East"],
  "exclude": false
}
```

### TOP Filters  
Get top/bottom N records by a measure:
```json
{
  "field": {"fieldCaption": "Customer Name"},
  "filterType": "TOP",
  "howMany": 10,
  "direction": "TOP",
  "fieldToMeasure": {"fieldCaption": "Sales", "function": "SUM"}
}
```

### QUANTITATIVE Filters
Filter numeric ranges:
```json
{
  "field": {"fieldCaption": "Sales"},
  "filterType": "QUANTITATIVE_NUMERICAL",
  "quantitativeFilterType": "RANGE",
  "min": 1000,
  "max": 50000,
  "includeNulls": false
}
```

### DATE Filters
Filter relative date periods:
```json
{
  "field": {"fieldCaption": "Order Date"},
  "filterType": "DATE",
  "periodType": "MONTHS",
  "dateRangeType": "LAST"
}
```

## Example Queries (JSON structure)
- The JSON snippets below illustrate the shape of `fields` and `filters` when constructing queries.

"""

        return text

    # ----------------------------
    # Internal helpers
    # ----------------------------

    def _build_headers(self) -> Dict[str, str]:
        if not self._auth_token or not self._site_id:
            raise RuntimeError("Not authenticated")
        return {
            "X-Tableau-Auth": self._auth_token,
            "X-Tableau-Site-Id": self._site_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _vizql_read_metadata(self, datasource_id: str) -> List[Dict]:
        """
        Call VizQL Data Service read-metadata for a single datasource and return fields list.
        """
        self.connect()
        url = f"{self.server_url.rstrip('/')}{self._vizql_site_prefix()}/vizql-data-service/v1/read-metadata"
        headers = self._build_headers()
        body = {
            "datasource": {
                "datasourceLuid": datasource_id,
            },
            "options": {
                "returnFormat": "OBJECTS",
                "debug": False,
            },
        }
        resp = self._http.post(url, json=body, headers=headers, timeout=self.timeout_sec, verify=self.verify_ssl)
        if resp.status_code == 404:
            # Feature or endpoint may be unavailable; allow fallback to Metadata API
            return []
        if resp.status_code >= 300:
            raise RuntimeError(f"read-metadata failed for {datasource_id}: HTTP {resp.status_code} {resp.text}")
        payload = resp.json() or {}
        fields = payload.get("data") or []
        if not isinstance(fields, list):
            return []
        return fields

    def _metadata_fields_for_datasource(self, datasource_id: str) -> List[Dict]:
        """
        Fetch fields via Tableau Metadata API using publishedDatasources(filter: { luid: "..." }).
        Returns simplified list of dicts with fieldName/fieldCaption/dataType.
        """
        self.connect()
        query = (
            f"""
            query datasourceFieldInfo {{
              publishedDatasources(filter: {{ luid: "{datasource_id}" }}) {{
                name
                fields {{
                  name
                  __typename
                  ... on ColumnField {{ dataType }}
                  ... on CalculatedField {{ dataType }}
                  ... on BinField {{ dataType }}
                  ... on GroupField {{ dataType }}
                }}
              }}
            }}
            """
        ).strip()
        result = self._metadata_graphql_post(query)
        if not isinstance(result, dict) or not result.get("data"):
            return []
        data = result.get("data") or {}
        published = data.get("publishedDatasources") or []
        if not isinstance(published, list) or len(published) == 0:
            return []
        raw_fields = (published[0] or {}).get("fields") or []
        if not isinstance(raw_fields, list):
            return []
        out: List[Dict] = []
        for f in raw_fields:
            if not isinstance(f, dict):
                continue
            out.append(
                {
                    "fieldName": f.get("name") or "",
                    "fieldCaption": f.get("name") or "",
                    "dataType": f.get("dataType") or "unknown",
                }
            )
        return out

    def _combined_fields_for_datasource(self, datasource_id: str) -> List[Dict]:
        """
        Combine VizQL read-metadata with Metadata API fields by name, preferring VizQL types.
        """
        read_fields = self._vizql_read_metadata(datasource_id)  # list of dicts
        gql_fields = self._metadata_fields_for_datasource(datasource_id)
        if not read_fields and not gql_fields:
            return []
        name_to_gql = {str(f.get("fieldName") or f.get("fieldCaption") or f.get("name") or ""): f for f in gql_fields}
        combined: List[Dict] = []
        for rf in read_fields or []:
            n = str(rf.get("fieldCaption") or rf.get("fieldName") or "")
            gf = name_to_gql.get(n)
            combined.append(
                {
                    "fieldName": rf.get("fieldName") or n,
                    "fieldCaption": n,
                    "dataType": rf.get("dataType") or ((gf or {}).get("dataType")) or "unknown",
                }
            )
        # Include any extra fields present only in GQL
        read_names = {str(rf.get("fieldCaption") or rf.get("fieldName") or "") for rf in (read_fields or [])}
        for gf in gql_fields or []:
            n = str(gf.get("fieldCaption") or gf.get("fieldName") or gf.get("name") or "")
            if n and n not in read_names:
                combined.append(
                    {
                        "fieldName": gf.get("fieldName") or n,
                        "fieldCaption": n,
                        "dataType": gf.get("dataType") or "unknown",
                    }
                )
        return combined

    def _metadata_graphql_post(self, query: str, variables: Optional[Dict] = None) -> Dict:
        """
        POST to Tableau Metadata GraphQL endpoint and return JSON dict.
        """
        self.connect()
        base_url = self.server_url.rstrip('/')
        url = f"{base_url}/api/metadata/graphql"
        headers = self._build_headers()
        headers["X-Requested-With"] = "XMLHttpRequest"
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = self._http.post(url, json=payload, headers=headers, timeout=self.timeout_sec, verify=self.verify_ssl)
        if resp.status_code >= 300:
            return {"errors": [{"message": f"HTTP {resp.status_code}", "raw": resp.text}]}
        try:
            return resp.json() or {}
        except Exception:
            return {"errors": [{"message": "Invalid JSON", "raw": resp.text}]}

    # Note: _extract_fields_from_metadata_result removed as we now query a single shape

    def _vizql_site_prefix(self) -> str:
        """
        Build the site-scoped prefix for VizQL endpoints (Tableau Cloud/Server).
        On Default site (empty contentUrl), returns empty string.
        """
        if self.site_name:
            content = str(self.site_name).strip()
            if content and content.lower() != "default":
                return f"/t/{content}"
        return ""


