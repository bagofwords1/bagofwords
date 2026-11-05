"""A source that is briefly unreachable must not cost the customer anything.

Two behaviors, both reported on Qlik Sense on-prem (whose Engine is busy or
mid-reload for part of every night) but implemented for every connector:

1. Status: one failed health probe used to deactivate a shared connection,
   which drops its tables from every agent's context ("Qlik is
   disconnected"). Now a connection is deactivated only after
   ``BOW_CONN_DEACTIVATE_AFTER_FAILURES`` failures in a row; a success
   reactivates it and resets the count.

2. Reindex: a catalog crawler that cannot open one part of its catalog (one
   Qlik app) returns nothing for it. The reindex used to prune those tables,
   cascading away the agent's selection, so the next good run recreated them
   inactive ("re-activate the Qlik objects every day"). Parts the client
   reports through ``unindexed_table_prefixes()`` now keep their tables.

Only the network client is faked; services, models and the DB run for real.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.membership import Membership
from app.models.notification import SOURCE_CONNECTION
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.user import User
from app.services.connection_service import ConnectionService
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


def _table(name):
    return {
        "name": name,
        "columns": [{"name": "id", "dtype": "int"}],
        "pks": [], "fks": [], "metadata_json": {},
    }


class _FakeClient:
    """A crawler that returns `names` and reports `unread` as unreadable."""

    def __init__(self, names=(), unread=(), probe_ok=True):
        self._names = list(names)
        self._unread = list(unread)
        self._probe_ok = probe_ok

    async def aget_schemas(self, progress_callback=None, prior_catalog=None, **kw):
        return [_table(n) for n in self._names]

    def unindexed_table_prefixes(self):
        return list(self._unread)

    async def atest_connection(self):
        if self._probe_ok:
            return {"success": True, "message": "ok"}
        return {"success": False, "message": "Read timed out"}


@pytest.fixture
def fake_client(monkeypatch):
    """Install a fake client for every ConnectionService call; returns a setter."""
    holder = {"client": _FakeClient()}

    async def _construct(self, db, connection, current_user=None, **kw):
        # Real credential resolution, so the crawl is classified as the org
        # identity exactly as the scheduled reindex is in production.
        await self.resolve_credentials(db, connection, current_user)
        return holder["client"]

    monkeypatch.setattr(ConnectionService, "construct_client", _construct)

    def _set(**kw):
        holder["client"] = _FakeClient(**kw)

    return _set


async def _seed(table_names, active=True):
    """A shared (system_only) connection on one agent, with its catalog indexed
    and every table selected on the agent."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Transient Org {suffix}")
        db.add(org)
        await db.flush()
        owner = User(name="Owner", email=f"owner-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        db.add(owner)
        await db.flush()

        conn = Connection(
            organization_id=org.id,
            name=f"Src {suffix}",
            type="postgresql",
            config={"host": "127.0.0.1", "port": 5432, "database": "d"},
            auth_policy="system_only",
        )
        conn.encrypt_credentials({"user": "svc", "password": "p"})
        db.add(conn)
        await db.flush()

        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id,
                        is_active=True, owner_user_id=owner.id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id))

        for tname in table_names:
            ct = ConnectionTable(name=tname, connection_id=conn.id,
                                 columns=[{"name": "id", "dtype": "int"}],
                                 pks=[], fks=[], no_rows=0, metadata_json={})
            db.add(ct)
            await db.flush()
            db.add(DataSourceTable(
                name=tname, datasource_id=ds.id, connection_table_id=ct.id,
                is_active=active, metadata_json={},
                columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0))
        await db.commit()
        return {"org_id": org.id, "ds_id": ds.id, "conn_id": conn.id}


async def _reindex(ids):
    """What the scheduled reindex does: refresh the catalog, then sync the agent."""
    svc = ConnectionService()
    async with async_session_maker() as db:
        conn = await db.get(Connection, ids["conn_id"])
        await svc.refresh_schema(db=db, connection=conn, current_user=None)
    async with async_session_maker() as db:
        conn = await db.get(Connection, ids["conn_id"])
        ds = await db.get(DataSource, ids["ds_id"])
        await DataSourceService().sync_domain_tables_from_connection(
            db, ds, conn, max_auto_select=DataSourceService.ONBOARDING_MAX_TABLES)
        await db.commit()


