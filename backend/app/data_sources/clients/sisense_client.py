from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, ServiceFormatter
from typing import List, Dict, Optional
import requests
import pandas as pd
import logging

# Sisense column type codes → human-readable names
_COLUMN_TYPE_MAP = {
    0: "bigint",
    2: "boolean",
    3: "char",
    4: "timestamp",
    5: "decimal",
    6: "float",
    8: "integer",
    13: "real",
    16: "smallint",
    18: "varchar",
    20: "tinyint",
    31: "date",
    32: "time",
    40: "double",
    41: "numeric",
    43: "timestamp_tz",
}


def _col_type_name(type_code) -> str:
    """Convert Sisense numeric column type to a readable string."""
    if isinstance(type_code, int):
        return _COLUMN_TYPE_MAP.get(type_code, f"type_{type_code}")
    return str(type_code) if type_code else "unknown"


class SisenseClient(DataSourceClient):
    """
    Sisense client for discovering data models and executing queries.

    Supports two query modes:
    - SQL: standard SQL against ElastiCubes (default, simpler for LLMs)
    - JAQL: Sisense native structured queries (respects row-level security)

    Auto-discovers all data models (ElastiCubes) the authenticated user has access to.
    """

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        api_token: str = "",
    ):
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.api_token = api_token

        self._access_token: Optional[str] = None
        self._http: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Connection / Auth
    # ------------------------------------------------------------------

    def connect(self):
        """
        Authenticate with Sisense and obtain a bearer token.
        If an api_token was provided at init, use it directly.
        Otherwise, authenticate via username/password.
        """
        if self._http and self._access_token:
            return

        self._http = requests.Session()

        if self.api_token:
            self._access_token = self.api_token
            return

        url = f"{self.host}/api/v1/authentication/login"
        payload = {
            "username": self.username,
            "password": self.password,
        }

        resp = requests.post(url, data=payload, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(f"Failed to authenticate with Sisense: HTTP {resp.status_code} {resp.text}")

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError("Authentication did not return access token")

        self._access_token = token

    def test_connection(self) -> Dict:
        # Phase 1: Authenticate
        try:
            self.connect()
        except Exception as e:
            return {
                "success": False,
                "message": f"Authentication failed: {e}",
            }

        # Phase 2: List data models
        try:
            datamodels = self._list_datamodels()
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to list data models: {e}",
            }

        if not datamodels:
            return {
                "success": False,
                "message": "Connected but no data models found. Ensure the user has access to at least one ElastiCube or live model.",
            }

        # Phase 3: Try a test query on the first ElastiCube
        first_model = datamodels[0]
        first_title = first_model.get("title") or first_model.get("oid", "")
        try:
            test_sql = "SELECT 1 AS test"
            self._execute_sql_internal(first_title, test_sql, max_rows=1)
        except Exception as e:
            return {
                "success": False,
                "message": f"Connected but cannot query data model '{first_title}': {e}",
                "connectivity": True,
            }

        return {
            "success": True,
            "message": f"Connected to Sisense. Found {len(datamodels)} data model(s).",
            "datamodels": len(datamodels),
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _list_datamodels(self) -> List[Dict]:
        """List all data models the authenticated user has access to."""
        self.connect()
        headers = self._build_headers()

        # Try v2 API first (Linux L8.1+)
        url = f"{self.host}/api/v2/datamodels"
        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code < 300:
            return resp.json() or []

        # Fallback to v1 ElastiCubes API
        url = f"{self.host}/api/v1/elasticubes/getElasticubes"
        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(f"Failed to list data models: HTTP {resp.status_code} {resp.text}")

        return resp.json() or []

    def _get_datamodel_fields(self, datasource_title: str) -> Dict:
        """Get schema for a data model via the /api/datasources/{title}/fields endpoint."""
        self.connect()
        headers = self._build_headers()

        url = f"{self.host}/api/datasources/{requests.utils.quote(datasource_title, safe='')}/fields"
        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(f"Failed to get fields: HTTP {resp.status_code} {resp.text}")

        fields = resp.json() or []

        # Group fields by table name
        tables_map: Dict[str, List[Dict]] = {}
        for field in fields:
            tbl = field.get("table", "")
            if not tbl:
                continue
            tables_map.setdefault(tbl, []).append({
                "name": field.get("column", field.get("title", "")),
                "type": field.get("dimtype", "unknown"),
            })

        tables = []
        for tbl_name, columns in tables_map.items():
            tables.append({"name": tbl_name, "columns": columns})

        return {"tables": tables}

    def _list_dashboards(self) -> List[Dict]:
        """List all dashboards the authenticated user has access to."""
        self.connect()
        headers = self._build_headers()

        url = f"{self.host}/api/v1/dashboards"
        resp = self._http.get(url, headers=headers, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(f"Failed to list dashboards: HTTP {resp.status_code} {resp.text}")

        results = []
        for d in resp.json() or []:
            results.append({
                "id": d.get("oid"),
                "title": d.get("title"),
                "datasource": d.get("datasource"),
                "owner": d.get("owner"),
            })
        return results

    # ------------------------------------------------------------------
    # Schema building
    # ------------------------------------------------------------------

    def get_schemas(self) -> List[Table]:
        """
        Build Table objects for all tables across all data models.
        Each table is named "{DatamodelTitle}/{TableName}".
        """
        datamodels = self._list_datamodels()
        tables: List[Table] = []

        # Build dashboard lookup by datasource title
        dashboards_by_model: Dict[str, List[Dict]] = {}
        try:
            dashboards = self._list_dashboards()
            for d in dashboards:
                ds = d.get("datasource")
                if isinstance(ds, dict):
                    ds_title = ds.get("title", "")
                elif isinstance(ds, str):
                    ds_title = ds
                else:
                    continue
                if ds_title:
                    dashboards_by_model.setdefault(ds_title, []).append(d)
        except Exception:
            pass

        for model in datamodels:
            model_id = model.get("oid") or model.get("_id") or ""
            model_title = model.get("title") or model_id

            # Get full schema for this model
            try:
                schema = self._get_datamodel_fields(model_title)
            except Exception as e:
                logging.warning(f"Failed to get schema for model '{model_title}': {e}")
                continue

            # Extract tables from nested datasets structure
            model_tables = self._extract_tables_from_schema(schema)

            # Extract relationships
            model_relations = self._extract_relations_from_schema(schema)

            for tbl in model_tables:
                tbl_name = tbl.get("name", "")
                if not tbl_name:
                    continue

                full_name = f"{model_title}/{tbl_name}"

                # Columns
                columns: List[TableColumn] = []
                for col in tbl.get("columns", []):
                    col_name = col.get("name", "")
                    col_type = _col_type_name(col.get("type"))
                    if col_name:
                        columns.append(TableColumn(
                            name=col_name,
                            dtype=col_type,
                            description=None,
                        ))

                # Foreign keys from relationships
                fks: List[ForeignKey] = []
                for rel in model_relations:
                    if rel.get("fromTable") == tbl_name:
                        to_table = rel.get("toTable", "")
                        fks.append(ForeignKey(
                            column=TableColumn(
                                name=rel.get("fromColumn", ""),
                                dtype="unknown",
                            ),
                            references_name=f"{model_title}/{to_table}",
                            references_column=TableColumn(
                                name=rel.get("toColumn", ""),
                                dtype="unknown",
                            ),
                        ))

                metadata_json = {
                    "sisense": {
                        "datamodelId": model_id,
                        "datamodelTitle": model_title,
                        "tableName": tbl_name,
                        "dashboards": dashboards_by_model.get(model_title, []),
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

        return tables

    def _extract_tables_from_schema(self, schema: Dict) -> List[Dict]:
        """Extract flat list of tables with columns from a datamodel schema response."""
        tables = []

        # v2 API: datasets > schema > tables
        for dataset in schema.get("datasets", []):
            ds_schema = dataset.get("schema") or dataset
            for tbl in ds_schema.get("tables", []):
                tables.append({
                    "name": tbl.get("name", ""),
                    "columns": tbl.get("columns", []),
                })

        # v1 fallback: tables may be at top level
        if not tables:
            for tbl in schema.get("tables", []):
                tables.append({
                    "name": tbl.get("name", ""),
                    "columns": tbl.get("columns", []),
                })

        return tables

    def _extract_relations_from_schema(self, schema: Dict) -> List[Dict]:
        """Extract relationships from a datamodel schema response."""
        relations = []
        for rel in schema.get("relations", []):
            columns = rel.get("columns", [])
            if not columns:
                continue

            for col_pair in columns:
                from_tbl = col_pair.get("name", ["", ""])
                # Relations format: each column pair has references to table/column
                # The structure varies; handle both formats
                pass

            # Simpler format: direct fromTable/toTable
            from_table = rel.get("fromTable", "")
            to_table = rel.get("toTable", "")
            from_col = rel.get("fromColumn", "")
            to_col = rel.get("toColumn", "")

            if from_table and to_table and from_col and to_col:
                relations.append({
                    "fromTable": from_table,
                    "fromColumn": from_col,
                    "toTable": to_table,
                    "toColumn": to_col,
                })
                continue

            # v2 format: relations have nested column references
            cols = rel.get("columns", [])
            if len(cols) >= 2:
                relations.append({
                    "fromTable": cols[0].get("table", ""),
                    "fromColumn": cols[0].get("column", ""),
                    "toTable": cols[1].get("table", ""),
                    "toColumn": cols[1].get("column", ""),
                })

        return relations

    def get_schema(self, table_name: str) -> Table:
        """
        Get schema for a single table by name.

        Accepts:
          - "Datamodel/Table" name path (exact match)
          - Internal table name only (first match)
          - Datamodel ID (returns first table in that model)
        """
        all_tables = self.get_schemas()

        # Exact name match
        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl

        # By internal table name
        for tbl in all_tables:
            metadata = tbl.metadata_json or {}
            sis = metadata.get("sisense") or {}
            if sis.get("tableName") == table_name:
                return tbl

        # By datamodel ID
        for tbl in all_tables:
            metadata = tbl.metadata_json or {}
            sis = metadata.get("sisense") or {}
            if sis.get("datamodelId") == table_name:
                return tbl

        raise RuntimeError(f"Table not found for '{table_name}'")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: str,
        table_name: Optional[str] = None,
        datasource_title: Optional[str] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Execute a SQL query against a Sisense ElastiCube.

        Args:
            query: SQL query string
            table_name: Table name (e.g., "SalesModel/Customers") - used to resolve datasource
            datasource_title: ElastiCube/datasource title (alternative to table_name)
            max_rows: Maximum rows to return
        """
        if not query:
            raise ValueError("SQL query is required")

        # Resolve datasource title from table_name if needed
        if table_name and not datasource_title:
            try:
                table = self.get_schema(table_name)
                sis = (table.metadata_json or {}).get("sisense") or {}
                datasource_title = sis.get("datamodelTitle")
            except Exception:
                pass

        if not datasource_title:
            raise ValueError("datasource_title is required (pass table_name or datasource_title)")

        return self._execute_sql_internal(datasource_title, query, max_rows=max_rows)

    def _execute_sql_internal(
        self,
        datasource_title: str,
        sql: str,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """Execute SQL query against an ElastiCube."""
        self.connect()
        headers = self._build_headers()

        url = f"{self.host}/api/datasources/{requests.utils.quote(datasource_title, safe='')}/sql"
        params = {"query": sql}
        if max_rows:
            params["count"] = str(max_rows)

        resp = self._http.get(url, headers=headers, params=params, timeout=120)
        if resp.status_code >= 300:
            raise RuntimeError(f"SQL query failed: HTTP {resp.status_code} {resp.text}")

        payload = resp.json() or {}
        headers_list = payload.get("headers", [])
        values = payload.get("values", [])

        if not headers_list or not values:
            return pd.DataFrame()

        df = pd.DataFrame(values, columns=headers_list)

        if max_rows is not None and max_rows > 0 and len(df) > max_rows:
            df = df.head(max_rows)

        return df

    def execute_jaql(
        self,
        metadata: List[Dict],
        datasource_title: str,
        count: int = 20000,
        offset: int = 0,
    ) -> pd.DataFrame:
        """
        Execute a JAQL query against a Sisense datasource.

        Args:
            metadata: List of JAQL metadata objects describing dimensions/measures
            datasource_title: ElastiCube/datasource title
            count: Max rows to return
            offset: Row offset for pagination
        """
        self.connect()
        headers = self._build_headers()

        url = f"{self.host}/api/datasources/{requests.utils.quote(datasource_title, safe='')}/jaql"
        body = {
            "datasource": {"title": datasource_title},
            "metadata": metadata,
            "count": count,
            "offset": offset,
            "format": "json",
        }

        resp = self._http.post(url, json=body, headers=headers, timeout=120)
        if resp.status_code >= 300:
            raise RuntimeError(f"JAQL query failed: HTTP {resp.status_code} {resp.text}")

        payload = resp.json() or {}
        col_headers = payload.get("headers", [])
        values = payload.get("values", [])

        if not col_headers or not values:
            return pd.DataFrame()

        # JAQL values are nested: [[{data, text}, ...], ...]
        rows = []
        for row in values:
            parsed_row = []
            for cell in row:
                if isinstance(cell, dict):
                    parsed_row.append(cell.get("data"))
                else:
                    parsed_row.append(cell)
            rows.append(parsed_row)

        return pd.DataFrame(rows, columns=col_headers)

    # ------------------------------------------------------------------
    # Prompt / Description
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        """Format schemas for LLM prompt."""
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str

    @property
    def description(self) -> str:
        text = "Sisense Client: discover data models and execute SQL queries against ElastiCubes."
        text += self.system_prompt()
        return text

    def system_prompt(self) -> str:
        return """
## Sisense SQL Query Guide

Execute SQL queries against Sisense ElastiCubes (data models).

### Schema Structure

Each Sisense table is exposed as a separate schema table named `Datamodel/Table`:
- `SalesModel/Customers` - Customers table in SalesModel
- `SalesModel/Orders` - Orders table in SalesModel

Tables in the same data model share the same `metadata.sisense.datamodelId` and can be joined.

### Table Name vs SQL Table Name

- **Schema table name** (e.g., `SalesModel/Customers`) - Pass as second argument to `execute_query()`
- **SQL table name** - The part after `/` (e.g., `Customers`) - Use inside SQL queries

The SQL table name is also available in `metadata.sisense.tableName`.

### How to Execute Queries

**Signature**: `execute_query(sql_query, table_name)` - BOTH arguments are REQUIRED!

```python
# Schema table name as 2nd arg, SQL table name in query
df = db_clients['sisense'].execute_query(
    "SELECT * FROM Customers",        # SQL uses the table name (after /)
    "SalesModel/Customers"            # Schema table name (REQUIRED)
)
```

### SQL Query Examples

```sql
-- Get all rows (quote table name if it has spaces)
SELECT * FROM Customers
SELECT * FROM "Order Details"

-- Aggregate with grouping
SELECT Category, SUM(Amount) AS Total
FROM Orders
GROUP BY Category

-- Filter data
SELECT * FROM Customers
WHERE Status = 'Active'

-- Top N results
SELECT Name, SUM(Value) AS Total
FROM Orders
JOIN Customers ON Orders.CustomerID = Customers.ID
GROUP BY Name
ORDER BY Total DESC
LIMIT 10
```

### Key SQL Rules
- Table names with spaces MUST use square brackets: [Order Details]
- Standard SQL syntax (SELECT, FROM, WHERE, GROUP BY, ORDER BY, JOIN)
- Use LIMIT N for row limiting (TOP N is NOT supported)
- Use JOIN to combine tables within the same data model
- Row-level security is NOT applied via SQL endpoint - use JAQL if security is needed
"""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> Dict[str, str]:
        if not self._access_token:
            raise RuntimeError("Not authenticated")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }


# Compatibility alias for dynamic resolver
SisenseClient = SisenseClient
