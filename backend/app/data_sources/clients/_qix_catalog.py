"""Shared Qlik Engine discovery, semantic catalog, and object queries.

Adapters supply authentication, app resolution, and app-level metadata.
"""
from __future__ import annotations
import asyncio
import copy
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients._qix_common import (
    MasterItemIndex,
    QixSession,
    decide_condition,
    describe_options,
    dollar_formulas,
    expression_drivers,
    expression_choices,
    literal_text,
    object_options,
    resolve_expression,
    selector_fields,
    summarize_switch,
    summary_drivers,
    variable_dependencies,
    apply_field_selections,
    build_hypercube_def,
    describe_object,
    fetch_hypercube_pages,
    flatten_property_tree,
    hypercube_matrix_to_df,
    summarize_object_properties,
    tags_to_dtype,
)

logger = logging.getLogger(__name__)

# `Password=...` / `pwd = ...` inside a connection string or a SELECT that
# lineage hands back verbatim. Lineage is stored in table metadata, so anything
# secret-shaped is masked before it gets that far.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|pwd|passwd|secret|api[_-]?key|token)\s*=\s*[^;,\s\]]+"
)

# Section Access is Qlik's row-level security, declared as a script section.
_SECTION_ACCESS_RE = re.compile(r"(?im)^\s*section\s+access\s*;")

# Master items are app-level, not table-level: a Qlik measure is an expression
# over any fields in the model, so it has no home table the way a Power BI
# measure does. They are collected into one synthetic table per app.
_MASTER_TABLE_SUFFIX = "Master Items"

# Sheets are catalogued as `Stream/App/Sheets/<sheet title>`, one entry per
# sheet, one column per object on it — the way Splunk dashboards are.
_SHEET_TABLE_SEGMENT = "Sheets"

# Objects that only lay out or decorate a sheet. They compute nothing, so they
# are left out of the catalog (a container's tabs are catalogued themselves).
_LAYOUT_ONLY_TYPES = {"container", "sn-layout-container", "sn-tabbed-container"}




def _mask_secrets(text: str) -> str:
    """Blank out `password=...`-shaped assignments in free text."""
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=***", text or "")


def _compact(d: Dict[str, Any]) -> Dict[str, Any]:
    """Drop keys whose value carries no information.

    Qlik returns a full field-statistics struct for every field, most of it
    zeros and empty strings on any given one. `False` and `0` are kept — a
    `qnNonNulls` of 0 is the signal that a field is a synthetic-key participant,
    not an absent value.
    """
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}


def _tag_names(tags: Any) -> List[str]:
    """QRS tags arrive as full entity references; only the name is meaningful."""
    out = []
    for tag in tags or []:
        name = tag.get("name") if isinstance(tag, dict) else str(tag)
        if name:
            out.append(name)
    return out


def _custom_properties(props: Any) -> Dict[str, List[str]]:
    """Flatten QRS custom properties to {definition name: [values]}.

    Custom properties are how a governed Qlik site records the things an admin
    actually cares about — data classification, owning department, environment —
    so they are worth carrying even though the raw shape is three levels deep.
    """
    out: Dict[str, List[str]] = {}
    for prop in props or []:
        if not isinstance(prop, dict):
            continue
        name = ((prop.get("definition") or {}).get("name")) or prop.get("name")
        value = prop.get("value")
        if not name or value in (None, ""):
            continue
        out.setdefault(str(name), []).append(str(value))
    return out


def _master_item_provenance(item: Dict[str, Any]) -> Dict[str, Any]:
    """Who published a master item, when, and under what tags.

    `qMeta` is returned alongside every list entry. On a governed site the tags
    are the approval trail ("Certified", "Deprecated"), which is the difference
    between a measure the agent should reach for and one it should not.
    """
    meta = item.get("qMeta") or {}
    return _compact({
        "itemId": (item.get("qInfo") or {}).get("qId"),
        "tags": _tag_names(meta.get("tags")) or [t for t in (meta.get("tags") or []) if isinstance(t, str)],
        "owner": _owner_name(meta.get("owner")),
        "created": meta.get("createdDate"),
        "modified": meta.get("modifiedDate"),
        "approved": meta.get("approved"),
    })


def _owner_name(owner: Any) -> Optional[str]:
    """`DIRECTORY\\userId` for a QRS owner reference."""
    if not isinstance(owner, dict):
        return None
    user_id = owner.get("userId")
    if not user_id:
        return owner.get("name") or None
    directory = owner.get("userDirectory")
    return f"{directory}\\{user_id}" if directory else str(user_id)


