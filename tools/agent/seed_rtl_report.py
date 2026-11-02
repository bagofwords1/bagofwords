#!/usr/bin/env python3
"""Seed a Hebrew report that exercises every right-to-left surface of the
report page, for RTL verification and UI evidence.

    cd backend && TESTING=true ENVIRONMENT=production \
      TEST_DATABASE_URL=sqlite:///db/agent.db \
      uv run python ../tools/agent/seed_rtl_report.py

Creates (as admin@example.com, through the API + the app's own models):
  - turn 1: a Hebrew question that opens with an @mention; the answer opens
    with a bolded English metric and holds an English heading, a list of
    amounts, a table with English headers and Hebrew rows, a list whose items
    open with English metrics, a nested list and a quote
  - a create_data tool card whose rows are Hebrew (grid cell alignment)
  - a create_note tool card with a Hebrew note (markdown outside the chat
    containers)
  - turn 2: a Hebrew question that opens with an English term
  - turn 3: an English question with an English answer (no regressions)
  - a page artifact (dashboard) written in Hebrew with no `dir` of its own

Prints JSON {report_id, artifact_id}.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_shared_report as base  # noqa: E402

Q1 = "@Sales DB מה היו ההכנסות ברבעון האחרון לפי אזור, ואיך זה משתווה לאותו רבעון בשנה שעברה? תציג גם טבלה."
A1 = """**Revenue** ברבעון השלישי עלה ב-12% לעומת הרבעון המקביל אשתקד, בעיקר בזכות אזור הצפון שהציג צמיחה חדה במיוחד בסוף הרבעון.

## Summary

להלן ההכנסות לפי אזור:

- **North**: 1.2M USD
- **South**: 0.9M USD
- אזור המרכז: 0.7M USD
- Online / E-commerce: 0.4M USD

| Region | Customer | Status | Amount | Change |
|---|---|---|---|---|
| צפון | חברת אלפא בע״מ | פעיל | 1,200 | +14% |
| דרום | בטא תעשיות | לא פעיל | 900 | +6% |
| מרכז | גמא שירותים | Active | 700 | -2% |

### Key Drivers

1. **MRR** גדל ב-5% לעומת החודש הקודם
2. **Churn** ירד ל-2.1%
3. **NPS** נשאר יציב על 42

- נקודה ראשית: הצפון מוביל
  - Sub point: SQL query ran on `orders`
  - תת-נקודה: הדרום יציב
- נקודה שנייה: המרכז בירידה קלה

> הערה: הנתונים כוללים רק הזמנות שהושלמו, ללא ביטולים או החזרות כספיות."""

Q2 = "KPI dashboard: תבנה לי לוח עם ההכנסות, מספר ההזמנות והלקוחות החדשים לפי חודש, כולל השוואה לשנה שעברה."
A2 = """בניתי לוח מחוונים עם שלושה מדדים מרכזיים:

- **Revenue** לפי חודש, עם קו השוואה לשנה שעברה
- מספר ההזמנות לפי חודש
- לקוחות חדשים לפי ערוץ הגעה

`orders` היא הטבלה המרכזית, והיא מכילה את כל ההזמנות מאז 2019."""

Q3 = "Show revenue by region for Q3 and explain the main drivers."
A3 = """Revenue grew **12%** in Q3. The `orders` table includes the column סטטוס for each order.

## Summary

- North: 1.2M USD
- South: 0.9M USD
- Online sales were flat this quarter compared to last year.

1. Open the report
   - Click **Share**
   - בחר משתמשים
2. Send the invite"""

NOTE = """**סיכום פגישה** עם צוות המכירות:

- היעד לרבעון הבא: צמיחה של 10%
- **Pipeline** של 3.4M USD
- לבדוק את נתוני הדרום מול הצפון"""

GRID = {
    "rows": [
        {"region": "צפון", "customer": "חברת אלפא בע״מ", "status": "פעיל", "amount": 1200},
        {"region": "דרום", "customer": "בטא תעשיות", "status": "לא פעיל", "amount": 900},
        {"region": "מרכז", "customer": "גמא שירותים (סניף ראשי)", "status": "פעיל", "amount": 700},
    ],
    "columns": [{"field": "region"}, {"field": "customer"}, {"field": "status"}, {"field": "amount"}],
}

# The data card re-runs its step on view, so the code must return the same
# Hebrew rows.
GRID_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({
        "region": ["צפון", "דרום", "מרכז"],
        "customer": ["חברת אלפא בע״מ", "בטא תעשיות", "גמא שירותים (סניף ראשי)"],
        "status": ["פעיל", "לא פעיל", "פעיל"],
        "amount": [1200, 900, 700],
    })
"""

