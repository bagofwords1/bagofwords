"""Build complex Power BI semantic models for connector validation.

Creates two models in a dedicated workspace, shaped to exercise the metadata a
semantic-model connector has to recover:

  FleetOps            two fact tables over five dimensions, two of them
                      CONFORMED (both facts join dim_vehicle and dim_date).
                      Every foreign key is HIDDEN - the standard convention once
                      a relationship covers the join. A ROLE-PLAYING date is
                      materialised as a duplicate dimension (dim_completion_date)
                      so the model carries two independent date paths. Six
                      measures carry the model's business logic.

  SupplyChainDepth    a SNOWFLAKE schema: fact -> product -> category ->
                      department. Three hops, every key hidden, so answering a
                      department-level question requires traversing two
                      relationships that are not directly on the fact table.

Both shapes are ordinary modelling practice and both are invisible to a
connector that cannot read relationships or hidden columns.

Push datasets are used because they are fully scriptable and queryable through
executeQueries. Two API limits shape the fixtures:
  - `isActive: false` is rejected (HTTP 400), and a second relationship to an
    already-related table is rejected outright as an ambiguous path (Desktop
    would demote it to inactive instead). Inactive relationships therefore
    cannot be provisioned here; that path is covered by unit tests.
  - RLS roles cannot be created through any REST API (they are a service-layer
    or XMLA operation, and XMLA write needs dedicated capacity). RLS models must
    be provisioned separately; this script only reports which models in the
    tenant carry RLS.

Secrets via env only: PBI_TENANT_ID / PBI_CLIENT_ID / PBI_CLIENT_SECRET.
Idempotent: re-running deletes and recreates the fixture datasets.
"""
import os
import sys
import time

import requests

TENANT = os.environ["PBI_TENANT_ID"]
CLIENT = os.environ["PBI_CLIENT_ID"]
SECRET = os.environ["PBI_CLIENT_SECRET"]
BASE = "https://api.powerbi.com/v1.0/myorg"
WS_NAME = os.environ.get("PBI_FIXTURE_WS", "bow-semantic-fixtures")


def _token() -> str:
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT,
            "client_secret": SECRET,
            "scope": "https://analysis.windows.net/powerbi/api/.default",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


H = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _hidden(name, dtype="Int64"):
    """A foreign/surrogate key: present and fully queryable, hidden from report
    authors because a relationship covers the join."""
    return {"name": name, "dataType": dtype, "isHidden": True}


def _col(name, dtype):
    return {"name": name, "dataType": dtype}


FLEET_OPS = {
    "name": "FleetOps",
    "defaultMode": "Push",
    "tables": [
        {
            "name": "dim_vehicle",
            "columns": [
                _hidden("vehicle_key"),
                _col("vehicle_code", "String"),
                _col("make_model", "String"),
                _col("capacity_kg", "Int64"),
            ],
        },
        {
            "name": "dim_depot",
            "columns": [
                _hidden("depot_key"),
                _col("depot_name", "String"),
                _col("region", "String"),
            ],
        },
        {
            "name": "dim_date",
            "columns": [
                _hidden("date_key"),
                _col("calendar_date", "DateTime"),
                _col("month_name", "String"),
                _col("year_num", "Int64"),
            ],
        },
        {
            "name": "dim_technician",
            "columns": [
                _hidden("technician_key"),
                _col("technician_name", "String"),
                _col("certification_level", "String"),
            ],
        },
        {
            # Role-playing dimension, materialised as a duplicate. Power BI
            # Desktop would let a second relationship to dim_date exist as
            # INACTIVE, but the push API rejects the ambiguous path outright, so
            # the fixture uses the other standard resolution: a second physical
            # date dimension. Both facts still share dim_date, so conformed
            # dimensions are exercised either way.
            "name": "dim_completion_date",
            "columns": [
                _hidden("completion_date_key"),
                _col("completion_date", "DateTime"),
                _col("completion_month_name", "String"),
            ],
        },
        {
            "name": "fact_service_event",
            "columns": [
                _hidden("svc_vehicle_key"),
                _hidden("svc_depot_key"),
                _hidden("svc_technician_key"),
                # Two independent date paths: scheduled -> dim_date,
                # completed -> dim_completion_date.
                _hidden("svc_scheduled_date_key"),
                _hidden("svc_completed_date_key"),
                _col("labour_hours", "Double"),
                _col("parts_cost", "Double"),
            ],
            "measures": [
                {"name": "Total Parts Cost", "expression": "SUM(fact_service_event[parts_cost])"},
                {"name": "Total Labour Hours", "expression": "SUM(fact_service_event[labour_hours])"},
                {"name": "Service Event Count", "expression": "COUNTROWS(fact_service_event)"},
                {"name": "Avg Parts Cost per Event",
                 "expression": "DIVIDE(SUM(fact_service_event[parts_cost]), COUNTROWS(fact_service_event))"},
            ],
        },
        {
            "name": "fact_fuel_log",
            "columns": [
                _hidden("fuel_vehicle_key"),
                _hidden("fuel_date_key"),
                _col("litres", "Double"),
                _col("fuel_cost", "Double"),
            ],
            "measures": [
                {"name": "Total Litres", "expression": "SUM(fact_fuel_log[litres])"},
                {"name": "Total Fuel Cost", "expression": "SUM(fact_fuel_log[fuel_cost])"},
            ],
        },
    ],
    "relationships": [
        {"name": "svc_vehicle", "fromTable": "fact_service_event", "fromColumn": "svc_vehicle_key",
         "toTable": "dim_vehicle", "toColumn": "vehicle_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "svc_depot", "fromTable": "fact_service_event", "fromColumn": "svc_depot_key",
         "toTable": "dim_depot", "toColumn": "depot_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "svc_technician", "fromTable": "fact_service_event", "fromColumn": "svc_technician_key",
         "toTable": "dim_technician", "toColumn": "technician_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "svc_scheduled_date", "fromTable": "fact_service_event", "fromColumn": "svc_scheduled_date_key",
         "toTable": "dim_date", "toColumn": "date_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "svc_completed_date", "fromTable": "fact_service_event", "fromColumn": "svc_completed_date_key",
         "toTable": "dim_completion_date", "toColumn": "completion_date_key",
         "crossFilteringBehavior": "OneDirection"},
        {"name": "fuel_vehicle", "fromTable": "fact_fuel_log", "fromColumn": "fuel_vehicle_key",
         "toTable": "dim_vehicle", "toColumn": "vehicle_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "fuel_date", "fromTable": "fact_fuel_log", "fromColumn": "fuel_date_key",
         "toTable": "dim_date", "toColumn": "date_key", "crossFilteringBehavior": "OneDirection"},
    ],
}

