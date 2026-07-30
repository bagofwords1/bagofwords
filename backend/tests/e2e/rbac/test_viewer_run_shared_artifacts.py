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

async def _attach_source_with_connection(report_id: str, auth_policy: str, is_public: bool = True):
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
            is_public=is_public,
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

    # The report advertises its user-scoped source, so the share dialog shows
    # the run-identity toggle.
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], admin["org_id"]))
    assert resp.json()["has_user_scoped"] is True

    # Non-owner viewers get no snapshot — public and in-app reads alike —
    # and no code either (SQL leaks schema/table/filter details)…
    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")
    assert not step.get("code")

    resp = test_client.get(
        f"/api/queries/{qid}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    in_app = resp.json()["step"]
    assert in_app["snapshot_withheld"] is True
    assert not (in_app["data"] or {}).get("rows")
    assert not in_app.get("code")

    # …the owner keeps seeing their own snapshot…
    step = _public_step(test_client, report["id"], qid, token=owner["token"])
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"stale"}

    # …and anonymous viewers of a public link are withheld too.
    _set_artifact_visibility(test_client, report["id"], owner, "public")
    step = _public_step(test_client, report["id"], qid)
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")

    # Running as themselves replaces "nothing" with their own result. The
    # viewer has no stored credential for the user_required source, so the
    # run reports it with a machine-readable code (drives the /r gate's
    # "connect your source" state).
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["steps_succeeded"] == 1
    assert body["data_source_errors"], body
    assert body["data_source_errors"][0]["code"] == "credentials_required"
    assert body["data_source_errors"][0]["data_source_id"]
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
def test_viewer_run_reports_no_access_code(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A viewer without permission on the report's data source gets a
    machine-readable no_access error from their run (drives the /r gate's
    'ask an admin' state) — distinct from the missing-credential case."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(report["id"], "user_required", is_public=False))

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["data_source_errors"], body
    assert body["data_source_errors"][0]["code"] == "no_access"


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

    # No user-scoped source → the share dialog hides the run-identity toggle.
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], admin["org_id"]))
    assert resp.json()["has_user_scoped"] is False


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


@pytest.mark.e2e
def test_fork_of_rls_report_copies_empty_step_data(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Forking an RLS report is allowed (system_only, forker has data-source
    access) but must NOT copy the owner's snapshot: the shared Step.data is the
    owner's row slice of a shared materialization, so the fork gets empty data
    and the forker re-runs it under their own RLS identity."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_rls_relation(report["id"], rls_enabled=True))
    src_sid = seeded["step_ids"][0]

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
    # New step row, and the owner's RLS slice was NOT copied into it.
    assert fork_q["default_step_id"] != src_sid
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]
    assert not (fstep["data"] or {}).get("rows"), (
        "fork of an RLS report copied the owner's row slice into the fork")


@pytest.mark.e2e
def test_queries_endpoints_withhold_snapshot_for_non_owner(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The authenticated /queries list + detail endpoints embed the query's
    default_step (lazy-loaded), which carries the shared Step.data snapshot.
    A non-owner reading a credential-differentiated report (here RLS) must get
    the withheld/empty snapshot there too — not just on the public /r step and
    the /default_step endpoints."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_rls_relation(report["id"], rls_enabled=True))
    qid = seeded["query_ids"][0]

    # list_queries — non-owner is withheld the creator snapshot…
    q = test_client.get(
        f"/api/queries?report_id={report['id']}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()[0]
    assert q["default_step"]["snapshot_withheld"] is True
    assert not (q["default_step"]["data"] or {}).get("rows")

    # get_query — same withholding on the detail endpoint…
    q = test_client.get(
        f"/api/queries/{qid}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()
    assert q["default_step"]["snapshot_withheld"] is True
    assert not (q["default_step"]["data"] or {}).get("rows")

    # …while the owner still sees their own snapshot through both.
    q = test_client.get(
        f"/api/queries?report_id={report['id']}",
        headers=_headers(owner["token"], admin["org_id"]),
    ).json()[0]
    assert q["default_step"]["snapshot_withheld"] is False
    assert {r["month"] for r in q["default_step"]["data"]["rows"]} == {"stale"}


async def _seed_rls_entity(org_id: str, owner_id: str, rls_enabled: bool = True) -> str:
    """Create an org-visible (global/approved) entity owned by owner_id whose
    single data source exposes a bow relation with RLS. Its materialized
    `data` is the owner's row slice."""
    from app.models.connection import Connection
    from app.models.connection_table import ConnectionTable, KIND_BOW
    from app.models.data_source import DataSource
    from app.models.datasource_table import DataSourceTable
    from app.models.domain_connection import domain_connection
    from app.models.entity import Entity, entity_data_source_association

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        conn = Connection(
            name=f"warehouse-{suffix}", type="postgresql",
            config={"host": "localhost", "port": 5432, "database": "nope"},
            organization_id=str(org_id), auth_policy="system_only",
        )
        db.add(conn)
        await db.flush()
        ds = DataSource(name=f"Warehouse {suffix}", organization_id=str(org_id), is_public=True)
        db.add(ds)
        await db.flush()
        ct = ConnectionTable(
            connection_id=str(conn.id), name=f"sales_{suffix}", kind=KIND_BOW,
            columns=[{"name": "month"}], pks=[], fks=[], rls_enabled=rls_enabled,
        )
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name=f"sales_{suffix}", datasource_id=str(ds.id),
            connection_table_id=str(ct.id), is_active=True,
        ))
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn.id)))
        ent = Entity(
            organization_id=str(org_id), owner_id=str(owner_id), type="model",
            title="Sales", slug=f"sales-{suffix}", code="SELECT 1",
            data={"rows": [{"month": "owner"}]}, status="published",
            global_status="approved",
        )
        db.add(ent)
        await db.flush()
        await db.execute(entity_data_source_association.insert().values(
            entity_id=str(ent.id), data_source_id=str(ds.id)))
        await db.commit()
        return str(ent.id)