ARTIFACT_CODE = """<script type="text/babel">
function App() {
  const rows = [
    { region: 'צפון', revenue: '1.2M', change: '+14%' },
    { region: 'דרום', revenue: '0.9M', change: '+6%' },
    { region: 'מרכז', revenue: '0.7M', change: '-2%' },
  ];
  return (
    <div className="min-h-full bg-slate-50 p-8">
      <h1 className="text-2xl font-bold text-slate-800 mb-1">הכנסות לפי אזור</h1>
      <p className="text-sm text-slate-500 mb-6">הרבעון השלישי בהשוואה לרבעון המקביל אשתקד</p>
      <div className="flex gap-4 mb-6">
        <div className="bg-white rounded-lg shadow p-4 w-48"><div className="text-xs text-slate-500">סך הכנסות</div><div className="text-xl font-semibold">2.8M</div></div>
        <div className="bg-white rounded-lg shadow p-4 w-48"><div className="text-xs text-slate-500">שינוי שנתי</div><div className="text-xl font-semibold">+12%</div></div>
      </div>
      <table className="bg-white rounded-lg shadow text-sm w-96">
        <thead><tr className="text-start text-slate-500"><th className="px-4 py-2 text-start">אזור</th><th className="px-4 py-2 text-start">הכנסות</th><th className="px-4 py-2 text-start">שינוי</th></tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={i} className="border-t border-slate-100"><td className="px-4 py-2 font-medium">{r.region}</td><td className="px-4 py-2">{r.revenue}</td><td className="px-4 py-2">{r.change}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""


async def seed_turns(report_id):
    import main  # noqa: F401
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.plan_decision import PlanDecision
    from app.models.report import Report
    from app.models.query import Query
    from app.models.step import Step
    from app.models.tool_execution import ToolExecution
    from app.models.widget import Widget

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        t0 = datetime.utcnow() - timedelta(hours=1)
        sfx = report_id[:8]
        widget = Widget(title="הכנסות לפי אזור", slug=f"w-rtl-{sfx}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title="הכנסות לפי אזור", report_id=report_id, widget_id=widget.id,
                      organization_id=report.organization_id, user_id=report.user_id)
        db.add(query)
        await db.flush()
        step = Step(title="הכנסות לפי אזור", slug=f"step-rtl-{sfx}", status="success", widget_id=widget.id,
                    query_id=query.id, code=GRID_CODE, data=GRID, data_model={"type": "table"})
        db.add(step)
        await db.flush()
        query.default_step_id = step.id

        async def turn(i, question, answer, tools=()):
            ts = t0 + timedelta(minutes=10 * i)
            u = Completion(report_id=report_id, user_id=report.user_id, role="user", message_type="table",
                           prompt={"content": question}, completion={}, status="success", created_at=ts, turn_index=i)
            db.add(u)
            await db.flush()
            s = Completion(report_id=report_id, role="system", parent_id=u.id, prompt=None,
                           completion={"content": answer}, status="success",
                           created_at=ts + timedelta(seconds=5), turn_index=i)
            db.add(s)
            await db.flush()
            ae = AgentExecution(completion_id=s.id, status="completed", organization_id=report.organization_id,
                                user_id=report.user_id, report_id=report_id)
            db.add(ae)
            await db.flush()
            pd = PlanDecision(agent_execution_id=ae.id, seq=0, reasoning="", assistant="")
            db.add(pd)
            await db.flush()
            idx = 0
            for tool_name, args, result in tools:
                te = ToolExecution(agent_execution_id=ae.id, plan_decision_id=pd.id, tool_name=tool_name,
                                   arguments_json=args, status="success", success=True, duration_ms=1200,
                                   result_json=result, result_summary="ok",
                                   created_step_id=step.id if tool_name == "create_data" else None,
                                   created_widget_id=widget.id if tool_name == "create_data" else None)
                db.add(te)
                await db.flush()
                db.add(CompletionBlock(completion_id=s.id, agent_execution_id=ae.id, source_type="tool",
                                       tool_execution_id=te.id, plan_decision_id=pd.id, block_index=idx,
                                       title=tool_name, status="completed"))
                idx += 1
            db.add(CompletionBlock(completion_id=s.id, agent_execution_id=ae.id, source_type="final",
                                   block_index=idx, title="Answer", status="completed", content=answer))

        await turn(0, Q1, A1, tools=[
            ("create_data", {"title": "הכנסות לפי אזור", "visualization_type": "table"},
             {"title": "הכנסות לפי אזור", "data": GRID, "view": {"type": "table"}}),
            ("create_note", {"title": "סיכום פגישת מכירות", "content": NOTE},
             {"title": "סיכום פגישת מכירות"}),
        ])
        await turn(1, Q2, A2)
        await turn(2, Q3, A3)
        await db.commit()


def main():
    client, headers, report = base.api_setup()
    client.put(f"/api/reports/{report['id']}", headers=headers, json={"title": "הכנסות רבעוניות לפי אזור"})
    r = client.post("/api/artifacts", headers=headers, json={
        "report_id": report["id"], "title": "הכנסות לפי אזור", "mode": "page",
        "content": {"code": ARTIFACT_CODE, "visualization_ids": []}})
    r.raise_for_status()
    asyncio.run(seed_turns(report["id"]))
    print(json.dumps({"report_id": report["id"], "artifact_id": r.json()["id"]}))


if __name__ == "__main__":
    main()
