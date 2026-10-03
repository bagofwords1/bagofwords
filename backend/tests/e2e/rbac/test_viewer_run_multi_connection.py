"""Viewer runs on a data source with several connections, one of them missing.

A data source (agent) can hold several connections. When a viewer has no
credential for ONE of them, viewer runs must still build every other
connection's client — a chart on a shared-credential connection has nothing
to do with the viewer's missing Power BI sign-in.

Invariants under test:
- A chart on a connection the viewer CAN use succeeds, both in the dashboard
  run (POST /r/{id}/run) and in the single-query viewer run.
- A chart on the missing connection fails with that connection's own
  credential error — not a KeyError on the client key.
- The step sees the same client keys the owner does; the missing connection
  is present but fails only when used, so code that touches every client can
  never silently compute on a partial set.
- The missing connection is still reported in data_source_errors with the
  same code as today (the viewer gate's "connect" state keys off it), now
  naming the connection.
- construct_clients without the opt-in keeps today's all-or-nothing behavior
  for every other caller.

A data source whose ONLY connection is missing keeps today's behavior too —
covered by test_viewer_identity_mode_withholds_creator_snapshot_on_user_scoped_sources
in test_viewer_run_shared_artifacts.py.
"""
import uuid

import pytest
from fastapi import HTTPException

from app.dependencies import async_session_maker
from app.models.report import Report
from tests.e2e.rbac.test_viewer_run_shared_artifacts import (
    _headers,
    _public_step,
    _run,
    _set_step_code,
    _shared_report,
)


async def _attach_multi_connection_source(report_id: str) -> dict:
    """Attach one data source with two connections to the report:
    - a shared-credential (system_only) connection every viewer can use;
    - a per-user (user_required) connection no viewer has signed in to.
    The user_required connection uses the legacy per-user path (no OAuth
    mode), whose missing-credential error is classified credentials_required.
    """
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        shared = Connection(
            name=f"prod-pg-{suffix}", type="postgresql",
            # Complete enough to construct a client; construction never connects.
            config={"host": "localhost", "port": 5432, "database": "nope", "user": "nobody"},
            organization_id=report.organization_id, auth_policy="system_only",
        )
        personal = Connection(
            name=f"powerbi-{suffix}", type="powerbi",
            config={"auth_type": "service_principal"},
            organization_id=report.organization_id, auth_policy="user_required",
        )
        db.add_all([shared, personal])
        await db.flush()
        ds = DataSource(name=f"Sales {suffix}", organization_id=report.organization_id, is_public=True)
        db.add(ds)
        await db.flush()
        for conn in (shared, personal):
            await db.execute(domain_connection.insert().values(
                data_source_id=str(ds.id), connection_id=str(conn.id)))
        await db.execute(report_data_source_association.insert().values(
            report_id=str(report_id), data_source_id=str(ds.id)))
        await db.commit()
        return {
            "ds_id": str(ds.id),
            "ds_name": ds.name,
            "shared_conn": shared.name,
            "personal_conn": personal.name,
            "shared_key": f"{ds.name}:{shared.name}",
            "personal_key": f"{ds.name}:{personal.name}",
        }


def _uses_client_code(key: str) -> str:
    """Touches the client (so a missing key fails) without querying it — the
    shared connection points at no real database."""
    return (
        "def generate_df(ds_clients, excel_files):\n"
        "    import pandas as pd\n"
        f"    ds_clients[{key!r}]\n"
        "    return pd.DataFrame({'ok': [1]})\n"
    )


