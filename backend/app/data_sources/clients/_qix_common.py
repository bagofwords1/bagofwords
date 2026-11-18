"""Shared Qlik Engine (QIX) plumbing.

The Engine API is the same JSON-RPC 2.0-over-WebSocket protocol on Qlik Cloud and
on Qlik Sense Enterprise on Windows; only the URL, the TLS setup and the auth
headers differ. Cloud sends ``Authorization: Bearer <token>`` to
``wss://tenant/app/{id}``; on-prem presents a client certificate to
``wss://host:4747/app/{id}`` and names the acting user in ``X-Qlik-User``.

Everything that is genuinely common lives here — the session/RPC loop, the
hypercube builder, the result-matrix decoder, and the two catalog mappers — so a
protocol-level fix lands once for both clients rather than twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from functools import lru_cache
from numbers import Real
import ssl
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session / RPC
# ---------------------------------------------------------------------------


class QixSession:
    """One Engine WebSocket, used as an async context manager.

    ``async with QixSession(url, headers=…, ssl_context=…) as qix:``
        ``result = await qix.rpc(-1, "OpenDoc", [app_id])``

    Handles are the Engine's object addressing scheme: ``-1`` is the global
    object, ``OpenDoc`` returns a document handle, and each session object
    (hypercube, measure list, …) returns its own.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[Sequence[Tuple[str, str]]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        timeout_sec: int = 30,
    ):
        self.url = url
        self.headers = list(headers or [])
        self.ssl_context = ssl_context
        self.timeout_sec = timeout_sec
        self._cm = None
        self._ws = None
        self._next_id = 1

    async def __aenter__(self) -> "QixSession":
        import websockets

        # `additional_headers`, NOT `extra_headers`: the legacy asyncio client
        # was removed in websockets 15 and the parameter renamed in 14. Passing
        # the old name does not raise a clean error — it falls through
        # `**kwargs` into `loop.create_connection()` and dies there with an
        # unrelated-looking TypeError.
        self._cm = websockets.connect(
            self.url,
            additional_headers=self.headers,
            ssl=self.ssl_context,
            open_timeout=self.timeout_sec,
            close_timeout=5,
        )
        self._ws = await self._cm.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._cm is not None:
            return await self._cm.__aexit__(exc_type, exc, tb)
        return False

    async def rpc(self, handle: int, method: str, params: Any) -> Dict[str, Any]:
        """Send one JSON-RPC request and return its ``result``.

        Reads until the reply carrying our own ``id`` arrives. The Engine
        interleaves unsolicited notifications on the same socket — ``OnConnected``
        on open, plus ``OnAuthenticationInformation`` and per-object change
        events — so treating the next frame as the answer desynchronises the
        session and every later call reads the previous call's reply.
        """
        request_id = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "handle": handle,
            "method": method,
            "params": params,
        }))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self.timeout_sec)
            msg = json.loads(raw)
            if msg.get("id") != request_id:
                continue  # notification or a stale frame — keep reading
            if "error" in msg:
                err = msg["error"] or {}
                raise RuntimeError(f"Qlik QIX error on {method}: {err.get('message') or err}")
            return msg.get("result") or {}


async def apply_field_selections(qix: QixSession, doc_handle: int, selections: List) -> None:
    """Select typed values on Field handles before building a cube.

    GetField is a Doc method; SelectValues is a Field method. Refusing empty,
    unsupported, rejected, or unapplied selections prevents returning
    unfiltered data. An RPC success flag alone does not prove a value matched.
    """
    for field, values in selections:
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"Qlik filter for {field!r} requires a non-empty list of values")
        field_values = []
        for value in values:
            if isinstance(value, str):
                field_values.append({"qText": value})
            elif isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value):
                field_values.append({"qIsNumeric": True, "qNumber": float(value)})
            else:
                raise ValueError(f"Qlik filter for {field!r} requires strings or finite numbers")
        result = await qix.rpc(doc_handle, "GetField", [field])
        field_handle = (result.get("qReturn") or {}).get("qHandle")
        if field_handle is None:
            raise RuntimeError(f"Qlik field {field!r} was not found")
        selected = await qix.rpc(field_handle, "SelectValues", [field_values, False, False])
        if selected.get("qReturn") is not True:
            raise RuntimeError(f"Qlik selection failed for field {field!r}; no query was executed")

    if not selections:
        return
    # Check after ALL selections: a later associative selection can clear an
    # earlier one. The session object lives only as long as this query's socket.
    result = await qix.rpc(doc_handle, "CreateSessionObject", [{
        "qInfo": {"qType": "SelectionCheck"}, "qSelectionObjectDef": {},
    }])
    handle = (result.get("qReturn") or {}).get("qHandle")
    if handle is None:
        raise RuntimeError("Qlik selections could not be verified; no query was executed")
    result = await qix.rpc(handle, "GetLayout", [])
    current = (result.get("qLayout") or {}).get("qSelectionObject") or {}
    selected_fields = {
        item.get("qField") for item in current.get("qSelections", [])
        if (item.get("qSelectedCount") or 0) > 0
    }
    for field, _ in selections:
        if field not in selected_fields:
            raise RuntimeError(
                f"Qlik filter for {field!r} has no selected values; no query was executed. "
                "Verify the field values and types. Numeric and dual/date fields may require "
                "their verified numeric values rather than display text."
            )


def build_ssl_context(
    *,
    verify_ssl: bool = True,
    ca_file: Optional[str] = None,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    key_password: Optional[str] = None,
) -> ssl.SSLContext:
    """Build the TLS context for an Engine WebSocket or a REST call.

    ``cert_file``/``key_file`` add the client certificate on-prem needs (mutual
    TLS). ``verify_ssl`` alone decides whether the server is verified; when it
    is on, ``ca_file`` (Qlik's own ``root.pem``) is the trust store to verify
    against. ``verify_ssl=False`` turns verification off even when a
    ``ca_file`` is given — the toggle the admin sees is the one that wins.
    """
    if verify_ssl:
        ctx = ssl.create_default_context(cafile=ca_file)
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if cert_file:
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file, password=key_password or None)
    return ctx


# ---------------------------------------------------------------------------
# Field tags → dtypes
# ---------------------------------------------------------------------------

_TAG_DTYPE_ORDER = [
    ("$key", "key"),
    ("$date", "date"),
    ("$timestamp", "timestamp"),
    ("$numeric", "numeric"),
    ("$integer", "integer"),
    ("$text", "text"),
    ("$ascii", "text"),
]


def tags_to_dtype(tags: Optional[List[str]]) -> str:
    tags = tags or []
    for tag, dtype in _TAG_DTYPE_ORDER:
        if tag in tags:
            return dtype
    return "unknown"


# ---------------------------------------------------------------------------
# Catalog mappers
# ---------------------------------------------------------------------------


def app_key_for(app: Dict[str, Any]) -> str:
    """"Container/App" prefix for a table name — space on Cloud, stream on-prem."""
    app_name = app.get("name") or app.get("id") or ""
    container = app.get("space_name") or ""
    return f"{container}/{app_name}" if container else app_name


