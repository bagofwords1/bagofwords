#!/usr/bin/env python
"""Artifact Store e2e — huge Chinook (1000x), create data -> encrypted DuckDB
artifact, report, HTTP rerun, slices. Loop D-shaped: drives the REAL server
over HTTP for org/data-source/report/rerun, and the real in-process pipeline
(executor -> format -> step persist -> spill -> handle) for the data plane
exactly as agent_v2 + create_data do it.

Run with the backend server already listening on :8000 against the same
BOW_DATABASE_URL.

  BOW_DATABASE_URL="sqlite:///db/app.db" .venv/bin/python scripts/artifact_store_e2e.py
"""
import asyncio
import json
import os
import sys
import time
import uuid

import requests

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
CHINOOK = os.environ.get("CHINOOK_1000X", "/tmp/chinook_1000x.sqlite")

EMAIL = os.environ.get("E2E_EMAIL", f"e2e_{uuid.uuid4().hex[:8]}@example.com")
PASSWORD = os.environ.get("E2E_PASSWORD", "Str0ng!passw0rd")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} - {name}" + (f"   >> {detail}" if (detail and not ok) else ""))
    return ok


def http_setup():
    r = requests.post(f"{BASE}/api/auth/register", json={"name": f"e2e_{uuid.uuid4().hex[:6]}", "email": EMAIL, "password": PASSWORD})
    # 201 = fresh user; if sign-up already closed (first user exists), we log
    # into the existing account passed via E2E_EMAIL/E2E_PASSWORD.
    check("register or reuse user", r.status_code in (201, 400, 403), r.text[:200])
    r = requests.post(f"{BASE}/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
    check("login (200)", r.status_code == 200, r.text[:200])
    token = r.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}

    # Registration auto-creates an organization; use it.
    r = requests.get(f"{BASE}/api/organizations", headers=H)
    orgs = r.json() if r.status_code == 200 else []
    if not orgs:
        r = requests.post(f"{BASE}/api/organizations", json={"name": "Artifact E2E Org"}, headers=H)
        orgs = [r.json()]
    check("resolve organization", bool(orgs) and orgs[0].get("id"), str(orgs[:1])[:200])
    org_id = orgs[0]["id"]
    HO = {**H, "X-Organization-Id": str(org_id)}

    r = requests.post(
        f"{BASE}/api/data_sources",
        json={
            "name": f"Chinook 1000x {uuid.uuid4().hex[:6]}",
            "type": "sqlite",
            "config": {"database": CHINOOK},
            "credentials": {},
            "auth_policy": "system_only",
            "generate_summary": False,
            "generate_conversation_starters": False,
            "generate_ai_rules": False,
        },
        headers=HO,
    )
    check("create sqlite data source (chinook 1000x)", r.status_code == 200, r.text[:300])
    ds_id = r.json()["id"]

    r = requests.post(
        f"{BASE}/api/reports",
        json={"title": "Invoice lines deep-dive (1000x)", "widget": None, "files": [], "data_sources": [ds_id]},
        headers=HO,
    )
    check("create report", r.status_code == 200, r.text[:300])
    report_id = r.json()["id"]
    return token, org_id, ds_id, report_id, HO


# The exact shape create_data's codegen produces — this is the only
# LLM-authored piece of the pipeline; everything downstream is the real path.
GENERATED_CODE = '''
def generate_df(db_clients, excel_files):
    import pandas as pd
    client = list(db_clients.values())[0]
    sql = """
        SELECT il.InvoiceLineId, i.InvoiceId, i.InvoiceDate, c.LastName AS Customer,
               t.Name AS TrackName, il.UnitPrice, il.Quantity,
               il.UnitPrice * il.Quantity AS Amount
        FROM InvoiceLine il
        JOIN Invoice i ON i.InvoiceId = il.InvoiceId
        JOIN Customer c ON c.CustomerId = i.CustomerId
        JOIN Track t ON t.TrackId = il.TrackId
        ORDER BY il.InvoiceLineId
    """
    df = client.execute_query(sql)
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"])
    return df
'''


def _import_all_models():
    """Register the same model graph as the running server (import the app;
    startup hooks only fire when served, so this is side-effect-safe)."""
    import sys
    sys.path.insert(0, os.getcwd())
    import main  # noqa: F401


