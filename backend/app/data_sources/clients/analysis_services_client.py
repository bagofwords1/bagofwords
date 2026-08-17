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

    @classmethod
    def _metadata_scalar(cls, value: Optional[str]):
        """Preserve CSDL scalar meaning without leaking XML strings downstream."""
        if value is None or value == "":
            return None
        normalized = str(value).strip()
        lowered = normalized.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        try:
            return int(normalized)
        except ValueError:
            return normalized

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
            key_names = [
                ref.get("Name") or ""
                for key in self._children_named(entity, "Key")
                for ref in self._children_named(key, "PropertyRef")
                if ref.get("Name")
            ]

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

                # CSDL is the least-privilege metadata contract: ordinary model
                # readers receive these semantic and data-shape properties even
                # when SSAS refuses every TMSCHEMA DMV. Keep the names aligned
                # with the Power BI metadata vocabulary where one exists.
                property_attrs = {
                    "nullable": prop.get("Nullable"),
                    "max_length": prop.get("MaxLength"),
                    "unicode": prop.get("Unicode"),
                    "fixed_length": prop.get("FixedLength"),
                    "precision": prop.get("Precision"),
                    "scale": prop.get("Scale"),
                }
                annotation_attrs = {
                    "format_string": annotation.get("FormatString") if annotation is not None else None,
                    "contents": annotation.get("Contents") if annotation is not None else None,
                    "stability": annotation.get("Stability") if annotation is not None else None,
                }
                for key, raw_value in {**property_attrs, **annotation_attrs}.items():
                    value = self._metadata_scalar(raw_value)
                    if value is not None:
                        metadata[key] = value

                column = TableColumn(
                    name=display_name,
                    dtype="measure" if is_measure else (prop.get("Type") or "unknown"),
                    metadata=metadata,
                )
                columns.append(column)
                column_lookup[internal_name] = column
                column_lookup[display_name] = column

            pks = [
                column_lookup[name]
                for name in key_names
                if name in column_lookup
            ]
            entity_annotation = self._first_child_named(entity, "EntityType")
            table_metadata = {
                "catalog": catalog,
                "tableName": table_name,
                "modelType": "TABULAR",
                "supportsDax": True,
                "preferredDialect": "DAX",
                "metadata_source": "CSDL",
            }
            if entity_annotation is not None and entity_annotation.get("Contents"):
                table_metadata["entity_contents"] = entity_annotation.get("Contents")

            table = Table(
                name=f"{catalog}/{table_name}",
                columns=columns,
                pks=pks,
                fks=[],
                is_active=True,
                metadata_json={self.META_KEY: table_metadata},
            )
            tables_by_entity[entity_name] = table
            columns_by_entity[entity_name] = column_lookup

        # CSDL expresses relationships as associations. Preserve every resolved
        # association (name, state, endpoints and multiplicities) as metadata,
        # but only active many-to-one associations become BOW foreign keys.
        # This keeps ordinary joins safe while allowing DAX to opt into an
        # inactive role-playing relationship with USERELATIONSHIP.
        for association in self._children_named(schema, "Association"):
            association_name = association.get("Name") or ""
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
            # Role-playing dimensions suffix the one-side role (Date2/Date3)
            # even though every relationship targets the same physical Date
            # key. When the role is not a real column, the single declared CSDL
            # key is the authoritative endpoint.
            if to_column is None and len(to_table.pks or []) == 1:
                to_column = to_table.pks[0]
            if from_column is None or to_column is None:
                continue
            state = association_states.get(association_name, "").lower() or "active"
            relationship = {
                "name": association_name,
                "state": state,
                "fromTable": from_table.name.split("/", 1)[-1],
                "fromColumn": from_column.name,
                "toTable": to_table.name.split("/", 1)[-1],
                "toColumn": to_column.name,
                "fromMultiplicity": many.get("Multiplicity") or "",
                "toMultiplicity": one.get("Multiplicity") or "",
            }
            source_meta = from_table.metadata_json[self.META_KEY]
            source_meta.setdefault("relationships", []).append(relationship)
            if state == "inactive":
                continue
            from_column.metadata = {**(from_column.metadata or {}), "relationship_key": True}
            to_column.metadata = {**(to_column.metadata or {}), "relationship_key": True}
            from_table.fks.append(ForeignKey(
                column=TableColumn(name=from_column.name, dtype=from_column.dtype),
                references_name=to_table.name,
                references_column=TableColumn(name=to_column.name, dtype=to_column.dtype),
            ))

        return list(tables_by_entity.values())

    # ------------------------------------------------------------------
    # Optional administrator metadata enrichment
    # ------------------------------------------------------------------

    @staticmethod
    def _row_value(row: Dict, *names: str):
        """Read a DMV field independent of XMLA provider casing."""
        lowered = {str(key).lower(): value for key, value in row.items()}
        for name in names:
            value = lowered.get(name.lower())
            if value is not None and value != "":
                return value
        return None

    def _tmschema_rows(self, catalog: str, rowset: str) -> List[Dict]:
        return self._execute_statement(f"SELECT * FROM $SYSTEM.TMSCHEMA_{rowset}", catalog)

    def _optional_tmschema_rows(self, catalog: str, rowset: str) -> List[Dict]:
        try:
            return self._tmschema_rows(catalog, rowset)
        except Exception:
            # TMSCHEMA is administrator-only on SSAS. A read-only connection is
            # the normal deployment and must retain the complete CSDL baseline.
            return []

    def _enrich_tabular_tables_from_tmschema(
        self, catalog: str, tables: List[Table],
    ) -> List[Table]:
        """Add privileged Tabular semantics while preserving CSDL as fallback.

        The first TABLES request is the permission probe. If it is refused, no
        further administrator rowsets are attempted. Successful rowsets add
        query-authoring metadata only; partition source expressions and other
        operational payloads are intentionally not persisted.
        """
        try:
            table_rows = self._tmschema_rows(catalog, "TABLES")
        except Exception:
            return tables

        tables_by_name = {
            str((table.metadata_json or {}).get(self.META_KEY, {}).get("tableName") or "").lower(): table
            for table in tables
        }
        tables_by_id: Dict[str, Table] = {}
        columns_by_id: Dict[str, TableColumn] = {}

        for row in table_rows:
            table_name = str(self._row_value(row, "Name", "ExplicitName") or "")
            table = tables_by_name.get(table_name.lower())
            if table is None:
                continue
            table_id = self._row_value(row, "ID", "TableID")
            if table_id is not None:
                tables_by_id[str(table_id)] = table
            description = self._row_value(row, "Description")
            if description:
                table.description = str(description)
            metadata = table.metadata_json[self.META_KEY]
            metadata["metadata_source"] = "CSDL+TMSCHEMA"
            for source, target in (
                ("DataCategory", "data_category"),
                ("IsHidden", "hidden"),
                ("IsPrivate", "private"),
            ):
                value = self._metadata_scalar(self._row_value(row, source))
                if value is not None:
                    metadata[target] = value

        # A successful DMV probe enriches every CSDL table, even if a provider
        # omits a table from TMSCHEMA_TABLES (for example a calculated table).
        for table in tables:
            table.metadata_json[self.META_KEY]["metadata_source"] = "CSDL+TMSCHEMA"

        column_rows = self._optional_tmschema_rows(catalog, "COLUMNS")
        for row in column_rows:
            table = tables_by_id.get(str(self._row_value(row, "TableID") or ""))
            if table is None:
                continue
            name = str(self._row_value(row, "Name", "ExplicitName", "InferredName") or "")
            column = next((item for item in (table.columns or []) if item.name == name), None)
            if column is None:
                continue
            column_id = self._row_value(row, "ID", "ColumnID")
            if column_id is not None:
                columns_by_id[str(column_id)] = column
            description = self._row_value(row, "Description")
            if description:
                column.description = str(description)
            metadata = dict(column.metadata or {})
            for source, target in (
                ("Expression", "expression"),
                ("FormatString", "format_string"),
                ("DataCategory", "data_category"),
                ("DisplayFolder", "display_folder"),
                ("SummarizeBy", "summarize_by"),
                ("SourceColumn", "source_column"),
                ("IsAvailableInMDX", "available_in_mdx"),
                ("ColumnStorageID", "storage_id"),
            ):
                value = self._metadata_scalar(self._row_value(row, source))
                if value is not None:
                    metadata[target] = value
            hidden = self._metadata_scalar(self._row_value(row, "IsHidden"))
            if hidden is not None:
                metadata["hidden"] = hidden
            sort_id = self._row_value(row, "SortByColumnID")
            if sort_id is not None:
                metadata["sort_by_column_id"] = str(sort_id)
            column.metadata = metadata

        measure_by_id: Dict[str, TableColumn] = {}
        for row in self._optional_tmschema_rows(catalog, "MEASURES"):
            table = tables_by_id.get(str(self._row_value(row, "TableID") or ""))
            if table is None:
                continue
            name = str(self._row_value(row, "Name") or "")
            measure = next((item for item in (table.columns or []) if item.name == name), None)
            if measure is None:
                continue
            measure_id = self._row_value(row, "ID", "MeasureID")
            if measure_id is not None:
                measure_by_id[str(measure_id)] = measure
            description = self._row_value(row, "Description")
            metadata = dict(measure.metadata or {})
            for source, target in (
                ("Expression", "expression"),
                ("FormatString", "format_string"),
                ("DataCategory", "data_category"),
                ("DisplayFolder", "display_folder"),
                ("DetailRowsDefinition", "detail_rows_expression"),
            ):
                value = self._metadata_scalar(self._row_value(row, source))
                if value is not None:
                    metadata[target] = value
            expression = metadata.get("expression")
            if description:
                measure.description = str(description)
            elif expression:
                # Match Power BI: a bounded formula preview helps the model
                # understand custom business logic without flooding context.
                measure.description = str(expression)[:200]
            returns = self._row_value(row, "DataType")
            if returns:
                metadata["returns"] = str(returns)
            hidden = self._metadata_scalar(self._row_value(row, "IsHidden"))
            if hidden is not None:
                metadata["hidden"] = hidden
            measure.metadata = metadata

        # Resolve sort-by IDs only after all columns have been indexed.
        for table in tables:
            for column in table.columns or []:
                metadata = column.metadata or {}
                sort_id = metadata.pop("sort_by_column_id", None)
                if sort_id and sort_id in columns_by_id:
                    metadata["sort_by_column"] = columns_by_id[sort_id].name

        for row in self._optional_tmschema_rows(catalog, "RELATIONSHIPS"):
            from_table = tables_by_id.get(str(self._row_value(row, "FromTableID") or ""))
            to_table = tables_by_id.get(str(self._row_value(row, "ToTableID") or ""))
            from_column = columns_by_id.get(str(self._row_value(row, "FromColumnID") or ""))
            to_column = columns_by_id.get(str(self._row_value(row, "ToColumnID") or ""))
            if any(item is None for item in (from_table, to_table, from_column, to_column)):
                continue
            state = "active" if self._truthy(self._row_value(row, "IsActive")) else "inactive"
            metadata = from_table.metadata_json[self.META_KEY]
            relationships = metadata.setdefault("relationships", [])
            relationship = next((
                item for item in relationships
                if item.get("fromColumn") == from_column.name
                and item.get("toTable") == to_table.name.split("/", 1)[-1]
                and item.get("toColumn") == to_column.name
            ), None)
            if relationship is None:
                relationship = {
                    "name": str(self._row_value(row, "Name") or ""),
                    "state": state,
                    "fromTable": from_table.name.split("/", 1)[-1],
                    "fromColumn": from_column.name,
                    "toTable": to_table.name.split("/", 1)[-1],
                    "toColumn": to_column.name,
                }
                relationships.append(relationship)
            else:
                relationship["state"] = state
                if self._row_value(row, "Name"):
                    relationship["name"] = str(self._row_value(row, "Name"))
            for source, target in (
                ("CrossFilteringBehavior", "crossFilteringBehavior"),
                ("SecurityFilteringBehavior", "securityFilteringBehavior"),
                ("RelyOnReferentialIntegrity", "relyOnReferentialIntegrity"),
            ):
                value = self._metadata_scalar(self._row_value(row, source))
                if value is not None:
                    relationship[target] = value

        hierarchy_rows = self._optional_tmschema_rows(catalog, "HIERARCHIES")
        level_rows = self._optional_tmschema_rows(catalog, "LEVELS")
        levels_by_hierarchy: Dict[str, List[Dict]] = {}
        for row in level_rows:
            hierarchy_id = str(self._row_value(row, "HierarchyID") or "")
            column = columns_by_id.get(str(self._row_value(row, "ColumnID") or ""))
            level = {
                "name": str(self._row_value(row, "Name") or ""),
                "ordinal": self._metadata_scalar(self._row_value(row, "Ordinal")),
            }
            if column is not None:
                level["column"] = column.name
            levels_by_hierarchy.setdefault(hierarchy_id, []).append(level)
        for row in hierarchy_rows:
            table = tables_by_id.get(str(self._row_value(row, "TableID") or ""))
            if table is None:
                continue
            hierarchy_id = str(self._row_value(row, "ID", "HierarchyID") or "")
            hierarchy = {
                "name": str(self._row_value(row, "Name") or ""),
                "description": str(self._row_value(row, "Description") or ""),
                "displayFolder": str(self._row_value(row, "DisplayFolder") or ""),
                "hidden": bool(self._truthy(self._row_value(row, "IsHidden"))),
                "levels": sorted(
                    levels_by_hierarchy.get(hierarchy_id, []),
                    key=lambda item: item.get("ordinal") if item.get("ordinal") is not None else 0,
                ),
            }
            table.metadata_json[self.META_KEY].setdefault("hierarchies", []).append(hierarchy)

        for row in self._optional_tmschema_rows(catalog, "PARTITIONS"):
            table = tables_by_id.get(str(self._row_value(row, "TableID") or ""))
            if table is None:
                continue
            partition = {
                "name": str(self._row_value(row, "Name") or ""),
                "mode": str(self._row_value(row, "Mode") or ""),
                "state": str(self._row_value(row, "State") or ""),
            }
            table.metadata_json[self.META_KEY].setdefault("partitions", []).append(partition)

        for row in self._optional_tmschema_rows(catalog, "KPIS"):
            measure = measure_by_id.get(str(self._row_value(row, "MeasureID") or ""))
            if measure is None:
                continue
            measure.metadata = {
                **(measure.metadata or {}),
                "kpi": {
                    "name": str(self._row_value(row, "Name") or measure.name),
                    "target_expression": str(self._row_value(row, "TargetExpression") or ""),
                    "status_expression": str(self._row_value(row, "StatusExpression") or ""),
                    "trend_expression": str(self._row_value(row, "TrendExpression") or ""),
                    "graphic": str(self._row_value(row, "StatusGraphic", "Graphic") or ""),
                },
            }

        model_metadata = {
            "roles": [
                {
                    "name": str(self._row_value(row, "Name") or ""),
                    "permission": str(self._row_value(row, "ModelPermission") or ""),
                }
                for row in self._optional_tmschema_rows(catalog, "ROLES")
            ],
            "perspectives": [
                str(self._row_value(row, "Name") or "")
                for row in self._optional_tmschema_rows(catalog, "PERSPECTIVES")
            ],
            "cultures": [
                str(self._row_value(row, "Name") or "")
                for row in self._optional_tmschema_rows(catalog, "CULTURES")
            ],
        }
        model_metadata = {key: value for key, value in model_metadata.items() if value}
        if model_metadata:
            for table in tables:
                table.metadata_json[self.META_KEY]["model_metadata"] = model_metadata

        return tables

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
                tabular_tables = self._tabular_tables_from_csdl(catalog, schema)
                tables.extend(self._enrich_tabular_tables_from_tmschema(catalog, tabular_tables))
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
