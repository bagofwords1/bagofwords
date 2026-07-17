"""Shared-artifact viewer runs (POST /r/{id}/run) — per-viewer step results.

Invariants under test:
- An authenticated non-owner viewer of a shared dashboard can re-run its
  queries; the results land in the viewer's own step_user_results rows and
  overlay THEIR reads only — the shared Step.data snapshot the owner and
  other viewers see is never modified.
- The endpoint is gated exactly like the /r read surface: anonymous callers
  get 401, viewers of a private report get 404, non-recipients of a
  'shared' report get 403.
- The owner cannot use the viewer endpoint (their refresh is /rerun, which
  updates the shared snapshot).
- reports.shared_run_identity ('viewer' | 'creator') is settable through the
  artifact visibility route, persists, and stamps executed_as on runs.
  Creator-credential runs are refused for authenticated strangers outside
  the report's org even when the dashboard is public.
- An owner rerun rewrites the shared snapshot and invalidates all cached
  per-viewer results.
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


def _headers(token: str, org_id: str = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = str(org_id)
    return headers


# Deterministic step code — no data source needed (ds_clients unused), so
# runs execute in a clean sandbox with no external boundary to stub.
GOOD_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"month": ["2024-01", "2024-02"], "revenue": [10, 20]})
"""

STALE_DATA = {
    "rows": [{"month": "stale", "revenue": -1}],
    "columns": [{"field": "month"}, {"field": "revenue"}],
}
FRESH_MONTHS = {"2024-01", "2024-02"}


async def _seed_artifact_graph(report_id: str, n_queries: int = 1):
    """Attach an artifact dashboard graph to an API-created report.

    Queries/visualizations/artifacts are produced by the AI completion flow
    in production; there is no public CRUD API that creates them, so the
    graph is seeded directly (mirrors tests/e2e/test_report_rerun_artifact.py).
    Every query's default step holds a distinguishable stale snapshot so a
    viewer's fresh run is observable against it.
    """
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        org_id, user_id = report.organization_id, report.user_id

        query_ids, viz_ids, step_ids = [], [], []
        for qi in range(n_queries):
            widget = Widget(title=f"W{qi} {suffix}", slug=f"w{qi}-{suffix}", report_id=report_id)
            db.add(widget)
            await db.flush()

            query = Query(
                title=f"Query {qi}",
                report_id=report_id,
                widget_id=widget.id,
                organization_id=org_id,
                user_id=user_id,
            )
            db.add(query)
            await db.flush()

            step = Step(
                title=f"Default {qi}",
                slug=f"default-{qi}-{suffix}",
                status="success",
                widget_id=widget.id,
                query_id=query.id,
                code=GOOD_CODE,
                data=STALE_DATA,
                created_at=now - timedelta(hours=1),
            )
            db.add(step)
            await db.flush()
            query.default_step_id = step.id

            viz = Visualization(
                title=f"Viz {qi}",
                status="success",
                report_id=report_id,
                query_id=query.id,
                view={"type": "bar_chart"},
            )
            db.add(viz)
            await db.flush()
            query_ids.append(str(query.id))
            viz_ids.append(str(viz.id))
            step_ids.append(str(step.id))

        db.add(Artifact(
            report_id=report_id,
            user_id=user_id,
            organization_id=org_id,
            title="Dashboard",
            mode="page",
            version=1,
            content={"code": "function App() {}", "visualization_ids": viz_ids},
            status="completed",
        ))
        await db.commit()

    return {"query_ids": query_ids, "viz_ids": viz_ids, "step_ids": step_ids}


