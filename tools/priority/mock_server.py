#!/usr/bin/env python3
"""Mock Priority ERP OData v4 service for the priority_erp connector.

Priority is licensed ERP software with no public sandbox — the documented demo
tenant is auth-gated — so this stands in at the protocol level: the OData
service root, a real EDMX `$metadata` document carrying Priority's own
annotations, `GetMetadataFor`, and query options over seeded rows.

Deliberately faithful to the details that shape the client:

* **`$metadata` carries no titles on `<Property>`.** The human-readable column
  title lives in `<Annotation Term="Priority.OData.Description" String="..."/>`,
  and mandatory columns in `Priority.OData.Mandatory` (v25.1+). A client that
  parses only `<Property Name= Type=>` gets a catalog of bare column names —
  this mock makes that failure visible.
* **PAT auth is `Basic base64("<PAT>:PAT")`** — the token in the *username*
  position with the literal password `PAT`. Wrong shapes get a 401.
* **No `$apply`.** Priority documents no aggregation, so `$apply` returns 501
  rather than silently succeeding.
* **Unimplemented string functions** (`--no-string-functions`): `contains`,
  `startswith` and `endswith` in `$filter` answer 501 with a reason in the
  body — a server that parses the standard function names without supporting
  them. Off by default.
* **Subforms are contained, not addressable.** A subform (ORDERITEMS under
  ORDERS, ORDERITEMSTEXT under ORDERITEMS) has an `EntityType` but no
  `EntitySet`, exactly as on a real tenant: `GET {root}/ORDERITEMS` is a 404.
  Its rows are reachable only through the parent — `$expand=ORDERITEMS_SUBFORM`
  (with `;`-separated options inside the parentheses, nested `$expand`
  included) or `ORDERS('SO25001')/ORDERITEMS_SUBFORM`. `GetMetadataFor` serves
  top-level forms only ("you can request metadata for ORDERS but not for
  ORDERITEMS").
* **Unknown properties are 400s naming the property**, in `$select`, `$filter`
  and `$expand` alike — the body a client must surface for the agent to fix
  its query.
* **Rate limiting** returns 429 like Priority Cloud (100 calls/minute).
* **Non-English titles.** ORDERS/CUSTOMERS carry English titles; PART carries
  Hebrew ones, mirroring a real Israeli tenant, so the catalog must never
  assume titles are English or ASCII.

**Large-tenant mode** (`--orders N`) adds purchasing forms (SUPPLIERS,
PORDERS, PORDERITEMS) and N generated sales and purchase orders, and makes
every response cost time in proportion to the parent rows it reads — expanded
rows cost more — the way a real tenant does. On such a tenant a subform read
with nothing limiting the parent form runs for minutes; a bounded one returns
in a second. All generated data is synthetic.

Run:  uv run python tools/priority/mock_server.py [--port 8901]
      uv run python tools/priority/mock_server.py --orders 6000   # large + slow
Then point a Priority ERP connection at:
    service_root = http://127.0.0.1:8901/odata/Priority/tabmock.ini/demo
    PAT          = DEMOTOKEN   (or API user demo/demo)
"""
from __future__ import annotations

import argparse
import base64
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

VALID_PAT = "DEMOTOKEN"
VALID_USER = ("demo", "demo")
COMPANY = "demo"
TABULA = "tabmock.ini"
ROOT = f"/odata/Priority/{TABULA}/{COMPANY}"

RATE_LIMIT_PER_MIN = 100
_calls: List[float] = []

# Every request path served, in order — lets a test prove what the client did
# NOT request (e.g. no full $metadata crawl at query time).
REQUEST_LOG: List[str] = []

app = FastAPI(title="Mock Priority ERP OData")


# Optional JSON-lines log of every request (path, decoded query, status,
# seconds) — set by --log, for evidence of what a client actually sent.
ACCESS_LOG_PATH: Optional[str] = None


@app.middleware("http")
async def _log_requests(request, call_next):
    REQUEST_LOG.append(request.url.path)
    started = time.monotonic()
    response = await call_next(request)
    if ACCESS_LOG_PATH:
        import json
        with open(ACCESS_LOG_PATH, "a") as fh:
            fh.write(json.dumps({
                "at": time.strftime("%H:%M:%S"), "path": unquote(request.url.path.split(ROOT, 1)[-1]),
                "query": unquote(request.url.query), "status": response.status_code,
                "seconds": round(time.monotonic() - started, 2)}) + "\n")
    return response


