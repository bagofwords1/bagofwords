"""Reusing a saved catalog entity keeps the line shape it was saved with.

describe_entity rebuilds the view from the entity's data model when it
materializes the entity into a new step. New line/area views default to
straight lines, so the rebuild must carry the saved `smooth` across, or a saved
curved chart would come back straight (and vice versa).

Run:
    cd backend && uv run pytest tests/e2e/test_describe_entity_view_shape.py -v
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.widget import Widget

CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame([{"month": "2025-01", "total": 10}, {"month": "2025-02", "total": 14}])
"""
ROWS = {"rows": [{"month": "2025-01", "total": 10}, {"month": "2025-02", "total": 14}],
        "columns": [{"field": "month"}, {"field": "total"}]}


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    info = whoami(token)
    return {"token": token, "org_id": info["organizations"][0]["id"], "user_id": info["id"]}


async def _seed_chart_step(report_id, chart_type, smooth, axis_x=None, extra=None):
    """A finished chart step as the agent leaves it (no public API creates steps;
    the same seeding as tests/e2e/rbac/test_rbac_entity_creation.py)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(title="Q", report_id=report_id, widget_id=widget.id,
                      organization_id=report.organization_id, user_id=report.user_id)
        db.add(query)
        await db.flush()
        step = Step(
            title=f"Monthly total {suffix}", slug=f"step-{suffix}", status="success",
            widget_id=widget.id, query_id=query.id, code=CODE, data=ROWS,
            data_model={"type": chart_type, "series": [{"name": "Total", "key": "month", "value": "total"}]},
            view={"version": "v2", "view": {"type": chart_type, "x": "month", "y": "total", "smooth": smooth,
                                            **({"axisX": axis_x} if axis_x else {}), **(extra or {})}},
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()
        return str(step.id)


async def _materialize(org_id, user_id, name):
    from app.ai.tools.implementations.describe_entity import DescribeEntityTool

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        settings = await org.get_settings(db)
        last = None
        async for evt in DescribeEntityTool().run_stream(
            {"name_or_id": name, "should_create": True},
            {"db": db, "organization": org, "user": user, "settings": settings},
        ):
            last = evt
        return last.payload


def _reuse_saved_chart(test_client, admin, chart_type, smooth, axis_x=None, extra=None):
    h = _hdr(admin["token"], admin["org_id"])
    resp = test_client.post("/api/reports", json={"title": f"r_{uuid.uuid4().hex[:6]}", "data_sources": []}, headers=h)
    assert resp.status_code == 200, resp.text
    step_id = asyncio.run(_seed_chart_step(resp.json()["id"], chart_type, smooth, axis_x, extra))

    title = f"Monthly total {uuid.uuid4().hex[:6]}"
    resp = test_client.post(f"/api/entities/from_step/{step_id}",
                            json={"type": "model", "title": title, "publish": True, "data_source_ids": []}, headers=h)
    assert resp.status_code == 200, resp.text

    payload = asyncio.run(_materialize(admin["org_id"], admin["user_id"], title))
    out, obs = payload["output"], payload["observation"]
    assert out["success"], out.get("errors")
    view = obs["view"]
    return view.get("view", view)


@pytest.mark.e2e
@pytest.mark.parametrize("chart_type", ["line_chart", "area_chart"])
@pytest.mark.parametrize("smooth", [True, False])
def test_reused_entity_keeps_its_saved_line_shape(test_client, admin, chart_type, smooth):
    inner = _reuse_saved_chart(test_client, admin, chart_type, smooth)
    assert inner["type"] == chart_type
    assert inner["smooth"] is smooth


@pytest.mark.e2e
@pytest.mark.parametrize("chart_type", ["line_chart", "bar_chart"])
@pytest.mark.parametrize("stored,expected", [
    ({"show": True, "rotate": 45, "interval": 0}, (45, 0)),
    ({"show": True, "rotate": 90, "interval": 3}, (90, 3)),
    (None, (45, 0)),  # saved before labels could be automatic: drawn with every label
    ({"show": True}, (None, None)),  # saved as automatic: stays automatic
])
def test_reused_entity_keeps_its_saved_labels(test_client, admin, chart_type, stored, expected):
    axis = _reuse_saved_chart(test_client, admin, chart_type, False, axis_x=stored)["axisX"]
    assert (axis.get("rotate"), axis.get("interval")) == expected


@pytest.mark.e2e
@pytest.mark.parametrize("chart_type", ["line_chart", "bar_chart"])
def test_reused_entity_keeps_its_units_and_zoom(test_client, admin, chart_type):
    inner = _reuse_saved_chart(test_client, admin, chart_type, False,
                               extra={"axisY": {"show": True, "format": "currency", "currency": "EUR"}, "showDataZoom": True})
    assert (inner["axisY"].get("format"), inner["axisY"].get("currency")) == ("currency", "EUR")
    assert inner.get("showDataZoom") is True
