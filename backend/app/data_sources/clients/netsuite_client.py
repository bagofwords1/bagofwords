from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter

import pandas as pd
import requests
from requests_oauthlib import OAuth1
from typing import List, Optional
from contextlib import contextmanager


class NetsuiteClient(DataSourceClient):
    def __init__(
        self,
        account_id: str,
        consumer_key: str,
        consumer_secret: str,
        token_id: str,
        token_secret: str,
        table_filter: Optional[str] = None,
    ):
        self.account_id = account_id
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.token_id = token_id
        self.token_secret = token_secret
        self.table_filter = table_filter

    @property
    def _base_url(self) -> str:
        normalized = self.account_id.lower().replace("_", "-")
        return f"https://{normalized}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"

    @property
    def _realm(self) -> str:
        return self.account_id.upper()

    @contextmanager
    def connect(self):
        auth = OAuth1(
            client_key=self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=self.token_id,
            resource_owner_secret=self.token_secret,
            realm=self._realm,
            signature_method="HMAC-SHA256",
        )
        session = requests.Session()
        session.auth = auth
        session.headers.update({
            "Content-Type": "application/json",
            "Prefer": "transient",
        })
        try:
            yield session
        finally:
            session.close()

    @staticmethod
    def _get_field(row: dict, camel_key: str, default=None):
        """Get a field from a NetSuite response row, handling inconsistent casing (tableName vs tablename)."""
        return row.get(camel_key) or row.get(camel_key.lower(), default)

    def _execute_suiteql(self, session, query: str, limit: int = 1000, max_rows: int = 100_000) -> list:
        all_items = []
        offset = 0
        while True:
            resp = session.post(
                self._base_url,
                json={"q": query},
                params={"limit": limit, "offset": offset},
                timeout=120,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"SuiteQL error ({resp.status_code}): {resp.text}")
            data = resp.json()
            items = data.get("items", [])
            all_items.extend(items)
            if not data.get("hasMore", False) or len(all_items) >= max_rows:
                break
            offset += limit
        return all_items[:max_rows]

    def test_connection(self):
        try:
            with self.connect() as session:
                items = self._execute_suiteql(session, "SELECT 1 as test FROM DUAL", limit=1, max_rows=1)
                if items:
                    return {"success": True, "message": f"Connected to NetSuite account {self.account_id}"}
                return {"success": False, "message": "Query returned no results"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_schemas(self) -> List[Table]:
        with self.connect() as session:
            # Bulk fetch: 2 queries total instead of 1+N
            raw_tables = self._execute_suiteql(session, "SELECT tableName, description FROM metadata.tables")
            raw_columns = self._execute_suiteql(session, "SELECT tableName, columnName, dataType, description FROM metadata.columns")

            if self.table_filter:
                allowed = {t.strip().lower() for t in self.table_filter.split(",") if t.strip()}
                raw_tables = [t for t in raw_tables if self._get_field(t, "tableName", "").lower() in allowed]

            # Group columns by table name
            columns_by_table = {}
            for c in raw_columns:
                tname = self._get_field(c, "tableName", "").lower()
                columns_by_table.setdefault(tname, []).append(c)

            tables = []
            for tbl in raw_tables:
                table_name = self._get_field(tbl, "tableName")
                if not table_name:
                    continue
                cols_raw = columns_by_table.get(table_name.lower(), [])
                columns = [
                    TableColumn(
                        name=self._get_field(c, "columnName", ""),
                        dtype=self._get_field(c, "dataType", ""),
                        description=c.get("description"),
                    )
                    for c in cols_raw
                ]
                tables.append(Table(
                    name=table_name,
                    description=tbl.get("description"),
                    columns=columns,
                    pks=[],
                    fks=[],
                ))
            return tables

    def get_schema(self, table_name: str) -> Table:
        with self.connect() as session:
            raw_cols = self._execute_suiteql(
                session,
                f"SELECT columnName, dataType, description FROM metadata.columns WHERE tableName = '{table_name}'",
            )
            columns = [
                TableColumn(
                    name=self._get_field(c, "columnName", ""),
                    dtype=self._get_field(c, "dataType", ""),
                    description=c.get("description"),
                )
                for c in raw_cols
            ]
            return Table(name=table_name, columns=columns, pks=[], fks=[])

    def execute_query(self, query: str) -> pd.DataFrame:
        with self.connect() as session:
            items = self._execute_suiteql(session, query)
            if not items:
                return pd.DataFrame()
            return pd.DataFrame(items)

    def prompt_schema(self) -> str:
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def system_prompt(self) -> str:
        return """
## NetSuite SuiteQL Integration

This connector queries NetSuite data using SuiteQL, a SQL-like query language.
Use `execute_query` to run SuiteQL queries that return pandas DataFrames.

### SuiteQL Syntax Notes
- Based on Oracle SQL syntax with NetSuite-specific extensions
- Supports: SELECT, FROM, WHERE, JOIN, GROUP BY, ORDER BY, HAVING
- Does NOT support: UNION, subqueries in FROM clause, CREATE/INSERT/UPDATE/DELETE (read-only)
- String literals use single quotes: WHERE name = 'Acme Corp'
- Date literals: TO_DATE('2024-01-01', 'YYYY-MM-DD')
- Use BUILTIN.DF() for date formatting: BUILTIN.DF(trandate, 'YYYY-MM-DD')
- NVL() for null handling (like COALESCE): NVL(email, 'N/A')
- Table and column names are case-insensitive
- JOIN syntax: INNER JOIN, LEFT OUTER JOIN (not LEFT JOIN)
- Use FETCH FIRST N ROWS ONLY instead of LIMIT N
- Pagination: OFFSET M ROWS FETCH NEXT N ROWS ONLY

### Common Tables
- transaction: Sales orders, invoices, payments, purchase orders, etc.
- transactionline: Line items for transactions
- customer: Customer records
- vendor: Vendor records
- item: Inventory and service items
- employee: Employee records
- account: Chart of accounts
- department, location, subsidiary: Classification dimensions
- accountingperiod: Fiscal periods
- customrecord_*: Custom record types (vary by account)

### Example Queries
```python
# Revenue by month
df = client.execute_query(\"\"\"
    SELECT BUILTIN.DF(t.trandate, 'YYYY-MM') AS month,
           SUM(tl.netamount) AS revenue
    FROM transaction t
    INNER JOIN transactionline tl ON t.id = tl.transaction
    WHERE t.type = 'SalesOrd'
    GROUP BY BUILTIN.DF(t.trandate, 'YYYY-MM')
    ORDER BY month
\"\"\")

# Top customers by order count
df = client.execute_query(\"\"\"
    SELECT c.companyname, COUNT(t.id) AS order_count
    FROM transaction t
    INNER JOIN customer c ON t.entity = c.id
    WHERE t.type = 'SalesOrd'
    GROUP BY c.companyname
    ORDER BY order_count DESC
    FETCH FIRST 20 ROWS ONLY
\"\"\")
```

### Important
- ALWAYS include FETCH FIRST N ROWS ONLY to limit results
- Use LEFT OUTER JOIN (not LEFT JOIN)
- Results are automatically paginated; no need to handle pagination manually
"""

    @property
    def description(self) -> str:
        text = f"NetSuite SuiteQL client for account '{self.account_id}'. Query NetSuite ERP data using SuiteQL (SQL-like syntax)."
        return text + "\n\n" + self.system_prompt()
