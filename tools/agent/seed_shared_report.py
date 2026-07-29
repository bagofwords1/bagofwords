#!/usr/bin/env python3
"""Seed a shared dashboard report for viewer-run UI verification.

Run with the live stack's env so the models bind the same sqlite DB:

    cd backend && TESTING=true ENVIRONMENT=production \
      TEST_DATABASE_URL=sqlite:///db/agent.db \
      uv run python <this file>

Creates (as admin@example.com):
  - a report with artifact_visibility='internal'
  - one query whose default step holds an obviously STALE snapshot and whose
    saved code produces FRESH 2024 rows when re-executed
  - a page artifact rendering the visualization as a table

Prints JSON {report_id, query_id, step_id}.
"""
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta

import httpx

BASE = "http://localhost:8000"
ADMIN = {"username": "admin@example.com", "password": "Password123!"}

GOOD_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({
        "month": ["2024-01", "2024-02", "2024-03"],
        "revenue": [1250, 1810, 2140],
    })
"""

STALE_DATA = {
    "rows": [
        {"month": "2023-10 (creator snapshot)", "revenue": 999},
        {"month": "2023-11 (creator snapshot)", "revenue": 888},
    ],
    "columns": [{"field": "month"}, {"field": "revenue"}],
}

ARTIFACT_CODE = """<script type="text/babel">
function App() {
  const data = useArtifactData();
  if (!data) return <div className="flex items-center justify-center h-screen">Loading…</div>;
  const viz = (data.visualizations && data.visualizations[0]) || { rows: [] };
  return (
    <div className="min-h-full bg-gradient-to-br from-slate-50 to-slate-100 p-10">
      <h1 className="text-2xl font-bold text-slate-800 mb-1">Monthly Revenue</h1>
      <p className="text-sm text-slate-500 mb-6">Demo dashboard — shared with the organization</p>
      <table className="bg-white rounded-lg shadow text-sm w-96">
        <thead>
          <tr className="text-left text-slate-500">
            <th className="px-4 py-2">Month</th>
            <th className="px-4 py-2">Revenue</th>
          </tr>
        </thead>
        <tbody>
          {viz.rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-100">
              <td className="px-4 py-2 font-medium text-slate-700">{r.month}</td>
              <td className="px-4 py-2 text-slate-600">{r.revenue}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""


def api_setup():
    client = httpx.Client(base_url=BASE, timeout=30)
    r = client.post("/api/auth/jwt/login", data=ADMIN)
    r.raise_for_status()
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    orgs = client.get("/api/organizations", headers=headers).json()
    org_id = orgs[0]["id"]
    headers["X-Organization-Id"] = org_id
    r = client.post(
        "/api/reports",
        json={"title": "Monthly Revenue (shared)", "widget": None, "files": [], "data_sources": []},
        headers=headers,
    )
    r.raise_for_status()
    report = r.json()
    return client, headers, report


async def seed_graph(report_id: str):
    # Load the app exactly like the server boot does so the FULL model
    # registry is configured (relationship strings resolve against classes
    # defined outside app/models too, e.g. ee modules).
    import main  # noqa: F401
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
            title="Monthly revenue",
            report_id=report_id,
            widget_id=widget.id,
            organization_id=org_id,
            user_id=user_id,
        )
        db.add(query)
        await db.flush()

        step = Step(
            title="Monthly revenue",
            slug=f"step-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            code=GOOD_CODE,
            data=STALE_DATA,
            created_at=datetime.utcnow() - timedelta(hours=5),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id

        viz = Visualization(
            title="Monthly revenue",
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
            title="Monthly Revenue",
            mode="page",
            version=1,
            content={"code": ARTIFACT_CODE, "visualization_ids": [str(viz.id)]},
            status="completed",
        ))
        await db.commit()
        return {"query_id": str(query.id), "step_id": str(step.id)}


def main():
    client, headers, report = api_setup()
    ids = asyncio.run(seed_graph(report["id"]))
    r = client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal"},
        headers=headers,
    )
    r.raise_for_status()
    print(json.dumps({"report_id": report["id"], **ids}))


if __name__ == "__main__":
    main()