# ── schema definition ──────────────────────────────────────────────────────
# columns: (name, edm type, title, mandatory, max_length)
# subforms: (navigation property name, child form). A form listed as a child
# is a SUBFORM: it gets an EntityType but no EntitySet. Children listed in
# SINGLE_SUBFORMS are single-entity (Priority's text subforms): the navigation
# is not a Collection and $expand yields one object, or null.
FORMS: Dict[str, dict] = {
    "ORDERS": {
        "title": "Customer Orders",
        "key": ["ORDNAME"],
        "columns": [
            ("ORDNAME", "Edm.String", "Order Number", True, "20"),
            ("CUSTNAME", "Edm.String", "Customer Number", True, "20"),
            ("CDES", "Edm.String", "Customer Name", False, "48"),
            ("CURDATE", "Edm.DateTimeOffset", "Order Date", False, None),
            ("TOTPRICE", "Edm.Decimal", "Total Price", False, None),
            ("ORDSTATUSDES", "Edm.String", "Order Status", False, "32"),
            ("STATDES", "Edm.String", "Order Stage", False, "16"),
        ],
        "subforms": [("ORDERITEMS_SUBFORM", "ORDERITEMS"), ("ORDERSTEXT_SUBFORM", "ORDERSTEXT"),
                     ("EXTFILES_SUBFORM", "EXTFILES")],
    },
    # Subform of ORDERS. Like a real tenant, the line has no ORDNAME of its
    # own — the order it belongs to is implied by the containment path — and
    # it shares one column name (STATDES) with its parent.
    "ORDERITEMS": {
        "title": "Order Items",
        "key": ["KLINE"],
        "columns": [
            ("KLINE", "Edm.Int64", "Line Number", True, None),
            ("PARTNAME", "Edm.String", "Part Number", True, "20"),
            ("PDES", "Edm.String", "Part Description", False, "48"),
            ("TQUANT", "Edm.Decimal", "Quantity", False, None),
            ("PRICE", "Edm.Decimal", "Unit Price", False, None),
            ("STATDES", "Edm.String", "Line Status", False, "16"),
        ],
        "subforms": [("ORDERITEMSTEXT_SUBFORM", "ORDERITEMSTEXT")],
    },
    # Second-level subform: ORDERS > ORDERITEMS > ORDERITEMSTEXT.
    "ORDERITEMSTEXT": {
        "title": "Order Item Remarks",
        "key": ["TEXTLINE"],
        "columns": [
            ("TEXTLINE", "Edm.Int64", "Remark Line", True, None),
            ("TEXT", "Edm.String", "Remark", False, "68"),
        ],
        "subforms": [],
    },
    # A second subform of ORDERS, so a query expanding two siblings can be
    # exercised against a real response.
    "ORDERSTEXT": {
        "title": "Order Remarks",
        "key": ["TEXTLINE"],
        "columns": [
            ("TEXTLINE", "Edm.Int64", "Remark Line", True, None),
            ("TEXT", "Edm.String", "Remark", False, "68"),
        ],
        "subforms": [],
    },
    # A generic subform shared by several parents (attachments), as Priority
    # has: one EntityType reached from ORDERS and from CUSTOMERS.
    "EXTFILES": {
        "title": "Attachments",
        "key": ["EXTFILENUM"],
        "columns": [
            ("EXTFILENUM", "Edm.Int64", "Attachment No.", True, None),
            ("EXTFILEDES", "Edm.String", "Description", False, "48"),
        ],
        "subforms": [],
    },
    "CUSTOMERS": {
        "title": "Customers",
        "key": ["CUSTNAME"],
        "columns": [
            ("CUSTNAME", "Edm.String", "Customer Number", True, "20"),
            ("CUSTDES", "Edm.String", "Customer Name", True, "48"),
            ("COUNTRYNAME", "Edm.String", "Country", False, "32"),
            ("BALANCE", "Edm.Decimal", "Balance", False, None),
        ],
        "subforms": [("EXTFILES_SUBFORM", "EXTFILES")],
    },
    # Hebrew titles — a real Israeli tenant returns these, and the catalog must
    # carry them through unmangled rather than assuming English.
    "PART": {
        "title": "פריטים",
        "key": ["PARTNAME"],
        "columns": [
            ("PARTNAME", "Edm.String", "מק\"ט", True, "20"),
            ("PARTDES", "Edm.String", "תיאור פריט", False, "48"),
            ("PRICE", "Edm.Decimal", "מחיר", False, None),
        ],
        "subforms": [],
    },
    # A top-level form with a composite key (part × warehouse). Not curated, so
    # only visible with discover_all.
    "WARHSBAL": {
        "title": "Warehouse Balances",
        "key": ["PARTNAME", "WARHSNAME"],
        "columns": [
            ("PARTNAME", "Edm.String", "Part Number", True, "20"),
            ("WARHSNAME", "Edm.String", "Warehouse", True, "4"),
            ("BALANCE", "Edm.Decimal", "Balance", False, None),
        ],
        "subforms": [],
    },
    # A customer-specific form (four-letter prefix, per Priority's SDK rules).
    # Only visible with discover_all, since it isn't in the curated set.
    "ACME_PROJECTS": {
        "title": "Acme Projects",
        "key": ["PROJCODE"],
        "columns": [
            ("PROJCODE", "Edm.String", "Project Code", True, "20"),
            ("PROJDES", "Edm.String", "Project Description", False, "64"),
        ],
        "subforms": [],
    },
}

