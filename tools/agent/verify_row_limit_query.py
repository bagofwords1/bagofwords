#!/usr/bin/env python3
"""Manual harness #2: verify org limit_row_count is honored on the query-run,
query-preview, and report-rerun (step refresh) paths — the other fixed files
(query_service.py, step_service.py). Real backend, real HTTP. No pytest.
"""
import json
import sys
import httpx

BASE = "http://localhost:8000"
EMAIL = "admin@example.com"
PASSWORD = "Password123!"
NROWS = 5000

c = httpx.Client(base_url=BASE, timeout=60)
CODE = f"def generate_df(ds_clients, excel_files):\n    return pd.DataFrame({{'a': range({NROWS})}})\n"


def h(token, org=None):
    hd = {"Authorization": f"Bearer {token}"}
    if org:
        hd["X-Organization-Id"] = str(org)
    return hd


def main():
    c.post("/api/auth/register", json={"name": "Admin", "email": EMAIL, "password": PASSWORD})
    token = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
    org_id = c.get("/api/organizations", headers=h(token)).json()[0]["id"]
    print(f"org_id={org_id}")

    def set_limit(v):
        r = c.put("/api/organization/settings",
                  json={"config": {"limit_row_count": {"value": v}}}, headers=h(token, org_id))
        r.raise_for_status()
        print(f"  set limit_row_count={v}")

    # Report + query (query create makes a widget under the report).
    rep = c.post("/api/reports", json={"title": "RL Report", "files": [], "data_sources": []},
                 headers=h(token, org_id))
    rep.raise_for_status()
    report_id = rep.json()["id"]
    q = c.post("/api/queries", json={"title": "RL Query", "report_id": report_id}, headers=h(token, org_id))
    q.raise_for_status()
    query_id = q.json()["id"]
    print(f"report_id={report_id} query_id={query_id}")

    def query_run_rows():
        r = c.post(f"/api/queries/{query_id}/run", json={"code": CODE, "title": "RL Query"},
                   headers=h(token, org_id))
        r.raise_for_status()
        return len(r.json()["step"]["data"].get("rows", []))

    def query_preview_rows():
        r = c.post(f"/api/queries/{query_id}/preview", json={"code": CODE}, headers=h(token, org_id))
        r.raise_for_status()
        return len(r.json()["preview"].get("rows", []))

    def report_rerun_rows():
        # Reruns every step in the report (step_service.rerun_step path).
        r = c.post(f"/api/reports/{report_id}/rerun", headers=h(token, org_id))
        r.raise_for_status()
        # Fetch the query's default step data after rerun.
        s = c.get(f"/api/queries/{query_id}/default_step", headers=h(token, org_id))
        s.raise_for_status()
        step = s.json().get("step") or {}
        return len((step.get("data") or {}).get("rows", []))

    results = {}
    print("\n[limit=100000] expect 5000 on every path")
    set_limit(100000)
    results["query_run@100k"] = query_run_rows()
    results["query_preview@100k"] = query_preview_rows()
    results["report_rerun@100k"] = report_rerun_rows()
    for k in ("query_run@100k", "query_preview@100k", "report_rerun@100k"):
        print(f"  {k} = {results[k]}")

    print("\n[limit=1000] expect 1000 on every path")
    set_limit(1000)
    results["query_run@1k"] = query_run_rows()
    results["query_preview@1k"] = query_preview_rows()
    results["report_rerun@1k"] = report_rerun_rows()
    for k in ("query_run@1k", "query_preview@1k", "report_rerun@1k"):
        print(f"  {k} = {results[k]}")

    print("\n==== RESULTS ====")
    print(json.dumps(results, indent=2))
    ok = (results["query_run@100k"] == NROWS and results["query_preview@100k"] == NROWS
          and results["report_rerun@100k"] == NROWS
          and results["query_run@1k"] == 1000 and results["query_preview@1k"] == 1000
          and results["report_rerun@1k"] == 1000)
    print("\nVERDICT:", "PASS ✅ limit honored on run/preview/rerun" if ok else "FAIL ❌")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