async def _agent_tables(ids):
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(DataSourceTable.name, DataSourceTable.is_active)
            .where(DataSourceTable.datasource_id == ids["ds_id"])
        )).all()
        return {name: bool(active) for name, active in rows}


async def _catalog(ids):
    async with async_session_maker() as db:
        return set((await db.execute(
            select(ConnectionTable.name).where(ConnectionTable.connection_id == ids["conn_id"])
        )).scalars().all())


async def _probe(ids):
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        await ConnectionService().test_connection(
            db=db, connection_id=str(ids["conn_id"]), organization=org)


async def _probe_agent(ids):
    """The agent-level "Test connection" (tests every connection of the agent)."""
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        await DataSourceService().test_data_source_connection(
            db=db, data_source_id=str(ids["ds_id"]), organization=org, current_user=None)


async def _active_flags(ids):
    async with async_session_maker() as db:
        conn = await db.get(Connection, ids["conn_id"])
        ds = await db.get(DataSource, ids["ds_id"])
        return bool(conn.is_active), bool(ds.is_active)


# ---------------------------------------------------------------------------
# Reindex: unread parts keep their tables and the agent's selection
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.parametrize("unread_app", ["Finance/Budget", "Ops/Fleet Monitor"])
def test_unread_part_keeps_its_tables_and_agent_selection(fake_client, unread_app):
    readable = "Sales/Orders"
    ids = _run(_seed([
        f"{readable}/Orders", f"{readable}/Customers",
        f"{unread_app}/Facts", f"{unread_app}/Master Items",
    ]))

    # The unread app comes back as nothing but its placeholder row.
    fake_client(
        names=[f"{readable}/Orders", f"{readable}/Customers", unread_app],
        unread=[unread_app],
    )
    _run(_reindex(ids))

    tables = _run(_agent_tables(ids))
    assert tables.get(f"{unread_app}/Facts") is True
    assert tables.get(f"{unread_app}/Master Items") is True
    assert tables.get(f"{readable}/Orders") is True
    # No placeholder next to the tables that were kept.
    assert unread_app not in _run(_catalog(ids))
    assert unread_app not in tables

    # Next run reads the app again: nothing to re-activate.
    fake_client(names=[
        f"{readable}/Orders", f"{readable}/Customers",
        f"{unread_app}/Facts", f"{unread_app}/Master Items",
    ])
    _run(_reindex(ids))
    tables = _run(_agent_tables(ids))
    assert all(tables[n] for n in (f"{unread_app}/Facts", f"{unread_app}/Master Items"))


@pytest.mark.e2e
def test_tables_missing_from_a_part_that_was_read_are_still_pruned(fake_client):
    ids = _run(_seed([
        "Sales/Orders/Orders", "Sales/Orders/Legacy",
        "HR/People/Staff", "HR/People Archive/Old",
    ]))

    fake_client(names=["Sales/Orders/Orders", "HR/People"], unread=["HR/People"])
    _run(_reindex(ids))

    catalog = _run(_catalog(ids))
    assert "Sales/Orders/Legacy" not in catalog, "a read part's dropped table must be pruned"
    assert "HR/People/Staff" in catalog
    # A prefix covers names under it, not names that merely start with it.
    assert "HR/People Archive/Old" not in catalog


@pytest.mark.e2e
def test_unread_part_with_no_prior_tables_is_recorded_by_its_placeholder(fake_client):
    ids = _run(_seed(["Sales/Orders/Orders"]))

    fake_client(names=["Sales/Orders/Orders", "Finance/New App"], unread=["Finance/New App"])
    _run(_reindex(ids))

    assert "Finance/New App" in _run(_catalog(ids))


@pytest.mark.e2e
def test_without_unread_parts_missing_tables_are_pruned(fake_client):
    ids = _run(_seed(["A/App/T1", "A/App/T2"]))
    fake_client(names=["A/App/T1"])
    _run(_reindex(ids))
    assert _run(_catalog(ids)) == {"A/App/T1"}


