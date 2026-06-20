"""
Per-agent channel availability gating.

An agent (data source) can be configured to be available only in a subset of the
org's connected channels via ``DataSource.channel_availability`` (a JSON map of
``{channel_type: bool}``). This validates:

  - default (NULL map)        -> available in every channel (no regression)
  - ``{"slack": false}``      -> excluded from Slack, still in Teams / MCP / web
  - the ``channel`` argument threaded into the two selector methods
    (``get_public_data_sources`` for channel mentions, ``get_active_data_sources``
    for DMs / MCP) actually filters.

No external services needed — pure DB + service logic on SQLite.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_channel_availability.py -v -s
"""
import uuid
import asyncio
from datetime import datetime

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


async def _seed(channel_availability):
    """Seed one public, published, system_only agent with the given
    channel_availability map. Returns (org_id, ds_id, admin_id)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Chan Org {suffix}")
        db.add(org)
        await db.flush()

        admin = User(
            name="Admin",
            email=f"admin-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(admin)
        await db.flush()

        conn = Connection(
            organization_id=org.id,
            name=f"PG {suffix}",
            type="postgresql",
            config={"host": "localhost", "database": "demo"},
            auth_policy="system_only",
        )
        conn.encrypt_credentials({"user": "u", "password": "p"})
        db.add(conn)
        await db.flush()

        ds = DataSource(
            name=f"Sales {suffix}",
            organization_id=org.id,
            is_active=True,
            is_public=True,
            publish_status="published",
            channel_availability=channel_availability,
            owner_user_id=admin.id,
        )
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id,
        ))
        await db.commit()
        return str(org.id), str(ds.id), str(admin.id)


async def _names_for_channel(org_id, channel, public=True, user_id=None):
    """Return the set of agent ids the selector exposes for `channel`."""
    svc = DataSourceService()
    async with async_session_maker() as db:
        from sqlalchemy import select
        org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        if public:
            items = await svc.get_public_data_sources(db, org, channel=channel)
        else:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            items = await svc.get_active_data_sources(db, org, user, channel=channel)
        return {it.id for it in items}


def test_model_is_available_in_defaults_to_available():
    ds = DataSource(name="x")
    # NULL map -> available everywhere
    assert ds.is_available_in("slack") is True
    assert ds.is_available_in(None) is True
    # explicit false only blocks that channel
    ds.channel_availability = {"slack": False}
    assert ds.is_available_in("slack") is False
    assert ds.is_available_in("teams") is True
    assert ds.is_available_in("mcp") is True
    # internal/web context is never gated
    assert ds.is_available_in(None) is True


def test_default_agent_available_in_all_channels():
    org_id, ds_id, admin_id = _run(_seed(channel_availability=None))
    for ch in ("slack", "teams", "mcp"):
        assert ds_id in _run(_names_for_channel(org_id, ch)), f"public/{ch}"
    # DM / MCP path
    assert ds_id in _run(_names_for_channel(org_id, "mcp", public=False, user_id=admin_id))
    print("[default] agent visible in slack/teams/mcp (public + active) ✓")


def test_disabled_channel_is_filtered_out():
    org_id, ds_id, admin_id = _run(_seed(channel_availability={"slack": False}))

    # Channel-mention selector (public): excluded from Slack, present elsewhere.
    assert ds_id not in _run(_names_for_channel(org_id, "slack"))
    assert ds_id in _run(_names_for_channel(org_id, "teams"))

    # DM / MCP selector (active): excluded from Slack, present in MCP + web (None).
    assert ds_id not in _run(_names_for_channel(org_id, "slack", public=False, user_id=admin_id))
    assert ds_id in _run(_names_for_channel(org_id, "mcp", public=False, user_id=admin_id))
    assert ds_id in _run(_names_for_channel(org_id, None, public=False, user_id=admin_id))
    print("[gated] agent hidden from slack, still visible in teams/mcp/web ✓")
