"""
Query management surface used by the artifact panel's Data modal:

- GET /api/queries must report the query's real last run (`last_run`):
  the latest executed step whether it succeeded or failed. `default_step`
  only repoints on success, so a failing latest run would otherwise stay
  hidden behind the last good one.
- DELETE /api/queries/{id} removes the query from every read path.
- POST /api/artifacts/report/{id}/add-visualization and
  .../remove-visualization attach/detach a query's visualization to the
  dashboard artifact; each creates a new version, and the artifact-scoped
  queries listing follows.

Run:
    cd backend
    uv run pytest tests/e2e/test_query_manage.py -v
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.artifact import Artifact
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.widget import Widget


def _run(coro):
    return asyncio.run(coro)


async def _seed_query(report_id, *, failing_rerun: bool, data_model=None):
    """Seed a query whose default step succeeded; optionally add a newer
    failed run on top — the exact state where default_step lies about the
    last outcome."""
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)

        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()

        query = Query(
            title=f"Q {suffix}",
            report_id=report_id,
            widget_id=widget.id,
            organization_id=report.organization_id,
            user_id=report.user_id,
        )
        db.add(query)
        await db.flush()

        ok_step = Step(
            title="ok",
            slug=f"ok-{suffix}",
            status="success",
            widget_id=widget.id,
            query_id=query.id,
            data={"rows": [{"n": 1}], "columns": [{"field": "n"}]},
            data_model=data_model or {"type": "table"},
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
        )
        db.add(ok_step)
        await db.flush()
        query.default_step_id = ok_step.id

        viz = Visualization(
            title=f"Viz {suffix}",
            status="success",
            report_id=report_id,
            query_id=query.id,
            view={"type": "table"},
        )
        db.add(viz)
        await db.flush()

        if failing_rerun:
            bad_step = Step(
                title="bad",
                slug=f"bad-{suffix}",
                status="error",
                status_reason="relation 'orders' does not exist",
                widget_id=widget.id,
                query_id=query.id,
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=5),
            )
            db.add(bad_step)

        await db.commit()
        return str(query.id), str(viz.id)


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.mark.e2e
def test_last_run_reflects_latest_failure_not_default_step(
    create_report, create_user, login_user, whoami, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Data modal", user_token=token, org_id=org_id)

    failing_id, _ = _run(_seed_query(report["id"], failing_rerun=True))
    healthy_id, _ = _run(_seed_query(report["id"], failing_rerun=False))

    response = test_client.get(
        f"/api/queries?report_id={report['id']}", headers=_auth(token, org_id)
    )
    assert response.status_code == 200, response.json()
    by_id = {q["id"]: q for q in response.json()}

    failing = by_id[failing_id]
    assert failing["last_run"] is not None
    assert failing["last_run"]["status"] == "error"
    assert failing["last_run"]["status_reason"]
    assert failing["last_run"]["ran_at"]
    # default_step still points at the last success — that's its contract
    assert failing["default_step"]["status"] == "success"

    healthy = by_id[healthy_id]
    assert healthy["last_run"] is not None
    assert healthy["last_run"]["status"] == "success"


@pytest.mark.e2e
def test_delete_query_removes_it_from_all_reads(
    create_report, create_user, login_user, whoami, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Data modal", user_token=token, org_id=org_id)
    query_id, _ = _run(_seed_query(report["id"], failing_rerun=False))
    headers = _auth(token, org_id)

    response = test_client.delete(f"/api/queries/{query_id}", headers=headers)
    assert response.status_code == 200, response.json()

    listed = test_client.get(
        f"/api/queries?report_id={report['id']}", headers=headers
    )
    assert listed.status_code == 200
    assert query_id not in [q["id"] for q in listed.json()]

    fetched = test_client.get(f"/api/queries/{query_id}", headers=headers)
    assert fetched.status_code == 404

    # Deleting twice is a 404, not a crash
    again = test_client.delete(f"/api/queries/{query_id}", headers=headers)
    assert again.status_code == 404


@pytest.mark.e2e
def test_attach_and_detach_visualization_on_dashboard(
    create_report, create_user, login_user, whoami, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Data modal", user_token=token, org_id=org_id)
    headers = _auth(token, org_id)

    # q1 is a CHART: its injected section carries an inner `})();` (the
    # option IIFE), the exact shape that used to break block removal
    bar_dm = {
        "type": "bar_chart",
        "columns": [{"generated_column_name": "artist"}, {"generated_column_name": "total"}],
        "series": [{"name": "s", "key": "artist", "value": "total"}],
    }
    q1_id, v1_id = _run(_seed_query(report["id"], failing_rerun=False, data_model=bar_dm))
    q2_id, v2_id = _run(_seed_query(report["id"], failing_rerun=False))

    # Attach both — second call versions on top of the first
    r1 = test_client.post(
        f"/api/artifacts/report/{report['id']}/add-visualization",
        json={"visualization_id": v1_id}, headers=headers,
    )
    assert r1.status_code == 200, r1.json()
    r2 = test_client.post(
        f"/api/artifacts/report/{report['id']}/add-visualization",
        json={"visualization_id": v2_id}, headers=headers,
    )
    assert r2.status_code == 200, r2.json()
    artifact = r2.json()
    assert set(artifact["content"]["visualization_ids"]) == {v1_id, v2_id}

    # The artifact-scoped queries listing shows both
    listed = test_client.get(
        f"/api/queries?report_id={report['id']}&artifact_id={artifact['id']}",
        headers=headers,
    )
    assert {q["id"] for q in listed.json()} == {q1_id, q2_id}

    # Detach the first — new version, id gone, code no longer references it
    r3 = test_client.post(
        f"/api/artifacts/report/{report['id']}/remove-visualization",
        json={"visualization_id": v1_id}, headers=headers,
    )
    assert r3.status_code == 200, r3.json()
    detached = r3.json()
    assert detached["content"]["visualization_ids"] == [v2_id]
    assert detached["version"] == artifact["version"] + 1
    # v1 is no longer BOUND anywhere (a tagged `removed:` stub may remain so
    # a re-add can restore the card in place)
    detached_code = detached["content"]["code"]
    assert f'vizById("{v1_id}")' not in detached_code
    assert v2_id in detached_code
    # The chart block was stripped WHOLE — balanced code, no orphaned tail
    assert detached_code.count("{") == detached_code.count("}"), "unbalanced braces after removal"
    assert detached_code.count("(") == detached_code.count(")"), "unbalanced parens after removal"

    listed = test_client.get(
        f"/api/queries?report_id={report['id']}&artifact_id={detached['id']}",
        headers=headers,
    )
    assert {q["id"] for q in listed.json()} == {q2_id}

    # Detaching something not on the dashboard is a 404
    again = test_client.post(
        f"/api/artifacts/report/{report['id']}/remove-visualization",
        json={"visualization_id": v1_id}, headers=headers,
    )
    assert again.status_code == 404


async def _seed_authored_artifact(report_id, viz_id, mode="page"):
    """An artifact whose section is AI-authored (bare vizById reference, no
    programmatic-injection marker) — the case where a remove cannot delete
    the section and must stub it."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        artifact = Artifact(
            report_id=report_id,
            user_id=report.user_id,
            organization_id=report.organization_id,
            title="Authored dash",
            mode=mode,
            content={
                "code": (
                    '<script type="text/babel">\n'
                    "function App() {\n"
                    f'  const v = vizById("{viz_id}");\n'
                    '  return <SectionCard title="Critical" viz={vizById("' + viz_id + '")} />;\n'
                    "}\n"
                    "</script>"
                ),
                "visualization_ids": [viz_id],
            },
            version=1,
            status="completed",
        )
        db.add(artifact)
        await db.commit()
        return str(artifact.id)


