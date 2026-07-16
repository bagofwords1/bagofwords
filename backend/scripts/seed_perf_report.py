"""Seed a report whose recent turns created large-dataset steps, into the
running stack's DB, so the report page can be opened in a browser to verify the
preview-then-full step-data loading. Prints the report id.

Usage (from backend/, with the running stack's env):
    TESTING=true TEST_DATABASE_URL=sqlite:///db/agent.db \
      uv run python scripts/seed_perf_report.py <org_id> <user_id> [rows]
"""
import asyncio
import importlib
import pkgutil
import sys
import uuid

# Import the full app so every model/mapper (incl. EE) is registered.
import main  # noqa: F401

from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
from app.models.completion_block import CompletionBlock

ORG_ID = sys.argv[1]
USER_ID = sys.argv[2]
ROWS = int(sys.argv[3]) if len(sys.argv) > 3 else 12000
N = 3


def make_rows(n):
    return [
        {
            "order_id": i,
            "customer_name": f"customer_{i % 997}",
            "region": ("north", "south", "east", "west")[i % 4],
            "revenue": round(((i * 7) % 40 + 1) * (3.5 + (i % 900) * 0.13), 2),
        }
        for i in range(n)
    ]


async def main():
    suffix = uuid.uuid4().hex[:8]
    columns = [{"field": f} for f in make_rows(1)[0].keys()]
    async with async_session_maker() as db:
        report = Report(
            title=f"Perf Preview Report {suffix}",
            slug=f"perf-preview-{suffix}",
            status="draft",
            user_id=USER_ID,
            organization_id=ORG_ID,
        )
        db.add(report)
        await db.flush()

        for ci in range(N):
            widget = Widget(title=f"Revenue by region {ci}", slug=f"w{ci}-{suffix}", report_id=report.id)
            db.add(widget)
            await db.flush()
            query = Query(title=f"Q{ci}", report_id=report.id, widget_id=widget.id,
                          organization_id=ORG_ID, user_id=USER_ID)
            db.add(query)
            await db.flush()
            step = Step(
                title=f"Revenue by region {ci}", slug=f"s{ci}-{suffix}", status="success",
                widget_id=widget.id, query_id=query.id,
                data={"rows": make_rows(ROWS), "columns": columns},
                data_model={"type": "table", "columns": columns},
                view={"type": "table"},
            )
            db.add(step)
            await db.flush()
            query.default_step_id = step.id

            completion = Completion(
                prompt={"content": f"show revenue by region {ci}"},
                completion={"content": f"Here is the data for turn {ci}."},
                status="success", role="system", report_id=report.id, user_id=USER_ID, turn_index=ci,
            )
            db.add(completion)
            await db.flush()
            ae = AgentExecution(completion_id=completion.id, organization_id=ORG_ID,
                                user_id=USER_ID, report_id=report.id, status="completed")
            db.add(ae)
            await db.flush()
            te = ToolExecution(
                agent_execution_id=ae.id, tool_name="create_data", status="success", success=True,
                arguments_json={}, result_json={"code": "SELECT ..."},
                created_widget_id=widget.id, created_step_id=step.id,
            )
            db.add(te)
            await db.flush()
            db.add(CompletionBlock(
                completion_id=completion.id, agent_execution_id=ae.id, source_type="tool",
                tool_execution_id=te.id, block_index=0, title="create_data", status="completed",
            ))
        await db.commit()
        print(str(report.id))


asyncio.run(main())