@pytest.mark.e2e
def test_entity_snapshot_withheld_for_non_owner_on_rls(
    test_client, bootstrap_admin, invite_user_to_org,
):
    """GET /entities/{id} serves EntitySchema.data — a single materialized
    snapshot honoring user_required/RLS. A non-owner reading a global entity
    backed by an RLS source must be withheld the owner's row slice; the owner
    still sees it, and a non-RLS system-only entity keeps serving to everyone."""
    admin = bootstrap_admin()
    owner = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    viewer = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    ent_id = _run(_seed_rls_entity(admin["org_id"], owner["user_id"], rls_enabled=True))

    # Non-owner is withheld the snapshot…
    body = test_client.get(
        f"/api/entities/{ent_id}", headers=_headers(viewer["token"], admin["org_id"]),
    ).json()
    assert body["snapshot_withheld"] is True
    assert not (body["data"] or {}).get("rows")

    # …owner still sees their own snapshot…
    body = test_client.get(
        f"/api/entities/{ent_id}", headers=_headers(owner["token"], admin["org_id"]),
    ).json()
    assert body["snapshot_withheld"] is False
    assert {r["month"] for r in body["data"]["rows"]} == {"owner"}

    # …and a non-RLS system-only entity keeps serving to a non-owner (control).
    ctrl_id = _run(_seed_rls_entity(admin["org_id"], owner["user_id"], rls_enabled=False))
    body = test_client.get(
        f"/api/entities/{ctrl_id}", headers=_headers(viewer["token"], admin["org_id"]),
    ).json()
    assert body["snapshot_withheld"] is False
    assert {r["month"] for r in body["data"]["rows"]} == {"owner"}


# ── Thumbnails dropped for strict-mode dashboards ──

async def _set_artifact_thumbnail(report_id: str) -> str:
    """Give the report's artifact a thumbnail_path (as generation would)."""
    from sqlalchemy import select
    async with async_session_maker() as db:
        art = (await db.execute(
            select(Artifact).where(Artifact.report_id == str(report_id))
        )).scalars().first()
        art.thumbnail_path = f"thumbnails/{art.id}.png"
        await db.commit()
        return str(art.id)


