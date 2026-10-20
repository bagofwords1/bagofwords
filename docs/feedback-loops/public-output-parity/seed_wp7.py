"""Seed the WP7 sandbox scenario into the running stack's DB (owner = seeded admin).

1. Report "Identity dash": query with an identity-source param, rows marked
   SECRET-ROW-*, page artifact, artifact_visibility=public.
2. Report "Plain dash": same shape, no identity param (must still export rows).
3. A public shared conversation whose prompt/blocks contain an email + EMP id.
PII protection is enabled through the real API before running this.
Prints a JSON of ids/tokens.
"""
import asyncio
import json
import sys
import uuid

sys.path.insert(0, ".")
import main  # noqa: F401,E402
from sqlalchemy import select  # noqa: E402

from app.dependencies import async_session_maker  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.report import Report  # noqa: E402
from app.models.widget import Widget  # noqa: E402
from app.models.step import Step  # noqa: E402
from app.models.query import Query  # noqa: E402
from app.models.visualization import Visualization  # noqa: E402
from app.models.completion import Completion  # noqa: E402
from app.models.completion_block import CompletionBlock  # noqa: E402
from app.models.agent_execution import AgentExecution  # noqa: E402
from app.models.plan_decision import PlanDecision  # noqa: E402
from tests.fixtures.artifact import seed_artifact  # noqa: E402

CODE = """<script type="text/babel">
function App() {
  const data = useArtifactData();
  if (!data) return <div>loading</div>;
  const v = data.visualizations[0];
  const rows = v.rows || [];
  return (<div className="p-6"><h1 className="text-2xl font-bold">{data.report.title}</h1>
    <div id="rowcount">rows: {rows.length}</div>
    <EChart option={{xAxis:{type:'category',data:rows.map(r=>r.name)},yAxis:{type:'value'},
      series:[{type:'bar',data:rows.map(r=>r.amount)}]}} style={{height:240}} />
    <table>{rows.map((r,i)=><tr key={i}><td>{r.name}</td><td>{r.amount}</td></tr>)}</table></div>);
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""


async def dash(db, org, user, title, params, prefix):
    s = uuid.uuid4().hex[:6]
    r = Report(title=title, slug=f"wp7-{s}", organization_id=org.id, user_id=user.id,
               status="published", artifact_visibility="public")
    db.add(r); await db.flush()
    w = Widget(title=title, slug=f"w-{s}", report_id=r.id); db.add(w); await db.flush()
    st = Step(title=title, slug=f"s-{s}", status="success", widget_id=w.id,
              data={"rows": [{"name": f"{prefix}-{i}", "amount": 10 * (i + 1)} for i in range(4)],
                    "columns": [{"field": "name"}, {"field": "amount"}]},
              data_model={"type": "bar_chart"}, applied_params={"email": user.email} if params else None)
    db.add(st); await db.flush()
    q = Query(title=title, report_id=r.id, widget_id=w.id, default_step_id=st.id,
              parameters=params, organization_id=org.id)
    db.add(q); await db.flush()
    st.query_id = q.id
    v = Visualization(title=title, status="success", report_id=r.id, query_id=q.id, view={"type": "bar_chart"})
    db.add(v); await db.flush()
    a = await seed_artifact(db, report_id=r.id, user_id=user.id, organization_id=org.id, mode="page",
                            title=title, content={"code": CODE, "visualization_ids": [str(v.id)]})
    return {"report_id": str(r.id), "query_id": str(q.id), "artifact_id": str(a.id)}


async def main_():
    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "admin@example.com"))).scalar_one()
        org = (await db.execute(select(Organization).where(Organization.id == "b95f4dc2-d638-488e-9c07-0a3d4f6a2cac"))).scalars().first()
        out = {
            "identity": await dash(db, org, user, "Identity dash", [{"name": "email", "type": "string", "source": "identity"}], "SECRET-ROW"),
            "plain": await dash(db, org, user, "Plain dash", [], "PUBLIC-ROW"),
        }
        tok = f"tok-{uuid.uuid4().hex}"
        r = Report(title="PII conversation", slug=f"wp7c-{uuid.uuid4().hex[:6]}", status="draft",
                   user_id=user.id, organization_id=org.id, conversation_visibility="public",
                   conversation_share_token=tok)
        db.add(r); await db.flush()
        u = Completion(prompt={"content": "Who is jane.doe@acme-corp.com (EMP-4821)? Also: what was Q3 revenue?"},
                       completion=None, status="success", role="user", report_id=r.id, user_id=user.id, turn_index=0)
        sc = Completion(prompt=None, completion={"content": "ok"}, status="success", role="system",
                        report_id=r.id, turn_index=0)
        db.add_all([u, sc]); await db.flush()
        ae = AgentExecution(completion_id=sc.id, organization_id=org.id, user_id=user.id, report_id=r.id, status="success")
        db.add(ae); await db.flush()
        pd = PlanDecision(agent_execution_id=ae.id, seq=0, loop_index=0, plan_type="action",
                          reasoning="EMP-4821 matched", assistant="Contact jane.doe@acme-corp.com",
                          final_answer="EMP-4821 is jane.doe@acme-corp.com. Q3 revenue was $1.2M.",
                          analysis_complete=True)
        db.add(pd); await db.flush()
        db.add(CompletionBlock(completion_id=sc.id, agent_execution_id=ae.id, plan_decision_id=pd.id,
                               source_type="decision", title="answer", block_index=0, status="completed",
                               content="EMP-4821 is jane.doe@acme-corp.com. Q3 revenue was $1.2M.",
                               reasoning="looked up jane.doe@acme-corp.com"))
        await db.commit()
        out["conversation_token"] = tok
        print(json.dumps(out))


asyncio.run(main_())