SINGLE_SUBFORMS = {"ORDERSTEXT"}
SUBFORMS = {child for spec in FORMS.values() for _, child in spec["subforms"]}
ROOT_FORMS = [f for f in FORMS if f not in SUBFORMS]

# Child rows carry `_p`: the key tuple of the parent row they belong to. It is
# storage only and never leaves the server.
ROWS: Dict[str, List[dict]] = {
    "ORDERS": [
        {"ORDNAME": "SO25001", "CUSTNAME": "1011", "CDES": "Globex", "CURDATE": "2026-01-15T00:00:00Z", "TOTPRICE": 15400.50, "ORDSTATUSDES": "Approved", "STATDES": "Open"},
        {"ORDNAME": "SO25002", "CUSTNAME": "1012", "CDES": "Initech", "CURDATE": "2026-02-03T00:00:00Z", "TOTPRICE": 2300.00, "ORDSTATUSDES": "Draft", "STATDES": "Open"},
        {"ORDNAME": "SO25003", "CUSTNAME": "1011", "CDES": "Globex", "CURDATE": "2026-03-21T00:00:00Z", "TOTPRICE": 8750.25, "ORDSTATUSDES": "Approved", "STATDES": "Final"},
        # An order with no lines at all.
        {"ORDNAME": "SO25004", "CUSTNAME": "1013", "CDES": "Umbrella", "CURDATE": "2026-04-02T00:00:00Z", "TOTPRICE": 640.00, "ORDSTATUSDES": "Closed", "STATDES": "Final"},
    ],
    "ORDERITEMS": [
        {"_p": ("SO25001",), "KLINE": 1, "PARTNAME": "P-100", "PDES": "Widget", "TQUANT": 10, "PRICE": 1200.00, "STATDES": "Supplied"},
        {"_p": ("SO25001",), "KLINE": 2, "PARTNAME": "P-200", "PDES": "Gadget", "TQUANT": 3, "PRICE": 1133.50, "STATDES": "Pending"},
        {"_p": ("SO25002",), "KLINE": 1, "PARTNAME": "P-100", "PDES": "Widget", "TQUANT": 2, "PRICE": 1150.00, "STATDES": "Pending"},
        {"_p": ("SO25003",), "KLINE": 1, "PARTNAME": "P-300", "PDES": "Doohickey and Sons", "TQUANT": 5, "PRICE": 1750.05, "STATDES": "Supplied"},
        {"_p": ("SO25003",), "KLINE": 2, "PARTNAME": "P-400", "PDES": "O'Brien bolt", "TQUANT": 40, "PRICE": 12.75, "STATDES": "Pending"},
    ],
    "ORDERITEMSTEXT": [
        {"_p": ("SO25001", 1), "TEXTLINE": 1, "TEXT": "Deliver to dock 4"},
        {"_p": ("SO25001", 1), "TEXTLINE": 2, "TEXT": "Fragile"},
        {"_p": ("SO25003", 2), "TEXTLINE": 1, "TEXT": "Galvanised only"},
    ],
    "ORDERSTEXT": [
        {"_p": ("SO25001",), "TEXTLINE": 1, "TEXT": "Priority customer"},
    ],
    "EXTFILES": [
        {"_p": ("SO25001",), "EXTFILENUM": 1, "EXTFILEDES": "Signed PO"},
        {"_p": ("1011",), "EXTFILENUM": 1, "EXTFILEDES": "Contract"},
    ],
    "CUSTOMERS": [
        {"CUSTNAME": "1011", "CUSTDES": "Globex", "COUNTRYNAME": "USA", "BALANCE": 24150.75},
        {"CUSTNAME": "1012", "CUSTDES": "Initech", "COUNTRYNAME": "Israel", "BALANCE": 2300.00},
        {"CUSTNAME": "1013", "CUSTDES": "Umbrella", "COUNTRYNAME": "UK", "BALANCE": 640.00},
    ],
    "PART": [
        {"PARTNAME": "P-100", "PARTDES": "Widget", "PRICE": 1200.00},
        {"PARTNAME": "P-200", "PARTDES": "Gadget", "PRICE": 1133.50},
        {"PARTNAME": "P-300", "PARTDES": "Doohickey", "PRICE": 1750.05},
    ],
    "WARHSBAL": [
        {"PARTNAME": "P-100", "WARHSNAME": "Main", "BALANCE": 120},
        {"PARTNAME": "P-100", "WARHSNAME": "East", "BALANCE": 15},
    ],
    "ACME_PROJECTS": [
        {"PROJCODE": "PRJ-1", "PROJDES": "Warehouse rollout"},
    ],
}


