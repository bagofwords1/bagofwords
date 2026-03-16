from app.data_sources.clients.base import DataSourceClient
from app.ai.prompt_formatters import Table, TableColumn, ForeignKey, TableFormatter
from typing import List, Dict, Optional, Set
import json
import requests
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Internal Timbr columns to exclude from schema display
_EXCLUDED_COLUMNS = {"entity_id", "entity_type", "entity_label"}

# Concept schemas ranked from richest to simplest
_CONCEPT_SCHEMAS_RANKED = ["dtimbr", "etimbr", "timbr"]


class TimbrClient(DataSourceClient):
    """
    Timbr semantic layer client.

    Discovers ontology concepts and views via the Timbr REST API and executes
    SQL queries against the Timbr query endpoint.

    Schema discovery is permission-aware:
    - Probes ``timbr.sys_permissions`` to find accessible schemas.
    - For concepts: picks the richest accessible schema (dtimbr > etimbr > timbr).
    - For views: discovers from ``timbr.sys_views`` if vtimbr is accessible.
    - Merges both into a single table list, each tagged with its schema prefix.
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
        self._accessible_schemas: Optional[Set[str]] = None
        self._concept_schema: Optional[str] = None

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
    # Permission discovery
    # ------------------------------------------------------------------

    def _get_accessible_schemas(self) -> Set[str]:
        """Query sys_permissions to find which schemas the user can query."""
        if self._accessible_schemas is not None:
            return self._accessible_schemas

        schemas: Set[str] = set()
        rows = self._query_internal(
            "SELECT permission, resource FROM timbr.sys_permissions"
        )
        for row in rows:
            perm = row.get("permission", "")
            resource = row.get("resource", "")
            if perm != "QUERY":
                continue
            # Resource format: `ontology`.`name`.`schema`.`schema_name`
            if ".`schema`." in resource:
                schema_name = resource.rsplit("`.`", 1)[-1].rstrip("`")
                if schema_name:
                    schemas.add(schema_name)

        self._accessible_schemas = schemas
        logger.info(f"Timbr accessible schemas: {schemas}")
        return schemas

    def _get_concept_schema(self) -> Optional[str]:
        """Pick the richest accessible concept schema, or None.

        Falls back to probing each schema directly when sys_permissions
        does not contain schema-level grants.
        """
        if self._concept_schema is not None:
            return self._concept_schema
        schemas = self._get_accessible_schemas()
        for s in _CONCEPT_SCHEMAS_RANKED:
            if s in schemas:
                self._concept_schema = s
                logger.info(f"Timbr concept schema selected: {s}")
                return s

        # Fallback: permissions didn't list schemas explicitly —
        # probe each schema with a lightweight query.
        logger.info("No schema-level permissions found, probing schemas directly")
        for s in _CONCEPT_SCHEMAS_RANKED:
            rows = self._query_internal(
                f"SELECT 1 FROM `{s}`.`thing` LIMIT 1"
            )
            if rows:
                self._concept_schema = s
                logger.info(f"Timbr concept schema discovered by probing: {s}")
                return s

        return None

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
        Permission-aware discovery of concepts and views.

        1. Probe ``sys_permissions`` to find accessible schemas.
        2. If a concept schema is accessible (dtimbr/etimbr/timbr), discover
           concepts via ``sys_concepts`` + DESCRIBE.
        3. If vtimbr is accessible, discover views via ``sys_views``.
        4. Merge into a single list, each tagged with its schema prefix.
        """
        accessible = self._get_accessible_schemas()
        tables: List[Table] = []

        # --- Concepts (dtimbr / etimbr / timbr) ---
        concept_schema = self._get_concept_schema()
        if concept_schema:
            concepts = self._get_concepts()
            for name, desc in concepts.items():
                try:
                    table = self._describe_concept(name, desc, concept_schema)
                    if table is not None:
                        tables.append(table)
                except Exception as e:
                    logger.warning(f"Failed to describe concept '{name}': {e}")
                    tables.append(Table(
                        name=f"{concept_schema}.{name}",
                        description=desc,
                        columns=[],
                        pks=[],
                        fks=[],
                        metadata_json={"timbr": {
                            "ontology": self.ontology,
                            "schema": concept_schema,
                            "type": "concept",
                        }},
                    ))

        # --- Views (vtimbr) ---
        has_vtimbr = "vtimbr" in accessible
        if not has_vtimbr:
            # Probe vtimbr if permissions didn't list it
            has_vtimbr = bool(self._query_internal(
                "SELECT view_name FROM timbr.sys_views LIMIT 1"
            ))
        if has_vtimbr:
            views = self._discover_views()
            tables.extend(views)

        if not tables:
            logger.warning("Timbr: no accessible concepts or views found")

        return tables

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        raise RuntimeError(
            f"'{table_name}' not found in ontology '{self.ontology}'"
        )

    # ------------------------------------------------------------------
    # Concept discovery (timbr / etimbr / dtimbr)
    # ------------------------------------------------------------------

    def _get_concepts(self) -> Dict[str, Optional[str]]:
        """Fetch concept names and descriptions from system tables."""
        concepts: Dict[str, Optional[str]] = {}
        rows = self._query_internal(
            "SELECT concept, description FROM timbr.sys_concepts"
        )
        for row in rows:
            name = row.get("concept", "")
            if name and name != "thing":
                concepts[name] = row.get("description") or None
        return concepts

    def _describe_concept(
        self, concept_name: str, description: Optional[str], schema: str
    ) -> Table:
        """
        Run ``DESCRIBE concept `<schema>`.`<name>``` and parse the result.

        The DESCRIBE output classifies each row as:
        - **Regular column**: no special prefix
        - **Measure**: name starts with ``measure.``
        - **Relationship**: name contains ``[`` bracket notation
        """
        rows = self._query_internal(
            f"DESCRIBE concept `{schema}`.`{concept_name}`"
        )

        qualified_name = f"{schema}.{concept_name}"

        if not rows:
            return Table(
                name=qualified_name,
                description=description,
                columns=[],
                pks=[],
                fks=[],
                metadata_json={"timbr": {
                    "ontology": self.ontology,
                    "schema": schema,
                    "type": "concept",
                }},
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
                measures.append(TableColumn(
                    name=col_name,
                    dtype=col_type,
                    description=col_comment,
                    metadata={"role": "measure"},
                ))

            # ---- Relationship (only present in dtimbr) ----
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
            name=qualified_name,
            description=description,
            columns=all_columns if all_columns else [],
            pks=[],
            fks=fks,
            is_active=True,
            metadata_json={
                "timbr": {
                    "ontology": self.ontology,
                    "schema": schema,
                    "type": "concept",
                    "measure_count": len(measures),
                    "relationship_count": len(fks),
                }
            },
        )

    # ------------------------------------------------------------------
    # View discovery (vtimbr)
    # ------------------------------------------------------------------

    def _discover_views(self) -> List[Table]:
        """Discover views from sys_views and parse view_json for columns."""
        rows = self._query_internal(
            "SELECT view_name, description, view_json, view_properties "
            "FROM timbr.sys_views"
        )
        tables: List[Table] = []
        for row in rows:
            name = row.get("view_name", "")
            if not name:
                continue
            try:
                table = self._parse_view(row)
                tables.append(table)
            except Exception as e:
                logger.warning(f"Failed to parse view '{name}': {e}")
                tables.append(Table(
                    name=f"vtimbr.{name}",
                    description=row.get("description"),
                    columns=[],
                    pks=[],
                    fks=[],
                    metadata_json={"timbr": {
                        "ontology": self.ontology,
                        "schema": "vtimbr",
                        "type": "view",
                    }},
                ))
        return tables

    def _parse_view(self, row: dict) -> Table:
        """Parse a sys_views row into a Table using view_json."""
        name = row["view_name"]
        description = row.get("description")
        view_json_str = row.get("view_json", "")

        columns: List[TableColumn] = []
        measures: List[TableColumn] = []

        if view_json_str:
            try:
                vj = json.loads(view_json_str) if isinstance(view_json_str, str) else view_json_str
            except (json.JSONDecodeError, TypeError):
                vj = {}

            # Parse columns from view_json
            for col in vj.get("columns", []):
                alias = col.get("alias", "").strip("`")
                dtype = col.get("type", "string")
                if alias:
                    columns.append(TableColumn(name=alias, dtype=dtype))

            # Parse measures from view_json
            for m in vj.get("measures", []):
                alias = m.get("alias", "").strip("`")
                dtype = m.get("type", "string")
                if alias:
                    measures.append(TableColumn(
                        name=alias,
                        dtype=dtype,
                        metadata={"role": "measure"},
                    ))
        else:
            # Fallback: parse view_properties string "col1 type1,col2 type2,..."
            props_str = row.get("view_properties", "")
            if props_str:
                for prop in props_str.split(","):
                    parts = prop.strip().rsplit(" ", 1)
                    if len(parts) == 2:
                        pname, ptype = parts
                        if pname.startswith("measure."):
                            measures.append(TableColumn(
                                name=pname,
                                dtype=ptype,
                                metadata={"role": "measure"},
                            ))
                        else:
                            columns.append(TableColumn(name=pname, dtype=ptype))

        all_columns = columns + measures

        return Table(
            name=f"vtimbr.{name}",
            description=description,
            columns=all_columns,
            pks=[],
            fks=[],
            is_active=True,
            metadata_json={
                "timbr": {
                    "ontology": self.ontology,
                    "schema": "vtimbr",
                    "type": "view",
                    "measure_count": len(measures),
                }
            },
        )

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(self, query: str) -> pd.DataFrame:
        """
        Execute a SQL query against the Timbr ontology.

        The query should use the correct schema prefix for each table
        (e.g. ``dtimbr`` for concepts, ``vtimbr`` for views).
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
        accessible = self._get_accessible_schemas()
        concept_schema = self._get_concept_schema()
        has_views = "vtimbr" in accessible

        sections = [f"""## Timbr Semantic Layer Query Guide