def _queries_client_code(key: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients[{key!r}].execute_query(\"EVALUATE 'Orders'\")\n"
    )


def _lists_keys_code(ds_name: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        "    import pandas as pd\n"
        f"    keys = sorted(k for k in ds_clients if k.startswith({ds_name + ':'!r}))\n"
        "    return pd.DataFrame({'key': keys})\n"
    )


def _setup(test_client, create_report, bootstrap_admin, invite_user_to_org):
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal", n_queries=3,
    )
    src = _run(_attach_multi_connection_source(report["id"]))
    shared_step, personal_step, keys_step = seeded["step_ids"]
    _run(_set_step_code(shared_step, _uses_client_code(src["shared_key"])))
    _run(_set_step_code(personal_step, _queries_client_code(src["personal_key"])))
    _run(_set_step_code(keys_step, _lists_keys_code(src["ds_name"])))
    return admin, owner, viewer, report, seeded, src


def _viewer_step(test_client, report, seeded, idx, viewer):
    return _public_step(test_client, report["id"], seeded["query_ids"][idx], token=viewer["token"])


@pytest.mark.e2e
def test_chart_on_usable_connection_succeeds_when_sibling_connection_is_missing(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded, src = _setup(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
    )
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()

    step = _viewer_step(test_client, report, seeded, 0, viewer)
    vr = step.get("viewer_result") or {}
    assert vr.get("status") == "success", (
        f"chart on the shared connection failed because a sibling connection "
        f"is missing: {vr.get('status_reason')!r}"
    )
    assert step["data"]["rows"] == [{"ok": 1}]


@pytest.mark.e2e
def test_chart_on_missing_connection_fails_with_that_connections_error(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded, src = _setup(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
    )
    test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))

    vr = _viewer_step(test_client, report, seeded, 1, viewer).get("viewer_result") or {}
    assert vr.get("status") == "error"
    assert "User credentials required" in (vr.get("status_reason") or ""), (
        f"expected the connection's own credential error, got {vr.get('status_reason')!r}"
    )


@pytest.mark.e2e
def test_step_sees_every_client_key_of_the_data_source(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded, src = _setup(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
    )
    test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))

    step = _viewer_step(test_client, report, seeded, 2, viewer)
    keys = {r["key"] for r in (step.get("data") or {}).get("rows") or []}
    assert keys == {src["shared_key"], src["personal_key"]}, (
        f"step saw {sorted(keys)} — the missing connection must be present "
        f"(and fail when used), never silently absent"
    )


@pytest.mark.e2e
def test_missing_connection_still_reported_in_data_source_errors(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded, src = _setup(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
    )
    resp = test_client.post(f"/api/r/{report['id']}/run", headers=_headers(viewer["token"]))
    assert resp.status_code == 200, resp.json()
    errors = resp.json()["data_source_errors"]

    assert len(errors) == 1, errors
    err = errors[0]
    assert err["code"] == "credentials_required"
    assert err["data_source_id"] == src["ds_id"]
    assert err["data_source"] == src["ds_name"]
    assert err.get("connection_name") == src["personal_conn"], err


@pytest.mark.e2e
def test_single_query_viewer_run_on_usable_connection_succeeds(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    admin, owner, viewer, report, seeded, src = _setup(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
    )
    resp = test_client.post(
        f"/api/queries/{seeded['query_ids'][0]}/run",
        json={"mode": "viewer", "params": {}},
        headers=_headers(viewer["token"], admin["org_id"]),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == "success", resp.json()


@pytest.mark.e2e
def test_construct_clients_default_stays_all_or_nothing(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Agent runs, owner reruns and fork hydration call construct_clients
    without the opt-in; for them a missing connection still fails the whole
    data source, exactly as before."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.data_source import DataSource
    from app.models.user import User
    from app.services.data_source_service import DataSourceService

    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org,
        visibility="internal",
    )
    src = _run(_attach_multi_connection_source(report["id"]))

    async def _build():
        async with async_session_maker() as db:
            ds = (await db.execute(
                select(DataSource).options(selectinload(DataSource.connections))
                .where(DataSource.id == src["ds_id"])
            )).scalar_one()
            viewer_u = await db.get(User, str(viewer["user_id"]))
            await DataSourceService().construct_clients(db, ds, current_user=viewer_u)

    with pytest.raises(HTTPException) as exc:
        _run(_build())
    assert exc.value.status_code == 403