# ---------------------------------------------------------------------------
# Status: deactivate only after consecutive failures
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.parametrize("threshold", [2, 4])
def test_connection_is_deactivated_only_after_consecutive_failures(
    fake_client, monkeypatch, threshold
):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", str(threshold))
    ids = _run(_seed(["t"]))

    fake_client(probe_ok=False)
    for _ in range(threshold - 1):
        _run(_probe(ids))
        assert _run(_active_flags(ids))[0] is True

    _run(_probe(ids))
    assert _run(_active_flags(ids))[0] is False

    fake_client(probe_ok=True)
    _run(_probe(ids))
    assert _run(_active_flags(ids))[0] is True


@pytest.mark.e2e
def test_a_success_resets_the_failure_count(fake_client, monkeypatch):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "3")
    ids = _run(_seed(["t"]))

    for ok in (False, False, True, False, False):
        fake_client(probe_ok=ok)
        _run(_probe(ids))
    assert _run(_active_flags(ids))[0] is True


@pytest.mark.e2e
def test_default_threshold_tolerates_a_single_failure(fake_client, monkeypatch):
    monkeypatch.delenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", raising=False)
    ids = _run(_seed(["t"]))
    fake_client(probe_ok=False)
    _run(_probe(ids))
    assert _run(_active_flags(ids))[0] is True


@pytest.mark.e2e
def test_agent_test_follows_the_connection_not_one_probe(fake_client, monkeypatch):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "2")
    ids = _run(_seed(["t"]))

    fake_client(probe_ok=False)
    _run(_probe_agent(ids))
    assert _run(_active_flags(ids)) == (True, True)

    _run(_probe_agent(ids))
    assert _run(_active_flags(ids)) == (False, False)

    fake_client(probe_ok=True)
    _run(_probe_agent(ids))
    assert _run(_active_flags(ids)) == (True, True)


# ---------------------------------------------------------------------------
# Health alerts: the automated sweep tells the connection's admins when a
# shared connection goes down and when it recovers — once per outage, never
# from a human-triggered test, and without email spam when a source flaps.
# Only the network client and the outbound mail transport are faked.
# ---------------------------------------------------------------------------


class _Outbox:
    """Stands in for the global SMTP transport and records what went out."""

    def __init__(self):
        self.sent = []
        self.fail_next = 0

    async def send_message(self, message):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("smtp unavailable")
        self.sent.append(message)

    def subjects(self):
        return [m.subject for m in self.sent]


@pytest.fixture
def outbox(monkeypatch):
    from app.settings.config import settings
    box = _Outbox()
    monkeypatch.setattr(settings, "email_client", box)
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "3")
    return box


async def _add_admin(org_id):
    """A real org admin — the audience a connection-down alert must reach."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        admin = User(name="Admin", email=f"admin-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        db.add(admin)
        await db.flush()
        db.add(Membership(user_id=admin.id, organization_id=org_id, role="admin"))
        await db.commit()
        return str(admin.id)


async def _conn_alerts(user_id, ntype):
    """The user's live (not deleted) connection alerts of one type."""
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Notification).where(
                Notification.user_id == str(user_id),
                Notification.source == SOURCE_CONNECTION,
                Notification.type == ntype,
                Notification.deleted_at.is_(None),
            )
        )).scalars().all()
        return rows


async def _probe_as(ids, user_id):
    """A human clicking "Test connection" — the probe carries a current_user."""
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, user_id)
        await ConnectionService().test_connection(
            db=db, connection_id=str(ids["conn_id"]), organization=org, current_user=user)


async def _set_connection(ids, **values):
    async with async_session_maker() as db:
        conn = await db.get(Connection, ids["conn_id"])
        for k, v in values.items():
            setattr(conn, k, v)
        await db.commit()


async def _set_org_alert_email(org_id, enabled):
    from app.models.organization_settings import OrganizationSettings
    from app.services.connection_alerts import EMAIL_ENABLED_KEY
    async with async_session_maker() as db:
        row = (await db.execute(select(OrganizationSettings).where(
            OrganizationSettings.organization_id == str(org_id)))).scalar_one_or_none()
        if row is None:
            row = OrganizationSettings(organization_id=str(org_id), config={})
            db.add(row)
        row.config = {**(row.config or {}), EMAIL_ENABLED_KEY: enabled}
        await db.commit()


def _go_down(fake_client, ids, probes=3):
    fake_client(probe_ok=False)
    for _ in range(probes):
        _run(_probe(ids))


def _recover(fake_client, ids):
    fake_client(probe_ok=True)
    _run(_probe(ids))


