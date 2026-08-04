"""Cross-agent targeting: with several agents in one session, an instruction
lands on the RIGHT one.

Mechanism under test (create_instruction tool): the model names tables; the
tool resolves each name to its owning data source WITHIN the report's agents.
So a rule about Agent B's tables attaches to Agent B alone — and a rule naming
no tables attaches to every agent in the session, never silently to the whole
org. The "intelligence" is the model naming the right tables (covered by the
real-LLM eval); this pins the deterministic half so a targeting bug can't hide
behind the model.
"""
import uuid
from types import SimpleNamespace

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _seed_agent_with_table(org_id, name, table):
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.datasource_table import DataSourceTable

    async with async_session_maker() as db:
        ds = DataSource(name=name, organization_id=str(org_id), is_active=True)
        db.add(ds)
        await db.flush()
        db.add(DataSourceTable(name=table, datasource_id=ds.id, columns=[], pks=[], fks=[]))
        await db.commit()
        return str(ds.id)


async def _create(*, org_id, user_id, ds_ids, table_names=None):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    async with async_session_maker() as db:
        ctx = {
            "db": db, "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id), "mode": "knowledge",
            "report": SimpleNamespace(id=str(uuid.uuid4()),
                                      data_sources=[SimpleNamespace(id=d) for d in ds_ids]),
        }
        payload = {"text": "Orders exclude cancelled rows in every revenue metric.",
                   "category": "general", "confidence": 0.9, "evidence": "User confirmed."}
        if table_names is not None:
            payload["table_names"] = table_names
        out = None
        async for evt in CreateInstructionTool().run_stream(payload, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                out = evt.payload["output"]
        return out


async def _attached_ds_ids(iid):
    """Attachment set straight from the association table — what's under test
    is the targeting, not HTTP visibility of draft suggestions (raw-seeded
    data sources here lack the access plumbing the GET route checks)."""
    from app.dependencies import async_session_maker
    from sqlalchemy import select
    from app.models.instruction import instruction_data_source_association as assoc

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(assoc.c.data_source_id).where(assoc.c.instruction_id == str(iid))
        )).scalars().all()
    return {str(r) for r in rows}


@pytest.fixture
def admin(create_user, login_user, whoami):
    email = f"target_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return {"token": token, "user_id": me["id"], "org_id": me["organizations"][0]["id"]}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_table_named_rule_attaches_only_to_owning_agent(admin):
    org, uid = admin["org_id"], admin["user_id"]
    ds_a = await _seed_agent_with_table(org, f"crm-{uuid.uuid4().hex[:4]}", "customers")
    ds_b = await _seed_agent_with_table(org, f"shop-{uuid.uuid4().hex[:4]}", "orders")

    out = await _create(org_id=org, user_id=uid, ds_ids=[ds_a, ds_b], table_names=["orders"])
    assert out["success"] is True, out

    attached = await _attached_ds_ids(out["instruction_id"])
    assert attached == {ds_b}, (
        f"a rule about 'orders' must attach to the orders agent only, got {attached}"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_unscoped_rule_attaches_to_all_session_agents_not_globally(admin):
    org, uid = admin["org_id"], admin["user_id"]
    ds_a = await _seed_agent_with_table(org, f"crm-{uuid.uuid4().hex[:4]}", "customers")
    ds_b = await _seed_agent_with_table(org, f"shop-{uuid.uuid4().hex[:4]}", "orders")

    out = await _create(org_id=org, user_id=uid, ds_ids=[ds_a, ds_b])  # no table_names
    assert out["success"] is True, out

    attached = await _attached_ds_ids(out["instruction_id"])
    assert attached == {ds_a, ds_b}, (
        "an unscoped rule must fall back to the SESSION's agents (both), "
        f"never become org-global: got {attached}"
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_foreign_table_name_cannot_pull_in_an_out_of_session_agent(admin):
    """Naming a table that belongs to an agent OUTSIDE the session must not
    widen the attachment beyond the session's agents."""
    org, uid = admin["org_id"], admin["user_id"]
    ds_a = await _seed_agent_with_table(org, f"crm-{uuid.uuid4().hex[:4]}", "customers")
    ds_out = await _seed_agent_with_table(org, f"ext-{uuid.uuid4().hex[:4]}", "invoices")

    out = await _create(org_id=org, user_id=uid, ds_ids=[ds_a], table_names=["invoices"])
    assert out["success"] is True, out

    attached = await _attached_ds_ids(out["instruction_id"])
    assert ds_out not in attached, "out-of-session agent must never be attached"
    assert attached == {ds_a}, (
        f"unresolvable table names fall back to the session's agents: {attached}"
    )