def _set_artifact_visibility(test_client, report_id, owner, visibility, **extra):
    resp = test_client.put(
        f"/api/reports/{report_id}/visibility/artifact",
        json={"visibility": visibility, **extra},
        headers=_headers(owner["token"], owner["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _public_step(test_client, report_id, query_id, token=None):
    headers = _headers(token) if token else {}
    resp = test_client.get(f"/api/r/{report_id}/queries/{query_id}/step", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _shared_report(test_client, create_report, bootstrap_admin, invite_user_to_org,
                   visibility="internal", n_queries=1, **extra):
    admin = bootstrap_admin()
    owner_user = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    viewer = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    owner = {**owner_user, "org_id": admin["org_id"]}

    report = create_report(
        title=f"Shared {uuid.uuid4().hex[:6]}",
        user_token=owner["token"], org_id=admin["org_id"], data_sources=[],
    )
    seeded = _run(_seed_artifact_graph(report["id"], n_queries=n_queries))
    if visibility:
        _set_artifact_visibility(test_client, report["id"], owner, visibility, **extra)
    return admin, owner, viewer, report, seeded


@pytest.mark.e2e
def test_viewer_run_writes_per_viewer_results_not_shared_snapshot(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["steps_total"] == 2
    assert body["steps_succeeded"] == 2
    assert body["steps_failed"] == 0
    assert body["executed_as"] == "viewer"

    for qid in seeded["query_ids"]:
        # The viewer reads their own fresh run…
        step = _public_step(test_client, report["id"], qid, token=viewer["token"])
        assert {r["month"] for r in step["data"]["rows"]} == FRESH_MONTHS
        assert step["viewer_result"]["status"] == "success"
        assert step["viewer_result"]["executed_as"] == "viewer"
        assert step["viewer_result"]["last_run_at"]

        # …while the owner (and the shared snapshot) are untouched.
        step = _public_step(test_client, report["id"], qid, token=owner["token"])
        assert {r["month"] for r in step["data"]["rows"]} == {"stale"}
        assert step["viewer_result"] is None

        # The authenticated in-app read overlays the same per-viewer result.
        resp = test_client.get(
            f"/api/queries/{qid}/default_step",
            headers=_headers(viewer["token"], admin["org_id"]),
        )
        assert resp.status_code == 200, resp.json()
        in_app = resp.json()["step"]
        assert {r["month"] for r in in_app["data"]["rows"]} == FRESH_MONTHS
        assert in_app["viewer_result"]["status"] == "success"

    # The owner's own rerun endpoint still reports the untouched snapshot,
    # so last_run_at semantics stay owner-scoped.
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], admin["org_id"]))
    assert resp.status_code == 200
    assert resp.json()["last_run_at"] is None


@pytest.mark.e2e
def test_viewer_run_gated_like_the_share_surface(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, _ = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility=None,  # stays private ('none')
    )

    # Anonymous callers are refused regardless of visibility.
    _set_artifact_visibility(test_client, report["id"], owner, "internal")
    resp = test_client.post(f"/api/r/{report['id']}/run")
    assert resp.status_code == 401, resp.text

    # A private report is invisible to the viewer.
    _set_artifact_visibility(test_client, report["id"], owner, "none")
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 404, resp.json()

    # 'shared' visibility only admits explicit recipients.
    _set_artifact_visibility(test_client, report["id"], owner, "shared", shared_user_ids=[])
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 403, resp.json()

    _set_artifact_visibility(
        test_client, report["id"], owner, "shared", shared_user_ids=[viewer["user_id"]],
    )
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["steps_succeeded"] == 1

    # The owner refreshes through /rerun — the viewer endpoint refuses them
    # so an owner can't accidentally produce a private copy of their own data.
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(owner["token"]))
    assert resp.status_code == 400, resp.json()


@pytest.mark.e2e
def test_run_identity_setting_persists_and_stamps_runs(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", run_identity="creator",
    )

    # Persisted and visible to the owner's share dialog…
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], admin["org_id"]))
    assert resp.json()["shared_run_identity"] == "creator"
    # …and on the public payload viewers load.
    resp = test_client.get(f"/api/r/{report['id']}", headers=_headers(viewer["token"]))
    assert resp.status_code == 200
    assert resp.json()["shared_run_identity"] == "creator"

    # Omitting run_identity on later visibility updates leaves it unchanged.
    _set_artifact_visibility(test_client, report["id"], owner, "internal")
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], admin["org_id"]))
    assert resp.json()["shared_run_identity"] == "creator"

    # Runs are stamped with the identity that executed them.
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["executed_as"] == "creator"

    step = _public_step(test_client, report["id"], seeded["query_ids"][0], token=viewer["token"])
    assert step["viewer_result"]["executed_as"] == "creator"
    assert {r["month"] for r in step["data"]["rows"]} == FRESH_MONTHS