def _columns(form: str) -> List[str]:
    return [c[0] for c in FORMS[form]["columns"]]


def _navs(form: str) -> Dict[str, str]:
    return dict(FORMS[form]["subforms"])


def _key_of(form: str, row: dict, parent_key: tuple = ()) -> tuple:
    """A row's full containment key: its parent's key, then its own."""
    return tuple(parent_key) + tuple(row.get(k) for k in FORMS[form]["key"])


_CHILD_INDEX: Dict[str, Dict[tuple, List[dict]]] = {}


def _children(child_form: str, parent_key: tuple) -> List[dict]:
    index = _CHILD_INDEX.get(child_form)
    if index is None or sum(map(len, index.values())) != len(ROWS.get(child_form, [])):
        index = {}
        for r in ROWS.get(child_form, []):
            index.setdefault(tuple(r.get("_p") or ()), []).append(r)
        _CHILD_INDEX[child_form] = index
    return list(index.get(tuple(parent_key), []))


# ── large-tenant mode ──────────────────────────────────────────────────────
# Seconds a response costs: a base, plus per parent row returned (much more
# when a subform is expanded into it), plus per line. 0 = instant (default).
LATENCY = {"base": 0.0, "per_row": 0.0, "per_expanded_row": 0.0, "per_line": 0.0}

_PURCHASING_FORMS: Dict[str, dict] = {
    "SUPPLIERS": {
        "title": "Vendors",
        "key": ["SUPNAME"],
        "columns": [
            ("SUPNAME", "Edm.String", "Vendor Number", True, "16"),
            ("SUPDES", "Edm.String", "Vendor Name", True, "48"),
            ("COUNTRYNAME", "Edm.String", "Country", False, "32"),
        ],
        "subforms": [],
    },
    "PORDERS": {
        "title": "Purchase Orders",
        "key": ["ORDNAME"],
        "columns": [
            ("ORDNAME", "Edm.String", "PO Number", True, "16"),
            ("SUPNAME", "Edm.String", "Vendor Number", True, "16"),
            ("CDES", "Edm.String", "Vendor Name", False, "48"),
            ("CURDATE", "Edm.DateTimeOffset", "Date", False, None),
            ("STATDES", "Edm.String", "Status", False, "16"),
            ("BRANCHNAME", "Edm.String", "Branch", False, "8"),
            ("TOTPRICE", "Edm.Decimal", "Total Price", False, None),
        ],
        "subforms": [("PORDERITEMS_SUBFORM", "PORDERITEMS")],
    },
    "PORDERITEMS": {
        "title": "Purchase Order Items",
        "key": ["KLINE"],
        "columns": [
            ("KLINE", "Edm.Int64", "Line", True, None),
            ("PARTNAME", "Edm.String", "Part Number", True, "20"),
            ("PDES", "Edm.String", "Part Description", False, "48"),
            ("TQUANT", "Edm.Decimal", "Quantity", False, None),
            ("PRICE", "Edm.Decimal", "Unit Price", False, None),
            ("QPRICE", "Edm.Decimal", "Total Price", False, None),
            ("REQDATE", "Edm.DateTimeOffset", "Due Date", False, None),
            ("PORDISTATUSDES", "Edm.String", "Line Status", False, "16"),
        ],
        "subforms": [],
    },
}