SUPPLY_CHAIN = {
    "name": "SupplyChainDepth",
    "defaultMode": "Push",
    "tables": [
        {
            "name": "dim_department",
            "columns": [_hidden("department_key"), _col("department_name", "String")],
        },
        {
            "name": "dim_category",
            "columns": [
                _hidden("category_key"),
                _col("category_name", "String"),
                _hidden("cat_department_key"),
            ],
        },
        {
            "name": "dim_product",
            "columns": [
                _hidden("product_key"),
                _col("sku", "String"),
                _col("product_name", "String"),
                _hidden("prod_category_key"),
            ],
        },
        {
            "name": "dim_supplier",
            "columns": [_hidden("supplier_key"), _col("supplier_name", "String"), _col("country", "String")],
        },
        {
            "name": "fact_shipment",
            "columns": [
                _hidden("ship_product_key"),
                _hidden("ship_supplier_key"),
                _col("units", "Int64"),
                _col("freight_cost", "Double"),
            ],
            "measures": [
                {"name": "Total Units", "expression": "SUM(fact_shipment[units])"},
                {"name": "Total Freight Cost", "expression": "SUM(fact_shipment[freight_cost])"},
                {"name": "Freight Cost per Unit",
                 "expression": "DIVIDE(SUM(fact_shipment[freight_cost]), SUM(fact_shipment[units]))"},
            ],
        },
    ],
    "relationships": [
        {"name": "ship_product", "fromTable": "fact_shipment", "fromColumn": "ship_product_key",
         "toTable": "dim_product", "toColumn": "product_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "ship_supplier", "fromTable": "fact_shipment", "fromColumn": "ship_supplier_key",
         "toTable": "dim_supplier", "toColumn": "supplier_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "product_category", "fromTable": "dim_product", "fromColumn": "prod_category_key",
         "toTable": "dim_category", "toColumn": "category_key", "crossFilteringBehavior": "OneDirection"},
        {"name": "category_department", "fromTable": "dim_category", "fromColumn": "cat_department_key",
         "toTable": "dim_department", "toColumn": "department_key", "crossFilteringBehavior": "OneDirection"},
    ],
}

