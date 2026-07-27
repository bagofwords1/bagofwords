#!/usr/bin/env python3
"""Mock SAP Datasphere OData Consumption API for the sap_datasphere feedback loop.

SAP Datasphere is cloud-only SaaS — there is no Docker/local build — so this
stands in for a tenant at the protocol level: OAuth token endpoint + catalog
API + analytical/relational OData with real server-side aggregation and
`$metadata` carrying SAP measure annotations. It is *identity-aware*: the
client_credentials "technical user" sees the full catalog but gets an EMPTY
result from a Data-Access-Control-protected model (reproducing the documented
Datasphere behavior), while a per-user authorization_code token sees only that
user's rows.

Run:  uv run python tools/datasphere/mock_server.py [--port 8899]
Then point a SAP Datasphere connection at:
    host        = http://127.0.0.1:8899
    token_url   = http://127.0.0.1:8899/oauth/token
    client_id / client_secret = anything (the mock accepts all)
"""
from __future__ import annotations

import argparse
from typing import Dict, List, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn


# --- seed semantic models --------------------------------------------------
# Each model: dimensions, measures, and raw (un-aggregated) fact rows. The
# analytical endpoint aggregates measures over whichever dimensions the caller
# selects — exactly like a Datasphere analytic model.

MODELS = {
    ("SALES", "SalesAnalyticModel"): {
        "label": "Sales Analytic Model",
        "dims": ["Country", "Product", "Year"],
        "measures": ["Revenue", "Quantity"],
        "dac_protected": False,
        "rows": [
            {"Country": "US", "Product": "Widget", "Year": "2024", "Revenue": 1200, "Quantity": 120},
            {"Country": "US", "Product": "Gadget", "Year": "2024", "Revenue": 800, "Quantity": 40},
            {"Country": "US", "Product": "Widget", "Year": "2025", "Revenue": 1500, "Quantity": 150},
            {"Country": "DE", "Product": "Widget", "Year": "2024", "Revenue": 600, "Quantity": 60},
            {"Country": "DE", "Product": "Gadget", "Year": "2025", "Revenue": 950, "Quantity": 48},
            {"Country": "FR", "Product": "Gadget", "Year": "2025", "Revenue": 700, "Quantity": 35},
            {"Country": "FR", "Product": "Widget", "Year": "2024", "Revenue": 300, "Quantity": 30},
            {"Country": "JP", "Product": "Widget", "Year": "2025", "Revenue": 1100, "Quantity": 90},
        ],
    },
    ("FINANCE", "ExpensesModel"): {
        "label": "Expenses Analytic Model",
        "dims": ["Department", "Category"],
        "measures": ["Amount"],
        "dac_protected": False,
        "rows": [
            {"Department": "R&D", "Category": "Travel", "Amount": 4200},
            {"Department": "R&D", "Category": "Software", "Amount": 9800},
            {"Department": "Sales", "Category": "Travel", "Amount": 6100},
            {"Department": "Sales", "Category": "Events", "Amount": 15000},
            {"Department": "Ops", "Category": "Software", "Amount": 3300},
        ],
    },
    # DAC-protected: technical user gets empty; per-user token gets their slice.
    ("HR", "SalariesModel"): {
        "label": "Salaries (row-level secured)",
        "dims": ["Department", "Level"],
        "measures": ["Headcount", "TotalComp"],
        "dac_protected": True,
        "rows": [
            {"Department": "R&D", "Level": "Senior", "Headcount": 12, "TotalComp": 2400000},
            {"Department": "Sales", "Level": "Junior", "Headcount": 20, "TotalComp": 1600000},
        ],
    },
}

NUMERIC_MEASURE = True  # measures rendered as Edm.Decimal in $metadata

app = FastAPI(title="Mock SAP Datasphere")


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# --- OAuth -----------------------------------------------------------------

@app.post("/oauth/token")
async def token(request: Request):
    form = {}
    try:
        form = dict(await request.form())
    except Exception:
        pass
    grant = form.get("grant_type", "client_credentials")
    if grant == "authorization_code":
        identity = "user:" + (form.get("code") or "alice")
        refresh = "refresh-" + identity
    elif grant == "refresh_token":
        identity = (form.get("refresh_token") or "refresh-user:alice").replace("refresh-", "")
        refresh = form.get("refresh_token")
    else:
        identity = "tech-user"
        refresh = None
    body = {"access_token": f"tok::{identity}", "token_type": "Bearer", "expires_in": 3600}
    if refresh:
        body["refresh_token"] = refresh
    return JSONResponse(body)


