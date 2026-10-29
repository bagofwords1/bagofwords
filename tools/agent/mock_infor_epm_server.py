#!/usr/bin/env python3
"""Mock Infor EPM Application Engine REST API for verifying the ``infor_epm``
connector WITHOUT an Infor farm.

It emulates the surface the connector talks to — an ION API Gateway token
endpoint plus the three published BOW_* processes — over a small in-memory
OLAP model with MOCK names (database ``DEMO_OLAP``, cubes ``Sales`` and
``Finance``). It serves canned/derived data only; it is NOT an Infor product.

Endpoints (default port 8765):

    POST /token                                    client-credentials -> access_token
    POST /api/rest/BowService/v1/<Process>         synchronous call   -> {"result": "<string>"}
    POST /api/rest/BowService/v1/<Process>/async   async call         -> {"taskId": "..."}
    POST /api/rest/BowService/v1/getasyncresult    {"taskId"}         -> {"status", "result"}

Processes: BOW_GetCubeList, BOW_GetCubeSchema(CubeName), BOW_ExecuteMdx(mdxQuery, rowLimit).
Credentials: client_id ``demo-client`` / client_secret ``demo-secret`` (token
endpoint) or the static bearer token ``demo-bearer-token``.

Run:  python tools/agent/mock_infor_epm_server.py [--port 8765]
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from xml.sax.saxutils import escape

DATABASE = "DEMO_OLAP"
CLIENT_ID = "demo-client"
CLIENT_SECRET = "demo-secret"
STATIC_BEARER = "demo-bearer-token"
SERVICE_PREFIX = "/api/rest/BowService/v1"
ALEA_NS = "http://www.misag.com"


# ---------------------------------------------------------------------------
# In-memory OLAP model (mock names)
# ---------------------------------------------------------------------------

class Dimension:
    def __init__(self, name, tree, odbo_type="3", description=None, levels=("Level 1", "Level 2")):
        self.name = name
        self.tree = tree  # {parent: [children]}; roots are keys not appearing as children
        self.odbo_type = odbo_type
        self.description = description or name
        self.levels = list(levels)
        self.parent = {c: p for p, kids in tree.items() for c in kids}
        self.elements = []
        for p, kids in tree.items():
            if p not in self.elements:
                self.elements.append(p)
            for c in kids:
                if c not in self.elements:
                    self.elements.append(c)

    @property
    def roots(self):
        return [e for e in self.elements if e not in self.parent]

    @property
    def default_element(self):
        return self.roots[0]

    def children(self, elem):
        return list(self.tree.get(elem, []))

    def leaves(self, elem):
        kids = self.children(elem)
        if not kids:
            return [elem]
        out = []
        for k in kids:
            out.extend(self.leaves(k))
        return out

    def has(self, elem):
        return elem in self.elements


PERIOD = Dimension(
    "Period",
    {"All": ["2024", "2025"],
     "2024": ["2024-Q1", "2024-Q2", "2024-Q3", "2024-Q4"],
     "2025": ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"]},
    odbo_type="1", description="Fiscal periods", levels=("Total", "Year", "Quarter"),
)
REGION = Dimension("Region", {"All": ["North", "South", "East", "West"]}, description="Sales regions")
PRODUCT = Dimension("Product", {"All": ["Bikes", "Helmets", "Apparel"]}, description="Product lines")
SALES_MEASURES = Dimension("SalesMeasures", {"Revenue": [], "Units": [], "Cost": []},
                           description="Sales measures", levels=("Level 1",))
COMPANY = Dimension("Company", {"All": ["Acme Corp", "Globex Inc"]}, description="Legal entities")
ACCOUNT = Dimension("Account", {"All": ["Revenue", "COGS", "Opex"]}, description="P&L accounts")
FIN_MEASURES = Dimension("FinMeasures", {"Amount": [], "Budget": []}, odbo_type="2",
                         description="Finance measures", levels=("Level 1",))


class Cube:
    def __init__(self, name, dims, description):
        self.name = name
        self.dims = dims
        self.description = description
        self.by_name = {d.name.lower(): d for d in dims}
        self._leaf_cache = {}

    def dim(self, name):
        return self.by_name.get(name.lower())

    def leaf_value(self, coords):
        """Deterministic pseudo-data for one leaf coordinate tuple."""
        key = tuple(coords)
        if key not in self._leaf_cache:
            h = 0
            for part in key:
                for ch in part:
                    h = (h * 131 + ord(ch)) % 1_000_003
            base = 1000 + (h % 9000)
            measure = key[-1]
            if measure in ("Units",):
                base = 10 + (h % 490)
            elif measure in ("Cost", "COGS", "Opex"):
                base = int(base * 0.6)
            elif measure == "Budget":
                base = int(base * 1.1)
            self._leaf_cache[key] = float(base)
        return self._leaf_cache[key]

    def value(self, coords):
        """Aggregate over consolidated elements by summing their leaves."""
        leaf_lists = [d.leaves(e) for d, e in zip(self.dims, coords, strict=False)]
        total = 0.0
        for combo in itertools.product(*leaf_lists):
            total += self.leaf_value(combo)
        return total


CUBES = {
    "Sales": Cube("Sales", [PERIOD, REGION, PRODUCT, SALES_MEASURES], "Sales by region and product"),
    "Finance": Cube("Finance", [PERIOD, COMPANY, ACCOUNT, FIN_MEASURES], "P&L by company and account"),
}
SYSTEM_CUBES = ["#SysConfig"]  # excluded by the '#' convention


# ---------------------------------------------------------------------------
# BOW_* process implementations
# ---------------------------------------------------------------------------

def bow_get_cube_list(params):
    db = params.get("olapname", "")
    if db and db != DATABASE:
        return f"BOW_ERROR: OLAP database '{db}' not found"
    return "\n".join(list(CUBES) + SYSTEM_CUBES)


def _alea_dimension_properties(dim: Dimension) -> str:
    levels = "".join(
        f'<Alea:Level Name="{escape(lvl)}" Number="{i}"/>' for i, lvl in enumerate(dim.levels)
    )
    hier = dim.name.upper()
    return (
        f'<Alea:Document xmlns:Alea="{ALEA_NS}"><Alea:Request RequestID="001"><Alea:Return>'
        f'<Alea:Properties xmlns:Alea="{ALEA_NS}">'
        f'<Alea:Translation LocaleIdentifier="0"><Alea:Description>{escape(dim.description)}</Alea:Description></Alea:Translation>'
        f'<Alea:ODBOType Code="{dim.odbo_type}"/>'
        f'<Alea:DefaultHierarchy Name="{escape(dim.name)}"/>'
        f'<Alea:Hierarchy Name="{hier}"><Alea:Properties xmlns:Alea="{ALEA_NS}">'
        f'<Alea:LevelNames>{levels}</Alea:LevelNames>'
        f'<Alea:Translation LocaleIdentifier="0"><Alea:Description>{hier}</Alea:Description></Alea:Translation>'
        f'</Alea:Properties></Alea:Hierarchy>'
        f'</Alea:Properties></Alea:Return></Alea:Request></Alea:Document>'
    )


def bow_get_cube_schema(params):
    cube_name = params.get("cubename", "")
    cube = next((c for c in CUBES.values() if c.name.lower() == cube_name.lower()), None)
    if cube is None:
        return f"BOW_ERROR: Cube '{cube_name}' does not exist"
    parts = [f'<BOWSchema cube="{escape(cube.name)}" db="{DATABASE}">']
    parts.append(
        f'<BOWSection kind="cube"><Alea:Document xmlns:Alea="{ALEA_NS}"><Alea:Request RequestID="001">'
        f'<Alea:Return><Alea:Properties xmlns:Alea="{ALEA_NS}"><Alea:Description>{escape(cube.description)}'
        f'</Alea:Description></Alea:Properties></Alea:Return></Alea:Request></Alea:Document></BOWSection>'
    )
    for pos, dim in enumerate(cube.dims, start=1):
        sample = "".join(f"<BOWElement>{escape(e)}</BOWElement>" for e in dim.elements[:20])
        parts.append(
            f'<BOWSection kind="dimension" name="{escape(dim.name)}" position="{pos}">'
            f"{_alea_dimension_properties(dim)}"
            f'<BOWElements count="{len(dim.elements)}">{sample}</BOWElements>'
            f"</BOWSection>"
        )
    parts.append("</BOWSchema>")
    return "".join(parts)


# --- a deliberately small MDX evaluator ---------------------------------------

_TOKEN_RE = re.compile(r"\s*(\[[^\]]*\]|\{|\}|\(|\)|,|\*|\.|[A-Za-z_][A-Za-z0-9_]*)")


class MdxError(Exception):
    pass


def _tokenize(text):
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise MdxError(f"Unexpected token near: {text[pos:pos + 20]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _SetParser:
    """Parses an axis set expression into a list of tuples, each tuple being a
    list of (dimension, element) pairs. Supports ``{...}``, member paths,
    ``.Members`` / ``.Children`` / ``.AllMembers``, ``CROSSJOIN(a, b)``, ``a * b``
    and ``(m1, m2)`` tuples — enough for the agent's typical queries."""

    def __init__(self, tokens, cube: Cube):
        self.t = tokens
        self.i = 0
        self.cube = cube

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, expected=None):
        tok = self.peek()
        if tok is None or (expected is not None and tok.upper() != expected.upper()):
            raise MdxError(f"Expected {expected!r}, got {tok!r}")
        self.i += 1
        return tok

    def parse_set(self):
        left = self.parse_term()
        while self.peek() == "*":
            self.take("*")
            right = self.parse_term()
            left = [a + b for a in left for b in right]
        return left

    def parse_term(self):
        tok = self.peek()
        if tok == "{":
            self.take("{")
            tuples = []
            if self.peek() != "}":
                tuples.extend(self.parse_set())
                while self.peek() == ",":
                    self.take(",")
                    tuples.extend(self.parse_set())
            self.take("}")
            return tuples
        if tok == "(":
            self.take("(")
            members = self.parse_member()
            while self.peek() == ",":
                self.take(",")
                members = [a + b for a in members for b in self.parse_member()]
            self.take(")")
            return members
        if tok and tok.upper() == "CROSSJOIN":
            self.take()
            self.take("(")
            left = self.parse_set()
            self.take(",")
            right = self.parse_set()
            self.take(")")
            return [a + b for a in left for b in right]
        if tok and tok.upper() in ("NONEMPTY", "NON"):
            raise MdxError("NON EMPTY must prefix the axis, e.g. 'NON EMPTY {...} ON ROWS'")
        return self.parse_member()

    def parse_member(self):
        path = []
        if not (self.peek() or "").startswith("["):
            raise MdxError(f"Expected a [Dimension] reference, got {self.peek()!r}")
        path.append(self.take()[1:-1])
        func = None
        while self.peek() == ".":
            self.take(".")
            nxt = self.take()
            if nxt.startswith("["):
                path.append(nxt[1:-1])
            else:
                func = nxt.upper()
                if func not in ("MEMBERS", "CHILDREN", "ALLMEMBERS"):
                    raise MdxError(f"Unsupported MDX function .{nxt}")
        dim = self.cube.dim(path[0])
        if dim is None:
            raise MdxError(f"Unknown dimension [{path[0]}] in cube [{self.cube.name}]")
        # [Dim].[Hier].[Elem] — the middle segment is a hierarchy name we accept
        # when it matches the dimension.
        elem = path[-1] if len(path) > 1 else None
        if len(path) == 3 and path[1].lower() != dim.name.lower():
            raise MdxError(f"Unknown hierarchy [{path[1]}] on dimension [{dim.name}]")
        if func in ("MEMBERS", "ALLMEMBERS"):
            if elem is not None:
                raise MdxError(".Members applies to a dimension, e.g. [Region].Members")
            return [[(dim, e)] for e in dim.elements]
        if func == "CHILDREN":
            if elem is None or not dim.has(elem):
                raise MdxError(f"Unknown element [{elem}] in dimension [{dim.name}]")
            return [[(dim, e)] for e in dim.children(elem)]
        if elem is None:
            raise MdxError(f"[{dim.name}] needs an element or .Members")
        if not dim.has(elem):
            raise MdxError(f"Unknown element [{elem}] in dimension [{dim.name}]")
        return [[(dim, elem)]]