@pytest.mark.e2e
@pytest.mark.db
def test_automated_probe_alerts_admins_on_down_then_restore(fake_client, outbox):
    ids = _run(_seed(["Sales/Orders"]))
    admin_id = _run(_add_admin(ids["org_id"]))

    # A sustained outage deactivates the shared connection.
    _go_down(fake_client, ids)
    assert _run(_active_flags(ids))[0] is False

    # The admin is told once, as an error, by inbox and email — linked to an
    # agent they can open.
    downs = _run(_conn_alerts(admin_id, "connection_down"))
    assert len(downs) == 1
    assert downs[0].severity == "error"
    assert downs[0].link == f"/agents/{ids['ds_id']}"
    assert len(outbox.sent) == 1

    # Still down across more probes: no new alert.
    _go_down(fake_client, ids)
    assert len(_run(_conn_alerts(admin_id, "connection_down"))) == 1
    assert len(outbox.sent) == 1

    # Recovery is announced once, and the "is down" row is retired.
    _recover(fake_client, ids)
    assert _run(_active_flags(ids))[0] is True
    assert len(_run(_conn_alerts(admin_id, "connection_restored"))) == 1
    assert _run(_conn_alerts(admin_id, "connection_down")) == []
    assert len(outbox.sent) == 2


@pytest.mark.e2e
@pytest.mark.db
def test_manual_test_never_alerts_but_the_sweep_still_announces(fake_client, outbox):
    """A person diagnosing with the Test button must not spray the admins —
    but if their test is what deactivated the connection, the outage must
    still be announced by the next automated probe."""
    ids = _run(_seed(["Sales/Orders"]))
    admin_id = _run(_add_admin(ids["org_id"]))

    fake_client(probe_ok=False)
    for _ in range(3):
        _run(_probe_as(ids, admin_id))
    assert _run(_active_flags(ids))[0] is False
    assert _run(_conn_alerts(admin_id, "connection_down")) == []
    assert outbox.sent == []

    # The next automated probe (still failing) announces the outage.
    _run(_probe(ids))
    assert len(_run(_conn_alerts(admin_id, "connection_down"))) == 1
    assert len(outbox.sent) == 1


@pytest.mark.e2e
@pytest.mark.db
def test_flapping_connection_sends_one_down_and_one_restored_email(fake_client, outbox):
    ids = _run(_seed(["Sales/Orders"]))
    admin_id = _run(_add_admin(ids["org_id"]))

    for _ in range(3):  # down / up three times inside the cooldown window
        _go_down(fake_client, ids)
        _recover(fake_client, ids)

    # One down email, and only the matching recovery email.
    assert len(outbox.sent) == 2
    # The inbox still tracks every change: the latest state is "back online".
    assert len(_run(_conn_alerts(admin_id, "connection_restored"))) == 1
    assert _run(_conn_alerts(admin_id, "connection_down")) == []


@pytest.mark.e2e
@pytest.mark.db
def test_failed_down_email_does_not_silence_the_next_outage(fake_client, outbox):
    ids = _run(_seed(["Sales/Orders"]))
    _run(_add_admin(ids["org_id"]))

    outbox.fail_next = 2  # the send and its one retry both fail
    _go_down(fake_client, ids)
    assert outbox.sent == []

    # Recovery: nobody was emailed about the outage, so no recovery email.
    _recover(fake_client, ids)
    assert outbox.sent == []

    # The next outage (inside the would-be cooldown) is emailed.
    _go_down(fake_client, ids)
    assert len(outbox.sent) == 1


@pytest.mark.e2e
@pytest.mark.db
@pytest.mark.parametrize("org_on,conn_on", [(False, True), (True, False)])
def test_alert_email_needs_both_org_and_connection_switches(fake_client, outbox, org_on, conn_on):
    ids = _run(_seed(["Sales/Orders"]))
    admin_id = _run(_add_admin(ids["org_id"]))
    _run(_set_org_alert_email(ids["org_id"], org_on))
    _run(_set_connection(ids, alert_emails_enabled=conn_on))

    _go_down(fake_client, ids)
    _recover(fake_client, ids)

    # The inbox is always delivered; the email is not.
    assert len(_run(_conn_alerts(admin_id, "connection_restored"))) == 1
    assert outbox.sent == []
