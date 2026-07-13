"""Reproduction: a data source stays visible/selected in the prompt box and on
the report, yet contributes nothing to the running agent's context.

Symptom (reported): the report "does not have the right context, even though the
prompt box has it" — the agent answers that it is connected to *no* data source
while PromptBoxV2 still shows the source selected.

Root cause under test
---------------------
Two code paths disagree about whether a report-attached data source is live:

  * SELECTOR / REPORT SERIALIZATION keep a source based only on the *data
    source* lifecycle — ``DataSourceService.is_execution_live`` checks
    ``DataSource.is_active`` + ``publish_status`` and ignores connection health
    (data_source_service.py). ``filter_live_data_sources`` reuses it. So the
    prompt box (``/data_sources/active``) and ``GET /reports/{id}`` still show
    the source.

  * THE AGENT keeps a source only if ``construct_clients`` produced a live
    client (agent_v2.py `_has_client`, ~L313-320). ``construct_clients`` builds
    clients solely from *active* connections
    (``active_connections = [c for c in ds.connections if c.is_active]``,
    data_source_service.py ~L2132) — a connection flagged unhealthy
    (``Connection.is_active == False``) yields **no** client, so the source is
    silently dropped from the agent context.

Because ``Connection.is_active`` is a system-managed health flag that flips on a
failed connection test, a source can be fully "live" to the selector/report and
simultaneously invisible to the agent. This test pins that asymmetry.
"""
import uuid
from unittest.mock import patch

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.services.data_source_service import DataSourceService


class _DummyClient:
    """Stand-in DB client so construct_clients does not import a real driver."""
    def __init__(self, *a, **kw):
        pass


def _agent_has_client_filter(data_sources, clients):
    """Replica of AgentV2.__init__'s `_has_client` guard (agent_v2.py L313-320):
    the agent keeps only data sources that produced a client key of the form
    ``"{name}"`` or ``"{name}:{conn}"``.
    """
    def _has_client(ds):
        name = getattr(ds, "name", None)
        if not name:
            return False
        prefix = f"{name}:"
        return any(k == name or k.startswith(prefix) for k in clients)
    return [ds for ds in data_sources if _has_client(ds)]


async def _seed_report_with_connected_source(healthy: bool):
    """Create an org + one published/active data source wired to a single
    connection whose health (``is_active``) is set by ``healthy``.

    Returns (org_id, data_source_id).
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Ctx Org {suffix}")
        db.add(org)
        await db.flush()

        # A normal, human-published, healthy-*data source* (agent).
        ds = DataSource(
            name=f"Music Store {suffix}",
            organization_id=org.id,
            is_active=True,               # data-source lifecycle: live
            publish_status="published",
            reliability_status="training",
        )
        db.add(ds)
        await db.flush()

        conn = Connection(
            name=f"chinook {suffix}",
            type="postgres",
            config={"host": "localhost", "database": "chinook"},
            credentials=None,
            organization_id=org.id,
            is_active=healthy,            # connection HEALTH flag
            auth_policy="system_only",
        )
        db.add(conn)
        await db.flush()

        # Link data source <-> connection via the M:N junction table directly
        # (appending to the lazy relationship would trigger a sync lazy-load).
        from app.models.domain_connection import domain_connection
        await db.execute(
            domain_connection.insert().values(
                data_source_id=ds.id, connection_id=conn.id
            )
        )
        await db.commit()
        return str(org.id), str(ds.id)


async def _load_ds(ds_id):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    async with async_session_maker() as db:
        res = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.connections))
            .where(DataSource.id == ds_id)
        )
        return res.scalar_one()


@pytest.mark.asyncio
async def test_healthy_connection_source_reaches_agent_context():
    """Baseline: a source with a healthy connection is live to BOTH the
    selector/report AND the agent (construct_clients yields a client)."""
    org_id, ds_id = await _seed_report_with_connected_source(healthy=True)
    ds = await _load_ds(ds_id)

    # Selector / report serialization keep it.
    assert DataSourceService.is_execution_live(ds) is True

    svc = DataSourceService()
    async with async_session_maker() as db:
        with patch(
            "app.services.data_source_service.resolve_client_class",
            return_value=_DummyClient,
        ), patch.object(
            DataSourceService, "resolve_credentials_for_connection",
            return_value={},
        ):
            clients = await svc.construct_clients(db, ds, current_user=None)

    # Agent keeps it: a client key exists, so `_has_client` retains the source.
    assert clients, "healthy connection should produce a client"
    assert _agent_has_client_filter([ds], clients) == [ds]


@pytest.mark.asyncio
async def test_unhealthy_connection_source_kept_by_selector_but_dropped_by_agent():
    """Reproduction of the bug: flip the *connection* health to unhealthy.

    The selector/report still consider the source live (is_execution_live /
    filter_live_data_sources), so PromptBoxV2 keeps showing it — but
    construct_clients yields no client, so the agent's `_has_client` guard drops
    it and the running agent has no data source context.
    """
    org_id, ds_id = await _seed_report_with_connected_source(healthy=False)
    ds = await _load_ds(ds_id)

    # --- PROMPT BOX / REPORT SIDE: still live -------------------------------
    # This is what makes the prompt box keep showing the source.
    assert DataSourceService.is_execution_live(ds) is True

    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ds.organization_id)
        # Report GET serialization filters attached sources through this; the
        # user-independent leg (no current_user) keeps the source.
        live = await svc.filter_live_data_sources(
            db, [ds], current_user=None, organization=org
        )
    assert [d.id for d in live] == [ds.id], (
        "report/selector serialization should still surface the source"
    )

    # --- AGENT SIDE: no client, source dropped ------------------------------
    async with async_session_maker() as db:
        with patch(
            "app.services.data_source_service.resolve_client_class",
            return_value=_DummyClient,
        ), patch.object(
            DataSourceService, "resolve_credentials_for_connection",
            return_value={},
        ):
            clients = await svc.construct_clients(db, ds, current_user=None)

    assert clients == {}, (
        "unhealthy connection must yield no client (construct_clients skips "
        "inactive connections)"
    )
    # The agent's `_has_client` guard therefore removes the source entirely,
    # even though the prompt box still shows it selected. THIS is the mismatch.
    surviving = _agent_has_client_filter([ds], clients)
    assert surviving == [], (
        "BUG: source is live to the prompt box/report but the agent drops it, "
        "so the agent reports 'no data source connected'"
    )
