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
from app.models.artifact import ArtifactVersion
from tests.fixtures.artifact import seed_artifact
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

        await seed_artifact(
            db,
            report_id=report_id,
            user_id=user_id,
            organization_id=org_id,
            mode="page",
            title="Dashboard",
            content={"code": "function App() {}", "visualization_ids": viz_ids},
        )
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

async def _attach_source_with_connection(report_id: str, auth_policy: str, is_public: bool = True,
                                         attach: bool = True, name: str | None = None):
    """Attach a data source backed by a connection with the given auth_policy.

    Creating a user_required connection through the API requires an enterprise
    license and a reachable database, neither of which this suite has — the
    rows are seeded directly to put the report into the state the withholding
    policy reads (connection.auth_policy). The saved step code never touches
    the (unreachable) source, so runs stay deterministic.

    `attach=False` only creates the source in the report's organization, the
    way a chat-created report runs on the organization's sources without any
    of them attached to it. `name` overrides the source's generated name.
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
            name=name or f"Warehouse {suffix}",
            organization_id=report.organization_id,
            is_public=is_public,
        )
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn.id)))
        if attach:
            await db.execute(report_data_source_association.insert().values(
                report_id=str(report_id), data_source_id=str(ds.id)))
        await db.commit()
        return str(ds.id)


def _source_key_reference(name: str) -> str:
    """Code that reaches the source by its client key, as generated code does
    (`ds_clients["<source name>"]`, the single-connection key), inside a
    function the run never calls: the step names the source while its rows
    stay those of the code around it."""
    return f'def _reach_source(ds_clients):\n    return ds_clients["{name}"]\n\n'


async def _name_source_in_step_code(report_id: str, data_source_id: str):
    """Make every step of the report reach the data source by its client key
    (_source_key_reference). A report with no attached sources runs on the
    organization's sources its code reaches by key."""
    from sqlalchemy import select as _select
    from app.models.data_source import DataSource

    async with async_session_maker() as db:
        name = (await db.execute(
            _select(DataSource.name).where(DataSource.id == data_source_id))).scalar_one()
        steps = (await db.execute(
            _select(Step).join(Query, Query.id == Step.query_id).where(Query.report_id == report_id)
        )).scalars().all()
        for step in steps:
            step.code = _source_key_reference(name) + (step.code or "")
        await db.commit()


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
def test_enabling_creator_mode_drops_viewers_own_cached_results(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A viewer who ran as themselves before the owner turned on 'run on my
    behalf' must see the owner's snapshot afterwards — not stay pinned to the
    result their own credentials produced (a success row beats the snapshot,
    and outside the withheld gate there is no Run button to replace it)."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(report["id"], "user_required"))
    qid = seeded["query_ids"][0]

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["viewer_result"]["executed_as"] == "viewer"
    assert {r["month"] for r in step["data"]["rows"]} == FRESH_MONTHS

    _set_artifact_visibility(test_client, report["id"], owner, "internal", run_identity="creator")

    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["viewer_result"] is None
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"stale"}


@pytest.mark.e2e
def test_revoking_creator_mode_drops_results_run_with_owner_credentials(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Turning 'run on my behalf' off must take effect immediately: rows a
    viewer cached while running on the owner's credentials are the owner's
    view, and serving them after the revoke would keep sharing it."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", run_identity="creator",
    )
    _run(_attach_source_with_connection(report["id"], "user_required"))
    qid = seeded["query_ids"][0]

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["executed_as"] == "creator"
    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["viewer_result"]["executed_as"] == "creator"

    _set_artifact_visibility(test_client, report["id"], owner, "internal", run_identity="viewer")

    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["viewer_result"] is None
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")


@pytest.mark.e2e
def test_unchanged_run_identity_keeps_cached_viewer_results(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Only an actual change invalidates: re-saving the share dialog with the
    same run identity must not wipe every viewer's cached results."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", run_identity="viewer",
    )
    qid = seeded["query_ids"][0]

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()

    _set_artifact_visibility(test_client, report["id"], owner, "internal", run_identity="viewer")

    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["viewer_result"]["status"] == "success"
    assert {r["month"] for r in step["data"]["rows"]} == FRESH_MONTHS


@pytest.mark.e2e
def test_param_run_stamps_the_identity_whose_credentials_executed(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A viewer's param run in creator mode executes on the owner's
    credentials and must be stamped 'creator', like the dashboard run."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", run_identity="creator",
    )
    qid = seeded["query_ids"][0]

    resp = test_client.post(
        f"/api/queries/{qid}/run", json={"mode": "viewer", "params": {}},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == "success", resp.json()

    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert step["viewer_result"]["executed_as"] == "creator"


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


@pytest.mark.e2e
def test_step_read_applies_viewer_policy_for_withheld_viewer(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """GET /steps/{id} follows the same viewer boundary as the query read
    path: a withheld viewer gets no rows, no code and no applied params."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_source_with_connection(report["id"], "user_required"))
    sid = seeded["step_ids"][0]
    _run(_set_step_params(sid, applied={"Year": "2024", "UserEmail": "owner@corp.test"}))

    step = test_client.get(f"/api/steps/{sid}", headers=_headers(viewer["token"], admin["org_id"])).json()
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")
    assert not step.get("code")
    assert not step.get("applied_params")

    mine = test_client.get(f"/api/steps/{sid}", headers=_headers(owner["token"], admin["org_id"])).json()
    assert mine["snapshot_withheld"] is False
    assert mine["code"]
    assert mine["applied_params"]["UserEmail"] == "owner@corp.test"


@pytest.mark.e2e
def test_step_read_drops_creator_identity_params_for_viewers(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A viewer holding their own successful run sees their rows, but the
    shared step's applied params still never carry the creator's identity."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    sid = seeded["step_ids"][0]
    _run(_set_step_params(sid, applied={"Year": "2024", "UserEmail": "owner@corp.test"}))
    _run(_seed_viewer_result(sid, viewer_email=viewer["email"]))

    step = test_client.get(f"/api/steps/{sid}", headers=_headers(viewer["token"], admin["org_id"])).json()
    assert step["snapshot_withheld"] is False
    assert {r["month"] for r in step["data"]["rows"]} == {"mine"}
    assert step["viewer_result"]
    assert step["applied_params"] == {"Year": "2024"}

    mine = test_client.get(f"/api/steps/{sid}", headers=_headers(owner["token"], admin["org_id"])).json()
    assert mine["applied_params"]["UserEmail"] == "owner@corp.test"


async def _seed_viewer_result(step_id: str, viewer_email: str, executed_as: str = "viewer", *,
                              month: str = "mine", params_fingerprint: str = "",
                              last_run_at: datetime | None = None):
    """The viewer's own successful run of the step (normally written by a
    viewer run, which needs a live data source). `executed_as` records whose
    credentials ran it ('creator': the report owner's); its one row's month
    is `month`, stored under `params_fingerprint` ('' = no parameters)."""
    from sqlalchemy import select as _select
    from app.models.step_user_result import StepUserResult
    from app.models.user import User as _User
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        query = await db.get(Query, step.query_id)
        viewer_id = (await db.execute(_select(_User.id).where(_User.email == viewer_email))).scalar_one()
        db.add(StepUserResult(
            step_id=step_id, user_id=viewer_id, organization_id=query.organization_id,
            report_id=query.report_id, status="success", executed_as=executed_as,
            data={"rows": [{"month": month}], "columns": [{"field": "month"}]},
            params_fingerprint=params_fingerprint,
            last_run_at=last_run_at or datetime.utcnow(),
        ))
        await db.commit()


async def _set_step_params(step_id: str, applied: dict):
    """Declare an input param and an identity-bound param on the step's query
    and stamp the values the snapshot was produced with."""
    def spec(name, source, binding=None):
        return {"name": name, "type": "string", "label": name, "default": None,
                "required": False, "source": source, "identity_binding": binding,
                "options": None, "options_source": None, "strict_options": False}
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        query = await db.get(Query, step.query_id)
        query.parameters = [spec("Year", "input"), spec("UserEmail", "identity", "viewer.email")]
        step.applied_params = applied
        await db.commit()


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
            select(ArtifactVersion).where(ArtifactVersion.report_id == str(report_id))
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
                select(ArtifactVersion).where(ArtifactVersion.report_id == str(report["id"]))
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
                select(ArtifactVersion).where(ArtifactVersion.report_id == str(plain["id"]))
            )).scalars().first()
            return art.thumbnail_path
    assert _run(_thumb2()) is not None


# ── Built-in RLS relations (system_only, but identity-differentiated) ──

async def _attach_rls_relation(report_id: str, rls_enabled: bool = True, attach: bool = True,
                               buildable: bool = False, relation: dict | None = None):
    """Attach a system_only source whose report reads a bow custom-query
    relation with RLS enabled.

    Built-in RLS filters a shared, single-credential materialization per
    requesting user, so the owner's snapshot is their own row slice — the
    withholding policy must treat it like a user-scoped source even though the
    connection is system_only. Seeded directly (the fast/DuckDB path needs the
    beta flag + a reachable source neither of which this suite has); the test
    exercises the DETECTION + gating, not the row filtering itself.

    `attach=False` only creates the source in the report's organization (see
    _attach_source_with_connection). `buildable=True` backs it with an
    in-memory SQLite connection, so a run can build its clients. `relation`
    overrides ConnectionTable fields (an artifact and a row policy, for a
    relation a run actually reads).
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
            name=f"warehouse-{suffix}",
            type="sqlite" if buildable else "postgresql",
            config={"database": ":memory:"} if buildable
            else {"host": "localhost", "port": 5432, "database": "nope"},
            organization_id=report.organization_id, auth_policy="system_only",
        )
        db.add(conn)
        await db.flush()
        ds = DataSource(name=f"Warehouse {suffix}", organization_id=report.organization_id, is_public=True)
        db.add(ds)
        await db.flush()
        fields = dict(
            name=f"sales_{suffix}",
            columns=[{"name": "month"}, {"name": "revenue"}], pks=[], fks=[],
            rls_enabled=rls_enabled,
        )
        fields.update(relation or {})
        ct = ConnectionTable(connection_id=str(conn.id), kind=KIND_BOW, **fields)
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(
            name=fields["name"], datasource_id=str(ds.id),
            connection_table_id=str(ct.id), is_active=True,
        ))
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn.id)))
        if attach:
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