_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?P<axes>.+?)\s+FROM\s+\[(?P<cube>[^\]]+)\]\s*(?:WHERE\s*(?:\((?P<where>.+)\)|(?P<where_bare>.+?)))?\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_AXIS_SPLIT_RE = re.compile(r"\s+ON\s+(COLUMNS|ROWS|AXIS\s*\(\s*\d\s*\)|\d)\s*,?", re.IGNORECASE)


def execute_mdx(mdx: str):
    m = _SELECT_RE.match(mdx or "")
    if not m:
        raise MdxError("Statement must be: SELECT <set> ON COLUMNS[, <set> ON ROWS] FROM [Cube] [WHERE <tuple>]")
    cube = next((c for c in CUBES.values() if c.name.lower() == m.group("cube").lower()), None)
    if cube is None:
        raise MdxError(f"Cube [{m.group('cube')}] does not exist")

    axes = []
    text = m.group("axes")
    pieces = _AXIS_SPLIT_RE.split(text)
    # pieces = [set0, axis0, set1, axis1, ..., trailing]
    for j in range(0, len(pieces) - 1, 2):
        set_text, axis_name = pieces[j].strip(), pieces[j + 1].upper().replace(" ", "")
        non_empty = False
        if set_text.upper().startswith("NON EMPTY"):
            non_empty = True
            set_text = set_text[len("NON EMPTY"):].strip()
        idx = {"COLUMNS": 0, "ROWS": 1, "0": 0, "1": 1, "AXIS(0)": 0, "AXIS(1)": 1}.get(axis_name)
        if idx is None:
            raise MdxError(f"Unsupported axis {axis_name}")
        parser = _SetParser(_tokenize(set_text), cube)
        tuples = parser.parse_set()
        if parser.peek() is not None:
            raise MdxError(f"Unexpected token {parser.peek()!r} in axis {axis_name}")
        axes.append((idx, tuples, non_empty))
    axes.sort(key=lambda a: a[0])
    if not axes:
        raise MdxError("At least one axis is required")

    slicer = {}
    where_text = m.group("where") or m.group("where_bare")
    if where_text:
        parser = _SetParser(_tokenize(where_text), cube)
        for tup in parser.parse_set():
            for dim, elem in tup:
                slicer[dim.name] = elem

    cells = []
    axis_tuples = [a[1] for a in axes]
    for combo in itertools.product(*axis_tuples):
        coords = {}
        ordered = []
        for tup in combo:
            for dim, elem in tup:
                coords[dim.name] = elem
                ordered.append(f"[{dim.name}].[{dim.name}].[{elem}]")
        full = []
        for dim in cube.dims:
            full.append(coords.get(dim.name) or slicer.get(dim.name) or dim.default_element)
        cells.append((ordered, cube.value(full)))
    return cells


