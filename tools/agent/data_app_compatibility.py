"""Seed synthetic dynamic options and an unversioned dashboard; use the sandbox backend environment."""

import json, httpx, asyncio, os
from pathlib import Path

run = Path(os.environ.get("BOW_DATA_APP_RUN", "/tmp/bow-data-app-run"))
s = json.loads((run / "session.json").read_text())
manifest = json.loads((run / "apps/manifest.json").read_text())
h = {
    "Authorization": "Bearer " + s["admin"]["token"],
    "X-Organization-Id": s["organization"]["id"],
}
c = httpx.Client(base_url=s["base_url"], headers=h, timeout=180)


def api(m, p, **kw):
    r = c.request(m, p, **kw)
    r.raise_for_status()
    return r.json()


if not manifest["catalog"].get("options_query_id"):
    app = manifest["catalog"]
    q = api(
        "POST",
        "/api/queries",
        json={"title": "Stable genre choices", "report_id": app["report_id"]},
    )
    code = 'def generate_df(ds_clients, excel_files):\n    return ds_clients[next(iter(ds_clients))].execute_query("SELECT DISTINCT genre, genre AS label FROM albums ORDER BY genre")\n'
    r = api(
        "POST",
        "/api/queries/" + q["id"] + "/run",
        json={"code": code, "mode": "builder"},
    )
    assert r["step"]["status"] == "success"
    for query in app["queries"]:
        for spec in query["parameters"]:
            if spec["name"] == "genre":
                spec.pop("options", None)
                spec["options_source"] = {
                    "query_id": q["id"],
                    "value_column": "genre",
                    "label_column": "label",
                }
        r = api(
            "POST",
            "/api/queries/" + query["id"] + "/run",
            json={
                "code": query["code"],
                "parameters": query["parameters"],
                "params": {p["name"]: p.get("default") for p in query["parameters"]},
                "row_limit": 10000,
                "mode": "builder",
            },
        )
        assert r["step"]["status"] == "success"
    app["options_query_id"] = q["id"]
    (run / "apps/manifest.json").write_text(json.dumps(manifest, indent=2))
# A stored pre-v11 artifact exercises positional data binding, raw palette classes,
# legacy formatting, charts, and declaration-driven controls. No runtime stamp.
if (run / "legacy-manifest.json").exists():
    print("Compatibility fixtures already installed")
    raise SystemExit(0)
report = api(
    "POST",
    "/api/reports",
    json={
        "title": "Legacy Compatibility Dashboard",
        "data_sources": [s["sqlite_sources"][0]["id"]],
    },
)
original = manifest["commerce"]["queries"][0]
query = api(
    "POST",
    "/api/queries",
    json={"title": "Legacy weekly revenue", "report_id": report["id"]},
)
r = api(
    "POST",
    "/api/queries/" + query["id"] + "/run",
    json={
        "code": original["code"],
        "parameters": original["parameters"],
        "params": {p["name"]: p.get("default") for p in original["parameters"]},
        "row_limit": 10000,
        "mode": "builder",
    },
)
assert r["step"]["status"] == "success"


async def viz():
    import main
    from app.dependencies import async_session_maker
    from app.models.visualization import Visualization

    async with async_session_maker() as db:
        v = Visualization(
            title="Legacy weekly revenue",
            status="success",
            query_id=query["id"],
            report_id=report["id"],
            view={"type": "table"},
        )
        db.add(v)
        await db.commit()
        return str(v.id)


vid = asyncio.run(viz())
code = """<script type="text/babel">
function App() {
 const data=useArtifactData(); const p=useParams(); const options=useParamOptions('region')||[];
 if (!data) return <div>Loading...</div>;
 const viz=data.visualizations[0]; const rows=viz?.rows||[];
 return <div className="p-8 bg-slate-50 text-slate-900 min-h-screen">
 <h1 className="text-2xl font-bold mb-4">Legacy revenue dashboard</h1>
 <label>Region <select aria-label="Region" value={p.values.region||''} onChange={e=>p.setParam('region',e.target.value||null)}><option value="">All regions</option>{options.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
 <label>Minimum order <input type="number" value={p.values.min_order??0} onChange={e=>p.setParam('min_order',Number(e.target.value))}/></label>
 <div>{p.loading?'Updating...':''}{p.error}</div>
 <KPICard title="Revenue" value={fmt(rows.reduce((s,r)=>s+r.revenue,0),{currency:true})}/>
 <p>Legacy percentage: {fmt(0.37,{pct:true})}</p>
 <SectionCard title="Weekly revenue"><EChart option={{xAxis:{type:'category',data:rows.map(r=>r.week)},yAxis:{type:'value'},series:[{type:'line',data:rows.map(r=>r.revenue)}]}}/></SectionCard>
 </div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>"""
artifact = api(
    "POST",
    "/api/artifacts",
    json={
        "report_id": report["id"],
        "title": "Legacy Compatibility Dashboard",
        "mode": "page",
        "content": {"code": code, "visualization_ids": [vid]},
    },
)
api(
    "PUT",
    "/api/reports/" + report["id"] + "/visibility/artifact",
    json={"visibility": "internal", "run_identity": "viewer"},
)
(run / "legacy.json").write_text(
    json.dumps(
        {
            "report_id": report["id"],
            "artifact_id": artifact["id"],
            "query_id": query["id"],
            "viz_id": vid,
        },
        indent=2,
    )
)
print("Dynamic options and legacy dashboard seeded")


# Exercise an ordinary edit on a legacy artifact whose old controls are incomplete.
# The create gate is intentionally stricter than the legacy edit delta gate.
async def edit_legacy():
    import main
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.models.user import User
    from app.models.organization import Organization
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool

    async with async_session_maker() as db:
        stored = await db.get(Report, report["id"])
        user = await db.get(User, stored.user_id)
        org = await db.get(Organization, stored.organization_id)
        args = {
            "artifact_id": artifact["id"],
            "edits": [
                {
                    "find": "Legacy revenue dashboard",
                    "replace": "Legacy revenue overview",
                }
            ],
        }
        async for event in EditArtifactTool().run_stream(
            args, {"db": db, "report": stored, "user": user, "organization": org}
        ):
            if event.type == "tool.end":
                observation = event.payload.get("observation", {})
                output = event.payload.get("output", {})
                if observation.get("error"):
                    raise RuntimeError(str(observation["error"]))
                return output.get("artifact_id") or observation["artifact_id"]


latest = asyncio.run(edit_legacy())
(run / "legacy-manifest.json").write_text(
    json.dumps(
        {"legacy": {"report_id": report["id"], "artifact_id": latest, "viz_id": vid}},
        indent=2,
    )
)
