"""A viewer's stored result is served only while what its run read still holds.

A viewer's run lands in step_user_results and is served back to them (the
query run's cache, the shared page's step read). Its rows depend on more than
the step: the row policy of each accelerated relation it read, compiled for
that viewer; the extract it read; the auth policy of each connection behind
its sources; and the viewer's own attributes those policies read. When any of
them changes, the stored rows are no longer what a run would return, so they
are not served and the next run executes again.

The step, the accelerated relation and the source are seeded directly (no
public API creates the query graph or activates a custom table for an
agent without a live warehouse); every change under test goes through its
route: the row policy, the extract refresh, the connection's auth policy and
the viewer's groups.
"""
import sqlite3
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.report import Report
from tests.fixtures.identity_race import GROUPS_PARAM, group_joined_after_bind  # noqa: F401 (fixture)
from tests.fixtures.member_purge_spy import member_purges  # noqa: F401 (fixture)
from tests.e2e.rbac.test_viewer_run_shared_artifacts import (
    _headers,
    _public_step,
    _run,
    _set_step_code,
    _shared_report,
)

READ_CODE = """
# reads {source}
def generate_df(ds_clients, excel_files, params=None):
    fast = [client for key, client in ds_clients.items() if key.endswith("::fast")]
    return fast[0].execute_query('SELECT owner_email, amount FROM "{relation}" ORDER BY amount')
"""

OWN_ROWS = {"column": "owner_email", "source": "user.email", "op": "in"}


def _orders_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS orders (owner_email TEXT, amount INTEGER)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", rows)
    con.commit()
    con.close()


async def _seed_accelerated_source(report_id: str, db_path: str, relation: str) -> dict:
    """A system_only SQLite connection holding `orders`, an accelerated
    relation over it (not yet extracted) activated on a data source, and that
    source attached to the report."""
    from app.models.connection import Connection
    from app.models.connection_table import ConnectionTable, KIND_BOW
    from app.models.data_source import DataSource
    from app.models.datasource_table import DataSourceTable
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        conn = Connection(name=f"orders-{suffix}", type="sqlite", config={"database": db_path},
                          organization_id=report.organization_id, auth_policy="system_only")
        db.add(conn)
        await db.flush()
        ds = DataSource(name=f"Orders {suffix}", organization_id=report.organization_id, is_public=True)
        db.add(ds)
        await db.flush()
        ct = ConnectionTable(connection_id=str(conn.id), kind=KIND_BOW, name=relation,
                             definition_sql="SELECT owner_email, amount FROM orders",
                             columns=[], pks=[], fks=[])
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(name=relation, datasource_id=str(ds.id),
                               connection_table_id=str(ct.id), is_active=True))
        await db.execute(domain_connection.insert().values(
            data_source_id=str(ds.id), connection_id=str(conn.id)))
        await db.execute(report_data_source_association.insert().values(
            report_id=str(report_id), data_source_id=str(ds.id)))
        await db.commit()
        return {"conn": str(conn.id), "ds": str(ds.id), "ct": str(ct.id), "ds_name": ds.name}