Query the Timbr ontology-based semantic layer using SQL.
Ontology: `{self.ontology}`

### Important Rules

1. ALWAYS use backticks around schema and table names: `` `schema`.`TableName` ``
2. Table names are case-sensitive
3. ALWAYS include a LIMIT clause to avoid returning too many rows
4. Standard SQL (GROUP BY, ORDER BY, HAVING, WHERE) works as expected
5. Each table in the schema specifies which schema prefix to use — check the table metadata"""]

        # --- Concept schema section ---
        if concept_schema:
            sections.append(f"""
### Querying Concepts (schema: `{concept_schema}`)

Concepts are ontology entities. Query them with the `{concept_schema}` schema prefix:

```python
df = timbr_client.execute_query("SELECT column1, column2 FROM `{concept_schema}`.`ConceptName` WHERE condition LIMIT 100")
```""")

            if concept_schema == "dtimbr":
                sections.append("""
### Traversing Relationships (dtimbr only)

Relationships are listed as foreign keys in the schema. Use bracket syntax
to access related concept properties without explicit JOINs:

```python
# Single-hop
df = timbr_client.execute_query("SELECT name, `has_orders[Order].order_date` AS order_date FROM `dtimbr`.`Customer` LIMIT 100")

# Multi-hop
df = timbr_client.execute_query("SELECT name, `has_orders[Order].includes_product[Product].product_name` AS product FROM `dtimbr`.`Customer` LIMIT 100")
```""")

        # --- View schema section ---
        if has_views:
            sections.append(f"""
### Querying Views (schema: `vtimbr`)

Views are pre-built flat projections with columns and measures. Query them
with the `vtimbr` schema prefix:

```python
df = timbr_client.execute_query("SELECT column1, column2 FROM `vtimbr`.`ViewName` WHERE condition LIMIT 100")
```""")

        # --- Measures section ---
        if concept_schema or has_views:
            sections.append("""
### Using Measures

Measures are pre-defined aggregations (shown in schema with role=measure).
Reference them using their ``measure.`` prefixed name with standard SQL aggregates:

```python
df = timbr_client.execute_query("SELECT category, SUM(`measure.total_sales`) AS total_sales FROM `schema`.`TableName` GROUP BY category ORDER BY total_sales DESC LIMIT 100")
```""")

        return "\n".join(sections)