@pytest.mark.e2e
def test_strict_mode_drops_artifact_thumbnail(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A dashboard that becomes viewer-identity strict must lose its thumbnail
    (it renders the creator snapshot and /thumbnails is unauthenticated)."""
    admin, owner, viewer, report, _ = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility=None,
    )
    _run(_attach_source_with_connection(report["id"], "user_required"))
    _run(_set_artifact_thumbnail(report["id"]))

    # Configuring viewer-identity sharing on a user-scoped source clears it.
    _set_artifact_visibility(test_client, report["id"], owner, "internal", run_identity="viewer")

    async def _thumb():
        from sqlalchemy import select
        async with async_session_maker() as db:
            art = (await db.execute(
                select(Artifact).where(Artifact.report_id == str(report["id"]))
            )).scalars().first()
            return art.thumbnail_path
    assert _run(_thumb()) is None

    # A system-only report keeps its thumbnail (plain sharing unchanged).
    admin2, owner2, _, plain, _ = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility=None,
    )
    _run(_attach_source_with_connection(plain["id"], "system_only"))
    _run(_set_artifact_thumbnail(plain["id"]))
    _set_artifact_visibility(test_client, plain["id"], owner2, "internal", run_identity="viewer")

    async def _thumb2():
        from sqlalchemy import select
        async with async_session_maker() as db:
            art = (await db.execute(
                select(Artifact).where(Artifact.report_id == str(plain["id"]))
            )).scalars().first()
            return art.thumbnail_path
    assert _run(_thumb2()) is not None


# ── Built-in RLS relations (system_only, but identity-differentiated) ──

async def _attach_rls_relation(report_id: str, rls_enabled: bool = True):
    """Attach a system_only source whose report reads a bow custom-query
    relation with RLS enabled.

    Built-in RLS filters a shared, single-credential materialization per
    requesting user, so the owner's snapshot is their own row slice — the
    withholding policy must treat it like a user-scoped source even though the
    connection is system_only. Seeded directly (the fast/DuckDB path needs the
    beta flag + a reachable source neither of which this suite has); the test
    exercises the DETECTION + gating, not the row filtering itself.
    """
    from app.models.connection import Connection
    from app.models.connection_table import ConnectionTable, KIND_BOW
    from app.models.data_source import DataSource
    from app.models.datasource_table import DataSourceTable
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        conn = Connection(
            name=f"warehouse-{suffix}", type="postgresql",
            config={"host": "localhost", "port": 5432, "database": "nope"},
            organization_id=report.organization_id, auth_policy="system_only",
        )
        db.add(conn)
        await db.flush()
        ds = DataSource(name=f"Warehouse {suffix}", organization_id=report.organization_id, is_public=True)
        db.add(ds)
        await db.flush()
        ct = ConnectionTable(
            connection_id=str(conn.id), name=f"sales_{suffix}", kind=KIND_BOW,
            columns=[{"name": "month"}, {"name": "revenue"}], pks=[], fks=[],
            rls_enabled=rls_enabled,
        )
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name=f"sales_{suffix}", datasource_id=str(ds.id),
            connection_table_id=str(ct.id), is_active=True,
        ))
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn.id)))
        await db.execute(report_data_source_association.insert().values(
            report_id=str(report_id), data_source_id=str(ds.id)))
        await db.commit()
        return str(ds.id)


@pytest.mark.e2e
def test_rls_relation_withholds_snapshot_on_system_only(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """An RLS-enabled relation makes the shared snapshot identity-differentiated
    even though the connection is system_only — non-owner viewers must be
    withheld, and the report advertises has_rls for the share dialog."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_rls_relation(report["id"], rls_enabled=True))
    qid = seeded["query_ids"][0]

    # Viewer is withheld the creator snapshot…
    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")

    # …owner still sees their own snapshot…
    step = _public_step(test_client, report["id"], qid, token=owner["token"])
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"stale"}

    # …and the report surfaces has_rls to the owner's share dialog.
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], admin["org_id"]))
    assert resp.status_code == 200
    assert resp.json()["has_rls"] is True

    # A relation with rls_enabled=False must NOT withhold (control).
    admin2, owner2, viewer2, plain, plain_seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_rls_relation(plain["id"], rls_enabled=False))
    step = _public_step(test_client, plain["id"], plain_seeded["query_ids"][0], token=viewer2["token"])
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"stale"}
    r = test_client.get(f"/api/reports/{plain['id']}", headers=_headers(owner2["token"], admin2["org_id"]))
    assert r.json()["has_rls"] is False


@pytest.mark.e2e
def test_creator_mode_blocked_on_rls_dashboards(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """'Run on my behalf' would hand the owner's RLS slice to every viewer,
    bypassing the row policy — setting it must be refused, and any run stays
    viewer-identity."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_rls_relation(report["id"], rls_enabled=True))

    # Setting creator mode on an RLS report is rejected.
    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal", "run_identity": "creator"},
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 400, resp.text

    # It stays viewer identity.
    resp = test_client.get(f"/api/reports/{report['id']}", headers=_headers(owner["token"], admin["org_id"]))
    assert resp.json()["shared_run_identity"] == "viewer"

    # A viewer run executes as the viewer (never creator) on an RLS report.
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["executed_as"] == "viewer"
