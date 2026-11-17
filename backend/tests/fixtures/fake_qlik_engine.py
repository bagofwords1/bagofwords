"""A stand-in Qlik Sense Enterprise site: QRS over HTTPS and the Engine (QIX).

The Qlik clients are tested at the transport boundary with this fake. It speaks
the Engine's JSON-RPC 2.0 protocol — the same methods, handles, parameters and
result shapes a real Engine uses — over an in-process WebSocket (unit tests) or
a real TLS WebSocket on localhost (the sandbox loop). Everything above the
socket, `QixSession` included, runs unchanged.

What it models, because the client depends on it:

* **Handles.** `OpenDoc` returns a document handle, `GetObject`/`GetField`/
  `CreateSessionObject` return object handles, and each method is only valid
  on the handle type it belongs to (an unknown pairing is an RPC error, as on
  a real Engine).
* **List objects** (`qMeasureListDef`, `qDimensionListDef`, `qVariableListDef`,
  `qAppObjectListDef`) whose `qData` paths are resolved against each item's
  properties, so the client must ask for the paths it reads.
* **Sheets and objects**: `GetFullPropertyTree` returns a sheet with its
  children (containers and filter panes nest), `GetProperties` /
  `GetEffectiveProperties` return an object's definition, a linked master
  visualization stores only `qExtendsId`, and private sheets are returned to
  the owner exactly like shared ones (the client has to filter them).
* **An associative dataset** that hypercubes are computed over: selections
  narrow it, dimensions group it, measures aggregate it. The expression
  evaluator covers `Sum/Count/Avg/Min/Max`, `distinct`, `$(variable)`
  expansion and simple set analysis (`{<[Field]={'a','b'}>}`).
* **Qlik's quiet failure**: an unknown field evaluates to nothing, so a
  misspelled measure returns zeros instead of an error. `CheckExpression`
  names the unknown field, which is how a client can catch it.
* **Pivot cubes** cannot be paged with `GetHyperCubeData` (pivot mode needs
  the pivot API), as on a real Engine.

Usage (unit tests):

    site = FakeQlikSite.demo()
    site.install(monkeypatch, client)   # QRS + websockets now hit the fake
    client.get_schemas()

Usage (sandbox): see `docs/feedback-loops/2026-11-16-qlik-onprem-sheet-objects.py`,
which serves the same site over real TLS with `serve_qrs()` / `serve_engine()`.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import operator
import re
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


class EngineError(Exception):
    """An Engine JSON-RPC error (`{"error": {"code", "message"}}`)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