def bow_execute_mdx(params):
    mdx = (params.get("mdxquery") or "").strip()
    if not mdx:
        return "BOW_ERROR: parameter mdxQuery is empty"
    try:
        limit = int(params.get("rowlimit") or 1000)
    except (TypeError, ValueError):
        limit = 1000
    if limit <= 0:
        limit = 1000
    try:
        cells = execute_mdx(mdx)
    except MdxError as exc:
        return f"BOW_ERROR: {exc}"
    if not cells:
        return "BOW_ERROR: MDX returned no cells"
    lines = []
    for ordered, value in cells[:limit]:
        lines.append("\t".join(ordered) + f"\tNUM\t{value:g}")
    out = "\n".join(lines) + "\n"
    if len(cells) > limit:
        out += "BOW_TRUNCATED\n"
    return out


PROCESSES = {
    "bow_getcubelist": bow_get_cube_list,
    "bow_getcubeschema": bow_get_cube_schema,
    "bow_executemdx": bow_execute_mdx,
}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

_tokens = set()
_tasks = {}
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[EPM]", self.command, self.path, fmt % args, flush=True)

    def _send(self, code=200, body=None):
        payload = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {}
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:].strip()
        return token == STATIC_BEARER or token in _tokens

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        body = self._read_body()

        if path == "/token":
            if body.get("grant_type") != "client_credentials":
                return self._send(400, {"error": "unsupported_grant_type"})
            if body.get("client_id") != CLIENT_ID or body.get("client_secret") != CLIENT_SECRET:
                return self._send(401, {"error": "invalid_client"})
            token = f"mock-{uuid.uuid4().hex}"
            with _lock:
                _tokens.add(token)
            return self._send(200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600})

        if not path.startswith(SERVICE_PREFIX + "/"):
            return self._send(404, {"error": f"unknown route {path}"})
        if not self._authorized():
            return self._send(401, {"error": "The HTTP request is unauthorized (Bearer realm=\"IONAPI\")"})

        rest = path[len(SERVICE_PREFIX) + 1:]
        params = {str(k).lower(): v for k, v in (body or {}).items()}

        if rest == "getasyncresult":
            task_id = params.get("taskid")
            with _lock:
                task = _tasks.get(task_id)
            if not task:
                return self._send(200, {"error": f"Unknown taskId {task_id}"})
            # First poll reports Running so the client's polling loop is exercised.
            if task["polls"] == 0:
                task["polls"] += 1
                return self._send(200, {"status": "Running", "taskId": task_id})
            return self._send(200, {"status": "Completed", "taskId": task_id, "result": task["result"]})

        is_async = rest.endswith("/async")
        process = rest[:-len("/async")] if is_async else rest
        fn = PROCESSES.get(process.lower())
        if fn is None:
            return self._send(404, {"error": f"Process '{process}' is not published"})
        result = fn(params)
        if is_async:
            task_id = uuid.uuid4().hex
            with _lock:
                _tasks[task_id] = {"result": result, "polls": 0}
            return self._send(200, {"taskId": task_id})
        return self._send(200, {"result": result})


def serve(port: int):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"mock Infor EPM Application Engine listening on :{port}  (api base http://localhost:{port}{SERVICE_PREFIX})", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    serve(args.port)
