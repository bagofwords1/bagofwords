"""Priority column titles reach the agent of a member on per-user credentials.

A Priority connection can require each member's own token
(`auth_policy=user_required`). Such a member's schema context is built from
their per-user overlay: it decides which forms and columns they see, while the
canonical catalog (indexed with the service token) says what they are. Free-text
descriptions are deliberately NOT carried from the canonical catalog into the
overlay, because the system identity may reach more than the member does. A
Priority column title is not free text, though: it is the form column's fixed
label, identical for every user who can open the form. Without it, the member's
agent sees bare names (ORDNAME, CDES) and cannot map "customer" or "לקוח" to
a column.

The test indexes the catalog as the admin, then has a member save their own PAT
and refresh their schema through the API, and reads exactly what the planner is
handed for that member.
"""
import asyncio
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.organization import Organization
from app.models.user import User
from tests.e2e.test_priority_subforms import priority_mock  # noqa: F401  (fixture)


def _run(coro):
    return asyncio.run(coro)


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _activate(ds_id, deadline_s=90.0):
    """Wait for the catalog indexing that data-source creation starts, then
    select every table for the agent. Written to the DB directly: the tables
    are still being indexed when this runs, so there is no stable set to send
    to the selection endpoint yet."""
    end = time.monotonic() + deadline_s
    while True:
        async with async_session_maker() as db:
            rows = (await db.execute(
                select(DataSourceTable).where(DataSourceTable.datasource_id == ds_id)
            )).scalars().all()
            if any(r.name == "ORDERS" for r in rows) or time.monotonic() > end:
                for r in rows:
                    r.is_active = True
                    db.add(r)
                await db.commit()
                return [r.name for r in rows]
        await asyncio.sleep(0.2)


async def _context_for(ds_id, org_id, user_id):
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    async with async_session_maker() as db:
        ds = (await db.execute(
            select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.id == ds_id)
        )).scalar_one()
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        ctx = await SchemaContextBuilder(
            db=db, data_sources=[ds], organization=org, report=None, user=user
        ).build(with_stats=False)
        metas = [t.metadata_json for d in (ctx.data_sources if ctx else []) for t in d.tables]
        return (ctx.render() if ctx else ""), metas


@pytest.mark.e2e
def test_member_on_own_token_sees_priority_titles(
    priority_mock, test_client, bootstrap_admin, invite_user_to_org, create_connection, grant_resource,  # noqa: F811
):
    mock, root = priority_mock
    mock._calls.clear()
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    conn = create_connection(
        name="priority-per-user", type="priority_erp",
        config={"service_root": root}, credentials={"pat": "DEMOTOKEN"},
        auth_policy="user_required", allowed_user_auth_modes=["pat"],
        user_token=admin["token"], org_id=org_id,
    )
    r = test_client.post("/api/data_sources", headers=_hdr(admin["token"], org_id), json={
        "name": "priority-agent", "connection_ids": [conn["id"]],
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
    })
    assert r.status_code == 200, r.text
    ds_id = r.json()["id"]
    indexed = _run(_activate(ds_id))
    assert "ORDERS" in indexed, f"catalog indexing did not finish in time; indexed: {indexed}"

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    g = grant_resource(resource_type="data_source", resource_id=ds_id, principal_type="user",
                       principal_id=member["user_id"], permissions=["view"],
                       user_token=admin["token"], org_id=org_id)
    assert g.status_code == 200, g.json()
    mh = _hdr(member["token"], org_id)
    r = test_client.post(f"/api/data_sources/{ds_id}/my-credentials", headers=mh,
                         json={"auth_mode": "pat", "credentials": {"pat": "DEMOTOKEN"}})
    assert r.status_code == 200, r.text
    r = test_client.post(f"/api/connections/{conn['id']}/my-schema/refresh", headers=mh)
    assert r.status_code == 200, r.text
    _run(_activate(ds_id))

    ctx, metas = _run(_context_for(ds_id, org_id, member["user_id"]))
    # client-only bookkeeping that names columns (hidden ones too) never
    # reaches the prompt tables — the client reads it from the DB itself
    hidden = ("column_titles", "columns", "path", "mandatory")
    assert metas and all(k not in (m or {}).get("priority_erp", {}) for m in metas for k in hidden)
    assert 'name="ORDNAME"' in ctx, "the member's overlay was not used to build the context"
    # column titles, carried from the canonical catalog as structured metadata
    assert 'name="ORDNAME" dtype="Edm.String" description="Order Number"' in ctx
    assert 'description="Customer Name"' in ctx
    # the form title, and the note telling the planner to answer with titles
    assert 'title="Customer Orders"' in ctx
    assert "<presentation_note>" in ctx
