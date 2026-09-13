#!/usr/bin/env python3
"""Verify real parameterized query results against an independent source calculation."""

import argparse, json, sqlite3, math
from pathlib import Path
from collections import defaultdict
import httpx

p = argparse.ArgumentParser()
p.add_argument("--session", required=True)
p.add_argument("--apps", required=True)
p.add_argument("--source-db", required=True)
p.add_argument("--output", required=True)
a = p.parse_args()
s = json.loads(Path(a.session).read_text())
apps = json.loads(Path(a.apps).read_text())
c = httpx.Client(
    base_url=s["base_url"],
    headers={
        "Authorization": "Bearer " + s["admin"]["token"],
        "X-Organization-Id": s["organization"]["id"],
    },
    timeout=180,
)
db = sqlite3.connect(a.source_db)
db.row_factory = sqlite3.Row
# Independent reference: no CTE/window/grouping from the application queries.
orders = [
    dict(r)
    for r in db.execute(
        "SELECT o.*,c.name customer,c.region,c.rep_id FROM orders o JOIN customers c ON c.id=o.customer_id"
    )
]
amounts = defaultdict(float)
for r in db.execute("SELECT order_id,quantity,unit_price FROM order_lines"):
    amounts[r["order_id"]] += r["quantity"] * r["unit_price"]
base = {
    "region": None,
    "channels": ["Direct", "Partner", "Marketplace"],
    "period": {"from": "2026-01-01", "to": "2026-06-30"},
    "min_order": 0,
}
cases = [
    {},
    {"region": "Europe"},
    {
        "region": "Asia Pacific",
        "channels": ["Direct", "Partner"],
        "min_order": 800,
        "period": {"from": "2026-03-01", "to": "2026-04-30"},
    },
    {"channels": []},
    {"min_order": 10000000},
    {"period": {"from": "2030-01-01", "to": "2030-01-31"}},
]
results = []
for change in cases:
    values = {**base, **change}
    period = values["period"]
    expected = [
        o
        for o in orders
        if (values["region"] is None or o["region"] == values["region"])
        and o["channel"] in values["channels"]
        and period["from"] <= o["ordered_at"] <= period["to"]
        and amounts[o["id"]] >= values["min_order"]
    ]
    revenue = sum(amounts[o["id"]] for o in expected)
    for slug in ["commerce", "reps"]:
        for q in apps[slug]["queries"]:
            r = c.post(
                "/api/queries/" + q["id"] + "/run",
                json={"mode": "viewer", "params": values, "force_refresh": True},
            )
            r.raise_for_status()
            body = r.json()
            assert body["status"] == "success", body
            rows = body["data"]["rows"]
            actual = sum(float(row["revenue"]) for row in rows)
            assert math.isclose(actual, revenue, abs_tol=0.05), (
                slug,
                q["title"],
                values,
                actual,
                revenue,
            )
            if rows and "orders" in rows[0]:
                assert sum(int(row["orders"]) for row in rows) == len(expected)
            results.append(
                {
                    "app": slug,
                    "query": q["title"],
                    "params": values,
                    "rows": len(rows),
                    "revenue": actual,
                    "status": "PASS",
                }
            )
    print("Sales combination passed", change, flush=True)
# Invalid typed/strict values must fail at the backend, not silently produce data.
for change in [
    {"min_order": "not-a-number"},
    {"period": "not-a-range"},
    {"region": "Europe' OR 1=1 --"},
]:
    r = c.post(
        "/api/queries/" + apps["commerce"]["queries"][0]["id"] + "/run",
        json={"mode": "viewer", "params": {**base, **change}},
    )
    assert r.status_code == 400, (change, r.status_code)
    results.append({"invalid": change, "status": "PASS"})
tracks = [
    dict(r)
    for r in db.execute(
        "SELECT t.*,a.title album,a.artist,a.genre FROM tracks t JOIN albums a ON a.id=t.album_id"
    )
]
for change in [
    {},
    {"genre": "Jazz"},
    {"search": "Ada", "max_price": 1.3, "min_minutes": 3},
    {"search": "' OR 1=1 --"},
    {"max_price": 0},
]:
    values = {"genre": None, "search": "", "max_price": 2, "min_minutes": 0, **change}
    expected = [
        t
        for t in tracks
        if (not values["genre"] or t["genre"] == values["genre"])
        and any(
            values["search"].lower() in str(t[k]).lower()
            for k in ["title", "album", "artist"]
        )
        and t["price"] <= values["max_price"]
        and t["minutes"] >= values["min_minutes"]
    ]
    for q in apps["catalog"]["queries"]:
        r = c.post(
            "/api/queries/" + q["id"] + "/run",
            json={"mode": "viewer", "params": values, "force_refresh": True},
        )
        r.raise_for_status()
        body = r.json()
        assert body["status"] == "success", body
        rows = body["data"]["rows"]
        if q["title"] == "Track details":
            assert {r["track_id"] for r in rows} == {r["id"] for r in expected}
        else:
            assert sum(r["tracks"] for r in rows) == len(expected)
        results.append(
            {
                "app": "catalog",
                "query": q["title"],
                "params": values,
                "rows": len(rows),
                "status": "PASS",
            }
        )
    print("Catalog combination passed", change, flush=True)
Path(a.output).write_text(json.dumps(results, indent=2))
print(len(results), "real backend checks passed")