async def data_plane(org_id, ds_id, report_id):
    # Run inside the backend app context against the same DB as the server.
    _import_all_models()
    from sqlalchemy import select
    from app.settings.database import create_async_session_factory
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.data_source import DataSource
    from app.models.result_file import ResultFile
    from app.project_manager import ProjectManager
    from app.services.data_source_service import DataSourceService
    from app.services.result_store import ResultStore
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    Session = create_async_session_factory()
    svc = ResultStore()
    async with Session() as db:
        org = await db.get(Organization, str(org_id))
        report = (await db.execute(
            select(Report).where(Report.id == str(report_id))
        )).scalar_one()
        ds = await db.get(DataSource, str(ds_id))
        org_settings = await org.get_settings(db)

        # 1) real data source client (same construction as the agent)
        ds_service = DataSourceService()
        clients = await ds_service.construct_clients(db, ds, current_user=None)

        # 2) execute the "generated" code through the real executor
        executor = StreamingCodeExecutor(organization_settings=org_settings)
        t0 = time.time()
        raw_df, output_log, _ = await executor.execute_code_async(
            code=GENERATED_CODE, ds_clients=clients, excel_files=[],
        )
        exec_s = time.time() - t0
        check(f"executed 2.24M-row join through real executor ({exec_s:.1f}s)", len(raw_df) >= 2_240_000, f"rows={len(raw_df)}")

        formatted = await asyncio.to_thread(executor.format_df_for_widget, raw_df)
        stored_rows = len(formatted.get("rows", []))
        total_rows = int(formatted["info"]["total_rows"])
        check("row cap truncated the stored copy (floor condition)", stored_rows < total_rows, f"stored={stored_rows} total={total_rows}")

        # 3) widget + query + step — the same graph the agent builds
        #    (query-bound step, default step set, widget published so the
        #    report rerun endpoint picks it up)
        title = "All invoice lines (1000x)"
        pm = ProjectManager()
        widget = await pm.create_widget(db, report, title)
        query = await pm.create_query_v2(db, report, title)
        step = await pm.create_step_for_query(db, query, title, "table")
        step.widget_id = str(widget.id)
        query.widget_id = str(widget.id)
        widget.status = "published"
        db.add_all([step, query, widget])
        await db.commit()
        await pm.set_query_default_step_if_empty(db, query, str(step.id))
        await pm.update_step_with_code(db, step, GENERATED_CODE)
        await pm.update_step_with_data(db, step, formatted)
        await pm.update_step_status(db, step, "success")

        # 4) spill + persist handle (the create_data + persistence hook path)
        assert ResultStore.enabled(org_settings)
        assert svc.should_spill(total_rows, stored_rows, int(formatted["info"].get("memory_usage", 0)))
        t0 = time.time()
        spill = await svc.spill_dataframe(
            raw_df, organization_id=str(org.id), producer="create_data",
            source_meta={"title": title},
        )
        spill_s = time.time() - t0
        artifact = await svc.persist_handle(
            db, spill, organization_id=str(org.id), report_id=str(report.id),
            step_id=str(step.id), query_id=str(query.id),
        )
        check(f"spilled 2.24M rows to encrypted DuckDB artifact ({spill_s:.1f}s, {spill.byte_size//(1<<20)}MB)",
              artifact.status == "published" and artifact.row_count == total_rows)
        check("timestamp column detected for time slicing", artifact.ts_column == "InvoiceDate", artifact.ts_column)

        path = svc.storage.abs_path(artifact.storage_ref)
        raw_head = open(path, "rb").read(4 << 20)
        check("payload encrypted (no plaintext needle in file head)", b"ZZZ-NEEDLE-TRACK" not in raw_head)

        return str(step.id), str(artifact.id), total_rows


async def verify_slices(org_id, report_id, result_file_id, total_rows):
    from app.settings.database import create_async_session_factory
    from app.services.result_store import ResultStore, SLICE_MAX_ROWS

    Session = create_async_session_factory()
    svc = ResultStore()
    async with Session() as db:
        artifact = await svc.get_result_file(db, str(org_id), result_file_id)

        t0 = time.time()
        page = svc.slice_sync(artifact, offset=2_000_000, limit=5)
        # Artifact rows are TIME-SORTED on write (D14); assert deterministic
        # page shape + monotonic InvoiceDate ordering, not source-id order.
        dates = [r[2] for r in page["rows"]]
        check(f"page at offset 2,000,000 of {total_rows} ({time.time()-t0:.2f}s)",
              len(page["rows"]) == 5 and page["total_matches"] == total_rows
              and dates == sorted(dates) and page["next_offset"] == 2_000_005,
              json.dumps(page["rows"][:1], default=str)[:150])

        t0 = time.time()
        needle = svc.slice_sync(artifact, match="ZZZ-NEEDLE-TRACK-\\d+")
        check(f"regex grep over {total_rows} rows found the 1 planted needle ({time.time()-t0:.2f}s)",
              needle["total_matches"] == 1 and any("ZZZ-NEEDLE-TRACK-424242" in str(v) for v in needle["rows"][0]))

        # Derive a real 7-day window from the data itself (chinook dates vary
        # by distribution version), then slice it.
        bounds = svc.slice_sync(artifact, sql="SELECT min(InvoiceDate) AS lo, max(InvoiceDate) AS hi FROM data")
        lo = str(bounds["rows"][0][0])[:10]
        t0 = time.time()
        win = svc.slice_sync(artifact, time_from=f"{lo} 00:00:00", time_to=f"{lo} 23:59:59",
                             columns=["InvoiceLineId", "InvoiceDate", "Amount"])
        check(f"time-window slice on {lo} ({time.time()-t0:.2f}s)",
              win["total_matches"] > 0 and win["columns"] == ["InvoiceLineId", "InvoiceDate", "Amount"],
              json.dumps({"lo": lo, "matches": win["total_matches"]}))

        t0 = time.time()
        agg = svc.slice_sync(artifact, sql="SELECT Customer, count(*) AS n, sum(Amount) AS revenue FROM data GROUP BY Customer ORDER BY revenue DESC LIMIT 5")
        check(f"SELECT-only aggregation over full artifact ({time.time()-t0:.2f}s)", len(agg["rows"]) == 5)

        big = svc.slice_sync(artifact, limit=100000)
        check("oversized page request bounded server-side", len(big["rows"]) <= SLICE_MAX_ROWS and big["next_offset"] is not None)

        for attack in ("COPY data TO '/tmp/x.csv'", "SELECT * FROM read_csv('/etc/passwd')", "ATTACH ':memory:' AS x"):
            try:
                svc.slice_sync(artifact, sql=attack)
                check(f"SQL jail blocks: {attack[:30]}", False, "was not rejected!")
            except Exception:
                check(f"SQL jail blocks: {attack[:30]}", True)

        res = svc.slice_sync(artifact, match="ZZZ-NEEDLE", allow_llm_see_data=False)
        check("privacy mode returns counts only (no raw rows)", res.get("rows_hidden") and "rows" not in res and res["total_matches"] == 1)


