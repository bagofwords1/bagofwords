"""A mixed agent must not be offered for Slack channel mentions.

`get_public_data_sources` serves the agents a CHANNEL mention may use. Its own
docstring states the rule — "only ... system_only auth ... where we can't rely
on individual user credentials" — but it read the policy off
`data_source.connections[0]`:

    conn = d.connections[0] if d.connections else None
    auth_policy = conn.auth_policy if conn else "system_only"
    if auth_policy == "user_required":
        continue

So a MIXED agent (system_only warehouse first, delegated Power BI second)
answered "system_only" and was offered in the channel anyway. Channel members
who have not connected their own account get "Connect required" from an agent
the channel advertised, and an owner or admin mentioning it reaches the
system-credentials fallback in a public channel.

Same `connections[0]` root cause as the tables-selector bug, in the gate that
decides what a public channel may reach.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_channel_mixed_agent_gate.py -v -s
"""
import uuid
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


async def _agent(org_id, name, policies, base):
    """One public agent whose connections carry `policies`, in that order."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as db:
        ds = DataSource(name=f"{name} {suffix}", organization_id=org_id,
                        is_active=True, is_public=True)
        db.add(ds)
        await db.flush()
        for i, policy in enumerate(policies):
            c = Connection(
                organization_id=org_id, name=f"{name}-{i}-{suffix}",
                type="postgresql" if policy == "system_only" else "powerbi",
                config={}, auth_policy=policy,
                allowed_user_auth_modes=None if policy == "system_only" else ["oauth"],
                created_at=base + timedelta(minutes=i),
            )
            c.encrypt_credentials({"user": "u", "password": "p"})
            db.add(c)
            await db.flush()
            await db.execute(domain_connection.insert().values(
                data_source_id=ds.id, connection_id=c.id))
        await db.commit()
        return str(ds.id), ds.name


async def _seed():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with async_session_maker() as db:
        org = Organization(name=f"Channel Org {uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.commit()
        org_id = org.id
    out = {}
    # The delegated connection sorts SECOND — the ordering under which the old
    # gate read "system_only" and let the agent through.
    out["mixed"] = await _agent(org_id, "Mixed", ["system_only", "user_required"], base)
    # ...and FIRST, which the old gate happened to catch. Both must be skipped.
    out["mixed_delegated_first"] = await _agent(
        org_id, "MixedDelegatedFirst", ["user_required", "system_only"], base)
    out["open"] = await _agent(org_id, "OpenOnly", ["system_only", "system_only"], base)
    out["delegated"] = await _agent(org_id, "DelegatedOnly", ["user_required"], base)
    return org_id, out


async def _channel_agents(org_id):
    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        items = await DataSourceService().get_public_data_sources(
            db, org, channel="slack")
        return {i.name for i in items}


@pytest.mark.e2e
def test_channel_mentions_skip_any_agent_with_a_delegated_connection():
    org_id, agents = _run(_seed())
    offered = _run(_channel_agents(org_id))
    print(f"\noffered to a Slack channel: {sorted(offered)}")

    open_name = agents["open"][1]
    assert open_name in offered, "a fully system_only agent must still be offered"

    for key in ("mixed", "mixed_delegated_first", "delegated"):
        name = agents[key][1]
        assert name not in offered, (
            f"{name} has a user_required connection and must not be offered for "
            f"channel mentions — a channel cannot supply individual credentials"
        )


@pytest.mark.e2e
def test_the_legacy_type_field_still_renders():
    """The gate rebinds `conn` for the response's legacy single `type` field;
    dropping that binding would NameError the whole endpoint."""
    org_id, agents = _run(_seed())

    async def _items():
        async with async_session_maker() as db:
            org = await db.get(Organization, org_id)
            return await DataSourceService().get_public_data_sources(
                db, org, channel="slack")

    items = _run(_items())
    assert items, "expected the open agent to be listed"
    assert all(getattr(i, "type", None) == "postgresql" for i in items)