# ── Forking a delegated (user_required) source ──────────────────────────────
#
# Forking these was refused outright until the fork stopped carrying anything
# credential-differentiated. The fork is now created EMPTY — no rows and no SQL
# — and `hydrate_fork` fills in each query only where the forker's own
# credentials could run it. These cover the creation half; the hydration half
# is unit-tested (tests/unit/test_fork_hydration.py), where success and failure
# can be driven deterministically instead of racing a background task.


async def _attach_user_scoped_source(report_id: str, attach: bool = True):
    """Attach a delegated (user_required) source to the report.

    The counterpart to _attach_rls_relation: there the connection is
    system_only and RLS differentiates the rows; here the connection itself
    resolves credentials per user, so a reader may have no access at all.
    `attach=False` only creates it in the report's organization.
    """
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        conn = Connection(
            name=f"delegated-{suffix}", type="powerbi",
            config={"auth_type": "service_principal"},
            organization_id=report.organization_id,
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        db.add(conn)
        await db.flush()
        ds = DataSource(
            name=f"Delegated {suffix}",
            organization_id=report.organization_id,
            is_public=True,
        )
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn.id)))
        if attach:
            await db.execute(report_data_source_association.insert().values(
                report_id=str(report_id), data_source_id=str(ds.id)))
        await db.commit()
        return str(ds.id)


@pytest.mark.e2e
def test_fork_eligibility_allows_user_scoped_source(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A delegated source no longer makes a report un-forkable.

    auth_policy describes HOW a connection authenticates, never who is
    entitled to it — that is user_can_access_data_source, which still runs.
    """
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))

    resp = test_client.get(
        f"/api/r/{report['id']}", headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    elig = resp.json()["fork_eligibility"]
    assert elig["can_fork"] is True, f"still blocked: {elig['reason']}"


@pytest.mark.e2e
def test_fork_of_user_scoped_source_carries_no_code_or_data(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """The regression that opening the fork could have introduced.

    The share refuses a withheld reader the SQL itself ("the SQL leaks
    schema/table/filter details even without rows"), so the fork must not hand
    them the same SQL in their own copy. Both code and data land empty; only a
    successful run under the forker's own credentials writes the code back.

    Hydration is stubbed out so this asserts what fork_report itself produces —
    otherwise the background pass (which, on this unreachable connection, fails
    every step and deletes the fork) would race the assertions.
    """
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))

    spawned = []
    monkeypatch.setattr(
        "app.core.fire_and_forget.spawn",
        lambda coro: (spawned.append(coro), coro.close())[0],
    )

    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]
    assert spawned, "hydration was not scheduled for a delegated-source fork"

    fork_q = test_client.get(
        f"/api/queries?report_id={fork_id}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()[0]
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]

    assert not (fstep.get("code") or "").strip(), (
        "fork of a delegated source carried the source SQL into the fork"
    )
    assert not (fstep.get("data") or {}).get("rows"), (
        "fork of a delegated source carried the owner's rows into the fork"
    )


@pytest.mark.e2e
def test_fork_of_system_only_source_still_carries_code(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The narrow rule stays narrow: a plain system-only fork is unchanged —
    everyone resolves the same credentials, so there is nothing to withhold."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )

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
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]

    assert (fstep.get("code") or "").strip(), (
        "system-only fork lost its code — the withholding rule leaked past "
        "delegated sources"
    )


# ── Reports with no attached data sources ───────────────────────────────────
#
# A chat-created report has no attached sources: its runs use the
# organization's sources, and its code reaches them by name
# (QueryService._report_data_sources). Every rule that asks whether a report
# reads a row-filtered relation or a per-user connection answers for the
# organization sources its code names, exactly as for attached ones.


def _fork_default_step_as(test_client, report_id, user, org_id) -> dict:
    resp = test_client.post(f"/api/reports/{report_id}/fork", json={}, headers=_headers(user["token"], org_id))
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]
    fork_q = test_client.get(f"/api/queries?report_id={fork_id}", headers=_headers(user["token"], org_id)).json()[0]
    return test_client.get(
        f"/api/queries/{fork_q['id']}/default_step", headers=_headers(user["token"], org_id),
    ).json()["step"]


def _assert_hydration_runs_as_the_forker(test_client, spawned, query_id, forker, org_id):
    """Run the fork's hydration (captured from spawn) for real and check what
    it leaves: never the owner's rows (the seeded snapshot is {"month":
    "stale"}), and the code only where the forker's own run succeeded."""
    assert len(spawned) == 1, "hydration was not scheduled for the fork"
    outcome = _run(spawned[0])
    resp = test_client.get(f"/api/queries/{query_id}/default_step", headers=_headers(forker["token"], org_id))
    if outcome.get("deleted"):
        # The forker could run nothing: the fork is retired, rows and code never land.
        return outcome
    after = resp.json()["step"]
    rows = (after.get("data") or {}).get("rows") or []
    assert {r.get("month") for r in rows} != {"stale"}, "hydration filled the fork with the owner's rows"
    if after.get("status") == "success":
        assert (after.get("code") or "").strip(), "a successful run as the forker did not write its code back"
    else:
        assert not (after.get("code") or "").strip(), "the code came back without a successful run as the forker"
    return outcome