def build_tables_from_rest_metadata(
    app: Dict[str, Any],
    metadata: Dict[str, Any],
    *,
    namespace: str = "qlik_sense",
) -> List[Table]:
    """Convert REST ``/apps/{id}/data/metadata`` into Table objects.

    Served by Qlik Cloud and — through the proxy — by QSEoW, in both cases from
    the post-reload snapshot, so it needs no Engine session and does not load the
    app into engine memory.
    """
    app_id = app.get("id")
    app_name = app.get("name") or app_id or ""
    space_name = app.get("space_name") or ""
    app_key = app_key_for(app)

    # Build an index: table name -> list of (field_name, tags)
    fields_by_table: Dict[str, List[Dict[str, Any]]] = {}
    for field in metadata.get("fields") or []:
        src_tables = field.get("srcTables") or []
        for t in src_tables:
            fields_by_table.setdefault(t, []).append({
                "name": field.get("name") or "",
                "tags": field.get("tags") or [],
                "is_key": "$key" in (field.get("tags") or []),
            })

    # Cross-table key fields become approximate relationships — every table
    # that shares a key field is related via that field. Build pks from $key.
    key_to_tables: Dict[str, List[str]] = {}
    for field in metadata.get("fields") or []:
        if "$key" in (field.get("tags") or []):
            for t in field.get("srcTables") or []:
                key_to_tables.setdefault(field.get("name") or "", []).append(t)

    tables: List[Table] = []
    for tbl in metadata.get("tables") or []:
        tbl_name = tbl.get("name") or ""
        if not tbl_name:
            continue
        full_name = f"{app_key}/{tbl_name}" if app_key else tbl_name

        columns: List[TableColumn] = []
        pks: List[TableColumn] = []
        for f in fields_by_table.get(tbl_name, []):
            col = TableColumn(
                name=f["name"],
                dtype=tags_to_dtype(f["tags"]),
                description=None,
                metadata={"qlik_tags": f["tags"]},
            )
            columns.append(col)
            if f["is_key"]:
                pks.append(col)

        fks: List[ForeignKey] = []
        for f in fields_by_table.get(tbl_name, []):
            if not f["is_key"]:
                continue
            other_tables = [t for t in key_to_tables.get(f["name"], []) if t != tbl_name]
            for other in other_tables:
                fks.append(ForeignKey(
                    column=TableColumn(name=f["name"], dtype=tags_to_dtype(f["tags"])),
                    references_name=(f"{app_key}/{other}" if app_key else other),
                    references_column=TableColumn(name=f["name"], dtype=tags_to_dtype(f["tags"])),
                ))

        tables.append(Table(
            name=full_name,
            description=None,
            columns=columns,
            pks=pks,
            fks=fks,
            is_active=True,
            metadata_json={
                namespace: {
                    "appId": app_id,
                    "appName": app_name,
                    "spaceId": app.get("space_id"),
                    "spaceName": space_name,
                    "tableName": tbl_name,
                    "rowCount": tbl.get("rows"),
                    "source": "rest_metadata",
                }
            },
        ))

    return tables


def build_tables_from_qix(
    app: Dict[str, Any],
    qtr: List[Dict[str, Any]],
    qk: List[Dict[str, Any]],
    *,
    namespace: str = "qlik_sense",
) -> List[Table]:
    """Convert a QIX ``GetTablesAndKeys`` result into Table objects.

    Qlik is associative rather than relational: ``qk`` describes which tables a
    key field links, with no direction, so each link is emitted as a foreign key
    from both sides.
    """
    app_id = app.get("id")
    app_name = app.get("name") or app_id or ""
    space_name = app.get("space_name") or ""
    app_key = app_key_for(app)

    # Index keys (association links) by table name
    keys_by_table: Dict[str, List[Dict[str, Any]]] = {}
    for key in qk or []:
        key_fields = key.get("qKeyFields") or []
        tables_for_key = key.get("qTables") or []
        for t in tables_for_key:
            keys_by_table.setdefault(t, []).append({
                "fields": key_fields,
                "other_tables": [o for o in tables_for_key if o != t],
            })

    tables: List[Table] = []
    for tbl in qtr or []:
        tbl_name = tbl.get("qName") or ""
        if not tbl_name:
            continue
        full_name = f"{app_key}/{tbl_name}" if app_key else tbl_name

        key_fields_set: set = set()
        for entry in keys_by_table.get(tbl_name, []):
            for kf in entry["fields"]:
                key_fields_set.add(kf)

        columns: List[TableColumn] = []
        pks: List[TableColumn] = []
        for field in tbl.get("qFields") or []:
            fname = field.get("qName") or ""
            if not fname:
                continue
            tags = field.get("qTags") or []
            col = TableColumn(
                name=fname,
                dtype=tags_to_dtype(tags),
                description=None,
                metadata={"qlik_tags": tags},
            )
            columns.append(col)
            if fname in key_fields_set:
                pks.append(col)

        fks: List[ForeignKey] = []
        for entry in keys_by_table.get(tbl_name, []):
            for kf in entry["fields"]:
                for other in entry["other_tables"]:
                    fks.append(ForeignKey(
                        column=TableColumn(name=kf, dtype="key"),
                        references_name=(f"{app_key}/{other}" if app_key else other),
                        references_column=TableColumn(name=kf, dtype="key"),
                    ))

        tables.append(Table(
            name=full_name,
            description=None,
            columns=columns,
            pks=pks,
            fks=fks,
            is_active=True,
            metadata_json={
                namespace: {
                    "appId": app_id,
                    "appName": app_name,
                    "spaceId": app.get("space_id"),
                    "spaceName": space_name,
                    "tableName": tbl_name,
                    "rowCount": tbl.get("qNoOfRows"),
                    "source": "qix",
                }
            },
        ))
    return tables


# ---------------------------------------------------------------------------
# Hypercubes
# ---------------------------------------------------------------------------


