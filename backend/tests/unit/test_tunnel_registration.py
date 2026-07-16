"""Advertisement persistence — design A10/D2.

Drives register_advertisement against a real (sqlite) session from conftest and
checks the outcomes the Data Tunnels UI depends on: agent upsert, tunnel-mode
Connection rows with no credentials, withdrawal deactivation, and name-conflict
rejection.
"""
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.connection import Connection
from app.services.tunnel_registration_service import (
    register_advertisement,
    TunnelRegistrationService,
)


async def _org() -> str:
    org_id = str(uuid.uuid4())
    async with async_session_maker() as db:
        db.add(Organization(id=org_id, name=f"Org-{org_id[:8]}"))
        await db.commit()
    return org_id


def _ad(edge_agent_id="nyc-01", name_types=(("lego-pg", "postgresql"),), **extra):
    return {
        "edge_agent_id": edge_agent_id,
        "edge_agent_name": "NYC Office",
        "version": "1.0.0",
        "connections": [
            {"name": n, "type": t, "label": f"{n} label"} for n, t in name_types
        ],
        **extra,
    }


async def _connections(org_id):
    async with async_session_maker() as db:
        rows = (
            await db.execute(select(Connection).filter(Connection.organization_id == org_id))
        ).scalars().all()
        return {r.name: r for r in rows}


async def _agents(org_id):
    svc = TunnelRegistrationService()
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one()
        return await svc.list_agents(db, org)


@pytest.mark.asyncio
async def test_advertisement_creates_agent_and_tunnel_connections():
    org_id = await _org()

    await register_advertisement(org_id, _ad(name_types=(("lego-pg", "postgresql"), ("sp", "sharepoint"))))

    agents = await _agents(org_id)
    assert len(agents) == 1
    assert agents[0].edge_agent_id == "nyc-01"
    assert agents[0].status == "online"
    outcomes = {c["name"]: c["status"] for c in agents[0].last_advertisement["connections"]}
    assert outcomes == {"lego-pg": "registered", "sp": "registered"}

    conns = await _connections(org_id)
    assert set(conns) == {"lego-pg", "sp"}
    for c in conns.values():
        assert c.tunnel_mode is True
        assert c.edge_agent_id == "nyc-01"
        assert c.credentials is None  # never for a tunneled connection (D1)
        assert c.is_active is True


@pytest.mark.asyncio
async def test_re_advertisement_updates_in_place_not_duplicates():
    org_id = await _org()
    await register_advertisement(org_id, _ad())
    await register_advertisement(org_id, _ad())  # same again

    conns = await _connections(org_id)
    assert len(conns) == 1  # upsert, not a second row
    assert len(await _agents(org_id)) == 1


@pytest.mark.asyncio
async def test_withdrawn_connection_is_deactivated_not_deleted():
    org_id = await _org()
    await register_advertisement(org_id, _ad(name_types=(("lego-pg", "postgresql"), ("sp", "sharepoint"))))
    # sp drops out of the next advertisement
    await register_advertisement(org_id, _ad(name_types=(("lego-pg", "postgresql"),)))

    conns = await _connections(org_id)
    assert conns["sp"].is_active is False   # deactivated
    assert conns["lego-pg"].is_active is True
    assert "sp" in conns  # not deleted


@pytest.mark.asyncio
async def test_returning_connection_is_reactivated():
    org_id = await _org()
    await register_advertisement(org_id, _ad(name_types=(("lego-pg", "postgresql"),)))
    await register_advertisement(org_id, _ad(name_types=()))  # withdrawn
    assert (await _connections(org_id))["lego-pg"].is_active is False
    await register_advertisement(org_id, _ad(name_types=(("lego-pg", "postgresql"),)))  # back
    assert (await _connections(org_id))["lego-pg"].is_active is True


@pytest.mark.asyncio
async def test_name_conflict_across_agents_is_rejected_not_merged():
    org_id = await _org()
    await register_advertisement(org_id, _ad(edge_agent_id="nyc-01", name_types=(("shared", "postgresql"),)))
    await register_advertisement(org_id, _ad(edge_agent_id="tokyo-02", name_types=(("shared", "postgresql"),)))

    # the row still belongs to nyc-01 — not flapped to tokyo-02
    conns = await _connections(org_id)
    assert conns["shared"].edge_agent_id == "nyc-01"

    # tokyo-02's advertisement records the conflict
    tokyo = [a for a in await _agents(org_id) if a.edge_agent_id == "tokyo-02"][0]
    outcome = tokyo.last_advertisement["connections"][0]
    assert outcome["status"] == "conflict"
    assert "already claimed" in outcome["reason"]


@pytest.mark.asyncio
async def test_missing_edge_agent_id_is_ignored():
    org_id = await _org()
    await register_advertisement(org_id, {"connections": [{"name": "x", "type": "postgresql"}]})
    assert await _agents(org_id) == []


@pytest.mark.asyncio
async def test_unknown_org_is_ignored():
    # Subject named an org that does not exist here — nothing to attach to.
    await register_advertisement(str(uuid.uuid4()), _ad())  # must not raise
