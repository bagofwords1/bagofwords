from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, TableFormatter
from typing import List, Dict, Optional
import requests
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Internal Timbr columns to exclude from schema display
_EXCLUDED_COLUMNS = {"entity_id", "entity_type", "entity_label"}

# Default schema prefix (supports relationships + measures)
_DEFAULT_SCHEMA = "dtimbr"


class TimbrClient(DataSourceClient):
    """
    Timbr semantic layer client.

    Discovers ontology concepts via the Timbr REST API and executes SQL
    queries against the Timbr query endpoint.  Concepts are exposed as
    tables; properties as columns; measures and relationships are captured
    in column metadata / foreign keys.
    """

    def __init__(
        self,
        host: str,
        ontology: str,
        api_key: str,
        verify_ssl: bool = True,
    ):
        self.host = host.rstrip("/")
        self.ontology = ontology
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self._base_url = f"{self.host}/timbr/api"
        self._session: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
            })
            self._session.verify = self.verify_ssl
        return self._session

    def connect(self):
        """No-op – session is created lazily on first request."""
        pass

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _api_get(self, path: str, timeout: int = 30) -> dict:
        session = self._get_session()
        url = f"{self._base_url}/{path.lstrip('/')}"
        resp = session.get(url, timeout=timeout)
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Timbr API error: HTTP {resp.status_code} {resp.text}"
            )
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Timbr API returned non-success: {data}")
        return data

    def _api_post(self, path: str, payload: dict, timeout: int = 120) -> dict:
        session = self._get_session()
        url = f"{self._base_url}/{path.lstrip('/')}"
        resp = session.post(url, json=payload, timeout=timeout)
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Timbr API error: HTTP {resp.status_code} {resp.text}"
            )
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Timbr API returned non-success: {data}")
        return data

    def _query_internal(self, sql: str) -> List[dict]:
        """Execute a SQL query via the Timbr query API. Returns rows as dicts."""
        try:
            data = self._api_post(
                "query/",
                payload={"query": sql, "ontology_name": self.ontology},
                timeout=60,
            )
            return data.get("data", [])
        except Exception:
            return []

    # ------------------------------------------------------------------
    # DataSourceClient interface
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """
        Validate connectivity in two phases:
        1. GET /get_ontologies/ – checks host + API key
        2. Verify the configured ontology exists in the list
        """
        try:
            data = self._api_get("get_ontologies/")
            ontologies = data.get("data", [])

            if not ontologies:
                return {
                    "success": False,
                    "message": "Connected to Timbr but no ontologies found.",
                }

            if self.ontology not in ontologies:
                return {
                    "success": False,
                    "message": (
                        f"Connected to Timbr but ontology '{self.ontology}' "
                        f"not found. Available: {', '.join(ontologies)}"
                    ),
                }

            return {
                "success": True,
                "message": f"Connected to Timbr. Ontology '{self.ontology}' found.",
            }
        except requests.exceptions.ConnectionError as e:
            return {
                "success": False,
                "message": f"Cannot reach Timbr server at {self.host}: {e}",
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_tables(self) -> List[Table]:
        """
        Discover all concepts and build Table objects.

        1. Query ``timbr.sys_concepts`` for concept names + descriptions.
        2. For each concept run ``DESCRIBE concept `dtimbr`.`<name>``` to get
           properties, measures, and relationships.
        3. Parse DESCRIBE output into Table / TableColumn / ForeignKey objects.
        """
        concepts = self._get_concepts()
        if not concepts:
            return []

        tables: List[Table] = []
        for name, desc in concepts.items():
            try:
                table = self._describe_concept(name, desc)
                if table is not None:
                    tables.append(table)
            except Exception as e:
                logger.warning(f"Failed to describe concept '{name}': {e}")
                tables.append(Table(
                    name=name,
                    description=desc,
                    columns=[],
                    pks=[],
                    fks=[],
                    metadata_json={"timbr": {"ontology": self.ontology}},
                ))
        return tables

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        raise RuntimeError(
            f"Concept '{table_name}' not found in ontology '{self.ontology}'"
        )

    def _get_concepts(self) -> Dict[str, Optional[str]]:
        """Fetch concept names and descriptions from system tables."""
        concepts: Dict[str, Optional[str]] = {}

        rows = self._query_internal(
            "SELECT concept, description FROM timbr.sys_concepts"
        )
        for row in rows:
            name = row.get("concept", "")
            if name:
                concepts[name] = row.get("description") or None

        return concepts

    def _describe_concept(
        self, concept_name: str, description: Optional[str]
    ) -> Table:
        """
        Run ``DESCRIBE concept `dtimbr`.`<name>``` and parse the result.

        The DESCRIBE output classifies each row as:
        - **Regular column**: no special prefix
        - **Measure**: name starts with ``measure.``
        - **Relationship**: name contains ``[`` bracket notation
        """
        rows = self._query_internal(
            f"DESCRIBE concept `{_DEFAULT_SCHEMA}`.`{concept_name}`"
        )

        if not rows:
            return Table(
                name=concept_name,
                description=description,
                columns=[],
                pks=[],
                fks=[],
                metadata_json={"timbr": {"ontology": self.ontology}},
            )

        columns: List[TableColumn] = []
        measures: List[TableColumn] = []
        fks: List[ForeignKey] = []
        relationships_seen: set = set()

        for row in rows:
            col_name = (
                row.get("col_name")
                or row.get("column_name")
                or row.get("name")
                or ""
            )
            col_type = (
                row.get("data_type")
                or row.get("col_type")
                or row.get("type")
                or "string"
            )
            col_comment = row.get("comment") or row.get("description") or None

            if not col_name:
                continue

            # Skip internal Timbr columns
            if col_name.lower() in _EXCLUDED_COLUMNS:
                continue

            # ---- Measure ----
            if col_name.startswith("measure."):
                measure_display = col_name[len("measure."):]
                measures.append(TableColumn(
                    name=measure_display,
                    dtype=col_type,
                    description=col_comment,
                    metadata={"role": "measure", "timbr_name": col_name},
                ))

            # ---- Relationship ----
            elif "[" in col_name and "]" in col_name:
                try:
                    bracket_start = col_name.index("[")
                    bracket_end = col_name.index("]")
                    rel_name = col_name[:bracket_start]
                    target_concept = col_name[bracket_start + 1:bracket_end]

                    if target_concept and target_concept not in relationships_seen:
                        relationships_seen.add(target_concept)
                        fks.append(ForeignKey(
                            column=TableColumn(
                                name=rel_name,
                                dtype="relationship",
                            ),
                            references_name=target_concept,
                            references_column=TableColumn(
                                name="entity_id",
                                dtype="string",
                            ),
                        ))
                except (ValueError, IndexError):
                    columns.append(TableColumn(
                        name=col_name,
                        dtype=col_type,
                        description=col_comment,
                    ))

            # ---- Regular property ----
            else:
                columns.append(TableColumn(
                    name=col_name,
                    dtype=col_type,
                    description=col_comment,
                ))

        all_columns = columns + measures

        return Table(
            name=concept_name,
            description=description,
            columns=all_columns if all_columns else [],
            pks=[],
            fks=fks,
            is_active=True,
            metadata_json={
                "timbr": {
                    "ontology": self.ontology,
                    "schema": _DEFAULT_SCHEMA,
                    "measure_count": len(measures),
                    "relationship_count": len(fks),
                }
            },
        )

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(self, query: str) -> pd.DataFrame:
        """
        Execute a SQL query against the Timbr ontology.

        Args:
            query: SQL string (should use ``dtimbr`` schema prefix).

        Returns:
            pandas DataFrame with query results.
        """
        if not query or not query.strip():
            raise ValueError("SQL query is required")

        data = self._api_post(
            "query/",
            payload={"query": query, "ontology_name": self.ontology},
            timeout=120,
        )
        rows = data.get("data", [])

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # LLM prompt helpers
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    @property
    def description(self) -> str:
        text = f"Timbr semantic layer \u2013 ontology '{self.ontology}' at {self.host}"
        text += "\n\n" + self.system_prompt()
        return text

    def system_prompt(self) -> str:
        return f"""
## Timbr Semantic Layer Query Guide

Query the Timbr ontology-based semantic layer using SQL.
Ontology: `{self.ontology}`

### How to Execute Queries

```python
df = client.execute_query("SELECT * FROM `dtimbr`.`Customer` LIMIT 10")
```

### SQL Syntax

All queries MUST use backtick-quoted schema and concept names:

```sql
SELECT column1, column2
FROM `dtimbr`.`ConceptName`
WHERE condition
LIMIT 100
```

### Schema Prefixes

- **`dtimbr`** (default, recommended) \u2013 supports properties, relationships, and measures
- **`timbr`** \u2013 direct properties only, no relationship traversal

Always use `dtimbr` unless you have a specific reason not to.

### Querying Properties

```sql
-- Simple query
SELECT name, email, age
FROM `dtimbr`.`Customer`
WHERE age > 30
LIMIT 100

-- Aggregation
SELECT category, COUNT(*) AS cnt, SUM(amount) AS total
FROM `dtimbr`.`Order`
GROUP BY category
ORDER BY total DESC
```

### Traversing Relationships

Relationships are listed as foreign keys in the schema. Use bracket syntax
to access related concept properties without explicit JOINs:

```sql
-- Single-hop: relationship_name[TargetConcept].property
SELECT
    name,
    `has_orders[Order].order_date` AS order_date,
    `has_orders[Order].amount` AS amount
FROM `dtimbr`.`Customer`

-- Multi-hop: chain brackets
SELECT
    name,
    `has_orders[Order].includes_product[Product].product_name` AS product
FROM `dtimbr`.`Customer`
```

### Using Measures

Measures are pre-defined aggregations (shown in schema with role=measure).
Use the `AGGREGATE()` function or standard SQL aggregates:

```sql
SELECT
    `of_customer[Customer].customer_segment` AS segment,
    AGGREGATE(`measure.total_revenue`) AS revenue
FROM `dtimbr`.`Order`
GROUP BY segment
ORDER BY revenue DESC
```

### Important Rules

1. ALWAYS use backticks: `dtimbr`.`ConceptName`
2. ALWAYS use the `dtimbr` schema prefix
3. Concept names are case-sensitive
4. ALWAYS include a LIMIT clause to avoid returning too many rows
5. Standard SQL (GROUP BY, ORDER BY, HAVING, WHERE) works as expected
6. Relationship traversal uses bracket syntax: `rel[Target].property`
7. The method takes a single SQL string: `execute_query("YOUR SQL")`
"""
