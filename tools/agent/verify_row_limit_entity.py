#!/usr/bin/env python3
"""Manual harness: verify org limit_row_count is honored on entity refresh/preview.

Reproduces the reported bug (org sets 100k row limit but data still caps at 1000)
against the REAL running backend, through real HTTP endpoints. No pytest.
"""
import json
import sys
import httpx

BASE = "http://localhost:8000"
EMAIL = "admin@example.com"
PASSWORD = "Password123!"
NROWS = 5000  # > default 1000 cap, < any org limit we set

c = httpx.Client(base_url=BASE, timeout=60)


def login():
    r = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


def h(token, org=None):
    hd = {"Authorization": f"Bearer {token}"}
    if org:
        hd["X-Organization-Id"] = str(org)
    return hd


def main():
    # Register admin (first user); ignore if exists.
    c.post("/api/auth/register", json={"name": "Admin", "email": EMAIL, "password": PASSWORD})
    token = login()
    orgs = c.get("/api/organizations", headers=h(token)).json()
    org_id = orgs[0]["id"]
    print(f"org_id={org_id}")

    def set_limit(v):
        r = c.put("/api/organization/settings",
                  json={"config": {"limit_row_count": {"value": v}}},
                  headers=h(token, org_id))
        r.raise_for_status()
        got = r.json()["config"]["limit_row_count"]["value"]
        assert got == v, f"expected stored limit {v}, got {got}"
        print(f"  set limit_row_count={v} (persisted={got})")

    code = f"def generate_df(ds_clients, excel_files):\n    return pd.DataFrame({{'a': range({NROWS})}})\n"

    # Create an entity carrying pure-pandas code that yields NROWS rows.
    payload = {"type": "model", "title": "RowLimit Probe", "slug": "rowlimit-probe",
               "code": code, "status": "published", "data_source_ids": []}
    r = c.post("/api/entities", json=payload, headers=h(token, org_id))
    if r.status_code != 200:
        print("create entity failed:", r.status_code, r.text); sys.exit(1)
    ent_id = r.json()["id"]
    print(f"entity_id={ent_id}")

    def run_rows():
        r = c.post(f"/api/entities/{ent_id}/run", json={}, headers=h(token, org_id))
        r.raise_for_status()
        return len(r.json()["data"].get("rows", []))

    def preview_rows():
        r = c.post(f"/api/entities/{ent_id}/preview", json={"code": code}, headers=h(token, org_id))
        r.raise_for_status()
        return len(r.json()["data"].get("rows", []))

    results = {}

    print("\n[Scenario 1] limit=100000, generate 5000 rows -> expect 5000")
    set_limit(100000)
    results["run@100k"] = run_rows()
    results["preview@100k"] = preview_rows()
    print(f"  run rows     = {results['run@100k']}")
    print(f"  preview rows = {results['preview@100k']}")

    print("\n[Scenario 2] limit=1000 (default), generate 5000 rows -> expect 1000")
    set_limit(1000)
    results["run@1k"] = run_rows()
    results["preview@1k"] = preview_rows()
    print(f"  run rows     = {results['run@1k']}")
    print(f"  preview rows = {results['preview@1k']}")

    print("\n[Scenario 3] limit=0 (disabled/no cap) -> expect 5000")
    set_limit(0)
    results["run@0"] = run_rows()
    print(f"  run rows     = {results['run@0']}")

    print("\n==== RESULTS ====")
    print(json.dumps(results, indent=2))

    ok = (results["run@100k"] == NROWS and results["preview@100k"] == NROWS
          and results["run@1k"] == 1000 and results["preview@1k"] == 1000
          and results["run@0"] == NROWS)
    print("\nVERDICT:", "PASS ✅ limit honored" if ok else "FAIL ❌ limit NOT honored")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