def seed_large(orders: int, seed: int = 7) -> None:
    """Add the purchasing forms and `orders` generated sales and purchase
    orders (1-8 lines each, dated over the two years up to 2026-10-15)."""
    import random
    from datetime import datetime, timedelta

    global SUBFORMS, ROOT_FORMS
    FORMS.update(_PURCHASING_FORMS)
    SUBFORMS = {child for spec in FORMS.values() for _, child in spec["subforms"]}
    ROOT_FORMS = [f for f in FORMS if f not in SUBFORMS]

    rnd = random.Random(seed)
    end = datetime(2026, 10, 15)
    parts = [(f"P-{n:04d}", f"{a} {b}") for n, (a, b) in enumerate(
        (a, b) for a in ("Steel", "Copper", "Nylon", "Brass", "Cable", "Valve", "Pump", "Filter")
        for b in ("bracket", "fitting", "gasket", "housing", "seal", "sleeve", "spacer", "clamp"))]
    vendors = [(f"V{n:04d}", f"Vendor {n:04d}") for n in range(1, 61)]
    customers = [(f"C{n:04d}", f"Customer {n:04d}") for n in range(1, 121)]
    ROWS["SUPPLIERS"] = [{"SUPNAME": v, "SUPDES": d, "COUNTRYNAME": rnd.choice(["USA", "Israel", "Germany", "UK"])}
                         for v, d in vendors]
    ROWS.setdefault("PORDERS", [])
    ROWS.setdefault("PORDERITEMS", [])
    statuses = ["Draft", "Approved", "Sent", "Partially received", "Closed"]
    for i in range(orders):
        day = end - timedelta(days=int(730 * i / max(orders, 1)), hours=rnd.randint(0, 23))
        when = day.strftime("%Y-%m-%dT00:00:00Z")
        for form, line_form, prefix, party in (("ORDERS", "ORDERITEMS", "SO", customers),
                                               ("PORDERS", "PORDERITEMS", "PO", vendors)):
            name = f"{prefix}{26 - (730 * i // max(orders, 1)) // 365:02d}{orders - i:06d}"
            code, label = rnd.choice(party)
            total = 0.0
            for k in range(1, rnd.randint(1, 8) + 1):
                pn, pd_ = rnd.choice(parts)
                qty = rnd.choice([1, 2, 5, 10, 20, 50, 100])
                price = round(rnd.uniform(2, 2500), 2)
                total += qty * price
                line = {"_p": (name,), "KLINE": k, "PARTNAME": pn, "PDES": pd_, "TQUANT": qty, "PRICE": price}
                if line_form == "ORDERITEMS":
                    line["STATDES"] = rnd.choice(["Pending", "Supplied"])
                else:
                    line.update({"QPRICE": round(qty * price, 2),
                                 "REQDATE": (day + timedelta(days=rnd.randint(7, 60))).strftime("%Y-%m-%dT00:00:00Z"),
                                 "PORDISTATUSDES": rnd.choice(["Open", "Received", "Partially received"])})
                ROWS[line_form].append(line)
            head = {"ORDNAME": name, "CDES": label, "CURDATE": when, "TOTPRICE": round(total, 2),
                    "STATDES": rnd.choice(statuses)}
            if form == "ORDERS":
                head.update({"CUSTNAME": code, "ORDSTATUSDES": head["STATDES"]})
            else:
                head.update({"SUPNAME": code, "BRANCHNAME": rnd.choice(["HQ", "NORTH", "SOUTH"])})
            ROWS[form].append(head)
    _CHILD_INDEX.clear()


def _response_cost(value: List[dict], expanded: bool) -> float:
    lines = sum(len(v) for row in value for v in row.values() if isinstance(v, list)) if expanded else 0
    per_row = LATENCY["per_expanded_row"] if expanded else LATENCY["per_row"]
    return LATENCY["base"] + len(value) * per_row + lines * LATENCY["per_line"]


# ── auth + rate limiting ───────────────────────────────────────────────────

def _unauthorized(msg: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "401", "message": msg}},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Priority"'},
    )


def _check(request: Request) -> Optional[JSONResponse]:
    """Enforce Priority's auth shapes, then the Cloud rate limit."""
    header = request.headers.get("authorization") or ""

    if header.startswith("Bearer "):
        # Per-user OAuth (on-prem/External ID). Any non-empty token passes;
        # the mock is not an IdP.
        if not header[7:].strip():
            return _unauthorized("Empty bearer token.")
    elif header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
        except Exception:
            return _unauthorized("Malformed Basic credentials.")
        user, _, pwd = decoded.partition(":")
        # PAT: token as username, literal "PAT" as password.
        if pwd == "PAT":
            if user != VALID_PAT:
                return _unauthorized("Unknown Personal Access Token.")
        elif (user, pwd) != VALID_USER:
            return _unauthorized("Invalid API username or password.")
    else:
        return _unauthorized("Authorization header required.")

    now = time.monotonic()
    _calls[:] = [t for t in _calls if now - t < 60.0]
    if len(_calls) >= RATE_LIMIT_PER_MIN:
        return JSONResponse(
            {"error": {"code": "429", "message": "Rate limit exceeded (100 calls/minute)."}},
            status_code=429,
        )
    _calls.append(now)
    return None


def _not_found(what: str) -> JSONResponse:
    return JSONResponse({"error": {"code": "404", "message": f"Resource not found for the segment '{what}'."}},
                        status_code=404)


class _BadRequest(Exception):
    pass


class _NotImplemented(Exception):
    pass


