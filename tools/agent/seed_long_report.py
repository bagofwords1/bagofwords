#!/usr/bin/env python3
"""Seed a long report — 12 turns with tool cards and a dashboard artifact — to
reproduce report-page scroll jumps and dashboard reloads on load.

    cd backend && TESTING=true ENVIRONMENT=production       TEST_DATABASE_URL=sqlite:///db/agent.db       uv run python ../tools/agent/seed_long_report.py

Prints JSON {report_id, artifact_id, viz_id}. Refresh-on-view reruns a report
at most once per 5 minutes, so seed a fresh report per measured page load.
"""
import asyncio, json, os, sys, uuid
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_shared_report as base

LONG = "\n\n".join([
    "## Findings\n\nRevenue grew steadily across the quarter. " * 2,
    "| Month | Revenue |\n|---|---|\n| Jan | 1,250 |\n| Feb | 1,810 |\n| Mar | 2,140 |",
    "- Point one about the data\n- Point two about seasonality\n- Point three about retention",
    "Next, I would look at churn by cohort and the regional split. " * 3,
])

async def seed_turns(report_id, viz_id, artifact_id, turns=12):
    import main  # noqa
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.agent_execution import AgentExecution
    from app.models.plan_decision import PlanDecision
    from app.models.tool_execution import ToolExecution
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        t0 = datetime.utcnow() - timedelta(hours=3)
        for i in range(turns):
            ts = t0 + timedelta(minutes=10 * i)
            u = Completion(report_id=report_id, user_id=report.user_id, role="user",
                           message_type="table", prompt={"content": f"Question {i+1}: how did revenue change, and update the dashboard?"},
                           completion={}, status="success", created_at=ts, turn_index=i)
            db.add(u); await db.flush()
            s = Completion(report_id=report_id, role="system", parent_id=u.id, prompt=None,
                           completion={"content": LONG}, status="success", created_at=ts + timedelta(seconds=5), turn_index=i)
            db.add(s); await db.flush()
            ae = AgentExecution(completion_id=s.id, status="completed", organization_id=report.organization_id,
                                user_id=report.user_id, report_id=report_id)
            db.add(ae); await db.flush()
            pd = PlanDecision(agent_execution_id=ae.id, seq=0, reasoning="Looking at the data. " * 5,
                              assistant="I'll read the dashboard and edit it.")
            db.add(pd); await db.flush()
            idx = 0
            db.add(CompletionBlock(completion_id=s.id, agent_execution_id=ae.id, source_type="decision",
                                   plan_decision_id=pd.id, block_index=idx, title="Planning", status="completed",
                                   content="I'll read the dashboard and edit it.", reasoning="Looking at the data. " * 5)); idx += 1
            tool = "read_artifact" if i % 2 == 0 else "edit_artifact"
            rj = {"artifact_id": artifact_id, "title": "Monthly Revenue", "mode": "page", "version": 1,
                  "artifact_preview": {"title": "Monthly Revenue", "mode": "page", "version": 1, "code_stats": {"lines": 40, "chars": 1800}},
                  "code_preview": {"code": base.ARTIFACT_CODE}}
            te = ToolExecution(agent_execution_id=ae.id, plan_decision_id=pd.id, tool_name=tool,
                               arguments_json={"artifact_id": artifact_id}, status="success", success=True,
                               duration_ms=5400, result_json=rj, result_summary="ok",
                               artifact_refs_json={"visualizations": [viz_id]})
            db.add(te); await db.flush()
            db.add(CompletionBlock(completion_id=s.id, agent_execution_id=ae.id, source_type="tool",
                                   tool_execution_id=te.id, plan_decision_id=pd.id, block_index=idx,
                                   title=tool, status="completed")); idx += 1
            db.add(CompletionBlock(completion_id=s.id, agent_execution_id=ae.id, source_type="final",
                                   block_index=idx, title="Answer", status="completed", content=LONG))
        await db.commit()

async def seed_graph(report_id):
    # Query graph only; the artifact is created through the API.
    import main  # noqa
    from app.dependencies import async_session_maker
    from app.models.query import Query
    from app.models.report import Report
    from app.models.step import Step
    from app.models.visualization import Visualization
    from app.models.widget import Widget
    sfx = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        w = Widget(title=f"W {sfx}", slug=f"w-{sfx}", report_id=report_id); db.add(w); await db.flush()
        q = Query(title="Monthly revenue", report_id=report_id, widget_id=w.id, organization_id=report.organization_id, user_id=report.user_id); db.add(q); await db.flush()
        st = Step(title="Monthly revenue", slug=f"step-{sfx}", status="success", widget_id=w.id, query_id=q.id, code=base.GOOD_CODE, data=base.STALE_DATA, created_at=datetime.utcnow() - timedelta(hours=5)); db.add(st); await db.flush()
        q.default_step_id = st.id
        v = Visualization(title="Monthly revenue", status="success", report_id=report_id, query_id=q.id, view={"type": "table"}); db.add(v)
        await db.commit()
        return str(v.id)

def main():
    client, headers, report = base.api_setup()
    async def run():
        vid = await seed_graph(report["id"])
        r = client.post("/api/artifacts", headers=headers, json={"report_id": report["id"], "title": "Monthly Revenue", "mode": "page",
                        "content": {"code": base.ARTIFACT_CODE, "visualization_ids": [vid]}})
        r.raise_for_status(); art = r.json()
        await seed_turns(report["id"], vid, art["id"])
        return vid, art["id"]
    vid, aid = asyncio.run(run())
    print(json.dumps({"report_id": report["id"], "artifact_id": aid, "viz_id": vid}))

if __name__ == "__main__":
    main()
