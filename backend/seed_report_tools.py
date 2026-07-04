"""Seed a real report conversation with CreateData tool executions (table + chart)
so the report chat renders them the way the agent would. Raw SQL — no ORM mappers.

Run from backend/:  python3 seed_report_tools.py
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta

DB = "db/app.db"


def main():
    c = sqlite3.connect(DB)
    org = c.execute("SELECT id FROM organizations LIMIT 1").fetchone()[0]
    user = c.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    report = c.execute(
        "SELECT id FROM reports WHERE artifact_visibility='public' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]

    t0 = datetime.utcnow()
    now = t0.isoformat(sep=" ")

    def ins(table, **cols):
        keys = ",".join(cols)
        qs = ",".join("?" * len(cols))
        c.execute(f"INSERT INTO {table} ({keys}) VALUES ({qs})", tuple(cols.values()))

    uid = lambda: str(uuid.uuid4())

    # ---- wide table data ----
    tcols = ['region', 'month', 'revenue', 'orders', 'aov', 'growth_pct', 'margin_pct', 'refunds']
    regions = ['North America', 'Europe', 'APAC', 'LATAM', 'Middle East & Africa']
    trows = [{
        'region': regions[i % 5], 'month': f'2026-{(i % 12) + 1:02d}',
        'revenue': 100000 + i * 8123, 'orders': 1200 + i * 47,
        'aov': round(80 + i * 1.7, 2), 'growth_pct': ((i % 5) - 2) * 3.4,
        'margin_pct': round(18 + (i % 7), 1), 'refunds': 12 + (i % 9),
    } for i in range(12)]
    table_columns = [{'field': f, 'headerName': f} for f in tcols]

    # ---- bar chart data (revenue by region) ----
    ccols = [{'field': 'region', 'headerName': 'region'}, {'field': 'revenue', 'headerName': 'revenue'}]
    crows = [{'region': r, 'revenue': 820000 - i * 150000} for i, r in enumerate(regions)]

    sql_table = ("SELECT region, month, SUM(revenue) AS revenue, COUNT(*) AS orders,\n"
                 "       AVG(order_value) AS aov, growth_pct, margin_pct, refunds\n"
                 "FROM sales GROUP BY region, month ORDER BY revenue DESC")
    sql_chart = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region ORDER BY revenue DESC"

    # ---- user turn ----
    user_c = uid()
    ins("completions", id=user_c, prompt=json.dumps({"content": "מה המכירות לפי קטגוריה וגם לפי אזור?", "mode": "chat"}),
        completion=json.dumps(""), status="success", model="claude", turn_index=0, role="user",
        message_type="user", main_router="table", report_id=report, user_id=user,
        created_at=now, updated_at=now)

    # ---- assistant turn ----
    sys_c = uid()
    ae = uid()
    ins("completions", id=sys_c, prompt=json.dumps({}),
        completion=json.dumps({"content": "הנה המכירות: טבלה מפורטת לפי חודש/אזור, ותרשים מכירות לפי אזור."}),
        status="success", model="claude", turn_index=1, role="system", message_type="ai_completion", main_router="table",
        report_id=report, user_id=user,
        created_at=now, updated_at=now)
    ins("agent_executions", id=ae, completion_id=sys_c, organization_id=org, user_id=user,
        report_id=report, status="completed", started_at=now, completed_at=now,
        total_duration_ms=5200, latest_seq=2, created_at=now, updated_at=now)

    # ---- query + steps + visualization ----
    query_id = uid()
    step_table = uid()
    step_chart = uid()
    viz_id = uid()

    ins("steps", id=step_table, title="מכירות לפי חודש ואזור", slug="s-" + uuid.uuid4().hex[:8],
        status="success", prompt="", code=sql_table, data=json.dumps({"columns": table_columns, "rows": trows}),
        type="data", data_model=json.dumps({"type": "table"}), view=json.dumps({"type": "table"}),
        query_id=query_id, created_at=now, updated_at=now)
    ins("steps", id=step_chart, title="מכירות לפי אזור", slug="s-" + uuid.uuid4().hex[:8],
        status="success", prompt="", code=sql_chart, data=json.dumps({"columns": ccols, "rows": crows}),
        type="data",
        data_model=json.dumps({"type": "bar_chart", "series": [{"key": "region", "value": "revenue", "name": "Revenue"}]}),
        view=json.dumps({"view": {"type": "bar_chart", "x": "region", "y": "revenue"}, "version": "v2"}),
        query_id=query_id, created_at=now, updated_at=now)
    ins("visualizations", id=viz_id, title="מכירות לפי אזור", status="success", report_id=report,
        query_id=query_id, view=json.dumps({"view": {"type": "bar_chart", "x": "region", "y": "revenue"}, "version": "v2"}),
        created_at=now, updated_at=now)

    # ---- tool executions ----
    te_table = uid()
    te_chart = uid()
    ins("tool_executions", id=te_table, agent_execution_id=ae, tool_name="create_data",
        arguments_json=json.dumps({"title": "מכירות לפי חודש ואזור"}),
        result_json=json.dumps({"code": sql_table, "stats": {"total_rows": len(trows)},
                                "data_model": {"type": "table"}, "execution_ms": 820}),
        status="success", success=1, started_at=now, completed_at=now, duration_ms=3400,
        attempt_number=1, max_retries=3, created_step_id=step_table, created_at=now, updated_at=now)
    ins("tool_executions", id=te_chart, agent_execution_id=ae, tool_name="create_data",
        arguments_json=json.dumps({"title": "מכירות לפי אזור"}),
        result_json=json.dumps({"code": sql_chart, "stats": {"total_rows": len(crows)},
                                "data_model": {"type": "bar_chart"}, "execution_ms": 610}),
        status="success", success=1, started_at=now, completed_at=now, duration_ms=2600,
        attempt_number=1, max_retries=3, created_step_id=step_chart,
        artifact_refs_json=json.dumps({"visualizations": [viz_id]}), created_at=now, updated_at=now)

    # ---- completion blocks (belong to the assistant completion) ----
    ins("completion_blocks", id=uid(), completion_id=sys_c, agent_execution_id=ae, source_type="tool",
        tool_execution_id=te_table, block_index=0, loop_index=0, title="Create data",
        status="completed", icon="table", started_at=now, completed_at=now, duration_ms=3400,
        created_at=now, updated_at=now)
    ins("completion_blocks", id=uid(), completion_id=sys_c, agent_execution_id=ae, source_type="tool",
        tool_execution_id=te_chart, block_index=1, loop_index=0, title="Create data",
        status="completed", icon="chart", started_at=now, completed_at=now, duration_ms=2600,
        created_at=now, updated_at=now)

    c.commit()
    print("REPORT_ID", report)
    print("USER_COMPLETION", user_c)
    print("SYS_COMPLETION", sys_c)
    print("TE_TABLE", te_table, "TE_CHART", te_chart)


if __name__ == "__main__":
    main()