def _no_property(name: str, form: str) -> _BadRequest:
    return _BadRequest(f"Could not find a property named '{name}' on type 'Priority.OData.{form}'.")


# ── $metadata ──────────────────────────────────────────────────────────────

def _entity_type_xml(form: str) -> str:
    spec = FORMS[form]
    parts = [f'      <EntityType Name="{form}">']
    parts.append("        <Key>")
    for k in spec["key"]:
        parts.append(f'          <PropertyRef Name="{k}"/>')
    parts.append("        </Key>")
    for name, edm, title, mandatory, maxlen in spec["columns"]:
        attrs = f'Name="{name}" Type="{edm}"'
        if maxlen:
            attrs += f' MaxLength="{maxlen}"'
        # Titles and mandatory flags are ANNOTATIONS, never attributes — this
        # is the shape a real Priority tenant returns.
        parts.append(f"        <Property {attrs}>")
        parts.append(f'          <Annotation Term="Priority.OData.Description" String="{_esc(title)}"/>')
        if mandatory:
            parts.append('          <Annotation Term="Priority.OData.Mandatory" Bool="true"/>')
        parts.append("        </Property>")
    for nav_name, target in spec["subforms"]:
        etype = f"Priority.OData.{target}"
        if target not in SINGLE_SUBFORMS:
            etype = f"Collection({etype})"
        parts.append(
            f'        <NavigationProperty Name="{nav_name}" Type="{etype}" ContainsTarget="true"/>'
        )
    parts.append(f'        <Annotation Term="Priority.OData.Description" String="{_esc(spec["title"])}"/>')
    parts.append("      </EntityType>")
    return "\n".join(parts)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _with_descendants(form: str) -> List[str]:
    out = [form]
    for _, child in FORMS[form]["subforms"]:
        out.extend(_with_descendants(child))
    return out