def _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
                        *, policy=OWN_ROWS, params=None):
    """A shared report whose step reads an extracted, row-filtered relation
    holding one order of the owner's and one of the viewer's."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal")
    db_path = str(tmp_path / f"orders-{uuid.uuid4().hex[:6]}.sqlite")
    _orders_db(db_path, [(owner["email"], 1), (viewer["email"], 2)])
    relation = f"orders_{uuid.uuid4().hex[:6]}"
    src = _run(_seed_accelerated_source(report["id"], db_path, relation))
    h = _headers(admin["token"], admin["org_id"])
    on = test_client.put("/api/organization/settings",
                         json={"config": {"enable_custom_queries": {"value": True}}}, headers=h)
    assert on.status_code == 200, on.text
    ctx = {"admin": admin, "owner": owner, "viewer": viewer, "report": report, "db_path": db_path,
           "qid": seeded["query_ids"][0], "sid": seeded["step_ids"][0], **src}
    _refresh(test_client, ctx)
    if policy is not None:
        _set_policy(test_client, ctx, policy)
    _run(_set_step_code(ctx["sid"], READ_CODE.format(source=src["ds_name"], relation=relation)))
    if params is not None:
        _run(_set_query_params(ctx["qid"], params))
    return ctx


async def _set_query_params(query_id: str, params: list):
    from app.models.query import Query

    async with async_session_maker() as db:
        q = await db.get(Query, query_id)
        q.parameters = params
        await db.commit()


def _refresh(test_client, ctx):
    resp = test_client.post(f"/api/connections/{ctx['conn']}/custom-queries/{ctx['ct']}/refresh",
                            headers=_headers(ctx["admin"]["token"], ctx["admin"]["org_id"]))
    assert resp.status_code == 200, resp.text


def _set_policy(test_client, ctx, policy):
    resp = test_client.put(
        f"/api/connections/{ctx['conn']}/custom-queries/{ctx['ct']}/rls",
        json={"rls_enabled": True, "rls_mode": "attribute", "rls_policy": policy, "rls_default_deny": True},
        headers=_headers(ctx["admin"]["token"], ctx["admin"]["org_id"]),
    )
    assert resp.status_code == 200, resp.text


def _viewer_run(test_client, ctx, params=None):
    resp = test_client.post(
        f"/api/queries/{ctx['qid']}/run", json={"mode": "viewer", "params": params or {}},
        headers=_headers(ctx["viewer"]["token"], ctx["admin"]["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _pairs(body) -> set:
    return {(r["owner_email"], r["amount"]) for r in ((body or {}).get("data") or {}).get("rows") or []}


def _cached_slice(test_client, ctx) -> set:
    """The viewer runs the query twice: the second answer comes from their
    stored result. Returns the rows it holds."""
    first = _viewer_run(test_client, ctx)
    assert first["status"] == "success" and first["cached"] is False, first
    again = _viewer_run(test_client, ctx)
    assert again["cached"] is True, again
    assert _pairs(again) == _pairs(first)
    return _pairs(first)


def _step_read(test_client, ctx) -> set:
    return _pairs(_public_step(test_client, ctx["report"]["id"], ctx["qid"], token=ctx["viewer"]["token"]))


@pytest.mark.e2e
def test_a_row_policy_edit_stops_serving_the_slice_the_old_policy_filtered(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    viewer, owner = ctx["viewer"], ctx["owner"]
    stale = _cached_slice(test_client, ctx)
    assert stale == {(viewer["email"], 2)}
    assert _step_read(test_client, ctx) == stale

    # The viewer is granted the owner's orders too.
    _set_policy(test_client, ctx, {**OWN_ROWS, "grants": [
        {"principal_type": "user", "principal_id": viewer["user_id"], "values": [owner["email"]]}]})

    assert _step_read(test_client, ctx) != stale
    body = _viewer_run(test_client, ctx)
    assert body["cached"] is False, body
    assert _pairs(body) == {(owner["email"], 1), (viewer["email"], 2)}
    assert _step_read(test_client, ctx) == _pairs(body)


@pytest.mark.e2e
def test_an_extract_refresh_stops_serving_rows_read_from_the_old_extract(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    viewer = ctx["viewer"]
    stale = _cached_slice(test_client, ctx)
    assert stale == {(viewer["email"], 2)}

    _orders_db(ctx["db_path"], [(viewer["email"], 7)])
    _refresh(test_client, ctx)

    assert _step_read(test_client, ctx) != stale
    body = _viewer_run(test_client, ctx)
    assert body["cached"] is False, body
    assert _pairs(body) == {(viewer["email"], 2), (viewer["email"], 7)}


@pytest.mark.e2e
def test_an_auth_policy_change_stops_serving_rows_run_under_the_old_policy(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    """Without a row policy: the connection's auth policy alone decides."""
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
                              policy=None)
    stale = _cached_slice(test_client, ctx)
    assert stale == {(ctx["owner"]["email"], 1), (ctx["viewer"]["email"], 2)}

    resp = test_client.put(f"/api/connections/{ctx['conn']}", json={"auth_policy": "user_required"},
                           headers=_headers(ctx["admin"]["token"], ctx["admin"]["org_id"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_policy"] == "user_required"

    assert _step_read(test_client, ctx) != stale
    # The viewer holds no credential for the connection now: the run asks
    # for one instead of answering from their stored result.
    resp = test_client.post(
        f"/api/queries/{ctx['qid']}/run", json={"mode": "viewer", "params": {}},
        headers=_headers(ctx["viewer"]["token"], ctx["admin"]["org_id"]),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.e2e
def test_a_change_to_the_viewers_groups_stops_serving_the_slice_filtered_for_their_old_groups(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, create_group, add_user_to_group,
):
    """Members of a group are granted the owner's orders; the viewer joins it."""
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    admin, viewer, owner = ctx["admin"], ctx["viewer"], ctx["owner"]
    group = create_group(name=f"Owners' orders {uuid.uuid4().hex[:6]}", user_token=admin["token"],
                         org_id=admin["org_id"])
    assert group.status_code in (200, 201), group.text
    _set_policy(test_client, ctx, {**OWN_ROWS, "grants": [
        {"principal_type": "group", "principal_id": group.json()["id"], "values": [owner["email"]]}]})

    stale = _cached_slice(test_client, ctx)
    assert stale == {(viewer["email"], 2)}

    added = add_user_to_group(group_id=group.json()["id"], user_id=viewer["user_id"],
                              user_token=admin["token"], org_id=admin["org_id"])
    assert added.status_code in (200, 201), added.text

    assert _step_read(test_client, ctx) != stale
    body = _viewer_run(test_client, ctx)
    assert body["cached"] is False, body
    assert _pairs(body) == {(owner["email"], 1), (viewer["email"], 2)}


@pytest.mark.e2e
def test_an_unrelated_change_keeps_serving_the_viewers_stored_result(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    """Saving the same row policy again changes nothing a run reads."""
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    stale = _cached_slice(test_client, ctx)
    _set_policy(test_client, ctx, OWN_ROWS)
    assert _step_read(test_client, ctx) == stale
    assert _viewer_run(test_client, ctx)["cached"] is True


YEAR = {
    "name": "Year", "type": "string", "label": "Year", "default": None,
    "required": False, "source": "input", "identity_binding": None,
    "options": None, "options_source": None, "strict_options": False,
}


async def _viewer_rows(step_id: str, user_id: str) -> list:
    from sqlalchemy import select
    from app.models.step_user_result import StepUserResult

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(StepUserResult.applied_params).where(
                StepUserResult.step_id == step_id, StepUserResult.user_id == user_id)
        )).all()
        return sorted((p or {}).get("Year") or "" for (p,) in rows)


@pytest.mark.e2e
def test_a_row_policy_edit_deletes_superseded_slices_and_keeps_the_latest(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    """The viewer's other cached slices go; the latest stays (it seeds their
    next run's values) but is not served."""
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
                              params=[YEAR])
    viewer, owner = ctx["viewer"], ctx["owner"]
    for year in ("2023", "2024"):
        assert _viewer_run(test_client, ctx, {"Year": year})["status"] == "success"
    assert _run(_viewer_rows(ctx["sid"], viewer["user_id"])) == ["2023", "2024"]

    _set_policy(test_client, ctx, {**OWN_ROWS, "grants": [
        {"principal_type": "user", "principal_id": viewer["user_id"], "values": [owner["email"]]}]})

    assert _run(_viewer_rows(ctx["sid"], viewer["user_id"])) == ["2024"]
    body = _viewer_run(test_client, ctx, {"Year": "2024"})
    assert body["cached"] is False, body
    assert _pairs(body) == {(owner["email"], 1), (viewer["email"], 2)}


async def _seed_unstamped_result(step_id: str, user_id: str):
    """A viewer's result as stored before results carried a stamp
    (vrstamp01): such rows are in customers' databases at the upgrade, so it
    is written directly."""
    from datetime import datetime

    from app.models.query import Query
    from app.models.step import Step
    from app.models.step_user_result import StepUserResult

    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        query = await db.get(Query, step.query_id)
        db.add(StepUserResult(
            step_id=step_id, user_id=user_id, organization_id=query.organization_id,
            report_id=query.report_id, status="success", executed_as="viewer", params_fingerprint="",
            data={"rows": [{"owner_email": "unstamped", "amount": -1}],
                  "columns": [{"field": "owner_email"}, {"field": "amount"}]},
            last_run_at=datetime.utcnow(),
        ))
        await db.commit()


@pytest.mark.e2e
def test_a_result_stored_before_stamps_is_served_only_where_no_stamped_input_is_in_play(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
):
    """Nothing tells what an unstamped result read. Over an accelerated
    relation (or a per-user connection, or an identity-scoped step) it is not
    served and the next run executes; over plain shared sources it still is."""
    unstamped = {("unstamped", -1)}
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    _run(_seed_unstamped_result(ctx["sid"], ctx["viewer"]["user_id"]))
    assert _step_read(test_client, ctx) != unstamped
    body = _viewer_run(test_client, ctx)
    assert body["cached"] is False and _pairs(body) == {(ctx["viewer"]["email"], 2)}, body

    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal")
    _run(_seed_unstamped_result(seeded["step_ids"][0], viewer["user_id"]))
    plain = _public_step(test_client, report["id"], seeded["query_ids"][0], token=viewer["token"])
    assert _pairs(plain) == unstamped


# ── a run stamps what it reads before it builds its clients ─────────────

@pytest.fixture
def auth_policy_switch_after_clients(monkeypatch):
    """Once armed with {"conn", "to"}: the connection's auth policy changes
    right after a run built the clients of its first data source, before the
    run stores any result (in the run's own transaction, as a change landing
    in between would look to it)."""
    from sqlalchemy import update

    from app.models.connection import Connection
    from app.services.data_source_service import DataSourceService

    original = DataSourceService.construct_clients
    armed: dict = {}

    async def build_then_switch(self, db, data_source, *args, **kwargs):
        clients = await original(self, db, data_source, *args, **kwargs)
        if armed:
            await db.execute(update(Connection).where(Connection.id == armed.pop("conn"))
                             .values(auth_policy=armed.pop("to")).execution_options(synchronize_session=False))
        return clients

    monkeypatch.setattr(DataSourceService, "construct_clients", build_then_switch)
    return armed


async def _stored_successes(step_id: str, user_id: str) -> int:
    from sqlalchemy import func, select

    from app.models.step_user_result import StepUserResult

    async with async_session_maker() as db:
        return (await db.execute(select(func.count(StepUserResult.id)).where(
            StepUserResult.step_id == step_id, StepUserResult.user_id == user_id,
            StepUserResult.status == "success"))).scalar_one()


@pytest.mark.e2e
def test_a_viewer_rerun_stores_results_stamped_before_an_auth_policy_switch(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, auth_policy_switch_after_clients,
):
    """The run's clients were built under the old policy: what it stores is
    stale once the switch lands, so it is not served as the viewer's result."""
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
                              policy=None)
    auth_policy_switch_after_clients.update(conn=ctx["conn"], to="user_required")
    rerun = test_client.post(f"/api/r/{ctx['report']['id']}/run", headers=_headers(ctx["viewer"]["token"]))
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["steps_succeeded"] == 1, rerun.json()
    assert not auth_policy_switch_after_clients, "the policy switched during the run"
    assert _run(_stored_successes(ctx["sid"], ctx["viewer"]["user_id"])) == 1

    read = _public_step(test_client, ctx["report"]["id"], ctx["qid"], token=ctx["viewer"]["token"])
    assert read.get("viewer_result") is None, read


# ── a run stamps the identity its params were bound from ───────────────

GROUPS_CODE = """
def generate_df(ds_clients, excel_files, params=None):
    import pandas as pd
    return pd.DataFrame({"groups": [",".join((params or {}).get("Groups") or []) or "none"], "n": [1]})
"""


async def _stored_result_current(step_id: str, user_id: str, report_id: str) -> bool:
    """Whether the viewer's stored result passes the check every read of it
    applies (viewer_result_current), as of now."""
    from sqlalchemy import select

    from app.models.step import Step
    from app.models.step_user_result import StepUserResult
    from app.services.viewer_result_freshness import viewer_result_current

    async with async_session_maker() as db:
        (row,) = (await db.execute(select(StepUserResult).where(
            StepUserResult.step_id == step_id, StepUserResult.user_id == user_id))).scalars().all()
        return await viewer_result_current(db, row, await db.get(Step, step_id), report_id=report_id)


@pytest.mark.e2e
def test_a_viewer_run_stamps_the_identity_its_params_were_bound_from(
    test_client, create_report, bootstrap_admin, invite_user_to_org, create_group, group_joined_after_bind,
):
    """The viewer joins a group after the run bound their groups (none) into
    its params, before it stored the result. The stored rows hold the old
    binding: once the join is in, they are not current for the viewer."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal")
    qid, sid = seeded["query_ids"][0], seeded["step_ids"][0]
    _run(_set_step_code(sid, GROUPS_CODE))
    _run(_set_query_params(qid, [GROUPS_PARAM]))
    group = create_group(name=f"Sales {uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
    assert group.status_code in (200, 201), group.text

    group_joined_after_bind.update(group=group.json()["id"], user=viewer["user_id"])
    body = _viewer_run(test_client, {"qid": qid, "viewer": viewer, "admin": admin})
    assert body["status"] == "success" and body["cached"] is False, body
    assert body["data"]["rows"] == [{"groups": "none", "n": 1}], body
    assert not group_joined_after_bind, "the viewer joined the group during the run"

    assert _run(_stored_result_current(sid, viewer["user_id"], report["id"])) is False


@pytest.mark.e2e
def test_a_view_as_preview_runs_again_once_the_previewed_members_groups_change(
    test_client, create_report, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group,
):
    """The owner previews the dashboard as the viewer: identity params bind
    the viewer, and the preview is stored under the owner. Once the viewer
    joins a group, the stored preview holds their old groups: the next
    preview runs again instead of serving it."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal")
    qid, sid = seeded["query_ids"][0], seeded["step_ids"][0]
    _run(_set_step_code(sid, GROUPS_CODE))
    _run(_set_query_params(qid, [GROUPS_PARAM]))
    name = f"Sales {uuid.uuid4().hex[:6]}"
    group = create_group(name=name, user_token=admin["token"], org_id=admin["org_id"])
    assert group.status_code in (200, 201), group.text

    def preview():
        resp = test_client.post(
            f"/api/queries/{qid}/run",
            json={"mode": "viewer", "params": {}, "run_as_user_id": viewer["user_id"]},
            headers=_headers(owner["token"], admin["org_id"]),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    first = preview()
    assert first["status"] == "success" and first["cached"] is False, first
    assert first["data"]["rows"] == [{"groups": "none", "n": 1}], first
    assert preview()["cached"] is True

    added = add_user_to_group(group_id=group.json()["id"], user_id=viewer["user_id"],
                              user_token=admin["token"], org_id=admin["org_id"])
    assert added.status_code in (200, 201), added.text

    again = preview()
    assert again["cached"] is False, again
    assert again["data"]["rows"] == [{"groups": name, "n": 1}], again
    assert preview()["cached"] is True


# ── a stamp in a long-lived session reads the state as committed now ──

async def _stamp_in_one_session_across_a_change(ctx: dict, change) -> tuple:
    """(stamp before, stamp after) the change, both taken in one session
    that stays open across it (expire_on_commit off, as every request
    session), with its transaction ended in between; and the stamp a fresh
    session takes after it. The session holds the viewer's user and
    membership loaded, as a request holds its caller."""
    from sqlalchemy import select

    from app.models.membership import Membership
    from app.models.step import Step
    from app.models.user import User
    from app.services.viewer_result_freshness import stamp_columns

    def stamp(db, step):
        return stamp_columns(db, step, user_id=ctx["viewer"]["user_id"], organization_id=ctx["admin"]["org_id"],
                             executed_as="viewer", report_id=ctx["report"]["id"])

    async with async_session_maker() as long_lived:
        held = [await long_lived.get(User, ctx["viewer"]["user_id"]), (await long_lived.execute(
            select(Membership).where(Membership.user_id == ctx["viewer"]["user_id"],
                                     Membership.organization_id == ctx["admin"]["org_id"]))).scalar_one()]
        step = await long_lived.get(Step, ctx["sid"])
        before = (await stamp(long_lived, step))["source_stamp"]
        await long_lived.commit()
        async with async_session_maker() as other:
            await change(other)
            await other.commit()
        after = (await stamp(long_lived, step))["source_stamp"]
        assert all(held)
    async with async_session_maker() as fresh:
        now = (await stamp(fresh, await fresh.get(Step, ctx["sid"])))["source_stamp"]
    return before, after, now


@pytest.mark.e2e
def test_a_stamp_in_a_long_lived_session_reads_the_viewers_groups_and_the_policy_as_they_are_now(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, create_group,
):
    from sqlalchemy import update

    from app.models.connection_table import ConnectionTable
    from app.models.group_membership import GroupMembership
    from app.models.membership import Membership
    from app.models.user import User

    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    gid = create_group(name=f"Owners' orders {uuid.uuid4().hex[:6]}", user_token=ctx["admin"]["token"],
                       org_id=ctx["admin"]["org_id"]).json()["id"]
    _set_policy(test_client, ctx, {**OWN_ROWS, "grants": [
        {"principal_type": "group", "principal_id": gid, "values": [ctx["owner"]["email"]]}]})

    async def join(db):
        db.add(GroupMembership(group_id=gid, user_id=ctx["viewer"]["user_id"]))

    before, after, now = _run(_stamp_in_one_session_across_a_change(ctx, join))
    assert before != now and after == now

    # The policy reads the viewer's email: a new one is a new filter.
    async def change_email(db):
        await db.execute(update(User).where(User.id == ctx["viewer"]["user_id"])
                         .values(email=f"moved-{uuid.uuid4().hex[:6]}@example.com"))

    before, after, now = _run(_stamp_in_one_session_across_a_change(ctx, change_email))
    assert before != now and after == now

    # A policy on a profile attribute: a new value is a new filter.
    _set_policy(test_client, ctx, {"column": "owner_email", "source": "profile.region", "op": "in"})

    async def change_profile(db):
        await db.execute(update(Membership).where(Membership.user_id == ctx["viewer"]["user_id"],
                                                  Membership.organization_id == ctx["admin"]["org_id"])
                         .values(profile_attributes={"region": ctx["owner"]["email"]}))

    before, after, now = _run(_stamp_in_one_session_across_a_change(ctx, change_profile))
    assert before != now and after == now

    async def turn_policy_off(db):
        await db.execute(update(ConnectionTable).where(ConnectionTable.id == ctx["ct"]).values(rls_enabled=False))

    before, after, now = _run(_stamp_in_one_session_across_a_change(ctx, turn_policy_off))
    assert before != now and after == now


# ── a purge never undoes or breaks the change that ran it ───────────────

@pytest.fixture
def failing_purge(monkeypatch):
    """Every purge of stored results fails (a lost connection, a timeout).
    Returns the list of attempts."""
    import app.services.viewer_result_freshness as freshness

    attempts: list = []

    async def fail(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("purge failed")

    monkeypatch.setattr(freshness, "_delete_unless_latest", fail)
    return attempts


def _audit_actions(org_id: str, resource_id: str) -> list:
    from sqlalchemy import select

    from app.ee.audit.models import AuditLog

    async def read():
        async with async_session_maker() as db:
            return sorted((await db.execute(select(AuditLog.action).where(
                AuditLog.organization_id == org_id, AuditLog.resource_id == resource_id))).scalars().all())
    return _run(read())


@pytest.mark.e2e
def test_a_failed_purge_leaves_the_auth_policy_change_saved_and_answered(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, failing_purge,
):
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
                              policy=None)
    _cached_slice(test_client, ctx)
    h = _headers(ctx["admin"]["token"], ctx["admin"]["org_id"])

    resp = test_client.put(f"/api/connections/{ctx['conn']}", json={"auth_policy": "user_required"}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_policy"] == "user_required"
    assert failing_purge, "the purge ran"
    saved = test_client.get(f"/api/connections/{ctx['conn']}", headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["auth_policy"] == "user_required"
    # The rows it could not purge are refused at read all the same: no result
    # of the viewer's, and the snapshot of a per-user source is withheld.
    read = _public_step(test_client, ctx["report"]["id"], ctx["qid"], token=ctx["viewer"]["token"])
    assert read.get("viewer_result") is None, read
    assert _pairs(read) == set(), read


@pytest.mark.e2e
def test_a_failed_purge_keeps_the_group_change_and_its_audit_trail(
    test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group, failing_purge,
):
    admin = bootstrap_admin()
    viewer = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    h = _headers(admin["token"], admin["org_id"])
    group = create_group(name=f"Team {uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
    assert group.status_code in (200, 201), group.text
    gid = group.json()["id"]

    added = add_user_to_group(group_id=gid, user_id=viewer["user_id"], user_token=admin["token"],
                              org_id=admin["org_id"])
    assert added.status_code in (200, 201), added.text
    assert failing_purge, "the purge ran"
    assert _audit_actions(admin["org_id"], gid).count("group.member_added") == 1

    removed = test_client.delete(f"/api/organizations/{admin['org_id']}/groups/{gid}/members/{viewer['user_id']}",
                                 headers=h)
    assert removed.status_code == 204, removed.text
    assert len(failing_purge) == 2
    assert _audit_actions(admin["org_id"], gid).count("group.member_removed") == 1
    listed = test_client.get(f"/api/organizations/{admin['org_id']}/groups", headers=h)
    assert next(g for g in listed.json() if g["id"] == gid)["member_user_ids"] == []


@pytest.mark.e2e
def test_a_failed_purge_does_not_fail_an_extract_refresh(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, failing_purge,
):
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    before = len(failing_purge)
    _orders_db(ctx["db_path"], [(ctx["viewer"]["email"], 7)])
    _refresh(test_client, ctx)  # 200
    assert len(failing_purge) == before + 1, "the purge ran"
    body = _viewer_run(test_client, ctx)
    assert _pairs(body) == {(ctx["viewer"]["email"], 2), (ctx["viewer"]["email"], 7)}


# ── a group deleted takes its members' outdated results with it ────────

@pytest.mark.e2e
def test_deleting_a_group_purges_for_each_of_its_members_after_the_delete(
    test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group, member_purges,
):
    admin = bootstrap_admin()
    a = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    b = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    group = create_group(name=f"Team {uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=admin["org_id"])
    gid = group.json()["id"]
    for member in (a, b):
        assert add_user_to_group(group_id=gid, user_id=member["user_id"], user_token=admin["token"],
                                 org_id=admin["org_id"]).status_code in (200, 201)
    assert [c["user_ids"] for c in member_purges] == [[a["user_id"]], [b["user_id"]]]
    assert gid in member_purges[0]["groups"][a["user_id"]]

    gone = test_client.delete(f"/api/organizations/{admin['org_id']}/groups/{gid}",
                              headers=_headers(admin["token"], admin["org_id"]))
    assert gone.status_code == 204, gone.text
    last = member_purges[-1]
    assert last["organization_id"] == admin["org_id"]
    assert last["user_ids"] == sorted([a["user_id"], b["user_id"]])
    assert all(gid not in groups for groups in last["groups"].values())


# ── a purge runs after the change it follows is audited ────────────────

@pytest.fixture
def audits_at_purge(monkeypatch):
    """Each purge, as it starts: the audit rows committed by then, counted
    by (resource_id, action). The purge itself still runs."""
    from collections import Counter

    from sqlalchemy import select

    import app.services.viewer_result_freshness as freshness
    from app.ee.audit.models import AuditLog

    original = freshness.purge_quietly
    seen: list = []

    async def record(db, purge, **kwargs):
        async with async_session_maker() as own:
            seen.append(Counter((await own.execute(select(AuditLog.resource_id, AuditLog.action))).all()))
        return await original(db, purge, **kwargs)

    monkeypatch.setattr(freshness, "purge_quietly", record)
    return seen


def _audit_count(resource_id: str, action: str) -> int:
    from sqlalchemy import func, select

    from app.ee.audit.models import AuditLog

    async def read():
        async with async_session_maker() as db:
            return (await db.execute(select(func.count(AuditLog.id)).where(
                AuditLog.resource_id == resource_id, AuditLog.action == action))).scalar_one()
    return _run(read())


@pytest.mark.e2e
def test_a_policy_or_auth_policy_change_is_audited_before_its_purge(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, audits_at_purge,
):
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path)
    conn = ctx["conn"]

    _set_policy(test_client, ctx, {**OWN_ROWS, "grants": [
        {"principal_type": "user", "principal_id": ctx["viewer"]["user_id"], "values": [ctx["owner"]["email"]]}]})
    changed = _audit_count(conn, "connection.custom_query.rls_changed")
    assert audits_at_purge[-1][(conn, "connection.custom_query.rls_changed")] == changed

    resp = test_client.put(f"/api/connections/{conn}", json={"auth_policy": "user_required"},
                           headers=_headers(ctx["admin"]["token"], ctx["admin"]["org_id"]))
    assert resp.status_code == 200, resp.text
    updated = _audit_count(conn, "connection.updated")
    assert updated >= 1
    assert audits_at_purge[-1][(conn, "connection.updated")] == updated


@pytest.mark.e2e
def test_an_auth_policy_change_that_also_changes_member_bindings_is_audited_before_its_purge(
    test_client, bootstrap_admin, monkeypatch, audits_at_purge,
):
    """One save turns per-user auth off on a member-mapped connection and
    rebinds its fields: both audit rows of the request are in before the
    purge of results run under the previous auth policy."""
    from app.services.connection_service import ConnectionService
    from tests.e2e.test_member_identity import QLIK_CREDS

    async def reachable(self, **kwargs):
        return {"success": True, "message": "ok", "reachable": True}

    monkeypatch.setattr(ConnectionService, "test_connection_params", reachable)
    admin = bootstrap_admin()
    h = _headers(admin["token"], admin["org_id"])
    created = test_client.post("/api/connections", json={
        "name": f"qlik-{uuid.uuid4().hex[:6]}", "type": "qlik_sense_onprem",
        "config": {"server_url": "https://qlik.example.com"}, "credentials": QLIK_CREDS,
        "auth_policy": "user_required", "member_bindings": {"user_id": "qlik_user_id"},
    }, headers=h)
    if created.status_code == 402:
        pytest.skip("per-user auth for this connector needs an enterprise license in this environment")
    assert created.status_code == 200, created.text
    conn = created.json()["id"]

    resp = test_client.put(f"/api/connections/{conn}", json={
        "auth_policy": "system_only", "member_bindings": {"user_id": "employee_id"}}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["auth_policy"] == "system_only"
    assert _audit_count(conn, "connection.member_identity_changed") == 1
    assert audits_at_purge, "the purge ran"
    assert audits_at_purge[-1][(conn, "connection.updated")] == _audit_count(conn, "connection.updated")
    assert audits_at_purge[-1][(conn, "connection.member_identity_changed")] == 1


@pytest.mark.e2e
def test_an_auth_policy_change_through_its_data_source_is_audited_before_its_purge(
    test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path, audits_at_purge,
):
    """The data source's update saves its connection's auth policy, then
    writes its own audit row: the purge runs after it."""
    ctx = _accelerated_report(test_client, create_report, bootstrap_admin, invite_user_to_org, tmp_path,
                              policy=None)
    _cached_slice(test_client, ctx)
    before = len(audits_at_purge)
    resp = test_client.put(f"/api/data_sources/{ctx['ds']}", json={"auth_policy": "user_required"},
                           headers=_headers(ctx["admin"]["token"], ctx["admin"]["org_id"]))
    assert resp.status_code == 200, resp.text
    assert len(audits_at_purge) == before + 1, "one purge ran"
    assert _audit_count(ctx["ds"], "data_source.updated") == 1
    assert audits_at_purge[-1][(ctx["ds"], "data_source.updated")] == 1


@pytest.mark.e2e
def test_a_group_change_is_audited_before_its_purge(
    test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group, audits_at_purge,
):
    admin = bootstrap_admin()
    viewer = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    h = _headers(admin["token"], admin["org_id"])
    gid = create_group(name=f"Team {uuid.uuid4().hex[:6]}", user_token=admin["token"],
                       org_id=admin["org_id"]).json()["id"]

    assert add_user_to_group(group_id=gid, user_id=viewer["user_id"], user_token=admin["token"],
                             org_id=admin["org_id"]).status_code in (200, 201)
    assert audits_at_purge[-1][(gid, "group.member_added")] == 1

    removed = test_client.delete(f"/api/organizations/{admin['org_id']}/groups/{gid}/members/{viewer['user_id']}",
                                 headers=h)
    assert removed.status_code == 204, removed.text
    assert audits_at_purge[-1][(gid, "group.member_removed")] == 1

    assert add_user_to_group(group_id=gid, user_id=viewer["user_id"], user_token=admin["token"],
                             org_id=admin["org_id"]).status_code in (200, 201)
    gone = test_client.delete(f"/api/organizations/{admin['org_id']}/groups/{gid}", headers=h)
    assert gone.status_code == 204, gone.text
    assert audits_at_purge[-1][(gid, "group.deleted")] == 1


# ── a purge of many members' results costs per member, not per row ─────

async def _seed_member_results(step_id: str, user_ids: list, n: int, tag: str, age_days: int):
    """n identity-scoped results per member, stamped with what their runs
    read then (not what they read now)."""
    from datetime import datetime, timedelta

    from sqlalchemy import insert

    from app.models.query import Query
    from app.models.step import Step
    from app.models.step_user_result import StepUserResult

    async with async_session_maker() as db:
        step = await db.get(Step, step_id)
        query = await db.get(Query, step.query_id)
        start = datetime.utcnow() - timedelta(days=age_days)
        await db.execute(insert(StepUserResult), [
            {"step_id": step_id, "user_id": user_id, "organization_id": query.organization_id,
             "report_id": query.report_id, "status": "success", "executed_as": "viewer",
             "params_fingerprint": f"{tag}{i}", "last_run_at": start + timedelta(seconds=i),
             "source_stamp": "read-then", "source_refs": "i"}
            for user_id in user_ids for i in range(n)])
        await db.commit()


async def _counted_member_purge(org_id: str, user_ids: list) -> tuple:
    """purge_members_results for these members: (statements run, connections
    taken from the pool)."""
    from sqlalchemy import event

    from app.services.viewer_result_freshness import purge_members_results

    counts = {"statements": 0, "checkouts": 0}

    def statement(*args, **kwargs):
        counts["statements"] += 1

    def checkout(*args, **kwargs):
        counts["checkouts"] += 1

    async with async_session_maker() as db:
        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", statement)
        event.listen(engine, "checkout", checkout)
        try:
            await purge_members_results(db, organization_id=org_id, user_ids=user_ids)
        finally:
            event.remove(engine, "before_cursor_execute", statement)
            event.remove(engine, "checkout", checkout)
    return counts["statements"], counts["checkouts"]


async def _member_slots(step_id: str, user_ids: list) -> dict:
    from sqlalchemy import text

    async with async_session_maker() as db:
        slots: dict = {u: [] for u in user_ids}
        for uid, fp, stamp in (await db.execute(text(
                "SELECT user_id, params_fingerprint, source_stamp FROM step_user_results WHERE step_id = :s"),
                {"s": step_id})).all():
            slots[uid].append((fp, stamp))
        return slots


@pytest.mark.e2e
def test_a_purge_of_many_members_results_runs_a_bounded_number_of_statements(
    test_client, create_report, bootstrap_admin, invite_user_to_org,
):
    """Three members' group change. Five times the stored results per member
    take no more statements (one more per member at most), and all members
    are purged over one connection."""
    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal")
    sid = seeded["step_ids"][0]
    members = sorted([admin["user_id"], owner["user_id"], viewer["user_id"]])

    _run(_seed_member_results(sid, members, 5, "a", age_days=2))
    few, few_checkouts = _run(_counted_member_purge(admin["org_id"], members))
    assert _run(_member_slots(sid, members)) == {u: [("a4", "read-then")] for u in members}

    _run(_seed_member_results(sid, members, 25, "b", age_days=1))
    many, many_checkouts = _run(_counted_member_purge(admin["org_id"], members))
    assert _run(_member_slots(sid, members)) == {u: [("b24", "read-then")] for u in members}

    assert many <= few + len(members), f"statements: {few} for 5 results per member, {many} for 25"
    assert (few_checkouts, many_checkouts) == (1, 1), (few_checkouts, many_checkouts)


# ── a purge stops when its time is up; the request goes on ─────────────

@pytest.mark.e2e
def test_a_purge_that_runs_out_of_time_stops_and_the_group_delete_succeeds(
    test_client, create_report, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group,
    monkeypatch, caplog,
):
    """A group of three is deleted. The purge's clock runs past its budget
    once the first member's results are purged: it stops there, the delete
    is answered and audited, and the other members' results stay (refused at
    read, purged by their next change)."""
    import logging

    import app.services.viewer_result_freshness as freshness

    admin, owner, viewer, report, seeded = _shared_report(
        test_client, create_report, bootstrap_admin, invite_user_to_org, visibility="internal")
    sid = seeded["step_ids"][0]
    members = sorted([admin["user_id"], owner["user_id"], viewer["user_id"]])
    gid = create_group(name=f"Team {uuid.uuid4().hex[:6]}", user_token=admin["token"],
                       org_id=admin["org_id"]).json()["id"]
    for uid in members:
        assert add_user_to_group(group_id=gid, user_id=uid, user_token=admin["token"],
                                 org_id=admin["org_id"]).status_code in (200, 201)
    _run(_seed_member_results(sid, members, 3, "a", age_days=1))

    now = [0.0]
    original = freshness.purge_viewer_results

    async def purge_then_time_passes(db, **kwargs):
        n = await original(db, **kwargs)
        now[0] += getattr(freshness, "PURGE_BUDGET_SECONDS", 10) + 1
        return n

    # raising=False: the clock stands in whether or not the app defines it yet.
    monkeypatch.setattr(freshness, "_clock", lambda: now[0], raising=False)
    monkeypatch.setattr(freshness, "purge_viewer_results", purge_then_time_passes)
    # Heard on the module's own logger: an earlier test in this process may
    # have reconfigured logging (disabled existing loggers, cut propagation).
    monkeypatch.setattr(freshness.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=freshness.__name__)
    freshness.logger.addHandler(caplog.handler)
    try:
        gone = test_client.delete(f"/api/organizations/{admin['org_id']}/groups/{gid}",
                                  headers=_headers(admin["token"], admin["org_id"]))
    finally:
        freshness.logger.removeHandler(caplog.handler)
    assert gone.status_code == 204, gone.text
    assert _audit_count(gid, "group.deleted") == 1
    slots = _run(_member_slots(sid, members))
    assert slots[members[0]] == [("a2", "read-then")]
    for uid in members[1:]:
        assert sorted(slots[uid]) == [("a0", "read-then"), ("a1", "read-then"), ("a2", "read-then")]
    # Once (the handler may hear it twice: directly and through the root).
    (spent,) = {id(r): r for r in caplog.records
                if r.getMessage() == "viewer_results.purge_budget_exhausted"}.values()
    assert (spent.purge, spent.deleted, spent.members, spent.members_purged) == ("_purge_members", 2, 3, 1)
