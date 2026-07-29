#!/usr/bin/env python3
"""Comprehensive multi-user / group seed for the viewer-run e2e pass.

Builds, through the real API where possible:
  - admin owner (admin@example.com)
  - three member users u_alice / u_bob / u_carol, each mapped to a Postgres
    DB role (alice/bob/carol) whose RLS policy returns a distinct row set
  - a group "Analysts" containing the three members
  - a user_required Postgres data source (system creds = superuser) with each
    member's per-user credentials stored
  - a shared dashboard (artifact) querying the RLS table, materialized by an
    owner rerun (so the shared snapshot holds the owner's / alice's rows)

Run with the live stack env:
  cd backend && TESTING=true ENVIRONMENT=production \
    TEST_DATABASE_URL=sqlite:///db/agent.db \
    uv run python ../tools/agent/seed_comprehensive.py
Prints a JSON summary.
"""
import asyncio
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta

import httpx

BASE = "http://localhost:8000"
DB_PATH = os.environ.get("AGENT_DB", "db/agent.db")
PG = {"host": "localhost", "port": int(os.environ.get("RLS_PG_PORT", "55432")), "database": "salesdb"}

# member email -> (password, db role, db password)
MEMBERS = {
    "u_alice@example.com": ("Password123!", "alice", "alice-pass-1"),
    "u_bob@example.com": ("Password123!", "bob", "bob-pass-1"),
    "u_carol@example.com": ("Password123!", "carol", "carol-pass-1"),
}

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
        <thead><tr className="text-left text-slate-500">
          <th className="px-4 py-2">Month</th><th className="px-4 py-2">Revenue</th>
          <th className="px-4 py-2">Sales rep (DB identity)</th></tr></thead>
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


def login(c, email, pw):
    r = c.post("/api/auth/jwt/login", data={"username": email, "password": pw})
    r.raise_for_status()
    return r.json()["access_token"]


def h(tok, org=None):
    d = {"Authorization": f"Bearer {tok}"}
    if org:
        d["X-Organization-Id"] = org
    return d


def invite_and_register(c, org, admin_tok, email, pw):
    r = c.post(f"/api/organizations/{org}/members",
               json={"organization_id": org, "email": email, "role": "member"}, headers=h(admin_tok, org))
    if r.status_code not in (200, 201):
        # maybe already exists
        if "ALREADY" not in r.text and r.status_code != 400:
            r.raise_for_status()
    token = sqlite3.connect(DB_PATH).execute(
        "SELECT invite_token FROM memberships WHERE email=? AND user_id IS NULL ORDER BY created_at DESC LIMIT 1",
        (email,)).fetchone()
    invite_token = token[0] if token else None
    body = {"name": email.split("@")[0], "email": email, "password": pw}
    if invite_token:
        body["invite_token"] = invite_token
    rr = c.post("/api/auth/register", json=body)
    if rr.status_code not in (201, 200) and "ALREADY" not in rr.text:
        # already registered is fine
        pass
    return login(c, email, pw)


async def seed_graph(report_id):
    import main  # noqa: F401 — load full model registry
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
        db.add(widget); await db.flush()
        query = Query(title="Revenue by rep", report_id=report_id, widget_id=widget.id,
                      organization_id=org_id, user_id=user_id)
        db.add(query); await db.flush()
        step = Step(title="Revenue by rep", slug=f"step-{suffix}", status="success",
                    widget_id=widget.id, query_id=query.id, code=STEP_CODE,
                    data={"rows": [], "columns": []}, created_at=datetime.utcnow() - timedelta(hours=1))
        db.add(step); await db.flush()
        query.default_step_id = step.id
        viz = Visualization(title="Revenue by rep", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "table"})
        db.add(viz); await db.flush()
        db.add(Artifact(report_id=report_id, user_id=user_id, organization_id=org_id,
                        title="Revenue by Sales Rep", mode="page", version=1,
                        content={"code": ARTIFACT_CODE, "visualization_ids": [str(viz.id)]},
                        status="completed"))
        await db.commit()
        return {"query_id": str(query.id), "step_id": str(step.id)}


def main_():
    c = httpx.Client(base_url=BASE, timeout=60)
    admin_tok = login(c, "admin@example.com", "Password123!")
    org = c.get("/api/organizations", headers=h(admin_tok)).json()[0]["id"]

    members = {}
    for email, (pw, role, dbpw) in MEMBERS.items():
        tok = invite_and_register(c, org, admin_tok, email, pw)
        uid = c.get("/api/users/whoami", headers=h(tok, org)).json()["id"]
        members[email] = {"token": tok, "user_id": uid, "db_role": role, "db_pw": dbpw}

    # Group "Analysts" with all three members (enterprise; best-effort).
    group_id = None
    try:
        g = c.post(f"/api/organizations/{org}/groups",
                   json={"name": f"Analysts {uuid.uuid4().hex[:4]}", "description": "RLS demo"},
                   headers=h(admin_tok, org))
        if g.status_code in (200, 201):
            group_id = g.json()["id"]
            for m in members.values():
                c.post(f"/api/organizations/{org}/groups/{group_id}/members",
                       json={"user_id": m["user_id"]}, headers=h(admin_tok, org))
    except Exception as e:
        print(f"group setup skipped: {e}", file=sys.stderr)

    # user_required Postgres data source; system creds = superuser.
    ds = c.post("/api/data_sources", json={
        "name": f"Sales DB (RLS) {uuid.uuid4().hex[:4]}", "type": "postgresql",
        "config": PG, "credentials": {"user": "postgres", "password": ""},
        "auth_policy": "user_required", "allowed_user_auth_modes": ["userpass"],
        "is_public": True,
    }, headers=h(admin_tok, org))
    ds.raise_for_status()
    ds_id = ds.json()["id"]

    # Owner queries as alice; each member stores their own DB role creds.
    c.post(f"/api/data_sources/{ds_id}/my-credentials",
           json={"auth_mode": "userpass", "credentials": {"user": "alice", "password": "alice-pass-1"}},
           headers=h(admin_tok, org)).raise_for_status()
    for m in members.values():
        c.post(f"/api/data_sources/{ds_id}/my-credentials",
               json={"auth_mode": "userpass", "credentials": {"user": m["db_role"], "password": m["db_pw"]}},
               headers=h(m["token"], org)).raise_for_status()

    # Shared dashboard.
    report = c.post("/api/reports", json={"title": "Revenue by Sales Rep (RLS)", "widget": None,
                                          "files": [], "data_sources": [ds_id]}, headers=h(admin_tok, org))
    report.raise_for_status()
    rid = report.json()["id"]
    ids = asyncio.run(seed_graph(rid))
    c.put(f"/api/reports/{rid}/visibility/artifact",
          json={"visibility": "internal", "run_identity": "viewer"}, headers=h(admin_tok, org)).raise_for_status()
    c.post(f"/api/reports/{rid}/rerun", headers=h(admin_tok, org)).raise_for_status()

    print(json.dumps({
        "org_id": org, "data_source_id": ds_id, "group_id": group_id,
        "report_id": rid, **ids,
        "members": {e: {"user_id": m["user_id"], "db_role": m["db_role"]} for e, m in members.items()},
    }))


if __name__ == "__main__":
    main_()
