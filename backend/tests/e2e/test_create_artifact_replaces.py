"""create_artifact + replaces_artifact_id — new artifact vs next version.

Decision D3: the tool's default is a NEW artifact (its own parent, v1, its
own row in the chat summary) even when the report already has one. Only an
explicit `replaces_artifact_id` — the planner passes it for "rebuild from
scratch" asks — makes the result the next VERSION of that artifact. A target
that doesn't match this report+mode falls back to a new artifact and says so
in the observation, so the planner is never silently misled.

Render validation is stubbed out (it needs Playwright + a repair LLM);
these tests are about which row the tool mints, not about rendering.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_create_artifact_replaces.py --db=sqlite
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
from app.models.visualization import Visualization
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


PAGE_CODE = '<script type="text/babel">function App() { return null }</script>'


def _make_report(create_report, create_user, login_user, whoami, title):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]
    report = create_report(title=title, user_token=user_token, org_id=org_id, data_sources=[])
    return report, user_token, org_id


async def _seed_viz(report_id: str):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(
            title="Q", report_id=report_id, widget_id=widget.id,
            organization_id=report.organization_id, user_id=report.user_id,
        )
        db.add(query)
        await db.flush()
        step = Step(
            title="S", slug=f"s-{suffix}", status="success",
            widget_id=widget.id, query_id=query.id, code="",
            data={"rows": [{"artist": "Queen", "revenue": 10}],
                  "columns": [{"field": "artist"}, {"field": "revenue"}]},
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(
            title="Viz", status="success", report_id=report_id,
            query_id=query.id, view={"type": "bar_chart"},
        )
        db.add(viz)
        await db.flush()
        await db.commit()
        return str(viz.id)


@pytest.fixture()
def stub_render_validation(monkeypatch):
    """Skip Playwright: report the authored code as rendering cleanly."""
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async def _clean(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": True, "screenshot": None,
               "errors": [], "repair_attempts": 0}

    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", _clean)


async def _run_create(report_id: str, tool_input: dict):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = await db.get(User, report.user_id)
        organization = await db.get(Organization, report.organization_id)
        runtime_ctx = {"db": db, "report": report, "user": user, "organization": organization}
        events = []
        async for evt in CreateArtifactTool().run_stream(tool_input, runtime_ctx):
            events.append(evt)
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, f"no tool.end event in {[e.type for e in events]}"
    return ends[-1].payload


async def _version_row(version_id: str):
    from app.services.artifact_service import ArtifactService
    async with async_session_maker() as db:
        row = await ArtifactService().get(db, version_id)
        assert row is not None
        return {"artifact_id": str(row.artifact_id), "version": row.version,
                "title": row.title, "mode": row.mode}


@pytest.mark.e2e
def test_without_replaces_every_call_starts_a_new_artifact(
    create_report, create_user, login_user, whoami, test_client, stub_render_validation
):
    report, _token, _org = _make_report(create_report, create_user, login_user, whoami, "D3 default")
    viz_id = _run(_seed_viz(report["id"]))

    base = {"prompt": "build", "mode": "page", "code": PAGE_CODE, "visualization_ids": [viz_id]}
    first = _run(_run_create(report["id"], {**base, "title": "Sales"}))
    second = _run(_run_create(report["id"], {**base, "title": "Inventory"}))
    assert first["output"].get("artifact_id"), first
    assert second["output"].get("artifact_id"), second

    a = _run(_version_row(first["output"]["artifact_id"]))
    b = _run(_version_row(second["output"]["artifact_id"]))
    assert a["version"] == 1 and b["version"] == 1
    assert a["artifact_id"] != b["artifact_id"], (
        "two create calls without replaces_artifact_id must mint two artifacts"
    )


@pytest.mark.e2e
def test_replaces_makes_the_next_version_of_that_artifact(
    create_report, create_user, login_user, whoami, test_client, stub_render_validation
):
    report, _token, _org = _make_report(create_report, create_user, login_user, whoami, "Rebuild")
    viz_id = _run(_seed_viz(report["id"]))

    base = {"prompt": "build", "mode": "page", "code": PAGE_CODE, "visualization_ids": [viz_id]}
    first = _run(_run_create(report["id"], {**base, "title": "Sales"}))
    v1_id = first["output"]["artifact_id"]

    rebuild = _run(_run_create(report["id"], {
        **base, "title": None, "replaces_artifact_id": v1_id,
    }))
    assert rebuild["output"].get("artifact_id"), rebuild

    v1 = _run(_version_row(v1_id))
    v2 = _run(_version_row(rebuild["output"]["artifact_id"]))
    assert v2["artifact_id"] == v1["artifact_id"], "the rebuild must join the same artifact"
    assert v2["version"] == 2
    assert v2["title"] == "Sales", "no title passed — the parent's title carries over"
    assert "v2" in rebuild["observation"]["summary"]


@pytest.mark.e2e
def test_mismatched_replaces_falls_back_to_a_new_artifact(
    create_report, create_user, login_user, whoami, test_client, stub_render_validation
):
    report_a, token, org_id = _make_report(create_report, create_user, login_user, whoami, "Report A")
    report_b = create_report(title="Report B", user_token=token, org_id=org_id, data_sources=[])
    viz_a = _run(_seed_viz(report_a["id"]))
    viz_b = _run(_seed_viz(report_b["id"]))

    first = _run(_run_create(report_a["id"], {
        "prompt": "build", "mode": "page", "code": PAGE_CODE,
        "visualization_ids": [viz_a], "title": "A's dashboard",
    }))
    foreign_version_id = first["output"]["artifact_id"]

    # Rebuild in report B naming report A's artifact: refused, falls back.
    fallback = _run(_run_create(report_b["id"], {
        "prompt": "build", "mode": "page", "code": PAGE_CODE,
        "visualization_ids": [viz_b], "title": "B's dashboard",
        "replaces_artifact_id": foreign_version_id,
    }))
    assert fallback["output"].get("artifact_id"), fallback

    foreign = _run(_version_row(foreign_version_id))
    minted = _run(_version_row(fallback["output"]["artifact_id"]))
    assert minted["artifact_id"] != foreign["artifact_id"]
    assert minted["version"] == 1
    assert "NEW artifact was created instead" in fallback["observation"]["summary"], (
        "the fallback must be stated in the observation so the planner is not misled"
    )


@pytest.mark.e2e
def test_duplicate_and_list_speak_artifact_id(
    create_report, create_user, login_user, whoami, test_client, stub_render_validation
):
    """REST layer: the list carries artifact_id, and "Use this version"
    (duplicate) stays inside the source's own chain at max+1."""
    report, token, org_id = _make_report(create_report, create_user, login_user, whoami, "REST")
    viz_id = _run(_seed_viz(report["id"]))
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    base = {"prompt": "build", "mode": "page", "code": PAGE_CODE, "visualization_ids": [viz_id]}
    dash_v1 = _run(_run_create(report["id"], {**base, "title": "Dash"}))["output"]["artifact_id"]
    dash_v2 = _run(_run_create(report["id"], {
        **base, "replaces_artifact_id": dash_v1,
    }))["output"]["artifact_id"]
    other_v1 = _run(_run_create(report["id"], {**base, "title": "Other"}))["output"]["artifact_id"]

    listed = test_client.get(f"/api/artifacts/report/{report['id']}", headers=headers)
    assert listed.status_code == 200, listed.text
    by_id = {a["id"]: a for a in listed.json()}
    assert all("artifact_id" in a for a in by_id.values())
    assert by_id[dash_v1]["artifact_id"] == by_id[dash_v2]["artifact_id"]
    assert by_id[other_v1]["artifact_id"] != by_id[dash_v1]["artifact_id"]

    # Revert to Dash v1: continues DASH's chain (v3), untouched by Other.
    reverted = test_client.post(f"/api/artifacts/{dash_v1}/duplicate", headers=headers)
    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    assert body["artifact_id"] == by_id[dash_v1]["artifact_id"]
    assert body["version"] == 3