@pytest.mark.e2e
def test_creator_identity_refuses_out_of_org_strangers_on_public_dashboards(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, _ = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="public", run_identity="creator",
    )
    outsider = bootstrap_admin("outsider")

    # A public link lets any signed-in user *view*, but creator-credential
    # runs stay limited to the report org's members / share recipients.
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(outsider["token"]))
    assert resp.status_code == 403, resp.json()

    # Same-org viewers may still run on the owner's behalf.
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["executed_as"] == "creator"


@pytest.mark.e2e
def test_owner_rerun_invalidates_cached_viewer_results(
    test_client, create_report, bootstrap_admin, invite_user_to_org, rerun_report,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    qid = seeded["query_ids"][0]

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert _public_step(test_client, report["id"], qid, token=viewer["token"])["viewer_result"]

    # Owner refresh rewrites the shared snapshot → stale per-viewer rows drop.
    rerun_report(report["id"], user_token=owner["token"], org_id=admin["org_id"])

    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["viewer_result"] is None
    assert {r["month"] for r in step["data"]["rows"]} == FRESH_MONTHS


# ── Snapshot withholding (viewer-identity mode on user-scoped connections) ──

async def _attach_source_with_connection(report_id: str, auth_policy: str):
    """Attach a data source backed by a connection with the given auth_policy.

    Creating a user_required connection through the API requires an enterprise
    license and a reachable database, neither of which this suite has — the
    rows are seeded directly to put the report into the state the withholding
    policy reads (connection.auth_policy). The saved step code never touches
    the (unreachable) source, so runs stay deterministic.
    """
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        conn = Connection(
            name=f"warehouse-{suffix}",
            type="postgresql",
            config={"host": "localhost", "port": 5432, "database": "nope"},
            organization_id=report.organization_id,
            auth_policy=auth_policy,
        )
        db.add(conn)
        await db.flush()
        ds = DataSource(
            name=f"Warehouse {suffix}",
            organization_id=report.organization_id,
            is_public=True,
        )
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn.id)))
        await db.execute(report_data_source_association.insert().values(
            report_id=str(report_id), data_source_id=str(ds.id)))
        await db.commit()
        return str(ds.id)


@pytest.mark.e2e
def test_viewer_identity_mode_withholds_creator_snapshot_on_user_scoped_sources(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(report["id"], "user_required"))
    qid = seeded["query_ids"][0]

    # Non-owner viewers get no snapshot — public and in-app reads alike…
    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")

    resp = test_client.get(
        f"/api/queries/{qid}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    in_app = resp.json()["step"]
    assert in_app["snapshot_withheld"] is True
    assert not (in_app["data"] or {}).get("rows")

    # …the owner keeps seeing their own snapshot…
    step = _public_step(test_client, report["id"], qid, token=owner["token"])
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"stale"}

    # …and anonymous viewers of a public link are withheld too.
    _set_artifact_visibility(test_client, report["id"], owner, "public")
    step = _public_step(test_client, report["id"], qid)
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")

    # Running as themselves replaces "nothing" with their own result.
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["steps_succeeded"] == 1
    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == FRESH_MONTHS

    # Creator mode is the owner explicitly sharing their view — a fresh
    # viewer with no results of their own sees the snapshot again.
    _set_artifact_visibility(test_client, report["id"], owner, "public", run_identity="creator")
    viewer2 = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    step = _public_step(test_client, report["id"], qid, token=viewer2["token"])
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"stale"}


@pytest.mark.e2e
def test_system_only_sources_keep_serving_the_snapshot(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Withholding must not regress plain sharing: with system-only
    credentials the snapshot is not credential-differentiated."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(report["id"], "system_only"))

    step = _public_step(test_client, report["id"], seeded["query_ids"][0], token=viewer["token"])
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"stale"}