def build_hypercube_def(
    dimensions: List[str],
    measures: List[Dict[str, str]],
    max_rows: int,
) -> Dict[str, Any]:
    qdims = [{
        "qDef": {"qFieldDefs": [d]},
        "qNullSuppression": False,
    } for d in dimensions]
    qmeas = [{
        "qDef": {
            "qDef": (m.get("expr") if isinstance(m, dict) else str(m)) or "",
            "qLabel": (m.get("alias") if isinstance(m, dict) else "") or "",
        }
    } for m in measures]
    width = max(1, len(qdims) + len(qmeas))
    return {
        "qDimensions": qdims,
        "qMeasures": qmeas,
        # The Engine caps a single data page at 10 000 cells, so the initial
        # fetch height is derived from the cube's width rather than max_rows.
        "qInitialDataFetch": [{
            "qTop": 0,
            "qLeft": 0,
            "qHeight": min(max_rows, max(1, 10000 // width)),
            "qWidth": width,
        }],
        "qSuppressZero": False,
        "qSuppressMissing": False,
    }


def hypercube_matrix_to_df(
    hypercube_def: Dict[str, Any],
    rows: List[List[Dict[str, Any]]],
) -> pd.DataFrame:
    dim_defs = hypercube_def.get("qDimensions") or []
    meas_defs = hypercube_def.get("qMeasures") or []

    col_names: List[str] = []
    col_is_measure: List[bool] = []
    for d in dim_defs:
        field_defs = (d.get("qDef") or {}).get("qFieldDefs") or []
        col_names.append(field_defs[0] if field_defs else "dim")
        col_is_measure.append(False)
    for m in meas_defs:
        q_def = (m.get("qDef") or {})
        label = q_def.get("qLabel") or q_def.get("qDef") or "measure"
        col_names.append(label)
        col_is_measure.append(True)

    # De-dupe column names (append .1, .2 ... for collisions)
    seen: Dict[str, int] = {}
    unique_names: List[str] = []
    for name in col_names:
        if name in seen:
            seen[name] += 1
            unique_names.append(f"{name}.{seen[name]}")
        else:
            seen[name] = 0
            unique_names.append(name)

    parsed_rows: List[List[Any]] = []
    for row in rows:
        parsed: List[Any] = []
        for idx, cell in enumerate(row):
            if idx >= len(col_is_measure):
                parsed.append(cell.get("qText") if isinstance(cell, dict) else cell)
                continue
            if isinstance(cell, dict):
                qnum = cell.get("qNum")
                qtext = cell.get("qText")
                if col_is_measure[idx]:
                    if isinstance(qnum, (int, float)):
                        parsed.append(qnum)
                    else:
                        parsed.append(qtext)
                else:
                    parsed.append(qtext if qtext not in (None, "") else qnum)
            else:
                parsed.append(cell)
        parsed_rows.append(parsed)

    if not parsed_rows:
        return pd.DataFrame(columns=unique_names)
    return pd.DataFrame(parsed_rows, columns=unique_names)


async def fetch_hypercube_pages(
    qix: QixSession,
    obj_handle: int,
    width: int,
    max_rows: int,
) -> List[List[Dict[str, Any]]]:
    """Page ``GetHyperCubeData`` until ``max_rows`` or the cube is exhausted."""
    rows: List[List[Dict[str, Any]]] = []
    top = 0
    page_height = max(1, min(max_rows, 10000 // max(1, width)))
    while len(rows) < max_rows:
        height = min(page_height, max_rows - len(rows))
        page = await qix.rpc(obj_handle, "GetHyperCubeData", [
            "/qHyperCubeDef",
            [{"qTop": top, "qLeft": 0, "qWidth": width, "qHeight": height}],
        ])
        data_pages = page.get("qDataPages") or []
        if not data_pages:
            break
        matrix = data_pages[0].get("qMatrix") or []
        if not matrix:
            break
        rows.extend(matrix)
        if len(matrix) < height:
            break
        top += len(matrix)
    return rows


# ---------------------------------------------------------------------------
# Sheet objects (visualizations) — what a chart computes, in one line
# ---------------------------------------------------------------------------
#
# A sheet object stores its definition as generic-object properties: a chart
# carries a `qHyperCubeDef` (dimensions × measures), a listbox a
# `qListObjectDef`, a map one hypercube per layer. A measure is either written
# inline (`qDef.qDef`) or points at a master measure (`qLibraryId`). The parser
# below reads those shapes from `GetFullPropertyTree` / `GetProperties` output
# without assuming a visualization type, so an extension or a chart type added
# in a later Qlik release still yields its dimensions and measures.

# Property keys that hold alternatives (the "exploration" dims/measures a user
# can swap in) rather than what the object shows by default.
_ALTERNATIVES_KEY = "qLayoutExclude"
_MAX_PROPERTY_DEPTH = 8

# `$(vName)` and `$(=expr)`-style dollar expansions; only named ones are kept.
_VARIABLE_REF_RE = re.compile(r"\$\(\s*=?\s*([A-Za-z_][\w.]*)\s*\)")


def text_or_expression(value: Any) -> str:
    """A title/label as written: plain text, or the expression behind it.

    Titles and labels are either a string or a string expression
    (``{"qStringExpression": {"qExpr": "='Sales ' & Year"}}``); a calculation
    condition is a value expression (``{"qv": "..."}``).
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("qStringExpression", "qValueExpression"):
            inner = value.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("qExpr"), str):
                return inner["qExpr"].strip()
        for key in ("qv", "qExpr"):
            if isinstance(value.get(key), str):
                return value[key].strip()
    return ""


def _normalize_expression(expr: str) -> str:
    text = (expr or "").strip()
    if text.startswith("="):
        text = text[1:]
    return " ".join(text.split())


def variables_in_expression(expr: str) -> List[str]:
    """Names of the variables an expression expands, e.g. ``$(vCurrentYear)``."""
    seen: List[str] = []
    for name in _VARIABLE_REF_RE.findall(expr or ""):
        if name not in seen:
            seen.append(name)
    return seen


def _find_defs(node: Any, key: str, out: List[Dict[str, Any]], depth: int = 0) -> None:
    """Collect every ``key`` dict under ``node``, skipping alternatives."""
    if depth > _MAX_PROPERTY_DEPTH:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k == _ALTERNATIVES_KEY:
                continue
            if k == key and isinstance(v, dict):
                out.append(v)
            else:
                _find_defs(v, key, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _find_defs(item, key, out, depth + 1)


class MasterItemIndex:
    """Master measures/dimensions by id, and master measures by expression.

    Sheet objects reference master items by id (`qLibraryId`); resolving the id
    to the item's name lets the agent connect a chart to the governed
    definition. The expression index folds a chart's inline copy of a master
    measure into a reference instead of repeating the expression.
    """

    def __init__(
        self,
        measures: Optional[Dict[str, Dict[str, str]]] = None,
        dimensions: Optional[Dict[str, str]] = None,
    ):
        self.measures = measures or {}
        self.dimensions = dimensions or {}
        self._by_expression: Dict[str, str] = {}
        for item in self.measures.values():
            norm = _normalize_expression(item.get("expression") or "")
            if norm and item.get("name"):
                self._by_expression.setdefault(norm, item["name"])

    def measure_for_expression(self, expr: str) -> Optional[str]:
        return self._by_expression.get(_normalize_expression(expr))


def _column_condition(item: Dict[str, Any]) -> str:
    """A column's "show column if" condition (table and pivot columns)."""
    return (
        text_or_expression((item.get("qCalcCondition") or {}).get("qCond"))
        or text_or_expression(item.get("qCalcCond"))
    )


def _summarize_dimension(dim: Dict[str, Any], index: MasterItemIndex) -> Dict[str, Any]:
    qdef = dim.get("qDef") or {}
    fields = [f for f in (qdef.get("qFieldDefs") or []) if isinstance(f, str)]
    labels = [lbl for lbl in (qdef.get("qFieldLabels") or []) if isinstance(lbl, str)]
    # A label expression, when set, is what Qlik shows instead of the label.
    label = text_or_expression(qdef.get("qLabelExpression")) or (labels[0].strip() if labels and labels[0] else "")
    out: Dict[str, Any] = {}
    lib = dim.get("qLibraryId")
    if lib:
        out["master"] = index.dimensions.get(lib) or lib
    if fields:
        out["field"] = fields[0]
    if label:
        out["label"] = label
    condition = _column_condition(dim)
    if condition:
        out["showWhen"] = condition
    return out


def _summarize_measure(meas: Dict[str, Any], index: MasterItemIndex) -> Dict[str, Any]:
    qdef = meas.get("qDef") or {}
    expr = qdef.get("qDef") if isinstance(qdef.get("qDef"), str) else ""
    label = (qdef.get("qLabel") or "").strip() if isinstance(qdef.get("qLabel"), str) else ""
    # A label expression, when set, is what Qlik shows instead of the label.
    label = text_or_expression(qdef.get("qLabelExpression")) or label
    out: Dict[str, Any] = {}
    lib = meas.get("qLibraryId")
    if lib:
        master = index.measures.get(lib) or {}
        out["master"] = master.get("name") or lib
        if master.get("expression"):
            out["expression"] = master["expression"]
    elif expr:
        out["expression"] = expr.strip()
        same_as = index.measure_for_expression(expr)
        if same_as:
            out["master"] = same_as
    if label:
        out["label"] = label
    condition = _column_condition(meas)
    if condition:
        out["showWhen"] = condition
    return out


def summarize_object_properties(props: Dict[str, Any], index: Optional[MasterItemIndex] = None) -> Dict[str, Any]:
    """Reduce one generic object's properties to what it computes.

    Returns ``{id, type, title, dimensions, measures, alternativeDimensions,
    alternativeMeasures, listFields, calcCondition, state, extendsId,
    variables}`` with empty entries dropped. Works on any visualization type,
    including extensions, because it looks for hypercube/list-object
    definitions anywhere in the properties rather than at a fixed path.
    """
    index = index or MasterItemIndex()
    info = props.get("qInfo") or {}
    out: Dict[str, Any] = {
        "id": info.get("qId") or "",
        "type": props.get("visualization") or info.get("qType") or "object",
        "title": text_or_expression(props.get("title")),
    }
    subtitle = text_or_expression(props.get("subtitle"))
    if subtitle:
        out["subtitle"] = subtitle
    if props.get("qExtendsId"):
        out["extendsId"] = props["qExtendsId"]

    cubes: List[Dict[str, Any]] = []
    _find_defs(props, "qHyperCubeDef", cubes)
    dims: List[Dict[str, Any]] = []
    meas: List[Dict[str, Any]] = []
    conditions: List[str] = []
    for cube in cubes:
        dims.extend(_summarize_dimension(d, index) for d in (cube.get("qDimensions") or []) if isinstance(d, dict))
        meas.extend(_summarize_measure(m, index) for m in (cube.get("qMeasures") or []) if isinstance(m, dict))
        cond = text_or_expression((cube.get("qCalcCondition") or {}).get("qCond")) or text_or_expression(cube.get("qCalcCond"))
        if cond and cond not in conditions:
            conditions.append(cond)
        if cube.get("qStateName") and not out.get("state"):
            out["state"] = cube["qStateName"]
    out["dimensions"] = [d for d in dims if d]
    out["measures"] = [m for m in meas if m]

    alternatives = props.get(_ALTERNATIVES_KEY) or {}
    alt_cubes: List[Dict[str, Any]] = []
    if isinstance(alternatives, dict):
        _find_defs({"_": alternatives}, "qHyperCubeDef", alt_cubes)
    alt_dims: List[Dict[str, Any]] = []
    alt_meas: List[Dict[str, Any]] = []
    for cube in alt_cubes:
        alt_dims.extend(_summarize_dimension(d, index) for d in (cube.get("qDimensions") or []) if isinstance(d, dict))
        alt_meas.extend(_summarize_measure(m, index) for m in (cube.get("qMeasures") or []) if isinstance(m, dict))
    out["alternativeDimensions"] = [d for d in alt_dims if d]
    out["alternativeMeasures"] = [m for m in alt_meas if m]

    lists: List[Dict[str, Any]] = []
    _find_defs(props, "qListObjectDef", lists)
    list_fields: List[str] = []
    for lst in lists:
        lib = lst.get("qLibraryId")
        fields = (lst.get("qDef") or {}).get("qFieldDefs") or []
        name = (index.dimensions.get(lib) or lib) if lib else (fields[0] if fields else "")
        if name and name not in list_fields:
            list_fields.append(name)
    out["listFields"] = list_fields

    if conditions:
        out["calcCondition"] = conditions[0]
    if props.get("qStateName") and not out.get("state"):
        out["state"] = props["qStateName"]

    variables: List[str] = []
    for item in out["measures"] + out["alternativeMeasures"]:
        for name in variables_in_expression(item.get("expression") or ""):
            if name not in variables:
                variables.append(name)
    for item in out["dimensions"]:
        for name in variables_in_expression(item.get("field") or ""):
            if name not in variables:
                variables.append(name)
    for name in variables_in_expression(out.get("calcCondition") or ""):
        if name not in variables:
            variables.append(name)
    for item in out["measures"] + out["dimensions"]:
        for name in variables_in_expression(item.get("showWhen") or ""):
            if name not in variables:
                variables.append(name)
    out["variables"] = variables
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _format_dimension(d: Dict[str, Any]) -> str:
    text = _format_dimension_body(d)
    return f"{text} (shown only when {d['showWhen']})" if d.get("showWhen") else text


def _format_dimension_body(d: Dict[str, Any]) -> str:
    if d.get("master"):
        name = d["master"]
        label = d.get("label") or ""
        if label and label != name:
            return f"{label} = master dimension '{name}'"
        return f"master dimension '{name}'"
    field = d.get("field") or ""
    label = d.get("label") or ""
    return f"{label} = {field}" if label and label != field else field


def _format_measure(m: Dict[str, Any]) -> str:
    text = _format_measure_body(m)
    return f"{text} (shown only when {m['showWhen']})" if m.get("showWhen") else text


def _format_measure_body(m: Dict[str, Any]) -> str:
    label = m.get("label") or ""
    if m.get("master"):
        name = m["master"]
        return f"{label} = master measure '{name}'" if label and label != name else f"master measure '{name}'"
    expr = m.get("expression") or ""
    return f"{label} = {expr}" if label else expr


def describe_object(summary: Dict[str, Any], max_chars: int = 600) -> str:
    """One line a model can act on: id, type, what it groups by and computes."""
    parts = [f"{summary.get('type') or 'object'} id={summary.get('id') or '?'}"]
    if summary.get("dimensions"):
        parts.append("dimensions: " + ", ".join(_format_dimension(d) for d in summary["dimensions"]))
    if summary.get("measures"):
        parts.append("measures: " + "; ".join(_format_measure(m) for m in summary["measures"]))
    if summary.get("listFields"):
        parts.append("filter fields: " + ", ".join(summary["listFields"]))
    if summary.get("alternativeDimensions"):
        parts.append("alternative dimensions: " + ", ".join(_format_dimension(d) for d in summary["alternativeDimensions"]))
    if summary.get("alternativeMeasures"):
        parts.append("alternative measures: " + "; ".join(_format_measure(m) for m in summary["alternativeMeasures"]))
    if summary.get("calcCondition"):
        parts.append(f"calculated only when: {summary['calcCondition']}")
    if summary.get("state"):
        parts.append(f"alternate state: {summary['state']}")
    if summary.get("extendsId"):
        parts.append(f"linked to master visualization {summary['extendsId']}")
    text = "; ".join(parts)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def flatten_property_tree(entry: Dict[str, Any], depth: int = 0) -> List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """``GetFullPropertyTree`` → [(properties, parent properties)], depth first.

    The root (the sheet itself) is returned with parent ``None``; a container's
    tabs and a filter pane's listboxes come back with that object as parent.
    """
    out: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = []

    def walk(node: Dict[str, Any], parent: Optional[Dict[str, Any]], level: int) -> None:
        if level > _MAX_PROPERTY_DEPTH or not isinstance(node, dict):
            return
        props = node.get("qProperty") or {}
        out.append((props, parent))
        for child in node.get("qChildren") or []:
            walk(child, props, level + 1)

    walk(entry or {}, None, depth)
    return out


# ---------------------------------------------------------------------------
# Dynamic measures — what a chart computes under each user choice
# ---------------------------------------------------------------------------
#
# A "dynamic" chart lets the user switch what it shows: a button or variable
# input sets a variable (`vMetric`), or a selection in a small unlinked table
# (a data island, `MetricSelector`) picks a row. The chart's measure reads that
# choice: `Pick(Match(vMetric,'Sales','Qty'), Sum(A), Sum(B))`,
# `If(Only(Metric)='Qty', ...)`, `$(vMeasureExpr)` or `$(=Only(MetricExpr))`,
# and table columns carry "show column if" conditions on it.
#
# Read raw, such a measure names every branch but not which applies, so a model
# asked for "quantity" guesses. The helpers below substitute one concrete
# choice into the expression and collapse the branches that choice decides, so
# each option becomes an explicit formula. Substitution is text-level and local
# to this process: nothing is written to the Engine, so other users of the app
# never see a changed variable.

_IDENT_RE = re.compile(r"[A-Za-z_][\w.]*")


def qlik_literal(value: Any) -> str:
    """A value as a Qlik literal: numbers bare, text single-quoted."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(int(value)) if float(value).is_integer() else str(value)
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


def _scan(expr: str):
    """Yield (index, char, depth, quoted) with Qlik's quoting rules.

    Text inside '...', "...", [...] and `...` is quoted: commas, parentheses
    and names there are content, not structure.
    """
    depth = 0
    quote: Optional[str] = None
    closing = {"'": "'", '"': '"', "[": "]", "`": "`"}
    i = 0
    while i < len(expr):
        ch = expr[i]
        if quote is not None:
            yield i, ch, depth, True
            if ch == closing[quote]:
                # A doubled closing quote is an escaped quote, not an end.
                if i + 1 < len(expr) and expr[i + 1] == ch and quote in ("'", "]"):
                    yield i + 1, expr[i + 1], depth, True
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in closing:
            quote = ch
            yield i, ch, depth, True
            i += 1
            continue
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        yield i, ch, depth, False
        i += 1


def _call_args(expr: str, open_index: int) -> Optional[Tuple[List[str], int]]:
    """Arguments of the call whose '(' is at open_index, and the ')' index."""
    args: List[str] = []
    start = open_index + 1
    base = None
    for i, ch, depth, quoted in _scan(expr[open_index:]):
        i += open_index
        if base is None:
            base = depth  # depth after the opening '('
            continue
        if quoted:
            continue
        if ch == "," and depth == base:
            args.append(expr[start:i])
            start = i + 1
        elif ch == ")" and depth == base - 1:
            args.append(expr[start:i])
            return [a.strip() for a in args], i
    return None


def _find_calls(expr: str, names: Tuple[str, ...]) -> List[Tuple[int, int, str, List[str]]]:
    """(start, end, name, args) of every unquoted call to one of `names`."""
    out = []
    lowered = {n.lower() for n in names}
    quoted_at = {i for i, _, _, q in _scan(expr) if q}
    for m in _IDENT_RE.finditer(expr):
        if m.start() in quoted_at or m.group(0).lower() not in lowered:
            continue
        j = m.end()
        while j < len(expr) and expr[j].isspace():
            j += 1
        if j >= len(expr) or expr[j] != "(":
            continue
        parsed = _call_args(expr, j)
        if parsed is None:
            continue
        args, close = parsed
        out.append((m.start(), close + 1, m.group(0), args))
    return out


def _literal_value(text: str) -> Tuple[bool, Any]:
    """(is_literal, value) for a quoted string or a number."""
    t = (text or "").strip()
    # Drop wrapping parentheses only when they enclose the whole text.
    while t.startswith("("):
        parsed = _call_args(t, 0)
        if parsed is None or parsed[1] != len(t) - 1:
            break
        t = t[1:-1].strip()
    if re.fullmatch(r"'(?:[^']|'')*'", t):
        return True, t[1:-1].replace("''", "'")
    try:
        number = float(t)
    except ValueError:
        return False, None
    return True, number


def _same_literal(a: Any, b: Any, *, case_sensitive: bool) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            pass
    sa, sb = str(a), str(b)
    return sa == sb if case_sensitive else sa.lower() == sb.lower()


def _decide_comparison(text: str) -> Optional[bool]:
    """`'a' = 'b'`, `'a' <> 'b'` or a lone literal (non-zero = true), else None."""
    t = text.strip()
    for op in ("<>", "="):
        parts = []
        last = 0
        for i, _ch, depth, quoted in _scan(t):
            if quoted or depth != 0:
                continue
            if t.startswith(op, i) and not (op == "=" and i > 0 and t[i - 1] in "<>!"):
                parts.append(t[last:i])
                last = i + len(op)
        if len(parts) == 1:
            left_ok, left = _literal_value(parts[0])
            right_ok, right = _literal_value(t[last:])
            if left_ok and right_ok:
                equal = _same_literal(left, right, case_sensitive=False)
                return equal if op == "=" else not equal
            return None
    ok, value = _literal_value(t)
    if ok:
        try:
            return float(value) != 0
        except (TypeError, ValueError):
            return None
    return None


def _match_index(name: str, args: List[str]) -> Optional[int]:
    """1-based position Match/MixMatch/WildMatch returns, 0 for no match."""
    if len(args) < 2:
        return None
    ok, target = _literal_value(args[0])
    if not ok:
        return None
    for position, arg in enumerate(args[1:], start=1):
        arg_ok, candidate = _literal_value(arg)
        if not arg_ok:
            return None
        if name.lower() == "wildmatch":
            pattern = "^" + re.escape(str(candidate)).replace(r"\*", ".*").replace(r"\?", ".") + "$"
            if re.match(pattern, str(target), re.IGNORECASE):
                return position
        elif _same_literal(target, candidate, case_sensitive=name.lower() == "match"):
            return position
    return 0


@lru_cache(maxsize=2048)
def simplify_expression(expr: str, max_passes: int = 20) -> str:
    """Collapse branches whose deciding argument is a literal.

    `Pick(Match('Qty','Sales','Qty'), A, B)` → `B`; `If('Qty'='Sales', A, B)`
    → `B`. Anything not decided by literals is left exactly as written, so the
    Engine still evaluates it.
    """
    text = expr or ""
    for _ in range(max_passes):
        changed = False
        for start, end, name, args in reversed(_find_calls(text, ("Pick", "If", "Match", "MixMatch", "WildMatch"))):
            lname = name.lower()
            replacement = None
            if lname in ("match", "mixmatch", "wildmatch"):
                position = _match_index(lname, args)
                if position is not None:
                    replacement = str(position)
            elif lname == "pick" and len(args) >= 2:
                ok, index = _literal_value(args[0])
                if ok and isinstance(index, float) and index.is_integer() and 1 <= int(index) < len(args):
                    replacement = args[int(index)]
                elif ok and isinstance(index, float) and index == 0:
                    replacement = "Null()"
            elif lname == "if" and len(args) in (2, 3):
                decided = _decide_comparison(args[0])
                if decided is True:
                    replacement = args[1]
                elif decided is False:
                    replacement = args[2] if len(args) == 3 else "Null()"
            if replacement is not None:
                text = text[:start] + replacement + text[end:]
                changed = True
                break  # positions shifted; rescan
        if not changed:
            break
    return text


def substitute_expression(
    expr: str,
    variables: Optional[Dict[str, Any]] = None,
    field_values: Optional[Dict[str, Any]] = None,
) -> str:
    """Put concrete choices into an expression, as Qlik would see them.

    * `$(=Only(F))` / `$(=Only([F]))` → the raw text of F's chosen value (a
      formula kept in a data island is spliced in as a formula);
    * `$(v)` → v's value as raw text (dollar expansion is textual);
    * `Only(F)`, `GetFieldSelections(F)`, `MinString/MaxString(F)` → F's value
      as a literal;
    * a bare variable name → its value as a literal.
    """
    variables = variables or {}
    field_values = field_values or {}
    text = expr or ""

    def field_key(arg: str) -> Optional[str]:
        a = arg.strip()
        if a.startswith("[") and a.endswith("]"):
            a = a[1:-1].replace("]]", "]")
        return a if a in field_values else None

    def replace_dollar(m: re.Match) -> str:
        inner = m.group(1).strip()
        if inner.startswith("="):
            calls = _find_calls(inner[1:].strip(), ("Only", "MinString", "MaxString"))
            body = inner[1:].strip()
            if len(calls) == 1 and calls[0][0] == 0 and calls[0][1] == len(body) and len(calls[0][3]) == 1:
                key = field_key(calls[0][3][0])
                if key is not None:
                    return str(field_values[key])
            return m.group(0)
        if inner in variables:
            return str(variables[inner])
        return m.group(0)

    for _ in range(5):  # a variable may expand to another $(...)
        new = re.sub(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)", replace_dollar, text)
        if new == text:
            break
        text = new

    for start, end, _name, args in reversed(
        _find_calls(text, ("Only", "GetFieldSelections", "MinString", "MaxString"))
    ):
        if len(args) == 1:
            key = field_key(args[0])
            if key is not None:
                text = text[:start] + qlik_literal(field_values[key]) + text[end:]

    if variables:
        quoted_at = {i for i, _, _, q in _scan(text) if q}
        pieces = []
        last = 0
        for m in _IDENT_RE.finditer(text):
            if m.group(0) not in variables or m.start() in quoted_at:
                continue
            after = text[m.end():].lstrip()
            if after.startswith("("):
                continue  # a function call, not a variable
            before = text[:m.start()].rstrip()
            if before.endswith("$("):
                continue
            pieces.append(text[last:m.start()])
            pieces.append(qlik_literal(variables[m.group(0)]))
            last = m.end()
        pieces.append(text[last:])
        text = "".join(pieces)
    return text


@lru_cache(maxsize=4096)
def _direct_variable_dependencies(text: str, known: frozenset) -> Tuple[str, ...]:
    # A crawl resolves the same definitions for many charts and choices. Cache
    # parsing by text AND variable names; no app/session values are evaluated.
    names = expression_drivers(text, known)["variables"]
    for pattern in (r"\$\(\s*([^()]+?)\s*\)", r"\$\(\s*([^\s(),]+)\s*\("):
        for match in re.finditer(pattern, text):
            name = match.group(1).strip()
            if name in known and name not in names:
                names.append(name)
    return tuple(names)


def variable_dependencies(expressions: List[str], definitions: Dict[str, str], *, strict: bool = True) -> List[str]:
    """Transitive dependencies, including quoted dollar expansions and names with hyphens.

    Definitions are text, not values. Never evaluate an aggregate here: calculated
    variables must still be evaluated by Qlik in the query's selection context.
    """
    found: List[str] = []
    active: set = set()

    def visit(text: str, depth: int) -> None:
        if depth > 32:
            if strict:
                raise ValueError("Qlik variable dependency depth exceeds 32")
            return
        names = _direct_variable_dependencies(text, frozenset(definitions))
        for name in names:
            if name in active:
                if strict:
                    raise ValueError(f"Qlik variable dependency cycle at {name!r}")
                continue
            if name in found:
                continue
            found.append(name)
            if name in definitions:
                active.add(name)
                visit(definitions[name], depth + 1)
                active.remove(name)

    for expression in expressions:
        visit(expression or "", 0)
    return found


def expand_variable_dependencies(expr: str, definitions: Dict[str, str], overrides: Dict[str, Any]) -> str:
    """Inline only definitions affected by an override, without writing variables.

    An evaluated definition (=...) becomes $(=...), preserving Qlik's evaluation
    boundary. Other variables stay with the Engine, including parameterized
    macros and selection-dependent defaults. This also preserves quoted formula
    factories such as =Pick(vChoice, 'Sum(A)', 'Sum(B)').
    """
    variable_dependencies([expr], {**definitions, **{k: "" for k in overrides}})
    affected = set(overrides)
    for _ in range(33):
        extra = {name for name, definition in definitions.items()
                 if name not in affected
                 and set(_direct_variable_dependencies(definition, frozenset(definitions) | frozenset(overrides))) & affected}
        if not extra:
            break
        affected.update(extra)
    else:
        raise ValueError("Qlik variable dependency depth exceeds 32")

    def expand(text: str, depth: int = 0) -> str:
        if depth > 32:
            raise ValueError("Qlik variable dependency depth exceeds 32")

        def dollar(match):
            name = match.group(1).strip()
            if name in overrides or name not in affected or name not in definitions:
                return match.group(0)
            definition = definitions[name].strip()
            body = expand(definition[1:] if definition.startswith("=") else definition, depth + 1)
            return "$(=" + body + ")" if definition.startswith("=") else body

        text = re.sub(r"\$\(\s*([^()]+?)\s*\)", dollar, text)
        quoted_at = {i for i, _, _, quoted in _scan(text) if quoted}
        pieces, last = [], 0
        for match in _IDENT_RE.finditer(text):
            name = match.group(0)
            if name not in affected or name in overrides or name not in definitions or match.start() in quoted_at:
                continue
            if text[match.end():].lstrip().startswith("("):
                if text[:match.start()].rstrip().endswith("$("):
                    raise ValueError(f"Cannot apply an override through parameterized Qlik variable {name!r}")
                continue
            if text[:match.start()].rstrip().endswith("$("):
                continue
            definition = definitions[name].strip()
            body = expand(definition[1:] if definition.startswith("=") else definition, depth + 1)
            pieces += [text[last:match.start()], "(" + body + ")" if definition.startswith("=") else qlik_literal(body)]
            last = match.end()
        pieces.append(text[last:])
        result = "".join(pieces)
        if len(result) > 1000000:
            raise ValueError("Expanded Qlik expression exceeds 1 MB")
        return result

    return expand(expr)


def resolve_expression(
    expr: str,
    variables: Optional[Dict[str, Any]] = None,
    field_values: Optional[Dict[str, Any]] = None,
    variable_definitions: Optional[Dict[str, str]] = None,
) -> str:
    """Expand affected dependencies, substitute choices, and simplify branches."""
    # The same shared expression appears on many sheets. The key includes
    # every definition and choice, so a reindex/session with changed definitions
    # cannot reuse an old expansion. Only text is cached, never evaluated data.
    return _resolve_expression_cached(
        expr, tuple((variables or {}).items()), tuple((field_values or {}).items()),
        tuple((variable_definitions or {}).items()),
    )


@lru_cache(maxsize=1024)
def _resolve_expression_cached(expr: str, variables: tuple, field_values: tuple, definitions: tuple) -> str:
    variable_map, fields, definition_map = dict(variables), dict(field_values), dict(definitions)
    if definition_map and variable_map:
        expr = expand_variable_dependencies(expr, definition_map, variable_map)
    return simplify_expression(substitute_expression(expr, variable_map, fields))


_SELECTOR_CALLS = ("Only", "GetFieldSelections", "MinString", "MaxString", "GetSelectedCount", "GetPossibleCount")


def dollar_formulas(expr: str) -> List[str]:
    """The bodies of `$(=...)` expansions, with nested parentheses intact."""
    text = expr or ""
    out = []
    for m in re.finditer(r"\$\(\s*=", text):
        parsed = _call_args(text, m.start() + 1)
        if parsed is not None:
            body = text[m.end():parsed[1]]
            out.append(body.strip())
    return out


def selector_fields(expr: str) -> List[str]:
    """Fields an expression reads through Only(), GetFieldSelections() etc."""
    fields: List[str] = []
    for _, _, _, args in _find_calls(expr or "", _SELECTOR_CALLS):
        if args:
            a = args[0].strip()
            if a.startswith("[") and a.endswith("]"):
                a = a[1:-1].replace("]]", "]")
            if a and a not in fields and _IDENT_RE.fullmatch(a.replace(" ", "_")):
                fields.append(a)
    return fields


def expression_drivers(expr: str, known_variables: Optional[set] = None) -> Dict[str, List[str]]:
    """Which variables and selector fields an expression's value depends on.

    Variables: `$(v)` anywhere, and bare names in `known_variables`. Selector
    fields: fields read through Only(), GetFieldSelections() and the like —
    the reads that turn a selection into a choice.
    """
    text = expr or ""
    variables: List[str] = []
    for name in variables_in_expression(text):
        if name not in variables:
            variables.append(name)
    if known_variables:
        quoted_at = {i for i, _, _, q in _scan(text) if q}
        for m in _IDENT_RE.finditer(text):
            name = m.group(0)
            is_call = text[m.end():].lstrip().startswith("(")
            if name in known_variables and name not in variables and m.start() not in quoted_at and not is_call:
                variables.append(name)
    fields = selector_fields(text)
    for body in dollar_formulas(text):
        fields += [f for f in selector_fields(body) if f not in fields]
    return {"variables": variables, "fields": fields}


def summarize_switch(props: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What a button or variable input lets the user set, or None.

    * Button (`action-button`): `actions[]` with `actionType` `setVariable`
      (`variable`, `value`) or a field action such as `selectValues` /
      `selectMatchingValues` / `toggleSelect` (`field`, `value`).
    * Variable input (`variable`): `variableName` plus fixed `alternatives`
      (`[{value, label}]`) or `dynamicvalues` (`"a~Label A|b"`).
    """
    info = props.get("qInfo") or {}
    kind = props.get("visualization") or info.get("qType") or ""
    sets: List[Dict[str, Any]] = []
    for action in props.get("actions") or []:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("actionType") or "")
        if action_type == "setVariable" and action.get("variable"):
            sets.append({"variable": str(action["variable"]), "value": action.get("value")})
        elif action.get("field") and action_type.lower().startswith(("select", "toggle")):
            sets.append({"field": str(action["field"]), "value": action.get("value")})
    variable_name = props.get("variableName")
    if not variable_name and isinstance(props.get("variableValue"), dict):
        variable_name = props["variableValue"].get("name")
    if variable_name:
        for alt in props.get("alternatives") or []:
            if isinstance(alt, dict) and alt.get("value") not in (None, ""):
                sets.append({"variable": str(variable_name), "value": alt.get("value"), "label": alt.get("label") or None})
        dynamic = props.get("dynamicvalues")
        if isinstance(dynamic, str) and dynamic.strip():
            for token in dynamic.split("|"):
                value, _, label = token.partition("~")
                if value.strip():
                    sets.append({"variable": str(variable_name), "value": value.strip(), "label": label.strip() or None})
        if not any(s.get("variable") == variable_name for s in sets):
            sets.append({"variable": str(variable_name), "value": None, "freeInput": True})
    if not sets:
        return None
    for entry in sets:
        value = entry.get("value")
        if isinstance(value, dict):
            expression = text_or_expression(value)
            ok, literal = _literal_value(expression.lstrip("="))
            if ok:
                entry["value"] = literal
            else:
                # A Qlik expression-valued control is not a literal choice.
                # Preserve it for inspection without advertising an invalid
                # variables={name: {qStringExpression: ...}} execution call.
                entry.pop("value", None)
                entry["valueExpression"] = expression
        if isinstance(entry.get("label"), dict):
            expression = text_or_expression(entry["label"])
            entry["label"] = literal_text(expression) or expression
    style = props.get("style")
    label = (text_or_expression(props.get("title"))
             or text_or_expression(style.get("label") if isinstance(style, dict) else None)
             or text_or_expression(props.get("label")))
    return {
        "id": info.get("qId") or "",
        "type": kind or "switch",
        "title": label,
        "sets": [{k: v for k, v in s.items() if v not in (None, "")} for s in sets],
    }


def _summary_expressions(summary: Dict[str, Any]) -> List[str]:
    texts: List[str] = []
    for key in ("measures", "dimensions"):
        for item in summary.get(key) or []:
            texts += [item.get("expression") or "", item.get("label") or "", item.get("showWhen") or ""]
            if (item.get("field") or "").startswith("="):
                texts.append(item["field"])
    texts.append(summary.get("calcCondition") or "")
    return [t for t in texts if t]


def summary_drivers(summary: Dict[str, Any], known_variables: Optional[set] = None) -> Dict[str, List[str]]:
    """Variables and selector fields that decide what one object shows."""
    variables: List[str] = []
    fields: List[str] = []
    for text in _summary_expressions(summary):
        found = expression_drivers(text, known_variables)
        variables += [v for v in found["variables"] if v not in variables]
        fields += [f for f in found["fields"] if f not in fields]
    return {"variables": variables, "fields": fields}


def expression_choices(expressions: List[str], known_variables: set) -> Dict[str, List[Dict[str, Any]]]:
    """Explicit Pick/Match branches when no readable UI switch lists choices.

    These are expression branches, not a claim about the UI's allowed values.
    Never invent the value of an If/else branch or parse an arbitrary expression
    as a selector. Only a single variable (bare or dollar-expanded) qualifies.
    """
    choices: Dict[str, List[Dict[str, Any]]] = {}
    for expression in expressions:
        for _, _, function, args in _find_calls(expression, ("Pick", "Match", "MixMatch")):
            if len(args) < 2:
                continue
            selector = args[0].strip()
            if selector.startswith("$(") and selector.endswith(")"):
                selector = selector[2:-1].strip()
            if selector not in known_variables:
                continue
            if function.lower() == "pick":
                values = list(range(1, len(args)))
            else:
                literals = [_literal_value(arg) for arg in args[1:]]
                if not all(ok for ok, _ in literals):
                    continue
                values = [value for _, value in literals]
            bucket = choices.setdefault(selector, [])
            for value in values:
                if not any(c["value"] == value for c in bucket):
                    bucket.append({"value": value, "label": "expression branch; UI choice not verified"})
    return choices


def _resolve_items(items: List[Dict[str, Any]], variables: Dict[str, Any], field_values: Dict[str, Any], kind: str, definitions: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Resolve one option: substitute, collapse, and drop columns it hides."""
    out = []
    for item in items:
        shown = item.get("showWhen")
        if shown:
            decided = _decide_comparison(resolve_expression(shown, variables, field_values, definitions))
            if decided is False:
                continue
        label = resolve_expression(item.get("label") or "", variables, field_values, definitions)
        resolved = {"label": literal_text(label) or label or None}
        if kind == "measure":
            resolved["expression"] = resolve_expression(item.get("expression") or "", variables, field_values, definitions)
        else:
            field = item.get("field") or ""
            resolved["field"] = resolve_expression(field, variables, field_values, definitions) if field.startswith("=") else field
            if item.get("master"):
                resolved["master"] = item["master"]
        out.append({k: v for k, v in resolved.items() if v})
    return out


def object_options(
    summary: Dict[str, Any],
    variable_choices: Dict[str, List[Dict[str, Any]]],
    islands: Dict[str, Dict[str, Any]],
    known_variables: Optional[set] = None,
    max_options: Optional[int] = None,
    variable_definitions: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """The explicit formulas an object shows under each user choice.

    `variable_choices` maps a variable to the values a switch (button,
    variable input) can give it. `islands` maps every field of a data-island
    table to `{"name", "selector", "rows"}`: the table, the field users select
    in, and its rows. Selecting a row makes every field of that row the
    current value, so `Only(MetricExpr)` follows a selection in `Metric`. Each option is one choice — a variable
    value, or one island row — with the dimensions and measures the object
    then shows (hidden columns removed) and how to reproduce it:
    `{"variables": {...}}` or `{"filters": {field: [value]}}`.
    """
    drivers = summary_drivers(summary, known_variables)
    if variable_definitions:
        drivers["variables"] = variable_dependencies(_summary_expressions(summary), variable_definitions)
    options: List[Dict[str, Any]] = []
    for variable in drivers["variables"]:
        for choice in variable_choices.get(variable) or []:
            value = choice.get("value")
            if value in (None, ""):
                continue
            values = {variable: value}
            options.append({
                "when": f"{variable}={qlik_literal(value)}",
                "label": choice.get("label"),
                "run": {"variables": values},
                "dimensions": _resolve_items(summary.get("dimensions") or [], values, {}, "dimension", variable_definitions),
                "measures": _resolve_items(summary.get("measures") or [], values, {}, "measure", variable_definitions),
            })
    seen_islands: set = set()
    for field in drivers["fields"]:
        island = islands.get(field)
        if not island or island["name"] in seen_islands:
            continue
        seen_islands.add(island["name"])
        selector = island["selector"]
        for row in island["rows"]:
            if row.get(selector) in (None, ""):
                continue
            options.append({
                "when": f"{selector}={qlik_literal(row[selector])}",
                "run": {"filters": {selector: [row[selector]]}},
                "dimensions": _resolve_items(summary.get("dimensions") or [], {}, row, "dimension"),
                "measures": _resolve_items(summary.get("measures") or [], {}, row, "measure"),
            })
    return options if max_options is None else options[:max_options]


def describe_options(options: List[Dict[str, Any]], object_id: str) -> str:
    """The options line of an object description."""
    if not options:
        return ""
    parts = []
    for option in options:
        measures = "; ".join(
            (f"{m['label']} = {m['expression']}" if m.get("label") and m.get("label") != m.get("expression") else m.get("expression", ""))
            for m in option.get("measures") or []
        ) or "(no measures shown)"
        dims = ", ".join(d.get("label") or d.get("field") or d.get("master") or "" for d in option.get("dimensions") or [])
        run = option.get("run") or {}
        how = (
            f"variables={json.dumps(run['variables'], ensure_ascii=False)}" if "variables" in run
            else f"filters={json.dumps(run['filters'], ensure_ascii=False)}"
        )
        label = f" ({option['label']})" if option.get("label") else ""
        parts.append(
            f"when {option['when']}{label}: " + (f"by {dims}: " if dims else "") + measures
            + f" [execute_query(object_id='{object_id}', {how})]"
        )
    description = "DYNAMIC — what it shows depends on a user choice; " + " | ".join(parts)
    if len(description) <= 4000:
        return description

    # Do not repeat a large expanded formula for every control on every chart.
    # The original chart expression and complete variable definitions remain
    # in the catalog. Preserve ALL choices and executable arguments here.
    compact = []
    for option in options:
        label = f" ({option['label']})" if option.get("label") else ""
        run = option.get("run") or {}
        how = ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in run.items())
        expressions = "; ".join(m.get("expression", "") for m in option.get("measures") or [])
        formula = f" => {expressions}" if expressions and len(expressions) <= 200 else ""
        compact.append(f"when {option['when']}{label}: {how}{formula}")
    return (
        f"DYNAMIC — execute_query(object_id='{object_id}', <choice arguments below>). "
        "Uses the original chart formulas under each override; full variable definitions are in Master Items. "
        "Long expanded formulas are referenced, not repeated; column conditions still apply. "
        + " | ".join(compact)
    )


def decide_condition(expr: str) -> Optional[bool]:
    """True/False when a condition is decided by literals alone, else None."""
    text = (expr or "").strip()
    if text.startswith("="):
        text = text[1:]
    return _decide_comparison(simplify_expression(text))


def literal_text(expr: str) -> Optional[str]:
    """The text of an expression that is a single string literal, else None."""
    text = (expr or "").strip()
    if text.startswith("="):
        text = text[1:]
    ok, value = _literal_value(simplify_expression(text))
    return str(value) if ok and isinstance(value, str) else None
