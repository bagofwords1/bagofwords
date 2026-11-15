#!/usr/bin/env python3
"""Loop B: give a report one finished visualization, without an LLM.

A visualization's stored result is written by an agent run; this writes the
same rows directly so the loop needs no paid model.

    cd backend && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \
        uv run python ../docs/feedback-loops/2026-11-15-viz-id-resolution/seed_viz.py <report_id>

Prints the visualization id.
"""
import asyncio
import sys
import uuid

import main  # noqa: F401  (registers every SQLAlchemy mapper)
from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.widget import Widget

ROWS = [
    {"genre": "Rock", "revenue": 826.65}, {"genre": "Latin", "revenue": 382.14},
    {"genre": "Metal", "revenue": 261.36}, {"genre": "Alternative & Punk", "revenue": 241.56},
    {"genre": "TV Shows", "revenue": 93.53}, {"genre": "Jazz", "revenue": 79.2},
]


async def seed(report_id: str) -> str:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title="Revenue by genre", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title="Revenue by genre", report_id=report_id, widget_id=widget.id,
                      organization_id=report.organization_id, user_id=report.user_id)
        db.add(query)
        await db.flush()
        step = Step(title="Revenue by genre", slug=f"s-{suffix}", status="success",
                    widget_id=widget.id, query_id=query.id, code="",
                    data={"rows": ROWS, "columns": [{"field": "genre"}, {"field": "revenue"}]})
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(title="Revenue by genre", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "bar_chart"})
        db.add(viz)
        await db.commit()
        return str(viz.id)


if __name__ == "__main__":
    print(asyncio.run(seed(sys.argv[1])))