async def verify_read_query_tool(org_id, report_id, result_file_id):
    """Drive the REAL read_query tool (slice mode) as the agent would."""
    from sqlalchemy import select
    from app.settings.database import create_async_session_factory
    from app.models.organization import Organization
    from app.models.report import Report
    from app.ai.tools.implementations.read_query import ReadQueryTool

    Session = create_async_session_factory()
    async with Session() as db:
        org = await db.get(Organization, str(org_id))
        report = (await db.execute(select(Report).where(Report.id == str(report_id)))).scalar_one()
        tool = ReadQueryTool()
        events = []
        async for evt in tool.run_stream(
            {"result_file_id": result_file_id, "match": "ZZZ-NEEDLE-TRACK-\\d+"},
            {"db": db, "organization": org, "report": report, "settings": None},
        ):
            events.append(evt)
        end = events[-1]
        out = end.payload["output"]
        ok = out["success"] and out["slice"]["total_matches"] == 1
        check("read_query tool slice-mode grep (real agent tool)", ok, json.dumps(out)[:200])
        summary = end.payload["observation"]["summary"]
        check("read_query slice observation summarizes for the LLM", "Sliced artifact" in summary, summary)


async def verify_rerun(HO, org_id, report_id, step_id, first_result_file_id):
    """HTTP rerun -> step_service.rerun_step -> new artifact + lineage."""
    r = requests.post(f"{BASE}/api/reports/{report_id}/rerun", headers=HO, timeout=600)
    check("HTTP POST /reports/{id}/rerun (real endpoint)", r.status_code == 200, r.text[:300])

    from sqlalchemy import select
    from app.settings.database import create_async_session_factory
    from app.models.result_file import ResultFile
    from app.services.result_store import ResultStore

    Session = create_async_session_factory()
    svc = ResultStore()
    async with Session() as db:
        rows = (await db.execute(
            select(ResultFile).where(ResultFile.step_id == str(step_id))
            .order_by(ResultFile.created_at)
        )).scalars().all()
        check("rerun produced a second artifact for the step", len(rows) == 2, f"count={len(rows)}")
        if len(rows) == 2:
            old, new = rows
            check("old artifact superseded by new (write-once lineage)",
                  old.id == first_result_file_id and old.superseded_by == new.id and new.superseded_by is None)
            check("rerun artifact produced by the rerun path", new.producer == "rerun", new.producer)
            # old payload still frozen + readable
            page = svc.slice_sync(old, offset=0, limit=3)
            check("superseded artifact still slices (frozen evidence)", len(page["rows"]) == 3)
            latest = await svc.latest_for_step(db, str(org_id), step_id=str(step_id))
            check("latest_for_step resolves to rerun artifact", latest.id == new.id)


async def main():
    token, org_id, ds_id, report_id, HO = http_setup()
    step_id, result_file_id, total_rows = await data_plane(org_id, ds_id, report_id)
    await verify_slices(org_id, report_id, result_file_id, total_rows)
    await verify_read_query_tool(org_id, report_id, result_file_id)
    await verify_rerun(HO, org_id, report_id, step_id, result_file_id)

    print("\n==== SUMMARY ====")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} passed")
    if passed == len(RESULTS):
        print("ALL PASSED")
        print(json.dumps({"report_id": report_id, "step_id": step_id, "result_file_id": result_file_id,
                          "email": EMAIL, "password": PASSWORD, "org_id": org_id}))
        return 0
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