@pytest.mark.e2e
def test_remove_then_readd_restores_the_original_card(
    create_report, create_user, login_user, whoami, test_client,
):
    """Removing an AI-authored card stubs it in place; re-adding the query
    restores the original binding instead of appending a duplicate."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Data modal", user_token=token, org_id=org_id)
    headers = _auth(token, org_id)

    _q_id, viz_id = _run(_seed_query(report["id"], failing_rerun=False))
    artifact_id = _run(_seed_authored_artifact(report["id"], viz_id))

    removed = test_client.post(
        f"/api/artifacts/report/{report['id']}/remove-visualization",
        json={"visualization_id": viz_id, "artifact_id": artifact_id},
        headers=headers,
    )
    assert removed.status_code == 200, removed.json()
    removed_code = removed.json()["content"]["code"]
    assert removed.json()["content"]["visualization_ids"] == []
    # Section survives, disconnected but tagged for restoration
    assert 'vizById("' + viz_id + '")' not in removed_code
    assert f"removed:{viz_id}" in removed_code

    readded = test_client.post(
        f"/api/artifacts/report/{report['id']}/add-visualization",
        json={"visualization_id": viz_id, "artifact_id": removed.json()["id"]},
        headers=headers,
    )
    assert readded.status_code == 200, readded.json()
    readded_code = readded.json()["content"]["code"]
    assert readded.json()["content"]["visualization_ids"] == [viz_id]
    # The original card is rebound in place — no duplicate appended section
    assert 'vizById("' + viz_id + '")' in readded_code
    assert f"removed:{viz_id}" not in readded_code
    assert "Programmatically added" not in readded_code


@pytest.mark.e2e
def test_add_targets_the_named_artifact_not_the_newest(
    create_report, create_user, login_user, whoami, test_client,
):
    """With artifact_id in the body, add builds on that artifact even when a
    newer artifact exists in the report."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Data modal", user_token=token, org_id=org_id)
    headers = _auth(token, org_id)

    _q1, v1_id = _run(_seed_query(report["id"], failing_rerun=False))
    _q2, v2_id = _run(_seed_query(report["id"], failing_rerun=False))

    older_id = _run(_seed_authored_artifact(report["id"], v1_id))
    _newer_id = _run(_seed_authored_artifact(report["id"], v2_id))

    added = test_client.post(
        f"/api/artifacts/report/{report['id']}/add-visualization",
        json={"visualization_id": v2_id, "artifact_id": older_id},
        headers=headers,
    )
    assert added.status_code == 200, added.json()
    # Built on the OLDER artifact: both vizs present, v1 binding intact
    assert set(added.json()["content"]["visualization_ids"]) == {v1_id, v2_id}
    assert 'vizById("' + v1_id + '")' in added.json()["content"]["code"]

    # An artifact from another report is rejected
    other = create_report(title="Other", user_token=token, org_id=org_id)
    rejected = test_client.post(
        f"/api/artifacts/report/{other['id']}/add-visualization",
        json={"visualization_id": v2_id, "artifact_id": older_id},
        headers=headers,
    )
    assert rejected.status_code in (400, 404)

    # A doc artifact is never a valid target — versioning it through the
    # dashboard code paths would wipe its markdown
    doc_id = _run(_seed_authored_artifact(report["id"], v1_id, mode="doc"))
    doc_rejected = test_client.post(
        f"/api/artifacts/report/{report['id']}/remove-visualization",
        json={"visualization_id": v1_id, "artifact_id": doc_id},
        headers=headers,
    )
    assert doc_rejected.status_code == 400

    # The new version numbers past the report-wide max — not base.version+1 —
    # so building on an older artifact cannot mint a duplicate number
    assert added.json()["version"] == 2