# Deterministic rows. Totals are stated in the feedback-loop doc so a wrong
# answer from the agent is detectable, not just plausible.
FLEET_ROWS = {
    "dim_vehicle": [
        {"vehicle_key": 1, "vehicle_code": "VH-001", "make_model": "Volvo FH16", "capacity_kg": 24000},
        {"vehicle_key": 2, "vehicle_code": "VH-002", "make_model": "Scania R450", "capacity_kg": 18000},
        {"vehicle_key": 3, "vehicle_code": "VH-003", "make_model": "MAN TGX", "capacity_kg": 21000},
    ],
    "dim_depot": [
        {"depot_key": 10, "depot_name": "Northgate", "region": "North"},
        {"depot_key": 20, "depot_name": "Southfield", "region": "South"},
    ],
    "dim_date": [
        {"date_key": 20250106, "calendar_date": "2025-01-06T00:00:00Z", "month_name": "January", "year_num": 2025},
        {"date_key": 20250203, "calendar_date": "2025-02-03T00:00:00Z", "month_name": "February", "year_num": 2025},
        {"date_key": 20250310, "calendar_date": "2025-03-10T00:00:00Z", "month_name": "March", "year_num": 2025},
    ],
    "dim_technician": [
        {"technician_key": 100, "technician_name": "Robin Vale", "certification_level": "Senior"},
        {"technician_key": 200, "technician_name": "Sam Okonkwo", "certification_level": "Junior"},
    ],
    "dim_completion_date": [
        {"completion_date_key": 20250106, "completion_date": "2025-01-06T00:00:00Z", "completion_month_name": "January"},
        {"completion_date_key": 20250203, "completion_date": "2025-02-03T00:00:00Z", "completion_month_name": "February"},
        {"completion_date_key": 20250310, "completion_date": "2025-03-10T00:00:00Z", "completion_month_name": "March"},
    ],
    # Northgate parts_cost = 1200 + 800 + 450 = 2450 ; Southfield = 300 + 1500 = 1800
    "fact_service_event": [
        {"svc_vehicle_key": 1, "svc_depot_key": 10, "svc_technician_key": 100,
         "svc_scheduled_date_key": 20250106, "svc_completed_date_key": 20250203,
         "labour_hours": 4.0, "parts_cost": 1200.0},
        {"svc_vehicle_key": 2, "svc_depot_key": 10, "svc_technician_key": 200,
         "svc_scheduled_date_key": 20250106, "svc_completed_date_key": 20250106,
         "labour_hours": 2.5, "parts_cost": 800.0},
        {"svc_vehicle_key": 3, "svc_depot_key": 20, "svc_technician_key": 100,
         "svc_scheduled_date_key": 20250203, "svc_completed_date_key": 20250310,
         "labour_hours": 1.0, "parts_cost": 300.0},
        {"svc_vehicle_key": 1, "svc_depot_key": 20, "svc_technician_key": 200,
         "svc_scheduled_date_key": 20250310, "svc_completed_date_key": 20250310,
         "labour_hours": 6.0, "parts_cost": 1500.0},
        {"svc_vehicle_key": 2, "svc_depot_key": 10, "svc_technician_key": 100,
         "svc_scheduled_date_key": 20250310, "svc_completed_date_key": 20250310,
         "labour_hours": 3.5, "parts_cost": 450.0},
    ],
    "fact_fuel_log": [
        {"fuel_vehicle_key": 1, "fuel_date_key": 20250106, "litres": 320.0, "fuel_cost": 480.0},
        {"fuel_vehicle_key": 2, "fuel_date_key": 20250203, "litres": 210.5, "fuel_cost": 315.75},
        {"fuel_vehicle_key": 3, "fuel_date_key": 20250310, "litres": 175.0, "fuel_cost": 262.5},
        {"fuel_vehicle_key": 1, "fuel_date_key": 20250310, "litres": 290.0, "fuel_cost": 435.0},
    ],
}

SUPPLY_ROWS = {
    "dim_department": [
        {"department_key": 1, "department_name": "Ambient"},
        {"department_key": 2, "department_name": "Chilled"},
    ],
    "dim_category": [
        {"category_key": 11, "category_name": "Beverages", "cat_department_key": 1},
        {"category_key": 12, "category_name": "Snacks", "cat_department_key": 1},
        {"category_key": 21, "category_name": "Dairy", "cat_department_key": 2},
    ],
    "dim_product": [
        {"product_key": 101, "sku": "SKU-101", "product_name": "Sparkling Water 1L", "prod_category_key": 11},
        {"product_key": 102, "sku": "SKU-102", "product_name": "Salted Pretzels", "prod_category_key": 12},
        {"product_key": 103, "sku": "SKU-103", "product_name": "Whole Milk 2L", "prod_category_key": 21},
    ],
    "dim_supplier": [
        {"supplier_key": 900, "supplier_name": "Harborline", "country": "Portugal"},
        {"supplier_key": 901, "supplier_name": "Meridian Foods", "country": "Ireland"},
    ],
    # Ambient freight = 500 + 250 + 120 = 870 ; Chilled = 640
    "fact_shipment": [
        {"ship_product_key": 101, "ship_supplier_key": 900, "units": 1000, "freight_cost": 500.0},
        {"ship_product_key": 102, "ship_supplier_key": 901, "units": 400, "freight_cost": 250.0},
        {"ship_product_key": 101, "ship_supplier_key": 901, "units": 250, "freight_cost": 120.0},
        {"ship_product_key": 103, "ship_supplier_key": 900, "units": 800, "freight_cost": 640.0},
    ],
}