_AGG_RE = re.compile(r"^\s*(Sum|Count|Avg|Min|Max)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_DISTINCT_RE = re.compile(r"^\s*distinct\s+", re.IGNORECASE)
_VAR_RE = re.compile(r"\$\(\s*([A-Za-z_][\w.]*)\s*\)")
_BRACKET_RE = re.compile(r"\[((?:[^\]]|\]\])+)\]")
_SET_RE = re.compile(r"^\{\s*\$?\s*<(.*)>\s*\}$", re.DOTALL)
_SET_ITEM_RE = re.compile(r"\s*(\[(?:[^\]]|\]\])+\]|[A-Za-z_][\w ]*?)\s*=\s*\{([^}]*)\}\s*")
_ARITH_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _split_top_level(text: str, sep: str = ",") -> List[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _literal(token: str) -> Any:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    try:
        number = float(token)
        return int(number) if number.is_integer() else number
    except ValueError:
        return token


def _parse_agg(text: str) -> Optional[Tuple[str, Optional[str], bool, str, int]]:
    """`Sum({<set>} distinct inner)` → (func, set, distinct, inner, inner offset)."""
    match = _AGG_RE.match(text)
    if not match:
        return None
    body, offset = match.group(2), match.start(2)
    set_expr = None
    stripped = body.lstrip()
    lead = len(body) - len(stripped)
    if stripped.startswith("{"):
        depth = 0
        for i, ch in enumerate(stripped):
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                set_expr = stripped[: i + 1]
                lead += i + 1
                break
        else:
            return None
    rest = body[lead:]
    distinct = _DISTINCT_RE.match(rest)
    if distinct:
        lead += distinct.end()
        rest = body[lead:]
    inner_lead = len(rest) - len(rest.lstrip())
    return match.group(1).lower(), set_expr, bool(distinct), rest.strip(), offset + lead + inner_lead


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


_CHOICE_CALL_RE = re.compile(r"\b(Pick|If|Match|MixMatch|Only|GetFieldSelections)\s*\(", re.IGNORECASE)
_ISLAND_FORMULA_RE = re.compile(r"\$\(\s*=\s*Only\(\s*\[?([^\]()]+?)\]?\s*\)\s*\)")


def _close_paren(text: str, open_index: int) -> Optional[int]:
    depth, quote = 0, None
    for i in range(open_index, len(text)):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'[":
            quote = "'" if ch == "'" else "]"
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _as_literal(token: str) -> Tuple[bool, Any]:
    t = token.strip()
    if len(t) >= 2 and t[0] == t[-1] == "'":
        return True, t[1:-1]
    try:
        return True, float(t)
    except ValueError:
        return False, None


def _quote(value: Any) -> str:
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class Evaluator:
    """Computes the subset of Qlik expressions the demo app uses.

    Choice logic — `Pick(Match(...))`, `If(a = b, ...)`, `Only(Field)`,
    `$(=Only(Field))` and bare variable names — is reduced against the
    session's selections and the app's variables before aggregating. It is
    written independently of the client's resolver so the tests compare two
    implementations, not one with itself.
    """

    def __init__(self, fields: List[str], variables: Dict[str, str]):
        self.fields = set(fields)
        self.variables = variables

    def expand(self, expr: str) -> str:
        for _ in range(5):  # nested variables
            new = _VAR_RE.sub(lambda m: str(self.variables.get(m.group(1), "")), expr)
            if new == expr:
                break
            expr = new
        return expr

    # -- validation (CheckExpression) --------------------------------------
    def field_refs(self, expr: str) -> List[Tuple[int, int, str]]:
        """(start, length, name) of every field reference outside `$( )`."""
        masked = _VAR_RE.sub(lambda m: " " * len(m.group(0)), expr)
        refs = [(m.start(1), len(m.group(1)), m.group(1).replace("]]", "]")) for m in _BRACKET_RE.finditer(masked)]
        agg = _parse_agg(masked)
        if agg and re.fullmatch(r"[A-Za-z_]\w*", agg[3]):
            refs.append((agg[4], len(agg[3]), agg[3]))
        return refs

    def check(self, expr: str, labels: List[str]) -> Dict[str, Any]:
        text = (expr or "").lstrip("=")
        offset = len(expr or "") - len(text)
        if text.count("(") != text.count(")"):
            return {"qErrorMsg": "Error in expression: ')' expected", "qBadFieldNames": [], "qDangerousFieldNames": []}
        bad = [
            {"qFrom": start + offset, "qCount": length}
            for start, length, name in self.field_refs(text)
            if name not in self.fields and name not in labels
        ]
        return {"qErrorMsg": "", "qBadFieldNames": bad, "qDangerousFieldNames": []}

    # -- evaluation ----------------------------------------------------------
    def _row_value(self, node: ast.AST, row: Dict[str, Any], names: Dict[str, str]) -> Optional[float]:
        if isinstance(node, ast.Expression):
            return self._row_value(node.body, row, names)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            value = row.get(names.get(node.id, node.id))
            return float(value) if isinstance(value, (int, float)) else None
        if isinstance(node, ast.BinOp) and type(node.op) in _ARITH_OPS:
            left = self._row_value(node.left, row, names)
            right = self._row_value(node.right, row, names)
            if left is None or right is None:
                return None
            try:
                return _ARITH_OPS[type(node.op)](left, right)
            except ZeroDivisionError:
                return None
        raise EngineError(-32602, "Unsupported expression in the fake Engine")

    def _apply_set(self, set_expr: Optional[str], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not set_expr:
            return rows
        match = _SET_RE.match(set_expr.strip())
        if not match:
            raise EngineError(-32602, f"Unsupported set expression in the fake Engine: {set_expr}")
        for part in _split_top_level(match.group(1)):
            item = _SET_ITEM_RE.fullmatch(part)
            if not item:
                raise EngineError(-32602, f"Unsupported set modifier in the fake Engine: {part}")
            field = item.group(1).strip()
            field = field[1:-1].replace("]]", "]") if field.startswith("[") else field
            values = [_literal(v) for v in _split_top_level(item.group(2)) if v.strip()]
            rows = [r for r in rows if any(_same(r.get(field), v) for v in values)]
        return rows

    def reduce(self, expr: str, only) -> str:
        """Resolve choice logic to the branch the current state selects."""
        text = _ISLAND_FORMULA_RE.sub(lambda m: str(only(m.group(1).strip()) or ""), expr or "")
        text = self.expand(text)
        for name, value in self.variables.items():
            text = re.sub(r"(?<![\w\[.'])" + re.escape(name) + r"(?![\w\].(]|\s*\()", _quote(value), text)
        for _ in range(30):
            calls = list(_CHOICE_CALL_RE.finditer(text))
            for m in reversed(calls):
                close = _close_paren(text, m.end() - 1)
                if close is None:
                    continue
                args = _split_top_level(text[m.end():close])
                name = m.group(1).lower()
                out = None
                if name in ("only", "getfieldselections") and len(args) == 1:
                    field = args[0].strip().strip("[]")
                    value = only(field)
                    out = "Null()" if value is None else _quote(value)
                elif name in ("match", "mixmatch"):
                    parsed = [_as_literal(a) for a in args]
                    if all(ok for ok, _ in parsed):
                        target = parsed[0][1]
                        hits = [i for i, (_, v) in enumerate(parsed[1:], 1)
                                if (str(v).lower() == str(target).lower() if name == "mixmatch" else str(v) == str(target))]
                        out = str(hits[0] if hits else 0)
                elif name == "pick":
                    ok, n = _as_literal(args[0])
                    if ok and isinstance(n, float):
                        out = args[int(n)].strip() if 1 <= n < len(args) else "Null()"
                elif name == "if" and len(args) in (2, 3):
                    decided = self._compare(args[0])
                    if decided is not None:
                        out = args[1].strip() if decided else (args[2].strip() if len(args) == 3 else "Null()")
                if out is not None:
                    text = text[:m.start()] + out + text[close + 1:]
                    break
            else:
                break
        return text

    @staticmethod
    def _compare(cond: str) -> Optional[bool]:
        parts = _split_top_level(cond, "=")
        if len(parts) == 2:
            (lok, left), (rok, right) = _as_literal(parts[0]), _as_literal(parts[1])
            if lok and rok:
                return str(left).lower() == str(right).lower()
        return None

    def measure(self, expr: str, rows: List[Dict[str, Any]], only=None) -> Optional[float]:
        only = only or (lambda field: None)
        text = self.reduce(expr or "", only).strip().lstrip("=")
        if text in ("", "Null()"):
            # `$(=Only(F))` with no single value expands to nothing: the
            # Engine shows the column as nulls.
            return None
        decided = self._compare(text)
        if decided is not None:
            return 1.0 if decided else 0.0
        ok, number = _as_literal(text)
        if ok and isinstance(number, float):
            return number
        agg = _parse_agg(text)
        if not agg:
            raise EngineError(-32602, f"Unsupported measure in the fake Engine: {expr}")
        func, set_expr, distinct, inner, _ = agg
        rows = self._apply_set(set_expr, rows)
        names: Dict[str, str] = {}

        def name_for(m: re.Match) -> str:
            key = f"f{len(names)}"
            names[key] = m.group(1).replace("]]", "]")
            return key

        tree = ast.parse(_BRACKET_RE.sub(name_for, inner.strip()), mode="eval")
        if func == "count":
            if isinstance(tree.body, ast.Name):
                field = names.get(tree.body.id, tree.body.id)
                values = [r.get(field) for r in rows if r.get(field) is not None]
            else:
                values = [v for v in (self._row_value(tree, r, names) for r in rows) if v is not None]
            return float(len(set(values)) if distinct else len(values))
        values = [v for v in (self._row_value(tree, r, names) for r in rows) if v is not None]
        if func == "sum":
            return float(sum(values))  # unknown field → no values → 0, as in Qlik
        if not values:
            return None
        if func == "avg":
            return sum(values) / len(values)
        return float(min(values) if func == "min" else max(values))


# ---------------------------------------------------------------------------
# App model
# ---------------------------------------------------------------------------


def _resolve_path(props: Dict[str, Any], path: str) -> Any:
    node: Any = props
    for segment in [s for s in path.split("/") if s]:
        if not isinstance(node, dict):
            return ""
        node = node.get(segment)
    return "" if node is None else node


class FakeApp:
    def __init__(
        self,
        app_id: str,
        rows: List[Dict[str, Any]],
        tables: Dict[str, List[str]],
        *,
        master_measures: Optional[List[Dict[str, Any]]] = None,
        master_dimensions: Optional[List[Dict[str, Any]]] = None,
        variables: Optional[Dict[str, str]] = None,
        sheets: Optional[List[Dict[str, Any]]] = None,
        master_objects: Optional[List[Dict[str, Any]]] = None,
        script: str = "",
        lineage: Optional[List[str]] = None,
        islands: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ):
        self.id = app_id
        self.rows = rows
        self.tables = tables
        self.master_measures = master_measures or []
        self.master_dimensions = master_dimensions or []
        self.variables = variables or {}
        self.sheets = sheets or []
        self.master_objects = master_objects or []
        self.script = script
        self.lineage = lineage or []
        # Data islands: small tables with no key to the model.
        self.islands = islands or {}
        self.island_fields = {f: name for name, rows in self.islands.items() for f in (rows[0] if rows else {})}
        self.fields = sorted({f for cols in tables.values() for f in cols} | set(self.island_fields))
        self.evaluator = Evaluator(self.fields, self.variables)

    # Objects are stored as {"props": {...}, "children": [...]} trees.
    def all_object_nodes(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        def walk(node: Dict[str, Any]) -> None:
            out.append(node)
            for child in node.get("children") or []:
                walk(child)

        for node in self.sheets + self.master_objects:
            walk(node)
        return out

    def find_object(self, object_id: str) -> Optional[Dict[str, Any]]:
        for node in self.all_object_nodes():
            if node["props"]["qInfo"]["qId"] == object_id:
                return node
        return None

    def field_rows(self, field: str) -> List[Dict[str, Any]]:
        """The rows a field's values live in: its island, or the dataset."""
        island = self.island_fields.get(field)
        return self.islands[island] if island else self.rows

    def master_measure(self, item_id: str) -> Optional[Dict[str, Any]]:
        return next((m for m in self.master_measures if m["qInfo"]["qId"] == item_id), None)

    def master_dimension(self, item_id: str) -> Optional[Dict[str, Any]]:
        return next((d for d in self.master_dimensions if d["qInfo"]["qId"] == item_id), None)


# ---------------------------------------------------------------------------
# Engine session (one per WebSocket)
# ---------------------------------------------------------------------------


class EngineSession:
    def __init__(self, site: "FakeQlikSite", app_id: str, user: Optional[str]):
        self.site = site
        self.app_id = app_id
        self.user = user
        self.app: Optional[FakeApp] = None
        self.handles: Dict[int, Tuple[str, Any]] = {}
        self.selections: Dict[str, List[Any]] = {}
        self._next = 1

    def _new_handle(self, kind: str, payload: Any) -> int:
        handle = self._next
        self._next += 1
        self.handles[handle] = (kind, payload)
        return handle

    # -- dispatch --------------------------------------------------------
    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        method, handle, params = request.get("method"), request.get("handle"), request.get("params")
        self.site.calls.append({"user": self.user, "app": self.app_id, "handle": handle, "method": method, "params": params})
        reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
        try:
            reply["result"] = self._dispatch(handle, method, params if params is not None else [])
        except EngineError as e:
            reply["error"] = {"code": e.code, "message": e.message}
        return reply

    def _arg(self, params: Any, index: int, name: str, default: Any = None) -> Any:
        if isinstance(params, dict):
            return params.get(name, default)
        return params[index] if len(params) > index else default

    def _dispatch(self, handle: int, method: str, params: Any) -> Any:
        if handle == -1:
            if method == "OpenDoc":
                app_id = self._arg(params, 0, "qDocName")
                app = self.site.apps.get(app_id)
                if app is None or app_id != self.app_id:
                    raise EngineError(1002, "App not found")
                self.app = app
                return {"qReturn": {"qType": "Doc", "qHandle": self._new_handle("doc", app)}}
            raise EngineError(-32601, f"Method not found: {method} on Global")
        if handle not in self.handles:
            raise EngineError(-32602, "Invalid handle")
        kind, payload = self.handles[handle]
        fn = getattr(self, f"_{kind}_{method}", None)
        if fn is None:
            raise EngineError(-32601, f"Method not found: {method} on {kind}")
        return fn(payload, params)

    # -- data helpers ----------------------------------------------------
    def _selected_rows(self, exclude: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self.app.rows
        for field, values in self.selections.items():
            if field == exclude or field in self.app.island_fields:
                continue  # an island is unlinked: selecting in it filters no data
            rows = [r for r in rows if any(_same(r.get(field), v) for v in values)]
        return rows

    def _island_rows(self, island: str) -> List[Dict[str, Any]]:
        rows = self.app.islands.get(island) or []
        for field, values in self.selections.items():
            if self.app.island_fields.get(field) == island:
                rows = [r for r in rows if any(_same(r.get(field), v) for v in values)]
        return rows

    def _only(self, field: str) -> Any:
        """Only(field): the single possible value, else None."""
        island = self.app.island_fields.get(field)
        rows = self._island_rows(island) if island else self._selected_rows()
        values = {r.get(field) for r in rows if r.get(field) is not None}
        return next(iter(values)) if len(values) == 1 else None

    def _dimension_field(self, dim: Dict[str, Any]) -> Tuple[str, str]:
        """(field, title) for a hypercube dimension."""
        if dim.get("qLibraryId"):
            master = self.app.master_dimension(dim["qLibraryId"])
            if master is None:
                raise EngineError(-32602, f"Unknown library dimension {dim['qLibraryId']}")
            field = master["qDim"]["qFieldDefs"][0]
            return field, master["qMetaDef"]["title"]
        qdef = dim.get("qDef") or {}
        field = (qdef.get("qFieldDefs") or [""])[0]
        if field.startswith("="):
            raise EngineError(-32602, "Calculated dimensions are not supported by the fake Engine")
        bare = field[1:-1] if field.startswith("[") and field.endswith("]") else field
        label = (qdef.get("qFieldLabels") or [""])[0]
        return bare, label or bare

    def _measure_expr(self, meas: Dict[str, Any]) -> Tuple[str, str]:
        if meas.get("qLibraryId"):
            master = self.app.master_measure(meas["qLibraryId"])
            if master is None:
                raise EngineError(-32602, f"Unknown library measure {meas['qLibraryId']}")
            return master["qMeasure"]["qDef"], master["qMeasure"].get("qLabel") or master["qMetaDef"]["title"]
        qdef = meas.get("qDef") or {}
        expr = qdef.get("qDef") or ""
        return expr, qdef.get("qLabel") or expr

    def _compute_cube(self, cube: Dict[str, Any]) -> Dict[str, Any]:
        dims = [self._dimension_field(d) for d in cube.get("qDimensions") or []]
        meas = [self._measure_expr(m) for m in cube.get("qMeasures") or []]
        island_dims = {self.app.island_fields.get(f) for f, _ in dims}
        if dims and None not in island_dims and len(island_dims) == 1:
            rows = self._island_rows(island_dims.pop())
        else:
            rows = self._selected_rows()
        groups: Dict[Tuple, List[Dict[str, Any]]] = {}
        if dims:
            for row in rows:
                groups.setdefault(tuple(row.get(f) for f, _ in dims), []).append(row)
            if not groups:
                groups = {}
        else:
            groups = {(): rows}
        records = []
        for key, members in groups.items():
            values = [self.app.evaluator.measure(expr, members, self._only) for expr, _ in meas]
            records.append((key, values))

        order = cube.get("qInterColumnSortOrder") or []
        if order and order[0] >= len(dims) and meas:
            m_index = order[0] - len(dims)
            sort_by = (cube["qMeasures"][m_index].get("qSortBy") or {})
            descending = sort_by.get("qSortByNumeric", 1) == -1
            records.sort(key=lambda r: (r[1][m_index] is None, r[1][m_index] or 0), reverse=descending)
        elif dims:
            records.sort(key=lambda r: tuple((v is None, str(v) if not isinstance(v, (int, float)) else f"{v:020.4f}") for v in r[0]))

        matrix = []
        for key, values in records:
            row = []
            for value in key:
                if value is None:
                    row.append({"qText": "-", "qNum": "NaN", "qElemNumber": -2, "qState": "L"})
                elif isinstance(value, (int, float)):
                    row.append({"qText": str(value), "qNum": value, "qElemNumber": 0, "qState": "O"})
                else:
                    row.append({"qText": str(value), "qNum": "NaN", "qElemNumber": 0, "qState": "O"})
            for value in values:
                if value is None:
                    row.append({"qText": "-", "qNum": "NaN", "qElemNumber": 0, "qState": "L"})
                else:
                    row.append({"qText": f"{value:,.0f}", "qNum": value, "qElemNumber": 0, "qState": "L"})
            matrix.append(row)
        return {
            "matrix": matrix,
            "dim_titles": [t for _, t in dims],
            "meas_titles": [t for _, t in meas],
        }

    # -- Doc methods -----------------------------------------------------
    def _doc_GetAppLayout(self, app: FakeApp, params: Any) -> Any:
        return {"qLayout": {"qTitle": app.id, "qHasData": True, "qHasScript": bool(app.script), "qStateNames": []}}

    def _doc_GetTablesAndKeys(self, app: FakeApp, params: Any) -> Any:
        qtr = []
        for name, cols in app.tables.items():
            qtr.append({
                "qName": name,
                "qNoOfRows": len(app.rows),
                "qFields": [{"qName": c, "qTags": ["$numeric"] if all(isinstance(r.get(c), (int, float)) for r in app.rows) else ["$text"],
                             "qnTotalDistinctValues": len({r.get(c) for r in app.rows})} for c in cols],
            })
        for name, island_rows in app.islands.items():
            qtr.append({
                "qName": name,
                "qNoOfRows": len(island_rows),
                "qFields": [{"qName": c, "qTags": ["$text"]} for c in (island_rows[0] if island_rows else {})],
            })
        owners: Dict[str, List[str]] = {}
        for name, cols in app.tables.items():
            for c in cols:
                owners.setdefault(c, []).append(name)
        qk = [{"qKeyFields": [f], "qTables": t} for f, t in owners.items() if len(t) > 1]
        return {"qtr": qtr, "qk": qk}

    def _doc_GetLineage(self, app: FakeApp, params: Any) -> Any:
        return {"qLineage": [{"qDiscriminator": d, "qStatement": ""} for d in app.lineage]}

    def _doc_GetScript(self, app: FakeApp, params: Any) -> Any:
        return {"qScript": app.script}

    def _doc_GetField(self, app: FakeApp, params: Any) -> Any:
        name = self._arg(params, 0, "qFieldName")
        if name not in app.fields:
            return {"qReturn": {"qType": "Field", "qHandle": None}}
        return {"qReturn": {"qType": "Field", "qHandle": self._new_handle("field", name)}}

    def _doc_GetObject(self, app: FakeApp, params: Any) -> Any:
        object_id = self._arg(params, 0, "qId")
        node = app.find_object(object_id)
        if node is None:
            return {"qReturn": {"qType": "GenericObject", "qHandle": None}}
        info = node["props"]["qInfo"]
        return {"qReturn": {"qType": "GenericObject", "qHandle": self._new_handle("object", node),
                            "qGenericType": info["qType"], "qGenericId": info["qId"]}}

    def _doc_CheckExpression(self, app: FakeApp, params: Any) -> Any:
        expr = self._arg(params, 0, "qExpr") or ""
        labels = self._arg(params, 1, "qLabels") or []
        return app.evaluator.check(expr, labels)

    def _doc_SearchResults(self, app: FakeApp, params: Any) -> Any:
        options = self._arg(params, 0, "qOptions") or {}
        terms = self._arg(params, 1, "qTerms") or []
        fields = options.get("qSearchFields") or app.fields
        items = []
        for field in fields:
            if field not in app.fields:
                continue
            values = sorted({str(r.get(field)) for r in app.field_rows(field) if r.get(field) is not None})
            matches = [v for v in values if all(t.lower() in v.lower() for t in terms)]
            if matches:
                items.append({
                    "qItemType": "Field",
                    "qTotalNumberOfMatches": len(matches),
                    "qIdentifier": field,
                    "qItemMatches": [{"qText": v, "qAttributes": [], "qFields": []} for v in matches],
                    "qSearchTermsMatched": [0],
                })
        groups = [{"qGroupType": "DatasetType", "qSearchTermsMatched": [0], "qId": 0, "qItems": items}] if items else []
        return {"qResult": {"qSearchTerms": terms, "qTotalNumberOfGroups": len(groups), "qSearchGroupArray": groups}}

    def _doc_GetMeasure(self, app: FakeApp, params: Any) -> Any:
        item = app.master_measure(self._arg(params, 0, "qId"))
        if item is None:
            return {"qReturn": {"qType": "GenericMeasure", "qHandle": None}}
        return {"qReturn": {"qType": "GenericMeasure", "qHandle": self._new_handle("library", item)}}

    def _doc_GetDimension(self, app: FakeApp, params: Any) -> Any:
        item = app.master_dimension(self._arg(params, 0, "qId"))
        if item is None:
            return {"qReturn": {"qType": "GenericDimension", "qHandle": None}}
        return {"qReturn": {"qType": "GenericDimension", "qHandle": self._new_handle("library", item)}}

    def _library_GetProperties(self, item: Dict[str, Any], params: Any) -> Any:
        return {"qProp": copy.deepcopy(item)}

    def _doc_CreateSessionObject(self, app: FakeApp, params: Any) -> Any:
        definition = self._arg(params, 0, "qProp") or {}
        return {"qReturn": {"qType": "GenericObject", "qHandle": self._new_handle("session", definition)}}

    # -- Field methods ---------------------------------------------------
    def _field_SelectValues(self, field: str, params: Any) -> Any:
        wanted = self._arg(params, 0, "qFieldValues") or []
        values = {r.get(field) for r in self.app.field_rows(field) if r.get(field) is not None}
        matched = []
        for w in wanted:
            for v in values:
                numeric_hit = w.get("qIsNumeric") and isinstance(v, (int, float)) and float(v) == float(w.get("qNumber"))
                text_hit = not w.get("qIsNumeric") and str(v) == w.get("qText")
                if numeric_hit or text_hit:
                    matched.append(v)
        # A real Engine answers true even when nothing matched; only the
        # selection state tells whether the filter took.
        if matched:
            self.selections[field] = matched
        else:
            self.selections.pop(field, None)
        return {"qReturn": True}

    # -- session objects -------------------------------------------------
    def _session_GetLayout(self, definition: Dict[str, Any], params: Any) -> Any:
        if "qSelectionObjectDef" in definition:
            return {"qLayout": {"qSelectionObject": {"qSelections": [
                {"qField": f, "qSelectedCount": len(v), "qSelected": ", ".join(map(str, v))}
                for f, v in self.selections.items()
            ]}}}
        for list_key, layout_key in (
            ("qMeasureListDef", "qMeasureList"),
            ("qDimensionListDef", "qDimensionList"),
            ("qVariableListDef", "qVariableList"),
            ("qAppObjectListDef", "qAppObjectList"),
        ):
            if list_key in definition:
                return {"qLayout": {layout_key: {"qItems": self._list_items(list_key, definition[list_key])}}}
        if "qHyperCubeDef" in definition:
            cube = definition["qHyperCubeDef"]
            computed = self._compute_cube(cube)
            return {"qLayout": {"qHyperCube": {
                "qSize": {"qcx": len(computed["dim_titles"]) + len(computed["meas_titles"]), "qcy": len(computed["matrix"])},
                "qDimensionInfo": [{"qFallbackTitle": t} for t in computed["dim_titles"]],
                "qMeasureInfo": [{"qFallbackTitle": t} for t in computed["meas_titles"]],
                "qMode": cube.get("qMode", "S"),
            }}}
        raise EngineError(-32602, "Unsupported session object in the fake Engine")

    def _session_GetHyperCubeData(self, definition: Dict[str, Any], params: Any) -> Any:
        cube = definition.get("qHyperCubeDef")
        if cube is None:
            raise EngineError(-32602, "Object has no hypercube")
        if cube.get("qMode") in ("P", "K"):
            raise EngineError(7009, "GetHyperCubeData is not supported for pivot or stacked hypercubes")
        pages = self._arg(params, 1, "qPages") or []
        matrix = self._compute_cube(cube)["matrix"]
        out = []
        for page in pages:
            top, height = page.get("qTop", 0), page.get("qHeight", 0)
            out.append({"qMatrix": matrix[top: top + height], "qArea": page})
        return {"qDataPages": out}

    def _list_items(self, list_key: str, definition: Dict[str, Any]) -> List[Dict[str, Any]]:
        app = self.app
        paths = definition.get("qData") or {}

        def item(props: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "qInfo": props["qInfo"],
                "qMeta": meta,
                "qData": {k: _resolve_path(props, p) for k, p in paths.items()},
            }

        if list_key == "qMeasureListDef":
            return [item(m, {"title": m["qMetaDef"]["title"], "description": m["qMetaDef"].get("description", "")}) for m in app.master_measures]
        if list_key == "qDimensionListDef":
            return [item(d, {"title": d["qMetaDef"]["title"], "description": d["qMetaDef"].get("description", "")}) for d in app.master_dimensions]
        if list_key == "qVariableListDef":
            return [
                {"qInfo": {"qId": f"var-{name}", "qType": "variable"}, "qName": name,
                 "qDefinition": value, "qData": {k: _resolve_path({"qDefinition": value}, p) for k, p in paths.items()}}
                for name, value in app.variables.items()
            ]
        qtype = definition.get("qType")
        nodes = app.sheets if qtype == "sheet" else app.master_objects if qtype == "masterobject" else []
        out = []
        for node in nodes:
            props = node["props"]
            meta = {"title": props.get("qMetaDef", {}).get("title", ""), **(node.get("meta") or {})}
            out.append(item(props, meta))
        return out

    # -- GenericObject methods ---------------------------------------------
    def _object_GetProperties(self, node: Dict[str, Any], params: Any) -> Any:
        return {"qProp": copy.deepcopy(node["props"])}

    def _object_GetEffectiveProperties(self, node: Dict[str, Any], params: Any) -> Any:
        props = copy.deepcopy(node["props"])
        extends = props.get("qExtendsId")
        if extends:
            master = self.app.find_object(extends)
            if master is not None:
                merged = copy.deepcopy(master["props"])
                merged.update({k: v for k, v in props.items() if k not in ("qExtendsId",)})
                merged["qInfo"] = props["qInfo"]
                return {"qProp": merged}
        return {"qProp": props}

    def _object_GetFullPropertyTree(self, node: Dict[str, Any], params: Any) -> Any:
        def entry(n: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "qProperty": copy.deepcopy(n["props"]),
                "qChildren": [entry(c) for c in n.get("children") or []],
                "qEmbeddedSnapshotRef": None,
            }

        return {"qPropEntry": entry(node)}

    def _object_GetChildInfos(self, node: Dict[str, Any], params: Any) -> Any:
        return {"qInfos": [c["props"]["qInfo"] for c in node.get("children") or []]}

    def _object_GetLayout(self, node: Dict[str, Any], params: Any) -> Any:
        props = node["props"]
        layout = {"qInfo": props["qInfo"], "visualization": props.get("visualization")}
        if "qHyperCubeDef" in props and props["qHyperCubeDef"].get("qMode", "S") == "S":
            computed = self._compute_cube(props["qHyperCubeDef"])
            layout["qHyperCube"] = {"qSize": {"qcx": len(computed["dim_titles"]) + len(computed["meas_titles"]), "qcy": len(computed["matrix"])}}
        return {"qLayout": layout}

    def _object_GetHyperCubeData(self, node: Dict[str, Any], params: Any) -> Any:
        return self._session_GetHyperCubeData({"qHyperCubeDef": node["props"].get("qHyperCubeDef")}, params)


# ---------------------------------------------------------------------------
# Site: QRS data + Engine sessions + transports
# ---------------------------------------------------------------------------


class FakeQlikSite:
    """A QSEoW site: QRS rows (streams, apps) and the Engine apps behind them."""

    def __init__(self, apps: Dict[str, FakeApp], qrs_apps: List[Dict[str, Any]], streams: List[Dict[str, Any]]):
        self.apps = apps
        self.qrs_apps = qrs_apps
        self.streams = streams
        self.calls: List[Dict[str, Any]] = []
        self.sessions: List[EngineSession] = []

    # -- QRS ---------------------------------------------------------------
    def qrs(self, path: str) -> Any:
        path = path.strip("/")
        routes = {
            "about": {"buildVersion": "14.254.6", "buildDate": "2026-08-01"},
            "stream/count": {"value": len(self.streams)},
            "stream/full": self.streams,
            "app/full": self.qrs_apps,
            "app": [{"id": a["id"], "name": a["name"], "published": a.get("published"), "stream": a.get("stream")} for a in self.qrs_apps],
            "dataconnection/full": [],
        }
        if path not in routes:
            raise KeyError(path)
        return routes[path]

    def open_session(self, uri: str, headers: Optional[List[Tuple[str, str]]] = None) -> EngineSession:
        app_id = urlparse(uri).path.rsplit("/", 1)[-1]
        user = dict(headers or []).get("X-Qlik-User")
        session = EngineSession(self, app_id, user)
        self.sessions.append(session)
        return session

    def methods(self) -> List[str]:
        return [c["method"] for c in self.calls]

    # -- in-process transport (unit tests) -------------------------------------
    def websockets_module(self) -> SimpleNamespace:
        """A drop-in for the `websockets` module that `QixSession` imports.

        Enforces the real websockets 14+ `connect()` signature, so a client
        passing a removed keyword fails here as it would in production.
        """
        site = self

        class _Socket:
            def __init__(self, session: EngineSession):
                self.session = session
                self.outbox: List[str] = [json.dumps({
                    "jsonrpc": "2.0", "method": "OnConnected", "params": {"qSessionState": "SESSION_CREATED"},
                })]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def send(self, raw: str) -> None:
                self.outbox.append(json.dumps(self.session.handle_request(json.loads(raw))))

            async def recv(self) -> str:
                if not self.outbox:
                    raise AssertionError("client read with no reply pending")
                return self.outbox.pop(0)

        def connect(uri, *, additional_headers=None, ssl=None, open_timeout=None, close_timeout=None, **unknown):
            if unknown:
                raise TypeError(f"create_connection() got an unexpected keyword argument '{next(iter(unknown))}'")
            return _Socket(site.open_session(uri, list(additional_headers or [])))

        return SimpleNamespace(connect=connect)

    def install(self, monkeypatch, client) -> None:
        """Point one client at this site: QRS via `_qrs_get`, Engine via websockets."""
        import sys

        monkeypatch.setitem(sys.modules, "websockets", self.websockets_module())
        monkeypatch.setattr(client, "_qrs_get", lambda path, params=None, user=None: self.qrs(path))

    # -- real transports (sandbox loop) ----------------------------------------
    def serve_engine(self, host: str, port: int, ssl_context) -> "asyncio.AbstractServer":
        """Serve the Engine on wss://host:port/app/{id} (call inside a loop)."""
        from websockets.asyncio.server import serve

        site = self

        async def handler(ws):
            request = ws.request
            session = site.open_session(request.path, list(request.headers.raw_items()))
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "OnConnected",
                                      "params": {"qSessionState": "SESSION_CREATED"}}))
            async for raw in ws:
                await ws.send(json.dumps(session.handle_request(json.loads(raw))))

        return serve(handler, host, port, ssl=ssl_context)

    def serve_qrs(self, host: str, port: int, ssl_context) -> threading.Thread:
        """Serve QRS on https://host:port/qrs/* in a daemon thread."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                xrf = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p).get("xrfkey")
                if not xrf or xrf != self.headers.get("X-Qlik-Xrfkey"):
                    self.send_response(400)
                    self.end_headers()
                    return
                try:
                    body = json.dumps(site.qrs(parsed.path.removeprefix("/qrs/"))).encode()
                    self.send_response(200)
                except KeyError:
                    body = b"{}"
                    self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer((host, port), Handler)
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        thread.server = server  # type: ignore[attr-defined]
        return thread

    # -- the demo site -----------------------------------------------------------
    @classmethod
    def demo(cls) -> "FakeQlikSite":
        """A synthetic retail app with sheets that exercise every object shape."""
        regions = ["Northeast", "Midwest", "South", "West"]
        groups = ["Beverages", "Snacks", "Produce"]
        rows: List[Dict[str, Any]] = []
        order = 1000
        for year in (2023, 2024):
            for r_i, region in enumerate(regions):
                for g_i, group in enumerate(groups):
                    for k in range(2):
                        order += 1
                        rows.append({
                            "Order Number": order,
                            "Customer Number": 500 + (order % 7),
                            "Region Name": region,
                            "Product Group": group,
                            "Year": year,
                            "Sales Quantity": 10 + r_i * 3 + g_i * 2 + k + (year - 2023) * 5,
                            "Sales Price": 2.5 + g_i,
                        })
        tables = {
            "Sales": ["Order Number", "Product Group", "Sales Quantity", "Sales Price"],
            "Orders": ["Order Number", "Customer Number", "Region Name", "Year"],
        }

        def obj(object_id, vis, title, cube=None, extra=None, children=None):
            props: Dict[str, Any] = {"qInfo": {"qId": object_id, "qType": vis}, "visualization": vis, "title": title}
            if cube is not None:
                props["qHyperCubeDef"] = cube
            props.update(extra or {})
            return {"props": props, "children": children or []}

        def dim(field, label=""):
            return {"qDef": {"qFieldDefs": [field], "qFieldLabels": [label]}}

        def meas(expr, label="", sort_desc=False):
            m: Dict[str, Any] = {"qDef": {"qDef": expr, "qLabel": label}}
            if sort_desc:
                m["qSortBy"] = {"qSortByNumeric": -1}
            return m

        overview = {
            "props": {"qInfo": {"qId": "sheet-overview", "qType": "sheet"},
                      "qMetaDef": {"title": "Revenue Overview", "description": "Top-line revenue by region and year"},
                      "cells": []},
            "meta": {"published": True, "approved": True, "description": "Top-line revenue by region and year"},
            "children": [
                obj("obj-rev-region", "barchart", "Revenue by Region", {
                    "qDimensions": [dim("Region Name")],
                    "qMeasures": [{"qLibraryId": "mm-revenue", "qSortBy": {"qSortByNumeric": -1}}],
                    "qInterColumnSortOrder": [1, 0],
                }),
                obj("obj-cy-kpi", "kpi", "Current Year Revenue", {
                    "qDimensions": [],
                    "qMeasures": [meas("Sum({<[Year]={$(vCurrentYear)}>} [Sales Quantity]*[Sales Price])", "CY Revenue")],
                }),
                obj("obj-filters", "filterpane", "", None, children=[
                    obj("lb-year", "listbox", "", None, {"qListObjectDef": {"qDef": {"qFieldDefs": ["Year"]}}}),
                    obj("lb-region", "listbox", "", None, {"qListObjectDef": {"qLibraryId": "md-region"}}),
                ]),
                obj("obj-tabs", "container", "Detail tabs", None, children=[
                    obj("obj-orders-year", "table", "Orders by Year", {
                        "qDimensions": [dim("Year")],
                        "qMeasures": [meas("Count(distinct [Order Number])", "Orders")],
                    }),
                ]),
                obj("obj-trend-link", "linechart", "", None, {"qExtendsId": "mv-trend"}),
                obj("obj-mix", "pivot-table", "Product Mix", {
                    "qMode": "P",
                    "qDimensions": [dim("Product Group"), dim("Region Name")],
                    "qMeasures": [meas("Sum([Sales Quantity])", "Quantity")],
                }),
                obj("obj-note", "text-image", "About this sheet", None, {"markdown": "Revenue in USD."}),
            ],
        }
        community = {
            "props": {"qInfo": {"qId": "sheet-community", "qType": "sheet"},
                      "qMetaDef": {"title": "Regional Deep Dive", "description": ""}},
            "meta": {"published": True, "approved": False},
            "children": [
                obj("obj-avg-price", "barchart", "Average Price by Group", {
                    "qDimensions": [dim("Product Group", "Group")],
                    "qMeasures": [meas("Avg([Sales Price])", "Avg Price")],
                }),
            ],
        }
        private = {
            "props": {"qInfo": {"qId": "sheet-private", "qType": "sheet"},
                      "qMetaDef": {"title": "Private scratch", "description": ""}},
            "meta": {"published": False, "approved": False},
            "children": [
                obj("obj-private", "table", "Unreviewed margin", {
                    "qDimensions": [dim("Customer Number")],
                    "qMeasures": [meas("Sum([Sales Quantity])*0.37", "Margin guess")],
                }),
            ],
        }
        # A sheet whose charts follow a user choice, in the three shapes Qlik
        # apps use: a variable set by buttons / a variable input, "show column
        # if" conditions on that variable, and a selection in a data island
        # that holds each choice's formula.
        def column(expr, label, show_when):
            m = meas(expr, label)
            m["qCalcCondition"] = {"qCond": {"qv": show_when}}
            return m

        dynamic = {
            "props": {"qInfo": {"qId": "sheet-dynamic", "qType": "sheet"},
                      "qMetaDef": {"title": "Dynamic Metrics", "description": "Pick a metric, read it by region"}},
            "meta": {"published": True, "approved": True},
            "children": [
                obj("var-metric", "variable", "Metric", None, {
                    "variableName": "vMetric",
                    "alternatives": [{"value": "Sales", "label": "Sales amount"},
                                     {"value": "Quantity", "label": "Units"}],
                }),
                obj("btn-sales", "action-button", "", None, {
                    "style": {"label": "Show sales"},
                    "actions": [{"actionType": "setVariable", "variable": "vMetric", "value": "Sales"}],
                }),
                obj("btn-qty", "action-button", "", None, {
                    "style": {"label": "Show units"},
                    "actions": [{"actionType": "setVariable", "variable": "vMetric", "value": "Quantity"}],
                }),
                obj("obj-dyn-bar", "barchart", "Metric by Region", {
                    "qDimensions": [dim("Region Name")],
                    "qMeasures": [{"qLibraryId": "mm-dynamic", "qDef": {
                        "qLabel": "Metric", "qLabelExpression": "=If(vMetric='Sales','Revenue','Units')"}}],
                }),
                obj("obj-dyn-table", "table", "Metric by Group", {
                    "qDimensions": [dim("Product Group")],
                    "qMeasures": [
                        column("Sum([Sales Quantity]*[Sales Price])", "Revenue", "vMetric='Sales'"),
                        column("Sum([Sales Quantity])", "Units", "vMetric='Quantity'"),
                        meas("Count(distinct [Order Number])", "Orders"),
                    ],
                }),
                obj("obj-island-bar", "barchart", "Selected metric by Year", {
                    "qDimensions": [dim("Year")],
                    "qMeasures": [meas("$(=Only(MetricExpr))", "Selected metric")],
                }),
                obj("obj-metric-pick", "filterpane", "", None, children=[
                    obj("lb-metric", "listbox", "", None, {"qListObjectDef": {"qDef": {"qFieldDefs": ["Metric"]}}}),
                ]),
            ],
        }
        trend = {
            "props": {"qInfo": {"qId": "mv-trend", "qType": "masterobject"}, "visualization": "linechart",
                      "title": "Revenue Trend", "qMetaDef": {"title": "Revenue Trend", "description": "Revenue per year"},
                      "qHyperCubeDef": {"qDimensions": [dim("Year")],
                                        "qMeasures": [meas("Sum([Sales Quantity]*[Sales Price])", "Revenue")]}},
            "children": [],
        }
        app = FakeApp(
            "aaaaaaaa-1111-2222-3333-444444444444",
            rows,
            tables,
            master_measures=[
                {"qInfo": {"qId": "mm-revenue", "qType": "measure"},
                 "qMeasure": {"qDef": "Sum([Sales Quantity]*[Sales Price])", "qLabel": "Revenue"},
                 "qMetaDef": {"title": "Revenue", "description": "Gross revenue in USD"}},
                {"qInfo": {"qId": "mm-dynamic", "qType": "measure"},
                 "qMeasure": {"qDef": "Pick(Match(vMetric,'Sales','Quantity'), Sum([Sales Quantity]*[Sales Price]), Sum([Sales Quantity]))",
                              "qLabel": "Metric"},
                 "qMetaDef": {"title": "Dynamic Metric", "description": "Sales amount or units, per vMetric"}},
                {"qInfo": {"qId": "mm-ne", "qType": "measure"},
                 "qMeasure": {"qDef": "Sum({<[Region Name]={'Northeast'}>} [Sales Quantity]*[Sales Price])", "qLabel": ""},
                 "qMetaDef": {"title": "Northeast Revenue", "description": ""}},
            ],
            master_dimensions=[
                {"qInfo": {"qId": "md-region", "qType": "dimension"},
                 "qDim": {"qGrouping": "N", "qFieldDefs": ["Region Name"]},
                 "qMetaDef": {"title": "Region", "description": "Sales region"}},
            ],
            variables={"vCurrentYear": "2024", "vMetric": "Sales"},
            sheets=[overview, community, private, dynamic],
            islands={"MetricSelector": [
                {"MetricExpr": "Sum([Sales Quantity]*[Sales Price])", "Metric": "Sales"},
                {"MetricExpr": "Sum([Sales Quantity])", "Metric": "Quantity"},
            ]},
            master_objects=[trend],
            script="LOAD * FROM [lib://Demo/sales.qvd] (qvd);",
            lineage=["lib://Demo/sales.qvd"],
        )
        qrs_apps = [{
            "id": app.id,
            "name": "Consumer Sales",
            "published": True,
            "stream": {"id": "str-sales", "name": "Sales"},
            "owner": {"userDirectory": "DEMO", "userId": "app.owner"},
            "lastReloadTime": "2026-11-01T02:00:00.000Z",
        }]
        streams = [{"id": "str-sales", "name": "Sales"}]
        return cls({app.id: app}, qrs_apps, streams)
