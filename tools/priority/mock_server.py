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
* **Subforms are navigation properties** reachable via `$expand`.
* **Rate limiting** returns 429 like Priority Cloud (100 calls/minute).
* **Non-English titles.** ORDERS/CUSTOMERS carry English titles; PART carries
  Hebrew ones, mirroring a real Israeli tenant, so the catalog must never
  assume titles are English or ASCII.

Run:  uv run python tools/priority/mock_server.py [--port 8901]
Then point a Priority ERP connection at:
    service_root = http://127.0.0.1:8901/odata/Priority/tabmock.ini/demo
    PAT          = DEMOTOKEN   (or API user demo/demo)
"""
from __future__ import annotations

import argparse
import base64
import re
import time
from typing import Dict, List, Optional

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

app = FastAPI(title="Mock Priority ERP OData")


# ── schema definition ──────────────────────────────────────────────────────
# (name, edm type, title, mandatory, max_length)
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
        ],
        "subforms": [("ORDERITEMS_SUBFORM", "ORDERITEMS")],
    },
    "ORDERITEMS": {
        "title": "Order Items",
        "key": ["ORDNAME", "KLINE"],
        "columns": [
            ("ORDNAME", "Edm.String", "Order Number", True, "20"),
            ("KLINE", "Edm.Int64", "Line Number", True, None),
            ("PARTNAME", "Edm.String", "Part Number", True, "20"),
            ("PDES", "Edm.String", "Part Description", False, "48"),
            ("TQUANT", "Edm.Decimal", "Quantity", False, None),
            ("PRICE", "Edm.Decimal", "Unit Price", False, None),
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
        "subforms": [],
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

ROWS: Dict[str, List[dict]] = {
    "ORDERS": [
        {"ORDNAME": "SO25001", "CUSTNAME": "1011", "CDES": "Globex", "CURDATE": "2026-01-15T00:00:00Z", "TOTPRICE": 15400.50, "ORDSTATUSDES": "Approved"},
        {"ORDNAME": "SO25002", "CUSTNAME": "1012", "CDES": "Initech", "CURDATE": "2026-02-03T00:00:00Z", "TOTPRICE": 2300.00, "ORDSTATUSDES": "Draft"},
        {"ORDNAME": "SO25003", "CUSTNAME": "1011", "CDES": "Globex", "CURDATE": "2026-03-21T00:00:00Z", "TOTPRICE": 8750.25, "ORDSTATUSDES": "Approved"},
        {"ORDNAME": "SO25004", "CUSTNAME": "1013", "CDES": "Umbrella", "CURDATE": "2026-04-02T00:00:00Z", "TOTPRICE": 640.00, "ORDSTATUSDES": "Closed"},
    ],
    "ORDERITEMS": [
        {"ORDNAME": "SO25001", "KLINE": 1, "PARTNAME": "P-100", "PDES": "Widget", "TQUANT": 10, "PRICE": 1200.00},
        {"ORDNAME": "SO25001", "KLINE": 2, "PARTNAME": "P-200", "PDES": "Gadget", "TQUANT": 3, "PRICE": 1133.50},
        {"ORDNAME": "SO25002", "KLINE": 1, "PARTNAME": "P-100", "PDES": "Widget", "TQUANT": 2, "PRICE": 1150.00},
        {"ORDNAME": "SO25003", "KLINE": 1, "PARTNAME": "P-300", "PDES": "Doohickey", "TQUANT": 5, "PRICE": 1750.05},
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
    "ACME_PROJECTS": [
        {"PROJCODE": "PRJ-1", "PROJDES": "Warehouse rollout"},
    ],
}

# ORDERS.ORDERITEMS_SUBFORM -> child rows keyed on ORDNAME
SUBFORM_LINK = {("ORDERS", "ORDERITEMS_SUBFORM"): ("ORDERITEMS", "ORDNAME")}


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
        parts.append(
            f'        <NavigationProperty Name="{nav_name}" '
            f'Type="Collection(Priority.OData.{target})" ContainsTarget="true"/>'
        )
    parts.append(f'        <Annotation Term="Priority.OData.Description" String="{_esc(spec["title"])}"/>')
    parts.append("      </EntityType>")
    return "\n".join(parts)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _metadata_xml(only: Optional[str] = None) -> str:
    names = [only] if only else list(FORMS)
    types = "\n".join(_entity_type_xml(f) for f in names if f in FORMS)
    sets = "\n".join(
        f'        <EntitySet Name="{f}" EntityType="Priority.OData.{f}"/>'
        for f in names if f in FORMS
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
    """Priority v25.0+ single-entity metadata — cheaper than the full document."""
    err = _check(request)
    if err:
        return err
    if entity not in FORMS:
        return JSONResponse({"error": {"code": "404", "message": f"No such form: {entity}"}}, status_code=404)
    return Response(content=_metadata_xml(only=entity), media_type="application/xml")


# ── query ──────────────────────────────────────────────────────────────────

_FILTER_RE = re.compile(r"^\s*(\w+)\s+(eq|ne|gt|ge|lt|le)\s+(.+?)\s*$", re.I)


def _coerce(raw: str):
    raw = raw.strip()
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def _apply_filter(rows: List[dict], expr: str) -> List[dict]:
    """Single-predicate `$filter`, plus `and` chains. Enough to prove the
    client builds correct OData; not a full expression engine."""
    out = rows
    for clause in re.split(r"\s+and\s+", expr, flags=re.I):
        m = _FILTER_RE.match(clause)
        if not m:
            continue
        field, op, raw = m.group(1), m.group(2).lower(), _coerce(m.group(3))
        def keep(r, field=field, op=op, raw=raw):
            v = r.get(field)
            if v is None:
                return False
            try:
                return {
                    "eq": v == raw, "ne": v != raw, "gt": v > raw,
                    "ge": v >= raw, "lt": v < raw, "le": v <= raw,
                }[op]
            except TypeError:
                return False
        out = [r for r in out if keep(r)]
    return out


@app.get(ROOT + "/{entity}")
def query(entity: str, request: Request):
    err = _check(request)
    if err:
        return err
    if entity not in FORMS:
        return JSONResponse({"error": {"code": "404", "message": f"No such form: {entity}"}}, status_code=404)

    qp = dict(request.query_params)

    # Priority documents no $apply — aggregation is NOT available. Say so
    # loudly instead of quietly returning unaggregated rows.
    if "$apply" in qp:
        return JSONResponse(
            {"error": {"code": "501", "message": "$apply (aggregation) is not supported by Priority OData."}},
            status_code=501,
        )

    rows = [dict(r) for r in ROWS.get(entity, [])]

    if qp.get("$filter"):
        rows = _apply_filter(rows, qp["$filter"])

    if qp.get("$orderby"):
        field, _, direction = qp["$orderby"].strip().partition(" ")
        rows.sort(key=lambda r: (r.get(field) is None, r.get(field)),
                  reverse=direction.strip().lower() == "desc")

    if qp.get("$skip", "").isdigit():
        rows = rows[int(qp["$skip"]):]
    if qp.get("$top", "").isdigit():
        rows = rows[: int(qp["$top"])]

    expand = qp.get("$expand")
    if expand:
        nav = expand.split("(")[0].strip()
        link = SUBFORM_LINK.get((entity, nav))
        if link:
            child_form, key = link
            for r in rows:
                r[nav] = [c for c in ROWS.get(child_form, []) if c.get(key) == r.get(key)]

    if qp.get("$select"):
        keep = [c.strip() for c in qp["$select"].split(",") if c.strip()]
        rows = [{k: v for k, v in r.items() if k in keep or k == expand} for r in rows]

    return {
        "@odata.context": f"{request.base_url}".rstrip("/") + f"{ROOT}/$metadata#{entity}",
        "value": rows,
    }


@app.get("/health")
def health():
    return {"ok": True, "root": ROOT, "forms": sorted(FORMS)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"Priority mock: http://{args.host}:{args.port}{ROOT}  (PAT={VALID_PAT})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