def _report_flags(test_client, report_id, owner, org_id) -> tuple:
    resp = test_client.get(f"/api/reports/{report_id}", headers=_headers(owner["token"], org_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body.get("has_rls"), body.get("has_user_scoped")


async def _store_run_identity(report_id: str, identity: str):
    """Store the run identity directly. The visibility route now refuses
    'creator' on these reports; a report saved that way before the rule
    reached unattached reports still carries it."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        report.shared_run_identity = identity
        await db.commit()


@pytest.mark.e2e
def test_fork_of_unattached_report_reading_a_row_filtered_relation_copies_no_rows(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The owner's snapshot is their row slice of the relation whether or not
    the source is attached to the report: the fork starts with no rows."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    ds_id = _run(_attach_rls_relation(report["id"], attach=False))
    _run(_name_source_in_step_code(report["id"], ds_id))

    fstep = _fork_default_step_as(test_client, report["id"], viewer, admin["org_id"])
    assert not (fstep["data"] or {}).get("rows"), (
        "the fork received the owner's rows of a row-filtered relation")


@pytest.mark.e2e
def test_fork_of_unattached_report_over_a_per_user_connection_carries_no_code_or_rows(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """Same rule as the attached delegated source: the fork is created with no
    rows and no SQL, and hydration, run here for real, fills it in only from
    the forker's own run."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    ds_id = _run(_attach_user_scoped_source(report["id"], attach=False))
    _run(_name_source_in_step_code(report["id"], ds_id))
    # Captured, then run once the creation-time state is read, so the
    # background pass cannot race the first assertions.
    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)

    fstep = _fork_default_step_as(test_client, report["id"], viewer, admin["org_id"])
    assert not (fstep.get("code") or "").strip(), "the fork carried the source SQL"
    assert not (fstep.get("data") or {}).get("rows"), "the fork carried the owner's rows"

    _assert_hydration_runs_as_the_forker(test_client, spawned, fstep["query_id"], viewer, admin["org_id"])


@pytest.mark.e2e
def test_report_flags_describe_the_unattached_sources_its_code_names(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """has_rls / has_user_scoped follow the organization sources the code
    names — not every source the organization happens to have."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    org_id = admin["org_id"]
    plain_id = _run(_attach_source_with_connection(report["id"], "system_only", attach=False))
    rls_id = _run(_attach_rls_relation(report["id"], attach=False))
    scoped_id = _run(_attach_user_scoped_source(report["id"], attach=False))

    # The organization has both kinds, but the code names only a plain source.
    _run(_name_source_in_step_code(report["id"], plain_id))
    assert _report_flags(test_client, report["id"], owner, org_id) == (False, False)

    _run(_name_source_in_step_code(report["id"], scoped_id))
    assert _report_flags(test_client, report["id"], owner, org_id) == (False, True)

    _run(_name_source_in_step_code(report["id"], rls_id))
    assert _report_flags(test_client, report["id"], owner, org_id) == (True, True)


@pytest.mark.e2e
def test_unattached_report_reading_a_row_filtered_relation_runs_viewers_as_themselves(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    ds_id = _run(_attach_rls_relation(report["id"], attach=False, buildable=True))
    _run(_name_source_in_step_code(report["id"], ds_id))

    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal", "run_identity": "creator"},
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 400, resp.text

    # Even a report stored in creator mode runs a viewer as the viewer: the
    # query run...
    _run(_store_run_identity(report["id"], "creator"))
    qid = seeded["query_ids"][0]
    resp = test_client.post(
        f"/api/queries/{qid}/run", json={"mode": "viewer", "params": {}},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success", resp.json()
    stored = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert stored["viewer_result"]["executed_as"] == "viewer"
    # ...and the page's run.
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["executed_as"] == "viewer"


def _row_filtered_artifact(tmp_path, relation: str, rows: list) -> tuple:
    """A real encrypted DuckDB artifact (as tests/unit/test_custom_query_rls.py
    builds one) holding the relation `relation(owner_email, amount)`."""
    from app.data_sources.fast import artifacts

    key = artifacts.new_artifact_key()
    path = tmp_path / f"orders-{uuid.uuid4().hex[:6]}.duckdb"
    con = artifacts.connect_encrypted(path, key)
    con.execute(f"CREATE TABLE {relation} (owner_email VARCHAR, amount INTEGER)")
    con.executemany(f"INSERT INTO {relation} VALUES (?, ?)", rows)
    con.close()
    return str(path), artifacts.encrypt_key(key)


FAST_READ_CODE = """
def _reach_source(ds_clients):
    return ds_clients["{plain}"]


def generate_df(ds_clients, excel_files):
    fast = [client for key, client in ds_clients.items() if key.endswith("::fast")]
    return fast[0].execute_query("SELECT owner_email, amount FROM {relation}")
"""


async def _source_name(data_source_id: str) -> str:
    from sqlalchemy import select as _select
    from app.models.data_source import DataSource

    async with async_session_maker() as db:
        return (await db.execute(
            _select(DataSource.name).where(DataSource.id == data_source_id))).scalar_one()


def _report_over_a_row_filtered_relation(test_client, create_report, bootstrap_admin, invite_user_to_org,
                                         tmp_path, *, attach: bool):
    """A shared report whose step reads a real row-filtered relation (one row
    per user email: the owner's and the viewer's). Its code reaches a plain
    source by key and the relation's client by iterating ds_clients, so
    unattached it counts every organization source -- the relation's
    included."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    relation = f"orders_{uuid.uuid4().hex[:6]}"
    path, key_enc = _row_filtered_artifact(tmp_path, relation, [(owner["email"], 1), (viewer["email"], 2)])
    _run(_attach_rls_relation(report["id"], attach=attach, buildable=True, relation={
        "name": relation,
        "columns": [{"name": "owner_email"}, {"name": "amount"}],
        "artifact_path": path, "artifact_key_enc": key_enc, "no_rows": 2,
        "rls_mode": "attribute",
        "rls_policy": {"column": "owner_email", "source": "user.email", "op": "in"},
        "rls_default_deny": True,
    }))
    plain_id = _run(_attach_source_with_connection(report["id"], "system_only", attach=False))
    _run(_set_step_code(seeded["step_ids"][0],
                        FAST_READ_CODE.format(plain=_run(_source_name(plain_id)), relation=relation)))
    return admin, owner, viewer, report, seeded


@pytest.mark.e2e
def test_a_creator_mode_report_reaching_a_row_filtered_relation_runs_the_viewer_as_themselves(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    """The step reaches the relation's client without a key for it, beside a
    key for a plain source: the report still counts as reading the relation,
    so 'run on my behalf' is refused, and a report stored in that mode runs a
    viewer as themselves -- the rows are the viewer's."""
    admin, owner, viewer, report, seeded = _report_over_a_row_filtered_relation(
        test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, attach=False)
    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal", "run_identity": "creator"},
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 400, resp.text
    _run(_store_run_identity(report["id"], "creator"))

    resp = test_client.post(
        f"/api/queries/{seeded['query_ids'][0]}/run",
        json={"mode": "viewer", "params": {}},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success", body.get("error")
    assert {r["owner_email"] for r in body["data"]["rows"]} == {viewer["email"]}
    stored = _public_step(test_client, report["id"], seeded["query_ids"][0], token=viewer["token"])
    assert stored["viewer_result"]["executed_as"] == "viewer"


@pytest.mark.e2e
def test_the_page_run_of_a_creator_mode_report_reaching_a_row_filtered_relation_runs_as_the_viewer(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    """The same through the shared page's run (POST /r/{id}/run)."""
    admin, owner, viewer, report, seeded = _report_over_a_row_filtered_relation(
        test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, attach=False)
    _run(_store_run_identity(report["id"], "creator"))

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["executed_as"] == "viewer"
    step = _public_step(test_client, report["id"], seeded["query_ids"][0], token=viewer["token"])
    assert {r["owner_email"] for r in step["data"]["rows"]} == {viewer["email"]}


# A plain source whose name is also part of an identifier the code uses, and
# a row-filtered relation the code reaches by iterating ds_clients.
IDENTIFIER_NAMING_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    {plain}_rows = [client for client in ds_clients.values()]
    return pd.DataFrame({{"month": ["2024-01", "2024-02"], "revenue": [10, 20]}})
"""


@pytest.mark.e2e
def test_a_source_name_inside_an_identifier_does_not_narrow_a_step_reaching_every_client(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """A source counts as reached by its client key, not by its name showing
    up in the code. A step that iterates ds_clients reaches every source,
    a row-filtered one included, even when a plain source's name is part of a
    variable name: the report reads a row-filtered relation, 'run on my
    behalf' is refused, and a fork copies none of its rows."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    org_id = admin["org_id"]
    plain = f"db{uuid.uuid4().hex[:6]}"
    _run(_attach_source_with_connection(report["id"], "system_only", attach=False, name=plain))
    _run(_attach_rls_relation(report["id"], attach=False))
    _run(_set_step_code(seeded["step_ids"][0], IDENTIFIER_NAMING_CODE.format(plain=plain)))

    assert _report_flags(test_client, report["id"], owner, org_id)[0] is True
    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal", "run_identity": "creator"},
        headers=_headers(owner["token"], org_id),
    )
    assert resp.status_code == 400, resp.text

    monkeypatch.setattr("app.core.fire_and_forget.spawn", lambda job: None)
    fstep = _fork_default_step_as(test_client, report["id"], viewer, org_id)
    assert not (fstep["data"] or {}).get("rows"), "the fork received the owner's rows"


# generate_df names the clients dict `clients` and hands it to a helper whose
# parameter is called ds_clients and reads a plain source by key; the
# row-filtered source is reached through the renamed name.
RENAMED_CLIENTS_CODE = """
def fetch(ds_clients, sql):
    return ds_clients["{plain}"].execute_query(sql)


def generate_df(clients, excel_files):
    import pandas as pd
    if False:
        fetch(clients, "SELECT 1")
        clients["{rls}"].execute_query("SELECT 1")
    return pd.DataFrame({{"month": ["2024-01", "2024-02"], "revenue": [10, 20]}})
"""


@pytest.mark.e2e
def test_a_renamed_clients_parameter_still_counts_the_sources_it_reaches(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """generate_df receives the clients dict positionally under any name.
    Reaching the row-filtered source through that name, and handing the dict
    to a helper, makes the report read the row-filtered relation, whatever the
    helper's parameter is called: a fork copies none of its rows."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    org_id = admin["org_id"]
    plain = _run(_source_name(_run(_attach_source_with_connection(report["id"], "system_only", attach=False))))
    rls = _run(_source_name(_run(_attach_rls_relation(report["id"], attach=False))))
    _run(_set_step_code(seeded["step_ids"][0], RENAMED_CLIENTS_CODE.format(plain=plain, rls=rls)))

    assert _report_flags(test_client, report["id"], owner, org_id)[0] is True

    monkeypatch.setattr("app.core.fire_and_forget.spawn", lambda job: None)
    fstep = _fork_default_step_as(test_client, report["id"], viewer, org_id)
    assert not (fstep["data"] or {}).get("rows"), "the fork received the owner's rows"


# The run's namespace also binds the clients dict as the global `db_clients`:
# the plain source by key on the parameter, the row-filtered one on the global.
NAMESPACE_CLIENTS_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    if False:
        ds_clients["{plain}"].execute_query("SELECT 1")
        db_clients["{rls}"].execute_query("SELECT 1")
    return pd.DataFrame({{"month": ["2024-01", "2024-02"], "revenue": [10, 20]}})
"""


@pytest.mark.e2e
def test_the_clients_global_of_the_run_counts_the_sources_it_reaches(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """A source reached through the `db_clients` global counts like one
    reached through generate_df's parameter: the report reads the
    row-filtered relation and a fork copies none of its rows."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    org_id = admin["org_id"]
    plain = _run(_source_name(_run(_attach_source_with_connection(report["id"], "system_only", attach=False))))
    rls = _run(_source_name(_run(_attach_rls_relation(report["id"], attach=False))))
    _run(_set_step_code(seeded["step_ids"][0], NAMESPACE_CLIENTS_CODE.format(plain=plain, rls=rls)))

    assert _report_flags(test_client, report["id"], owner, org_id)[0] is True

    monkeypatch.setattr("app.core.fire_and_forget.spawn", lambda job: None)
    fstep = _fork_default_step_as(test_client, report["id"], viewer, org_id)
    assert not (fstep["data"] or {}).get("rows"), "the fork received the owner's rows"


@pytest.mark.e2e
def test_a_stored_owner_credential_result_gives_way_to_the_viewers_own_older_result(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The viewer ran the step as themselves; a newer result stored under the
    owner's credentials over a row-filtered relation is not theirs to see.
    The page serves the viewer's own result instead of none."""
    from app.ai.code_execution.query_params import params_fingerprint

    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    ds_id = _run(_attach_rls_relation(report["id"], attach=False, buildable=True))
    _run(_name_source_in_step_code(report["id"], ds_id))
    qid, sid = seeded["query_ids"][0], seeded["step_ids"][0]
    year = {"name": "Year", "type": "string", "label": "Year", "default": "2024", "required": False,
            "source": "input", "identity_binding": None, "options": None, "options_source": None,
            "strict_options": False}
    _run(_set_query_params(qid, [year]))
    # Seeded directly: a viewer run needs a reachable source, and the API no
    # longer stores an owner-credential result for this report. The viewer's
    # own run predates the query's parameter (no fingerprint); the newer one
    # ran with its default value.
    now = datetime.utcnow()
    _run(_seed_viewer_result(sid, viewer["email"], "viewer", month="viewers-own",
                             last_run_at=now - timedelta(hours=1)))
    _run(_seed_viewer_result(sid, viewer["email"], "creator", month="owners-slice",
                             params_fingerprint=params_fingerprint({"Year": "2024"}), last_run_at=now))

    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert {r["month"] for r in (step["data"] or {}).get("rows") or []} == {"viewers-own"}, step["data"]
    assert step["viewer_result"]["executed_as"] == "viewer"


@pytest.mark.e2e
def test_viewer_step_run_building_its_own_clients_filters_rows_for_the_viewer(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    """StepService.run_step_to_user_result with no prebuilt clients builds
    them itself: under the owner's credentials, the rows still filter for the
    viewer whose result it writes. (Called directly: the HTTP routes always
    hand it prebuilt clients.)"""
    from sqlalchemy import select as _select
    from sqlalchemy.orm import selectinload
    from app.models.user import User as _User
    from app.services.step_service import StepService

    admin, owner, viewer, report, seeded = _report_over_a_row_filtered_relation(
        test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, attach=True)

    async def run():
        async with async_session_maker() as db:
            loaded = (await db.execute(
                _select(Report).options(selectinload(Report.data_sources), selectinload(Report.files))
                .where(Report.id == report["id"])
            )).scalar_one()
            row = await StepService().run_step_to_user_result(
                db, seeded["step_ids"][0],
                run_user=await db.get(_User, viewer["user_id"]),
                credential_user=await db.get(_User, owner["user_id"]),
                executed_as="creator", report=loaded, db_clients=None,
            )
            return row.status, row.status_reason, row.data

    status, reason, data = _run(run())
    assert status == "success", reason
    assert {r["owner_email"] for r in data["rows"]} == {viewer["email"]}


@pytest.mark.e2e
def test_result_stored_from_the_owners_credentials_is_not_served_on_a_row_filtered_report(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A viewer result run under the owner's credentials on a report that
    reads a row-filtered relation holds the owner's slice. Neither the step
    read nor the query run's cache serves it; the run executes again."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    ds_id = _run(_attach_rls_relation(report["id"], attach=False, buildable=True))
    _run(_name_source_in_step_code(report["id"], ds_id))
    qid, sid = seeded["query_ids"][0], seeded["step_ids"][0]
    # Seeded directly: the API no longer writes a creator-credential result
    # for such a report, but rows stored before that rule remain.
    _run(_seed_viewer_result(sid, viewer["email"], executed_as="creator"))

    step = _public_step(test_client, report["id"], qid, token=viewer["token"])
    assert not (step["data"] or {}).get("rows"), step["data"]
    assert step["snapshot_withheld"] is True

    resp = test_client.post(
        f"/api/queries/{qid}/run",
        json={"mode": "viewer", "params": {}},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cached"] is False, body
    assert {r["month"] for r in body["data"]["rows"]} == FRESH_MONTHS


@pytest.mark.e2e
def test_fork_eligibility_on_unattached_report_follows_the_sources_its_code_names(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A forker needs access to the organization sources the code names; a
    restricted source the code does not name does not stand in the way."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    restricted_id = _run(_attach_source_with_connection(
        report["id"], "system_only", is_public=False, attach=False))

    def eligibility():
        resp = test_client.get(f"/api/r/{report['id']}", headers=_headers(viewer["token"], admin["org_id"]))
        assert resp.status_code == 200, resp.json()
        return resp.json()["fork_eligibility"]

    assert eligibility()["can_fork"] is True

    _run(_name_source_in_step_code(report["id"], restricted_id))
    elig = eligibility()
    assert elig["can_fork"] is False
    assert elig["reason"] == "no_data_source_access"


# The restricted source's name appears in a comment, in a SQL column and in a
# string; the only client the code reaches is a plain source's, by key.
NAME_IN_SQL_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    # totals per {restricted}
    sql = 'SELECT amount AS "{restricted}" FROM orders'
    if False:
        ds_clients["{plain}"].execute_query(sql)
    return pd.DataFrame({{"month": ["2024-01", "2024-02"], "revenue": [10, 20]}})
"""


@pytest.mark.e2e
def test_fork_eligibility_ignores_a_source_name_that_is_not_a_client_key(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A restricted source whose name appears only in a comment, a SQL column
    or a string is not one the report reaches: the forker without access to
    it may still fork. Reaching it by key then refuses the fork."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    restricted_id = _run(_attach_source_with_connection(
        report["id"], "system_only", is_public=False, attach=False))
    plain_id = _run(_attach_source_with_connection(report["id"], "system_only", attach=False))
    restricted, plain = _run(_source_name(restricted_id)), _run(_source_name(plain_id))
    _run(_set_step_code(seeded["step_ids"][0], NAME_IN_SQL_CODE.format(restricted=restricted, plain=plain)))

    def eligibility():
        resp = test_client.get(f"/api/r/{report['id']}", headers=_headers(viewer["token"], admin["org_id"]))
        assert resp.status_code == 200, resp.json()
        return resp.json()["fork_eligibility"]

    assert eligibility()["can_fork"] is True

    _run(_name_source_in_step_code(report["id"], restricted_id))
    elig = eligibility()
    assert elig["can_fork"] is False
    assert elig["reason"] == "no_data_source_access"


# Reaches every client it was handed without naming any source.
UNNAMED_SOURCES_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    for key, client in ds_clients.items():
        pass
    return pd.DataFrame({"month": ["2024-01", "2024-02"], "revenue": [10, 20]})
"""


@pytest.mark.e2e
@pytest.mark.parametrize("kind", ["row_filtered", "per_user"])
def test_a_step_naming_no_source_counts_every_organization_source(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch, kind,
):
    """Each step runs on the organization sources its own code names, or on
    all of them when it names none. A sibling step naming a plain source does
    not narrow a step that reaches its clients without naming them: the
    report counts as reading the row-filtered or per-user source, and a fork copies none of
    that step's rows (nor, over a per-user connection, its code)."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    org_id = admin["org_id"]
    plain_id = _run(_attach_source_with_connection(report["id"], "system_only", attach=False))
    if kind == "row_filtered":
        _run(_attach_rls_relation(report["id"], attach=False))
    else:
        _run(_attach_user_scoped_source(report["id"], attach=False))
    named_step, unnamed_step = seeded["step_ids"]
    _run(_set_step_code(named_step, _source_key_reference(_run(_source_name(plain_id))) + GOOD_CODE))
    _run(_set_step_code(unnamed_step, UNNAMED_SOURCES_CODE))

    has_rls, has_user_scoped = _report_flags(test_client, report["id"], owner, org_id)
    assert (has_rls if kind == "row_filtered" else has_user_scoped) is True

    # Captured, then run once the creation-time state is read.
    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    resp = test_client.post(f"/api/reports/{report['id']}/fork", json={},
                            headers=_headers(viewer["token"], org_id))
    assert resp.status_code == 200, resp.json()
    fork_queries = test_client.get(f"/api/queries?report_id={resp.json()['id']}",
                                   headers=_headers(viewer["token"], org_id)).json()
    copy = next(q for q in fork_queries if q["title"] == "Query 1")
    fstep = test_client.get(f"/api/queries/{copy['id']}/default_step",
                            headers=_headers(viewer["token"], org_id)).json()["step"]
    assert not (fstep["data"] or {}).get("rows"), "the fork received the owner's rows"
    if kind == "per_user":
        assert not (fstep.get("code") or "").strip(), "the fork carried the source SQL"
        _assert_hydration_runs_as_the_forker(test_client, spawned, copy["id"], viewer, org_id)
    else:
        # Over a system-only connection with a row-filtered relation the code
        # is copied and nothing is left to hydrate.
        assert not spawned, "a fork with nothing withheld scheduled hydration"


async def _add_superseded_step(query_id: str, code: str):
    """A failed earlier attempt of the query, no longer its default step.
    Seeded directly: a failed builder run leaves one behind, and producing it
    through the API needs the custom-code permission and a failing source."""
    async with async_session_maker() as db:
        query = await db.get(Query, query_id)
        db.add(Step(
            title="Earlier attempt", slug=f"earlier-{uuid.uuid4().hex[:8]}", status="error",
            widget_id=query.widget_id, query_id=query.id, code=code,
            created_at=datetime.utcnow() - timedelta(days=1),
        ))
        await db.commit()


@pytest.mark.e2e
def test_only_the_steps_a_report_serves_decide_its_run_sources(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A failed earlier attempt that is no longer any query's step does not
    run and is not served, so it does not widen the sources the report runs
    on: a plain report stays plain and 'run on my behalf' stays available."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    plain_id = _run(_attach_source_with_connection(report["id"], "system_only", attach=False))
    _run(_attach_rls_relation(report["id"], attach=False))
    _run(_set_step_code(seeded["step_ids"][0], _source_key_reference(_run(_source_name(plain_id))) + GOOD_CODE))
    _run(_add_superseded_step(seeded["query_ids"][0], UNNAMED_SOURCES_CODE))

    assert _report_flags(test_client, report["id"], owner, admin["org_id"])[0] is False
    resp = test_client.put(
        f"/api/reports/{report['id']}/visibility/artifact",
        json={"visibility": "internal", "run_identity": "creator"},
        headers=_headers(owner["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text


def _identity_spec(name: str) -> dict:
    return {"name": name, "type": "string", "label": name, "default": None, "required": False,
            "source": "identity", "identity_binding": "viewer.email",
            "options": None, "options_source": None, "strict_options": False}


async def _set_step_data(step_id: str, data: dict):
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        step.data = data
        await db.commit()


LOAD_STEP_CODE = """
def generate_df(ds_clients, excel_files):
    return load_step('{step_id}')
"""


@pytest.mark.e2e
def test_run_on_my_behalf_loads_other_steps_as_the_viewer(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Under the owner's credentials, a step that load_step()s an
    identity-parameter step still reads it as the viewer: the viewer's result
    never holds the owner's slice of the step it loads."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    resp = test_client.put("/api/organization/settings",
                           json={"config": {"enable_load_step": {"value": True}}},
                           headers=_headers(admin["token"], admin["org_id"]))
    assert resp.status_code == 200, resp.text
    scoped_q, loader_q = seeded["query_ids"]
    scoped_step, loader_step = seeded["step_ids"]
    marker = f"owner-slice-{uuid.uuid4().hex[:6]}"
    _run(_set_query_params(scoped_q, [_identity_spec("UserEmail")]))
    _run(_set_step_data(scoped_step, {"rows": [{"month": marker, "revenue": 1}],
                                      "columns": [{"field": "month"}, {"field": "revenue"}]}))
    _run(_set_step_code(loader_step, LOAD_STEP_CODE.format(step_id=scoped_step)))
    _set_artifact_visibility(test_client, report["id"], owner, "internal", run_identity="creator")

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["executed_as"] == "creator"
    for qid in (scoped_q, loader_q):
        served = test_client.get(f"/api/r/{report['id']}/queries/{qid}/step",
                                 headers=_headers(viewer["token"]))
        assert served.status_code == 200, served.text
        assert marker not in served.text, f"query {qid} served the owner's slice to the viewer"


LOAD_STEP_NAMING_CODE = """
def _reach_source(ds_clients):
    return ds_clients["{plain}"]


def generate_df(ds_clients, excel_files):
    return load_step('{step_id}')
"""


def _report_with_a_step_loading_a_row_filtered_step(test_client, create_report, bootstrap_admin,
                                                     invite_user_to_org):
    """An unattached report: step A names a source with a row-filtered
    relation, step B names a plain source and load_step()s A, step C names
    the plain source only. Returns (admin, viewer, report, (A, B, C) query
    ids, (A, B, C) step ids)."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=3,
    )
    rls_id = _run(_attach_rls_relation(report["id"], attach=False, buildable=True))
    plain_id = _run(_attach_source_with_connection(report["id"], "system_only", attach=False))
    rls_name, plain_name = _run(_source_name(rls_id)), _run(_source_name(plain_id))
    a, b, c = seeded["step_ids"]
    _run(_set_step_code(a, _source_key_reference(rls_name) + GOOD_CODE))
    _run(_set_step_code(b, LOAD_STEP_NAMING_CODE.format(plain=plain_name, step_id=a)))
    _run(_set_step_code(c, _source_key_reference(plain_name) + GOOD_CODE))
    return admin, viewer, report, seeded["query_ids"], seeded["step_ids"]


@pytest.mark.e2e
def test_a_step_reads_the_sources_of_the_steps_it_loads(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """A step that load_step()s a step over a row-filtered relation holds that
    step's rows as the owner saw them: its snapshot is withheld from a viewer
    like the loaded step's. A plain step loading nothing is still served."""
    admin, viewer, report, (qa, qb, qc), _ = _report_with_a_step_loading_a_row_filtered_step(
        test_client, create_report, bootstrap_admin, invite_user_to_org)

    for qid in (qa, qb):
        step = _public_step(test_client, report["id"], qid, token=viewer["token"])
        assert step["snapshot_withheld"] is True, qid
        assert not (step["data"] or {}).get("rows"), qid
    plain = _public_step(test_client, report["id"], qc, token=viewer["token"])
    assert plain["snapshot_withheld"] is False
    assert {r["month"] for r in plain["data"]["rows"]} == {"stale"}


@pytest.mark.e2e
def test_a_creator_result_of_a_step_loading_a_row_filtered_step_is_not_served(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, viewer, report, (qa, qb, qc), (sa, sb, sc) = _report_with_a_step_loading_a_row_filtered_step(
        test_client, create_report, bootstrap_admin, invite_user_to_org)
    # Seeded directly: viewer runs here no longer execute as the creator, but
    # a result stored before that remains.
    _run(_seed_viewer_result(sb, viewer["email"], executed_as="creator"))

    step = _public_step(test_client, report["id"], qb, token=viewer["token"])
    assert not (step["data"] or {}).get("rows"), step["data"]

    resp = test_client.post(
        f"/api/queries/{qb}/run", json={"mode": "viewer", "params": {}},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cached"] is False, resp.json()
    assert "mine" not in resp.text


async def _clear_default_step(query_id: str) -> str:
    """A legacy query with no default step: what it serves is its latest
    successful step (query_service.get_default_step_for_query)."""
    async with async_session_maker() as db:
        query = await db.get(Query, query_id)
        step_id = str(query.default_step_id)
        query.default_step_id = None
        await db.commit()
        return step_id


@pytest.mark.e2e
def test_a_query_without_a_default_step_counts_the_step_it_serves(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    plain_id = _run(_attach_source_with_connection(report["id"], "system_only", attach=False))
    rls_id = _run(_attach_rls_relation(report["id"], attach=False))
    _run(_set_step_code(seeded["step_ids"][0], _source_key_reference(_run(_source_name(plain_id))) + GOOD_CODE))
    _run(_set_step_code(seeded["step_ids"][1], _source_key_reference(_run(_source_name(rls_id))) + GOOD_CODE))
    _run(_clear_default_step(seeded["query_ids"][1]))

    assert _report_flags(test_client, report["id"], owner, admin["org_id"])[0] is True


async def _replace_default_step(query_id: str, code: str):
    """The query's next successful run: a new step (new id and slug) becomes
    its default. Seeded directly: through the API this is a builder run, which
    needs the custom-code permission and a reachable source."""
    async with async_session_maker() as db:
        query = await db.get(Query, query_id)
        step = Step(title="Next run", slug=f"next-{uuid.uuid4().hex[:8]}", status="success",
                    widget_id=query.widget_id, query_id=query.id, code=code, data=STALE_DATA)
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()


@pytest.mark.e2e
@pytest.mark.parametrize("case", ["loaded_step_superseded", "full_width_underscore", "loaded_step_has_no_code"])
def test_a_step_loading_a_row_filtered_step_stays_withheld(
    test_client, create_report, bootstrap_admin, invite_user_to_org, case,
):
    """Step A names a plain source and load_step()s B, whose sources include a
    row-filtered relation, so A's snapshot holds B's rows as the owner saw
    them and stays withheld from a viewer -- also when B's query has since run
    a new step naming only the plain source (A's snapshot still holds the old
    B), when the call is spelled with a full-width underscore (Python reads
    `load＿step` as `load_step`), and when B has no code to name a source
    (so all the organization's sources count)."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    rls_name = _run(_source_name(_run(_attach_rls_relation(report["id"], attach=False))))
    plain_name = _run(_source_name(_run(_attach_source_with_connection(
        report["id"], "system_only", attach=False))))
    (qa, qb), (sa, sb) = seeded["query_ids"], seeded["step_ids"]
    call = "load＿step" if case == "full_width_underscore" else "load_step"
    _run(_set_step_code(sa, _source_key_reference(plain_name) + "def generate_df(ds_clients, excel_files):\n"
                            f"    return {call}('{sb}')\n"))
    _run(_set_step_code(sb, "" if case == "loaded_step_has_no_code" else _source_key_reference(rls_name) + GOOD_CODE))
    if case == "loaded_step_superseded":
        _run(_replace_default_step(qb, _source_key_reference(plain_name) + GOOD_CODE))

    step = _public_step(test_client, report["id"], qa, token=viewer["token"])
    assert step["snapshot_withheld"] is True
    assert not (step["data"] or {}).get("rows")


SOURCE_BOUND_CODE = """
def generate_df(ds_clients, excel_files):
    client = ds_clients["Delegated:delegated"]
    return client.execute_query("EVALUATE 'Orders'")
"""


async def _set_step_code(step_id: str, code: str):
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        step.code = code
        await db.commit()


async def _set_step_applied_params(step_id: str, applied: dict):
    """The values a seeded snapshot was materialized with."""
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        step.applied_params = applied
        await db.commit()


async def _set_query_identity_param(query_id: str):
    """Declare an identity-sourced param alongside an ordinary one."""
    async with async_session_maker() as db:
        q = await db.get(Query, query_id)
        q.parameters = [
            {"name": "month", "type": "string", "source": "input"},
            {"name": "owner_email", "type": "string", "source": "identity"},
        ]
        await db.commit()


async def _report_status(report_id: str) -> str:
    async with async_session_maker() as db:
        return (await db.get(Report, report_id)).status


@pytest.mark.e2e
def test_fork_hydration_with_no_access_removes_the_fork_against_a_real_db(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """hydrate_fork end to end, on the real session and schema.

    The unit tests drive it with a stub session, which cannot catch what only
    the database enforces — foreign keys, cascades, relationship loading. Here
    the delegated connection is unreachable, so every query fails under the
    forker's credentials and the fork must be retired, the same way a user's
    own delete retires a report (archived, never hard-deleted).
    """
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))
    # GOOD_CODE never touches ds_clients, so it "succeeds" for anyone and
    # proves nothing about access. Real step code reaches its source through
    # its client — all 144 steps in a live install do — and a forker with no
    # usable identity on the source has no client under that key.
    _run(_set_step_code(seeded["step_ids"][0], SOURCE_BOUND_CODE))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)

    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]
    assert len(spawned) == 1

    outcome = _run(spawned[0])
    assert outcome["succeeded"] == 0 and outcome["deleted"] is True, outcome

    async def _status():
        async with async_session_maker() as db:
            return (await db.get(Report, fork_id)).status

    assert _run(_status()) == "archived"
    # The source report is untouched by its fork's retirement.
    async def _src_status():
        async with async_session_maker() as db:
            return (await db.get(Report, report["id"])).status
    assert _run(_src_status()) != "archived"


@pytest.mark.e2e
def test_fork_hydration_success_writes_code_and_rows_against_a_real_db(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """The mirror of the no-access case: a step the forker CAN run gets its
    code and its (forker-owned) rows committed by rerun_step's code_override,
    on the real session — the unit test only proves this against a stub.

    GOOD_CODE needs no client, so it runs for anyone; that is exactly what
    makes it usable here to drive the success path deterministically.
    """
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)

    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]

    outcome = _run(spawned[0])
    assert outcome == {"succeeded": 1, "failed": 0, "deleted": False}, outcome

    fork_q = test_client.get(
        f"/api/queries?report_id={fork_id}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()[0]
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]

    assert (fstep.get("code") or "").strip() == GOOD_CODE.strip()
    # The forker's own fresh run — not the source owner's stale snapshot.
    assert {r["month"] for r in fstep["data"]["rows"]} == FRESH_MONTHS


async def _make_artifact_id_keyed(report_id: str):
    """Give the seeded artifact what real dashboards carry: its visualization
    ids baked into the source (`vizById("<uuid>")` in code, and the doc-mode
    markdown equivalent) — not just the `visualization_ids` list. The seed's
    placeholder `function App() {}` references nothing, which is why no fork
    test ever noticed the ids in the code going stale."""
    async with async_session_maker() as db:
        art = (await db.execute(
            __import__("sqlalchemy").select(ArtifactVersion).where(ArtifactVersion.report_id == report_id)
        )).scalars().first()
        ids = list((art.content or {}).get("visualization_ids") or [])
        code = "function App() {\n" + "".join(
            f'  const v{i} = vizById("{vid}");\n' for i, vid in enumerate(ids)
        ) + "  return null;\n}"
        md = "".join(f"{{{{viz:{vid}}}}}\n" for vid in ids)
        art.content = {**(art.content or {}), "code": code, "markdown": md,
                       "file_ids": ["file-untouched"]}
        await db.commit()
        return ids


@pytest.mark.e2e
def test_fork_remaps_visualization_ids_baked_into_the_dashboard(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Every fork of a vizById dashboard rendered EMPTY: the fork remapped the
    `visualization_ids` list to its own visualizations but left the code asking
    `vizById(<source id>)`, and vizById only searches the fork's own data — so
    it returned null for every chart. Independent of the source's auth policy,
    so this runs on a plain system-only report."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    source_viz_ids = _run(_make_artifact_id_keyed(report["id"]))
    assert len(source_viz_ids) == 2

    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]

    async def _fork_state():
        from sqlalchemy import select
        async with async_session_maker() as db:
            art = (await db.execute(select(ArtifactVersion).where(ArtifactVersion.report_id == fork_id))).scalars().first()
            own = {v.id for v in (await db.execute(
                select(Visualization).where(Visualization.report_id == fork_id))).scalars().all()}
            return art.content, own

    content, fork_viz_ids = _run(_fork_state())
    assert len(fork_viz_ids) == 2

    blob = __import__("json").dumps(content)
    # No trace of the source's visualizations anywhere in the fork's dashboard…
    for vid in source_viz_ids:
        assert vid not in blob, f"fork dashboard still references source viz {vid}"
    # …and every reference now resolves to one of the fork's own charts.
    for vid in fork_viz_ids:
        assert f'vizById("{vid}")' in content["code"]
        assert f"{{{{viz:{vid}}}}}" in content["markdown"]
    assert set(content["visualization_ids"]) == fork_viz_ids
    # Non-visualization ids pass through untouched.
    assert content["file_ids"] == ["file-untouched"]


# ── Parameterized dashboards ────────────────────────────────────────────────
#
# The shape that broke in a live fork: a dashboard query filtered by a
# parameter (`depot`) whose dropdown options come from a SECOND query in the
# same report. The fork dropped Query.parameters, so the saved code's
# `params["depot"]` raised KeyError — and hydration reported it as "no access"
# to a forker who had just run the same query successfully as a viewer.

PARAM_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    depot = params["depot"]
    return pd.DataFrame({"month": ["2024-01", "2024-02"], "revenue": [10, 20]})
"""


async def _make_second_query_parameterized(query_ids: list, step_ids: list):
    """Query[1] gets a `depot` parameter whose options come from query[0],
    and step code that reads it by name — as real dashboard code does."""
    async with async_session_maker() as db:
        options_q = await db.get(Query, query_ids[0])
        user_q = await db.get(Query, query_ids[1])
        user_q.parameters = [{
            "name": "depot", "type": "string", "label": "Depot",
            "default": None, "required": False, "source": "input",
            "identity_binding": None, "options": None,
            "options_source": {
                "query_id": str(options_q.id),
                "value_column": "month", "label_column": "month",
            },
        }]
        options_qid = str(options_q.id)
        await db.commit()
    # Separate unit of work: Query.steps and Query.default_step point at each
    # other, so dirtying a query and its step in one flush is a cycle.
    async with async_session_maker() as db:
        step = await db.get(Step, step_ids[1])
        step.code = PARAM_CODE
        await db.commit()
    return options_qid


async def _fork_query_params(fork_id: str):
    from sqlalchemy import select
    async with async_session_maker() as db:
        qs = (await db.execute(select(Query).where(Query.report_id == fork_id))).scalars().all()
        return {str(q.id): (q.title, q.parameters) for q in qs}


@pytest.mark.e2e
def test_fork_carries_parameters_with_options_repointed_to_the_fork(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The fork keeps the parameter definition, and its dropdown reads the
    FORK's copy of the options query — never the source report's."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    source_options_qid = _run(_make_second_query_parameterized(
        seeded["query_ids"], seeded["step_ids"]))

    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_queries = _run(_fork_query_params(resp.json()["id"]))

    with_params = [p for _, p in fork_queries.values() if p]
    assert len(with_params) == 1, f"fork lost the query parameters: {fork_queries}"
    spec = with_params[0][0]
    assert spec["name"] == "depot"
    target = spec["options_source"]["query_id"]
    assert target != source_options_qid, "dropdown still reads the SOURCE report's query"
    assert target in fork_queries, "dropdown points at a query that is not in the fork"


@pytest.mark.e2e
def test_fork_hydration_runs_parameterized_queries_against_a_real_db(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """The live regression, end to end on a delegated source: a forker with
    access must get BOTH queries — including the one that reads a parameter —
    rather than a KeyError reported to them as 'no access'."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    _run(_attach_user_scoped_source(report["id"]))
    _run(_make_second_query_parameterized(seeded["query_ids"], seeded["step_ids"]))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]

    outcome = _run(spawned[0])
    assert outcome == {"succeeded": 2, "failed": 0, "deleted": False}, outcome

    async def _param_step_code():
        from sqlalchemy import select
        async with async_session_maker() as db:
            qs = (await db.execute(select(Query).where(Query.report_id == fork_id))).scalars().all()
            q = next(q for q in qs if q.parameters)
            return (await db.get(Step, q.default_step_id)).code

    assert "params[\"depot\"]" in (_run(_param_step_code()) or "")


# ── The fork replays the creator's filter values, not the declared defaults ─
#
# The live case behind this: a delegated Power BI dashboard whose year filter
# had no default. The creator only ever ran it with a year selected. The fork
# dropped applied_params for a delegated source, so hydration ran the copied
# code with year empty — a branch nobody had exercised, whose DAX named a table
# the model does not have. Every query failed, the forker got an empty
# dashboard labelled as a credentials problem, and the same user ran the same
# queries on the source report fine seconds later.

UNTESTED_BRANCH_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    year = params.get("year")
    if not year:
        # The branch the creator never ran: it names a table the model lacks.
        raise RuntimeError("DAX query failed: HTTP 400 Cannot find table 'calendardates'.")
    return pd.DataFrame({"month": ["2024-01", "2024-02"], "revenue": [10, 20]})
"""


async def _make_query_year_filtered(query_id: str, step_id: str):
    async with async_session_maker() as db:
        q = await db.get(Query, query_id)
        q.parameters = [{
            "name": "year", "type": "number", "label": "Year",
            "default": None, "required": False, "source": "input",
            "identity_binding": None, "options": None, "options_source": None,
        }]
        await db.commit()
    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        step.code = UNTESTED_BRANCH_CODE
        step.applied_params = {"year": 2026}
        await db.commit()


@pytest.mark.e2e
def test_fork_hydration_replays_the_creators_filter_values(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """A delegated fork's first run executes with the filter state the
    dashboard was built with. With that state dropped, the copied code ran
    under the declared defaults — a run the creator never made — and failed."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))
    _run(_make_query_year_filtered(seeded["query_ids"][0], seeded["step_ids"][0]))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]

    async def _fork_step():
        from sqlalchemy import select
        async with async_session_maker() as db:
            q = (await db.execute(select(Query).where(Query.report_id == fork_id))).scalars().first()
            st = await db.get(Step, q.default_step_id)
            return {"code": st.code, "applied_params": st.applied_params,
                    "status": st.status, "status_reason": st.status_reason}

    # The copy itself carries the filter state — before any run.
    before = _run(_fork_step())
    assert before["code"] == "" and before["applied_params"] == {"year": 2026}, before

    from unittest.mock import MagicMock
    hydration_log = MagicMock()
    monkeypatch.setattr("app.services.fork_service.logger", hydration_log)
    outcome = _run(spawned[0])
    after = _run(_fork_step())
    assert outcome == {"succeeded": 1, "failed": 0, "deleted": False}, (
        outcome, after, [c.args for c in hydration_log.info.call_args_list],
    )

    fork_q = test_client.get(
        f"/api/queries?report_id={fork_id}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()[0]
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]

    assert fstep["status"] == "success", fstep.get("status_reason")
    assert (fstep.get("applied_params") or {}).get("year") == 2026, (
        "the fork's first run did not carry the creator's filter value"
    )
    assert {r["month"] for r in fstep["data"]["rows"]} == FRESH_MONTHS


# ── The fork page waits for hydration, and nothing runs twice ───────────────


@pytest.mark.e2e
def test_fork_status_reports_hydration_until_it_settles(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """The fork is returned before its queries have run; the page polls
    fork_status and waits rather than rendering the empty dashboard it would
    otherwise catch mid-hydration."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    fork_id = resp.json()["id"]
    status_url = f"/api/reports/{fork_id}/fork_status"
    hdrs = _headers(viewer["token"], admin["org_id"])

    assert test_client.get(status_url, headers=hdrs).json() == {"hydrating": True}
    _run(spawned[0])
    assert test_client.get(status_url, headers=hdrs).json() == {"hydrating": False}


@pytest.mark.e2e
def test_fork_with_a_blank_code_step_never_reports_itself_as_hydrating(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """A step with no code is not something hydration can run.

    Every step of a delegated fork was marked 'pending', but only steps with
    code to re-run were handed to hydrate_fork — so a blank one was never
    settled and the fork read as hydrating until the five-minute staleness
    cutoff: the page sat on "Preparing your copy…" for its full poll window and
    refresh-on-view stayed off the report. With no code and no rows there is
    nothing to withhold either, so such a step is born settled.
    """
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))
    _run(_set_step_code(seeded["step_ids"][0], ""))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    fork_id = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["id"]

    # Nothing to hydrate at all — so nothing may be left waiting on it.
    assert not spawned, "hydration was scheduled for a fork with no code to run"
    status = test_client.get(
        f"/api/reports/{fork_id}/fork_status",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()
    assert status == {"hydrating": False}, (
        "a fork whose steps hydration will never touch reported itself as "
        "hydrating, stranding the page on its waiting state"
    )


@pytest.mark.e2e
def test_system_only_fork_carries_applied_params_with_its_copied_rows(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """applied_params travels with `data`, or the copied snapshot lies.

    A system-only fork copies the creator's rows as-is. Those rows may already
    be narrowed by the values the snapshot ran with, and applied_params is the
    only record of that. Dropped, the dashboard reads the snapshot as
    unfiltered: ArtifactFrame's last-resort option tier derives filter choices
    from it and offers the single value it was filtered to. Harmless while the
    fork dropped Query.parameters too; live now that it copies them.
    """
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_set_step_applied_params(seeded["step_ids"][0], {"month": "2024-01"}))

    fork_id = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["id"]

    fork_q = test_client.get(
        f"/api/queries?report_id={fork_id}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()[0]
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]

    assert (fstep.get("data") or {}).get("rows"), "precondition: rows were copied"
    assert (fstep.get("applied_params") or {}).get("month") == "2024-01", (
        "the copied snapshot lost the values it was materialized with"
    )


@pytest.mark.e2e
def test_fork_never_carries_an_identity_derived_applied_param(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The boundary the copy must not cross. An identity-sourced param's
    applied value names the CREATOR — their email, department, group list. It
    is dropped on the fork's copy exactly as redact_applied_params drops it on
    a reader's, while the ordinary values stay."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_set_query_identity_param(seeded["query_ids"][0]))
    _run(_set_step_applied_params(
        seeded["step_ids"][0], {"month": "2024-01", "owner_email": "creator@example.com"},
    ))

    fork_id = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["id"]

    fork_q = test_client.get(
        f"/api/queries?report_id={fork_id}",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()[0]
    fstep = test_client.get(
        f"/api/queries/{fork_q['id']}/default_step",
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["step"]

    applied = fstep.get("applied_params") or {}
    assert "owner_email" not in applied, (
        "the fork carried the creator's identity into the forker's copy"
    )
    assert applied.get("month") == "2024-01", "the ordinary value was dropped too"


# ── A strict fork's first run starts from the creator's filter values ───────
#
# A strict fork (delegated or RLS) drops the creator's rows, and used to drop
# the values they were materialized with alongside them — so the forker's first
# run resolved from the declared defaults, not the year / month / agent the
# dashboard was showing. The values now travel on their own, through the same
# gate the read path applies to this forker.

MONTH_PARAM_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"month": [params["month"]], "revenue": [10]})
"""

MONTH_PARAM = {
    "name": "month", "type": "string", "label": "Month",
    "default": "2024-01", "required": False, "source": "input",
    "identity_binding": None, "options": None, "options_source": None,
}


async def _set_query_params(query_id: str, specs: list):
    async with async_session_maker() as db:
        q = await db.get(Query, query_id)
        q.parameters = specs
        await db.commit()


async def _seed_viewer_success(step_id: str, user_id: str, report_id: str):
    """A successful per-viewer run — what makes the read path serve this
    viewer instead of withholding from them."""
    from app.services.viewer_result_store import store_viewer_result
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        await store_viewer_result(
            db, step_id=step_id, user_id=user_id, params_fingerprint="seed",
            organization_id=str(report.organization_id), report_id=str(report_id),
            status="success", status_reason=None,
            data={"rows": [{"month": "2024-02", "revenue": 1}],
                  "columns": [{"field": "month"}, {"field": "revenue"}]},
            executed_as="viewer", applied_params={"month": "2024-02"},
            last_run_at=datetime.utcnow(),
        )


async def _fork_default_step(fork_id: str) -> Step:
    from sqlalchemy import select
    async with async_session_maker() as db:
        q = (await db.execute(select(Query).where(Query.report_id == fork_id))).scalars().first()
        return await db.get(Step, q.default_step_id)


def _strict_month_report(test_client, create_report, bootstrap_admin, invite_user_to_org):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))
    _run(_set_query_params(seeded["query_ids"][0], [MONTH_PARAM]))
    _run(_set_step_code(seeded["step_ids"][0], MONTH_PARAM_CODE))
    _run(_set_step_applied_params(seeded["step_ids"][0], {"month": "2024-02"}))
    return admin, owner, viewer, report, seeded


@pytest.mark.e2e
def test_strict_fork_first_run_uses_the_creators_filter_values(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """The creator's dashboard shows month=2024-02 (default 2024-01). A forker
    the read path already serves gets a fork whose first run is 2024-02 too."""
    admin, owner, viewer, report, seeded = _strict_month_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org)
    _run(_seed_viewer_success(seeded["step_ids"][0], viewer["user_id"], report["id"]))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]

    outcome = _run(spawned[0])
    assert outcome == {"succeeded": 1, "failed": 0, "deleted": False}, outcome

    fstep = _run(_fork_default_step(fork_id))
    assert {r["month"] for r in fstep.data["rows"]} == {"2024-02"}, (
        "the fork's first run ignored the creator's filter and used the default"
    )
    assert (fstep.applied_params or {}).get("month") == "2024-02"


@pytest.mark.e2e
def test_strict_fork_carries_the_creators_filter_values_without_a_prior_run(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """Forking is usually the FIRST thing a reader does on a shared page, before
    any viewer run of their own exists. The filter values must travel anyway:
    gating them on a successful run (as the read path gates the snapshot) left
    exactly those forks on the declared defaults — the live failure, where a
    dashboard's untested empty-filter branch broke every query of the fork.
    The values are the dashboard's own filter state, not rows; identity-derived
    values are still stripped (see the test below)."""
    admin, owner, viewer, report, seeded = _strict_month_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org)

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    fork_id = resp.json()["id"]
    assert _run(_fork_default_step(fork_id)).applied_params == {"month": "2024-02"}

    outcome = _run(spawned[0])
    assert outcome["succeeded"] == 1, outcome
    fstep = _run(_fork_default_step(fork_id))
    assert {r["month"] for r in fstep.data["rows"]} == {"2024-02"}


@pytest.mark.e2e
def test_strict_fork_never_carries_an_identity_derived_applied_param(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """The identity boundary holds on the strict path as on the system-only
    one. Hydration is not run: it re-derives identity from the forker anyway;
    what is pinned here is what the fork itself was created holding."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))
    _run(_set_query_identity_param(seeded["query_ids"][0]))
    _run(_set_step_applied_params(
        seeded["step_ids"][0], {"month": "2024-02", "owner_email": "creator@example.com"},
    ))
    _run(_seed_viewer_success(seeded["step_ids"][0], viewer["user_id"], report["id"]))

    monkeypatch.setattr(
        "app.core.fire_and_forget.spawn", lambda coro: coro.close(),
    )
    resp = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()

    fstep = _run(_fork_default_step(resp.json()["id"]))
    assert fstep.applied_params == {"month": "2024-02"}, fstep.applied_params
    assert not (fstep.data or {}).get("rows"), "a strict fork carried the creator's rows"


@pytest.mark.e2e
def test_fork_queries_run_once_not_again_on_the_page_refresh(
    test_client, create_report, bootstrap_admin, invite_user_to_org, monkeypatch,
):
    """The dashboard's mount fires refresh-on-view, which used to rerun every
    query of a just-created fork: its last_run_at was empty, so the staleness
    gate saw stale data — the queries ran twice, the second time possibly
    while hydration was still writing the same steps. Now the rerun stays off
    the fork while hydration runs, and hydration's own last_run_at keeps it off
    afterwards."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    _run(_attach_user_scoped_source(report["id"]))

    spawned = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", spawned.append)
    fork_id = test_client.post(
        f"/api/reports/{report['id']}/fork", json={},
        headers=_headers(viewer["token"], admin["org_id"]),
    ).json()["id"]
    rerun_url = f"/api/r/{fork_id}/rerun"
    hdrs = _headers(viewer["token"], admin["org_id"])

    # Dashboard mounts while hydration is still running → no second run.
    during = test_client.post(rerun_url, headers=hdrs).json()
    assert during["skipped"] is True and "fork hydrating" in during["message"], during

    _run(spawned[0])

    # …and after it: hydration's run was the refresh, so the data is fresh.
    after = test_client.post(rerun_url, headers=hdrs).json()
    assert after["skipped"] is True and "data is fresh" in after["message"], after


# ── A viewer refused one dataset sees "no access", not the provider's error ─

REFUSED_CODE = """
def generate_df(ds_clients, excel_files):
    raise RuntimeError('DAX query failed: HTTP 401 {"error":{"model":"secret_orders_model"}}')
"""


@pytest.mark.e2e
def test_viewer_refused_one_dataset_gets_no_access_not_the_raw_provider_error(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """The live case: a viewer who can use the data source is refused ONE
    semantic model inside it. That refusal surfaces only at query time, and
    used to reach the dashboard verbatim — "DAX query failed: HTTP 401 {…}" —
    a status code instead of an explanation, and a response body that can
    name the refused model. It is now classified: a fixed "no access" reason
    and a machine-readable code, with nothing of the provider's text."""
    from app.services.access_errors import NO_ACCESS_REASON

    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=2,
    )
    refused_qid = seeded["query_ids"][1]
    _run(_set_step_code(seeded["step_ids"][1], REFUSED_CODE))

    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    assert resp.json()["steps_succeeded"] == 1 and resp.json()["steps_failed"] == 1

    step = _public_step(test_client, report["id"], refused_qid, token=viewer["token"])
    vr = step["viewer_result"]
    assert vr["status"] == "error"
    assert vr["status_reason"] == NO_ACCESS_REASON
    assert vr["error_code"] == "no_access"
    # The error surface carries nothing of the provider's response. (The
    # step's own `code` is out of scope here: this fixture is system-only, so
    # its code is visible to viewers by design — and it is where this test's
    # stand-in error literally spells the model name.)
    blob = __import__("json").dumps(vr)
    assert "secret_orders_model" not in blob and "HTTP 401" not in blob
