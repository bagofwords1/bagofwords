from typing import Dict, List, Optional
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape as xml_escape

import pandas as pd

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.xmla_base import XMLA_NS, XmlaClient


class AnalysisServicesClient(XmlaClient):
    """
    Microsoft SQL Server Analysis Services (SSAS) client.

    Connects to the SSAS XMLA endpoint (typically the IIS msmdpump.dll pump,
    e.g. ``https://server/olap/msmdpump.dll``) over HTTP with Basic auth and
    supports both SSAS model types:

      - Multidimensional models — queried with MDX.
      - Tabular models — queried with DAX (native) or MDX.

    All XMLA transport/discovery lives in ``XmlaClient``. This subclass adds
    per-catalog model-type detection (so the agent uses a supported dialect)
    and a guard that rejects DAX against a Multidimensional cube.
    """

    META_KEY = "analysis_services"
    PRODUCT_NAME = "Microsoft Analysis Services"
    EMPTY_NOTE = "No databases visible to this user — check permissions."
    QUERY_REQUIRED_MSG = "An MDX or DAX query is required"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._schemas_cache: Optional[List[Table]] = None
        self._table_metadata_map: Dict[str, Dict] = {}

    def attach_table_metadata(self, tables: List[Dict]) -> None:
        """Reuse indexed model metadata during query execution.

        This mirrors the Power BI client's query-time lookup: the selected
        ``Catalog/Table`` already carries the model type and catalog, so a
        generated query should not trigger a complete live metadata crawl.
        """
        mapping: Dict[str, Dict] = {}
        for table in tables or []:
            name = str(table.get("name") or "").strip()
            metadata = table.get("metadata_json") or {}
            analysis_services = metadata.get(self.META_KEY) if isinstance(metadata, dict) else None
            if name and isinstance(analysis_services, dict):
                mapping[name] = analysis_services
        self._table_metadata_map = mapping

    # ------------------------------------------------------------------
    # Model-type detection
    # ------------------------------------------------------------------

    def _catalog_context(self, catalog: str) -> Dict:
        """Detect whether a catalog is Tabular or Multidimensional.

        Tabular models (compatibility level 1200+) expose the ``TMSCHEMA_*``
        metadata DMVs; Multidimensional has none. We probe ``TMSCHEMA_MODEL``
        and treat success as Tabular. Any failure falls back to
        Multidimensional, which is always safe because MDX works on both —
        only DAX is Tabular-only.
        """
        try:
            self._execute_statement("SELECT * FROM $SYSTEM.TMSCHEMA_MODEL", catalog)
            return {"modelType": "TABULAR", "supportsDax": True}
        except Exception:
            return {"modelType": "MULTIDIMENSIONAL", "supportsDax": False}

    # ------------------------------------------------------------------
    # Read-only Tabular discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _local_name(element: Element) -> str:
        return element.tag.split("}", 1)[-1]

    @classmethod
    def _children_named(cls, element: Element, name: str) -> List[Element]:
        return [child for child in element if cls._local_name(child) == name]

    @classmethod
    def _first_child_named(cls, element: Element, *names: str) -> Optional[Element]:
        wanted = set(names)
        for child in element:
            if cls._local_name(child) in wanted:
                return child
        return None

    @staticmethod
    def _truthy(value: Optional[str]) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes"}

    @staticmethod
    def _dax_table_name(name: str) -> str:
        return "'" + name.replace("'", "''") + "'"

    def _discover_csdl_schema(self, catalog: str) -> Element:
        """Return the EDM Schema from ``DISCOVER_CSDL_METADATA``.

        Unlike ``TMSCHEMA_*`` DMVs, CSDL discovery is available to ordinary
        database readers. That makes it suitable for BOW's least-privilege
        connection accounts and is the same semantic-model shape Power BI
        exposes: physical tables, columns, measures, and associations.
        """
        properties_xml = self._property_list_xml(catalog, content="SchemaData")
        escaped_catalog = xml_escape(catalog)
        body = (
            f'<Discover xmlns="{XMLA_NS}">'
            "<RequestType>DISCOVER_CSDL_METADATA</RequestType>"
            "<Restrictions><RestrictionList>"
            f"<CATALOG_NAME>{escaped_catalog}</CATALOG_NAME>"
            "</RestrictionList></Restrictions>"
            f"<Properties><PropertyList>{properties_xml}</PropertyList></Properties>"
            "</Discover>"
        )
        root = self._soap_call("Discover", body)
        for element in root.iter():
            if self._local_name(element) == "Schema" and self._children_named(element, "EntityType"):
                return element
        raise RuntimeError(f"Catalog '{catalog}' did not expose Tabular CSDL metadata")

    @staticmethod
    def _entity_type_name(value: Optional[str]) -> str:
        return str(value or "").rsplit(".", 1)[-1]

    @staticmethod
    def _display_name(internal_name: str, annotation: Optional[Element]) -> str:
        if annotation is not None:
            caption = annotation.get("Caption") or annotation.get("ReferenceName")
            if caption:
                return caption
        return internal_name.replace("_", " ")

    @staticmethod
    def _role_column(role: Optional[str], entity_name: str) -> str:
        role = str(role or "")
        prefix = f"{entity_name}_"
        return role[len(prefix):] if role.startswith(prefix) else role

    def _tabular_tables_from_csdl(self, catalog: str, schema: Element) -> List[Table]:
        entity_sets: Dict[str, Dict[str, str]] = {}
        association_states: Dict[str, str] = {}

        for container in self._children_named(schema, "EntityContainer"):
            for entity_set in self._children_named(container, "EntitySet"):
                internal = self._entity_type_name(entity_set.get("EntityType"))
                annotation = self._first_child_named(entity_set, "EntitySet")
                display = self._display_name(entity_set.get("Name") or internal, annotation)
                entity_sets[internal] = {"display": display}
            for association_set in self._children_named(container, "AssociationSet"):
                annotation = self._first_child_named(association_set, "AssociationSet")
                association_states[self._entity_type_name(association_set.get("Association"))] = (
                    annotation.get("State") if annotation is not None else ""
                ) or ""

        tables_by_entity: Dict[str, Table] = {}
        columns_by_entity: Dict[str, Dict[str, TableColumn]] = {}

        for entity in self._children_named(schema, "EntityType"):
            entity_name = entity.get("Name") or ""
            if not entity_name:
                continue
            table_name = (entity_sets.get(entity_name) or {}).get("display") or entity_name.replace("_", " ")
            columns: List[TableColumn] = []
            column_lookup: Dict[str, TableColumn] = {}

            for prop in self._children_named(entity, "Property"):
                internal_name = prop.get("Name") or ""
                if not internal_name:
                    continue
                measure_annotation = self._first_child_named(prop, "Measure")
                property_annotation = self._first_child_named(prop, "Property")
                annotation = measure_annotation if measure_annotation is not None else property_annotation
                # SSAS injects a hidden, engine-owned row-number property in
                # every table. It is not a model field and cannot help queries.
                if property_annotation is not None and (
                    property_annotation.get("Contents") == "RowNumber"
                    or internal_name.startswith("RowNumber_")
                ):
                    continue

                display_name = self._display_name(internal_name, annotation)
                is_measure = measure_annotation is not None
                metadata = {
                    "role": "measure" if is_measure else "column",
                    "unique_name": (
                        f"[{display_name}]"
                        if is_measure
                        else f"{self._dax_table_name(table_name)}[{display_name}]"
                    ),
                }
                if annotation is not None and self._truthy(annotation.get("Hidden")):
                    metadata["hidden"] = True
                if is_measure:
                    metadata["returns"] = prop.get("Type") or "unknown"

                column = TableColumn(
                    name=display_name,
                    dtype="measure" if is_measure else (prop.get("Type") or "unknown"),
                    metadata=metadata,
                )
                columns.append(column)
                column_lookup[internal_name] = column
                column_lookup[display_name] = column

            table = Table(
                name=f"{catalog}/{table_name}",
                columns=columns,
                pks=[],
                fks=[],
                is_active=True,
                metadata_json={
                    self.META_KEY: {
                        "catalog": catalog,
                        "tableName": table_name,
                        "modelType": "TABULAR",
                        "supportsDax": True,
                        "preferredDialect": "DAX",
                    }
                },
            )
            tables_by_entity[entity_name] = table
            columns_by_entity[entity_name] = column_lookup

        # CSDL expresses relationships as associations. Only active many-to-one
        # associations become BOW foreign keys; inactive role-playing date
        # relationships are intentionally omitted so the agent does not assume
        # they filter by default.
        for association in self._children_named(schema, "Association"):
            association_name = association.get("Name") or ""
            if association_states.get(association_name, "").lower() == "inactive":
                continue
            ends = self._children_named(association, "End")
            many = next((end for end in ends if end.get("Multiplicity") == "*"), None)
            one = next((end for end in ends if end is not many), None)
            if many is None or one is None:
                continue
            from_entity = self._entity_type_name(many.get("Type"))
            to_entity = self._entity_type_name(one.get("Type"))
            from_table = tables_by_entity.get(from_entity)
            to_table = tables_by_entity.get(to_entity)
            if from_table is None or to_table is None:
                continue
            from_key = self._role_column(many.get("Role"), from_entity)
            to_key = self._role_column(one.get("Role"), to_entity)
            from_column = (columns_by_entity.get(from_entity) or {}).get(from_key)
            to_column = (columns_by_entity.get(to_entity) or {}).get(to_key)
            if from_column is None or to_column is None:
                continue
            from_column.metadata = {**(from_column.metadata or {}), "relationship_key": True}
            to_column.metadata = {**(to_column.metadata or {}), "relationship_key": True}
            from_table.fks.append(ForeignKey(
                column=TableColumn(name=from_column.name, dtype=from_column.dtype),
                references_name=to_table.name,
                references_column=TableColumn(name=to_column.name, dtype=to_column.dtype),
            ))

        return list(tables_by_entity.values())

    def get_schemas(self) -> List[Table]:
        """Discover physical tables for Tabular and cubes for Multidimensional."""
        if self._schemas_cache is not None:
            return self._schemas_cache

        tables: List[Table] = []
        for catalog in self._list_catalogs():
            try:
                schema = self._discover_csdl_schema(catalog)
            except Exception:
                context = {"modelType": "MULTIDIMENSIONAL", "supportsDax": False, "preferredDialect": "MDX"}
                tables.extend(self._cube_tables_for_catalog(catalog, context))
            else:
                tables.extend(self._tabular_tables_from_csdl(catalog, schema))
        self._schemas_cache = tables
        return tables

    @staticmethod
    def _is_dax(query: str) -> bool:
        """A DAX query statement starts with EVALUATE or DEFINE."""
        head = (query or "").lstrip().upper()
        return head.startswith("EVALUATE") or head.startswith("DEFINE")

    # ------------------------------------------------------------------
    # Query execution (adds the DAX-on-Multidimensional guard)
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: str,
        table_name: Optional[str] = None,
        catalog: Optional[str] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Execute an MDX or DAX statement via XMLA Execute and return a DataFrame.

        Args:
            query: an MDX SELECT or a DAX EVALUATE statement.
            table_name: optional ``Catalog/Cube`` hint. Used to resolve the
                catalog and, for DAX, to verify the target is a Tabular model.
            catalog: explicit catalog override (takes precedence).
            max_rows: optional client-side row cap.
        """
        if not query or not query.strip():
            raise ValueError(self.QUERY_REQUIRED_MSG)
        self.connect()

        table = None
        table_meta = self._table_metadata_map.get(table_name or "")
        if table_name:
            if table_meta is None:
                try:
                    table = self.get_schema(table_name)
                    table_meta = (table.metadata_json or {}).get(self.META_KEY) or {}
                except Exception:
                    table = None

        # Guard: DAX only runs on Tabular models. When we know the target's
        # model type and it is Multidimensional, reject DAX with a clear error
        # instead of surfacing a cryptic server fault.
        if self._is_dax(query) and table_meta is not None:
            if not table_meta.get("supportsDax"):
                raise RuntimeError(
                    "DAX queries are only supported on Tabular models; this "
                    "target is Multidimensional — use MDX instead."
                )

        target_catalog = catalog or self.catalog
        if not target_catalog and table_meta is not None:
            target_catalog = table_meta.get("catalog")
        if not target_catalog and table_name and "/" in table_name:
            target_catalog = table_name.split("/", 1)[0]

        rows = self._execute_statement(query, target_catalog)
        return self._rows_to_df(rows, max_rows)

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    @property
    def description(self) -> str:
        return (
            "Microsoft Analysis Services Client: discover cubes/models (XMLA "
            "Discover) and execute MDX or DAX against SSAS (XMLA Execute). "
            "Supports both Multidimensional (MDX) and Tabular (DAX/MDX) models."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Microsoft Analysis Services (SSAS) Query Guide

Execute queries against SSAS cubes and models over XMLA. SSAS has two model
types and they accept different query languages:

- **Multidimensional** models accept **MDX only**.
- **Tabular** models should be queried with **DAX**.

### Schema Structure

Schema names depend on the model type:
- Tabular: one physical model table per `Catalog/Table`.
- Multidimensional: one cube per `Catalog/Cube`.

Every table records its model type in
`metadata.analysis_services.modelType` (`MULTIDIMENSIONAL` or `TABULAR`) and
`metadata.analysis_services.supportsDax` and
`metadata.analysis_services.preferredDialect`. **Pick the language from this
metadata**: write MDX only for Multidimensional and DAX for Tabular.

For Tabular, each schema entry contains that table's physical columns and
measures. For Multidimensional, columns are dimension hierarchies and measures.
The exact query identifier is always in `metadata.unique_name`.

### How to Execute Queries

**Signature**: `execute_query(query, table_name)` — pass the exact selected
`Catalog/Table` or `Catalog/Cube` schema name as the second argument.

```python
# DAX for a selected Tabular schema table
df = db_clients['analysis_services'].execute_query(
    '''
    EVALUATE
    TOPN(100, 'Physical Table')
    ''',
    "Catalog/Physical Table"
)
```

### Rules

- Never copy the placeholders in the example. Replace `Catalog/Physical Table`
  and `'Physical Table'` with the exact selected schema names.
- **MDX**: use it only when `preferredDialect` is `MDX`. `FROM` must name the
  exact cube from the selected `Catalog/Cube`; never invent or abbreviate it.
- **DAX**: start with `EVALUATE`; use `SUMMARIZECOLUMNS`, `FILTER`,
  `CALCULATE`, `TOPN`; reference columns as `Table[Column]` and measures as
  `[Measure]`. Use the exact physical table and column names from the schema.
- Never send DAX to a Multidimensional model — it is not supported there.
"""
