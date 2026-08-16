"""Entity creation/authoring RBAC + user-scoped snapshot withholding.

Covers the two bug classes fixed together:

1. RBAC: an agent owner/manager (per-DS ``manage`` or ``create_entities``
   grant) must be able to create, publish, edit and delete entities on their
   own agent; a plain member may SUGGEST an entity from their own step on an
   accessible agent but cannot publish; members without access are refused.

2. Withholding: on a user-scoped (``auth_policy != system_only``) source the
   shared ``Entity.data`` snapshot is one identity's row slice. Creating an
   entity from someone else's step must not copy that slice to a new owner,
   the planner context builders must not render it for other readers, and
   code-execution loadables must refuse it.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


STEP_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"v": [1, 2]})
"""

OWNER_SLICE = {
    "rows": [{"v": "owner-private-row"}],
    "columns": [{"field": "v"}],
}


def _create_report(test_client, token, org_id, data_source_ids):
    resp = test_client.post(
        "/api/reports",
        json={"title": f"r_{uuid.uuid4().hex[:6]}", "data_sources": data_source_ids},
        headers=_hdr(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _seed_step(report_id: str, data=None):
    """Seed a successful default step on an API-created report (the AI flow
    normally produces these; no public CRUD API exists)."""
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
            title=f"Step {suffix}", slug=f"step-{suffix}", status="success",
            widget_id=widget.id, query_id=query.id, code=STEP_CODE,
            data=(data if data is not None else OWNER_SLICE),
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()
        return str(step.id)


async def _flip_ds_user_required(ds_id: str):
    from sqlalchemy import select, update
    from app.models.connection import Connection
    from app.models.domain_connection import domain_connection

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(domain_connection.c.connection_id).where(
                domain_connection.c.data_source_id == str(ds_id)
            )
        )).scalars().all()
        assert rows, "expected the data source to have a connection"
        await db.execute(
            update(Connection).where(Connection.id.in_(rows)).values(auth_policy="user_required")
        )
        await db.commit()


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    ds_public = sqlite_data_source(
        name="pub_agent", user_token=admin["token"], org_id=org_id, is_public=True,
    )
    ds_private = sqlite_data_source(
        name="prv_agent", user_token=admin["token"], org_id=org_id,
    )

    owner = invite_user_to_org(org_id=org_id, admin_token=admin["token"])   # agent manager
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])  # plain member

    # `manage` grant = the owner tier a user gets on an agent they created.
    grant_resource(
        resource_type="data_source", resource_id=ds_public["id"],
        principal_type="user", principal_id=owner["user_id"],
        permissions=["manage"], user_token=admin["token"], org_id=org_id,
    )

    return {
        "org_id": org_id, "admin": admin, "owner": owner, "member": member,
        "ds_public": ds_public, "ds_private": ds_private,
    }


# ── 1. Creation RBAC ─────────────────────────────────────────────────────

@pytest.mark.e2e
def test_agent_manager_can_create_entity(test_client, world):
    """Regression: the owner tier (`manage` grant) creates entities on its agent."""
    resp = test_client.post(
        "/api/entities",
        json={
            "type": "model", "title": "mine", "slug": f"mine-{uuid.uuid4().hex[:6]}",
            "code": "select 1", "data": {}, "status": "draft",
            "data_source_ids": [world["ds_public"]["id"]],
        },
        headers=_hdr(world["owner"]["token"], world["org_id"]),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.e2e
def test_agent_manager_can_publish_from_step(test_client, world):
    report_id = _create_report(
        test_client, world["owner"]["token"], world["org_id"], [world["ds_public"]["id"]],
    )
    step_id = _run(_seed_step(report_id))
    resp = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"type": "model", "title": "published by manager", "publish": True,
              "data_source_ids": [world["ds_public"]["id"]]},
        headers=_hdr(world["owner"]["token"], world["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["global_status"] == "approved"
    assert body["status"] == "published"


@pytest.mark.e2e
def test_plain_member_suggests_but_cannot_publish(test_client, world):
    report_id = _create_report(
        test_client, world["member"]["token"], world["org_id"], [world["ds_public"]["id"]],
    )
    step_id = _run(_seed_step(report_id))

    denied = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"title": "try publish", "publish": True,
              "data_source_ids": [world["ds_public"]["id"]]},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    )
    assert denied.status_code == 403, denied.text

    suggested = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"title": "suggestion", "publish": False,
              "data_source_ids": [world["ds_public"]["id"]]},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    )
    assert suggested.status_code == 200, suggested.text
    body = suggested.json()
    assert body["global_status"] == "suggested"
    assert body["status"] == "draft"


@pytest.mark.e2e
def test_member_without_ds_access_cannot_from_step(test_client, world):
    # Admin's report on the PRIVATE agent; member holds no grant on it.
    report_id = _create_report(
        test_client, world["admin"]["token"], world["org_id"], [world["ds_private"]["id"]],
    )
    step_id = _run(_seed_step(report_id))
    resp = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"title": "nope", "publish": False,
              "data_source_ids": [world["ds_private"]["id"]]},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.e2e