def _metadata_xml(only: Optional[str] = None) -> str:
    names = _with_descendants(only) if only else list(FORMS)
    types = "\n".join(_entity_type_xml(f) for f in names)
    # Only top-level forms are entity sets; subforms are contained.
    sets = "\n".join(
        f'        <EntitySet Name="{f}" EntityType="Priority.OData.{f}"/>'
        for f in names if f not in SUBFORMS
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="Priority.OData" xmlns="http://docs.oasis-open.org/odata/ns/edm">
{types}
      <EntityContainer Name="Priority">
{sets}
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


@app.get(ROOT + "/$metadata")
def metadata(request: Request):
    err = _check(request)
    if err:
        return err
    return Response(content=_metadata_xml(), media_type="application/xml")


@app.get(ROOT + "/GetMetadataFor(entity='{entity}')")
def metadata_for(entity: str, request: Request):
    """Priority v25.0+ single-entity metadata — top-level forms only."""
    err = _check(request)
    if err:
        return err
    if entity not in FORMS or entity in SUBFORMS:
        return JSONResponse({"error": {"code": "404", "message": f"No such form: {entity}"}}, status_code=404)
    return Response(content=_metadata_xml(only=entity), media_type="application/xml")


# ── $filter: a small but real expression evaluator ─────────────────────────
# Enough of OData's grammar to judge the client's queries honestly: and / or /
# not, parentheses, comparisons, contains/startswith/endswith, quoted strings
# with '' escapes, numbers, unquoted DateTimeOffset literals, null/true/false.
# Unknown properties raise, as Priority does.

_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<str>'(?:[^']|'')*')
    | (?P<num>-?\d[\d\-:.TZ+]*)
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<sym>[(),/])
    )""", re.X)

_CMP = {"eq", "ne", "gt", "ge", "lt", "le"}

# String functions parsed but not implemented, answered with a 501 — a server
# that knows the standard OData v4 function names without supporting them.
# Empty by default; `--no-string-functions` fills it.
UNIMPLEMENTED_FUNCTIONS: set = set()


def _tokenize(expr: str) -> List[Tuple[str, str]]:
    out, pos, expr = [], 0, expr.strip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise _BadRequest(f"Syntax error in $filter at position {pos}.")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
        while pos < len(expr) and expr[pos].isspace():
            pos += 1
    return out


def _literal(kind: str, text: str):
    if kind == "str":
        return text[1:-1].replace("''", "'")
    if "-" in text[1:] or ":" in text:  # DateTimeOffset, compared as ISO text
        return text
    return float(text) if "." in text else int(text)


def _compile_filter(expr: str, form: str) -> Callable[[dict], bool]:
    toks = _tokenize(expr)
    pos = 0
    cols = set(_columns(form))

    def peek(i=0):
        return toks[pos + i] if pos + i < len(toks) else (None, None)

    def take():
        nonlocal pos
        tok = toks[pos]
        pos += 1
        return tok

    def expect(sym):
        k, t = take() if pos < len(toks) else (None, None)
        if t != sym:
            raise _BadRequest(f"Syntax error in $filter: expected '{sym}'.")

    def operand():
        k, t = peek()
        if k is None:
            raise _BadRequest("Syntax error in $filter: unexpected end.")
        if t == "(":
            take()
            node = or_()
            expect(")")
            return node
        if k == "word" and t.lower() in ("contains", "startswith", "endswith") and peek(1)[1] == "(":
            if t.lower() in UNIMPLEMENTED_FUNCTIONS:
                raise _NotImplemented(f"The function '{t}' is not implemented.")
            take()
            take()
            a = operand()
            expect(",")
            b = operand()
            expect(")")
            fn = {"contains": lambda x, y: y in x, "startswith": lambda x, y: x.startswith(y),
                  "endswith": lambda x, y: x.endswith(y)}[t.lower()]
            return lambda r: (lambda x, y: isinstance(x, str) and isinstance(y, str) and fn(x, y))(a(r), b(r))
        take()
        if k in ("str", "num"):
            v = _literal(k, t)
            return lambda r: v
        if t.lower() in ("null", "true", "false"):
            v = {"null": None, "true": True, "false": False}[t.lower()]
            return lambda r: v
        if t not in cols:
            raise _no_property(t, form)
        return lambda r: r.get(t)

    def comparison():
        left = operand()
        k, t = peek()
        if k == "word" and t.lower() in _CMP:
            take()
            op = t.lower()
            right = operand()

            def cmp(r):
                a, b = left(r), right(r)
                if op == "eq":
                    return a == b
                if op == "ne":
                    return a != b
                if a is None or b is None:
                    return False
                try:
                    return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
                except TypeError:
                    return False
            return cmp
        return left

    def not_():
        k, t = peek()
        if k == "word" and t.lower() == "not":
            take()
            inner = not_()
            return lambda r: not inner(r)
        return comparison()

    def and_():
        node = not_()
        while peek()[0] == "word" and peek()[1].lower() == "and":
            take()
            right = not_()
            node = (lambda a, b: lambda r: bool(a(r)) and bool(b(r)))(node, right)
        return node

    def or_():
        node = and_()
        while peek()[0] == "word" and peek()[1].lower() == "or":
            take()
            right = and_()
            node = (lambda a, b: lambda r: bool(a(r)) or bool(b(r)))(node, right)
        return node

    tree = or_()
    if pos != len(toks):
        raise _BadRequest(f"Syntax error in $filter near '{toks[pos][1]}'.")
    return lambda r: bool(tree(r))


# ── query options ──────────────────────────────────────────────────────────

def _split_top(text: str, seps: str) -> List[str]:
    """Split on any of `seps` outside parentheses and quoted strings."""
    out, depth, quote, cur = [], 0, False, []
    for ch in text:
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        if not quote and depth == 0 and ch in seps:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def _parse_options(pairs: List[Tuple[str, str]]) -> Dict[str, str]:
    opts: Dict[str, str] = {}
    for k, v in pairs:
        opts[k.strip()] = v
    return opts


def _expand_items(expand: str) -> List[Tuple[str, Dict[str, str]]]:
    """`A($filter=x;$select=y),B` → [("A", {...}), ("B", {})]."""
    items = []
    for part in _split_top(expand, ","):
        name, _, rest = part.partition("(")
        inner = rest[:-1] if rest.endswith(")") else rest
        opts = _parse_options([tuple(p.split("=", 1)) for p in _split_top(inner, ";") if "=" in p])
        items.append((name.strip(), opts))
    return items


def _shape(form: str, rows: List[Tuple[dict, tuple]], opts: Dict[str, str]) -> List[dict]:
    """Apply $filter/$orderby/$skip/$top/$expand/$select to (row, key) pairs of
    one form, recursing into expanded subforms."""
    cols = _columns(form)

    if opts.get("$filter"):
        pred = _compile_filter(opts["$filter"], form)
        rows = [(r, k) for r, k in rows if pred(r)]

    if opts.get("$orderby"):
        for part in reversed(_split_top(opts["$orderby"], ",")):
            field, _, direction = part.partition(" ")
            if field not in cols:
                raise _no_property(field, form)
            rows.sort(key=lambda rk: (rk[0].get(field) is None, rk[0].get(field)),
                      reverse=direction.strip().lower() == "desc")

    if opts.get("$skip", "").isdigit():
        rows = rows[int(opts["$skip"]):]
    if opts.get("$top", "").isdigit():
        rows = rows[: int(opts["$top"])]

    select = [c for c in _split_top(opts.get("$select", ""), ",")]
    for c in select:
        if c not in cols:
            raise _no_property(c, form)

    expands = _expand_items(opts["$expand"]) if opts.get("$expand") else []
    navs = _navs(form)
    for nav, _ in expands:
        if nav not in navs:
            raise _no_property(nav, form)

    out = []
    for row, key in rows:
        shaped = {c: row.get(c) for c in (select or cols)}
        for nav, sub_opts in expands:
            child = navs[nav]
            child_rows = [(c, _key_of(child, c, key)) for c in _children(child, key)]
            shaped_children = _shape(child, child_rows, sub_opts)
            if child in SINGLE_SUBFORMS:
                shaped[nav] = shaped_children[0] if shaped_children else None
            else:
                shaped[nav] = shaped_children
        out.append(shaped)
    return out


_SEGMENT_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\((?P<key>[^)]*)\))?$")


def _parse_key(raw: str) -> List[Any]:
    vals = []
    for part in _split_top(raw, ","):
        part = part.split("=", 1)[-1].strip()
        vals.append(_literal("str", part) if part.startswith("'") else _literal("num", part))
    return vals


@app.get(ROOT + "/{path:path}")
def query(path: str, request: Request):
    err = _check(request)
    if err:
        return err

    qp = dict(request.query_params)
    # Priority documents no $apply — aggregation is NOT available. Say so
    # loudly instead of quietly returning unaggregated rows.
    if "$apply" in qp:
        return JSONResponse(
            {"error": {"code": "501", "message": "$apply (aggregation) is not supported by Priority OData."}},
            status_code=501,
        )

    segments = unquote(path).split("/")
    first = _SEGMENT_RE.match(segments[0])
    # Only an EntitySet is addressable at the root — a subform is a 404 here.
    if not first or first.group("name") not in ROOT_FORMS:
        return _not_found(segments[0])

    form = first.group("name")
    rows: List[Tuple[dict, tuple]] = [(r, _key_of(form, r)) for r in ROWS.get(form, [])]
    single = False
    try:
        for i, seg in enumerate(segments):
            m = _SEGMENT_RE.match(seg)
            if not m:
                return _not_found(seg)
            if i > 0:
                # Navigate from the single entity selected so far into a subform.
                nav = m.group("name")
                if nav not in _navs(form) or len(rows) != 1:
                    return _not_found(seg)
                parent_key = rows[0][1]
                form = _navs(form)[nav]
                rows = [(c, _key_of(form, c, parent_key)) for c in _children(form, parent_key)]
                single = form in SINGLE_SUBFORMS
                if single and not rows:
                    return _not_found(seg)
            if m.group("key") is not None:
                want = _parse_key(m.group("key"))
                rows = [(r, k) for r, k in rows if list(k[-len(want):]) == want]
                if not rows:
                    return _not_found(seg)
                single = True
        value = _shape(form, rows, qp)
    except _BadRequest as e:
        return JSONResponse({"error": {"code": "", "message": str(e)}}, status_code=400)
    except _NotImplemented as e:
        return JSONResponse({"error": {"code": "501", "message": str(e)}}, status_code=501)

    cost = _response_cost(value, bool(qp.get("$expand")))
    if cost > 0:
        time.sleep(cost)
    context = f"{request.base_url}".rstrip("/") + f"{ROOT}/$metadata#{form}"
    if single:
        return {"@odata.context": context, **value[0]}
    return {"@odata.context": context, "value": value}


@app.get("/health")
def health():
    return {"ok": True, "root": ROOT, "forms": sorted(ROOT_FORMS)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--orders", type=int, default=0,
                    help="large-tenant mode: generate this many sales and purchase orders")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="latency multiplier in large-tenant mode (0 = instant)")
    ap.add_argument("--log", help="append a JSON line per request to this file")
    ap.add_argument("--no-string-functions", action="store_true",
                    help="answer contains/startswith/endswith in $filter with a 501")
    args = ap.parse_args()
    ACCESS_LOG_PATH = args.log
    if args.no_string_functions:
        UNIMPLEMENTED_FUNCTIONS.update({"contains", "startswith", "endswith"})
    if args.orders:
        seed_large(args.orders)
        LATENCY.update({k: v * args.speed for k, v in
                        {"base": 0.25, "per_row": 0.002, "per_expanded_row": 0.03, "per_line": 0.004}.items()})
        print(f"large tenant: {len(ROWS['PORDERS'])} purchase orders, "
              f"{len(ROWS['PORDERITEMS'])} purchase order lines")
    print(f"Priority mock: http://{args.host}:{args.port}{ROOT}  (PAT={VALID_PAT})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