def ensure_workspace() -> str:
    groups = requests.get(f"{BASE}/groups", headers=H, timeout=30).json().get("value", [])
    ws = next((g for g in groups if g.get("name") == WS_NAME), None)
    if ws:
        return ws["id"]
    r = requests.post(f"{BASE}/groups?workspaceV2=true", headers=H, json={"name": WS_NAME}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def build(ws_id: str, definition: dict, rows: dict) -> str:
    name = definition["name"]
    existing = requests.get(f"{BASE}/groups/{ws_id}/datasets", headers=H, timeout=30).json().get("value", [])
    for d in existing:
        if d.get("name") == name:
            requests.delete(f"{BASE}/groups/{ws_id}/datasets/{d['id']}", headers=H, timeout=30)
            print(f"  removed previous {name}")

    r = requests.post(f"{BASE}/groups/{ws_id}/datasets", headers=H, json=definition, timeout=60)
    if r.status_code >= 300:
        sys.exit(f"create {name} failed: HTTP {r.status_code} {r.text[:400]}")
    ds_id = r.json()["id"]
    print(f"  created {name} = {ds_id}")

    for table, payload in rows.items():
        rr = requests.post(
            f"{BASE}/groups/{ws_id}/datasets/{ds_id}/tables/{table}/rows",
            headers=H, json={"rows": payload}, timeout=30,
        )
        status = "ok" if rr.status_code < 300 else f"HTTP {rr.status_code} {rr.text[:200]}"
        print(f"    rows/{table}: {len(payload)} -> {status}")
    return ds_id


def main():
    ws_id = ensure_workspace()
    print(f"workspace {WS_NAME} = {ws_id}\n")

    print("FleetOps (multi-fact star, conformed dims, role-playing date):")
    fleet_id = build(ws_id, FLEET_OPS, FLEET_ROWS)
    print("\nSupplyChainDepth (snowflake, 3 hops):")
    supply_id = build(ws_id, SUPPLY_CHAIN, SUPPLY_ROWS)

    time.sleep(3)

    print("\nverifying model metadata is readable:")
    for label, ds_id in (("FleetOps", fleet_id), ("SupplyChainDepth", supply_id)):
        r = requests.post(
            f"{BASE}/groups/{ws_id}/datasets/{ds_id}/executeQueries",
            headers=H,
            json={"queries": [{"query": 'EVALUATE SELECTCOLUMNS(INFO.VIEW.RELATIONSHIPS(), "R", [Relationship], "Active", [IsActive])'}]},
            timeout=60,
        )
        rows = (r.json().get("results") or [{}])[0].get("tables", [{}])[0].get("rows", []) if r.status_code < 300 else []
        print(f"  {label}: {len(rows)} relationship(s)")
        for row in rows:
            print(f"    active={str(row.get('[Active]')):5s}  {row.get('[R]')}")
        rm = requests.post(
            f"{BASE}/groups/{ws_id}/datasets/{ds_id}/executeQueries",
            headers=H,
            json={"queries": [{"query": 'EVALUATE SELECTCOLUMNS(INFO.VIEW.MEASURES(), "N", [Name], "E", [Expression])'}]},
            timeout=60,
        )
        mrows = (rm.json().get("results") or [{}])[0].get("tables", [{}])[0].get("rows", []) if rm.status_code < 300 else []
        print(f"  {label}: {len(mrows)} measure(s)")
        for row in mrows:
            print(f"    {row.get('[N]')} = {row.get('[E]')}")

    print("\nRLS models already provisioned in this tenant (roles are not API-creatable):")
    for g in requests.get(f"{BASE}/groups", headers=H, timeout=30).json().get("value", []):
        dr = requests.get(f"{BASE}/groups/{g['id']}/datasets", headers=H, timeout=30)
        for d in (dr.json().get("value", []) if dr.status_code < 300 else []):
            if d.get("isEffectiveIdentityRequired"):
                print(f"  {g['name']}/{d['name']}  roles_required={d.get('isEffectiveIdentityRolesRequired')}")


if __name__ == "__main__":
    main()