def test_owner_lifecycle_suggest_withdraw_update_delete(test_client, world):
    """A suggester owns their unapproved entity: edit, suggest, withdraw, delete."""
    report_id = _create_report(
        test_client, world["member"]["token"], world["org_id"], [world["ds_public"]["id"]],
    )
    step_id = _run(_seed_step(report_id))
    created = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"title": "my draft", "publish": False,
              "data_source_ids": [world["ds_public"]["id"]]},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    )
    assert created.status_code == 200, created.text
    entity_id = created.json()["id"]
    hdr = _hdr(world["member"]["token"], world["org_id"])

    upd = test_client.put(f"/api/entities/{entity_id}", json={"title": "renamed"}, headers=hdr)
    assert upd.status_code == 200, upd.text
    assert upd.json()["title"] == "renamed"

    wd = test_client.post(f"/api/entities/{entity_id}/withdraw", headers=hdr)
    assert wd.status_code == 200, wd.text
    assert wd.json()["global_status"] is None

    sg = test_client.post(f"/api/entities/{entity_id}/suggest", headers=hdr)
    assert sg.status_code == 200, sg.text
    assert sg.json()["global_status"] == "suggested"

    # Another plain member may not touch it.
    other = world["admin"]  # admin CAN touch; use a real stranger check below via 403 on owner-only endpoint
    stranger_wd = test_client.post(
        f"/api/entities/{entity_id}/withdraw",
        headers=_hdr(world["owner"]["token"], world["org_id"]),
    )
    assert stranger_wd.status_code == 403, stranger_wd.text

    dele = test_client.delete(f"/api/entities/{entity_id}", headers=hdr)
    assert dele.status_code == 200, dele.text


# ── 2. User-scoped snapshot withholding ──────────────────────────────────

@pytest.fixture
def user_scoped_world(test_client, world):
    """Public agent flipped to user_required, with an admin-owned published
    entity carrying the admin's snapshot slice."""
    _run(_flip_ds_user_required(world["ds_public"]["id"]))
    created = test_client.post(
        "/api/entities/global",
        json={
            "type": "model", "title": "admins entity",
            "slug": f"adm-{uuid.uuid4().hex[:6]}", "code": "select 1",
            "data": OWNER_SLICE, "status": "published",
            "data_source_ids": [world["ds_public"]["id"]],
        },
        headers=_hdr(world["admin"]["token"], world["org_id"]),
    )
    assert created.status_code == 200, created.text
    return {**world, "entity": created.json()}


@pytest.mark.e2e
def test_detail_withholds_snapshot_from_nonowner(test_client, user_scoped_world):
    w = user_scoped_world
    mine = test_client.get(
        f"/api/entities/{w['entity']['id']}", headers=_hdr(w["admin"]["token"], w["org_id"]),
    )
    assert mine.status_code == 200 and mine.json()["data"] == OWNER_SLICE

    other = test_client.get(
        f"/api/entities/{w['entity']['id']}", headers=_hdr(w["member"]["token"], w["org_id"]),
    )
    assert other.status_code == 200, other.text
    assert other.json()["snapshot_withheld"] is True
    assert other.json()["data"] in ({}, None)


@pytest.mark.e2e
def test_from_step_does_not_copy_foreign_slice(test_client, user_scoped_world):
    """A member saving the ADMIN's step must not mint an entity carrying the
    admin's credential-scoped rows."""
    w = user_scoped_world
    report_id = _create_report(
        test_client, w["admin"]["token"], w["org_id"], [w["ds_public"]["id"]],
    )
    step_id = _run(_seed_step(report_id, data=OWNER_SLICE))

    saved = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"title": "member copy", "publish": False,
              "data_source_ids": [w["ds_public"]["id"]]},
        headers=_hdr(w["member"]["token"], w["org_id"]),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"] in ({}, None), "foreign snapshot slice must be withheld"

    # The report owner saving their OWN step keeps their slice.
    own = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"title": "admin copy", "publish": True,
              "data_source_ids": [w["ds_public"]["id"]]},
        headers=_hdr(w["admin"]["token"], w["org_id"]),
    )
    assert own.status_code == 200, own.text
    assert own.json()["data"] == OWNER_SLICE


@pytest.mark.e2e
def test_context_builders_and_loadables_honor_withholding(user_scoped_world):
    """The planner entities section and code-execution loadables must resolve
    the snapshot per reader."""
    w = user_scoped_world

    async def check():
        from sqlalchemy import select
        from app.models.entity import Entity
        from app.models.user import User
        from app.models.organization import Organization
        from app.ai.context.builders.entity_context_builder import EntityContextBuilder
        from app.ai.code_execution.loadables import LoadablesResolver

        async with async_session_maker() as db:
            org = await db.get(Organization, w["org_id"])
            admin_u = await db.get(User, w["admin"]["user_id"])
            member_u = await db.get(User, w["member"]["user_id"])

            async def section_for(user):
                b = EntityContextBuilder(db, org, report=None, user=user)
                return await b.build(
                    keywords=["admins", "entity"], require_source_assoc=True,
                    data_source_ids=[w["ds_public"]["id"]],
                )

            owner_section = await section_for(admin_u)
            assert owner_section.items and owner_section.items[0].data, \
                "owner keeps their snapshot in context"
            member_section = await section_for(member_u)
            assert member_section.items and not member_section.items[0].data, \
                "non-owner must not get the snapshot in context"
            assert "owner-private-row" not in member_section.render()

            resolver = LoadablesResolver(db, org, report=None, current_user=member_u)
            out = await resolver.resolve(step_refs=[], entity_refs=[w["entity"]["id"]])
            assert not out["entities"], "loadables must refuse the withheld snapshot"
            assert out["errors"] and "credential" in out["errors"][0]

            owner_resolver = LoadablesResolver(db, org, report=None, current_user=admin_u)
            out2 = await owner_resolver.resolve(step_refs=[], entity_refs=[w["entity"]["id"]])
            assert w["entity"]["id"] in out2["entities"], "owner still loads their entity"

    _run(check())