def _identity(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer tok::"):
        return auth.split("tok::", 1)[1].strip()
    return "tech-user"


# --- Catalog ---------------------------------------------------------------

@app.get("/api/v1/dwc/catalog/spaces")
async def catalog_spaces():
    spaces = sorted({sp for (sp, _asset) in MODELS})
    return {"value": [{"name": sp, "label": sp} for sp in spaces]}


@app.get("/api/v1/dwc/catalog/assets")
async def catalog_assets(request: Request):
    b = _base(request)
    out = []
    for (space, asset), m in MODELS.items():
        out.append({
            "name": asset,
            "spaceName": space,
            "label": m["label"],
            "supportsAnalyticalQueries": True,
            "assetAnalyticalMetadataUrl": f"{b}/api/v1/dwc/consumption/analytical/{space}/{asset}/$metadata",
            "assetAnalyticalDataUrl": f"{b}/api/v1/dwc/consumption/analytical/{space}/{asset}/{asset}",
        })
    return {"value": out}


# --- $metadata -------------------------------------------------------------

@app.get("/api/v1/dwc/consumption/analytical/{space}/{asset}/$metadata")
async def metadata(space: str, asset: str):
    m = MODELS.get((space, asset))
    if not m:
        return Response(status_code=404)
    props = []
    for d in m["dims"]:
        props.append(f'        <Property Name="{d}" Type="Edm.String"/>')
    for meas in m["measures"]:
        props.append(
            f'        <Property Name="{meas}" Type="Edm.Decimal">\n'
            f'          <Annotation Term="com.sap.vocabularies.Analytics.v1.Measure"/>\n'
            f'          <Annotation Term="Aggregation.CustomAggregate" String="sum"/>\n'
            f'        </Property>'
        )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">\n'
        '  <edmx:DataServices>\n'
        f'    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="{space}">\n'
        f'      <EntityType Name="{asset}Type">\n'
        + "\n".join(props) + "\n"
        '      </EntityType>\n'
        '    </Schema>\n'
        '  </edmx:DataServices>\n'
        '</edmx:Edmx>'
    )
    return Response(content=xml, media_type="application/xml")


# --- analytical data (server-side aggregation) -----------------------------

def _parse_select(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def _apply_filter(rows: List[Dict], raw: Optional[str]) -> List[Dict]:
    # Minimal OData filter: "Field eq 'val'" (and-chained).
    if not raw:
        return rows
    out = rows
    for clause in raw.split(" and "):
        parts = clause.strip().split(None, 2)
        if len(parts) != 3:
            continue
        field, op, val = parts
        val = val.strip().strip("'")
        if op == "eq":
            out = [r for r in out if str(r.get(field)) == val]
        elif op == "ne":
            out = [r for r in out if str(r.get(field)) != val]
        elif op in ("gt", "ge", "lt", "le"):
            def _num(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return None
            fv = _num(val)
            cmp = {"gt": lambda a: a > fv, "ge": lambda a: a >= fv,
                   "lt": lambda a: a < fv, "le": lambda a: a <= fv}[op]
            out = [r for r in out if _num(r.get(field)) is not None and cmp(_num(r.get(field)))]
    return out


def _aggregate(m: Dict, select: List[str], rows: List[Dict]) -> List[Dict]:
    sel_dims = [c for c in select if c in m["dims"]] if select else []
    sel_meas = [c for c in select if c in m["measures"]] if select else list(m["measures"])
    if not select:
        sel_dims = list(m["dims"])
    groups: Dict[tuple, Dict] = {}
    for r in rows:
        key = tuple(r.get(d) for d in sel_dims)
        g = groups.setdefault(key, {d: r.get(d) for d in sel_dims})
        for meas in sel_meas:
            g[meas] = g.get(meas, 0) + (r.get(meas) or 0)
    return list(groups.values())


@app.get("/api/v1/dwc/consumption/analytical/{space}/{asset}/{entity}")
async def analytical_data(space: str, asset: str, entity: str, request: Request):
    m = MODELS.get((space, asset))
    if not m:
        return JSONResponse({"error": {"message": "asset not found"}}, status_code=404)

    identity = _identity(request)
    rows = m["rows"]
    # DAC enforcement: technical user gets an empty result on a protected model.
    if m["dac_protected"] and not identity.startswith("user:"):
        return {"value": []}

    qp = request.query_params
    rows = _apply_filter(rows, qp.get("$filter"))
    result = _aggregate(m, _parse_select(qp.get("$select")), rows)

    # $orderby (single field, optional " desc")
    ob = qp.get("$orderby")
    if ob:
        field, _, direction = ob.strip().partition(" ")
        result.sort(key=lambda r: (r.get(field) is None, r.get(field)),
                    reverse=direction.strip().lower() == "desc")
    # $top
    top = qp.get("$top")
    if top and top.isdigit():
        result = result[: int(top)]

    return {"value": result, "@odata.context": f"{_base(request)}/$metadata"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
