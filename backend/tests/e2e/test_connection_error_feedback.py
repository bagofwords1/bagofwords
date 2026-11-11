"""A failed connection test or schema refresh tells the admin *why*, safely.

The connection-status panel used to show either a bare "Internal server
error" (the test endpoint crashed while recording a failed probe) or a raw
exception string such as ``Indexing failed: '9f26…'`` (a ``KeyError`` with
an internal id). The contract now:

1. A failed probe always comes back as a readable result — never a 500 —
   even when the failure left the DB session unusable.
2. The message names what went wrong (exception type + text) but none of the
   connection's credentials, its server identity (host, IP) or a person's
   identity (username, email).
3. Agent sync after a refresh survives duplicate-name catalog rows (the
   ``KeyError`` behind the original report).

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
from app.models.organization import Organization
from app.models.user import User
from app.services.connection_service import ConnectionService
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


def _identity():
    """Fresh per-test values, so no assertion depends on one magic string."""
    s = uuid.uuid4().hex[:8]
    return {
        "host": f"qlik-{s}.corp.example.org",
        "ip": f"10.{int(s[:2], 16)}.{int(s[2:4], 16)}.{int(s[4:6], 16)}",
        "user": f"svc_{s}",
        "email": f"owner.{s}@example.org",
        "password": f"Pw!{s}x",
    }


async def _seed(ident, table_names=()):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Feedback Org {suffix}")
        db.add(org)
        await db.flush()
        owner = User(name="Owner", email=f"owner-{suffix}@example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        db.add(owner)
        await db.flush()
        conn = Connection(
            organization_id=org.id, name=f"Src {suffix}", type="postgresql",
            config={"host": ident["host"], "port": 5432, "database": "d"},
            auth_policy="system_only",
        )
        conn.encrypt_credentials({"user": ident["user"], "password": ident["password"]})
        db.add(conn)
        await db.flush()
        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id,
                        is_active=True, owner_user_id=owner.id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id))
        for tname in table_names:
            db.add(ConnectionTable(name=tname, connection_id=conn.id,
                                   columns=[{"name": "id", "dtype": "int"}],
                                   pks=[], fks=[], no_rows=0, metadata_json={}))
            await db.flush()
        await db.commit()
        return {"org_id": org.id, "ds_id": ds.id, "conn_id": conn.id}


async def _probe(ids):
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        return await ConnectionService().test_connection(
            db=db, connection_id=str(ids["conn_id"]), organization=org)


def _leaks(message, ident):
    """The secrets found in ``message``. The host and user are not secrets:
    they are the admin's own settings and what the message must still name."""
    return [k for k in ("password",) if ident[k] in message]


def _driver_error(ident):
    # The password is echoed bare, in no shape a pattern would recognise.
    return (
        f"could not connect to {ident['host']} ({ident['ip']}) as {ident['user']} "
        f"using {ident['password']} on behalf of {ident['email']}"
    )


@pytest.fixture
def install_client(monkeypatch):
    holder = {}

    async def _construct(self, db, connection, current_user=None, **kw):
        return await holder["build"](db)

    monkeypatch.setattr(ConnectionService, "construct_client", _construct)

    def _set(build):
        holder["build"] = build
    return _set


@pytest.mark.e2e
@pytest.mark.parametrize("exc_type", [ConnectionError, TimeoutError, KeyError, RuntimeError])
def test_failed_probe_names_the_error_without_the_password(install_client, exc_type):
    ident = _identity()
    ids = _run(_seed(ident))

    class _Client:
        async def atest_connection(self):
            raise exc_type(_driver_error(ident))

    async def _build(db):
        return _Client()
    install_client(_build)

    result = _run(_probe(ids))
    assert result["success"] is False
    assert exc_type.__name__ in result["message"]
    assert _leaks(result["message"], ident) == []
    assert ident["host"] in result["message"]


@pytest.mark.e2e
def test_failure_message_reported_by_client_is_redacted(install_client):
    ident = _identity()
    ids = _run(_seed(ident))

    class _Client:
        async def atest_connection(self):
            return {"success": False, "message": _driver_error(ident)}

    async def _build(db):
        return _Client()
    install_client(_build)

    result = _run(_probe(ids))
    assert result["success"] is False
    assert ident["host"] in result["message"]
    assert _leaks(result["message"], ident) == []


@pytest.mark.e2e
def test_probe_failure_that_breaks_the_session_is_still_reported(install_client):
    """A failure that poisons the session (a failed flush) used to make the
    endpoint's own bookkeeping commit raise -> bare 500. The admin must get
    the probe's result instead, and the failure must still be recorded."""
    ident = _identity()
    ids = _run(_seed(ident))

    async def _build(db):
        # name is NOT NULL: the flush fails and the session needs a rollback.
        db.add(Connection(organization_id=ids["org_id"], name=None, type="postgresql",
                          config={}, auth_policy="system_only"))
        await db.flush()
        raise AssertionError("unreachable")
    install_client(_build)

    result = _run(_probe(ids))
    assert result["success"] is False
    assert result["message"]
    assert _leaks(result["message"], ident) == []

    async def _status():
        async with async_session_maker() as db:
            return (await db.get(Connection, ids["conn_id"])).last_connection_status
    assert _run(_status()) == "not_connected"


@pytest.mark.e2e
@pytest.mark.parametrize("names", [["orders", "orders"], ["a", "b", "b", "b", "c"]])
def test_agent_sync_survives_duplicate_catalog_names(names):
    ids = _run(_seed(_identity(), table_names=names))

    async def _sync():
        async with async_session_maker() as db:
            conn = await db.get(Connection, ids["conn_id"])
            ds = await db.get(DataSource, ids["ds_id"])
            await DataSourceService().sync_domain_tables_from_connection(
                db, ds, conn, max_auto_select=None)
            rows = (await db.execute(
                select(DataSourceTable.name).where(DataSourceTable.datasource_id == ids["ds_id"])
            )).scalars().all()
            return rows
    rows = _run(_sync())
    # Every catalog name reaches the agent, once.
    assert sorted(rows) == sorted(set(names))


@pytest.mark.e2e
@pytest.mark.parametrize("linked", [0, 1])
def test_a_duplicate_name_keeps_the_table_the_agent_already_has(linked):
    """Of two catalog rows sharing a name, whichever the agent already links
    to is the one synced: no second same-named table appears beside it."""
    ids = _run(_seed(_identity(), table_names=["orders", "orders"]))

    async def _sync():
        async with async_session_maker() as db:
            twins = (await db.execute(
                select(ConnectionTable).where(ConnectionTable.connection_id == ids["conn_id"])
                .order_by(ConnectionTable.created_at, ConnectionTable.id)
            )).scalars().all()
            # No API produces a duplicate-name catalog (the service refuses
            # it); older installs hold them, so the link is written directly.
            existing = DataSourceTable(
                name="orders", datasource_id=ids["ds_id"], connection_table_id=twins[linked].id,
                is_active=True, columns=[], pks=[], fks=[], no_rows=0, metadata_json={})
            db.add(existing)
            await db.commit()
            existing_id = existing.id
            conn = await db.get(Connection, ids["conn_id"])
            ds = await db.get(DataSource, ids["ds_id"])
            await DataSourceService().sync_domain_tables_from_connection(
                db, ds, conn, max_auto_select=None)
            rows = (await db.execute(
                select(DataSourceTable.id).where(DataSourceTable.datasource_id == ids["ds_id"])
            )).scalars().all()
            return existing_id, rows
    existing_id, rows = _run(_sync())
    assert rows == [existing_id]