class QlikEngineCatalog:
    """Engine-only behavior shared by Cloud and Enterprise on Windows."""

    # A key field shared by more than this many tables is an associative hub
    # (a calendar key, an order number). Emitting foreign keys for it produces
    # n*(n-1) directed edges that say nothing a single "these tables associate"
    # note doesn't, so past this point the fan-out is recorded, not expanded.
    _MAX_KEY_FANOUT = 12

    _MAX_LINEAGE_ENTRIES = 60
    _MAX_STATEMENT_CHARS = 400
    _MAX_MASTER_ITEMS = 400
    _MAX_VARIABLES = 100
    _MAX_SHEETS = 100
    _MAX_LINEAGE_SOURCES = 25
    _MAX_SCRIPT_CHARS = 4000
    _MAX_ASSOCIATIONS = 50
    _MAX_OBJECTS_PER_SHEET = 60
    _MAX_MASTER_OBJECTS = 100
    _MAX_OBJECT_DESCRIPTION_CHARS = 600
    # A data island is a small table with no key to the model: a list of
    # choices (and sometimes their formulas), not data.
    _MAX_ISLAND_ROWS = 50
    _MAX_ISLAND_FIELDS = 8
    _MAX_ISLANDS = 10
    _MAX_SEARCH_RESULTS = 1000

    @staticmethod
    async def _session_list(qix: QixSession, doc_handle: int, layout_key: str, definition: Dict[str, Any]):
        """CreateSessionObject + GetLayout, returning one list's qItems.

        Master items are not readable in bulk any other way: the Engine exposes
        them as a *list object* you define, ask for the layout of, and throw
        away with the session.
        """
        obj = await qix.rpc(doc_handle, "CreateSessionObject", [definition])
        handle = (obj.get("qReturn") or {}).get("qHandle")
        if handle is None:
            return []
        layout = await qix.rpc(handle, "GetLayout", [])
        return ((layout.get("qLayout") or {}).get(layout_key) or {}).get("qItems") or []

    _MEASURE_LIST_DEF = {
        "qInfo": {"qType": "MeasureList"},
        "qMeasureListDef": {
            "qType": "measure",
            "qData": {
                "title": "/qMetaDef/title",
                "description": "/qMetaDef/description",
                "expression": "/qMeasure/qDef",
                "label": "/qMeasure/qLabel",
            },
        },
    }

    _DIMENSION_LIST_DEF = {
        "qInfo": {"qType": "DimensionList"},
        "qDimensionListDef": {
            "qType": "dimension",
            "qData": {
                "title": "/qMetaDef/title",
                "description": "/qMetaDef/description",
                "grouping": "/qDim/qGrouping",
            },
        },
    }

    _VARIABLE_LIST_DEF = {
        "qInfo": {"qType": "VariableList"},
        "qVariableListDef": {
            "qType": "variable",
            # Reserved and config variables are Qlik's own hundreds of
            # formatting/system entries — noise in a catalog.
            "qShowReserved": False,
            "qShowConfig": False,
            "qData": {"definition": "/qDefinition"},
        },
    }

    # Sheets are the on-prem equivalent of the dashboards the Power BI connector
    # records: the analyses the app's authors actually built. `cells` is
    # deliberately not requested — the per-visualisation layout is large and
    # says nothing a sheet title doesn't.
    _SHEET_LIST_DEF = {
        "qInfo": {"qType": "SheetList"},
        "qAppObjectListDef": {
            "qType": "sheet",
            "qData": {"title": "/qMetaDef/title", "description": "/qMetaDef/description"},
        },
    }

    # Master visualizations: charts saved to the library and reused on sheets.
    # A sheet object linked to one (`qExtendsId`) stores almost nothing itself,
    # so these are read to know what the linked copies compute.
    _MASTER_OBJECT_LIST_DEF = {
        "qInfo": {"qType": "MasterObjectList"},
        "qAppObjectListDef": {
            "qType": "masterobject",
            "qData": {"title": "/qMetaDef/title", "description": "/qMetaDef/description"},
        },
    }

    @staticmethod
    def _sheet_is_shared(item: Dict[str, Any], app_published: bool) -> bool:
        """Whether a sheet is visible to the app's audience, not just its author.

        In a published app, base sheets are `approved` and community sheets are
        `published`; a sheet that is neither is someone's private work and must
        not reach a catalog every member of the connection reads. An unpublished
        app is itself private to its owner, so its sheets are all in scope.
        """
        if not app_published:
            return True
        meta = item.get("qMeta") or {}
        if "published" not in meta and "approved" not in meta:
            return True
        return bool(meta.get("published") or meta.get("approved"))

    @staticmethod
    async def _object_handle(qix: QixSession, doc_handle: int, object_id: str) -> Optional[int]:
        result = await qix.rpc(doc_handle, "GetObject", [object_id])
        return (result.get("qReturn") or {}).get("qHandle")

    async def _read_sheet_objects(self, qix: QixSession, doc_handle: int, out: Dict[str, Any]) -> None:
        """Read each shared sheet's object tree and each master visualization.

        One `GetObject` + `GetFullPropertyTree` per sheet returns the sheet and
        every object on it — containers and filter panes with their children —
        in a single round trip, rather than one call per chart.
        """
        app_id = out.get("app_id")
        for item in (out.get("sheets") or [])[: self._MAX_SHEETS]:
            sheet_id = (item.get("qInfo") or {}).get("qId")
            if not sheet_id:
                continue
            try:
                handle = await self._object_handle(qix, doc_handle, sheet_id)
                if handle is None:
                    continue
                tree = await qix.rpc(handle, "GetFullPropertyTree", [])
                out["sheet_trees"][sheet_id] = tree.get("qPropEntry") or {}
            except Exception as e:
                # One unreadable sheet must not cost the rest of the app.
                logger.debug("Qlik sheet %s unreadable in app %s: %s", sheet_id, app_id, e)
        for item in (out.get("master_objects") or [])[: self._MAX_MASTER_OBJECTS]:
            object_id = (item.get("qInfo") or {}).get("qId")
            if not object_id:
                continue
            try:
                handle = await self._object_handle(qix, doc_handle, object_id)
                if handle is None:
                    continue
                props = await qix.rpc(handle, "GetProperties", [])
                out["master_object_props"][object_id] = props.get("qProp") or {}
            except Exception as e:
                logger.debug("Qlik master visualization %s unreadable in app %s: %s", object_id, app_id, e)

    def _expression_texts(self, payload: Dict[str, Any]) -> List[str]:
        """Every expression the app's sheets, master items and variables hold."""
        index = self._master_index(payload)
        texts: List[str] = []
        for tree in (payload.get("sheet_trees") or {}).values():
            for props, parent in flatten_property_tree(tree):
                if parent is None:
                    continue
                summary = summarize_object_properties(props, index)
                for key in ("measures", "dimensions"):
                    for item in summary.get(key) or []:
                        texts += [item.get("expression") or "", item.get("showWhen") or "", item.get("field") or ""]
                texts.append(summary.get("calcCondition") or "")
        for props in (payload.get("master_object_props") or {}).values():
            summary = summarize_object_properties(props, index)
            for item in summary.get("measures") or []:
                texts.append(item.get("expression") or "")
        for item in payload.get("measures") or []:
            texts.append((item.get("qData") or {}).get("expression") or "")
        for item in payload.get("variables") or []:
            texts.append(str((item.get("qData") or {}).get("definition") or ""))
        return [t for t in texts if t]

    async def _read_islands(self, qix: QixSession, doc_handle: int, out: Dict[str, Any]) -> None:
        """Read the rows of the small unlinked tables that drive expressions.

        A data island holds a dynamic chart's choices — "Sales amount",
        "Quantity" — and sometimes the formula of each. Only islands whose
        fields an expression actually reads (`Only(Metric)`,
        `$(=Only(MetricExpr))`, `GetFieldSelections(...)`) are read, and only
        while they stay small, so a crawl never pages a real data table.
        """
        drivers: set = set()
        for text in self._expression_texts(out):
            drivers.update(expression_drivers(text)["fields"])
        if not drivers:
            return
        linked = {t for key in out.get("qk") or [] for t in key.get("qTables") or []}
        read = 0
        for tbl in out.get("qtr") or []:
            name = tbl.get("qName") or ""
            fields = [f.get("qName") for f in tbl.get("qFields") or [] if f.get("qName")]
            rows = tbl.get("qNoOfRows")
            if (
                not name or name in linked or not fields
                or not drivers.intersection(fields)
                or not isinstance(rows, int) or rows > self._MAX_ISLAND_ROWS
                or len(fields) > self._MAX_ISLAND_FIELDS
            ):
                continue
            cube = build_hypercube_def(fields, [], self._MAX_ISLAND_ROWS)
            session = await qix.rpc(doc_handle, "CreateSessionObject", [{
                "qInfo": {"qType": "bow-island"}, "qHyperCubeDef": cube,
            }])
            handle = (session.get("qReturn") or {}).get("qHandle")
            if handle is None:
                continue
            matrix = await fetch_hypercube_pages(qix, handle, len(fields), self._MAX_ISLAND_ROWS)
            table_rows = []
            for cells in matrix:
                row = {}
                for field, cell in zip(fields, cells, strict=False):
                    text = cell.get("qText") if isinstance(cell, dict) else None
                    if text in (None, "", "-") and isinstance(cell, dict) and isinstance(cell.get("qNum"), (int, float)):
                        text = cell["qNum"]
                    row[field] = text
                table_rows.append(row)
            out["islands"][name] = {"fields": fields, "rows": table_rows}
            read += 1
            if read >= self._MAX_ISLANDS:
                break

    async def _read_app_async(self, app: Dict[str, Any], user: str) -> Dict[str, Any]:
        """One Engine session per app: model, master items, sheets, lineage, script size."""
        app_id = app.get("id")
        out: Dict[str, Any] = {
            "app_id": app_id,
            "qtr": [], "qk": [], "measures": [], "dimensions": [], "variables": [],
            "sheets": [], "master_objects": [], "sheet_trees": {}, "master_object_props": {},
            "islands": {},
            "lineage": [], "has_data": None, "script_chars": None,
        }
        async with self._qix_session(app_id, user) as qix:
            opendoc = await qix.rpc(-1, "OpenDoc", [app_id])
            doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
            if doc_handle is None:
                raise RuntimeError("OpenDoc returned no document handle")

            layout = (await qix.rpc(doc_handle, "GetAppLayout", [])).get("qLayout") or {}
            out["has_data"] = layout.get("qHasData")
            out["has_script"] = layout.get("qHasScript")
            out["file_name"] = layout.get("qFileName")
            # Alternate states let one sheet compare two selections. A measure
            # written against a state (`{[State1]<...>}`) is meaningless without
            # knowing the state exists.
            out["state_names"] = [s for s in (layout.get("qStateNames") or []) if s]

            tk = await qix.rpc(doc_handle, "GetTablesAndKeys", {
                "qWindowSize": {"qcx": 0, "qcy": 0},
                "qNullSize": {"qcx": 0, "qcy": 0},
                "qCellHeight": 0,
                "qSyntheticMode": False,
                "qIncludeSysVars": False,
            })
            out["qtr"] = tk.get("qtr") or []
            out["qk"] = tk.get("qk") or []

            lists = []
            if self.include_master_items:
                lists += [
                    ("measures", "qMeasureList", self._MEASURE_LIST_DEF),
                    ("dimensions", "qDimensionList", self._DIMENSION_LIST_DEF),
                    ("variables", "qVariableList", self._VARIABLE_LIST_DEF),
                ]
            if self.include_master_items or self.include_sheet_objects:
                lists.append(("sheets", "qAppObjectList", self._SHEET_LIST_DEF))
            if self.include_sheet_objects:
                lists.append(("master_objects", "qAppObjectList", self._MASTER_OBJECT_LIST_DEF))
            for key, layout_key, definition in lists:
                try:
                    out[key] = await self._session_list(qix, doc_handle, layout_key, definition)
                except Exception as e:
                    # A missing master-item list is not a reason to lose the
                    # data model we already have in hand.
                    logger.debug("Qlik %s list failed for app %s: %s", key, app_id, e)
            published = bool(app.get("published"))
            out["sheets"] = [s for s in out["sheets"] if self._sheet_is_shared(s, published)]

            if self.include_sheet_objects:
                await self._read_sheet_objects(qix, doc_handle, out)
                try:
                    await self._read_islands(qix, doc_handle, out)
                except Exception as e:
                    logger.debug("Qlik data islands unreadable in app %s: %s", app_id, e)

            if self.include_lineage:
                try:
                    lineage = await qix.rpc(doc_handle, "GetLineage", [])
                    # The Engine wraps the array as {"qLineage": [...]}, like
                    # every JSON-RPC return value; iterating the dict itself
                    # yields its key and silently drops all lineage.
                    if isinstance(lineage, dict):
                        lineage = lineage.get("qLineage")
                    out["lineage"] = lineage if isinstance(lineage, list) else []
                except Exception as e:
                    logger.debug("Qlik GetLineage failed for app %s: %s", app_id, e)
                try:
                    script = await qix.rpc(doc_handle, "GetScript", [])
                    text = script.get("qScript") or ""
                    out["script_chars"] = len(text)
                    # Section Access is Qlik's row-level security and is declared
                    # only in the load script, so this is the only place it can
                    # be detected.
                    out["section_access"] = bool(_SECTION_ACCESS_RE.search(text))
                    out["script_excerpt"] = _mask_secrets(text)[: self._MAX_SCRIPT_CHARS] or None
                except Exception as e:
                    logger.debug("Qlik GetScript failed for app %s: %s", app_id, e)

        out["sheet_names"] = [
            t for t in (
                (item.get("qData") or {}).get("title") or (item.get("qMeta") or {}).get("title")
                for item in out.get("sheets") or []
            ) if t
        ][: self._MAX_SHEETS]
        # Distinct upstream sources, names only — the compact form that every
        # table carries. Order-preserving so the list is stable across crawls.
        seen: set = set()
        sources: List[str] = []
        for entry in out.get("lineage") or []:
            if not isinstance(entry, dict):
                continue
            source = _mask_secrets(str(entry.get("qDiscriminator") or "")).strip()
            if not source or source in seen:
                continue
            seen.add(source)
            sources.append(source)
            if len(sources) >= self._MAX_LINEAGE_SOURCES:
                break
        out["lineage_sources"] = sources
        return out

    # ------------------------------------------------------------------
    # Schema construction
    # ------------------------------------------------------------------

    @staticmethod
    def _app_key(app: Dict[str, Any]) -> str:
        """The `Stream/App` prefix every table of this app is named under."""
        name = app.get("name") or app.get("id") or ""
        stream = app.get("space_name") or ""
        return f"{stream}/{name}" if stream else name

    def _clean_lineage(self, lineage: Any) -> List[Dict[str, str]]:
        """Dedupe, cap and de-secret the lineage entries."""
        cleaned: List[Dict[str, str]] = []
        seen: set = set()
        for entry in lineage or []:
            if not isinstance(entry, dict):
                continue
            discriminator = _mask_secrets(str(entry.get("qDiscriminator") or ""))
            statement = _mask_secrets(str(entry.get("qStatement") or ""))
            if not discriminator and not statement:
                continue
            statement = statement[: self._MAX_STATEMENT_CHARS]
            key = (discriminator, statement)
            if key in seen:
                continue
            seen.add(key)
            item = {}
            if discriminator:
                item["source"] = discriminator
            if statement:
                item["statement"] = statement
            cleaned.append(item)
            if len(cleaned) >= self._MAX_LINEAGE_ENTRIES:
                break
        return cleaned

    def _build_data_tables(
        self,
        app: Dict[str, Any],
        qtr: List[Dict],
        qk: List[Dict],
        payload: Optional[Dict[str, Any]] = None,
    ) -> List[Table]:
        """Map GetTablesAndKeys onto Tables.

        Qlik associates rather than joins: `qk` says a key field links a set of
        tables, with no direction and no notion of parent/child. Each link is
        therefore emitted from both sides.
        """
        app_key = self._app_key(app)
        app_meta = self._app_meta(app, payload)

        keys_by_table: Dict[str, List[Dict[str, Any]]] = {}
        for key in qk or []:
            key_fields = key.get("qKeyFields") or []
            linked = key.get("qTables") or []
            for t in linked:
                keys_by_table.setdefault(t, []).append({
                    "fields": key_fields,
                    "others": [o for o in linked if o != t],
                    "fanout": len(linked),
                })

        tables: List[Table] = []
        for tbl in qtr or []:
            tbl_name = tbl.get("qName") or ""
            if not tbl_name:
                continue

            entries = keys_by_table.get(tbl_name, [])
            key_fields = {kf for e in entries for kf in e["fields"]}

            columns: List[TableColumn] = []
            pks: List[TableColumn] = []
            for field in tbl.get("qFields") or []:
                fname = field.get("qName") or ""
                if not fname:
                    continue
                tags = field.get("qTags") or []
                # Qlik profiles every field at reload, and that profile is the
                # richest column metadata any BI connector here gets: cardinality,
                # null density, and what share of a key's values this table
                # actually holds. It is kept in full — the prompt allowlist
                # decides what renders, so storing it costs nothing at inference
                # time and answering "is this column selective enough to group
                # by" without it means querying the app.
                col_meta: Dict[str, Any] = _compact({
                    "role": "column",
                    "qlik_tags": tags,
                    "rows": field.get("qnRows"),
                    "nonNulls": field.get("qnNonNulls"),
                    "distinctValues": field.get("qnTotalDistinctValues"),
                    "presentDistinctValues": field.get("qnPresentDistinctValues"),
                    # -1 means Qlik could not compute it (a synthetic-key
                    # participant); 1.0 means every row has a value.
                    "informationDensity": field.get("qInformationDensity"),
                    # For a key: the fraction of the key's distinct values this
                    # table carries. < 1 means this side is incomplete.
                    "subsetRatio": field.get("qSubsetRatio"),
                    "keyType": field.get("qKeyType"),
                    "comment": field.get("qComment"),
                    "derivedFields": [
                        d.get("qName") for d in (field.get("qDerivedFields") or []) if d.get("qName")
                    ],
                })
                for flag, key in (
                    ("qIsSynthetic", "synthetic"),
                    ("qIsDetail", "detail"),
                    ("qIsSemantic", "semantic"),
                    ("qIsFieldOnTheFly", "onTheFly"),
                ):
                    if field.get(flag):
                        col_meta[key] = True
                # Qlik's own system fields are queryable but never meant for a
                # report, exactly like a hidden Power BI column.
                if "$hidden" in tags or "$system" in tags or field.get("qIsHidden"):
                    col_meta["hidden"] = True
                if fname in key_fields:
                    col_meta["relationship_key"] = True
                col = TableColumn(
                    name=fname,
                    dtype=tags_to_dtype(tags),
                    description=field.get("qComment") or None,
                    metadata=col_meta,
                )
                columns.append(col)
                if fname in key_fields:
                    pks.append(col)

            fks: List[ForeignKey] = []
            hubs: List[str] = []
            for entry in entries:
                if entry["fanout"] > self._MAX_KEY_FANOUT:
                    hubs.extend(entry["fields"])
                    continue
                for kf in entry["fields"]:
                    for other in entry["others"]:
                        fks.append(ForeignKey(
                            column=TableColumn(name=kf, dtype="key"),
                            references_name=f"{app_key}/{other}" if app_key else other,
                            references_column=TableColumn(name=kf, dtype="key"),
                        ))

            meta = dict(app_meta)
            meta.update(_compact({
                "tableName": tbl_name,
                "rowCount": tbl.get("qNoOfRows"),
                "fieldCount": len(columns),
                "comment": tbl.get("qComment"),
                "position": (tbl.get("qPos") or {}).get("qx") if isinstance(tbl.get("qPos"), dict) else None,
                "source": "qix",
                # Which tables this one associates with, and on which fields.
                # The foreign keys carry the same links, but a capped hub key
                # emits none — so this is the record that survives the cap.
                "associations": [
                    {"fields": e["fields"], "tables": e["others"]} for e in entries
                ][: self._MAX_ASSOCIATIONS],
            }))
            if hubs:
                meta["associativeHubKeys"] = sorted(set(hubs))
            if tbl.get("qLoose"):
                meta["loose"] = True
            if tbl.get("qIsSynthetic") or tbl_name.startswith("$Syn"):
                # A synthetic table is Qlik's own artifact from tables sharing
                # several fields, not something a user modelled.
                meta["synthetic"] = True

            tables.append(Table(
                name=f"{app_key}/{tbl_name}" if app_key else tbl_name,
                description=tbl.get("qComment") or None,
                columns=columns,
                pks=pks,
                fks=fks,
                is_active=True,
                metadata_json={self._metadata_namespace: meta},
            ))
        return tables

    def _build_master_table(
        self,
        app: Dict[str, Any],
        payload: Dict[str, Any],
        taken_names: set,
    ) -> Optional[Table]:
        """One synthetic table per app holding its reusable definitions.

        A master measure carries the business logic the app's authors agreed on
        — `Sum({$<[Region]={'North'}>} [Qty]*[Price])` — and re-deriving it from
        raw fields will not reproduce its set analysis. Surfacing the expression
        is the point: the agent can pass it straight to `execute_query`.
        """
        app_key = self._app_key(app)
        columns: List[TableColumn] = []

        for item in (payload.get("measures") or [])[: self._MAX_MASTER_ITEMS]:
            data = item.get("qData") or {}
            meta_def = item.get("qMeta") or {}
            name = data.get("title") or meta_def.get("title") or ""
            if not name:
                continue
            expression = data.get("expression") or ""
            item_meta: Dict[str, Any] = _compact({
                "role": "measure",
                "expression": expression,
                "label": data.get("label"),
                **_master_item_provenance(item),
            })
            # The expression MUST go in `description`. Column metadata is filtered
            # by an allowlist on its way into the prompt (`_COLUMN_META_KEYS` in
            # tables_schema_section) which does not include `expression`, so the
            # metadata copy is for storage only — description is the one field
            # that reaches the model. This is why the Power BI client writes its
            # DAX there too.
            human = data.get("description") or meta_def.get("description") or ""
            columns.append(TableColumn(
                name=name,
                dtype="measure",
                description=(f"{human} — {expression}" if human and expression else (human or expression))[:300] or None,
                metadata=item_meta,
            ))

        for item in (payload.get("dimensions") or [])[: self._MAX_MASTER_ITEMS]:
            data = item.get("qData") or {}
            meta_def = item.get("qMeta") or {}
            name = data.get("title") or meta_def.get("title") or ""
            if not name:
                continue
            item_meta: Dict[str, Any] = _compact({
                "role": "dimension",
                "grouping": data.get("grouping"),
                **_master_item_provenance(item),
            })
            # "H" is a drill-down group — several fields the user descends
            # through, not a single selectable field.
            if data.get("grouping") == "H":
                item_meta["drilldown"] = True
            columns.append(TableColumn(
                name=name,
                dtype="dimension",
                description=(data.get("description") or meta_def.get("description") or None),
                metadata=item_meta,
            ))

        variables = []
        variable_items = payload.get("variables") or []
        definitions = self._variable_definitions(variable_items)
        required = set(variable_dependencies(self._expression_texts({**payload, "variables": []}), definitions, strict=False))
        for position, item in enumerate(variable_items):
            if position >= self._MAX_VARIABLES and item.get("qName") not in required:
                continue
            data = item.get("qData") or {}
            name = (item.get("qName") or (item.get("qMeta") or {}).get("title") or "")
            if not name:
                continue
            definition = _mask_secrets(str(data.get("definition") or ""))
            variables.append({"name": name, "definition": definition})
            if any(c.name == name for c in columns):
                continue  # a master item already holds this name
            # A variable is a column too: table-level metadata does not reach
            # the prompt, and a chart written as `Sum({<Year={$(vCurrentYear)}>}
            # Sales)` is unreadable without knowing what vCurrentYear holds.
            columns.append(TableColumn(
                name=name,
                dtype="variable",
                description=(
                    f"variable; use as $({name}) — definition: {definition}"
                ),
                metadata=_compact({"role": "variable", "definition": definition}),
            ))

        index = self._master_index(payload)
        for object_id, props in list((payload.get("master_object_props") or {}).items())[: self._MAX_MASTER_OBJECTS]:
            summary = summarize_object_properties(props, index)
            if not (summary.get("dimensions") or summary.get("measures") or summary.get("listFields")):
                continue
            title = summary.get("title") or f"{summary.get('type')} {object_id}"
            if any(c.name == title for c in columns):
                title = f"{title} ({object_id})"
            columns.append(TableColumn(
                name=title,
                dtype=str(summary.get("type") or "object"),
                description=_mask_secrets(
                    "master visualization: " + describe_object(summary, self._MAX_OBJECT_DESCRIPTION_CHARS)
                ),
                metadata=self._object_column_meta(summary, master_visualization=True),
            ))

        sheets = []
        for item in (payload.get("sheets") or [])[: self._MAX_SHEETS]:
            data = item.get("qData") or {}
            meta_def = item.get("qMeta") or {}
            title = data.get("title") or meta_def.get("title") or ""
            if not title:
                continue
            entry = {"name": title}
            sheet_desc = data.get("description") or meta_def.get("description") or ""
            if sheet_desc:
                entry["description"] = sheet_desc[:200]
            sheets.append(entry)

        lineage = self._clean_lineage(payload.get("lineage"))

        if not columns and not variables and not sheets and not lineage:
            return None

        base = f"{app_key}/{_MASTER_TABLE_SUFFIX}" if app_key else _MASTER_TABLE_SUFFIX
        name = base if base not in taken_names else f"{base} ({app.get('id')})"

        meta = dict(self._app_meta(app, payload))
        meta.update({
            "objectType": "master_items",
            "measureCount": sum(1 for c in columns if c.dtype == "measure"),
            "dimensionCount": sum(1 for c in columns if c.dtype == "dimension"),
            "variableCount": sum(1 for c in columns if c.dtype == "variable"),
            "masterVisualizationCount": sum(
                1 for c in columns if (c.metadata or {}).get("role") == "master_visualization"
            ),
        })
        if variables:
            meta["variables"] = variables
        # The richer forms live here, once per app, rather than on every table:
        # sheet descriptions (not just names), lineage statements (not just
        # source names), and the load script itself.
        if sheets:
            meta["sheetDetails"] = sheets
        if lineage:
            meta["lineage"] = lineage
        if payload.get("script_excerpt"):
            meta["scriptExcerpt"] = payload["script_excerpt"]
            meta["scriptTruncated"] = (payload.get("script_chars") or 0) > self._MAX_SCRIPT_CHARS

        return Table(
            name=name,
            description=(
                f"Reusable definitions published in the Qlik app '{app.get('name')}': master "
                "measures (with their Qlik expressions), master dimensions, variables and master "
                "visualizations. Not a physical table — pass a measure's expression to "
                "execute_query(measures=[...])."
            ),
            columns=columns,
            pks=[],
            fks=[],
            is_active=True,
            metadata_json={self._metadata_namespace: meta},
        )

    @staticmethod
    def _master_index(payload: Dict[str, Any]) -> MasterItemIndex:
        """Master measures and dimensions by id, for resolving `qLibraryId`."""
        measures: Dict[str, Dict[str, str]] = {}
        for item in payload.get("measures") or []:
            item_id = (item.get("qInfo") or {}).get("qId")
            data = item.get("qData") or {}
            name = data.get("title") or (item.get("qMeta") or {}).get("title")
            if item_id and name:
                measures[item_id] = {"name": name, "expression": data.get("expression") or ""}
        dimensions: Dict[str, str] = {}
        for item in payload.get("dimensions") or []:
            item_id = (item.get("qInfo") or {}).get("qId")
            name = (item.get("qData") or {}).get("title") or (item.get("qMeta") or {}).get("title")
            if item_id and name:
                dimensions[item_id] = name
        return MasterItemIndex(measures, dimensions)

    @staticmethod
    def _object_column_meta(summary: Dict[str, Any], *, master_visualization: bool = False) -> Dict[str, Any]:
        """Structured copy of an object summary, stored with its column."""
        refs = sorted({
            item["master"]
            for key in ("dimensions", "measures", "alternativeDimensions", "alternativeMeasures")
            for item in summary.get(key) or []
            if item.get("master")
        })
        masked = {
            key: [
                {k: (_mask_secrets(v) if isinstance(v, str) else v) for k, v in item.items()}
                for item in summary.get(key) or []
            ]
            for key in ("dimensions", "measures", "alternativeDimensions", "alternativeMeasures")
        }
        return _compact({
            "role": "master_visualization" if master_visualization else "object",
            "objectId": summary.get("id"),
            "objectType": summary.get("type"),
            "title": summary.get("title"),
            "subtitle": summary.get("subtitle"),
            **masked,
            "listFields": summary.get("listFields"),
            "masterRefs": refs,
            "variables": summary.get("variables"),
            "calcCondition": _mask_secrets(summary.get("calcCondition") or "") or None,
            "state": summary.get("state"),
            "extendsId": summary.get("extendsId"),
            "container": summary.get("container"),
        })

    def _sheet_objects(
        self,
        tree: Dict[str, Any],
        index: MasterItemIndex,
        master_objects: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """The data-bearing objects on one sheet, as summaries.

        Folds a filter pane's listboxes into the pane, resolves a linked master
        visualization to what it computes, and drops pure layout objects.
        """
        objects: List[Dict[str, Any]] = []
        by_id: Dict[str, Dict[str, Any]] = {}
        for props, parent in flatten_property_tree(tree):
            if parent is None:
                continue  # the sheet itself
            summary = summarize_object_properties(props, index)
            switch = summarize_switch(props)
            if switch:
                summary["switch"] = switch
                summary.setdefault("title", switch.get("title") or "")
            parent_id = (parent.get("qInfo") or {}).get("qId")
            parent_summary = by_id.get(parent_id) if parent_id else None
            if parent_summary is not None and parent_summary.get("type") == "filterpane":
                for field in summary.get("listFields") or []:
                    fields = parent_summary.setdefault("listFields", [])
                    if field not in fields:
                        fields.append(field)
                continue
            linked = master_objects.get(summary.get("extendsId") or "")
            if linked and not (summary.get("dimensions") or summary.get("measures")):
                for key in ("dimensions", "measures", "alternativeDimensions",
                            "alternativeMeasures", "listFields", "calcCondition", "variables"):
                    if linked.get(key) and not summary.get(key):
                        summary[key] = linked[key]
                if not summary.get("title") and linked.get("title"):
                    summary["title"] = linked["title"]
                if linked.get("type") and summary.get("type") in (None, "", "object"):
                    summary["type"] = linked["type"]
            if parent_summary is not None:
                summary["container"] = parent_summary.get("title") or parent_id
            if summary.get("id"):
                by_id[summary["id"]] = summary
            objects.append(summary)
        return [
            o for o in objects
            if o.get("type") not in _LAYOUT_ONLY_TYPES
            and (o.get("dimensions") or o.get("measures") or o.get("listFields") or o.get("switch"))
        ][: self._MAX_OBJECTS_PER_SHEET]

    def _dynamic_context(
        self,
        payload: Dict[str, Any],
        sheet_objects: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Everything that lets a user switch what a chart shows, app-wide.

        * `variable_choices` — per variable, the values buttons and variable
          inputs on any sheet can set it to (a switch on one sheet drives
          charts on others);
        * `islands` — per field of a data-island table, its rows and the field
          users select in (the one shown in a filter pane or set by a button);
          a button that selects values in an ordinary field counts as a
          one-column island of those values;
        * `known_variables` — names an expression may use bare (`vMetric='x'`);
        * `current` — each variable's current definition.
        """
        variable_choices: Dict[str, List[Dict[str, Any]]] = {}
        field_choices: Dict[str, List[Any]] = {}
        user_selected: set = set()
        for summary in sheet_objects:
            user_selected.update(summary.get("listFields") or [])
            for entry in (summary.get("switch") or {}).get("sets") or []:
                if entry.get("variable") and entry.get("value") not in (None, ""):
                    bucket = variable_choices.setdefault(entry["variable"], [])
                    if not any(str(c["value"]) == str(entry["value"]) for c in bucket):
                        bucket.append({"value": entry["value"], "label": entry.get("label")})
                elif entry.get("field"):
                    user_selected.add(entry["field"])
                    if entry.get("value") not in (None, ""):
                        values = field_choices.setdefault(entry["field"], [])
                        if entry["value"] not in values:
                            values.append(entry["value"])

        # Fields whose value is spliced in as a formula (`$(=Only(MetricExpr))`)
        # hold formula text; users select in the island's other field.
        formula_fields: set = set()
        for text in self._expression_texts(payload):
            for body in dollar_formulas(text):
                formula_fields.update(selector_fields(body))

        islands: Dict[str, Dict[str, Any]] = {}
        for name, island in (payload.get("islands") or {}).items():
            fields = island.get("fields") or []
            rows = island.get("rows") or []
            if not fields or not rows:
                continue
            selector = (
                next((f for f in fields if f in user_selected), None)
                or next((f for f in fields if f not in formula_fields), None)
                or fields[0]
            )
            entry = {"name": name, "selector": selector, "rows": rows}
            for field in fields:
                islands[field] = entry
        for field, values in field_choices.items():
            if field not in islands:
                islands[field] = {
                    "name": f"switch:{field}",
                    "selector": field,
                    "rows": [{field: v} for v in values],
                }

        current: Dict[str, str] = {}
        for item in payload.get("variables") or []:
            name = item.get("qName") or (item.get("qMeta") or {}).get("title")
            if name:
                current[name] = str((item.get("qData") or {}).get("definition") or "")
        known = set(current) | set(variable_choices)
        for name, choices in expression_choices(self._expression_texts(payload), known).items():
            if not variable_choices.get(name):
                variable_choices[name] = choices
        return {
            "variable_choices": variable_choices,
            "islands": islands,
            "known_variables": known,
            "current": current,
        }

    def _describe_dynamic(self, summary: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        """Description and options of one object, given the app's switches."""
        base = describe_object(summary, self._MAX_OBJECT_DESCRIPTION_CHARS)
        if summary.get("switch"):
            sets = summary["switch"].get("sets") or []
            parts = []
            for entry in sets:
                target = entry.get("variable") or entry.get("field")
                if entry.get("valueExpression"):
                    parts.append(f"{target} uses expression {entry['valueExpression']!r} (choice not enumerated)")
                    continue
                if entry.get("freeInput"):
                    parts.append(f"{target} (free input)")
                    continue
                label = f" ({entry['label']})" if entry.get("label") else ""
                parts.append(f"{target}={entry.get('value')!r}{label}")
            base = (
                f"switch id={summary.get('id')}; a user choice, not data: sets " + ", ".join(parts)
                + ". Charts that read it are described with one formula per choice."
            )
            return base, []
        try:
            options = object_options(
                summary, context["variable_choices"], context["islands"], context["known_variables"],
                variable_definitions=context["current"],
            )
        except ValueError as exc:
            return base + "; dynamic choices unresolved: " + str(exc), []
        drivers = summary_drivers(summary, context["known_variables"])
        notes = []
        for variable in drivers["variables"]:
            if variable in context["current"] and not context["variable_choices"].get(variable):
                definition = context["current"][variable]
                notes.append(f"{variable} is currently {definition!r}" if len(definition) <= 200
                             else f"{variable}: full definition in Master Items")
        text = base
        if notes:
            text += "; depends on " + "; ".join(notes)
        if options:
            text += "; " + describe_options(options, summary.get("id") or "")
            return text, options
        return text, options

    def _build_sheet_tables(
        self,
        app: Dict[str, Any],
        payload: Dict[str, Any],
        taken_names: set,
    ) -> List[Table]:
        """One catalog entry per shared sheet, one column per object on it.

        A sheet is the analysis an app's authors actually built; its charts
        carry the expressions the business reads every day. Recording each
        chart's dimensions and measures lets the agent reuse that exact logic,
        or read the chart itself with `execute_query(object_id=...)`.

        A chart whose measure follows a user choice (a button, a variable
        input, a selection in a data island) is described once per choice,
        with the formula and visible columns that choice gives it.
        """
        trees = payload.get("sheet_trees") or {}
        if not trees:
            return []
        app_key = self._app_key(app)
        app_meta = self._app_meta(app, payload)
        index = self._master_index(payload)
        master_objects = {
            object_id: summarize_object_properties(props, index)
            for object_id, props in (payload.get("master_object_props") or {}).items()
        }

        sheets: List[Tuple[Dict[str, Any], str, List[Dict[str, Any]]]] = []
        for item in (payload.get("sheets") or [])[: self._MAX_SHEETS]:
            sheet_id = (item.get("qInfo") or {}).get("qId")
            tree = trees.get(sheet_id) if sheet_id else None
            if not tree:
                continue
            objects = self._sheet_objects(tree, index, master_objects)
            if objects:
                sheets.append((item, sheet_id, objects))
        context = self._dynamic_context(payload, [o for _, _, objs in sheets for o in objs])
        payload["_dynamic_context"] = context

        tables: List[Table] = []
        taken = set(taken_names)
        for item, sheet_id, objects in sheets:
            data = item.get("qData") or {}
            meta_def = item.get("qMeta") or {}
            title = data.get("title") or meta_def.get("title") or sheet_id

            columns: List[TableColumn] = []
            used: set = set()
            for summary in objects:
                name = summary.get("title") or (
                    f"Filter pane: {', '.join(summary['listFields'])}"
                    if summary.get("type") == "filterpane" and summary.get("listFields")
                    else f"{summary.get('type')} {summary.get('id')}"
                )
                if name in used:
                    name = f"{name} ({summary.get('id')})"
                used.add(name)
                description, options = self._describe_dynamic(summary, context)
                meta = self._object_column_meta(summary)
                if options:
                    meta["options"] = options
                if summary.get("switch"):
                    meta["role"] = "switch"
                    meta["sets"] = summary["switch"].get("sets")
                columns.append(TableColumn(
                    name=name,
                    dtype=str(summary.get("type") or "object"),
                    description=_mask_secrets(description),
                    metadata=meta,
                ))

            prefix = f"{app_key}/{_SHEET_TABLE_SEGMENT}" if app_key else _SHEET_TABLE_SEGMENT
            name = f"{prefix}/{title}"
            if name in taken:
                name = f"{name} ({sheet_id})"
            taken.add(name)

            meta = dict(app_meta)
            description = (data.get("description") or meta_def.get("description") or "")[:300]
            meta.update(_compact({
                "objectType": "sheet",
                "sheetId": sheet_id,
                "sheetTitle": title,
                "sheetDescription": description or None,
                "objectCount": len(columns),
                "dynamicObjectCount": sum(1 for c in columns if (c.metadata or {}).get("options")),
                "approved": meta_def.get("approved"),
                "sheetUrl": (
                    f"{self._app_meta(app).get('webUrl')}/sheet/{sheet_id}"
                    if self.host and app.get("id") else None
                ),
            }))
            tables.append(Table(
                name=name,
                description=(
                    f"Sheet '{title}' of the Qlik app '{app.get('name')}'. Not a physical table: each "
                    "column is an object on the sheet with what it groups by and computes. Reuse an "
                    "object's expressions in execute_query(measures=[...]), or read the object's own "
                    "data with execute_query(app=..., object_id=...)."
                ),
                columns=columns,
                pks=[],
                fks=[],
                is_active=True,
                metadata_json={self._metadata_namespace: meta},
            ))
        return tables

    def _execute_engine_query(
        self,
        app: Optional[str] = None,
        dimensions: Optional[List[str]] = None,
        measures: Optional[List[Dict[str, str]]] = None,
        filters: Optional[Dict[str, List[Any]]] = None,
        max_rows: int = 10000,
        table_name: Optional[str] = None,
        object_id: Optional[str] = None,
        search: Optional[str] = None,
        fields: Optional[List[str]] = None,
        variables: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> pd.DataFrame:
        """Run one Engine query against one app and return a DataFrame.

        Three modes, all through this one entry point so every call is timed,
        captured and metered the same way:

        * hypercube (default) — ``dimensions`` × ``measures``. Measures and
          dimensions are checked by the Engine first, so a misspelled field is
          an error instead of a column of zeros.
        * ``object_id`` — the data of an existing sheet object or master
          visualization, computed from the object's own definition (its
          dimensions, measures, sorting and suppression), so the numbers match
          what the chart shows. ``filters`` still apply.
        * ``search`` — which fields hold a value and how it is spelled there.
          Returns ``field`` / ``value`` rows. ``fields`` limits the search.

        Args:
            app: "Stream/AppName", "Stream/AppName/Table" or a raw app GUID.
                Alias: ``table_name``.
            dimensions: Qlik field names to group by.
            measures: ``{"expr": "Sum([Sales])", "alias": "Total"}`` dicts.
            filters: ``{field: [values]}``, applied as Qlik selections before
                the cube is built, so they propagate associatively. Values
                must be non-empty lists of strings or finite numbers.
            max_rows: row cap; paging is handled internally.
            object_id: id of a sheet object or master visualization.
            search: text to look up across field values.
            fields: optional field names to restrict ``search`` to.
            variables: ``{name: value}`` — run as if a Qlik variable held this
                value, the way a dynamic chart's switch sets it. Applied to
                this query's expressions only (``$(v)`` and bare ``v``
                references, then ``Pick``/``If`` branches collapsed); the
                app's variables are never changed, so other users are not
                affected.
        """
        target = app or table_name
        if not target:
            raise ValueError("app (or table_name) is required")
        dimensions = dimensions or []
        measures = measures or []
        if object_id and search is not None:
            raise ValueError("Pass either object_id or search, not both")
        if object_id:
            if dimensions or measures:
                raise ValueError(
                    "object_id reads the object's own dimensions and measures; "
                    "do not pass dimensions or measures with it"
                )
        elif search is not None:
            if not str(search).strip():
                raise ValueError("search requires a non-empty text")
            if dimensions or measures or filters:
                raise ValueError("search looks values up across fields; it takes no dimensions, measures or filters")
        elif not dimensions and not measures:
            raise ValueError("At least one dimension or measure is required")
        if variables is not None:
            if search is not None:
                raise ValueError("search takes no variables")
            if not isinstance(variables, dict) or not all(
                isinstance(k, str) and k and isinstance(v, (str, int, float)) and not isinstance(v, bool)
                for k, v in variables.items()
            ):
                raise ValueError("variables must be a dict of variable name to a text or number value")
        variables = variables or {}

        app_id, app_row = self._resolve_app(target)
        user = self._acting_user(app_row)
        selections = list((filters or {}).items())
        if object_id:
            return asyncio.run(
                self._execute_object(app_id, user, selections, str(object_id), max_rows, variables)
            )
        if search is not None:
            limit = max(1, min(int(max_rows or self._MAX_SEARCH_RESULTS), self._MAX_SEARCH_RESULTS))
            return asyncio.run(
                self._execute_search(app_id, user, str(search).strip(), list(fields or []), limit)
            )
        hypercube_def = build_hypercube_def(dimensions, measures, max_rows)
        return asyncio.run(
            self._execute_engine_hypercube(app_id, user, selections, hypercube_def, max_rows, variables)
        )

    @staticmethod
    async def _check_cube_expressions(
        qix: QixSession,
        doc_handle: int,
        hypercube_def: Dict[str, Any],
    ) -> None:
        """Ask the Engine to validate every dimension and measure first.

        Qlik evaluates an unknown field name as a valid expression worth 0 (a
        dimension on a typo collapses into one null row), so a misspelling
        returns plausible-looking zeros instead of an error. `CheckExpression`
        names the unknown fields. It is advisory: if the Engine cannot run the
        check, the query goes ahead unchecked rather than failing.
        """
        measures = hypercube_def.get("qMeasures") or []
        labels = [
            (m.get("qDef") or {}).get("qLabel") for m in measures if (m.get("qDef") or {}).get("qLabel")
        ]
        checks: List[Tuple[str, str]] = []
        for m in measures:
            expr = (m.get("qDef") or {}).get("qDef") or ""
            if expr.strip():
                checks.append((f"measure {expr!r}", expr))
        for d in hypercube_def.get("qDimensions") or []:
            field_defs = (d.get("qDef") or {}).get("qFieldDefs") or []
            field = field_defs[0] if field_defs else ""
            if not field:
                continue
            if field.startswith("="):
                checks.append((f"calculated dimension {field!r}", field[1:]))
            else:
                bare = field[1:-1] if field.startswith("[") and field.endswith("]") else field
                checks.append((f"dimension {field!r}", "Count([" + bare.replace("]", "]]") + "])"))

        problems: List[str] = []
        for what, expr in checks:
            try:
                result = await qix.rpc(doc_handle, "CheckExpression", [expr, labels])
            except Exception as e:
                logger.debug("Qlik CheckExpression unavailable, query runs unchecked: %s", e)
                return
            error = (result.get("qErrorMsg") or "").strip()
            bad = []
            for rng in result.get("qBadFieldNames") or []:
                start, count = rng.get("qFrom"), rng.get("qCount")
                if isinstance(start, int) and isinstance(count, int) and count > 0:
                    bad.append(expr[start:start + count])
            if bad:
                problems.append(f"{what}: unknown field(s) " + ", ".join(bad))
            elif error:
                problems.append(f"{what}: {error}")
        if problems:
            raise ValueError(
                "Qlik rejected the query before running it — " + "; ".join(problems)
                + ". Field names are case-sensitive and must match the catalog exactly "
                "(wrap names with spaces in [brackets]). Use execute_query(app=..., search=...) "
                "to find which field holds a value."
            )

    async def _open_doc(self, qix: QixSession, app_id: str) -> int:
        opendoc = await qix.rpc(-1, "OpenDoc", [app_id])
        doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
        if doc_handle is None:
            raise RuntimeError("Qlik OpenDoc did not return a document handle")
        return doc_handle

    async def _execute_engine_hypercube(
        self,
        app_id: str,
        user: str,
        selections: List,
        hypercube_def: Dict[str, Any],
        max_rows: int,
        variables: Optional[Dict[str, Any]] = None,
    ) -> pd.DataFrame:
        async with self._qix_session(app_id, user) as qix:
            doc_handle = await self._open_doc(qix, app_id)

            await apply_field_selections(qix, doc_handle, selections)
            if variables:
                await self._resolve_cube_variables(qix, doc_handle, hypercube_def, variables)
            await self._check_cube_expressions(qix, doc_handle, hypercube_def)

            session = await qix.rpc(doc_handle, "CreateSessionObject", [{
                "qInfo": {"qType": "bow-hypercube"},
                "qHyperCubeDef": hypercube_def,
            }])
            obj_handle = (session.get("qReturn") or {}).get("qHandle")
            if obj_handle is None:
                raise RuntimeError("Qlik CreateSessionObject did not return a handle")

            width = len(hypercube_def.get("qDimensions") or []) + len(
                hypercube_def.get("qMeasures") or []
            )
            rows = await fetch_hypercube_pages(qix, obj_handle, width, max_rows)

        return hypercube_matrix_to_df(hypercube_def, rows)

    @staticmethod
    def _object_cube_def(props: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The hypercube an object computes, reshaped for flat paging.

        Charts keep their cube at `qHyperCubeDef`; a listbox has a list object,
        which is the same as a one-dimension cube. The copy is forced into
        straight-table mode (a pivot cannot be paged with GetHyperCubeData) and
        its column reordering is dropped so columns come back as dimensions
        then measures. Sorting, suppression and limits are kept — they are
        part of what the chart shows.
        """
        cube = props.get("qHyperCubeDef")
        if not isinstance(cube, dict):
            lst = props.get("qListObjectDef")
            if not isinstance(lst, dict):
                return None
            dim: Dict[str, Any] = {"qNullSuppression": True}
            if lst.get("qLibraryId"):
                dim["qLibraryId"] = lst["qLibraryId"]
            else:
                dim["qDef"] = {"qFieldDefs": list((lst.get("qDef") or {}).get("qFieldDefs") or [])}
            cube = {"qDimensions": [dim], "qMeasures": []}
            if lst.get("qStateName"):
                cube["qStateName"] = lst["qStateName"]
        cube = copy.deepcopy(cube)
        cube["qMode"] = "S"
        cube["qInitialDataFetch"] = []
        cube["qColumnOrder"] = []
        width = len(cube.get("qDimensions") or []) + len(cube.get("qMeasures") or [])
        if len(cube.get("qInterColumnSortOrder") or []) != width:
            cube.pop("qInterColumnSortOrder", None)
        return cube

    async def _execute_object(
        self,
        app_id: str,
        user: str,
        selections: List,
        object_id: str,
        max_rows: int,
        variables: Optional[Dict[str, Any]] = None,
    ) -> pd.DataFrame:
        async with self._qix_session(app_id, user) as qix:
            doc_handle = await self._open_doc(qix, app_id)
            await apply_field_selections(qix, doc_handle, selections)

            try:
                handle = await self._object_handle(qix, doc_handle, object_id)
            except RuntimeError as e:
                raise ValueError(f"Qlik object {object_id!r} was not found in this app: {e}")
            if handle is None:
                raise ValueError(f"Qlik object {object_id!r} was not found in this app")

            # Effective properties include what a linked master visualization
            # contributes; plain properties are the fallback for older Engines.
            try:
                props = (await qix.rpc(handle, "GetEffectiveProperties", [])).get("qProp") or {}
            except RuntimeError:
                props = (await qix.rpc(handle, "GetProperties", [])).get("qProp") or {}
            if props.get("qExtendsId") and "qHyperCubeDef" not in props:
                master = await self._object_handle(qix, doc_handle, props["qExtendsId"])
                if master is not None:
                    props = (await qix.rpc(master, "GetProperties", [])).get("qProp") or props

            cube = self._object_cube_def(props)
            if cube is None:
                kind = props.get("visualization") or (props.get("qInfo") or {}).get("qType") or "object"
                children: List[str] = []
                try:
                    infos = (await qix.rpc(handle, "GetChildInfos", [])).get("qInfos") or []
                    children = [f"{i.get('qId')} ({i.get('qType')})" for i in infos if i.get("qId")]
                except RuntimeError:
                    pass
                hint = f" Query one of its child objects instead: {', '.join(children)}." if children else ""
                raise ValueError(f"Qlik object {object_id!r} is a {kind} and holds no data of its own.{hint}")

            if variables:
                await self._inline_library_items(qix, doc_handle, cube)
                await self._resolve_cube_variables(qix, doc_handle, cube, variables)
            hidden = await self._apply_column_conditions(qix, doc_handle, cube)
            if not (cube.get("qDimensions") or cube.get("qMeasures")):
                raise ValueError(
                    f"Qlik object {object_id!r} shows no columns under these choices "
                    f"(hidden: {', '.join(hidden) or 'all'})"
                )

            session = await qix.rpc(doc_handle, "CreateSessionObject", [{
                "qInfo": {"qType": "bow-object-data"},
                "qHyperCubeDef": cube,
            }])
            obj_handle = (session.get("qReturn") or {}).get("qHandle")
            if obj_handle is None:
                raise RuntimeError("Qlik CreateSessionObject did not return a handle")
            layout = (await qix.rpc(obj_handle, "GetLayout", [])).get("qLayout") or {}
            info = layout.get("qHyperCube") or {}
            if info.get("qError"):
                raise RuntimeError(f"Qlik could not calculate object {object_id!r}: {info['qError']}")
            calc_msg = info.get("qCalcCondMsg")
            if calc_msg and not (info.get("qSize") or {}).get("qcy"):
                raise RuntimeError(
                    f"Qlik object {object_id!r} is not calculated under the current selections: {calc_msg}"
                )

            def titles(infos: List[Dict[str, Any]], defs: List[Dict[str, Any]], is_measure: bool) -> List[str]:
                out = []
                for i, d in enumerate(defs):
                    title = (infos[i].get("qFallbackTitle") if i < len(infos) else "") or ""
                    if not title:
                        qdef = d.get("qDef") or {}
                        if is_measure:
                            title = qdef.get("qLabel") or qdef.get("qDef") or "measure"
                        else:
                            title = ((qdef.get("qFieldLabels") or [""])[0]
                                     or (qdef.get("qFieldDefs") or ["dimension"])[0])
                    out.append(title)
                return out

            dim_titles = titles(info.get("qDimensionInfo") or [], cube.get("qDimensions") or [], False)
            meas_titles = titles(info.get("qMeasureInfo") or [], cube.get("qMeasures") or [], True)
            width = len(dim_titles) + len(meas_titles)
            rows = await fetch_hypercube_pages(qix, obj_handle, width, max_rows)

        shaped = {
            "qDimensions": [{"qDef": {"qFieldDefs": [t]}} for t in dim_titles],
            "qMeasures": [{"qDef": {"qLabel": t}} for t in meas_titles],
        }
        df = hypercube_matrix_to_df(shaped, rows)
        total = (info.get("qSize") or {}).get("qcy")
        if isinstance(total, int):
            df.attrs["qlik_total_rows"] = total
        if hidden:
            df.attrs["qlik_hidden_columns"] = hidden
        if variables:
            df.attrs["qlik_variables"] = dict(variables)
        return df

    @staticmethod
    async def _inline_library_items(qix: QixSession, doc_handle: int, cube: Dict[str, Any]) -> None:
        """Replace master-item references with their definitions.

        A variable override is applied to expression text, and a master
        measure's text lives in the library, not in the chart. Inlining it
        (label, sort and column condition kept) lets the override reach it.
        """
        for key, getter, prop_key in (
            ("qMeasures", "GetMeasure", "qMeasure"),
            ("qDimensions", "GetDimension", "qDim"),
        ):
            for item in cube.get(key) or []:
                library_id = item.get("qLibraryId")
                if not library_id:
                    continue
                try:
                    found = await qix.rpc(doc_handle, getter, [library_id])
                    handle = (found.get("qReturn") or {}).get("qHandle")
                    if handle is None:
                        continue
                    props = (await qix.rpc(handle, "GetProperties", [])).get("qProp") or {}
                except RuntimeError as e:
                    logger.debug("Qlik library item %s unreadable: %s", library_id, e)
                    continue
                definition = props.get(prop_key) or {}
                title = (props.get("qMetaDef") or {}).get("title") or ""
                qdef = dict(item.get("qDef") or {})
                if key == "qMeasures":
                    qdef["qDef"] = definition.get("qDef") or ""
                    qdef["qLabel"] = qdef.get("qLabel") or definition.get("qLabel") or title
                    if definition.get("qLabelExpression") and not qdef.get("qLabelExpression"):
                        qdef["qLabelExpression"] = definition["qLabelExpression"]
                else:
                    qdef["qFieldDefs"] = list(definition.get("qFieldDefs") or [])
                    qdef["qFieldLabels"] = list(definition.get("qFieldLabels") or []) or [title]
                item["qDef"] = qdef
                item.pop("qLibraryId", None)

    async def _resolve_cube_variables(
        self, qix: QixSession, doc_handle: int, cube: Dict[str, Any], variables: Dict[str, Any],
    ) -> None:
        items = await self._session_list(qix, doc_handle, "qVariableList", self._VARIABLE_LIST_DEF)
        definitions = self._variable_definitions(items)
        expressions = []

        def collect(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("qDef", "qExpression", "qLabelExpression", "qv") and isinstance(value, str):
                        expressions.append(value)
                    elif key == "qFieldDefs" and isinstance(value, list):
                        expressions.extend(v for v in value if isinstance(v, str) and v.startswith("="))
                    else:
                        collect(value)
            elif isinstance(node, list):
                for value in node:
                    collect(value)

        collect(cube)
        dependencies = variable_dependencies(expressions, {**definitions, **{k: "" for k in variables}})
        unused = set(variables) - set(dependencies)
        if unused:
            raise ValueError("Qlik variable overrides do not affect this query: " + ", ".join(sorted(unused)))
        self._apply_variables(cube, variables, definitions)

    @staticmethod
    def _variable_definitions(items: List[Dict[str, Any]]) -> Dict[str, str]:
        return {
            item["qName"]: str(item.get("qDefinition", (item.get("qData") or {}).get("definition", "")) or "")
            for item in items if item.get("qName")
        }

    @staticmethod
    def _apply_variables(cube: Dict[str, Any], variables: Dict[str, Any], definitions: Optional[Dict[str, str]] = None) -> None:
        """Substitute variable values into every expression of a cube."""
        def resolve(text: Any) -> Any:
            return resolve_expression(text, variables, variable_definitions=definitions) if isinstance(text, str) else text

        def resolve_label_expression(qdef: Dict[str, Any], label_key: str) -> None:
            expression = qdef.get("qLabelExpression")
            if isinstance(expression, str) and expression:
                resolved = resolve(expression)
                text = literal_text(resolved)
                if text is not None:
                    qdef[label_key] = [text] if label_key == "qFieldLabels" else text
                    qdef.pop("qLabelExpression", None)
                else:
                    qdef["qLabelExpression"] = resolved

        def resolve_nested(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("qExpression", "qv") and isinstance(value, str):
                        node[key] = resolve(value)
                    else:
                        resolve_nested(value)
            elif isinstance(node, list):
                for value in node:
                    resolve_nested(value)

        resolve_nested(cube)
        for item in cube.get("qMeasures") or []:
            qdef = item.get("qDef") or {}
            qdef["qDef"] = resolve(qdef.get("qDef") or "")
            resolve_label_expression(qdef, "qLabel")
        for item in cube.get("qDimensions") or []:
            qdef = item.get("qDef") or {}
            qdef["qFieldDefs"] = [
                ("=" + resolve(f[1:])) if isinstance(f, str) and f.startswith("=") else f
                for f in qdef.get("qFieldDefs") or []
            ]
            resolve_label_expression(qdef, "qFieldLabels")
        for item in (cube.get("qMeasures") or []) + (cube.get("qDimensions") or []):
            condition = item.get("qCalcCondition") or {}
            if isinstance(condition.get("qCond"), dict) and isinstance(condition["qCond"].get("qv"), str):
                condition["qCond"]["qv"] = resolve(condition["qCond"]["qv"])
            if isinstance(item.get("qCalcCond"), dict) and isinstance(item["qCalcCond"].get("qv"), str):
                item["qCalcCond"]["qv"] = resolve(item["qCalcCond"]["qv"])
        cube_condition = (cube.get("qCalcCondition") or {}).get("qCond")
        if isinstance(cube_condition, dict) and isinstance(cube_condition.get("qv"), str):
            cube_condition["qv"] = resolve(cube_condition["qv"])

    @staticmethod
    async def _apply_column_conditions(qix: QixSession, doc_handle: int, cube: Dict[str, Any]) -> List[str]:
        """Drop the columns a "show column if" condition hides; return their labels.

        A condition decided by literals (after a variable override) is decided
        here. The rest are evaluated by the Engine, under the query's
        selections, as one measures-only cube. A condition the Engine cannot
        evaluate keeps its column: hiding data on a guess is worse than
        showing one column too many.
        """
        entries: List[Tuple[str, int, str, str]] = []
        for key in ("qDimensions", "qMeasures"):
            for position, item in enumerate(cube.get(key) or []):
                condition = (
                    ((item.get("qCalcCondition") or {}).get("qCond") or {}).get("qv")
                    or (item.get("qCalcCond") or {}).get("qv")
                )
                if isinstance(condition, str) and condition.strip():
                    qdef = item.get("qDef") or {}
                    label = (
                        qdef.get("qLabel") or (qdef.get("qFieldLabels") or [""])[0]
                        or (qdef.get("qFieldDefs") or [""])[0] or qdef.get("qDef") or key
                    )
                    entries.append((key, position, condition.strip(), label))
        if not entries:
            return []

        shown: Dict[Tuple[str, int], bool] = {}
        pending = []
        for key, position, condition, _label in entries:
            decided = decide_condition(condition)
            if decided is None:
                pending.append((key, position, condition))
            else:
                shown[(key, position)] = decided
        if pending:
            try:
                check = await qix.rpc(doc_handle, "CreateSessionObject", [{
                    "qInfo": {"qType": "bow-column-conditions"},
                    "qHyperCubeDef": build_hypercube_def(
                        [], [{"expr": c.lstrip("="), "alias": f"c{i}"} for i, (_, _, c) in enumerate(pending)], 1,
                    ),
                }])
                handle = (check.get("qReturn") or {}).get("qHandle")
                row = (await fetch_hypercube_pages(qix, handle, len(pending), 1) or [[]])[0] if handle is not None else []
            except RuntimeError as e:
                logger.debug("Qlik column conditions not evaluated: %s", e)
                row = []
            for i, (key, position, _condition) in enumerate(pending):
                value = row[i].get("qNum") if i < len(row) and isinstance(row[i], dict) else None
                shown[(key, position)] = not (isinstance(value, (int, float)) and value == 0)

        hidden: List[str] = []
        labels = {(k, p): label for k, p, _, label in entries}
        for key in ("qDimensions", "qMeasures"):
            kept = []
            for position, item in enumerate(cube.get(key) or []):
                if shown.get((key, position), True):
                    item.pop("qCalcCondition", None)
                    item.pop("qCalcCond", None)
                    kept.append(item)
                else:
                    hidden.append(labels[(key, position)])
            cube[key] = kept
        if hidden:
            # Sort order indexes columns by position; it no longer lines up.
            cube.pop("qInterColumnSortOrder", None)
        return hidden

    async def _execute_search(
        self,
        app_id: str,
        user: str,
        term: str,
        fields: List[str],
        limit: int,
    ) -> pd.DataFrame:
        options: Dict[str, Any] = {"qContext": "Cleared"}
        if fields:
            options["qSearchFields"] = fields
        page = {
            "qOffset": 0,
            "qCount": 10,
            "qMaxNbrFieldMatches": -1,
            "qGroupOptions": [{"qGroupType": "DatasetType", "qOffset": 0, "qCount": -1}],
            "qGroupItemOptions": [{"qGroupItemType": "Field", "qOffset": 0, "qCount": -1}],
        }
        async with self._qix_session(app_id, user) as qix:
            doc_handle = await self._open_doc(qix, app_id)
            result = await qix.rpc(doc_handle, "SearchResults", [options, [term], page])

        rows: List[Dict[str, Any]] = []
        for group in (result.get("qResult") or {}).get("qSearchGroupArray") or []:
            for item in group.get("qItems") or []:
                field = item.get("qIdentifier")
                if not field:
                    continue
                for match in item.get("qItemMatches") or []:
                    rows.append({
                        "field": field,
                        "value": match.get("qText"),
                        "field_match_count": item.get("qTotalNumberOfMatches"),
                    })
                    if len(rows) >= limit:
                        break
                if len(rows) >= limit:
                    break
        return pd.DataFrame(rows, columns=["field", "value", "field_match_count"])

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    def system_prompt(self) -> str:
        return """

## Qlik Sense Hypercube Query Guide

Each Qlik app is a self-contained associative model grouped into a space or stream. Queries are hypercubes:
dimensions (Qlik field names) crossed with measures (Qlik expressions).

### Schema Structure

Tables are named `SpaceOrStream/AppName/TableName`, or `AppName/TableName` for an app
that is not in a stream. Pass the full name — or just `SpaceOrStream/AppName` — to
`execute_query()` as the `app` argument.

Every app also exposes a `SpaceOrStream/AppName/Master Items` entry. It is NOT a
physical table. Its columns are the app's governed definitions, and each
column's description carries the definition itself:
- `dtype="measure"` — a master measure; the description ends with its Qlik
  expression.
- `dtype="dimension"` — a master dimension.
- `dtype="variable"` — a variable; reference it inside an expression as
  `$(vName)`. Its definition is in the description.
- any other dtype (`barchart`, `kpi`, ...) — a master visualization.

Each shared sheet is exposed as `SpaceOrStream/AppName/Sheets/<Sheet Title>`, also NOT
a physical table. Each column is one object on the sheet (`dtype` is the chart
type) and its description reads
`<type> id=<objectId>; dimensions: ...; measures: <label> = <expression>; ...`.
A measure shown as `master measure 'X'` is the master measure X.

Prefer, in this order:
1. The data of an existing object, when the question matches what a chart
   shows: `execute_query(app=..., object_id="<objectId>")` returns the chart's
   own numbers (same dimensions, measures, sorting and suppression).
2. The expression a sheet object or master measure already uses — it encodes
   the set analysis the app's authors agreed on, which a raw `Sum()` will not
   reproduce.
3. An expression you write yourself, only when nothing above fits.

#### Dynamic charts (the user switches what a chart shows)

Many charts change their measure when the user clicks a button, picks a value
in a variable input, or selects a value in a small selector field ("Sales
amount" / "Quantity"). Such a chart's description starts its options with
`DYNAMIC —` and lists one entry per choice:

`when vMetric='Quantity' (Units): by Region: Units = Sum([Sales Quantity]) [execute_query(object_id='abc', variables={"vMetric": "Quantity"})]`

- Pick the choice that matches the question and run exactly the call shown
  in brackets. Do not rebuild the formula yourself and do not run the
  chart's default: the default is only one of the choices.
- `variables={...}` applies a variable value to this query only (the app is
  not changed for anyone else). `filters={...}` selects the choice in its
  selector field, the same click a user makes.
- Columns marked `(shown only when ...)` appear only under that choice; the
  option lists already leave out the columns a choice hides.
- Objects whose dtype is a button or variable input are the switches
  themselves: they say which variable or field they set, and to what. They
  hold no data.
- `variables=` also works with ad-hoc `dimensions`/`measures` when you reuse
  a dynamic expression.

### How to Execute Queries

Signature: `execute_query(app, dimensions=None, measures=None, filters=None, max_rows=10000,
object_id=None, search=None, fields=None, variables=None)`

Filter values must retain their Qlik types: send numeric years/IDs as numbers,
and textual IDs (including leading zeros) as strings. Dual/date fields may
require verified numeric Qlik values rather than formatted date text; never
guess date serials or silently drop a filter when selection fails.

```python
# Total sales by region
df = client.execute_query(
    app="Sales/Consumer Sales",
    dimensions=["Region Name"],
    measures=[{"expr": "Sum([Sales Quantity] * [Sales Price])", "alias": "Revenue"}],
)

# Re-use a published master measure verbatim (set analysis included)
df = client.execute_query(
    app="Sales/Consumer Sales",
    dimensions=["Product Group Desc"],
    measures=[{"expr": "Sum({$<[Region Name]={'Northeast'}>} [Sales Quantity])",
               "alias": "Northeast Qty"}],
    max_rows=50,
)

# The numbers behind an existing chart (object id from a Sheets entry)
df = client.execute_query(app="Sales/Consumer Sales", object_id="kPqRsT", max_rows=500)

# Same chart, narrowed by a selection
df = client.execute_query(
    app="Sales/Consumer Sales", object_id="kPqRsT", filters={"Year": [2024]},
)

# A dynamic chart under one choice (copy the call from the DYNAMIC options)
df = client.execute_query(
    app="Sales/Consumer Sales", object_id="kPqRsT", variables={"vMetric": "Quantity"},
)

# Find which field holds a value and how it is spelled, before filtering on it
df = client.execute_query(app="Sales/Consumer Sales", search="northeast")
# -> columns: field, value, field_match_count
df = client.execute_query(app="Sales/Consumer Sales", search="north", fields=["Region Name"])

# Row count / profiling
df = client.execute_query(
    app="Sales/Consumer Sales",
    measures=[{"expr": "Count([Order Number])", "alias": "Orders"}],
)

# Read a table's rows — a cube always needs at least one dimension or measure,
# so list the fields you want as dimensions. Qlik returns distinct combinations,
# so include a key field when you need one row per record.
df = client.execute_query(
    app="Sales/Consumer Sales",
    dimensions=["Order Number", "Order Date", "Customer Number"],
    max_rows=1000,
)
```

### Key Query Rules

- For inspection, use the same hypercube API with `max_rows=3` and explicit
  dimensions or measures. Do not send SQL or infer that a connection/app error
  means the data is empty or the business logic is wrong.
- Preserve the full table path from `describe_tables` and its data source ID.
  `No active tables matched` is a BOW catalog-resolution error, not a Qlik data
  result: re-check the active catalog and source before one corrected retry.
  `create_data` uses this same connection; it cannot bypass an unavailable app.
- **Field names are case-sensitive** and must match the schema exactly. Wrap
  anything containing a space in square brackets: `Sum([Net Sales])`.
- **Measures are Qlik expressions, not SQL** — `Sum(...)`, `Count(distinct ...)`,
  `Avg(...)`, and set analysis `{$<Field={'Value'}>}`.
- **Filters are Qlik selections**, not a SQL `WHERE`: they narrow the associative
  state and propagate to every linked table automatically.
- **Look a value up before filtering on it** when you have not seen it in a
  result: a filter or set modifier on a value that does not exist yields zeros,
  not an error. `execute_query(app=..., search="text")` returns the matching
  field values with their exact spelling.
- Dimensions and measures are validated by the Engine before the cube runs; an
  error naming `unknown field(s)` means the name is wrong, not that data is
  missing. Re-check the catalog or search for the value.
- `object_id` takes no dimensions or measures; `search` takes no dimensions,
  measures or filters. An object that holds no data (a container, a text box)
  is reported with the ids of its children.
- **Do not write joins.** Fields associate through shared key fields, so name
  fields from several tables in one cube and Qlik resolves the path.
- A table whose entry is inactive with `status: empty` belongs to an app that
  has never been reloaded — it holds no data and cannot be queried.
- Results are a flat DataFrame of (dimensions..., measures...), capped at
  `max_rows`.
"""
