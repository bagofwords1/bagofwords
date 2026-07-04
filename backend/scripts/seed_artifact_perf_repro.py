"""Seed two public reports (small / large datasets) with a REAL renderable
artifact for the artifact-large-data perf repro.

Each report gets:
  - 4 queries, each with 3 step versions (only the last is the default);
    every step version stores the full result rows in steps.data
  - 4 visualizations (bar / line / area / pie)
  - 1 artifact whose code is generated with the app's own codegen
    (app/services/artifact_codegen.py) — the same code path the
    "add visualization to dashboard" endpoint uses, so the public page
    renders real charts from window.ARTIFACT_DATA.

Usage:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python scripts/seed_artifact_perf_repro.py
    # rows per step version for the LARGE report (default 15000):
    BOW_REPRO_ROWS=30000 ... scripts/seed_artifact_perf_repro.py

Prints the public URLs (/r/{id}) for both reports.
"""
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the app so the full SQLAlchemy model registry is loaded (string-based
# relationships like "ApiKey" fail to resolve otherwise). Same approach as
# tests/conftest.py. NB: don't sweep app/models/* blindly — application.py
# references a class that no longer exists.
import main  # noqa: E402,F401

from app.dependencies import async_session_maker  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.report import Report  # noqa: E402
from app.models.widget import Widget  # noqa: E402
from app.models.query import Query  # noqa: E402
from app.models.step import Step  # noqa: E402
from app.models.visualization import Visualization  # noqa: E402
from app.models.artifact import Artifact  # noqa: E402
from app.services.artifact_codegen import (  # noqa: E402
    generate_echart_option_code,
    generate_section_jsx,
    generate_scaffold,
)

ROWS_LARGE = int(os.environ.get("BOW_REPRO_ROWS", "15000"))
ROWS_SMALL = 50
N_STEP_VERSIONS = 3

# (title, data_model) per query — all supported by artifact_codegen
VIZ_SPECS = [
    ("Revenue by Region", {"type": "bar_chart", "series": [
        {"key": "region", "value": "revenue", "name": "Revenue"}]}),
    ("Revenue over Time", {"type": "line_chart", "series": [
        {"key": "order_month", "value": "revenue", "name": "Revenue"}]}),
    ("Quantity over Time", {"type": "area_chart", "series": [
        {"key": "order_month", "value": "quantity", "name": "Quantity"}]}),
    ("Revenue Share by Region", {"type": "pie_chart", "series": [
        {"key": "region", "value": "revenue", "name": "Revenue"}]}),
]


def _make_rows(n):
    return [
        {
            "order_id": i,
            "customer_name": f"customer_{i % 997}",
            "region": ("North", "South", "East", "West")[i % 4],
            "product": f"product_{i % 251}",
            "quantity": (i * 7) % 40 + 1,
            "unit_price": round(3.5 + (i % 900) * 0.13, 2),
            "revenue": round(((i * 7) % 40 + 1) * (3.5 + (i % 900) * 0.13), 2),
            "order_month": f"2025-{(i % 12) + 1:02d}",
        }
        for i in range(n)
    ]


async def seed_report(db, org, user, label: str, rows_per_step: int) -> dict:
    suffix = uuid.uuid4().hex[:8]
    columns = [{"field": f, "headerName": f.replace("_", " ").title()}
               for f in _make_rows(1)[0].keys()]

    report = Report(
        title=f"Perf {label} {suffix}",
        slug=f"perf-{label}-{suffix}",
        status="published",
        artifact_visibility="public",
        user_id=user.id,
        organization_id=org.id,
    )
    db.add(report)
    await db.flush()

    viz_ids, sections = [], []
    for qi, (title, data_model) in enumerate(VIZ_SPECS):
        widget = Widget(title=f"W{qi} {suffix}", slug=f"w{qi}-{label}-{suffix}",
                        report_id=report.id)
        db.add(widget)
        await db.flush()

        query = Query(title=title, report_id=report.id, widget_id=widget.id,
                      organization_id=org.id, user_id=user.id)
        db.add(query)
        await db.flush()

        last_step = None
        for si in range(N_STEP_VERSIONS):
            step = Step(
                title=f"{title} v{si + 1}",
                slug=f"s{qi}-{si}-{label}-{suffix}",
                status="success",
                widget_id=widget.id,
                query_id=query.id,
                data={"rows": _make_rows(rows_per_step), "columns": columns},
                data_model=data_model,
                view={"type": data_model["type"]},
            )
            db.add(step)
            last_step = step
        await db.flush()
        query.default_step_id = last_step.id

        viz = Visualization(title=title, status="success", report_id=report.id,
                            query_id=query.id, view={"type": data_model["type"]})
        db.add(viz)
        await db.flush()
        viz_ids.append(str(viz.id))

        option_code = generate_echart_option_code(data_model, qi)
        sections.append(generate_section_jsx(title, option_code, viz_index=qi))

    artifact = Artifact(
        report_id=report.id,
        user_id=user.id,
        organization_id=org.id,
        title=f"Perf Dashboard ({label})",
        mode="page",
        version=1,
        content={"code": generate_scaffold(sections), "visualization_ids": viz_ids},
        status="completed",
    )
    db.add(artifact)
    await db.flush()

    step_bytes = len(json.dumps({"rows": _make_rows(rows_per_step), "columns": columns}))
    return {
        "label": label,
        "report_id": str(report.id),
        "artifact_id": str(artifact.id),
        "rows_per_step": rows_per_step,
        "step_mb": round(step_bytes / 1e6, 2),
        "stored_mb": round(step_bytes * len(VIZ_SPECS) * N_STEP_VERSIONS / 1e6, 1),
    }


async def main():
    async with async_session_maker() as db:
        suffix = uuid.uuid4().hex[:8]
        org = Organization(name=f"Perf Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name="Perf User", email=f"perf-{suffix}@example.com",
                    hashed_password="x", is_active=True, is_superuser=False,
                    is_verified=True)
        db.add(user)
        await db.flush()

        small = await seed_report(db, org, user, "small", ROWS_SMALL)
        large = await seed_report(db, org, user, "large", ROWS_LARGE)
        await db.commit()

    for info in (small, large):
        print(f"[seed] {info['label']:5} report: /r/{info['report_id']} "
              f"(artifact {info['artifact_id']}, {info['rows_per_step']} rows/step, "
              f"{info['step_mb']} MB/step, {info['stored_mb']} MB stored across versions)")
    print(f"SMALL_REPORT_ID={small['report_id']}")
    print(f"LARGE_REPORT_ID={large['report_id']}")


if __name__ == "__main__":
    asyncio.run(main())
