#!/usr/bin/env python3
"""Seed a shared dashboard backed by a user_required Postgres data source,
for the per-user-credential verification loop
(docs/feedback-loops/shared-artifact-viewer-runs.md, Loop C).

Prerequisites:
  - running stack (backend :8000) with an enterprise license active
    (user_required auth is licence-gated)
  - a Postgres with an RLS table `monthly_revenue(month, revenue, sales_rep)`
    and per-rep policies, users alice/bob (see the feedback-loop doc)
  - admin@example.com (owner) and viewer@example.com (member) registered

Run with the live stack's env so the models bind the same sqlite DB:

    cd backend && TESTING=true ENVIRONMENT=production \
      TEST_DATABASE_URL=sqlite:///db/agent.db \
      uv run python ../tools/agent/seed_rls_report.py

Creates a user_required postgresql data source (system creds = postgres
superuser), stores alice's creds for the owner and bob's for the viewer,
attaches a report + artifact whose step queries the RLS table, and reruns
the report as the owner so the shared snapshot holds alice's rows.
Prints JSON {report_id, data_source_id, ...}.
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta

import httpx

BASE = "http://localhost:8000"
PG = {
    "host": "localhost",
    "port": int(os.environ.get("RLS_PG_PORT", "55432")),
    "database": "salesdb",
}
OWNER = {"username": "admin@example.com", "password": "Password123!"}
VIEWER = {"username": "viewer@example.com", "password": "Password123!"}

STEP_CODE = """
def generate_df(ds_clients, excel_files):
    client = ds_clients[next(iter(ds_clients))]
    return client.execute_query(
        "SELECT month, revenue, sales_rep FROM monthly_revenue ORDER BY month"
    )
"""

ARTIFACT_CODE = """<script type="text/babel">
function App() {
  const data = useArtifactData();
  if (!data) return <div className="flex items-center justify-center h-screen">Loading…</div>;
  const viz = (data.visualizations && data.visualizations[0]) || { rows: [] };
  return (
    <div className="min-h-full bg-gradient-to-br from-slate-50 to-slate-100 p-10">
      <h1 className="text-2xl font-bold text-slate-800 mb-1">Revenue by Sales Rep</h1>
      <p className="text-sm text-slate-500 mb-6">Live Postgres query — row-level security decides what you see</p>
      <table className="bg-white rounded-lg shadow text-sm w-[480px]">
        <thead>
          <tr className="text-left text-slate-500">
            <th className="px-4 py-2">Month</th>
            <th className="px-4 py-2">Revenue</th>
            <th className="px-4 py-2">Sales rep (DB identity)</th>
          </tr>
        </thead>
        <tbody>
          {viz.rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-100">
              <td className="px-4 py-2 font-medium text-slate-700">{r.month}</td>
              <td className="px-4 py-2 text-slate-600">{r.revenue}</td>
              <td className="px-4 py-2 font-semibold text-blue-600">{r.sales_rep}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""


def login(client, creds):
    r = client.post("/api/auth/jwt/login", data=creds)
    r.raise_for_status()
    return r.json()["access_token"]


def headers(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if org_id:
        h["X-Organization-Id"] = org_id
    return h


async def seed_graph(report_id: str):
    import main  # noqa: F401 — load the full model registry like server boot
    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.models.query import Query
    from app.models.report import Report
    from app.models.step import Step
    from app.models.visualization import Visualization
    from app.models.widget import Widget

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id

        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()

        query = Query(
            title="Revenue by rep",
            report_id=report_id,
            widget_id=widget.id,
            organization_id=org_id,
            user_id=user_id,
        )
        db.add(query)
        await db.flush()

        step = Step(
            title="Revenue by rep",
            slug=f"step-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            code=STEP_CODE,
            data={"rows": [], "columns": []},
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id

        viz = Visualization(
            title="Revenue by rep",
            status="success",
            report_id=report_id,
            query_id=query.id,
            view={"type": "table"},
        )
        db.add(viz)
        await db.flush()

        db.add(Artifact(
            report_id=report_id,
            user_id=user_id,
            organization_id=org_id,
            title="Revenue by Sales Rep",
            mode="page",
            version=1,
            content={"code": ARTIFACT_CODE, "visualization_ids": [str(viz.id)]},
            status="completed",
        ))
        await db.commit()
        return {"query_id": str(query.id), "step_id": str(step.id)}


def main_():
    c = httpx.Client(base_url=BASE, timeout=60)
    owner_tok = login(c, OWNER)
    org = c.get("/api/organizations", headers=headers(owner_tok)).json()[0]["id"]
    oh = headers(owner_tok, org)

    # user_required postgres data source; system creds (superuser) cover
    # indexing and the owner/admin fallback, per-user creds do the querying.
    r = c.post("/api/data_sources", json={
        "name": f"Sales DB (RLS) {uuid.uuid4().hex[:4]}",
        "type": "postgresql",
        "config": PG,
        "credentials": {"user": "postgres", "password": ""},
        "auth_policy": "user_required",
        "allowed_user_auth_modes": ["userpass"],
        "is_public": True,
    }, headers=oh)
    r.raise_for_status()
    ds_id = r.json()["id"]

    # Owner queries as alice; viewer queries as bob.
    r = c.post(f"/api/data_sources/{ds_id}/my-credentials", json={
        "auth_mode": "userpass",
        "credentials": {"user": "alice", "password": "alice-pass-1"},
    }, headers=oh)
    r.raise_for_status()

    viewer_tok = login(c, VIEWER)
    vh = headers(viewer_tok, org)
    r = c.post(f"/api/data_sources/{ds_id}/my-credentials", json={
        "auth_mode": "userpass",
        "credentials": {"user": "bob", "password": "bob-pass-1"},
    }, headers=vh)
    r.raise_for_status()

    r = c.post("/api/reports", json={
        "title": "Revenue by Sales Rep (RLS)", "widget": None,
        "files": [], "data_sources": [ds_id],
    }, headers=oh)
    r.raise_for_status()
    report = r.json()

    ids = asyncio.run(seed_graph(report["id"]))

    r = c.put(f"/api/reports/{report['id']}/visibility/artifact",
              json={"visibility": "internal", "run_identity": "viewer"}, headers=oh)
    r.raise_for_status()

    # Owner refresh materializes the shared snapshot under ALICE's credentials.
    r = c.post(f"/api/reports/{report['id']}/rerun", headers=oh)
    r.raise_for_status()
    rerun = r.json()

    print(json.dumps({
        "report_id": report["id"], "data_source_id": ds_id,
        "owner_rerun": rerun, **ids,
    }))


if __name__ == "__main__":
    main_()