@pytest.mark.e2e
def test_snapshot_withholding_policy_gates_email_pdfs(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The share/scheduled emails attach a PDF rendered from the creator
    snapshot; the same policy that hides the snapshot must skip the PDF."""
    from app.services.viewer_data_policy import report_snapshot_withheld

    async def policy(report_id):
        async with async_session_maker() as db:
            return await report_snapshot_withheld(db, report_id)

    # viewer-identity + user-scoped source → withheld
    admin, owner, _, strict_report, _ = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(strict_report["id"], "user_required"))
    assert _run(policy(strict_report["id"])) is True

    # creator mode → owner shares their view, PDF allowed
    _set_artifact_visibility(test_client, strict_report["id"], owner, "internal", run_identity="creator")
    assert _run(policy(strict_report["id"])) is False

    # system-only source → not credential-differentiated, PDF allowed
    admin2, owner2, _, plain_report, _ = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(plain_report["id"], "system_only"))
    assert _run(policy(plain_report["id"])) is False


# ── Export authorization (IDOR) + strict-mode withholding ──

@pytest.mark.e2e
def test_step_export_requires_report_access(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """/steps/{id}/export historically had no object-level check — any
    authenticated user could pull any step's rows by id. It must now enforce
    the report's visibility."""
    admin, owner, _, private_report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility=None,  # private ('none')
    )
    sid = seeded["step_ids"][0]
    outsider = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    # Owner may export their own private step…
    resp = test_client.get(
        f"/api/steps/{sid}/export?format=csv",
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert "stale" in resp.text

    # …a random org member may not (report is private).
    resp = test_client.get(
        f"/api/steps/{sid}/export?format=csv",
        headers=_headers(outsider["token"], admin["org_id"]),
    )
    assert resp.status_code in (403, 404), resp.text
    assert "stale" not in resp.text

    # Cross-org caller is refused too.
    other = bootstrap_admin("exporter-outsider")
    resp = test_client.get(
        f"/api/steps/{sid}/export?format=csv",
        headers=_headers(other["token"], other["org_id"]),
    )
    assert resp.status_code in (403, 404), resp.text


@pytest.mark.e2e
def test_step_export_withheld_for_strict_viewer(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A shared-report viewer in viewer-identity mode on a user-scoped source
    is refused the export (it would hand them the creator snapshot as CSV)."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(report["id"], "user_required"))
    sid = seeded["step_ids"][0]

    # Viewer with no run of their own → withheld → refused.
    resp = test_client.get(
        f"/api/steps/{sid}/export?format=csv",
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 403, resp.text
    assert "stale" not in resp.text

    # Owner still exports their own snapshot.
    resp = test_client.get(
        f"/api/steps/{sid}/export?format=csv",
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert "stale" in resp.text


# ── Fork: copies steps, never shares the reference ──

@pytest.mark.e2e
def test_fork_copies_steps_and_cannot_mutate_source(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Forking a report must give the fork its OWN step rows: the fork's data
    is a copy (system-only), and a rerun of the fork must not touch the source
    report's steps (the shared-reference cross-report write bug)."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    src_qid, src_sid = seeded["query_ids"][0], seeded["step_ids"][0]

    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]

    fork_q = test_client.get(
        f"/api/queries?report_id={fork_id}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()[0]
    # The fork's step is a NEW row, not the source step.
    assert fork_q["default_step_id"] != src_sid
    # System-only data was copied into the fork.
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]
    assert {r["month"] for r in fstep["data"]["rows"]} == {"stale"}

    # The forker reruns THEIR fork → the source report's step is untouched.
    resp = test_client.post(
        f"/api/reports/{fork_id}/rerun",
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    src = _public_step(test_client, report["id"], src_qid, token=owner["token"])
    assert {r["month"] for r in src["data"]["rows"]} == {"stale"}, (
        "fork rerun mutated the source report's step (shared-reference bug)")
